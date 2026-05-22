FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f -v

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=8080

EXPOSE 8080

# IMPORTANT: exactly 1 worker so the in-RAM JOBS dict is shared across
# all requests. Threads are fine (shared memory); multiple workers are NOT.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", \
     "--timeout", "600", "--graceful-timeout", "30", "app:app"]
