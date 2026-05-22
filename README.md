# FFmpeg Shorts Microservice

A free, self-hostable Flask + FFmpeg service that cuts a video segment, reformats it to 9:16 vertical, and burns animated word-by-word captions. Designed to plug into the n8n YouTube Long-to-Shorts workflow.

## What it does

- Downloads a source video from any HTTPS URL
- Cuts a specific time range
- Center-crops to 9:16 vertical (1080x1920) for Shorts/Reels/TikTok
- Burns karaoke-style word-by-word captions (TikTok style) using AssemblyAI word timestamps
- Returns the final MP4 as a binary response

## API

### `POST /process`

```json
{
  "source_url": "https://example.com/video.mp4",
  "start": 15.0,
  "end": 60.0,
  "aspect": "9:16",
  "add_captions": true,
  "caption_style": "tiktok",
  "title": "Optional title",
  "transcript_words": [
    { "text": "Hello", "start": 0, "end": 500 },
    { "text": "world", "start": 500, "end": 1000 }
  ]
}
```

Returns `video/mp4` binary. Send `Authorization: Bearer <API_KEY>` header if `API_KEY` env var is set.

### `GET /health`

Returns `{"status": "ok"}` when FFmpeg is available.

## Deploy free

### Option A — Render (easiest, fully free)

1. Push this folder to a GitHub repo
2. Sign up at render.com
3. New → Blueprint → connect your repo → Render picks up `render.yaml`
4. Copy the generated `API_KEY` from the Render dashboard
5. Your URL: `https://ffmpeg-shorts-service.onrender.com`

Free tier sleeps after 15 min of inactivity; first request after sleep takes ~30s to wake.

### Option B — Fly.io

```bash
fly launch --no-deploy
fly secrets set API_KEY=$(openssl rand -hex 32)
fly deploy
```

### Option C — Railway

1. Push to GitHub
2. railway.app → New Project → Deploy from GitHub
3. Add env var `API_KEY` with a random string
4. Get your public URL from the dashboard

### Option D — Local Docker

```bash
export API_KEY=$(openssl rand -hex 32)
docker compose up --build
# Service available at http://localhost:8080
```

## Wire into n8n

In your n8n workflow, edit the **Cut & Reformat to 9:16** node:

1. Change URL from `https://YOUR-FFMPEG-SERVICE.com/process` to your deployed URL
2. Add Header Auth credential with `Authorization: Bearer YOUR_API_KEY`
3. Update the JSON body to pass the transcript words from AssemblyAI:

```json
{
  "source_url": "{{ $json.source_video_url }}",
  "start": "{{ $json.start_seconds }}",
  "end": "{{ $json.end_seconds }}",
  "aspect": "9:16",
  "add_captions": true,
  "caption_style": "tiktok",
  "transcript_words": "{{ $('Poll Transcript Status').item.json.words }}"
}
```

## Resource notes

- A 60-second 1080p clip processes in ~30-60 seconds on Render free tier (single CPU)
- Memory: ~512 MB peak during encoding — fits comfortably on free tiers
- Disk: cleans up per-job under `/tmp/shorts/<job_id>` — add a cron to purge old dirs if running long

## Local quick test

```bash
curl -X POST http://localhost:8080/process \
  -H "Authorization: Bearer dev-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "https://download.samplelib.com/mp4/sample-30s.mp4",
    "start": 0,
deploy v6
    "end": 15,
    "aspect": "9:16",
    "add_captions": false
  }' \
  --output test_short.mp4
```
