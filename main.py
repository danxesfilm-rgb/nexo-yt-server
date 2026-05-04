import os
import re
import shutil
import subprocess
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp

# Garantizar ffmpeg en PATH usando static-ffmpeg
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass  # Si falla, yt-dlp usará el ffmpeg del sistema si existe

app = FastAPI(title="NEXO YT Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

BASE_URL = (
    os.getenv("BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "http://localhost:8000"
).rstrip("/")

_COOKIES_SECRET = "/etc/secrets/cookies.txt"
COOKIES_FILE    = "/tmp/yt-cookies.txt"

if os.path.exists(_COOKIES_SECRET):
    shutil.copy2(_COOKIES_SECRET, COOKIES_FILE)


def clean_yt_url(url: str) -> str:
    m = re.search(r'(?:v=|youtu\.be/)([^&?/\s]{8,})', url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url


def safe_title(title: str) -> str:
    return re.sub(r'[^\w\s\-áéíóúñü]', '', title, flags=re.I).strip()[:80] or 'video'


def cookies_opts() -> dict:
    return {"cookiefile": COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "NEXO YT Server"}


# ── Debug: captura stderr de yt-dlp para diagnóstico ─────────────────────────
@app.get("/debug")
def debug_info(url: str = Query(...)):
    import json as _json
    clean_url = clean_yt_url(url)

    ver = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)

    # Sin cookies
    cmd_no_cookies = ["yt-dlp", "--dump-json", "--no-playlist", "--no-warnings", clean_url]
    r_no = subprocess.run(cmd_no_cookies, capture_output=True, text=True, timeout=40)

    # Con cookies
    cmd_cookies = ["yt-dlp", "--dump-json", "--no-playlist", "--no-warnings"]
    if os.path.exists(COOKIES_FILE):
        cmd_cookies += ["--cookies", COOKIES_FILE]
    cmd_cookies.append(clean_url)
    r_co = subprocess.run(cmd_cookies, capture_output=True, text=True, timeout=40)

    # Parseo del JSON si exitoso
    formats_count = 0
    if r_co.returncode == 0 and r_co.stdout:
        try:
            info = _json.loads(r_co.stdout)
            formats_count = len(info.get("formats", []))
        except Exception:
            pass

    return {
        "yt_dlp_version": ver.stdout.strip(),
        "cookies_file_exists": os.path.exists(COOKIES_FILE),
        "no_cookies_rc": r_no.returncode,
        "no_cookies_stderr": r_no.stderr[:500] if r_no.stderr else "",
        "with_cookies_rc": r_co.returncode,
        "with_cookies_stderr": r_co.stderr[:500] if r_co.stderr else "",
        "with_cookies_formats_count": formats_count,
    }


# ── Info: lista todos los formatos sin seleccionar ninguno ───────────────────
@app.get("/info")
def get_info(url: str = Query(...)):
    clean_url = clean_yt_url(url)

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        "--no-warnings",
        clean_url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode != 0:
            raise HTTPException(status_code=502, detail=f"yt-dlp: {result.stderr[:300]}")
        import json as _json
        info = _json.loads(result.stdout)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp parse error: {e}")

    vid_id    = info.get("id", "")
    title_raw = info.get("title", "Video de YouTube")
    thumbnail = info.get("thumbnail") or f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
    formats   = info.get("formats", [])

    result_formats = []
    seen_heights   = set()

    # Formatos con video (cualquier codec), ordenados de mayor a menor calidad
    video_fmts = [
        f for f in formats
        if f.get("vcodec", "none") not in ("none", None, "")
        and f.get("height")
    ]

    for f in sorted(video_fmts, key=lambda x: x.get("height", 0) or 0, reverse=True):
        height = f.get("height") or 0
        if height not in seen_heights:
            seen_heights.add(height)
            label    = f.get("format_note") or f"{height}p"
            filesize = f.get("filesize") or f.get("filesize_approx")
            result_formats.append({
                "format_id":  str(height),
                "quality":    label,
                "ext":        "mp4",
                "type":       "video",
                "filesize":   filesize,
                "stream_url": (
                    f"{BASE_URL}/stream"
                    f"?url={quote(clean_url, safe='')}"
                    f"&format_id={height}"
                    f"&ext=mp4"
                    f"&title={quote(safe_title(title_raw), safe='')}"
                ),
            })

    # Mejor audio puro → MP3
    audio_fmts = [
        f for f in formats
        if f.get("vcodec") in ("none", None, "")
        and f.get("acodec") not in ("none", None, "")
    ]
    if audio_fmts:
        best_audio = max(audio_fmts, key=lambda x: x.get("abr") or 0)
        result_formats.append({
            "format_id":  best_audio["format_id"],
            "quality":    "MP3 Audio",
            "ext":        "mp3",
            "type":       "audio",
            "filesize":   best_audio.get("filesize") or best_audio.get("filesize_approx"),
            "stream_url": (
                f"{BASE_URL}/stream"
                f"?url={quote(clean_url, safe='')}"
                f"&format_id={best_audio['format_id']}"
                f"&ext=mp3"
                f"&mode=audio"
                f"&title={quote(safe_title(title_raw), safe='')}"
            ),
        })

    if not result_formats:
        raise HTTPException(status_code=502, detail="No se encontraron formatos descargables.")

    return {"title": title_raw, "thumbnail": thumbnail, "formats": result_formats}


# ── Stream: yt-dlp + ffmpeg pipe → siempre MP4 ───────────────────────────────
@app.get("/stream")
def stream_video(
    url:       str = Query(...),
    format_id: str = Query(...),
    ext:       str = Query("mp4"),
    mode:      str = Query("video"),
    title:     str = Query("video"),
):
    clean_url    = clean_yt_url(url)
    file_title   = safe_title(title)
    is_audio     = mode == "audio"
    content_type = "audio/mpeg" if is_audio else "video/mp4"

    if is_audio:
        fmt_arg = format_id
    else:
        fmt_arg = (
            f"bestvideo[height<={format_id}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={format_id}]+bestaudio"
            f"/best[height<={format_id}]"
            f"/best"
        )

    cmd = [
        "yt-dlp",
        "-f", fmt_arg,
        "--merge-output-format", "mp4",
        "-o", "-",
        "--no-playlist",
        "--quiet",
        clean_url,
    ]

    def generate():
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    return StreamingResponse(
        generate(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file_title}.{ext}"',
            "Cache-Control":       "no-cache",
        },
    )
