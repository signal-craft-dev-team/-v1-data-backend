"""
보간 모듈.

조건: 앞/뒤 1분 이내 running 존재 + 현재 stopped → running으로 보정
예외: 앞 또는 뒤로 1시간 이상 데이터 공백 → 보간 안 함

보정된 행: optional_text에 {"interpolated": true, "reason": "flanked_by_running"} 추가
"""
import json
import logging

import asyncpg

log = logging.getLogger(__name__)

WINDOW_SEC  = 60     # 앞뒤 탐색 범위 (초)
GAP_SEC     = 3600   # 이 이상 공백이면 보간 안 함


async def run(pool: asyncpg.Pool, machine_ids: list[str] | None = None) -> int:
    """
    보간 실행.

    Args:
        machine_ids: 특정 machine_id만 처리. None이면 전체.

    Returns:
        보정된 레코드 수
    """
    total = 0
    async with pool.acquire() as conn:
        targets = await _get_stopped_targets(conn, machine_ids)
        log.info(f"보간 대상 (stopped): {len(targets)}개")

        for row in targets:
            corrected = await _interpolate_one(conn, row)
            if corrected:
                total += 1

    if total:
        log.info(f"보간 완료: {total}개 → running 보정")
    return total


# ── 대상 조회 ─────────────────────────────────────────────────────

async def _get_stopped_targets(
    conn: asyncpg.Connection,
    machine_ids: list[str] | None,
) -> list[dict]:
    """
    보간 후보: stopped 이면서 아직 보간되지 않은 최근 이력.
    """
    base_query = """
        SELECT id, machine_id, recorded_at, optional_text
        FROM machine_status_history
        WHERE operational_state = 'stopped'
          AND interpolated = false
    """
    if machine_ids:
        placeholders = ", ".join(f"${i+1}" for i in range(len(machine_ids)))
        query = base_query + f" AND machine_id = ANY(ARRAY[{placeholders}]::uuid[])"
        rows = await conn.fetch(query, *machine_ids)
    else:
        rows = await conn.fetch(base_query)

    return [dict(r) for r in rows]


# ── 보간 판정 ─────────────────────────────────────────────────────

async def _interpolate_one(conn: asyncpg.Connection, row: dict) -> bool:
    """
    단일 stopped 행에 대해 보간 여부 판단 + 적용.

    Returns:
        True if corrected
    """
    machine_id  = str(row["machine_id"])
    recorded_at = row["recorded_at"]

    # 앞 1분 이내 가장 가까운 running
    prev = await conn.fetchrow(
        """
        SELECT recorded_at, operational_state
        FROM machine_status_history
        WHERE machine_id = $1
          AND recorded_at < $2
          AND recorded_at >= $2 - INTERVAL '1 minute'
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        machine_id, recorded_at,
    )

    # 뒤 1분 이내 가장 가까운 running
    next_ = await conn.fetchrow(
        """
        SELECT recorded_at, operational_state
        FROM machine_status_history
        WHERE machine_id = $1
          AND recorded_at > $2
          AND recorded_at <= $2 + INTERVAL '1 minute'
        ORDER BY recorded_at ASC
        LIMIT 1
        """,
        machine_id, recorded_at,
    )

    # 둘 다 running이 아니면 보간 안 함
    if not prev or prev["operational_state"] != "running":
        return False
    if not next_ or next_["operational_state"] != "running":
        return False

    # 1시간 이상 공백 체크
    prev_gap  = (recorded_at - prev["recorded_at"]).total_seconds()
    next_gap  = (next_["recorded_at"] - recorded_at).total_seconds()
    if prev_gap > GAP_SEC or next_gap > GAP_SEC:
        log.debug(f"공백 초과 스킵: {row['id']} (앞 {prev_gap:.0f}s / 뒤 {next_gap:.0f}s)")
        return False

    # 보간 적용
    await _apply(conn, row)
    return True


async def _apply(conn: asyncpg.Connection, row: dict) -> None:
    """stopped → running 보정 + optional_text 갱신."""
    existing = {}
    if row["optional_text"]:
        try:
            existing = json.loads(row["optional_text"])
        except Exception:
            pass

    existing["reason"] = "flanked_by_running"

    await conn.execute(
        """
        UPDATE machine_status_history
        SET operational_state = 'running',
            interpolated      = true,
            optional_text     = $1
        WHERE id = $2
        """,
        json.dumps(existing, ensure_ascii=False),
        str(row["id"]),
    )

    # machine_status도 동기화 (해당 machine의 최신 상태가 이 행이면)
    await conn.execute(
        """
        UPDATE machine_status
        SET operational_state = 'running',
            updated_at        = now()
        WHERE machine_id = $1
          AND last_machine_status_history_id = $2
        """,
        str(row["machine_id"]),
        str(row["id"]),
    )
