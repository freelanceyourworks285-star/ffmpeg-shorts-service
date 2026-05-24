"""
AI Shorts Generator microservice — v11 (RAM-optimized for Render free tier).
Changes from v10:
- Images resized to 540x960 before FFmpeg slideshow (half resolution = 1/4 RAM)
- Captions disabled by default to save one FFmpeg pass
- Single FFmpeg pass: concat + audio in one command
"""
import os
import io
import re
import base64
import shutil
import tempfile
import subprocess
import urllib.request

from flask import Flask, request, send_file, jsonify, abort

app = Flask(__name__)
MAX_IMAGES = 12


def run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


def get_audio_duration(audio_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = (result.stdout or "").strip()
    if not out:
        raise RuntimeError("could not read audio duration")
    return float(out)


def ms_to_ass_time(ms):
    cs = ms // 10
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_captions(script_text, total_duration, out_path):
    words = script_text.split()
    if not words:
        words = [" "]
    duration_per_word_ms = max(1, int((total_duration * 1000) / len(words)))
    style_line = ("Style: Default,Liberation Sans,48,&H00FFFFFF,&H0000FFFF,&H00000000,"
                  "&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,20,20,120,1")
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 540\nPlayResY: 960\n"
              "WrapStyle: 2\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
              "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
              "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
              "MarginL, MarginR, MarginV, Encoding\n"
              f"{style_line}\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    current_ms = 0
    for i in range(0, len(words), 3):
        phrase = words[i:i+3]
        phrase_ms = duration_per_word_ms * len(phrase)
        text = " ".join(w.upper() for w in phrase).replace("\n", " ")
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(current_ms)},{ms_to_ass_time(current_ms+phrase_ms)},"
            f"Default,,0,0,0,,{text}")
        current_ms += phrase_ms
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


def _decode_b64(value, label):
    if not value or not isinstance(value, str):
        abort(400, description=f"{label} is required")
    if "," in value and value.strip().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value)
    except Exception:
        abort(400, description=f"{label} is not valid base64")


def _download_drive_file(file_id, dest_path):
    """Download a Google Drive file, handling the virus-scan confirmation page."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()
    if "text/html" in content_type or data[:4] in (b"<!DO", b"<htm", b"<!do"):
        html = data.decode("utf-8", errors="ignore")
        match = re.search(r'name="uuid"\s+value="([^"]+)"', html)
        if match:
            confirm_url = (f"https://drive.google.com/uc?export=download"
                           f"&id={file_id}&confirm=t&uuid={match.group(1)}")
            req2 = urllib.request.Request(confirm_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=60) as resp2:
                data = resp2.read()
        else:
            raise RuntimeError(f"Drive returned HTML for file {file_id} — may not be public")
    with open(dest_path, "wb") as f:
        f.write(data)


def _resize_image(src, dst):
    """Resize image to 540x960 (half of 1080x1920) using ffmpeg to save RAM."""
    result = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vf", "scale=540:960:force_original_aspect_ratio=increase,crop=540:960",
        "-q:v", "3", dst
    ], capture_output=True, text=True)
    if result.returncode != 0:
        # If resize fails, just copy original
        import shutil as sh
        sh.copy(src, dst)


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available", "service": "ai-shorts-v11"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/assemble")
def assemble():
    """
    Assemble a 9:16 short. Stateless.
    {
      "audio_base64": "<base64 mp3>",
      "image_urls": ["https://drive.google.com/uc?id=...", ...],
      "add_captions": true,
      "script_text": "...",
      "job_id": "..."
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id") or "short"
    audio_bytes = _decode_b64(data.get("audio_base64"), "audio_base64")

    image_urls = data.get("image_urls") or []
    if not image_urls:
        abort(400, description="image_urls must be a non-empty array")
    if len(image_urls) > MAX_IMAGES:
        abort(400, description=f"too many images (max {MAX_IMAGES})")

    work = tempfile.mkdtemp(prefix="short_")
    try:
        audio_path = os.path.join(work, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        # Download and resize images to 540x960 (saves 75% RAM vs 1080x1920)
        image_paths = []
        for idx, url in enumerate(image_urls):
            raw_dest = os.path.join(work, f"raw_{idx:02d}.png")
            dest = os.path.join(work, f"img_{idx:02d}.jpg")
            m = re.search(r'id=([a-zA-Z0-9_-]+)', url)
            if not m:
                m = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
            if m:
                file_id = m.group(1)
                try:
                    _download_drive_file(file_id, raw_dest)
                except Exception as e:
                    abort(400, description=f"could not download image {idx}: {e}")
            else:
                abort(400, description=f"could not parse Drive file ID from URL: {url}")
            # Resize to half resolution
            _resize_image(raw_dest, dest)
            image_paths.append(dest)

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

        # Single pass: slideshow + audio together (saves one FFmpeg pass)
        output_path = os.path.join(work, "output.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            "-i", audio_path,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-r", "24",
            "-c:a", "aac", "-b:a", "128k",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            output_path,
        ])

        final_path = output_path
        if data.get("add_captions") and data.get("script_text"):
            subs_path = os.path.join(work, "subs.ass")
            build_captions(data["script_text"], total_duration, subs_path)
            captioned_path = os.path.join(work, "final.mp4")
            try:
                run_ffmpeg([
                    "ffmpeg", "-y", "-i", output_path,
                    "-vf", f"ass={subs_path}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-c:a", "copy", captioned_path,
                ])
                final_path = captioned_path
            except Exception:
                # If captions fail, use video without captions
                final_path = output_path

        with open(final_path, "rb") as f:
            video_bytes = f.read()
        shutil.rmtree(work, ignore_errors=True)

        return send_file(io.BytesIO(video_bytes), mimetype="video/mp4",
                         as_attachment=True, download_name=f"short_{job_id}.mp4")

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
    return jsonify({"error": str(getattr(e, "description", str(e)))}), getattr(e, "code", 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
