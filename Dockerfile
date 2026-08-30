FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV VIDEO_DOWNLOAD_DIR=/data
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "server.py"]
