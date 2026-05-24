"""
AI Shorts Generator microservice — v8 (STATELESS, URL-based images).

Why v8: v7 accepted audio+images as base64 in one request, but n8n cloud
ran out of memory holding 6 large images in RAM simultaneously.

v8 fix: images are passed as Google Drive download URLs (or any public URL).
The service downloads each image itself — n8n only needs to pass small URLs,
not megabytes of base64. Audio is still sent as base64 (it's small ~700KB).

Endpoints:
  POST /assemble  — assemble a 9:16 short. Accepts:
                    { audio_base64, image_urls: [...], script_text, add_captions, job_id }
  GET  /health    — health check.
"""
import os
import io
import base64
import shutil
import tempfile
import subprocess
import urllib.request

from flask import Flask, request, send_file, jsonify, abort

app = Flask(__name__)
MAX_IMAGES = 12


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
        raise RuntimeError("could not read audio duration")
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
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    phrase_size = 3
    current_ms = 0
    for i in range(0, len(words), phrase_size):
        phrase = words[i:i + phrase_size]
        phrase_ms = duration_per_word_ms * len(phrase)
        text = " ".join(w.upper() for w in phrase).replace("\n", " ")
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(current_ms)},{ms_to_ass_time(current_ms + phrase_ms)},"
            f"Default,,0,0,0,,{text}"
        )
        current_ms += phrase_ms
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


def _decode_b64(value, label: str) -> bytes:
    if not value or not isinstance(value, str):
        abort(400, description=f"{label} is required and must be a base64 string")
    if "," in value and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value)
    except Exception:
        abort(400, description=f"{label} is not valid base64")


def _download_url(url: str, dest_path: str) -> None:
    """Download a URL to dest_path. Supports Google Drive export links."""
    # Convert Google Drive view URLs to direct download URLs
    if "drive.google.com" in url:
        import re
        m = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
        if m:
            file_id = m.group(1)
            url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(dest_path, "wb") as f:
            f.write(resp.read())


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available", "service": "ai-shorts-v8"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/assemble")
def assemble():
    """
    Assemble a 9:16 short. Stateless — everything in one request.

    JSON body:
      {
        "audio_base64":  "<base64 mp3>",           # required
        "image_urls":    ["<url>", ...],            # required, 1..12 Google Drive or public URLs
        "add_captions":  true,                      # optional
        "script_text":   "narration text",          # optional, used if add_captions
        "job_id":        "abc123"                   # optional, for filename
      }
    Returns: video/mp4 attachment.
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id") or "short"

    audio_bytes = _decode_b64(data.get("audio_base64"), "audio_base64")

    image_urls = data.get("image_urls")
    if not isinstance(image_urls, list) or not image_urls:
        abort(400, description="image_urls must be a non-empty array of URLs")
    if len(image_urls) > MAX_IMAGES:
        abort(400, description=f"too many images (max {MAX_IMAGES})")

    work = tempfile.mkdtemp(prefix="short_")
    try:
        audio_path = os.path.join(work, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        image_paths = []
        for idx, url in enumerate(image_urls):
            p = os.path.join(work, f"img_{idx:02d}.png")
            try:
                _download_url(url, p)
            except Exception as e:
                abort(400, description=f"could not download image {idx}: {e}")
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

        with open(final_path, "rb") as f:
            video_bytes = f.read()

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
