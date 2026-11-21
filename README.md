---
title: ClipWay Render
emoji: 🎬
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# ClipWay Render (Hugging Face Spaces, free CPU)

Turns a YouTube clip time-range into a real playable 9:16 MP4
(1080×1920, H.264 + AAC, faststart) using yt-dlp + ffmpeg — the render
server behind ClipWay AI's "Download MP4" button.

- `GET /health` → `{"ok": true, ...}`
- `POST /render` `{"videoId": "169ZzvPxvvM", "start": 0, "end": 45}`
  → `200 video/mp4` bytes (max clip 180 s), or JSON `{"error": "..."}`
