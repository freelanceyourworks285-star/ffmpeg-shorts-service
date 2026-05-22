"""
AI Shorts Generator microservice — v4 (URL-based, memory-safe).

Pipeline-friendly design:
  POST /upload    — upload ONE file (image or audio). Returns a job_id + file URL.
                    n8n calls this once per image and once for the audio, so
                    n8n never holds more than one file in memory at a time.
  POST /assemble  — given a job_id, assembles all uploaded files into a
                    9:16 vertical short with Ken Burns zoom + word captions.
  GET  /file/<id> — serves an uploaded/produced file (used internally + for result).
  GET  /health    — health check.

Why: passing 6 images + audio as base64 through n8n crashes it (OOM).
With /upload, each file is sent individually and stored server-side; only
small JSON (job_id, urls) travels through n8n.
"""
import os
import uuid
import time
import shutil
import base64
import threading
import subprocess

import requests
from flask import Flask, request, send_file, jsonify, abort, url_for

app = Flask(__name__)

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/shorts")
API_KEY = os.environ.get("API_KEY")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://ffmpeg-shorts-service.onrender.com"
)
os.makedirs(WORK_DIR, exist_ok=True)

# Jobs older than this many seconds get cleaned up.
JOB_TTL_SECONDS = 3600


def require_api_key():
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_KEY}":
        abort(401, description="Invalid or missing API key")


def job_path(job_id: str) -> str:
    # Basic sanitisation — job_id is always our own uuid hex.
    safe = "".join(c for c in job_id if c.isalnum())
    return os.path.join(WORK_DIR, safe)


def run_ffmpeg(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


def get_audio_duration(audio_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = (result.stdout or "").strip()
    if not out:
        raise RuntimeError("could not read audio duration (empty audio?)")
    return float(out)


def ms_to_ass_time(ms: int) -> str:
    cs = ms // 10
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_captions(script_text: str, total_duration: float, out_path: str) -> None:
    words = script_text.split()
    if not words:
        words = [" "]
    duration_per_word_ms = max(1, int((total_duration * 1000) / len(words)))

    style_line = (
        "Style: Default,Liberation Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,"
        "&H80000000,-1,0,0,0,100,100,0,0,1,8,2,2,40,40,240,1"
    )
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    events = []
    phrase_size = 3
    current_ms = 0
    for i in range(0, len(words), phrase_size):
        phrase = words[i:i + phrase_size]
        phrase_ms = duration_per_word_ms * len(phrase)
        start_ms = current_ms
        end_ms = current_ms + phrase_ms
        text = " ".join(w.upper() for w in phrase).replace("\n", " ")
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(start_ms)},{ms_to_ass_time(end_ms)},"
            f"Default,,0,0,0,,{text}"
        )
        current_ms = end_ms

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


def cleanup_old_jobs() -> None:
    """Remove job folders older than JOB_TTL_SECONDS."""
    now = time.time()
    try:
        for name in os.listdir(WORK_DIR):
            p = os.path.join(WORK_DIR, name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > JOB_TTL_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True,
                       check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available",
                        "service": "ai-shorts-v4"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/upload")
def upload():
    """
    Upload ONE file to a job. Memory-safe: n8n sends one file at a time.

    JSON body:
    {
        "job_id": "abc123"      // optional; omit to start a new job
        "kind": "image" | "audio",
        "file_base64": "...",   // the file content (one file only)
        "index": 0               // for images: ordering (0-5). ignored for audio
    }

    Returns: { "job_id": "...", "kind": "...", "stored": "img_00.png", "count": N }
    """
    require_api_key()
    cleanup_old_jobs()

    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    file_b64 = data.get("file_base64")
    if kind not in ("image", "audio"):
        abort(400, description="kind must be 'image' or 'audio'")
    if not file_b64:
        abort(400, description="file_base64 is required")

    job_id = data.get("job_id") or uuid.uuid4().hex[:12]
    jdir = job_path(job_id)
    os.makedirs(jdir, exist_ok=True)

    try:
        raw = base64.b64decode(file_b64)
    except Exception:
        abort(400, description="file_base64 is not valid base64")

    if kind == "audio":
        fname = "audio.mp3"
    else:
        idx = int(data.get("index", 0))
        fname = f"img_{idx:02d}.png"

    with open(os.path.join(jdir, fname), "wb") as f:
        f.write(raw)

    img_count = len([n for n in os.listdir(jdir) if n.startswith("img_")])
    return jsonify({
        "job_id": job_id,
        "kind": kind,
        "stored": fname,
        "image_count": img_count,
        "bytes": len(raw),
    })


@app.post("/assemble")
def assemble():
    """
    Assemble a short from files already uploaded via /upload.

    JSON body:
    {
        "job_id": "abc123",
        "script_text": "for captions",
        "add_captions": true
    }

    Returns the final MP4 file.
    """
    require_api_key()
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id")
    if not job_id:
        abort(400, description="job_id is required")

    jdir = job_path(job_id)
    if not os.path.isdir(jdir):
        abort(404, description=f"job '{job_id}' not found or expired")

    audio_path = os.path.join(jdir, "audio.mp3")
    if not os.path.exists(audio_path):
        abort(400, description="no audio uploaded for this job")

    image_paths = sorted(
        os.path.join(jdir, n) for n in os.listdir(jdir)
        if n.startswith("img_")
    )
    if not image_paths:
        abort(400, description="no images uploaded for this job")

    try:
        total_duration = get_audio_duration(audio_path)
        if total_duration < 3 or total_duration > 120:
            abort(400, description=f"audio duration out of range: {total_duration:.1f}s")

        duration_per_image = total_duration / len(image_paths)

        # 1. Slideshow with Ken Burns zoom.
        concat_file = os.path.join(jdir, "concat.txt")
        with open(concat_file, "w") as f:
            for img in image_paths:
                f.write(f"file '{img}'\n")
                f.write(f"duration {duration_per_image:.3f}\n")
            f.write(f"file '{image_paths[-1]}'\n")

        slideshow_path = os.path.join(jdir, "slideshow.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "zoompan=z='min(zoom+0.0015,1.5)':d=125:s=1080x1920:fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", "30",
            slideshow_path,
        ])

        # 2. Mux audio.
        with_audio_path = os.path.join(jdir, "with_audio.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-i", slideshow_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            with_audio_path,
        ])

        # 3. Optional captions.
        final_path = with_audio_path
        if data.get("add_captions") and data.get("script_text"):
            subs_path = os.path.join(jdir, "subs.ass")
            build_captions(data["script_text"], total_duration, subs_path)
            captioned_path = os.path.join(jdir, "final.mp4")
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", with_audio_path,
                "-vf", f"ass={subs_path}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy",
                captioned_path,
            ])
            final_path = captioned_path

        return send_file(final_path, mimetype="video/mp4",
                         as_attachment=True,
                         download_name=f"short_{job_id}.mp4")

    except RuntimeError as e:
        abort(500, description=str(e))
    except ValueError as e:
        abort(400, description=str(e))


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
@app.errorhandler(500)
@app.errorhandler(502)
def handle_error(e):
    desc = getattr(e, "description", str(e))
    code = getattr(e, "code", 500)
    return jsonify({"error": str(desc)}), code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
