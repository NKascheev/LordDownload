"""Local HTTP server for downloading media from YouTube and Instagram.

The server listens on loopback only. Mutating requests require a valid Origin,
CSRF token, and JSON content type. Only known static assets, validated media
images, and JSON job state are exposed. Completed files are streamed to the
browser and kept temporarily until the client or TTL cleanup removes them.
"""

from __future__ import annotations

import argparse
import functools
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

try:
    import downloader
    from downloader import DownloadError
except ModuleNotFoundError as exc:
    if exc.name == "yt_dlp":
        print(
            "Не установлены зависимости приложения. Запустите start.bat "
            "или выполните: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    raise

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_FILE_MB = 4096
DEFAULT_JOB_TTL_MIN = 30
DEFAULT_MAX_ACTIVE_JOBS = 2
MIN_FILE_MB = 16
MAX_FILE_MB = 16384
MIN_JOB_TTL_MIN = 1
MAX_JOB_TTL_MIN = 1440
MIN_ACTIVE_JOBS = 1
MAX_ACTIVE_JOBS = 4
MIN_PORT = 1024
MAX_PORT = 65535
MAX_BODY_BYTES = 32 * 1024
MAX_JOBS = 200
KEEP_JOBS = 180
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
JOB_PATH_RE = re.compile(r"^/api/jobs/([0-9a-f]{32})(/file)?$")
MIB = 1024 * 1024
CONTENT_TYPES = {
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "subtitles": "text/plain; charset=utf-8",
}
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
FINISHED_JOB_STATUSES = frozenset({"ready", "error"})
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "worker-src 'none'; "
        "manifest-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), hid=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}

logger = logging.getLogger("youtube-downloader")


def _as_int(value: object) -> int | None:
    """Return a non-negative integer if the value is a real integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return None


def _as_float(value: object) -> float | None:
    """Return a non-negative finite float if the value is a real number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return max(0.0, number)
    return None


class ClientError(Exception):
    """API error that can be returned safely to the browser."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        """Store the HTTP status, error code, message, and retry delay."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class JobBusyError(Exception):
    """Raised when the concurrent download limit is reached."""


class TTLCache:
    """Thread-safe TTL cache for metadata and fetched images."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        """Create an empty cache with the given lifetime and capacity."""
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        """Return a cached value if it has not expired."""
        now = time.monotonic()
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= now:
                self._values.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        """Store a value and evict stale or oldest entries when full."""
        now = time.monotonic()
        with self._lock:
            if len(self._values) >= self._max:
                for item_key, (expires, _) in list(self._values.items()):
                    if expires <= now:
                        self._values.pop(item_key, None)
                if len(self._values) >= self._max:
                    oldest = min(self._values, key=lambda item: self._values[item][0])
                    self._values.pop(oldest, None)
            self._values[key] = (now + self._ttl, value)


@dataclass
class Job:
    """State of one download job."""

    job_id: str
    url: str
    source: str
    mode: str
    height: int | None
    subtitle_lang: str | None
    auto_subtitle: bool
    output_dir: Path
    status: str = "queued"
    phase: str = "В очереди"
    progress: float = 0.0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None
    path: Path | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: float = field(default_factory=time.time)


class RateLimiter:
    """Limit request frequency per client address and action."""

    def __init__(self) -> None:
        """Create an empty per-client rate limiter."""
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def retry_after(self, key: str, *, limit: int, window: int) -> int:
        """Return seconds to wait before the next allowed request."""
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, math.ceil(events[0] + window - now))
            events.append(now)
            return 0


class JobManager:
    """Own download jobs, their files, and their lifecycle."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        max_file_bytes: int,
        job_ttl_seconds: int,
        max_active_jobs: int,
    ) -> None:
        """Create the job manager and start its cleanup thread."""
        self.jobs_root = runtime_dir / "jobs"
        self.max_file_bytes = max_file_bytes
        self.job_ttl_seconds = job_ttl_seconds
        self.max_active_jobs = max_active_jobs
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._clean_stale_directories()
        threading.Thread(
            target=self._cleanup_loop,
            name="youtube-downloader-cleanup",
            daemon=True,
        ).start()

    def _clean_stale_directories(self) -> None:
        """Remove job directories left by a previous run."""
        for child in self.jobs_root.iterdir():
            if child.is_dir() and JOB_ID_RE.fullmatch(child.name):
                shutil.rmtree(child, ignore_errors=True)

    def _cleanup_loop(self) -> None:
        """Periodically remove expired jobs."""
        while not self._stop_event.wait(60.0):
            self.cleanup()

    def cleanup(self) -> None:
        """Delete expired and excess completed jobs."""
        now = time.time()
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status not in FINISHED_JOB_STATUSES:
                    continue
                if now - job.updated_at <= self.job_ttl_seconds:
                    continue
                self._remove_job_locked(job_id)
            if len(self._jobs) > MAX_JOBS:
                finished = sorted(
                    (job for job in self._jobs.values() if job.status in FINISHED_JOB_STATUSES),
                    key=lambda item: item.updated_at,
                )
                for job in finished[: len(self._jobs) - KEEP_JOBS]:
                    self._remove_job_locked(job.job_id)

    def _remove_job_locked(self, job_id: str) -> None:
        """Remove a job and its directory while holding the lock."""
        job = self._jobs.pop(job_id, None)
        if job is not None:
            self._remove_directory(job.output_dir)

    def _remove_directory(self, path: Path) -> None:
        """Delete a directory only if it is inside the jobs root."""
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved.parent == self.jobs_root.resolve():
            shutil.rmtree(resolved, ignore_errors=True)

    def create(
        self,
        *,
        url: str,
        source: str,
        mode: str,
        height: int | None,
        subtitle_lang: str | None,
        auto_subtitle: bool,
    ) -> str:
        """Create a download job and start its worker thread."""
        with self._lock:
            active = sum(job.status in ACTIVE_JOB_STATUSES for job in self._jobs.values())
            if active >= self.max_active_jobs:
                raise JobBusyError
            job_id = uuid.uuid4().hex
            output_dir = self.jobs_root / job_id
            output_dir.mkdir(parents=True, exist_ok=False)
            if os.name != "nt":
                output_dir.chmod(0o700)
            job = Job(
                job_id=job_id,
                url=url,
                source=source,
                mode=mode,
                height=height,
                subtitle_lang=subtitle_lang,
                auto_subtitle=auto_subtitle,
                output_dir=output_dir,
            )
            self._jobs[job_id] = job

        threading.Thread(
            target=self._run_job,
            args=(job_id,),
            name=f"download-{job_id[:8]}",
            daemon=True,
        ).start()
        return job_id

    def _run_job(self, job_id: str) -> None:
        """Execute a download job and update its state."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "running"
            job.phase = "Получение информации"
            job.updated_at = time.time()

        try:
            result = downloader.download(
                job.url,
                job.mode,
                source=job.source,
                height=job.height,
                subtitle_lang=job.subtitle_lang,
                auto_subtitle=job.auto_subtitle,
                output_dir=job.output_dir,
                max_file_bytes=self.max_file_bytes,
                progress=lambda payload: self._on_progress(job_id, payload),
            )
            resolved = result.path.resolve()
            if resolved.parent != job.output_dir.resolve() or not resolved.is_file():
                raise DownloadError(
                    "file_error",
                    "Скачанный файл не найден.",
                    http_status=500,
                )

            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return
                current.status = "ready"
                current.phase = "Готово"
                current.progress = 100.0
                current.path = resolved
                current.filename = result.filename
                current.content_type = CONTENT_TYPES[job.mode]
                current.size = result.size
                current.speed = None
                current.eta = None
                current.updated_at = time.time()
        except DownloadError as exc:
            logger.info("Job %s failed with %s", job_id, exc.code)
            self._fail_job(job_id, exc.code, exc.message)
        except Exception:
            logger.exception("Unexpected error in job %s", job_id)
            self._fail_job(
                job_id,
                "internal_error",
                "Внутренняя ошибка при скачивании. Подробности записаны в журнал.",
            )

    def _on_progress(self, job_id: str, payload: dict[str, Any]) -> None:
        """Merge a downloader progress update into the job state."""
        if not isinstance(payload, dict):
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return
            phase = payload.get("phase")
            if isinstance(phase, str) and phase:
                job.phase = phase[:80]
            percent = _as_float(payload.get("percent"))
            if percent is not None:
                job.progress = max(job.progress, min(99.0, percent))
            downloaded = _as_int(payload.get("downloaded_bytes"))
            if downloaded is not None:
                job.downloaded_bytes = downloaded
            total = _as_int(payload.get("total_bytes"))
            if total is not None:
                job.total_bytes = total
            speed = _as_float(payload.get("speed"))
            if speed is not None:
                job.speed = speed
            eta = _as_int(payload.get("eta"))
            if eta is not None:
                job.eta = eta
            job.updated_at = time.time()

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
        """Mark a job as failed and remove its temporary files."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "error"
            job.phase = "Ошибка"
            job.error_code = code
            job.error_message = message
            job.updated_at = time.time()
            output_dir = job.output_dir
        self._remove_directory(output_dir)

    def get(self, job_id: str) -> Job | None:
        """Return a job by its validated identifier."""
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def get_ready_file(self, job_id: str) -> Job | None:
        """Return a completed job whose file still exists."""
        job = self.get(job_id)
        if job is None or job.status != "ready" or job.path is None:
            return None
        try:
            path = job.path.resolve()
            if path.parent != job.output_dir.resolve() or not path.is_file():
                return None
        except OSError:
            return None
        return job

    def discard(self, job_id: str) -> bool:
        """Delete a completed job and its temporary file."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in FINISHED_JOB_STATUSES:
                return False
            self._remove_job_locked(job_id)
            return True

    def public(self, job: Job) -> dict[str, Any]:
        """Build the JSON-safe job representation for the client."""
        with self._lock:
            data: dict[str, Any] = {
                "status": job.status,
                "phase": job.phase,
                "progress": round(job.progress, 1),
                "downloaded_bytes": job.downloaded_bytes,
                "total_bytes": job.total_bytes,
                "speed": job.speed,
                "eta": job.eta,
            }
            if (
                job.status == "ready"
                and job.filename
                and job.content_type
                and job.size is not None
            ):
                data["file"] = {
                    "name": job.filename,
                    "size": job.size,
                    "url": f"/api/jobs/{job.job_id}/file",
                }
            if job.status == "error":
                data["error"] = {
                    "code": job.error_code or "download_failed",
                    "message": job.error_message or "Не удалось скачать файл.",
                }
            return data

    def shutdown(self) -> None:
        """Stop periodic cleanup and release job state."""
        self._stop_event.set()
        with self._lock:
            self._jobs.clear()


def _load_static() -> dict[str, tuple[bytes, str]]:
    """Load static assets into memory for safe serving."""
    static_root = STATIC_DIR.resolve()
    assets: dict[str, tuple[bytes, str]] = {}
    for route, (filename, content_type) in STATIC_ASSETS.items():
        path = (static_root / filename).resolve()
        if path.parent != static_root or not path.is_file():
            raise RuntimeError(f"Не найден статический файл: {filename}")
        assets[route] = (path.read_bytes(), content_type)
    return assets


def _content_disposition(filename: str) -> str:
    """Build a safe attachment Content-Disposition header."""
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._ -]+", "_", fallback).strip(" .")
    if not fallback or fallback.startswith("."):
        suffix = Path(filename).suffix
        suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) else ""
        fallback = f"download{suffix}"
    fallback = fallback[:150]
    encoded = quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


class DownloaderServer(ThreadingHTTPServer):
    """Loopback HTTP server that owns caches and download jobs."""

    daemon_threads = True
    # On Windows SO_REUSEADDR can let two processes share a port, which would
    # make a second local instance silently steal some requests.
    allow_reuse_address = False
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        runtime_dir: Path,
        max_file_mb: int,
        job_ttl_min: int,
        max_active_jobs: int,
    ) -> None:
        """Create caches, rate limits, and the job manager."""
        super().__init__(server_address, DownloaderHandler)
        self.port = int(self.server_address[1])
        hosts = (f"{LOOPBACK}:{self.port}", f"localhost:{self.port}")
        self.allowed_hosts = frozenset(hosts)
        self.allowed_origins = frozenset(f"http://{host}" for host in hosts)
        self.static = _load_static()
        self.csrf_token = secrets.token_urlsafe(32)
        self.info_cache = TTLCache(ttl_seconds=180, max_entries=32)
        self.thumbnail_cache = TTLCache(ttl_seconds=3600, max_entries=64)
        self.avatar_cache = TTLCache(ttl_seconds=6 * 60 * 60, max_entries=32)
        self.rate_limiter = RateLimiter()
        self.jobs = JobManager(
            runtime_dir,
            max_file_bytes=max_file_mb * MIB,
            job_ttl_seconds=job_ttl_min * 60,
            max_active_jobs=max_active_jobs,
        )

    def server_close(self) -> None:
        """Stop the job manager and close the listening socket."""
        self.jobs.shutdown()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Log unexpected connection errors quietly."""
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            logger.debug(
                "Client interrupted the connection: %s",
                exc_type.__name__ if exc_type else "?",
            )
            return
        logger.error("Connection handling failed: %s", exc, exc_info=True)


class DownloaderHandler(BaseHTTPRequestHandler):
    """Route HTTP requests to the local interface and JSON API."""

    server: DownloaderServer
    server_version = "YouTubeDownloader"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        """Configure a timeout for each connection."""
        super().setup()
        try:
            self.connection.settimeout(30.0)
        except OSError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        """Keep the console free of per-request access logs."""

    def do_GET(self) -> None:
        """Handle GET requests."""
        self._handle_safely(self._handle_get)

    def do_HEAD(self) -> None:
        """Handle HEAD requests."""
        self._handle_safely(self._handle_get)

    def do_POST(self) -> None:
        """Handle POST requests and close the connection afterward."""
        # A rejected POST may leave unread bytes, which must not be parsed as
        # the next request on a reused connection.
        self.close_connection = True
        self._handle_safely(self._handle_post)

    def _reject_method(self) -> None:
        """Reject an unsupported HTTP method with a JSON 405 response."""
        self._handle_safely(self._method_not_allowed)

    # Every unsupported method shares the same rejecting handler.
    do_OPTIONS = do_PUT = do_PATCH = do_DELETE = _reject_method

    def _handle_safely(self, handler: Callable[[], None]) -> None:
        """Validate the host and convert expected errors to JSON."""
        self._request_body_read = False
        try:
            if not self._host_is_valid():
                self.close_connection = True
                self._drain_request_body()
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": {"code": "invalid_host", "message": "Неверный адрес сервера."}},
                )
                return
            handler()
        except ClientError as exc:
            self._drain_request_body()
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                retry_after=exc.retry_after,
            )
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client interrupted the connection")
        except Exception:
            self._drain_request_body()
            logger.exception("Unhandled HTTP request error")
            try:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера."}},
                )
            except Exception:
                logger.debug("Could not send the error response", exc_info=True)

    def _host_is_valid(self) -> bool:
        """Check the Host header against allowed loopback names."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts

    def _method_not_allowed(self) -> None:
        """Return a JSON 405 response."""
        self.close_connection = True
        self._drain_request_body()
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": {"code": "method_not_allowed", "message": "Метод не поддерживается."}},
            extra_headers={"Allow": "GET, HEAD, POST"},
        )

    def _drain_request_body(self) -> None:
        """Consume an unread request body before closing the connection."""
        if self._request_body_read:
            return
        self._request_body_read = True
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return
        try:
            length = int(raw_length)
        except ValueError:
            return
        if 0 < length <= MAX_BODY_BYTES:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _handle_get(self) -> None:
        """Route GET and HEAD requests."""
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "app": "YouTube Downloader"})
            return
        if path == "/api/bootstrap":
            self._send_json(HTTPStatus.OK, {"csrf_token": self.server.csrf_token})
            return

        image_routes = (
            (
                "/api/thumbnail/",
                downloader.VIDEO_ID_RE,
                self.server.thumbnail_cache,
                downloader.fetch_thumbnail,
            ),
            (
                "/api/avatar/",
                downloader.CHANNEL_ID_RE,
                self.server.avatar_cache,
                downloader.fetch_channel_avatar,
            ),
        )
        for prefix, pattern, cache, fetcher in image_routes:
            if path.startswith(prefix):
                key = path[len(prefix) :]
                if pattern.fullmatch(key):
                    self._serve_cached_image(cache, key, fetcher)
                    return

        if path.startswith("/api/jobs/"):
            self._handle_job_get(path)
            return

        asset = self.server.static.get(path)
        if asset is not None:
            body, content_type = asset
            # Revalidate local assets so browsers never keep stale CSS or JS.
            self._send_bytes(HTTPStatus.OK, body, content_type, cache_control="no-cache")
            return
        self._not_found(api=path.startswith("/api/"))

    def _handle_job_get(self, path: str) -> None:
        """Return job state or stream a completed file."""
        match = JOB_PATH_RE.fullmatch(path)
        if match is None:
            self._not_found(api=True)
            return
        job_id = match.group(1)
        if match.group(2):
            job = self.server.jobs.get_ready_file(job_id)
            if job is None:
                self._not_found(api=True)
                return
            self._serve_file(job)
            return
        job = self.server.jobs.get(job_id)
        if job is None:
            self._not_found(api=True)
            return
        self._send_json(HTTPStatus.OK, self.server.jobs.public(job))

    def _serve_file(self, job: Job) -> None:
        """Stream a completed file without deleting it."""
        if job.path is None or job.filename is None or job.content_type is None:
            self._not_found(api=True)
            return
        try:
            path = job.path.resolve()
            if path.parent != job.output_dir.resolve():
                self._not_found(api=True)
                return
            size = path.stat().st_size
            handle = path.open("rb")
        except OSError:
            self._not_found(api=True)
            return

        with handle:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", job.content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", _content_disposition(job.filename))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("Accept-Ranges", "none")
            self._send_security_headers()
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                shutil.copyfileobj(handle, self.wfile, 64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                logger.debug("Client interrupted the file download")
            except OSError:
                logger.warning("Failed to read the completed file")

    def _serve_cached_image(
        self,
        cache: TTLCache,
        key: str,
        fetcher: Callable[[str], tuple[bytes, str] | None],
    ) -> None:
        """Serve an image from the cache, fetching it on a miss."""
        result = cache.get(key)
        if result is None:
            result = fetcher(key)
            if result is None:
                self._not_found(api=True)
                return
            cache.set(key, result)
        body, content_type = result
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type,
            cache_control="public, max-age=3600",
        )

    def _handle_post(self) -> None:
        """Route and validate mutating API requests."""
        path = urlsplit(self.path).path
        handlers = {
            "/api/info": self._post_info,
            "/api/download": self._post_download,
            "/api/jobs/delete": self._post_delete,
        }
        handler = handlers.get(path)
        if handler is None:
            self._drain_request_body()
            self._not_found(api=True)
            return
        self._require_same_origin()
        self._require_csrf()
        handler(self._read_json_body())

    def _post_delete(self, data: dict[str, Any]) -> None:
        """Delete a completed job and its temporary file."""
        self._reject_unknown_fields(data, {"job_id"})
        self._check_rate_limit(self.client_address[0], "job-delete", limit=120, window=60)
        job_id = data.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_job", "Некорректное задание.")
        self._send_json(HTTPStatus.OK, {"deleted": self.server.jobs.discard(job_id)})

    def _post_info(self, data: dict[str, Any]) -> None:
        """Return media metadata for a validated URL."""
        url, source = self._prepare_media_post(
            data,
            action="info",
            limit=30,
            window=60,
            allowed={"url", "source"},
        )
        cache_key = f"{source}:{url}"
        info = self.server.info_cache.get(cache_key)
        if info is None:
            try:
                info = downloader.get_video_info(url, source)
            except DownloadError as exc:
                raise self._as_client_error(exc) from exc
            self.server.info_cache.set(cache_key, info)
        self._send_json(HTTPStatus.OK, info)

    def _post_download(self, data: dict[str, Any]) -> None:
        """Create a download job for a validated request."""
        url, source = self._prepare_media_post(
            data,
            action="download",
            limit=12,
            window=60,
            allowed={"url", "source", "mode", "height", "subtitle_lang", "auto_subtitle"},
        )
        try:
            request = downloader.validate_request(
                source,
                data.get("mode"),
                data.get("height"),
                data.get("subtitle_lang"),
                data.get("auto_subtitle", False),
            )
        except DownloadError as exc:
            raise self._as_client_error(exc) from exc

        try:
            job_id = self.server.jobs.create(
                url=url,
                source=source,
                mode=request.mode,
                height=request.height,
                subtitle_lang=request.subtitle_lang,
                auto_subtitle=request.auto_subtitle,
            )
        except JobBusyError as exc:
            raise ClientError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "busy",
                "Уже выполняются другие загрузки. Дождитесь их завершения.",
                retry_after=20,
            ) from exc
        self._send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def _prepare_media_post(
        self,
        data: dict[str, Any],
        *,
        action: str,
        limit: int,
        window: int,
        allowed: set[str],
    ) -> tuple[str, str]:
        """Validate common fields and rate limits for a media POST route."""
        self._reject_unknown_fields(data, allowed)
        self._check_rate_limit(self.client_address[0], action, limit=limit, window=window)
        return self._parse_media_request(data)

    def _parse_media_request(self, data: dict[str, Any]) -> tuple[str, str]:
        """Validate the URL and source shared by info and download requests."""
        raw_url = data.get("url")
        source = data.get("source", "youtube")
        if not isinstance(raw_url, str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_url", "Вставьте ссылку на видео.")
        try:
            source = downloader.validate_source(source)
            return downloader.normalize_media_url(raw_url, source), source
        except DownloadError as exc:
            raise self._as_client_error(exc) from exc

    @staticmethod
    def _as_client_error(exc: DownloadError) -> ClientError:
        """Convert a downloader error into an API error."""
        return ClientError(exc.http_status, exc.code, exc.message)

    def _require_same_origin(self) -> None:
        """Require a same-origin browser request."""
        origin = (self.headers.get("Origin") or "").strip()
        if origin not in self.server.allowed_origins:
            raise ClientError(
                HTTPStatus.FORBIDDEN,
                "invalid_origin",
                "Запрос отклонён: неверный источник.",
            )
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise ClientError(
                HTTPStatus.FORBIDDEN,
                "invalid_origin",
                "Запрос отклонён: межсайтовый запрос.",
            )

    def _require_csrf(self) -> None:
        """Require the current CSRF token."""
        token = self.headers.get("X-CSRF-Token") or ""
        if not token or not hmac.compare_digest(token, self.server.csrf_token):
            raise ClientError(
                HTTPStatus.FORBIDDEN,
                "csrf",
                "Сессия устарела. Обновите страницу.",
            )

    def _read_json_body(self) -> dict[str, Any]:
        """Read and validate a bounded JSON request body."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ClientError(
                HTTPStatus.LENGTH_REQUIRED,
                "invalid_body",
                "Передача тела запроса частями не поддерживается.",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ClientError(
                HTTPStatus.LENGTH_REQUIRED,
                "invalid_body",
                "Не указан размер запроса.",
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ClientError(
                HTTPStatus.BAD_REQUEST,
                "invalid_body",
                "Некорректный размер запроса.",
            ) from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ClientError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                if length > MAX_BODY_BYTES
                else HTTPStatus.BAD_REQUEST,
                "invalid_body",
                "Некорректный размер запроса.",
            )
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ClientError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "invalid_content_type",
                "Ожидается JSON.",
            )
        raw = self.rfile.read(length)
        self._request_body_read = True
        if len(raw) != length:
            raise ClientError(
                HTTPStatus.BAD_REQUEST,
                "invalid_body",
                "Запрос прочитан не полностью.",
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_json", "Некорректный JSON.") from exc
        if not isinstance(data, dict):
            raise ClientError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "JSON должен быть объектом.",
            )
        return data

    @staticmethod
    def _reject_unknown_fields(data: dict[str, Any], allowed: set[str]) -> None:
        """Reject JSON fields not accepted by the route."""
        unknown = set(data) - allowed
        if unknown:
            raise ClientError(
                HTTPStatus.BAD_REQUEST,
                "unknown_field",
                "Запрос содержит лишние поля.",
            )

    def _check_rate_limit(
        self,
        client_ip: str,
        action: str,
        *,
        limit: int,
        window: int,
    ) -> None:
        """Apply a per-client rate limit for an action."""
        wait = self.server.rate_limiter.retry_after(
            f"{client_ip}:{action}",
            limit=limit,
            window=window,
        )
        if wait:
            raise ClientError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limited",
                "Слишком много запросов. Сделайте паузу.",
                retry_after=wait,
            )

    def _not_found(self, *, api: bool) -> None:
        """Return a JSON or plain-text 404 response."""
        if api:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": {"code": "not_found", "message": "Не найдено."}},
            )
        else:
            self._send_bytes(
                HTTPStatus.NOT_FOUND,
                b"Not found",
                "text/plain; charset=utf-8",
            )

    def _send_security_headers(self) -> None:
        """Send the hardening headers shared by every response."""
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        retry_after: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Serialize and send a JSON response."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            retry_after=retry_after,
            extra_headers=extra_headers,
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        retry_after: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Send bytes with security and caching headers."""
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if self.close_connection:
            self.send_header("Connection", "close")
        if retry_after is not None:
            self.send_header("Retry-After", str(max(1, int(retry_after))))
        self._send_security_headers()
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)


def _bounded_int(value: str, *, minimum: int, maximum: int) -> int:
    """Parse a base-10 integer within the inclusive range."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ожидается целое число") from exc
    if not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(f"допустимый диапазон {minimum}–{maximum}")
    return number


def _port(value: str) -> int:
    """Parse a local TCP port."""
    return _bounded_int(value, minimum=MIN_PORT, maximum=MAX_PORT)


def _require_range(value: int, minimum: int, maximum: int, message: str) -> None:
    """Raise ValueError when a value is outside the inclusive range."""
    if not minimum <= value <= maximum:
        raise ValueError(message)


def create_server(
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    max_file_mb: int = DEFAULT_MAX_FILE_MB,
    job_ttl_min: int = DEFAULT_JOB_TTL_MIN,
    max_active_jobs: int = DEFAULT_MAX_ACTIVE_JOBS,
) -> DownloaderServer:
    """Create the loopback HTTP server with validated limits."""
    _require_range(port, 0, MAX_PORT, "Некорректный порт")
    _require_range(
        max_file_mb,
        MIN_FILE_MB,
        MAX_FILE_MB,
        f"Лимит файла должен быть в диапазоне {MIN_FILE_MB}–{MAX_FILE_MB} МБ",
    )
    _require_range(
        job_ttl_min,
        MIN_JOB_TTL_MIN,
        MAX_JOB_TTL_MIN,
        f"Время хранения должно быть в диапазоне {MIN_JOB_TTL_MIN}–{MAX_JOB_TTL_MIN} минут",
    )
    _require_range(
        max_active_jobs,
        MIN_ACTIVE_JOBS,
        MAX_ACTIVE_JOBS,
        f"Число одновременных загрузок должно быть {MIN_ACTIVE_JOBS}–{MAX_ACTIVE_JOBS}",
    )
    runtime = (runtime_dir or (BASE_DIR / ".runtime")).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    return DownloaderServer(
        (LOOPBACK, port),
        runtime_dir=runtime,
        max_file_mb=max_file_mb,
        job_ttl_min=job_ttl_min,
        max_active_jobs=max_active_jobs,
    )


def _format_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer from an environment variable."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Environment variable %s must be an integer; using %s", name, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "Environment variable %s is outside %s–%s; using %s",
            name,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def main(argv: list[str] | None = None) -> int:
    """Run the local media downloader server."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Локальный загрузчик YouTube и Instagram")
    parser.add_argument(
        "--port",
        type=_port,
        default=_format_env_int("VD_PORT", DEFAULT_PORT, MIN_PORT, MAX_PORT),
        help="локальный порт (по умолчанию 8765)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="открыть интерфейс в браузере после запуска",
    )
    parser.add_argument(
        "--max-file-mb",
        type=functools.partial(
            _bounded_int,
            minimum=MIN_FILE_MB,
            maximum=MAX_FILE_MB,
        ),
        default=_format_env_int("VD_MAX_FILE_MB", DEFAULT_MAX_FILE_MB, MIN_FILE_MB, MAX_FILE_MB),
        help="максимальный размер одного файла в МБ",
    )
    parser.add_argument(
        "--job-ttl-min",
        type=functools.partial(
            _bounded_int,
            minimum=MIN_JOB_TTL_MIN,
            maximum=MAX_JOB_TTL_MIN,
        ),
        default=_format_env_int(
            "VD_JOB_TTL_MIN",
            DEFAULT_JOB_TTL_MIN,
            MIN_JOB_TTL_MIN,
            MAX_JOB_TTL_MIN,
        ),
        help="резервный срок хранения временных файлов, минут",
    )
    parser.add_argument(
        "--max-active",
        type=functools.partial(
            _bounded_int,
            minimum=MIN_ACTIVE_JOBS,
            maximum=MAX_ACTIVE_JOBS,
        ),
        default=_format_env_int(
            "VD_MAX_ACTIVE",
            DEFAULT_MAX_ACTIVE_JOBS,
            MIN_ACTIVE_JOBS,
            MAX_ACTIVE_JOBS,
        ),
        help="сколько загрузок выполнять одновременно",
    )
    args = parser.parse_args(argv)

    try:
        server = create_server(
            args.port,
            max_file_mb=args.max_file_mb,
            job_ttl_min=args.job_ttl_min,
            max_active_jobs=args.max_active,
        )
    except OSError as exc:
        logger.error("Could not bind port %s: %s", args.port, exc)
        print(
            f"Не удалось запустить загрузчик на порту {args.port}. "
            "Возможно, он уже занят. Попробуйте --port 8766.",
            file=sys.stderr,
        )
        return 1

    url = f"http://{LOOPBACK}:{server.port}/"
    print(flush=True)
    print("  Загрузчик запущен", flush=True)
    print(f"  {url}", flush=True)
    print("  Остановить: Ctrl+C или закрыть это окно.", flush=True)
    print(flush=True)

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url, new=2)).start()

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nОстановка загрузчика…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
