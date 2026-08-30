FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py drive_storage.py run_server.py ./

ENV VIDEO_DOWNLOAD_DIR=/tmp/tokisclone
ENV HOST=0.0.0.0
ENV PORT=8000
RUN mkdir -p /tmp/tokisclone

EXPOSE 8000

CMD ["python", "run_server.py"]
