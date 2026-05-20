"""
FFmpeg + yt-dlp microservice for YouTube Shorts pipeline.
Self-contained: downloads YouTube directly, cuts, reformats to 9:16, burns captions.

Endpoints:
  POST /download   — given a YouTube URL, returns direct mp4 URL
  POST /process    — cut + reformat + caption a clip
"""
import os
import uuid
import subprocess
import shutil
import json
import requests
from flask import Flask, request, send_file, jsonify, abort, url_for

app = Flask(__name__)

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/shorts")
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "500"))
API_KEY = os.environ.get("API_KEY")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
os.makedirs(WORK_DIR, exist_ok=True)


def require_api_key():
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_KEY}":
        abort(401, description="Invalid or missing API key")


def yt_download(url: str, dest: str) -> dict:
    """Download YouTube video using yt-dlp. Returns metadata."""
    cmd = [
        "yt-dlp",
        "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "-o", dest,
        "--print-json",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[-2000:]}")
    
    # yt-dlp prints metadata as JSON
    try:
        meta = json.loads(result.stdout.strip().split("\n")[-1])
    except Exception:
        meta = {}
    
    return {
        "title": meta.get("title", ""),
        "duration": meta.get("duration", 0),
        "uploader": meta.get("uploader", ""),
    }


def download_file(url: str, dest: str) -> None:
    """Stream a remote file to disk with a size cap."""
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                    raise ValueError(f"File exceeds {MAX_DOWNLOAD_MB}MB limit")
                f.write(chunk)


def ms_to_ass_time(ms: int) -> str:
    cs = ms // 10
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass_subtitles(words, start_sec: float, end_sec: float, out_path: str, style: str = "tiktok"):
    start_ms = int(start_sec * 1000)
    end_ms = int(end_sec * 1000)
    clip_words = []
    for w in words or []:
        w_start = w.get("start", 0)
        w_end = w.get("end", 0)
        if w_end < start_ms or w_start > end_ms:
            continue
        clip_words.append({
            "text": w.get("text", "").strip(),
            "start": max(0, w_start - start_ms),
            "end": max(0, w_end - start_ms),
        })

    if style == "tiktok":
        style_line = (
            "Style: Default,Liberation Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
            "-1,0,0,0,100,100,0,0,1,6,0,2,40,40,260,1"
        )
    else:
        style_line = (
            "Style: Default,Liberation Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
            "0,0,0,0,100,100,0,0,1,4,0,2,40,40,200,1"
        )

    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    PHRASE_SIZE = 3
    for i in range(0, len(clip_words), PHRASE_SIZE):
        phrase = clip_words[i:i + PHRASE_SIZE]
        if not phrase:
            continue
        p_start = phrase[0]["start"]
        p_end = phrase[-1]["end"]
        text_parts = []
        for w in phrase:
            duration_cs = max(1, (w["end"] - w["start"]) // 10)
            text_parts.append(r"{\kf%d}%s" % (duration_cs, w["text"].upper()))
        text = " ".join(text_parts)
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(p_start)},{ms_to_ass_time(p_end)},Default,,0,0,0,,{text}"
        )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


def run_ffmpeg(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available", "yt-dlp": "available"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/download")
def download():
    """Download a YouTube video and return the public URL where it's hosted."""
    require_api_key()
    data = request.get_json(force=True)
    if not data or "url" not in data:
        abort(400, description="url is required")

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    out_path = os.path.join(job_dir, "video.mp4")

    try:
        meta = yt_download(data["url"], out_path)
        # Build the public URL where this file will be served
        if PUBLIC_URL:
            public_link = f"{PUBLIC_URL.rstrip('/')}/files/{job_id}/video.mp4"
        else:
            public_link = url_for("serve_file", job_id=job_id, filename="video.mp4", _external=True)
        return jsonify({
            "url": public_link,
            "title": meta.get("title", ""),
            "duration": meta.get("duration", 0),
            "uploader": meta.get("uploader", ""),
            "job_id": job_id,
        })
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        abort(500, description=str(e))


@app.get("/files/<job_id>/<filename>")
def serve_file(job_id, filename):
    """Serve a downloaded file (used by AssemblyAI to fetch the audio)."""
    safe_id = "".join(c for c in job_id if c.isalnum() or c in "-_")
    safe_name = "".join(c for c in filename if c.isalnum() or c in "-_.")
    path = os.path.join(WORK_DIR, safe_id, safe_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="video/mp4")


@app.post("/process")
def process():
    require_api_key()
    data = request.get_json(force=True)
    if not data or "source_url" not in data:
        abort(400, description="source_url is required")

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    src = os.path.join(job_dir, "src.mp4")
    cut = os.path.join(job_dir, "cut.mp4")
    vertical = os.path.join(job_dir, "vertical.mp4")
    final = os.path.join(job_dir, "final.mp4")
    subs = os.path.join(job_dir, "subs.ass")

    try:
        # 1. Download source — either a YouTube URL or a direct mp4 link
        src_url = data["source_url"]
        if "youtube.com" in src_url or "youtu.be" in src_url:
            yt_download(src_url, src)
        else:
            download_file(src_url, src)

        start = float(data.get("start", 0))
        end = float(data.get("end", 60))
        duration = end - start
        if duration <= 0 or duration > 180:
            abort(400, description="Duration must be between 0 and 180 seconds")

        # 2. Cut
        run_ffmpeg([
            "ffmpeg", "-y",
            "-ss", str(start), "-to", str(end),
            "-i", src,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            cut,
        ])

        # 3. 9:16
        aspect = data.get("aspect", "9:16")
        if aspect == "9:16":
            vf = "crop='min(iw\\,ih*9/16)':'min(ih\\,iw*16/9)',scale=1080:1920,setsar=1"
        elif aspect == "1:1":
            vf = "crop='min(iw\\,ih)':'min(iw\\,ih)',scale=1080:1080,setsar=1"
        else:
            vf = "scale=1080:1920,setsar=1"

        run_ffmpeg([
            "ffmpeg", "-y", "-i", cut,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "copy",
            vertical,
        ])

        # 4. Captions
        if data.get("add_captions") and data.get("transcript_words"):
            build_ass_subtitles(
                data["transcript_words"], start, end, subs,
                style=data.get("caption_style", "tiktok"),
            )
            run_ffmpeg([
                "ffmpeg", "-y", "-i", vertical,
                "-vf", f"ass={subs}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy",
                final,
            ])
            output_path = final
        else:
            output_path = vertical

        return send_file(output_path, mimetype="video/mp4",
                         as_attachment=True, download_name=f"short_{job_id}.mp4")

    except requests.HTTPError as e:
        abort(502, description=f"Could not download source video: {e}")
    except ValueError as e:
        abort(400, description=str(e))
    except RuntimeError as e:
        abort(500, description=str(e))


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
@app.errorhandler(500)
@app.errorhandler(502)
def handle_error(e):
    return jsonify({"error": str(e.description) if hasattr(e, "description") else str(e)}), e.code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
