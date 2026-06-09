"""
FFT 기반 오디오 피처 추출 모듈.

파라미터 기준: reports/260609_REPORT.md
  N_FFT=8192, HOP=1024, Blackman window, SR=32000
"""
import numpy as np

SAMPLE_RATE = 32000
N_FFT       = 8192
HOP         = 1024
WINDOW      = np.blackman(N_FFT)
FREQS       = np.fft.rfftfreq(N_FFT, 1 / SAMPLE_RATE)

BANDS = {
    "b0_60":    (0,    60),
    "b60_200":  (60,   200),
    "b200_500": (200,  500),
    "b500_1k":  (500,  1000),
    "b1k_3k":   (1000, 3000),
    "b3k_6k":   (3000, 6000),
    "b6k_16k":  (6000, 16000),
}


def extract(audio: np.ndarray) -> dict | None:
    """
    단채널 오디오 → FFT 피처 dict.

    Returns:
        {
          "b0_60": float,      # 0~60Hz 에너지 비율 (%)
          "b60_200": float,
          ...
          "e60": float,        # 60Hz ±2Hz 에너지 비율 (%)
          "centroid": float,   # 전체 에너지 중심 주파수 (Hz)
          "centroid_3k": float # 3kHz 이하 에너지 중심 (Hz)
        }
        또는 None (데이터 부족)
    """
    target = SAMPLE_RATE * 5
    if len(audio) > target:
        audio = audio[:target]
    elif len(audio) < target:
        audio = np.pad(audio, (0, target - len(audio)))

    all_powers = []
    for chunk_start in range(0, target, SAMPLE_RATE):
        chunk = audio[chunk_start : chunk_start + SAMPLE_RATE]
        if len(chunk) < N_FFT:
            continue
        for frame_start in range(0, len(chunk) - N_FFT + 1, HOP):
            frame = chunk[frame_start : frame_start + N_FFT] * WINDOW
            power = np.abs(np.fft.rfft(frame)) ** 2 / N_FFT
            all_powers.append(power)

    if not all_powers:
        return None

    mean_power = np.mean(all_powers, axis=0)
    total      = np.sum(mean_power) + 1e-12

    features = {}

    # 대역별 에너지 비율
    for name, (lo, hi) in BANDS.items():
        mask = (FREQS >= lo) & (FREQS < hi)
        features[name] = float(np.sum(mean_power[mask]) / total * 100)

    # e60: 60Hz ±2Hz
    mask_e60 = (FREQS >= 58) & (FREQS < 62)
    features["e60"] = float(np.sum(mean_power[mask_e60]) / total * 100)

    # centroid (전체)
    features["centroid"] = float(np.sum(FREQS * mean_power) / total)

    # centroid (3kHz 이하)
    mask_3k = FREQS <= 3000
    total_3k = np.sum(mean_power[mask_3k]) + 1e-12
    features["centroid_3k"] = float(np.sum(FREQS[mask_3k] * mean_power[mask_3k]) / total_3k)

    return features
