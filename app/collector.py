"""
GCS 신규 파일 수집 + edge_sensor bookmark 관리.

- 활성 edge_server / edge_sensor 목록 조회
- 센서별 last_processed_file 이후 신규 파일 반환
- 처리 완료 시 last_processed_file 갱신
"""
import logging
import os
from datetime import date, datetime

import asyncpg
from google.cloud import storage

log = logging.getLogger(__name__)

GCS_BUCKET = os.getenv("GCS_BUCKET", "signalcraft-audio-bucket")


def _gcs_client() -> storage.Client:
    return storage.Client()


# ── DB 조회 ──────────────────────────────────────────────────────

async def get_active_servers(conn: asyncpg.Connection) -> list[dict]:
    """is_active=true 인 edge_server 목록."""
    rows = await conn.fetch(
        "SELECT id, hostname, customer_id FROM edge_server WHERE is_active = true"
    )
    return [dict(r) for r in rows]


async def get_sensors_by_server(
    conn: asyncpg.Connection, server_id: str
) -> list[dict]:
    """서버에 속한 센서 목록 + bookmark."""
    rows = await conn.fetch(
        """
        SELECT id, hardware_id, machine_id, last_processed_file
        FROM edge_sensor
        WHERE server_id = $1
        """,
        server_id,
    )
    return [dict(r) for r in rows]


# ── GCS 파일 목록 ─────────────────────────────────────────────────

def list_wav_files(server_key: str, target_date: date) -> list[str]:
    """GCS에서 특정 날짜의 WAV 파일명 목록 (정렬)."""
    client = _gcs_client()
    prefix = f"{server_key}/{target_date.isoformat()}/"
    blobs  = client.list_blobs(GCS_BUCKET, prefix=prefix)
    return sorted(
        blob.name.split("/")[-1]
        for blob in blobs
        if blob.name.endswith(".wav")
    )


def list_new_files(
    server_key: str,
    target_date: date,
    last_processed_file: str | None,
) -> list[str]:
    """
    last_processed_file 이후의 신규 파일 목록.
    last_processed_file=None 이면 해당 날짜 전체 반환.
    """
    all_files = list_wav_files(server_key, target_date)

    if not last_processed_file:
        return all_files

    # last_processed_file 이후 파일만 반환
    try:
        idx = all_files.index(last_processed_file)
        return all_files[idx + 1:]
    except ValueError:
        # bookmark 파일이 목록에 없으면 전체 반환
        log.warning(f"bookmark 파일 없음: {last_processed_file} — 전체 반환")
        return all_files


# ── 신규 파일 수집 (서버 + 날짜 단위) ────────────────────────────

async def collect(
    conn: asyncpg.Connection,
    server_key: str,
    server_id: str,
    target_date: date,
) -> list[dict]:
    """
    서버 + 날짜 기준 신규 파일 목록 반환.

    Returns:
        [
          {
            "filename": str,
            "server_key": str,
            "date": date,
            "sensors": [
              {"hardware_id": str, "machine_id": uuid, "sensor_id": uuid}
            ]
          }
        ]
    """
    sensors = await get_sensors_by_server(conn, server_id)
    if not sensors:
        return []

    # 센서 중 가장 오래된 bookmark 기준으로 신규 파일 수집
    oldest_bookmark = None
    for s in sensors:
        lp = s.get("last_processed_file")
        if lp is None:
            oldest_bookmark = None
            break
        if oldest_bookmark is None or lp < oldest_bookmark:
            oldest_bookmark = lp

    new_files = list_new_files(server_key, target_date, oldest_bookmark)
    if not new_files:
        return []

    log.info(f"[{server_key}] {target_date} 신규 파일: {len(new_files)}개")

    sensor_info = [
        {
            "hardware_id": s["hardware_id"],
            "machine_id":  s["machine_id"],
            "sensor_id":   s["id"],
        }
        for s in sensors
    ]

    return [
        {
            "filename":   fn,
            "server_key": server_key,
            "date":       target_date,
            "sensors":    sensor_info,
        }
        for fn in new_files
    ]


# ── Bookmark 갱신 ─────────────────────────────────────────────────

async def update_bookmark(
    conn: asyncpg.Connection,
    server_id: str,
    filename: str,
) -> None:
    """처리 완료된 파일명으로 해당 서버의 모든 센서 bookmark 갱신."""
    await conn.execute(
        """
        UPDATE edge_sensor
        SET last_processed_file = $1
        WHERE server_id = $2
          AND (last_processed_file IS NULL OR last_processed_file < $1)
        """,
        filename,
        server_id,
    )
