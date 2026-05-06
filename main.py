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
    pass  # Si falla, yt-dlp usará el ffmpeg del sistema si existeh

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

# Render guarda Secret Files sin extensión: /etc/secrets/cookies
_COOKIES_SECRET = (
    "/etc/secrets/cookies"      # nombre real en Render (sin .txt)
    if os.path.exists("/etc/secrets/cookies")
    else "/etc/secrets/cookies.txt"
)
COOKIES_FILE = "/tmp/yt-cookies.txt"

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

    base = ["--dump-json", "--no-playlist", "--no-warnings", "--no-check-formats"]
    cookies_arg = ["--cookies", COOKIES_FILE] if os.path.exists(COOKIES_FILE) else []

    def run(extra_args):
        cmd = ["yt-dlp"] + base + extra_args + [clean_url]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=40)

    r1 = run([])                                                          # sin cookies
    r2 = run(cookies_arg)                                                 # cookies, web
    r3 = run(cookies_arg + ["--extractor-args", "youtube:player_client=tv_embedded"])   # tv_embedded
    r4 = run(cookies_arg + ["--extractor-args", "youtube:player_client=ios"])           # ios

    def fcount(r):
        try:
            import json as _j
            return len(_j.loads(r.stdout).get("formats", [])) if r.returncode == 0 else 0
        except Exception:
            return 0

    return {
        "yt_dlp_version":        subprocess.run(["yt-dlp","--version"], capture_output=True, text=True).stdout.strip(),
        "cookies_file_exists":   os.path.exists(COOKIES_FILE),
        "r1_no_cookies_rc":      r1.returncode, "r1_stderr": r1.stderr[:300],
        "r2_web_cookies_rc":     r2.returncode, "r2_stderr": r2.stderr[:300],
        "r3_tv_embedded_rc":     r3.returncode, "r3_stderr": r3.stderr[:300], "r3_formats": fcount(r3),
        "r4_ios_rc":             r4.returncode, "r4_stderr": r4.stderr[:300], "r4_formats": fcount(r4),
    }


# ── Info: lista todos los formatos sin seleccionar ninguno ───────────────────
@app.get("/info")
def get_info(url: str = Query(...)):
    clean_url = clean_yt_url(url)

    import json as _json

    def extract_info(use_cookies: bool) -> dict:
        opts = {
            "quiet":                   True,
            "no_warnings":             True,
            "skip_download":           True,
            "check_formats":           False,
            "ignore_no_formats_error": True,   # no aborta si ningún formato encaja
            "noplaylist":              True,
        }
        if use_cookies and os.path.exists(COOKIES_FILE):
            opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(clean_url, download=False)

    try:
        # Intento 1: sin cookies
        info = extract_info(use_cookies=False)
        # Si no hay formatos o hay error de bot, reintenta con cookies
        if not info or not info.get("formats"):
            info = extract_info(use_cookies=True)
        if not info:
            raise HTTPException(status_code=502, detail="No se pudo extraer información del video.")
    except HTTPException:
        raise
    except Exception as e:
        err = str(e)
        if "Sign in" in err or "bot" in err:
            try:
                info = extract_info(use_cookies=True)
            except Exception as e2:
                raise HTTPException(status_code=502, detail=f"yt-dlp: {e2}")
        else:
            raise HTTPException(status_code=502, detail=f"yt-dlp: {e}")

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

    # Detectar si necesita cookies probando primero sin ellas
    probe = subprocess.run(
        ["yt-dlp", "--dump-json", "--no-playlist", "--no-warnings", clean_url],
        capture_output=True, text=True, timeout=30,
    )
    needs_cookies = probe.returncode != 0 and "Sign in" in probe.stderr

    cmd = [
        "yt-dlp",
        "-f", fmt_arg,
        "--merge-output-format", "mp4",
        "-o", "-",
        "--no-playlist",
        "--quiet",
    ]
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    cmd.append(clean_url)

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


# ── Embed: extrae URL directa del video desde la página embed de Instagram ───────────
@app.get("/embed")
def instagram_embed(url: str = Query(...)):
    """Obtiene la URL directa de MP4 de un post de Instagram scrapeando su embed page."""
    import re as _re
    import http.cookiejar as _cj
    import urllib.request as _ur

    m = _re.search(r'/(p|reel|tv|reels)/([A-Za-z0-9_-]+)', url)
    if not m:
        raise HTTPException(status_code=400, detail="URL de Instagram invalida")
    shortcode = m.group(2)

    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": "https://www.instagram.com/",
    }

    # Parsear cookies del archivo Netscape e inyectarlas como header Cookie
    if os.path.exists(COOKIES_FILE):
        try:
            cj = _cj.MozillaCookieJar()
            cj.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
            ig_cookies = [(c.name, c.value) for c in cj if "instagram" in c.domain]
            if ig_cookies:
                headers["Cookie"] = "; ".join(f"{n}={v}" for n, v in ig_cookies)
        except Exception:
            pass

    try:
        req = _ur.Request(embed_url, headers=headers)
        with _ur.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error fetching embed: {e}")

    # Extraer URL del video
    video_url = None
    for pat in [
        r'"video_url"\s*:\s*"([^"]+)"',
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video:secure_url["\']',
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
    ]:
        match = _re.search(pat, html)
        if match:
            video_url = match.group(1).replace("\\u0026", "&").replace("\\\\", "")
            break

    if not video_url:
        raise HTTPException(status_code=404, detail="No se encontro URL del video en el embed de Instagram")

    # Extraer thumbnail
    thumb_url = ""
    for pat in [
        r'"thumbnail_src"\s*:\s*"([^"]+)"',
        r'"display_url"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]:
        match = _re.search(pat, html)
        if match:
            thumb_url = match.group(1).replace("\\u0026", "&").replace("\\\\", "")
            break

    # Extraer título
    title = ""
    mt = _re.search(r'<title>([^<]+)</title>', html)
    if mt:
        title = _re.sub(r'\s*[•·]\s*Instagram.*$', '', mt.group(1), flags=_re.I).strip()

    return {"video_url": video_url, "thumbnail": thumb_url, "title": title or "Post de Instagram"}
