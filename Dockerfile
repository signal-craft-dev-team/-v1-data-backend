FROM python:3.12-slim

WORKDIR /app

# libsndfile (soundfile 의존성)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 git && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=secret,id=github_token \
    git config --global \
      url."https://x-access-token:$(cat /run/secrets/github_token)@github.com/".insteadOf \
      "https://github.com/" && \
    pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY app/    ./app/

# Cloud Run: 환경변수로 모드 지정
# schedule 모드가 기본 (HTTP 트리거 → 1회 실행 후 종료)
CMD ["python", "-m", "app.main", "--mode", "schedule"]
