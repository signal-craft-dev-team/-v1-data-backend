"""
WAV 슬라이싱 모듈.

MongoDB sensor_map 조회 → WAV 로드 → 슬라이싱 → FFT 분석
"""
import io
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import numpy as np
import soundfile as sf
from google.cloud import storage
from pymongo import MongoClient

from shared.features import extract
from shared.analyzer import analyze_slice

log = logging.getLogger(__name__)

GCS_BUCKET  = os.getenv("GCS_BUCKET", "signalcraft-audio-bucket")
MONGODB_URI = os.getenv("MONGODB_URI", "")

_DB_NAME   = "signalcraft-firestore"
_COLL_NAME = "audio_upload_logs"

# ── 싱글톤 클라이언트 ─────────────────────────────────────────────
_mongo: MongoClient | None = None
_gcs:   storage.Client | None = None


def _mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(MONGODB_URI)
        log.debug("MongoDB 클라이언트 생성")
    return _mongo


def _gcs_client() -> storage.Client:
    global _gcs
    if _gcs is None:
        _gcs = storage.Client()
        log.debug("GCS 클라이언트 생성")
    return _gcs


# ── sensor_map 일괄 조회 (날짜 단위 캐싱) ────────────────────────

_sensor_map_cache: dict[str, dict | None] = {}  # {filename: sensor_map}


def prefetch_sensor_maps(server_key: str, target_date: date) -> int:
    """
    특정 날짜의 sensor_map 전체를 한 번에 조회해서 메모리 캐싱.
    파일별 개별 조회 대신 이 함수를 날짜 처리 전에 1회 호출.

    Returns: 캐싱된 문서 수
    """
    t0     = time.perf_counter()
    prefix = f"{server_key}/{target_date.isoformat()}/"
    coll   = _mongo_client()[_DB_NAME][_COLL_NAME]
    docs   = coll.find({"gcs_path": {"$regex": f"^{re.escape(prefix)}"}})

    count = 0
    for doc in docs:
        gcs_path   = doc.get("gcs_path", "")
        filename   = gcs_path.split("/")[-1]
        sensor_map = doc.get("sensor_map")
        _sensor_map_cache[filename] = (
            dict(sensor_map) if sensor_map and isinstance(sensor_map, dict) else None
        )
        count += 1

    elapsed = time.perf_counter() - t0
    log.info(f"  [prefetch]  {count}개 sensor_map 캐싱 ({elapsed:.2f}s)")
    return count


def prefetch_sensor_maps_for_files(
    server_key: str,
    target_date: date,
    filenames: list[str],
) -> int:
    """처리 대상 파일 목록만 한 번에 조회해서 캐싱."""
    if not filenames:
        return 0

    t0   = time.perf_counter()
    coll = _mongo_client()[_DB_NAME][_COLL_NAME]
    paths = [f"{server_key}/{target_date.isoformat()}/{fn}" for fn in filenames]
    docs  = coll.find({"gcs_path": {"$in": paths}})

    count = 0
    for doc in docs:
        fn         = doc.get("gcs_path", "").split("/")[-1]
        sensor_map = doc.get("sensor_map")
        _sensor_map_cache[fn] = (
            dict(sensor_map) if sensor_map and isinstance(sensor_map, dict) else None
        )
        count += 1

    elapsed = time.perf_counter() - t0
    log.debug(f"[sensor_map] 일괄 프리패치 {count}/{len(filenames)}개 ({elapsed:.2f}s)")
    return count


def get_sensor_map(filename: str) -> dict[str, str] | None:
    """캐시에서 즉시 조회. 없으면 MongoDB 단건 조회."""
    if filename in _sensor_map_cache:
        result = _sensor_map_cache[filename]
        log.debug(f"  [sensor_map] 캐시 히트: {filename}")
        return result

    # 캐시 미스 → 단건 조회 (fallback)
    t0   = time.perf_counter()
    coll = _mongo_client()[_DB_NAME][_COLL_NAME]
    doc  = coll.find_one({"gcs_path": {"$regex": re.escape(filename) + "$"}})
    elapsed = time.perf_counter() - t0

    if not doc:
        log.debug(f"  [sensor_map] 없음: {filename} ({elapsed:.2f}s)")
        _sensor_map_cache[filename] = None
        return None

    sensor_map = doc.get("sensor_map")
    result = dict(sensor_map) if sensor_map and isinstance(sensor_map, dict) else None
    _sensor_map_cache[filename] = result
    log.debug(f"  [sensor_map] DB조회 {len(result) if result else 0}개 ({elapsed:.2f}s)")
    return result


# ── WAV 다운로드 ──────────────────────────────────────────────────

def download_wav(
    server_key: str,
    target_date: date,
    filename: str,
    cache_dir: str | None = None,
) -> bytes | None:
    t0 = time.perf_counter()

    if cache_dir:
        local = Path(cache_dir) / server_key / target_date.isoformat() / filename
        if local.exists():
            data = local.read_bytes()
            log.debug(f"  [wav_load]  캐시 {len(data):,}B ({time.perf_counter()-t0:.2f}s)")
            return data

    try:
        client = _gcs_client()
        path   = f"{server_key}/{target_date.isoformat()}/{filename}"
        data   = client.bucket(GCS_BUCKET).blob(path).download_as_bytes()
        log.debug(f"  [wav_load]  GCS {len(data):,}B ({time.perf_counter()-t0:.2f}s)")
        return data
    except Exception as e:
        log.warning(f"  [wav_load]  실패: {filename} — {e}")
        return None


# ── WAV 슬라이싱 ──────────────────────────────────────────────────

def _split(audio: np.ndarray, sensor_map: dict[str, str]) -> dict[str, np.ndarray]:
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
    파일 1개에 대해 sensor_map 조회 → WAV 로드 → 슬라이싱 → FFT 분석.

    Returns:
        [{"hardware_id": str, "machine_id": uuid, "result": dict}]
        또는 None
    """
    filename    = file_info["filename"]
    server_key  = file_info["server_key"]
    target_date = file_info["date"]
    t_start     = time.perf_counter()

    log.debug(f"[process] {filename}")

    # 1. sensor_map
    sensor_map = get_sensor_map(filename)
    if not sensor_map:
        return None

    # 2. WAV 로드
    wav_bytes = download_wav(server_key, target_date, filename, cache_dir)
    if not wav_bytes:
        return None

    # 3. 슬라이싱 + 분석
    t_fft = time.perf_counter()
    tmp_dir = Path(tempfile.mkdtemp(prefix="sc_"))
    try:
        with io.BytesIO(wav_bytes) as buf:
            audio, sample_rate = sf.read(buf, dtype="float32")

        slices = _split(audio, sensor_map)

        results = []
        for hw_id, audio_slice in slices.items():
            from app import cache
            machine_id = cache.machine_id(hw_id)
            if not machine_id:
                continue
            result = analyze_slice(audio_slice, hw_id)
            results.append({
                "hardware_id": hw_id,
                "machine_id":  machine_id,
                "result":      result,
            })

        log.debug(
            f"  [fft]       {len(results)}개 센서 ({time.perf_counter()-t_fft:.2f}s) "
            f"/ 총 {time.perf_counter()-t_start:.2f}s"
        )
        return results if results else None

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
