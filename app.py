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

import base64
import json
import os
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

# YouTube bot-gates the default web player client from datacenter IPs
# ("Sign in to confirm you're not a bot"). The android/tv Innertube
# clients usually still return working streams, so try them first.
PLAYER_CLIENTS = ["android", "tv", "web"]

# NOTE: commas inside min() MUST be backslash-escaped (\,) or ffmpeg's
# filtergraph parser splits the filter there.
VIDEO_FILTER = "crop=min(iw\\,ih*9/16):min(ih\\,iw*16/9),scale=1080:1920:flags=lanczos"

# YouTube cookies (optional, to unblock the "Sign in to confirm you're not
# a bot" gate on datacenter IPs). Set YT_COOKIES_B64 (base64 Netscape
# cookies.txt) or YT_COOKIES (raw Netscape text) as a Railway variable.
# Cookies are NEVER committed to the repo — they live only in the server env.
COOKIES_PATH = None
for _env_name in ("YT_COOKIES_B64", "YT_COOKIES"):
    _raw = os.environ.get(_env_name)
    if _raw:
        try:
            _data = (base64.b64decode(_raw).decode("utf-8", "replace")
                     if _env_name.endswith("B64") else _raw)
            COOKIES_PATH = "/tmp/yt-cookies.txt"
            Path(COOKIES_PATH).write_text(_data)
        except Exception:
            COOKIES_PATH = None
        break


def _cookie_args():
    return ["--cookies", COOKIES_PATH] if COOKIES_PATH else []


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
    for client in PLAYER_CLIENTS:
        for fmt in FORMAT_PREFS:
            p = subprocess.run(
                ["yt-dlp", "--get-url", "-f", fmt, "--js-runtimes", "node",
                 "--extractor-args", f"youtube:player_client={client}",
                 *_cookie_args(), url],
                capture_output=True, text=True, timeout=120,
            )
            urls = [ln.strip() for ln in p.stdout.splitlines()
                    if ln.strip().startswith("http")]
            if len(urls) >= 2:
                return urls[0], urls[1]
            if len(urls) == 1:
                return urls[0], None
    return None


def _download_source(video_id: str, src_tmpl: str):
    """Full-file download fallback, trying each player client in turn.
    Returns the downloaded Path, or None if every client failed."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    last_err = None
    for client in PLAYER_CLIENTS:
        p = subprocess.run(
            ["yt-dlp", "-f", FORMAT_PREFS[0], "--js-runtimes", "node",
             "--extractor-args", f"youtube:player_client={client}",
             *_cookie_args(),
             "--merge-output-format", "mp4", "-o", src_tmpl, url],
            capture_output=True, text=True, timeout=540,
        )
        src = next((f for f in Path(src_tmpl).parent.glob("source.*")
                    if not f.name.endswith((".part", ".ytdl", ".temp"))), None)
        if p.returncode == 0 and src is not None:
            return src
        last_err = (p.stderr or p.stdout or "")[-300:]
    raise subprocess.CalledProcessError(1, "yt-dlp",
                                        output=last_err or "all player clients failed")


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
    return {"ok": True, "service": "clipway-render", "maxClipSeconds": MAX_CLIP_SECONDS,
            "cookies_configured": COOKIES_PATH is not None}


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
                    src = _download_source(video_id, src_tmpl)
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
