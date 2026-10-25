# language: Python 3.11+, file: app.py, target: Windows/Linux/Mac
# YouTube downloader backend — yt-dlp + Flask
#
# deps:
#   pip install flask flask-cors yt-dlp
#   FFmpeg isi folder me hona chahiye (ffmpeg.exe, ffplay.exe, ffprobe.exe)
#
# run:
#   python app.py
#   open http://127.0.0.1:5000

import re
import uuid
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import yt_dlp

# ============================================================
#  CONFIG
# ============================================================
BASE_DIR     = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# FFmpeg isi folder me hai — app.py ke saath
FFMPEG_DIR = str(BASE_DIR)
print(f"[FFmpeg] {FFMPEG_DIR}")

# job_id -> {status, progress, filename, path, error}
JOBS = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)

# ============================================================
#  HELPERS
# ============================================================
VIDEO_ID_RE = re.compile(r'(?:youtu\.be/|v=|embed/|shorts/|live/)([a-zA-Z0-9_-]{11})')

def extract_video_id(url: str):
    if not url:
        return None
    url = url.strip()
    m = VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', url):
        return url
    return None

def human_size(n):
    if not n:
        return None
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()[:150]

# ============================================================
#  YT-DLP INFO EXTRACTION
# ============================================================
def extract_info(url: str):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'ffmpeg_location': FFMPEG_DIR,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def build_quality_list(info):
    """Return { 'mp4': [...], 'mp3': [...] } with only actually available qualities."""
    formats = info.get('formats') or []

    # ---- MP4: unique heights ----
    heights = {}
    for f in formats:
        if f.get('vcodec') in (None, 'none'):
            continue
        h = f.get('height')
        if not h:
            continue
        is_mp4 = (f.get('ext') in ('mp4', 'm4v'))
        has_audio = f.get('acodec') not in (None, 'none')
        score = (is_mp4, has_audio, f.get('tbr') or 0)
        if h not in heights or score > heights[h]['score']:
            heights[h] = {
                'score': score,
                'id': f['format_id'],
                'ext': f.get('ext', 'mp4'),
                'fps': f.get('fps'),
                'filesize': f.get('filesize') or f.get('filesize_approx'),
                'has_audio': has_audio,
            }

    mp4_list = []
    for h in sorted(heights.keys(), reverse=True):
        info_h = heights[h]
        label = f"{h}p"
        sub_parts = []
        if h >= 2160: sub_parts.append('4K Ultra HD')
        elif h >= 1440: sub_parts.append('2K Quad HD')
        elif h >= 1080: sub_parts.append('Full HD')
        elif h >= 720: sub_parts.append('HD')
        else: sub_parts.append('SD')
        if info_h['fps'] and info_h['fps'] >= 50:
            sub_parts.append(f"{int(info_h['fps'])}fps")
        if info_h['filesize']:
            sub_parts.append(human_size(info_h['filesize']))
        mp4_list.append({
            'id': str(h),
            'label': label,
            'sub': ' · '.join(sub_parts),
            'badge': 'BEST' if h == max(heights.keys()) else None,
        })

    # ---- MP3: derive from best audio stream ----
    best_audio_abr = 0
    for f in formats:
        if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none'):
            abr = f.get('abr') or 0
            if abr > best_audio_abr:
                best_audio_abr = abr
    if best_audio_abr == 0:
        for f in formats:
            if f.get('acodec') not in (None, 'none'):
                abr = f.get('abr') or 0
                if abr > best_audio_abr:
                    best_audio_abr = abr

    ladder = [320, 256, 192, 128, 96]
    usable = [b for b in ladder if b <= (best_audio_abr or 320) + 16]
    if not usable:
        usable = [128]

    mp3_list = []
    for b in usable:
        mp3_list.append({
            'id': str(b),
            'label': f"{b} kbps",
            'sub': 'Highest quality' if b == usable[0] and b >= 256 else
                   'Very good' if b >= 256 else
                   'Good' if b >= 192 else
                   'Standard' if b >= 128 else 'Small size',
            'badge': 'BEST' if b == usable[0] else None,
        })

    return {'mp4': mp4_list, 'mp3': mp3_list}

# ============================================================
#  DOWNLOAD WORKER
# ============================================================
def run_download(job_id, url, fmt, quality):
    try:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'downloading'
            JOBS[job_id]['progress'] = 0

        outtmpl = str(DOWNLOAD_DIR / f"{job_id}_%(title)s.%(ext)s")

        if fmt == 'mp4':
            h = int(quality)
            fmt_selector = (
                f"best[height<={h}][ext=mp4][acodec!=none][vcodec!=none]"
                f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/best[height<={h}]"
            )
            ydl_opts = {
                'format': fmt_selector,
                'outtmpl': outtmpl,
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'ffmpeg_location': FFMPEG_DIR,
                'progress_hooks': [lambda d: _hook(job_id, d)],
            }
        else:  # mp3
            bitrate = int(quality)
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': outtmpl,
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'ffmpeg_location': FFMPEG_DIR,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': str(bitrate),
                }],
                'progress_hooks': [lambda d: _hook(job_id, d)],
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            path = _resolve_final_path(job_id)

        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'done'
            JOBS[job_id]['progress'] = 100
            JOBS[job_id]['path'] = str(path)
            JOBS[job_id]['filename'] = safe_filename(Path(path).name.replace(f"{job_id}_", ""))

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error'] = str(e)

def _hook(job_id, d):
    if d.get('status') == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes') or 0
        if total:
            pct = int(done * 100 / total)
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]['progress'] = pct
    elif d.get('status') == 'finished':
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]['progress'] = 100

def _resolve_final_path(job_id):
    candidates = sorted(
        [p for p in DOWNLOAD_DIR.iterdir() if p.name.startswith(f"{job_id}_")],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    if not candidates:
        raise RuntimeError("Downloaded file not found on disk")
    return candidates[0]

# ============================================================
#  ROUTES
# ============================================================
@app.route('/')
def index():
    return send_file(BASE_DIR / 'index.html')

@app.route('/api/info')
def api_info():
    raw = request.args.get('url', '').strip()
    vid = extract_video_id(raw)
    if not vid:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    try:
        info = extract_info(f"https://www.youtube.com/watch?v={vid}")
        qualities = build_quality_list(info)
        return jsonify({
            'id': vid,
            'title': info.get('title'),
            'channel': info.get('uploader') or info.get('channel'),
            'duration': info.get('duration'),
            'views': info.get('view_count'),
            'thumbnail': info.get('thumbnail') or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            'qualities': qualities,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(force=True)
    raw = (data.get('url') or '').strip()
    fmt = data.get('format', 'mp4')
    quality = data.get('quality', '')
    vid = extract_video_id(raw)
    if not vid:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    if fmt not in ('mp4', 'mp3'):
        return jsonify({'error': 'Invalid format'}), 400
    if not quality:
        return jsonify({'error': 'No quality selected'}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            'status': 'queued',
            'progress': 0,
            'fmt': fmt,
            'quality': quality,
        }
    t = threading.Thread(
        target=run_download,
        args=(job_id, f"https://www.youtube.com/watch?v={vid}", fmt, quality),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': job_id})

@app.route('/api/progress/<job_id>')
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Unknown job'}), 404
    return jsonify(job)

@app.route('/api/file/<job_id>')
def api_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get('status') != 'done':
        abort(404)
    return send_file(job['path'], as_attachment=True, download_name=job['filename'])

# ============================================================
#  ENTRY
# ============================================================
if __name__ == '__main__':
    print("LUNAR YouTube Downloader — server starting")
    print(f"  downloads dir: {DOWNLOAD_DIR}")
    print("  open http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)