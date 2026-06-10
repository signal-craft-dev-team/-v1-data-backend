"""
data_backend 엔트리포인트.

모드:
  test     — DB/GCS 연결 확인 + 지정 파일 수만큼 분석 (--max)
  local    — 날짜 범위 전체 분석 (개수 제한 없음)
  schedule — 최신 파일만 분석 (Cloud Run / VM 주기 실행)

실행 예시:
  python -m app.main --mode test    --max 5
  python -m app.main --mode local   --start 2026-05-26 --end 2026-06-08
  python -m app.main --mode schedule
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from app import cache
from app.db import create_pool
from tqdm import tqdm
from app import collector
from app import slicer
from app import writer
from app import interpolator

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── 공통: 초기화 ─────────────────────────────────────────────────

async def init(pool):
    """앱 시작 시 1회 실행 — 캐시 로드."""
    async with pool.acquire() as conn:
        await cache.load(conn)
    log.info(f"캐시 로드: {cache.size()}개 센서")


# ── test 모드 ────────────────────────────────────────────────────

async def run_test(pool, max_files: int):
    log.info(f"── TEST 모드 (최대 {max_files}개) ──")

    async with pool.acquire() as conn:
        servers = await collector.get_active_servers(conn)

    log.info(f"활성 서버: {len(servers)}개")

    count = 0
    for server in servers:
        if count >= max_files:
            break

        async with pool.acquire() as conn:
            files = await collector.collect(
                conn,
                server["hostname"],
                str(server["id"]),
                date.today(),
            )

        log.info(f"[{server['hostname']}] 오늘 파일: {len(files)}개")

        for file_info in files:
            if count >= max_files:
                break

            results = slicer.process(file_info)
            if results is None:
                log.info(f"  스킵: {file_info['filename']}")
                continue

            saved = await writer.write(pool, file_info, results)
            log.info(f"  {file_info['filename']}  (저장: {saved}건)")
            for r in results:
                state = r["result"]["operational_state"]
                score = r["result"]["operational_score"]
                icon  = "🟢" if state == "running" else "⚫"
                log.info(f"    {icon} {r['hardware_id']}  {state}  score={score:.3f}")

            count += 1

    log.info(f"── TEST 완료 ({count}개 처리) ──")


# ── local 모드 ───────────────────────────────────────────────────

async def run_local(pool, start: date, end: date, cache_dir: str | None = None):
    log.info(f"── LOCAL 모드 ({start} ~ {end}) ──")
    if cache_dir:
        log.info(f"로컬 캐시 사용: {cache_dir}")

    async with pool.acquire() as conn:
        servers = await collector.get_active_servers(conn)

    total = saved_total = skipped = errors = 0
    current = start

    while current <= end:
        for server in servers:
            async with pool.acquire() as conn:
                files = await collector.collect(
                    conn, server["hostname"], str(server["id"]), current,
                )

            if not files:
                current += timedelta(days=1)
                continue

            # 날짜별 sensor_map 일괄 프리패치
            slicer.prefetch_sensor_maps(server["hostname"], current)

            with tqdm(files, desc=str(current), unit="file", ncols=80, leave=False) as pbar:
                last_ok = None
                for file_info in pbar:
                    total += 1
                    pbar.set_postfix_str(file_info["filename"][:20])
                    try:
                        results = slicer.process(file_info, cache_dir)
                        if results is None:
                            skipped += 1
                            continue
                        saved = await writer.write(pool, file_info, results)
                        saved_total += saved
                        last_ok = file_info["filename"]
                    except Exception as e:
                        log.error(f"처리 실패: {file_info['filename']} — {e}")
                        errors += 1

                if last_ok:
                    async with pool.acquire() as conn:
                        await collector.update_bookmark(
                            conn, str(server["id"]), last_ok
                        )

        log.info(
            f"[{current}] 완료 — "
            f"저장: {saved_total} / 스킵: {skipped} / 오류: {errors}"
        )
        current += timedelta(days=1)

    log.info(
        f"── LOCAL 완료 ──\n"
        f"  총 파일: {total} / 저장: {saved_total} / "
        f"스킵: {skipped} / 오류: {errors}"
    )
    corrected = await interpolator.run(pool)
    log.info(f"  보간 보정: {corrected}개")


# ── schedule 모드 ────────────────────────────────────────────────

async def run_schedule(pool):
    log.info("── SCHEDULE 모드 ──")

    async with pool.acquire() as conn:
        servers = await collector.get_active_servers(conn)

    total = 0
    for server in servers:
        server_key = server["hostname"]
        today      = date.today()

        # 1. bookmark 조회
        async with pool.acquire() as conn:
            bookmark = await collector.get_latest_bookmark(conn, str(server["id"]))

        # 2. bookmark + 3분 윈도우로 처리 대상 파일 선택
        files = collector.get_schedule_files(server_key, today, bookmark)
        if not files:
            log.info(f"[{server_key}] 신규 파일 없음 → 스킵")
            continue

        log.info(f"[{server_key}] 처리 대상: {len(files)}개 (bookmark: {bookmark})")

        # 3. 파일별 분석 → DB 저장 → bookmark 갱신 (성공/실패 무관)
        for filename in files:
            file_info = {
                "filename":   filename,
                "server_key": server_key,
                "date":       today,
            }
            results = slicer.process(file_info)

            if results:
                await writer.write(pool, file_info, results)
                total += 1

            async with pool.acquire() as conn:
                await collector.update_bookmark(conn, str(server["id"]), filename)

        log.info(f"[{server_key}] 완료 → bookmark: {files[-1]}")

    log.info(f"── SCHEDULE 완료 ({total}개) ──")
    await interpolator.run(pool)


# ── 진입점 ───────────────────────────────────────────────────────

async def async_main(args):
    pool = await create_pool(os.getenv("DATABASE_URL", ""))
    await init(pool)

    try:
        if args.mode == "test":
            await run_test(pool, args.max)
        elif args.mode == "local":
            start = datetime.strptime(args.start, "%Y-%m-%d").date()
            end   = datetime.strptime(args.end,   "%Y-%m-%d").date()
            await run_local(pool, start, end, args.cache_dir)
        elif args.mode == "schedule":
            await run_schedule(pool)
    finally:
        await pool.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="data_backend")
    parser.add_argument("--mode",  required=True,
                        choices=["test", "local", "schedule"],
                        help="test: 제한 분석 / local: 전체 배치 / schedule: 최신 파일")
    parser.add_argument("--max",   type=int, default=5,
                        help="[test] 처리할 최대 파일 수 (기본: 5)")
    parser.add_argument("--start", default=None,
                        help="[local] 시작일 YYYY-MM-DD")
    parser.add_argument("--end",       default=None,
                        help="[local] 종료일 YYYY-MM-DD")
    parser.add_argument("--cache-dir", default=None, dest="cache_dir",
                        help="[local] 로컬 캐시 경로 (GCS 재다운로드 방지)")
    args = parser.parse_args()

    if args.mode == "local" and (not args.start or not args.end):
        parser.error("local 모드는 --start, --end 필수")

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
