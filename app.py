"""
AI Shorts Generator microservice — v6 (RAM storage, memory-safe, no-auth).

Why v6: on Render's free tier the /tmp directory did not reliably persist
files between separate HTTP requests, so uploaded images vanished and
/assemble saw "no images". v6 keeps uploaded files in a module-level
Python dict (RAM). With a single gunicorn worker this dict is shared
across all requests for the lifetime of the service process — no disk
dependency.

Endpoints:
  POST /upload    — upload ONE file (image or audio) into the in-RAM job.
  POST /assemble  — assemble all files for a job into a 9:16 short.
  GET  /health    — health check.
  GET  /job/<id>  — debug: see what's stored for a job.
"""
import os
import io
import time
import uuid
import base64
import shutil
import tempfile
import subprocess

from flask import Flask, request, send_file, jsonify, abort

app = Flask(__name__)

# In-RAM job store:  job_id -> { "audio": bytes, "images": {index: bytes}, "ts": float }
JOBS = {}
JOB_TTL_SECONDS = 3600


def cleanup_old_jobs() -> None:
    now = time.time()
    stale = [jid for jid, j in JOBS.items() if now - j.get("ts", now) > JOB_TTL_SECONDS]
    for jid in stale:
        JOBS.pop(jid, None)


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


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True,
                       check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available",
                        "service": "ai-shorts-v6",
                        "active_jobs": len(JOBS)})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.get("/job/<job_id>")
def job_info(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"exists": False, "job_id": job_id})
    return jsonify({
        "exists": True,
        "job_id": job_id,
        "has_audio": j.get("audio") is not None,
        "image_count": len(j.get("images", {})),
        "image_indexes": sorted(j.get("images", {}).keys()),
    })


@app.post("/upload")
def upload():
    """Upload ONE file (image or audio) into the in-RAM job store."""
    cleanup_old_jobs()

    data = request.get_json(force=True, silent=True) or {}
    kind = data.get("kind")
    file_b64 = data.get("file_base64")
    if kind not in ("image", "audio"):
        abort(400, description="kind must be 'image' or 'audio'")
    if not file_b64:
        abort(400, description="file_base64 is required")

    job_id = data.get("job_id") or uuid.uuid4().hex[:12]

    try:
        raw = base64.b64decode(file_b64)
    except Exception:
        abort(400, description="file_base64 is not valid base64")

    job = JOBS.setdefault(job_id, {"audio": None, "images": {}, "ts": time.time()})
    job["ts"] = time.time()

    if kind == "audio":
        job["audio"] = raw
    else:
        idx = int(data.get("index", 0))
        job["images"][idx] = raw

    return jsonify({
        "job_id": job_id,
        "kind": kind,
        "image_count": len(job["images"]),
        "has_audio": job["audio"] is not None,
        "bytes": len(raw),
    })


@app.post("/assemble")
def assemble():
    """Assemble a short from files held in RAM for this job."""
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id")
    if not job_id:
        abort(400, description="job_id is required")

    job = JOBS.get(job_id)
    if not job:
        abort(404, description=f"job '{job_id}' not found or expired")
    if not job.get("audio"):
        abort(400, description="no audio uploaded for this job")
    if not job.get("images"):
        abort(400, description="no images uploaded for this job")

    # Write RAM files to a temp working dir just for ffmpeg.
    work = tempfile.mkdtemp(prefix="short_")
    try:
        audio_path = os.path.join(work, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(job["audio"])

        image_paths = []
        for idx in sorted(job["images"].keys()):
            p = os.path.join(work, f"img_{idx:02d}.png")
            with open(p, "wb") as f:
                f.write(job["images"][idx])
            image_paths.append(p)

        total_duration = get_audio_duration(audio_path)
        if total_duration < 3 or total_duration > 120:
            abort(400, description=f"audio duration out of range: {total_duration:.1f}s")

        duration_per_image = total_duration / len(image_paths)

        concat_file = os.path.join(work, "concat.txt")
        with open(concat_file, "w") as f:
            for img in image_paths:
                f.write(f"file '{img}'\n")
                f.write(f"duration {duration_per_image:.3f}\n")
            f.write(f"file '{image_paths[-1]}'\n")

        slideshow_path = os.path.join(work, "slideshow.mp4")
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

        with_audio_path = os.path.join(work, "with_audio.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-i", slideshow_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            with_audio_path,
        ])

        final_path = with_audio_path
        if data.get("add_captions") and data.get("script_text"):
            subs_path = os.path.join(work, "subs.ass")
            build_captions(data["script_text"], total_duration, subs_path)
            captioned_path = os.path.join(work, "final.mp4")
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", with_audio_path,
                "-vf", f"ass={subs_path}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy",
                captioned_path,
            ])
            final_path = captioned_path

        # Load result into memory, then free the job + temp dir.
        with open(final_path, "rb") as f:
            video_bytes = f.read()

        JOBS.pop(job_id, None)
        shutil.rmtree(work, ignore_errors=True)

        return send_file(
            io.BytesIO(video_bytes),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"short_{job_id}.mp4",
        )

    except RuntimeError as e:
        shutil.rmtree(work, ignore_errors=True)
        abort(500, description=str(e))
    except ValueError as e:
        shutil.rmtree(work, ignore_errors=True)
        abort(400, description=str(e))


@app.errorhandler(400)
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
