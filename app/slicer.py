"""
WAV 슬라이싱 모듈.

MongoDB sensor_map 조회 → GCS 다운로드 → /tmp/{uuid}/ 슬라이싱 저장
분석 완료 후 즉시 삭제.
"""
import io
import logging
import os
import shutil
import tempfile
import uuid
from datetime import date
from pathlib import Path

import numpy as np
import soundfile as sf
from google.cloud import storage

from shared.features import extract
from shared.analyzer import analyze_slice

log = logging.getLogger(__name__)

GCS_BUCKET  = os.getenv("GCS_BUCKET", "signalcraft-audio-bucket")
MONGODB_URI = os.getenv("MONGODB_URI", "")

# MongoDB 클라이언트 (data_backend 자체 구현)
from pymongo import MongoClient

_DB_NAME   = "signalcraft-firestore"
_COLL_NAME = "audio_upload_logs"


def _mongo_client() -> MongoClient:
    return MongoClient(MONGODB_URI)


def _gcs_client() -> storage.Client:
    return storage.Client()


# ── sensor_map 조회 ───────────────────────────────────────────────

def get_sensor_map(filename: str) -> dict[str, str] | None:
    """파일명으로 MongoDB에서 sensor_map 조회."""
    import re
    client = _mongo_client()
    coll   = client[_DB_NAME][_COLL_NAME]
    doc    = coll.find_one({"gcs_path": {"$regex": re.escape(filename) + "$"}})
    if not doc:
        return None
    sensor_map = doc.get("sensor_map")
    return dict(sensor_map) if sensor_map and isinstance(sensor_map, dict) else None


# ── GCS 다운로드 ──────────────────────────────────────────────────

def download_wav(
    server_key: str,
    target_date: date,
    filename: str,
    cache_dir: str | None = None,
) -> bytes | None:
    """
    WAV bytes 반환.
    cache_dir 지정 시 로컬 캐시 우선, 없으면 GCS 다운로드.
    """
    if cache_dir:
        local = Path(cache_dir) / server_key / target_date.isoformat() / filename
        if local.exists():
            return local.read_bytes()
        log.debug(f"로컬 캐시 없음, GCS 시도: {filename}")

    try:
        client = _gcs_client()
        path   = f"{server_key}/{target_date.isoformat()}/{filename}"
        return client.bucket(GCS_BUCKET).blob(path).download_as_bytes()
    except Exception as e:
        log.warning(f"GCS 다운로드 실패: {filename} — {e}")
        return None


# ── WAV 슬라이싱 ──────────────────────────────────────────────────

def _split(audio: np.ndarray, sensor_map: dict[str, str]) -> dict[str, np.ndarray]:
    """단채널 WAV를 sensor_map 순서대로 균등 분할."""
    n = len(sensor_map)
    if n == 0:
        return {}
    size = len(audio) // n
    return {
        hw_id: audio[i * size : (i + 1) * size]
        for i, hw_id in enumerate(sensor_map.keys())
    }


# ── 분석 단위 처리 ────────────────────────────────────────────────

def process(file_info: dict, cache_dir: str | None = None) -> list[dict] | None:
    """
    파일 1개에 대해 슬라이싱 + 분석 실행.

    Returns:
        [
          {
            "hardware_id": str,
            "machine_id":  uuid,
            "result": {
              "operational_state": str,
              "operational_score": float,
              "current_state":     str,
              "features":          dict,
            }
          }
        ]
        또는 None (sensor_map 없음 / 다운로드 실패)
    """
    filename   = file_info["filename"]
    server_key = file_info["server_key"]
    target_date = file_info["date"]

    # 1. sensor_map 조회
    sensor_map = get_sensor_map(filename)
    if not sensor_map:
        log.debug(f"sensor_map 없음: {filename}")
        return None

    # 2. WAV 다운로드 (로컬 캐시 우선)
    wav_bytes = download_wav(server_key, target_date, filename, cache_dir)
    if not wav_bytes:
        return None

    # 3. 슬라이싱
    tmp_dir = Path(tempfile.mkdtemp(prefix="sc_"))
    try:
        with io.BytesIO(wav_bytes) as buf:
            audio, sample_rate = sf.read(buf, dtype="float32")

        slices = _split(audio, sensor_map)

        # 4. 센서별 분석
        results = []
        for hw_id, audio_slice in slices.items():
            # cache에서 machine_id 조회
            from app import cache
            machine_id = cache.machine_id(hw_id)
            if not machine_id:
                log.debug(f"캐시 miss: {hw_id}")
                continue

            result = analyze_slice(audio_slice, hw_id)
            results.append({
                "hardware_id": hw_id,
                "machine_id":  machine_id,
                "result":      result,
            })

        return results if results else None

    finally:
        # 5. tmp 즉시 삭제
        shutil.rmtree(tmp_dir, ignore_errors=True)
