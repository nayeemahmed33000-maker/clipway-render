"""ClipWay AI — MP4 render service on Hugging Face Spaces (Docker SDK, free CPU).

Same pipeline as modal_app.py / render_local.py: yt-dlp resolves the direct
googlevideo stream URLs, ffmpeg seeks into them and centre-crops to 9:16
(1080x1920, H.264 + AAC, faststart). No auth; clip capped at 180 s.

Endpoints:
  GET  /health          -> {"ok": true, ...}
  POST /render          {"videoId": "169ZzvPxvvM", "start": 0, "end": 45}
                        -> 200 video/mp4 bytes, or JSON {"error": ...}
Runs on HF Spaces port 7860 (see README.md front-matter, sdk: docker).
"""

import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_CLIP_SECONDS = 180

FORMAT_PREFS = [
    "bestvideo[height<=1080][vcodec!*=av01]+bestaudio/best",
    "bestvideo[height<=1080]+bestaudio/best",
]

# NOTE: commas inside min() MUST be backslash-escaped (\,) or ffmpeg's
# filtergraph parser splits the filter there.
VIDEO_FILTER = "crop=min(iw\\,ih*9/16):min(ih\\,iw*16/9),scale=1080:1920:flags=lanczos"

# Free CPU Space has 2 vCPU — serialize renders so they don't fight each other.
_RENDER_LOCK = threading.Lock()

app = FastAPI(title="clipway-render")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


def _get_stream_urls(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}"
    for fmt in FORMAT_PREFS:
        p = subprocess.run(
            ["yt-dlp", "--get-url", "-f", fmt, "--js-runtimes", "node", url],
            capture_output=True, text=True, timeout=120,
        )
        urls = [ln.strip() for ln in p.stdout.splitlines()
                if ln.strip().startswith("http")]
        if len(urls) >= 2:
            return urls[0], urls[1]
        if len(urls) == 1:
            return urls[0], None
    return None


def _ffmpeg_cut(inputs, start: int, duration: int, out_path: Path):
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", inputs[0]]
    if len(inputs) > 1 and inputs[1]:
        cmd += ["-ss", str(start), "-i", inputs[1]]
    cmd += [
        "-t", str(duration),
        "-filter:v", VIDEO_FILTER,
        "-map", "0:v:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]
    if len(inputs) > 1 and inputs[1]:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero",
            str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=540)


@app.get("/health")
def health():
    return {"ok": True, "service": "clipway-render", "maxClipSeconds": MAX_CLIP_SECONDS}


@app.get("/")
def index():
    return {"ok": True, "service": "clipway-render",
            "usage": "POST /render {videoId, start, end} -> video/mp4"}


@app.post("/render")
async def render(req: Request):
    try:
        body = await req.json()
    except Exception:
        return _err(400, "body must be JSON {videoId, start, end}")
    video_id = str(body.get("videoId") or "")
    try:
        start = int(body.get("start"))
        end = int(body.get("end"))
    except (TypeError, ValueError):
        return _err(400, "start and end must be integers (seconds)")

    if not VIDEO_ID_RE.match(video_id):
        return _err(400, "videoId must be an 11-character YouTube video ID")
    if start < 0 or end <= start:
        return _err(400, "need 0 <= start < end")
    if end - start > MAX_CLIP_SECONDS:
        return _err(400, f"clip longer than {MAX_CLIP_SECONDS}s is not supported")

    duration = end - start
    with _RENDER_LOCK:
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / f"clip-{video_id}-{start}-{end}.mp4"
            urls = _get_stream_urls(video_id)
            try:
                if urls:
                    inputs = list(urls) if urls[1] else [urls[0]]
                    _ffmpeg_cut(inputs, start, duration, out_path)
                else:
                    src_tmpl = str(Path(td) / "source.%(ext)s")
                    subprocess.run(
                        ["yt-dlp", "-f", FORMAT_PREFS[0], "--js-runtimes", "node",
                         "--merge-output-format", "mp4", "-o", src_tmpl,
                         f"https://www.youtube.com/watch?v={video_id}"],
                        check=True, capture_output=True, text=True, timeout=540,
                    )
                    src = next(Path(td).glob("source.*"), None)
                    if src is None:
                        return _err(502, "yt-dlp download produced no file")
                    _ffmpeg_cut([str(src)], start, duration, out_path)
            except subprocess.CalledProcessError as e:
                tail = (e.stderr or e.stdout or "")[-500:]
                return _err(500, f"render failed: {tail}")
            except subprocess.TimeoutExpired:
                return _err(500, "render timed out")

            data = out_path.read_bytes()
            if not data:
                return _err(500, "render produced an empty file")
            return Response(
                content=data,
                media_type="video/mp4",
                headers={"Content-Disposition":
                         f'attachment; filename="clip-{video_id}-{start}-{end}.mp4"'},
            )
