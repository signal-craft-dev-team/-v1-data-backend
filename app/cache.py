"""
hardware_id → machine_id 메모리 캐시.

앱 시작 시 DB에서 edge_sensor 전체를 한 번 로드.
이후 모든 모듈에서 DB 조회 없이 사용.
"""
import logging
import asyncpg

log = logging.getLogger(__name__)

# {hardware_id: {"sensor_id": uuid, "machine_id": uuid}}
_cache: dict[str, dict] = {}


async def load(conn: asyncpg.Connection) -> None:
    """앱 시작 시 1회 호출 — edge_sensor 전체 로드."""
    global _cache
    rows = await conn.fetch(
        "SELECT hardware_id, id AS sensor_id, machine_id FROM edge_sensor"
    )
    _cache = {
        row["hardware_id"]: {
            "sensor_id":  row["sensor_id"],
            "machine_id": row["machine_id"],
        }
        for row in rows
    }
    log.info(f"캐시 로드 완료: {len(_cache)}개 센서")


def get(hardware_id: str) -> dict | None:
    """hardware_id → {sensor_id, machine_id} 반환. 없으면 None."""
    return _cache.get(hardware_id)


def machine_id(hardware_id: str):
    """hardware_id → machine_id 직접 반환. 없으면 None."""
    entry = _cache.get(hardware_id)
    return entry["machine_id"] if entry else None


def size() -> int:
    return len(_cache)
