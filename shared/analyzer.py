"""
분석 로직 원본 — Training에서 편집, make sync으로 serving에 반영.

⚠ serving/analyzer.py 는 직접 편집하지 마세요. 이 파일을 수정하세요.

기준: reports/260609_REPORT.md (2026-06-09)
"""
import numpy as np
from shared.features import extract

# ── 센서 ID → 기기명 매핑 ─────────────────────────────────────────
SENSOR_NAMES = {
    "3C0F02E3B604": "배큠펌프",
    "3C0F02E3B654": "진공오븐챔버",
    "A0F262EC9088": "오일펌프",
    "A0F262EC9388": "워터칠러",
}

# ── 판별 임계값 (REPORT 기준, 직접 수정하여 튜닝) ─────────────────
THRESHOLDS = {
    # 전체 OFF 공통 기준
    "global_off_b0_60": 20.0,

    # 배큠펌프: b0_60 < 5% AND b6k_16k < 5%
    "3C0F02E3B604": {"b0_60": 5.0, "b6k_16k": 5.0},

    # 진공오븐챔버: b1k_3k > 45%
    "3C0F02E3B654": {"b1k_3k": 45.0},

    # 오일펌프: b60_200 < 8% AND b1k_3k > 40%
    "A0F262EC9088": {"b60_200": 8.0, "b1k_3k": 40.0},

    # 워터칠러: b3k_6k > 15%
    "A0F262EC9388": {"b3k_6k": 15.0},
}


# ── 센서별 판별 규칙 ──────────────────────────────────────────────

def _is_running(sensor_id: str, f: dict) -> bool:
    """피처 dict → 해당 센서 running 여부."""
    # 전체 OFF 우선 체크
    if f["b0_60"] > THRESHOLDS["global_off_b0_60"]:
        return False

    t = THRESHOLDS.get(sensor_id)
    if t is None:
        return False

    if sensor_id == "3C0F02E3B604":
        return f["b0_60"] < t["b0_60"] and f["b6k_16k"] < t["b6k_16k"]
    elif sensor_id == "3C0F02E3B654":
        return f["b1k_3k"] > t["b1k_3k"]
    elif sensor_id == "A0F262EC9088":
        return f["b60_200"] < t["b60_200"] and f["b1k_3k"] > t["b1k_3k"]
    elif sensor_id == "A0F262EC9388":
        return f["b3k_6k"] > t["b3k_6k"]

    return False


def _operational_score(sensor_id: str, f: dict) -> float:
    """판별 핵심 피처 기반 0.0~1.0 점수."""
    try:
        if sensor_id == "3C0F02E3B604":
            return round(max(0.0, min(1.0, 1 - f["b0_60"] / 20.0)), 4)
        elif sensor_id == "3C0F02E3B654":
            return round(max(0.0, min(1.0, f["b1k_3k"] / 65.0)), 4)
        elif sensor_id == "A0F262EC9088":
            return round(max(0.0, min(1.0, f["b1k_3k"] / 52.0)), 4)
        elif sensor_id == "A0F262EC9388":
            return round(max(0.0, min(1.0, f["b3k_6k"] / 38.0)), 4)
    except Exception:
        pass
    return 0.0


# ── 공개 API ─────────────────────────────────────────────────────

def analyze_slice(audio: np.ndarray, sensor_id: str) -> dict:
    """
    단일 센서 슬라이스 분석.

    Args:
        audio:     1D numpy array (float32, 단채널)
        sensor_id: hardware_id (예: 'A0F262EC9388')

    Returns:
        {
          "operational_state": "running" | "stopped" | "unknown",
          "operational_score": float,   # 0.0~1.0
          "current_state":     "unknown",
          "features":          dict,    # FFT 피처값
        }
    """
    features = extract(audio)

    if features is None:
        return {
            "operational_state": "unknown",
            "operational_score": 0.0,
            "current_state": "unknown",
            "features": {},
        }

    running = _is_running(sensor_id, features)

    return {
        "operational_state": "running" if running else "stopped",
        "operational_score": _operational_score(sensor_id, features),
        "current_state": "unknown",
        "features": features,
    }


def aggregate_machine_state(sensor_results: dict[str, dict]) -> dict:
    """
    여러 센서 결과를 머신 단위로 집계.

    Args:
        sensor_results: {hardware_id: analyze_slice() 결과}

    Returns:
        {
          "operational_state": str,
          "operational_score": float,
          "current_state":     str,
        }
    """
    if not sensor_results:
        return {"operational_state": "unknown", "operational_score": 0.0, "current_state": "unknown"}

    scores = [r["operational_score"] for r in sensor_results.values()]
    states = [r["operational_state"] for r in sensor_results.values()]
    avg_score = float(np.mean(scores))

    running_count = states.count("running")
    operational_state = "running" if running_count > 0 else "stopped"

    return {
        "operational_state": operational_state,
        "operational_score": round(avg_score, 4),
        "current_state": "unknown",
    }
