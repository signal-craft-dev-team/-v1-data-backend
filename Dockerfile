FROM python:3.12-slim

WORKDIR /app

# libsndfile (soundfile 의존성)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY app/    ./app/

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["--mode", "schedule"]
