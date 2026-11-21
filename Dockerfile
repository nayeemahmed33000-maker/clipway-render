FROM python:3.12-slim

# ffmpeg + node (yt-dlp's JS runtime), no cache to keep the image small
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Port comes from the platform (Render sets $PORT; HF Spaces uses 7860).
# Default 7860 keeps local / HF behaviour unchanged.
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
