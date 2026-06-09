"""
data_backend 엔트리포인트.

실행:
  # 연결 및 캐시 테스트
  python -m app.main --test

  # 배치 모드 (로컬 과거 데이터 적재)
  python -m app.main --mode batch --start 2026-05-26 --end 2026-06-08

  # 스케줄 모드 (Cloud Run / VM)
  python -m app.main --mode schedule
"""
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from app import cache
from app.db import create_pool
from app import collector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def test_connections():
    """DB 연결 + 캐시 로드 테스트."""
    log.info("── 연결 테스트 시작 ──")

    # DB 연결
    log.info("PostgreSQL 연결 중...")
    pool = await create_pool(os.getenv("DATABASE_URL", ""))
    log.info("PostgreSQL 연결 성공")

    # 캐시 로드
    async with pool.acquire() as conn:
        await cache.load(conn)

    log.info(f"캐시 로드 완료: {cache.size()}개 센서")

    # 샘플 출력
    from app import cache as c
    items = list(c._cache.items())
    for hw_id, info in items:
        log.info(f"  {hw_id} → machine_id: {info['machine_id']}")

    # collector 테스트
    from datetime import date
    async with pool.acquire() as conn:
        servers = await collector.get_active_servers(conn)
        log.info(f"활성 서버: {len(servers)}개")
        for server in servers[:1]:  # 첫 번째 서버만 테스트
            log.info(f"  서버: {server['hostname']}")
            files = await collector.collect(
                conn,
                server["hostname"],
                str(server["id"]),
                date.today(),
            )
            log.info(f"  오늘 신규 파일: {len(files)}개")
            if files:
                log.info(f"  첫 파일: {files[0]['filename']}")

    await pool.close()
    log.info("── 테스트 완료 ──")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",  action="store_true", help="연결 및 캐시 테스트")
    parser.add_argument("--mode",  choices=["batch", "schedule"], default=None)
    parser.add_argument("--start", default=None, help="배치 시작일 YYYY-MM-DD")
    parser.add_argument("--end",   default=None, help="배치 종료일 YYYY-MM-DD")
    args = parser.parse_args()

    if args.test:
        asyncio.run(test_connections())
    elif args.mode == "batch":
        log.info("배치 모드 — 추후 구현")
    elif args.mode == "schedule":
        log.info("스케줄 모드 — 추후 구현")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
