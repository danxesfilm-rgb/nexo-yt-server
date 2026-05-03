import os
import re
import shutil
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import httpx

app = FastAPI(title="NEXO YT Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# Render inyecta RENDER_EXTERNAL_URL automáticamente
BASE_URL = (
    os.getenv("BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "http://localhost:8000"
).rstrip("/")

# Cookies de YouTube — copiadas a /tmp/ porque /etc/secrets/ es read-only
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


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "NEXO YT Server"}


# ── Info: devuelve título, thumbnail y lista de formatos ──────────────────────
@app.get("/info")
def get_info(url: str = Query(...)):
    clean_url = clean_yt_url(url)

    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,
        "format":        "bestvideo+bestaudio/best",  # evita "format not available"
        **({"cookiefile": COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp: {e}")

    vid_id    = info.get("id", "")
    title_raw = info.get("title", "Video de YouTube")
    thumbnail = info.get("thumbnail") or f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"
    formats   = info.get("formats", [])

    result_formats = []
    seen_heights   = set()

    # Formatos video+audio combinados — cualquier contenedor (mp4, webm, etc.)
    combined = [
        f for f in formats
        if f.get("vcodec", "none") not in ("none", None)
        and f.get("acodec", "none") not in ("none", None)
        and f.get("height")
    ]
    for f in sorted(combined, key=lambda x: x.get("height", 0) or 0, reverse=True):
        height = f.get("height") or 0
        if height not in seen_heights:
            seen_heights.add(height)
            ext   = f.get("ext") or "mp4"
            label = f.get("format_note") or f"{height}p"
            result_formats.append({
                "format_id":  f["format_id"],
                "quality":    label,
                "ext":        ext,
                "type":       "video",
                "filesize":   f.get("filesize") or f.get("filesize_approx"),
                "stream_url": (
                    f"{BASE_URL}/stream"
                    f"?url={quote(clean_url, safe='')}"
                    f"&format_id={f['format_id']}"
                    f"&ext={ext}"
                    f"&title={quote(safe_title(title_raw), safe='')}"
                ),
            })

    # Mejor audio puro
    audio_fmts = [
        f for f in formats
        if f.get("vcodec") in ("none", None)
        and f.get("acodec") not in ("none", None)
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

    return {
        "title":     title_raw,
        "thumbnail": thumbnail,
        "formats":   result_formats,
    }


# ── Stream: obtiene URL directa con yt-dlp y la proxia al cliente ─────────────
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
    content_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    # Pedimos la info fresca desde esta IP del servidor → URLs firmadas para nosotros
    ydl_opts = {
        "quiet":         True,
        "no_warnings":   True,
        "skip_download": True,
        "format":        format_id,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)

        all_fmts   = info.get("formats", [])
        fmt        = next((f for f in all_fmts if f.get("format_id") == format_id), None)

        # Fallback: si el formato exacto no aparece, tomamos el mejor disponible
        if not fmt:
            if mode == "audio":
                candidates = [f for f in all_fmts if f.get("vcodec") in ("none", None) and f.get("url")]
                fmt = max(candidates, key=lambda f: f.get("abr") or 0) if candidates else None
            else:
                candidates = [f for f in all_fmts if f.get("vcodec") not in ("none", None)
                              and f.get("acodec") not in ("none", None) and f.get("url")]
                fmt = max(candidates, key=lambda f: f.get("height") or 0) if candidates else None

        if not fmt or not fmt.get("url"):
            raise HTTPException(status_code=404, detail="Formato no encontrado.")

        direct_url   = fmt["url"]
        http_headers = {
            **fmt.get("http_headers", {}),
            "User-Agent": "Mozilla/5.0",
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp: {e}")

    # Proxy del stream: Render descarga de YouTube y lo manda al browser en chunks
    def generate():
        with httpx.stream(
            "GET",
            direct_url,
            headers=http_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, read=300.0),
        ) as r:
            for chunk in r.iter_bytes(chunk_size=65536):
                yield chunk

    return StreamingResponse(
        generate(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file_title}.{ext}"',
            "Cache-Control":       "no-cache",
        },
    )
