"""
AI Shorts Generator microservice.
Combines AI voiceover + AI images into a 9:16 vertical YouTube Short.

Endpoints:
  POST /assemble   — combine audio + images + captions → final video
  GET  /health     — health check
"""
import os
import uuid
import subprocess
import shutil
import base64
import json
import requests
from flask import Flask, request, send_file, jsonify, abort

app = Flask(__name__)

WORK_DIR = os.environ.get("WORK_DIR", "/tmp/shorts")
API_KEY = os.environ.get("API_KEY")
os.makedirs(WORK_DIR, exist_ok=True)


def require_api_key():
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_KEY}":
        abort(401, description="Invalid or missing API key")


def run_ffmpeg(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-2000:]}")


def get_audio_duration(audio_path: str) -> float:
    """Get duration of audio file in seconds."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def ms_to_ass_time(ms: int) -> str:
    cs = ms // 10
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_captions(script_text: str, total_duration: float, out_path: str):
    """Build word-by-word ASS captions from script text."""
    words = script_text.split()
    if not words:
        return

    duration_per_word_ms = int((total_duration * 1000) / len(words))

    style_line = (
        "Style: Default,Liberation Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,8,2,2,40,40,240,1"
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
    current_ms = 0
    for i in range(0, len(words), PHRASE_SIZE):
        phrase = words[i:i + PHRASE_SIZE]
        phrase_duration = duration_per_word_ms * len(phrase)
        start_ms = current_ms
        end_ms = current_ms + phrase_duration
        text = " ".join(w.upper() for w in phrase)
        events.append(
            f"Dialogue: 0,{ms_to_ass_time(start_ms)},{ms_to_ass_time(end_ms)},Default,,0,0,0,,{text}"
        )
        current_ms = end_ms

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events))


@app.get("/health")
def health():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        return jsonify({"status": "ok", "ffmpeg": "available", "service": "ai-shorts-v3"})
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


@app.post("/assemble")
def assemble():
    """
    Assemble a 9:16 vertical short from AI-generated audio + images.
    
    Expected JSON body:
    {
        "audio_url": "https://...",      // ElevenLabs audio URL or base64
        "audio_base64": "...",           // Alternative: base64 audio
        "images": [                       // List of image URLs or base64
            "https://...",
            ...
        ],
        "images_base64": [...],          // Alternative: list of base64 images
        "script_text": "Full script...", // For captions
        "add_captions": true,
        "music_url": null                // Optional background music URL
    }
    """
    require_api_key()
    data = request.get_json(force=True)
    if not data:
        abort(400, description="JSON body required")

    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # Step 1: Get audio file
        audio_path = os.path.join(job_dir, "audio.mp3")
        if data.get("audio_base64"):
            with open(audio_path, "wb") as f:
                f.write(base64.b64decode(data["audio_base64"]))
        elif data.get("audio_url"):
            r = requests.get(data["audio_url"], timeout=60)
            r.raise_for_status()
            with open(audio_path, "wb") as f:
                f.write(r.content)
        else:
            abort(400, description="audio_url or audio_base64 required")

        # Get audio duration
        total_duration = get_audio_duration(audio_path)
        if total_duration < 5 or total_duration > 90:
            abort(400, description=f"Audio duration must be 5-90 sec (got {total_duration:.1f}s)")

        # Step 2: Save images
        images = data.get("images", [])
        images_base64 = data.get("images_base64", [])
        
        image_paths = []
        if images_base64:
            for i, img_b64 in enumerate(images_base64):
                img_path = os.path.join(job_dir, f"img_{i:02d}.png")
                with open(img_path, "wb") as f:
                    f.write(base64.b64decode(img_b64))
                image_paths.append(img_path)
        elif images:
            for i, img_url in enumerate(images):
                r = requests.get(img_url, timeout=30)
                r.raise_for_status()
                img_path = os.path.join(job_dir, f"img_{i:02d}.png")
                with open(img_path, "wb") as f:
                    f.write(r.content)
                image_paths.append(img_path)
        else:
            abort(400, description="images or images_base64 required")

        if not image_paths:
            abort(400, description="At least 1 image required")

        # Step 3: Calculate per-image duration
        duration_per_image = total_duration / len(image_paths)

        # Step 4: Create video from images (slideshow)
        # Build concat file
        concat_file = os.path.join(job_dir, "concat.txt")
        with open(concat_file, "w") as f:
            for img in image_paths:
                f.write(f"file '{img}'\n")
                f.write(f"duration {duration_per_image:.3f}\n")
            # Last image needs to be repeated for proper duration
            f.write(f"file '{image_paths[-1]}'\n")

        # Create slideshow with ken-burns effect (subtle zoom)
        slideshow_path = os.path.join(job_dir, "slideshow.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,zoompan=z='min(zoom+0.0015,1.5)':d=125:s=1080x1920:fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            slideshow_path,
        ])

        # Step 5: Combine audio with video
        with_audio_path = os.path.join(job_dir, "with_audio.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-i", slideshow_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            with_audio_path,
        ])

        # Step 6: Add captions if requested
        final_path = with_audio_path
        if data.get("add_captions") and data.get("script_text"):
            subs_path = os.path.join(job_dir, "subs.ass")
            build_captions(data["script_text"], total_duration, subs_path)
            
            captioned_path = os.path.join(job_dir, "final.mp4")
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", with_audio_path,
                "-vf", f"ass={subs_path}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "copy",
                captioned_path,
            ])
            final_path = captioned_path

        # Return the final video
        return send_file(
            final_path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"short_{job_id}.mp4"
        )

    except requests.HTTPError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        abort(502, description=f"Could not download media: {e}")
    except ValueError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        abort(400, description=str(e))
    except RuntimeError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
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
