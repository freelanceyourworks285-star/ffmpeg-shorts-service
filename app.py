"""
AI Shorts Generator microservice — v9.

v8 passed Google Drive URLs but Drive returned HTML (login/scan warning) instead
of image bytes. v9 fixes this: images are sent as base64 per-image in the loop
but the service stores them in a per-request temp dict keyed by job_id + index,
then /assemble reads them all.

WAIT — that's the old stateful approach. Instead v9 goes back to all-in-one base64
but uses a smarter approach: images are sent as JPEG (not PNG) which are much smaller.
The gpt-image-1 'low' quality 1024x1536 PNG is ~500KB each. As JPEG q=60 it's ~80KB.
6 images = ~480KB base64 total — well within n8n memory.

Actually the simplest fix: accept images as base64 again (like v7) but the workflow
sends them one at a time via a new /add_image endpoint that stores in a per-job
tmp directory on disk (not RAM). Then /assemble reads from disk. On Render free
tier /tmp is ephemeral but persists within a single request lifecycle if we use
a shared tmp dir referenced by job_id. The risk is spin-down between calls.

BEST ACTUAL FIX for this situation:
- Keep the Drive URL approach (v8)  
- But fix the download: use requests with session to handle Google's redirect/cookie
- OR: make images publicly readable before downloading

Actually the SIMPLEST fix: in the workflow, instead of passing Drive URLs,
pass the Drive webContentLink which requires auth. Instead, after uploading to Drive,
use n8n's Google Drive 'download' operation to get the binary, then base64 encode
and send that to /assemble. But that's back to OOM.

THE REAL SIMPLEST FIX: 
Convert PNG to JPEG in the 'Upload Image to FFmpeg' Code node before uploading.
n8n can't do image conversion natively.

ACTUALLY — the real fix is dead simple:
The gpt-image-1 node outputs the image as binary 'data'. In 'Upload Image to FFmpeg'
(now a code node), we already have the raw PNG bytes. Instead of uploading to Drive,
just base64-encode the JPEG-compressed version. We can compress in the Code node
using the sharp library... but sharp isn't available in n8n cloud.

OK FINAL ANSWER: 
Keep v7 approach (base64 in one request) but send images as JPEG not PNG.
In the Code node, use the image binary as-is but tell the service it's JPEG.
The PNG from gpt-image-1 at 'low' quality 1024x1536 is actually stored as PNG
with size ~200-400KB (not 500KB as estimated). 6 x 400KB = 2.4MB base64 = 3.2MB
encoded. That's the total JSON payload. n8n cloud crashed at 3.2MB payload.

SIMPLEST FIX THAT ACTUALLY WORKS:
Split the /assemble call — send audio + images in chunks. But that requires state.

NO — the real answer: the crash was because the PREVIOUS run used medium quality
(not low). Let's check: the workflow was updated to low quality but the published
version may have still used medium. In execution 103 it crashed at Collect node
with OOM. In execution 104 it got to FFmpeg — meaning low quality + Drive URL
approach DID pass the OOM. The new problem is just Drive URL download = HTML.

SO THE FIX IS SIMPLE: just make the Drive files publicly accessible, or use
a different URL format. We can use the Drive API to make files public after upload,
OR we can pass a signed/authenticated download URL.

EASIEST: after uploading to Drive, use n8n to set the file permission to public,
then use the webContentLink. OR just fix the download in app.py to handle
Google's confirmation page for large files.
"""

# v9: Fix Google Drive download to handle confirmation pages
import os
import io
import re
import base64
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.parse

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
    style_line = ("Style: Default,Liberation Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,"
                  "&H80000000,-1,0,0,0,100,100,0,0,1,8,2,2,40,40,240,1")
    header = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
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
        events.append(f"Dialogue: 0,{ms_to_ass_time(current_ms)},{ms_to_ass_time(current_ms+phrase_ms)},"
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
    """Download a Google Drive file handling the virus-scan confirmation page."""
    session_cookies = {}
    
    # First request to get confirmation token if needed
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    
    with urllib.request.urlopen(req, timeout=60) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()
        
        # Check if we got HTML (confirmation page) instead of image
        if "text/html" in content_type or data[:4] == b"<!DO" or data[:4] == b"<htm":
            # Extract confirm token from HTML
            html = data.decode("utf-8", errors="ignore")
            match = re.search(r'name="uuid"\s+value="([^"]+)"', html)
            if not match:
                match = re.search(r'"downloadUrl":"([^"]+)"', html)
            if match:
                # Try direct download with confirm
                confirm_url = (f"https://drive.google.com/uc?export=download"
                               f"&id={file_id}&confirm=t&uuid={match.group(1)}")
                req2 = urllib.request.Request(confirm_url, headers={
                    "User-Agent": "Mozilla/5.0"
                })
                with urllib.request.urlopen(req2, timeout=60) as resp2:
                    data = resp2.read()
            else:
                raise RuntimeError(f"Got HTML from Drive, could not find confirm token. "
                                   f"File {file_id} may not be publicly accessible.")
    
    with open(dest_path, "wb") as f:
        f.write(data)


def _decode_b64_or_url(value, label, work_dir, idx):
    """Handle either base64 image data or a Google Drive file ID / URL."""
    if not value:
        abort(400, description=f"{label} is required")
    
    dest = os.path.join(work_dir, f"img_{idx:02d}.png")
    
    # If it looks like a Drive file ID (alphanumeric + underscores/dashes, ~33 chars)
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', value.strip()):
        _download_drive_file(value.strip(), dest)
        return dest
    
    # If it's a Drive URL, extract file ID
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', value)
    if m or "drive.google.com" in value:
        file_id = m.group(1) if m else re.search(r'id=([a-zA-Z0-9_-]+)', value).group(1)
        _download_drive_file(file_id, dest)
        return dest
    
    # Otherwise treat as base64
    raw = _decode_b64(value, label)
    with open(dest, "wb") as f:
        f.write(raw)
    return dest


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available", "service": "ai-shorts-v9"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/assemble")
def assemble():
    """
    Assemble a 9:16 short. Accepts:
      {
        "audio_base64": "<base64>",
        "image_urls": ["<drive_url_or_file_id>", ...],   # v8 style
        "images_base64": ["<base64>", ...],               # v7 style fallback
        "add_captions": true,
        "script_text": "...",
        "job_id": "..."
      }
    """
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("job_id") or "short"
    audio_bytes = _decode_b64(data.get("audio_base64"), "audio_base64")

    # Support both image_urls (Drive) and images_base64 (direct)
    image_urls = data.get("image_urls") or []
    images_b64 = data.get("images_base64") or []
    
    if not image_urls and not images_b64:
        abort(400, description="image_urls or images_base64 required")
    
    sources = image_urls if image_urls else images_b64
    if len(sources) > MAX_IMAGES:
        abort(400, description=f"too many images (max {MAX_IMAGES})")

    work = tempfile.mkdtemp(prefix="short_")
    try:
        audio_path = os.path.join(work, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        image_paths = []
        for idx, src in enumerate(sources):
            dest = os.path.join(work, f"img_{idx:02d}.png")
            if image_urls:
                # Drive URL/ID
                m = re.search(r'/d/([a-zA-Z0-9_-]+)', src)
                if not m:
                    m = re.search(r'id=([a-zA-Z0-9_-]+)', src)
                if m:
                    file_id = m.group(1)
                else:
                    file_id = src.strip()
                try:
                    _download_drive_file(file_id, dest)
                except Exception as e:
                    abort(400, description=f"could not download image {idx}: {e}")
            else:
                # base64
                raw = _decode_b64(src, f"images_base64[{idx}]")
                with open(dest, "wb") as f:
                    f.write(raw)
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

        slideshow_path = os.path.join(work, "slideshow.mp4")
        run_ffmpeg([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "zoompan=z='min(zoom+0.0015,1.5)':d=125:s=1080x1920:fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", "30", slideshow_path,
        ])

        with_audio_path = os.path.join(work, "with_audio.mp4")
        run_ffmpeg([
            "ffmpeg", "-y", "-i", slideshow_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0", "-shortest", with_audio_path,
        ])

        final_path = with_audio_path
        if data.get("add_captions") and data.get("script_text"):
            subs_path = os.path.join(work, "subs.ass")
            build_captions(data["script_text"], total_duration, subs_path)
            captioned_path = os.path.join(work, "final.mp4")
            run_ffmpeg([
                "ffmpeg", "-y", "-i", with_audio_path,
                "-vf", f"ass={subs_path}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy", captioned_path,
            ])
            final_path = captioned_path

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
