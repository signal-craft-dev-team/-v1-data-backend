# data_backend 개발 요약

> 작성일: 2026-06-10
> 브랜치: v001-dev
> 배포: Cloud Run Jobs (asia-northeast3)

---

## 1. 서비스 개요

GCS에 업로드된 엣지 서버 WAV 파일을 분석해 설비 ON/OFF 상태를 판별하고 PostgreSQL DB에 기록하는 백엔드 서비스.

```
GCS (WAV) → MongoDB (sensor_map) → FFT 분석 → PostgreSQL
                                              ↓
                                  machine_status (현재 상태)
                                  machine_status_history (이력)
                                  edge_sensor.last_processed_file (bookmark)
```

---

## 2. 실행 모드

| 모드 | 명령어 | 용도 |
|---|---|---|
| test | `python -m app.main --mode test --max 5` | 소수 파일 동작 확인 |
| local | `python -m app.main --mode local --start YYYY-MM-DD --end YYYY-MM-DD --cache-dir ../test_engine/data` | 과거 데이터 일괄 적재 |
| schedule | `python -m app.main --mode schedule` | Cloud Run Jobs 실행 (3분 주기) |

---

## 3. 핵심 로직

### Schedule 모드 파이프라인

```
1. DB에서 edge_sensor.last_processed_file (bookmark) 조회
2. GCS에서 파일 목록 조회 → 처리 대상 선택
   - bookmark가 오늘이면: bookmark 이후 파일 전부
   - bookmark 없거나 다른 날짜: now - 3분 이후 파일 (시간 윈도우)
3. 파일별:
   a. MongoDB audio_upload_logs에서 sensor_map 조회
   b. GCS에서 WAV 다운로드
   c. sensor_map 기반 센서별 슬라이싱
   d. FFT 분석 (N_FFT=8192, HOP=1024, Blackman)
   e. machine_status_history INSERT
   f. machine_status UPSERT
   g. edge_sensor.last_processed_file + updated_at 갱신 (성공/실패 무관)
4. 보간: 앞뒤 1분 이내 running → stopped → running인 경우 stopped→running 보정
```

### recorded_at 처리

```
파일명: 20260610_003859.wav
→ KST 기준 파싱: 2026-06-10 00:38:59+09:00
→ DB 저장: timestamptz (UTC 내부 저장, KST 조회 시 정확)
```

### sensor_map 캐싱 (schedule 모드)

- MongoDB 연결: 싱글톤 클라이언트 (재연결 없음)
- GCS 연결: 싱글톤 클라이언트

---

## 4. FFT 분석 파라미터

| 항목 | 값 |
|---|---|
| Sample Rate | 32,000 Hz |
| N_FFT | 8,192 |
| HOP | 1,024 (75% overlap 기준이나 실제 87.5%) |
| Window | Blackman |
| 주파수 해상도 | 3.906 Hz/bin |

### 센서별 판별 기준 (reports/260609_REPORT.md 참조)

| 센서 | 기기 | 판별 조건 |
|---|---|---|
| 3C0F02E3B604 | 배큠펌프 | b0_60 < 5% AND b6k_16k < 5% |
| 3C0F02E3B654 | 진공오븐챔버 | b1k_3k > 45% |
| A0F262EC9088 | 오일펌프 | b60_200 < 8% AND b1k_3k > 40% |
| A0F262EC9388 | 워터칠러 | b3k_6k > 15% |
| 전체 OFF 공통 | — | b0_60 > 20% |

---

## 5. DB 스키마 변경 사항

```sql
-- machine_status
ALTER TABLE machine_status
    ADD COLUMN last_machine_status_history_id uuid
        REFERENCES machine_status_history(id);

-- machine_status_history
ALTER TABLE machine_status_history
    ADD COLUMN optional_text jsonb,
    ADD COLUMN related_file_name varchar(64),
    ADD COLUMN interpolated boolean NOT NULL DEFAULT false;

-- edge_sensor
ALTER TABLE edge_sensor
    ADD COLUMN last_processed_file varchar(64);
```

### optional_text 구조

```json
{
  "description": {
    "b1k_3k": {"key": "1kHz~3kHz 에너지 비율", "value": "> 45% → running"}
  },
  "current_value": {"b0_60": 0.6, "b1k_3k": 65.7, ...},
  "threshold_value": {"b0_60 (global off)": "> 20%", "b1k_3k": "> 45%"}
}
```

---

## 6. 인프라 구성

### Cloud Run Jobs

| 항목 | 값 |
|---|---|
| 서비스명 | data-backend |
| 리전 | asia-northeast3 |
| 이미지 | asia-northeast3-docker.pkg.dev/signalcraft-ver-1-202605/data-backend/data-backend |
| Memory | 1Gi |
| CPU | 1 |
| Task Timeout | 300s |
| Max Retries | 1 |
| VPC | default / private-ranges-only |
| SA | data-backend-runner@signalcraft-ver-1-202605.iam.gserviceaccount.com |

### SA 권한

| 권한 | 용도 |
|---|---|
| roles/run.admin | Cloud Run Jobs 배포/실행 |
| roles/iam.serviceAccountUser | SA 지정 배포 |
| roles/secretmanager.secretAccessor | Secret Manager 접근 |
| roles/artifactregistry.writer | GAR 이미지 푸시 |
| roles/storage.objectViewer | GCS 읽기 (signal-craft-ver-260330 프로젝트) |
| roles/iam.workloadIdentityUser | GitHub Actions WIF 인증 |

### Secrets

| 이름 | 저장 위치 | 용도 |
|---|---|---|
| DATABASE_URL | GitHub Secrets (조합) | PostgreSQL 연결 |
| MONGODB_URI | GCP Secret Manager | MongoDB 연결 |

### 환경변수 (.env / Cloud Run)

```bash
DATABASE_URL=postgresql://signalcraft_app:{PASSWORD}@{HOST}:5432/signalcraft
GOOGLE_APPLICATION_CREDENTIALS=./signal-craft-test-engine.json  # 로컬만
GCS_BUCKET=signalcraft-audio-bucket
MONGODB_URI=mongodb://...
```

---

## 7. GitHub Actions 워크플로우

- 트리거: `v001-dev` 브랜치 push
- 인증: Workload Identity Federation (WIF)
  - Pool: `github-pool`
  - Provider: `github-provider`
  - SA 바인딩: `signal-craft-dev-team/-v1-data-backend`
- 빌드: GHCR → GAR 복사 → Cloud Run Jobs 배포

---

## 8. ⬜ 남은 작업 — Cloud Scheduler 등록

배포 완료 후 아래 명령어로 스케줄러를 등록합니다.

```bash
PROJECT="signalcraft-ver-1-202605"
REGION="asia-northeast3"
SA="data-backend-runner@signalcraft-ver-1-202605.iam.gserviceaccount.com"

gcloud scheduler jobs create http data-backend-trigger \
  --schedule="*/3 * * * *" \
  --location=$REGION \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/data-backend:run" \
  --http-method=POST \
  --oauth-service-account-email=$SA \
  --project=$PROJECT
```

### 스케줄러 관리 명령어

```bash
# 일시 중지
gcloud scheduler jobs pause data-backend-trigger \
  --location=asia-northeast3 --project=$PROJECT

# 재개
gcloud scheduler jobs resume data-backend-trigger \
  --location=asia-northeast3 --project=$PROJECT

# 수동 즉시 실행
gcloud scheduler jobs run data-backend-trigger \
  --location=asia-northeast3 --project=$PROJECT

# 삭제
gcloud scheduler jobs delete data-backend-trigger \
  --location=asia-northeast3 --project=$PROJECT
```

---

## 9. 로컬 배치 실행 (과거 데이터 적재)

```bash
# IAP 터널 (터미널 1)
gcloud compute start-iap-tunnel {DB_VM_NAME} 5432 \
  --local-host-port=localhost:5432 \
  --zone=asia-northeast3-a

# 배치 실행 (터미널 2)
cd data_backend
python -m app.main --mode local \
  --start 2026-05-26 --end 2026-06-09 \
  --cache-dir ../test_engine/data
```

---

## 10. shared/ 동기화

`test_engine/shared/`의 `features.py`, `analyzer.py`를 수정하면 반드시 sync 후 push:

```bash
cd data_backend
./sync_shared.sh
git add shared/
git commit -m "sync: shared/analyzer.py"
git push origin v001-dev
```
