"""
Swaram YouTube Audio Extraction Microservice

Lightweight FastAPI service that extracts audio from YouTube videos using yt-dlp.
Designed to run on free platforms (Render, etc.) where youtube.com is accessible.

Authentication: Cookies are preferred when configured; PO Tokens (via bgutil HTTP
server on localhost:4416) provide the fallback for cloud IP extraction.

Called by the main chord-service on HF Spaces when Piped proxy fails.
"""

import os
import glob
import re
import asyncio
import shutil
import tempfile
import logging
import time
import base64
import urllib.request
import urllib.error
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VERSION = "3.0.0"
MAX_FILE_SIZE = 50 * 1024 * 1024       # 50 MB
MAX_DURATION_SEC = 600                   # 10 min
DOWNLOAD_TIMEOUT = 120                   # seconds (includes PO token generation)
MIN_AUDIO_BYTES = 10_000                 # 10 KB
MAX_AUDIO_BITRATE = 96                   # Compact audio that remains suitable for BTC chords (kbps)
MAX_CONCURRENT_EXTRACTIONS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "1")))
CONCURRENT_FRAGMENTS = max(1, int(os.getenv("YT_CONCURRENT_FRAGMENTS", "2")))
FORMAT_SORT = os.getenv("YT_FORMAT_SORT", "+size,+br,proto:https:m3u8_native:m3u8")
PRIMARY_PLAYER_CLIENT = os.getenv("YT_PRIMARY_CLIENT", "web_creator").strip()
SKIP_MANIFESTS = os.getenv("YT_SKIP_MANIFESTS", "1").strip().lower() not in {"0", "false", "no"}
CHECK_FORMATS = os.getenv("YT_CHECK_FORMATS", "1").strip().lower() in {"1", "true", "yes"}
EXTRACTOR_RETRIES = max(1, int(os.getenv("YT_EXTRACTOR_RETRIES", "1")))
YT_VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')

# API key shared with HF Spaces backend (required environment variable)
API_KEY = os.getenv("API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("API_KEY environment variable is required")

# yt-dlp cache directory — stores nsig cache, EJS solver, etc.
YTDLP_CACHE_DIR = "/app/.ytdlp-cache"

# YouTube cookies — preferred authentication for cloud IP extraction.
# PO tokens (via bgutil server on localhost:4416) are the fallback method.
# Set YT_COOKIES_B64 env var to base64-encoded Netscape cookies.txt content
# ONLY if PO tokens alone are insufficient (rare).
YT_COOKIES_FILE = None  # Set at startup if cookies are available

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

# Track temp files for cleanup
_active_files: set[str] = set()
_extraction_slots = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
_inflight_downloads: dict[str, asyncio.Task[str]] = {}
_inflight_waiters: dict[str, int] = {}
_inflight_resolutions: dict[str, asyncio.Task[str]] = {}


@app.on_event("startup")
def _init_cookies():
    """Decode YT_COOKIES_B64 env var to a cookies.txt file on startup (optional fallback)."""
    global YT_COOKIES_FILE
    cookies_b64 = os.getenv("YT_COOKIES_B64", "")
    if not cookies_b64:
        logger.info("YT_COOKIES_B64 not set — using PO tokens only")
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


BGUTIL_SERVER_URL = "http://127.0.0.1:4416"
BGUTIL_READY = False


def _bgutil_is_reachable() -> bool:
    try:
        req = urllib.request.Request(BGUTIL_SERVER_URL, method="GET")
        urllib.request.urlopen(req, timeout=1)
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False
    return True


async def _wait_for_bgutil(timeout: float = 15.0) -> bool:
    global BGUTIL_READY
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_bgutil_is_reachable):
            BGUTIL_READY = True
            return True
        await asyncio.sleep(0.5)
    BGUTIL_READY = False
    return False


@app.on_event("startup")
def _check_bgutil_server():
    """Wait briefly for bgutil before advertising the service as ready."""
    global BGUTIL_READY
    for _ in range(20):
        if _bgutil_is_reachable():
            BGUTIL_READY = True
            logger.info(f"bgutil PO token server reachable on {BGUTIL_SERVER_URL}")
            return
        time.sleep(0.5)
    logger.error(f"bgutil PO token server is unreachable on {BGUTIL_SERVER_URL}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
async def verify_api_key(x_api_key: str = Header(None)):
    """Verify the required API key."""
    if x_api_key != API_KEY:
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
    global BGUTIL_READY
    BGUTIL_READY = _bgutil_is_reachable()
    payload = {
        "status": "ok",
        "version": VERSION,
        "po_token_server": "ok" if BGUTIL_READY else "unreachable",
        "cookies_loaded": YT_COOKIES_FILE is not None,
    }
    if not BGUTIL_READY:
        return JSONResponse(payload | {"status": "degraded"}, status_code=503)
    return payload


@app.get("/extract", dependencies=[Depends(verify_api_key)])
async def extract_audio(video_id: str, delivery: str = "binary"):
    """
    Extract audio from a YouTube video and return the file.

    Query params:
        video_id: 11-character YouTube video ID (SSRF-safe: no arbitrary URLs)
        delivery: binary (default) or redirect to YouTube's temporary audio URL

    Returns:
        Audio file (M4A/WebM) as streaming download

    Security:
        - Only accepts validated 11-char video IDs (no arbitrary URL injection)
        - Required API key auth via X-API-Key header
        - Max duration 10 min, max file size 50 MB
    """
    # Validate video ID (SSRF protection — only IDs, never URLs)
    if not video_id or not YT_VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video_id — must be 11 alphanumeric chars")
    if delivery not in {"binary", "redirect"}:
        raise HTTPException(400, "Invalid delivery — use binary or redirect")
    if not YT_COOKIES_FILE and not await _wait_for_bgutil():
        raise HTTPException(503, "PO token server is not ready")

    if delivery == "redirect":
        audio_url = await _resolve_audio_url(video_id)
        return RedirectResponse(audio_url, status_code=302)

    tmp_path = None
    try:
        tmp_path = await _download_with_ytdlp(video_id)

        # Determine media type from extension
        ext = os.path.splitext(tmp_path)[1].lower()
        media_types = {
            ".m4a": "audio/mp4",
            ".webm": "audio/webm",
            ".opus": "audio/opus",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
        }
        media_type = media_types.get(ext, "audio/mp4")

        _active_files.add(tmp_path)

        return FileResponse(
            path=tmp_path,
            media_type=media_type,
            filename=f"{video_id}{ext}",
            background=_cleanup_task(tmp_path),
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
    """Coalesce concurrent requests, returning a private response file to each caller."""
    task = _inflight_downloads.get(video_id)
    if task is None:
        task = asyncio.create_task(_download_with_ytdlp_limited(video_id))
        _inflight_downloads[video_id] = task
        _inflight_waiters[video_id] = 0
    else:
        logger.info(f"[yt-dlp] Joining in-flight extraction for {video_id}")

    _inflight_waiters[video_id] += 1
    try:
        source_path = await asyncio.shield(task)
        return _copy_for_response(source_path)
    finally:
        _inflight_waiters[video_id] -= 1
        if _inflight_waiters[video_id] == 0:
            _inflight_downloads.pop(video_id, None)
            _inflight_waiters.pop(video_id, None)
            if task.done() and not task.cancelled() and task.exception() is None:
                _safe_unlink(task.result())


async def _download_with_ytdlp_limited(video_id: str) -> str:
    """Run one yt-dlp extraction while the caller holds an extraction slot."""
    wait_started = time.perf_counter()
    async with _extraction_slots:
        queue_time = time.perf_counter() - wait_started
        if queue_time >= 0.1:
            logger.info(
                f"[yt-dlp] {video_id} waited {queue_time:.2f}s for an extraction slot"
            )
        return await _download_with_ytdlp_process(video_id)


async def _download_with_ytdlp_process(video_id: str) -> str:
    """Run one yt-dlp subprocess and return its completed output path."""
    output_handle = tempfile.NamedTemporaryFile(prefix="yt_audio_", delete=False)
    output_base = output_handle.name
    output_handle.close()
    _safe_unlink(output_base)
    output_template = f"{output_base}.%(ext)s"

    try:
        logger.info(f"[yt-dlp] Extracting audio for {video_id}...")
        t0 = time.perf_counter()

        base_cmd = _base_ytdlp_command()
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        attempts = _build_ytdlp_attempts(video_url, base_cmd, output_template)

        proc = None
        full_err = ""
        for attempt_index, (cmd, auth_mode) in enumerate(attempts):
            attempt_started = time.perf_counter()
            for previous_output in glob.glob(f"{output_base}.*"):
                _safe_unlink(previous_output)
            logger.info(f"[yt-dlp] Trying {auth_mode} extraction")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_ytdlp_environment(),
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DOWNLOAD_TIMEOUT
            )
            full_err = stderr.decode(errors="replace")
            attempt_elapsed = time.perf_counter() - attempt_started
            logger.info(
                f"[yt-dlp] {auth_mode} attempt {attempt_index + 1}/{len(attempts)} "
                f"finished in {attempt_elapsed:.2f}s (exit {proc.returncode})"
            )

            for line in full_err.split("\n"):
                if "[info]" in line and "format" in line.lower():
                    logger.info(f"[yt-dlp] {line.strip()}")

            if proc.returncode == 0:
                break

            err_lines = [l for l in full_err.split("\n")
                         if l.startswith("ERROR:") or l.startswith("WARNING:") or "Sign in" in l]
            err_msg = "\n".join(err_lines)[:1000] if err_lines else full_err[-500:]
            logger.warning(f"[yt-dlp] {auth_mode} attempt failed (exit {proc.returncode}): {err_msg}")
            if "HTTP Error 429" in full_err or "Too Many Requests" in full_err:
                raise HTTPException(
                    503,
                    "YouTube is temporarily rate-limiting this service; retry later",
                )
            if attempt_index < len(attempts) - 1:
                next_auth_mode = attempts[attempt_index + 1][1]
                logger.info(f"[yt-dlp] Retrying with {next_auth_mode}")
        else:
            if "HTTP Error 403" in full_err or "403: Forbidden" in full_err:
                raise HTTPException(
                    503,
                    "YouTube rejected the configured cookies or service IP; refresh YT_COOKIES_B64",
                )
            if "Sign in to confirm" in full_err or "confirm you're not a bot" in full_err.lower():
                raise HTTPException(503, "YouTube requires login — try again later")
            if "Video unavailable" in full_err:
                raise HTTPException(404, "Video not found or unavailable")
            if "Private video" in full_err:
                raise HTTPException(403, "This video is private")

            raise ValueError(f"yt-dlp exit {proc.returncode}: {err_msg[:500]}")

        elapsed = time.perf_counter() - t0

        # Validate output file
        output_files = [path for path in glob.glob(f"{output_base}.*") if os.path.isfile(path)]
        if len(output_files) != 1:
            raise ValueError("Downloaded file not found")
        output_path = output_files[0]

        file_size = os.path.getsize(output_path)
        if file_size < MIN_AUDIO_BYTES:
            raise ValueError(f"File too small ({file_size} bytes)")
        if file_size > MAX_FILE_SIZE:
            raise ValueError(f"File too large ({file_size} bytes)")

        logger.info(f"[yt-dlp] Success: {file_size/1024/1024:.1f} MB in {elapsed:.1f}s")
        return output_path

    except asyncio.TimeoutError:
        logger.warning(f"[yt-dlp] Timed out after {DOWNLOAD_TIMEOUT}s")
        try:
            proc.kill()
        except Exception:
            pass
        for output_file in glob.glob(f"{output_base}.*"):
            _safe_unlink(output_file)
        raise HTTPException(504, "Download timed out — video may be too long")
    except (HTTPException, ValueError):
        for output_file in glob.glob(f"{output_base}.*"):
            _safe_unlink(output_file)
        raise
    except Exception as e:
        for output_file in glob.glob(f"{output_base}.*"):
            _safe_unlink(output_file)
        raise ValueError(f"Unexpected error: {e}")


def _base_ytdlp_command() -> list[str]:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "-f", f"ba[abr<={MAX_AUDIO_BITRATE}]/ba",
        "--match-filter", f"duration <= {MAX_DURATION_SEC}",
        "-S", FORMAT_SORT,
        "--concurrent-fragments", str(CONCURRENT_FRAGMENTS),
        "--cache-dir", YTDLP_CACHE_DIR,
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--socket-timeout", "15",
        "--retries", "2",
        "--fragment-retries", "1",
        "--extractor-retries", str(EXTRACTOR_RETRIES),
        "--force-ipv4",
        "--force-overwrites",
    ]
    if not CHECK_FORMATS:
        command.append("--no-check-formats")
    return command


def _ytdlp_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["YT_DLP_POT_PROVIDER_URL"] = BGUTIL_SERVER_URL
    return environment


def _build_ytdlp_attempts(
    video_url: str,
    base_cmd: list[str],
    output_template: str | None = None,
    resolve_only: bool = False,
) -> list[tuple[list[str], str]]:
    supported_clients = {
        "tv", "web_embedded", "web_creator", "android_vr",
        "tv_downgraded", "mweb", "web_safari",
    }
    primary_client = (
        PRIMARY_PLAYER_CLIENT
        if PRIMARY_PLAYER_CLIENT in supported_clients
        else "web_creator"
    )
    attempts: list[tuple[list[str], str]] = []

    def add_attempt(client_spec: str, label: str, cookies: bool = False):
        command = base_cmd + [
            "--extractor-args",
            "youtube:"
            f"player_client={client_spec};player_skip=webpage"
            + (";skip=hls,dash,translated_subs" if SKIP_MANIFESTS else ""),
        ]
        if cookies and YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
            command.extend(["--cookies", YT_COOKIES_FILE])
        if resolve_only:
            command.append("--get-url")
        else:
            command.extend(["-o", output_template])
        command.append(video_url)
        attempts.append((command, label))

    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        add_attempt(primary_client, f"cookies ({primary_client} client)", cookies=True)
        if primary_client != "web_creator":
            add_attempt("web_creator", "cookies (web_creator client)", cookies=True)
        add_attempt("mweb", "cookies + PO tokens (mweb client)", cookies=True)
    add_attempt(
        "tv,web_embedded,web_creator,android_vr,tv_downgraded,mweb",
        "PO tokens (rotating clients)",
    )
    return attempts


async def _resolve_audio_url(video_id: str) -> str:
    task = _inflight_resolutions.get(video_id)
    if task is None:
        task = asyncio.create_task(_resolve_audio_url_limited(video_id))
        _inflight_resolutions[video_id] = task
    else:
        logger.info(f"[yt-dlp] Joining in-flight URL resolution for {video_id}")
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight_resolutions.get(video_id) is task:
            _inflight_resolutions.pop(video_id, None)


async def _resolve_audio_url_limited(video_id: str) -> str:
    async with _extraction_slots:
        return await _resolve_audio_url_process(video_id)


async def _resolve_audio_url_process(video_id: str) -> str:
    """Resolve a temporary audio URL without transferring media through Render."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    attempts = _build_ytdlp_attempts(video_url, _base_ytdlp_command(), resolve_only=True)
    last_error = "yt-dlp did not return a media URL"
    for attempt_index, (cmd, auth_mode) in enumerate(attempts):
        logger.info(f"[yt-dlp] Resolving URL with {auth_mode}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ytdlp_environment(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            last_error = "URL resolution timed out"
            continue

        if proc.returncode == 0:
            for line in stdout.decode(errors="replace").splitlines():
                candidate = line.strip()
                if candidate.startswith("https://") and ".googlevideo.com/" in candidate:
                    return candidate
            last_error = "yt-dlp returned no safe media URL"
        else:
            last_error = stderr.decode(errors="replace")[-500:]
            if "HTTP Error 429" in last_error or "Too Many Requests" in last_error:
                break
        if attempt_index < len(attempts) - 1:
            logger.warning(f"[yt-dlp] URL resolution failed with {auth_mode}; trying next client")

    raise HTTPException(503, f"YouTube URL resolution failed: {last_error[:200]}")


def _copy_for_response(source_path: str) -> str:
    """Create a response-owned link so each caller can clean up independently."""
    extension = os.path.splitext(source_path)[1]
    response_handle = tempfile.NamedTemporaryFile(
        prefix="yt_response_", suffix=extension, delete=False
    )
    response_path = response_handle.name
    response_handle.close()
    _safe_unlink(response_path)
    try:
        os.link(source_path, response_path)
    except OSError:
        shutil.copyfile(source_path, response_path)
    return response_path


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------
def _safe_unlink(path: str | None):
    if path:
        try:
            os.unlink(path)
            _active_files.discard(path)
        except OSError:
            pass


class _cleanup_task:
    """Starlette BackgroundTask-compatible callable for file cleanup."""
    def __init__(self, path: str):
        self.path = path

    async def __call__(self):
        _safe_unlink(self.path)
