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
from fastapi.responses import FileResponse
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
MAX_CONCURRENT_EXTRACTIONS = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "2")))
CONCURRENT_FRAGMENTS = max(1, int(os.getenv("YT_CONCURRENT_FRAGMENTS", "4")))
FORMAT_SORT = os.getenv("YT_FORMAT_SORT", "+size,+br,proto:https:m3u8_native:m3u8")
PRIMARY_PLAYER_CLIENT = os.getenv("YT_PRIMARY_CLIENT", "web_creator").strip()
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
async def extract_audio(video_id: str):
    """
    Extract audio from a YouTube video and return the file.

    Query params:
        video_id: 11-character YouTube video ID (SSRF-safe: no arbitrary URLs)

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
    """Run one yt-dlp extraction while the caller holds an extraction slot."""
    output_handle = tempfile.NamedTemporaryFile(prefix="yt_audio_", delete=False)
    output_base = output_handle.name
    output_handle.close()
    _safe_unlink(output_base)
    output_template = f"{output_base}.%(ext)s"

    try:
        logger.info(f"[yt-dlp] Extracting audio for {video_id}...")
        t0 = time.perf_counter()

        base_cmd = [
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
            "--retries", "1",
            "--force-overwrites",
        ]
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        attempts = []
        supported_clients = {"web_creator", "web_safari", "mweb"}
        primary_client = (
            PRIMARY_PLAYER_CLIENT
            if PRIMARY_PLAYER_CLIENT in supported_clients
            else "web_creator"
        )
        if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
            attempts.append((
                base_cmd + [
                    "--extractor-args", f"youtube:player_client={primary_client}",
                    "--cookies", YT_COOKIES_FILE,
                    video_url,
                ],
                f"cookies ({primary_client} client)",
            ))
            if primary_client != "web_creator":
                attempts.append((
                    base_cmd + [
                        "--extractor-args", "youtube:player_client=web_creator",
                        "--cookies", YT_COOKIES_FILE,
                        video_url,
                    ],
                    "cookies (web_creator client)",
                ))
            attempts.append((
                base_cmd + [
                    "--extractor-args", "youtube:player_client=mweb",
                    "--cookies", YT_COOKIES_FILE,
                    video_url,
                ],
                "cookies + PO tokens (mweb client)",
            ))
        attempts.append((
            base_cmd + [
                "--extractor-args", "youtube:player_client=mweb",
                video_url,
            ],
            "PO tokens (mweb client)",
        ))

        proc = None
        full_err = ""
        for attempt_index, (cmd, auth_mode) in enumerate(attempts):
            attempt_started = time.perf_counter()
            for previous_output in glob.glob(f"{output_base}.*"):
                _safe_unlink(previous_output)
            cmd.extend(["-o", output_template])
            logger.info(f"[yt-dlp] Trying {auth_mode} extraction")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
