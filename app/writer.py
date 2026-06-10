"""
DB 저장 모듈.

순서 중요:
  1. machine_status_history INSERT (id 생성)
  2. machine_status UPSERT (last_machine_status_history_id 참조)
"""
import json
import logging
from datetime import datetime
from uuid import uuid4

import numpy as np
from shared.analyzer import FEATURE_META, THRESHOLDS

log = logging.getLogger(__name__)


def _parse_recorded_at(filename: str) -> datetime:
    """
    파일명에서 실제 녹음 시각 추출.
    20260526_071119.wav → 2026-05-26 07:11:19 UTC
    파싱 실패 시 현재 시각 반환.
    """
    try:
        stem = filename.replace(".wav", "")
        return datetime.strptime(stem, "%Y%m%d_%H%M%S")
    except ValueError:
        log.warning(f"파일명 파싱 실패, now() 사용: {filename}")
        return datetime.utcnow()


class _NumpyEncoder(json.JSONEncoder):
    """numpy 타입을 Python 기본 타입으로 변환."""
    def default(self, obj):
        if isinstance(obj, np.floating):
            return round(float(obj), 4)
        if isinstance(obj, np.integer):
            return int(obj)
        return super().default(obj)

import asyncpg

log = logging.getLogger(__name__)


async def write(
    pool: asyncpg.Pool,
    file_info: dict,
    sensor_results: list[dict],
) -> int:
    """
    센서 분석 결과를 DB에 저장.

    Args:
        pool:           asyncpg connection pool
        file_info:      collector에서 반환된 파일 정보
        sensor_results: slicer.process() 반환값

    Returns:
        저장된 레코드 수
    """
    if not sensor_results:
        return 0

    saved = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in sensor_results:
                machine_id = str(item["machine_id"])
                hw_id      = item["hardware_id"]
                result     = item["result"]

                history_id = await _insert_history(
                    conn,
                    machine_id=machine_id,
                    sensor_id=hw_id,
                    result=result,
                    filename=file_info["filename"],
                )

                await _upsert_status(
                    conn,
                    machine_id=machine_id,
                    result=result,
                    history_id=history_id,
                )

                saved += 1
                log.debug(
                    f"  저장: {hw_id} "
                    f"{result['operational_state']} "
                    f"score={result['operational_score']:.3f}"
                )

    return saved


def _build_optional_text(sensor_id: str, features: dict) -> dict:
    """
    optional_text 구조 생성.

    {
      "description":     {feature: {"key": 주파수범위, "value": 임계값설명}},
      "current_value":   {feature: 실측값},
      "threshold_value": {feature: 임계값}
    }
    """
    # 해당 센서의 판별 피처 메타
    sensor_meta  = {**FEATURE_META.get("_global", {}), **FEATURE_META.get(sensor_id, {})}
    sensor_thresh = {
        "b0_60 (global off)": f"> {THRESHOLDS['global_off_b0_60']}%",
        **{
            k: (f"< {v}%" if k in ("b0_60", "b6k_16k", "b60_200") else f"> {v}%")
            for k, v in THRESHOLDS.get(sensor_id, {}).items()
        },
    }

    description = {
        feat: {"key": meta["key"], "value": meta["value"]}
        for feat, meta in sensor_meta.items()
    }

    return {
        "description":     description,
        "current_value":   {k: float(v) for k, v in features.items()},
        "threshold_value": sensor_thresh,
    }


async def _insert_history(
    conn: asyncpg.Connection,
    machine_id: str,
    sensor_id: str,
    result: dict,
    filename: str,
) -> str:
    """machine_status_history INSERT → history_id 반환."""
    history_id = str(uuid4())

    optional_text = json.dumps(
        _build_optional_text(sensor_id, result.get("features", {})),
        cls=_NumpyEncoder,
        ensure_ascii=False,
    )

    recorded_at = _parse_recorded_at(filename)

    await conn.execute(
        """
        INSERT INTO machine_status_history (
            id, machine_id,
            operational_state, operational_score,
            current_state,
            optional_text, related_file_name,
            recorded_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        history_id,
        machine_id,
        result["operational_state"],
        result["operational_score"],
        result["current_state"],
        optional_text,
        filename,
        recorded_at,
    )
    return history_id


async def _upsert_status(
    conn: asyncpg.Connection,
    machine_id: str,
    result: dict,
    history_id: str,
) -> None:
    """machine_status UPSERT."""
    await conn.execute(
        """
        INSERT INTO machine_status (
            machine_id,
            operational_state, operational_score,
            current_state,
            last_machine_status_history_id,
            updated_at
        ) VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (machine_id) DO UPDATE SET
            operational_state              = EXCLUDED.operational_state,
            operational_score              = EXCLUDED.operational_score,
            current_state                  = EXCLUDED.current_state,
            last_machine_status_history_id = EXCLUDED.last_machine_status_history_id,
            updated_at                     = now()
        """,
        machine_id,
        result["operational_state"],
        result["operational_score"],
        result["current_state"],
        history_id,
    )
