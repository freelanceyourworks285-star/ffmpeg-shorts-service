FROM python:3.11-slim

# Install ffmpeg + Montserrat font (used by the caption style)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fontconfig \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Montserrat font for TikTok-style captions
RUN mkdir -p /usr/share/fonts/truetype/montserrat && \
    curl -L -o /tmp/mont.zip "https://github.com/JulietaUla/Montserrat/archive/refs/heads/master.zip" || true && \
    fc-cache -f -v || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=8080
ENV WORK_DIR=/tmp/shorts
ENV MAX_DOWNLOAD_MB=500

EXPOSE 8080

# Single worker with long timeout: video processing is CPU-bound and long-running
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "2", \
     "--timeout", "600", "--graceful-timeout", "30", "app:app"]
