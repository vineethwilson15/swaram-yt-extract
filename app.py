"""
Swaram YouTube Audio Extraction Microservice

Lightweight FastAPI service that extracts audio from YouTube videos using yt-dlp.
Designed to run on free platforms (Render, etc.) where youtube.com is accessible.

Authentication: PO Tokens (via bgutil HTTP server on localhost:4416) eliminate the
need for manual cookie rotation. Cookies are kept as optional fallback only.

Called by the main chord-service on HF Spaces when Piped proxy fails.
"""

import os
import re
import asyncio
import tempfile
import logging
import time
import random
import base64
import urllib.request
import urllib.error
from collections import OrderedDict
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VERSION = "3.0.0"
MAX_FILE_SIZE = 50 * 1024 * 1024       # 50 MB
MAX_DURATION_SEC = 600                   # 10 min
DOWNLOAD_TIMEOUT = max(30, int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "120")))
MIN_AUDIO_BYTES = 10_000                 # 10 KB
YT_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')
MAX_CONCURRENT_EXTRACTS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTS", "1")))
YTDLP_MAX_ATTEMPTS = max(1, int(os.getenv("YTDLP_MAX_ATTEMPTS", "4")))
YTDLP_BACKOFF_BASE_SEC = max(1, int(os.getenv("YTDLP_BACKOFF_BASE_SEC", "5")))
YTDLP_FRAGMENT_CONCURRENCY = max(1, int(os.getenv("YTDLP_FRAGMENT_CONCURRENCY", "2")))
PLAYER_CLIENTS = [
    c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "mweb,web,ios,android").split(",")
    if c.strip()
] or ["mweb"]

# Clients that do NOT support cookies. When cookies are loaded, these clients
# are silently skipped by yt-dlp and waste a retry attempt. We keep them in
# the default list for cookie-less operation, but dynamically reorder/exclude
# them at request time based on cookie availability.
_NON_COOKIE_CLIENTS = {"ios", "android"}


def _effective_player_clients() -> list[str]:
    """Return player clients ordered by suitability for the current cookie state.

    - Cookies loaded: cookie-compatible clients first, non-cookie clients last.
    - No cookies: default order from PLAYER_CLIENTS.
    """
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        compatible = [c for c in PLAYER_CLIENTS if c not in _NON_COOKIE_CLIENTS]
        incompatible = [c for c in PLAYER_CLIENTS if c in _NON_COOKIE_CLIENTS]
        result = compatible + incompatible
        if not result:
            return ["mweb"]
        return result
    return PLAYER_CLIENTS

# Optional proxy(ies) for yt-dlp requests (residential/rotating proxy recommended).
# Mitigates IP-level 429 throttling from YouTube that cookies/PO tokens cannot fix.
# Accepts one or more comma-separated proxy URLs:
#   YTDLP_PROXY_URL=http://user:pass@host1:port,http://user:pass@host2:port
# Format per entry: http://user:pass@host:port or socks5://host:port. No-op if unset.
PROXY_URLS = [
    p.strip() for p in os.getenv("YTDLP_PROXY_URL", "").split(",")
    if p.strip()
]

# Pre-warm the EJS/nsig challenge-solver into --cache-dir at startup so a fresh
# container (Render free tier restarts often) doesn't pay for a cold-cache
# component fetch (and possible failure) on the first real user request.
YTDLP_WARMUP_ENABLED = os.getenv("YTDLP_WARMUP_ENABLED", "false").strip().lower() not in ("false", "0", "no")
YTDLP_WARMUP_VIDEO_ID = os.getenv("YTDLP_WARMUP_VIDEO_ID", "jNQXAC9IVRw")
# Cap how many proxies the warmup task will cycle through — it only needs ONE
# success to prime the nsig cache, so trying all configured proxies on every
# cold start just burns proxy bandwidth quota for no extra benefit.
YTDLP_WARMUP_MAX_PROXIES = max(1, int(os.getenv("YTDLP_WARMUP_MAX_PROXIES", "2")))

# Try a direct (no-proxy) connection on the first attempt before falling back
# to the proxy rotation. Proxy bandwidth (esp. free tiers) is a limited/costly
# resource — the audio download itself is the expensive part. If YouTube's
# IP-level block on this host isn't 100% consistent, this avoids spending any
# proxy quota at all on requests that would have succeeded directly anyway.
YTDLP_TRY_DIRECT_FIRST = os.getenv("YTDLP_TRY_DIRECT_FIRST", "false").strip().lower() not in ("false", "0", "no")

# API key shared with HF Spaces backend (set via environment variable)
API_KEY = os.getenv("API_KEY", "")

# yt-dlp cache directory — stores nsig cache, EJS solver, etc.
YTDLP_CACHE_DIR = "/app/.ytdlp-cache"

# Persistent directory for cached extracted audio files.
# Defaults to a subdirectory of YTDLP_CACHE_DIR so it survives container
# warmup cache clears and OS tmp cleanup between requests.
YTDLP_FILE_CACHE_DIR = os.getenv("YTDLP_FILE_CACHE_DIR", os.path.join(YTDLP_CACHE_DIR, "files"))

# YouTube cookies — optional fallback for cloud IP extraction.
# PO tokens (via bgutil server on localhost:4416) are the primary auth method.
# Set YT_COOKIES_B64 env var to base64-encoded Netscape cookies.txt content
# ONLY if PO tokens alone are insufficient (rare).
YT_COOKIES_FILE = None  # Set at startup if cookies are available

# ---------------------------------------------------------------------------
# Server-side LRU cache for extracted audio files
# ---------------------------------------------------------------------------
class FileCache:
    """In-memory LRU cache for extracted audio files keyed by video_id."""

    def __init__(self, ttl_sec: int = 3600, max_size: int = 50):
        self.ttl_sec = ttl_sec
        self.max_size = max_size
        self._cache: OrderedDict[str, dict] = OrderedDict()

    def get(self, video_id: str) -> str | None:
        if video_id not in self._cache:
            return None
        entry = self._cache[video_id]
        if time.time() > entry["expires"] or not os.path.exists(entry["path"]):
            self._remove(video_id)
            return None
        self._cache.move_to_end(video_id)
        return entry["path"]

    def put(self, video_id: str, path: str):
        if video_id in self._cache:
            self._remove(video_id)
        self._cache[video_id] = {"path": path, "expires": time.time() + self.ttl_sec}
        while len(self._cache) > self.max_size:
            self._remove(next(iter(self._cache)))

    def _remove(self, video_id: str):
        entry = self._cache.pop(video_id, None)
        if entry and os.path.exists(entry["path"]):
            try:
                os.unlink(entry["path"])
            except OSError:
                pass

    def cleanup_expired(self):
        now = time.time()
        for vid in list(self._cache.keys()):
            if now > self._cache[vid]["expires"]:
                self._remove(vid)


file_cache = FileCache(
    ttl_sec=int(os.getenv("CACHE_TTL_SEC", "7200")),
    max_size=int(os.getenv("CACHE_MAX_SIZE", "100")),
)


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter
# ---------------------------------------------------------------------------
class SimpleRateLimiter:
    """Sliding-window rate limiter keyed by client IP."""

    def __init__(self, max_requests: int = 20, window_sec: int = 60):
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._history: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        if key not in self._history:
            self._history[key] = []
        cutoff = now - self.window_sec
        self._history[key] = [t for t in self._history[key] if t > cutoff]
        if len(self._history[key]) >= self.max_requests:
            return False
        self._history[key].append(now)
        return True


rate_limiter = SimpleRateLimiter(
    max_requests=int(os.getenv("RATE_LIMIT_MAX", "20")),
    window_sec=int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60")),
)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-extract")

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Swaram YT Extract", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


class BandwidthTrackerMiddleware:
    """Log bytes served per /extract request for egress monitoring."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        bytes_sent = 0
        status_code = 0

        async def send_with_tracking(message):
            nonlocal bytes_sent, status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                bytes_sent += len(body)
            await send(message)

        await self.app(scope, receive, send_with_tracking)

        path = scope.get("path", "/")
        if path == "/extract" and status_code == 200:
            logger.info(
                f"[bandwidth] served {bytes_sent} bytes ({bytes_sent / 1024 / 1024:.2f} MB)"
            )


app.add_middleware(BandwidthTrackerMiddleware)

# Track extractor concurrency
_extract_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTS)


@app.on_event("startup")
def _clear_ytdlp_cache():
    """Clear yt-dlp cache on startup to avoid stale player/signature data causing 403s.

    Preserves the file cache directory so previously extracted audio files
    remain available across container restarts.
    """
    if not os.path.isdir(YTDLP_CACHE_DIR):
        return
    cleared = 0
    for entry in os.listdir(YTDLP_CACHE_DIR):
        if entry == "files":
            continue
        entry_path = os.path.join(YTDLP_CACHE_DIR, entry)
        try:
            if os.path.isfile(entry_path):
                os.unlink(entry_path)
                cleared += 1
            elif os.path.isdir(entry_path):
                import shutil
                shutil.rmtree(entry_path)
                cleared += 1
        except OSError:
            pass
    if cleared:
        logger.info(f"[startup] Cleared yt-dlp cache dir ({cleared} items)")


@app.on_event("startup")
def _ensure_cache_dirs():
    """Ensure persistent file cache directory exists."""
    os.makedirs(YTDLP_FILE_CACHE_DIR, exist_ok=True)


@app.on_event("startup")
async def _start_cache_cleanup():
    """Periodically clean expired entries from the file cache."""
    async def _cleanup_loop():
        while True:
            await asyncio.sleep(300)
            file_cache.cleanup_expired()
    asyncio.create_task(_cleanup_loop())


@app.on_event("startup")
def _init_cookies():
    """Decode YT_COOKIES_B64 env var to a cookies.txt file on startup (optional fallback)."""
    global YT_COOKIES_FILE
    cookies_b64 = os.getenv("YT_COOKIES_B64", "")
    if not cookies_b64:
        logger.info("YT_COOKIES_B64 not set — using PO tokens only (no cookie fallback)")
        return
    try:
        cookies_bytes = base64.b64decode(cookies_b64)
        tmp = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", prefix="yt_cookies_", delete=False
        )
        tmp.write(cookies_bytes)
        tmp.close()
        YT_COOKIES_FILE = tmp.name
        logger.info(f"YouTube cookies loaded as fallback ({len(cookies_bytes)} bytes)")
    except Exception as e:
        logger.error(f"Failed to decode YT_COOKIES_B64: {e}")


@app.on_event("startup")
def _log_proxy_status():
    """Log whether yt-dlp proxies are configured (without leaking credentials)."""
    if PROXY_URLS:
        logger.info(f"yt-dlp proxy rotation enabled ({len(PROXY_URLS)} proxies configured)")
    else:
        logger.info("YTDLP_PROXY_URL not set — requests go direct from this host's IP")


BGUTIL_SERVER_URL = "http://127.0.0.1:4416"


@app.on_event("startup")
def _check_bgutil_server():
    """Log bgutil PO token server status (non-blocking — server may still be starting)."""
    try:
        req = urllib.request.Request(BGUTIL_SERVER_URL, method="GET")
        urllib.request.urlopen(req, timeout=2)
        logger.info(f"bgutil PO token server reachable on {BGUTIL_SERVER_URL}")
    except urllib.error.HTTPError:
        # 404 etc. means server IS running (no root route defined)
        logger.info(f"bgutil PO token server reachable on {BGUTIL_SERVER_URL}")
    except Exception:
        logger.info(f"bgutil PO token server not yet reachable — supervisord will start it")


@app.on_event("startup")
async def _warm_ytdlp_cache():
    """Kick off a background task to pre-fetch the EJS/nsig solver into --cache-dir.

    Runs asynchronously — does not block startup or health checks. Tries each
    configured proxy (or direct if none) until one succeeds, using --simulate so
    no media is actually downloaded.
    """
    if not YTDLP_WARMUP_ENABLED:
        return
    asyncio.create_task(_run_ytdlp_warmup())


async def _run_ytdlp_warmup():
    proxy_candidates = (
        random.sample(PROXY_URLS, k=min(len(PROXY_URLS), YTDLP_WARMUP_MAX_PROXIES))
        if PROXY_URLS else [None]
    )
    for proxy_url in proxy_candidates:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--simulate",
            "--skip-download",
            "--quiet",
            "--force-ipv4",
            "--cache-dir", YTDLP_CACHE_DIR,
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--socket-timeout", "15",
            "--extractor-args", "youtube:player_client=mweb",
        ]
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(f"https://www.youtube.com/watch?v={YTDLP_WARMUP_VIDEO_ID}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
            if proc.returncode == 0:
                logger.info(f"[warmup] EJS solver cache primed (proxy={_mask_proxy(proxy_url)})")
                return
            logger.warning(
                f"[warmup] attempt failed (proxy={_mask_proxy(proxy_url)}): "
                f"{stderr.decode(errors='replace')[-300:]}"
            )
        except Exception as e:
            logger.warning(f"[warmup] attempt errored (proxy={_mask_proxy(proxy_url)}): {e}")
    logger.warning("[warmup] could not prime EJS solver cache — first real request may hit a cold-cache failure")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def verify_api_key(x_api_key: str = Header(None)):
    """Verify API key if one is configured."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
@app.head("/")
async def root():
    return {"service": "Swaram YT Extract", "version": VERSION, "status": "ok"}


@app.get("/health")
@app.head("/health")
async def health():
    # Check bgutil server reachability
    pot_status = "unreachable"
    try:
        req = urllib.request.Request(BGUTIL_SERVER_URL, method="GET")
        urllib.request.urlopen(req, timeout=2)
        pot_status = "ok"
    except urllib.error.HTTPError:
        # 404 etc. means server IS running (no root route defined)
        pot_status = "ok"
    except Exception:
        pass
    return {
        "status": "ok",
        "version": VERSION,
        "po_token_server": pot_status,
        "cookies_loaded": YT_COOKIES_FILE is not None,
    }


@app.get("/extract", dependencies=[Depends(verify_api_key)])
async def extract_audio(video_id: str, request: Request):
    """
    Extract audio from a YouTube video and return the file.

    Query params:
        video_id: 11-character YouTube video ID (SSRF-safe: no arbitrary URLs)

    Returns:
        Audio file (M4A/WebM) as streaming download

    Security:
        - Only accepts validated 11-char video IDs (no arbitrary URL injection)
        - Optional API key auth via X-API-Key header
        - Max duration 10 min, max file size 50 MB
    """
    # Validate video ID (SSRF protection — only IDs, never URLs)
    if not video_id or not YT_VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video_id — must be 11 alphanumeric chars")

    # Rate limiting (per client IP, with X-Forwarded-For support)
    client_ip = _get_client_ip(request)
    if not rate_limiter.is_allowed(client_ip):
        raise HTTPException(429, "Rate limit exceeded — please retry later")

    media_types = {
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".opus": "audio/opus",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
    }

    # Check server-side cache first
    cached_path = file_cache.get(video_id)
    if cached_path and os.path.exists(cached_path):
        ext = os.path.splitext(cached_path)[1].lower()
        media_type = media_types.get(ext, "audio/mp4")
        size_mb = os.path.getsize(cached_path) / 1024 / 1024
        logger.info(f"[cache] HIT {video_id} ({size_mb:.1f} MB)")
        return FileResponse(
            path=cached_path,
            media_type=media_type,
            filename=f"{video_id}{ext}",
            headers={"Cache-Control": "public, max-age=1800"},
        )

    tmp_path = None
    try:
        # Bound extractor concurrency to avoid burst 429/rate-limit storms.
        async with _extract_semaphore:
            tmp_path = await _download_with_ytdlp(video_id)

        ext = os.path.splitext(tmp_path)[1].lower()
        media_type = media_types.get(ext, "audio/mp4")

        # Cache the file for subsequent requests
        file_cache.put(video_id, tmp_path)

        size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        logger.info(f"[cache] MISS {video_id} ({size_mb:.1f} MB) — cached for {file_cache.ttl_sec}s")

        return FileResponse(
            path=tmp_path,
            media_type=media_type,
            filename=f"{video_id}{ext}",
            headers={"Cache-Control": "public, max-age=1800"},
        )
    except HTTPException:
        _safe_unlink(tmp_path)
        raise
    except Exception as e:
        _safe_unlink(tmp_path)
        logger.error(f"Extraction failed for {video_id}: {e}")
        raise HTTPException(502, f"YouTube extraction failed: {str(e)[:200]}")


# ---------------------------------------------------------------------------
# yt-dlp extraction
# ---------------------------------------------------------------------------
async def _download_with_ytdlp(video_id: str) -> str:
    """Download audio from YouTube using yt-dlp. Returns path to temp file."""
    last_error: Exception | None = None

    # Shuffle proxies once per request so each retry attempt tries a different
    # one (up to the number available) instead of repeating a failed proxy.
    # If YTDLP_TRY_DIRECT_FIRST is on, the first attempt is reserved for a
    # direct (no-proxy) connection, so only remaining attempts need a proxy.
    direct_first = YTDLP_TRY_DIRECT_FIRST and bool(PROXY_URLS)
    proxy_attempt_budget = max(0, YTDLP_MAX_ATTEMPTS - 1) if direct_first else YTDLP_MAX_ATTEMPTS
    proxy_sequence = (
        random.sample(PROXY_URLS, k=min(len(PROXY_URLS), proxy_attempt_budget))
        if PROXY_URLS and proxy_attempt_budget else []
    )

    for attempt in range(1, YTDLP_MAX_ATTEMPTS + 1):
        tmp = tempfile.NamedTemporaryFile(suffix=".m4a", delete=False, dir=YTDLP_FILE_CACHE_DIR)
        tmp.close()
        proc = None
        effective_clients = _effective_player_clients()
        player_client = effective_clients[(attempt - 1) % len(effective_clients)]
        if direct_first and attempt == 1:
            proxy_url = None  # try direct first to avoid spending proxy bandwidth quota
        else:
            proxy_idx = (attempt - 2) if direct_first else (attempt - 1)
            proxy_url = proxy_sequence[proxy_idx % len(proxy_sequence)] if proxy_sequence else None

        try:
            logger.info(
                f"[yt-dlp] Extracting audio for {video_id} (attempt {attempt}/{YTDLP_MAX_ATTEMPTS}, "
                f"client={player_client}, proxy={_mask_proxy(proxy_url)})..."
            )
            t0 = time.time()

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", "ba/b*",                     # Audio-only first, then any format
                "-S", "+size,+br,proto:m3u8_native:m3u8:https",  # Smallest + prefer m3u8 (~6MB) over https (~30MB)
                "--force-ipv4",
                "--concurrent-fragments", str(YTDLP_FRAGMENT_CONCURRENCY),
                "--cache-dir", YTDLP_CACHE_DIR,
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "--socket-timeout", "15",
                "--retries", "1",
                "--extractor-args", f"youtube:player_client={player_client}",
                "-o", tmp.name,
                "--force-overwrites",
            ]
            # PO tokens: bgutil plugin auto-discovers HTTP server on localhost:4416
            # Cookies: optional fallback (set YT_COOKIES_B64 env var if needed)
            if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
                cmd.extend(["--cookies", YT_COOKIES_FILE])
                logger.info("[yt-dlp] Using PO tokens + cookies (fallback)")
            else:
                logger.info("[yt-dlp] Using PO tokens only (no cookies)")
            # Proxy: routes yt-dlp traffic through a rotating proxy to avoid
            # IP-level 429 throttling on this host's outbound IP.
            if proxy_url:
                cmd.extend(["--proxy", proxy_url])
            cmd.append(f"https://www.youtube.com/watch?v={video_id}")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DOWNLOAD_TIMEOUT
            )

            elapsed = time.time() - t0

            # Log which format yt-dlp selected (from stderr [info] line)
            for line in stderr.decode(errors="replace").split("\n"):
                if "[info]" in line and "format" in line.lower():
                    logger.info(f"[yt-dlp] {line.strip()}")

            # Check yt-dlp exit status
            if proc.returncode != 0:
                full_err = stderr.decode(errors="replace")
                # Extract actual error/warning lines (skip verbose debug noise)
                err_lines = [l for l in full_err.split("\n")
                             if l.startswith("ERROR:") or l.startswith("WARNING:") or "Sign in" in l]
                err_msg = "\n".join(err_lines)[:1000] if err_lines else full_err[-500:]
                logger.warning(f"[yt-dlp] Failed (exit {proc.returncode}): {err_msg}")

                # Detect specific YouTube errors
                if "Sign in to confirm" in full_err or "confirm you're not a bot" in full_err.lower():
                    raise HTTPException(503, "YouTube requires login — try again later")
                if "Video unavailable" in full_err:
                    raise HTTPException(404, "Video not found or unavailable")
                if "Private video" in full_err:
                    raise HTTPException(403, "This video is private")

                raise ValueError(f"yt-dlp exit {proc.returncode}: {err_msg[:500]}")

            # Validate output file
            if not os.path.exists(tmp.name):
                raise ValueError("Downloaded file not found")

            file_size = os.path.getsize(tmp.name)
            if file_size < MIN_AUDIO_BYTES:
                raise ValueError(f"File too small ({file_size} bytes)")
            if file_size > MAX_FILE_SIZE:
                raise ValueError(f"File too large ({file_size} bytes)")

            logger.info(f"[yt-dlp] Success: {file_size/1024/1024:.1f} MB in {elapsed:.1f}s")
            return tmp.name

        except asyncio.TimeoutError:
            logger.warning(f"[yt-dlp] Timed out after {DOWNLOAD_TIMEOUT}s")
            try:
                if proc:
                    proc.kill()
            except Exception:
                pass
            _safe_unlink(tmp.name)
            last_error = HTTPException(504, "Download timed out — video may be too long")
        except (HTTPException, ValueError) as e:
            _safe_unlink(tmp.name)
            last_error = e
            if isinstance(e, HTTPException) and e.status_code in (403, 404):
                raise
        except Exception as e:
            _safe_unlink(tmp.name)
            last_error = ValueError(f"Unexpected error: {e}")

        if attempt < YTDLP_MAX_ATTEMPTS and last_error and _is_retryable_error(last_error):
            backoff = (YTDLP_BACKOFF_BASE_SEC * (2 ** (attempt - 1))) + random.uniform(0, 1.5)
            logger.warning(
                f"[yt-dlp] Retrying {video_id} in {backoff:.1f}s due to transient upstream failure"
            )
            await asyncio.sleep(backoff)
            continue

        if last_error:
            if _is_retryable_error(last_error):
                raise HTTPException(503, "YouTube is rate-limiting requests — please retry shortly")
            raise last_error

    raise ValueError("yt-dlp failed with no captured error")


def _mask_proxy(proxy_url: str | None) -> str:
    """Return a credential-free representation of a proxy URL for logging."""
    if not proxy_url:
        return "none"
    if "@" in proxy_url:
        scheme_part, host_part = proxy_url.split("@", 1)
        scheme = scheme_part.split("://", 1)[0] if "://" in scheme_part else "proxy"
        return f"{scheme}://***@{host_part}"
    return proxy_url


def _is_retryable_error(err: Exception) -> bool:
    """Return True when error likely came from temporary upstream throttling/challenge issues."""
    if isinstance(err, HTTPException):
        if err.status_code in (503, 504):
            return True
        detail = str(err.detail).lower()
        return "too many requests" in detail

    msg = str(err).lower()
    retryable_signatures = [
        "too many requests",
        "http error 429",
        "n challenge",
        "requested format is not available",
        "only images are available",
        "timed out",
        "unable to download webpage",
        "network is unreachable",
        "failed to establish a new connection",
    ]
    # 403 during media download is usually an IP/geo block; retry only if we have
    # proxies configured, otherwise retrying is pointless.
    if PROXY_URLS and "http error 403" in msg:
        return True
    return any(sig in msg for sig in retryable_signatures)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------
def _safe_unlink(path: str | None):
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass
