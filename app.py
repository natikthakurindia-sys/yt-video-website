
# YouTube Downloader Backend
# Flask + yt-dlp
# Compatible with Windows, Linux and macOS
#
# Install dependencies:
# pip install flask flask-cors "yt-dlp[default]" gunicorn
#
# Run locally:
# python app.py

import re
import uuid
import threading
import logging
from pathlib import Path

from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import yt_dlp


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Windows: keep ffmpeg.exe in the project folder.
# Linux deployment: install FFmpeg on the Linux system.
FFMPEG_DIR = str(BASE_DIR)

print(f"[FFmpeg] {FFMPEG_DIR}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-downloader")

JOBS = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)


# ============================================================
# HELPERS
# ============================================================

VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|v=|embed/|shorts/|live/)([a-zA-Z0-9_-]{11})"
)


def extract_video_id(url: str):
    if not url:
        return None

    url = url.strip()
    match = VIDEO_ID_RE.search(url)

    if match:
        return match.group(1)

    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", url):
        return url

    return None


def human_size(size):
    if not size:
        return None

    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:150]


def make_ydl_options(extra_options=None):
    """Create common yt-dlp options for diagnostic logging."""
    options = {
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG_DIR,
        "verbose": True,
    }

    if extra_options:
        options.update(extra_options)

    return options


# ============================================================
# YOUTUBE INFO EXTRACTION
# ============================================================

def extract_info(url: str):
    options = make_ydl_options({
        "skip_download": True,
    })

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        logger.exception("YouTube information extraction failed")
        raise


def build_quality_list(info):
    """Return available MP4 heights and MP3 bitrate choices."""
    formats = info.get("formats") or []

    heights = {}

    for item in formats:
        if item.get("vcodec") in (None, "none"):
            continue

        height = item.get("height")
        if not height:
            continue

        is_mp4 = item.get("ext") in ("mp4", "m4v")
        has_audio = item.get("acodec") not in (None, "none")

        score = (
            is_mp4,
            has_audio,
            item.get("tbr") or 0,
        )

        if height not in heights or score > heights[height]["score"]:
            heights[height] = {
                "score": score,
                "id": item["format_id"],
                "ext": item.get("ext", "mp4"),
                "fps": item.get("fps"),
                "filesize": (
                    item.get("filesize")
                    or item.get("filesize_approx")
                ),
                "has_audio": has_audio,
            }

    mp4_list = []

    for height in sorted(heights.keys(), reverse=True):
        info_height = heights[height]

        if height >= 2160:
            description = "4K Ultra HD"
        elif height >= 1440:
            description = "2K Quad HD"
        elif height >= 1080:
            description = "Full HD"
        elif height >= 720:
            description = "HD"
        else:
            description = "SD"

        sub_parts = [description]
        fps = info_height["fps"]

        if fps and fps >= 50:
            sub_parts.append(f"{int(fps)}fps")

        if info_height["filesize"]:
            sub_parts.append(human_size(info_height["filesize"]))

        mp4_list.append({
            "id": str(height),
            "label": f"{height}p",
            "sub": " · ".join(sub_parts),
            "badge": "BEST" if height == max(heights.keys()) else None,
        })

    best_audio_abr = 0

    for item in formats:
        if (
            item.get("acodec") not in (None, "none")
            and item.get("vcodec") in (None, "none")
        ):
            best_audio_abr = max(
                best_audio_abr,
                item.get("abr") or 0,
            )

    if best_audio_abr == 0:
        for item in formats:
            if item.get("acodec") not in (None, "none"):
                best_audio_abr = max(
                    best_audio_abr,
                    item.get("abr") or 0,
                )

    ladder = [320, 256, 192, 128, 96]
    usable = [
        bitrate for bitrate in ladder
        if bitrate <= (best_audio_abr or 320) + 16
    ]

    if not usable:
        usable = [128]

    mp3_list = []

    for bitrate in usable:
        if bitrate >= 256:
            description = "Very good"
        elif bitrate >= 192:
            description = "Good"
        elif bitrate >= 128:
            description = "Standard"
        else:
            description = "Small size"

        if bitrate == usable[0] and bitrate >= 256:
            description = "Highest quality"

        mp3_list.append({
            "id": str(bitrate),
            "label": f"{bitrate} kbps",
            "sub": description,
            "badge": "BEST" if bitrate == usable[0] else None,
        })

    return {
        "mp4": mp4_list,
        "mp3": mp3_list,
    }


# ============================================================
# DOWNLOAD WORKER
# ============================================================

def run_download(job_id, url, fmt, quality):
    try:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "downloading"
            JOBS[job_id]["progress"] = 0

        output_template = str(
            DOWNLOAD_DIR / f"{job_id}_%(title)s.%(ext)s"
        )

        if fmt == "mp4":
            height = int(quality)

            format_selector = (
                f"best[height<={height}][ext=mp4]"
                f"[acodec!=none][vcodec!=none]"
                f"/bestvideo[height<={height}][ext=mp4]"
                f"+bestaudio[ext=m4a]"
                f"/best[height<={height}]"
            )

            ydl_options = make_ydl_options({
                "format": format_selector,
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "progress_hooks": [
                    lambda data: progress_hook(job_id, data)
                ],
            })

        else:
            bitrate = int(quality)

            ydl_options = make_ydl_options({
                "format": "bestaudio/best",
                "outtmpl": output_template,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(bitrate),
                }],
                "progress_hooks": [
                    lambda data: progress_hook(job_id, data)
                ],
            })

        with yt_dlp.YoutubeDL(ydl_options) as ydl:
            ydl.extract_info(url, download=True)
            file_path = resolve_final_path(job_id)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["path"] = str(file_path)
            JOBS[job_id]["filename"] = safe_filename(
                file_path.name.replace(f"{job_id}_", "")
            )

    except Exception as error:
        logger.exception("Download job %s failed", job_id)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(error)


def progress_hook(job_id, data):
    if data.get("status") == "downloading":
        total = (
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
            or 0
        )
        downloaded = data.get("downloaded_bytes") or 0

        if total:
            percentage = int(downloaded * 100 / total)

            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["progress"] = percentage

    elif data.get("status") == "finished":
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = 100


def resolve_final_path(job_id):
    candidates = sorted(
        [
            path for path in DOWNLOAD_DIR.iterdir()
            if path.name.startswith(f"{job_id}_")
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        raise RuntimeError("Downloaded file not found on disk")

    return candidates[0]


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.route("/api/info")
def api_info():
    raw_url = request.args.get("url", "").strip()
    video_id = extract_video_id(raw_url)

    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        info = extract_info(video_url)
        qualities = build_quality_list(info)

        return jsonify({
            "id": video_id,
            "title": info.get("title"),
            "channel": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "views": info.get("view_count"),
            "thumbnail": (
                info.get("thumbnail")
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            ),
            "qualities": qualities,
        })

    except Exception as error:
        logger.exception("API info request failed")
        return jsonify({"error": str(error)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON request"}), 400

    raw_url = (data.get("url") or "").strip()
    fmt = data.get("format", "mp4")
    quality = data.get("quality", "")

    video_id = extract_video_id(raw_url)

    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400

    if fmt not in ("mp4", "mp3"):
        return jsonify({"error": "Invalid format"}), 400

    if not str(quality).isdigit():
        return jsonify({"error": "Invalid quality"}), 400

    job_id = uuid.uuid4().hex[:12]
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "fmt": fmt,
            "quality": quality,
        }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, video_url, fmt, quality),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if job:
            job = job.copy()

    if not job:
        return jsonify({"error": "Unknown job"}), 404

    return jsonify(job)


@app.route("/api/file/<job_id>")
def api_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job = job.copy()

    if not job or job.get("status") != "done":
        abort(404)

    return send_file(
        job["path"],
        as_attachment=True,
        download_name=job["filename"],
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    print("YouTube Downloader server starting")
    print(f"Downloads directory: {DOWNLOAD_DIR}")
    print("Open http://127.0.0.1:5000")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True,
    )
