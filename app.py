"""Локальный веб-сервер для скачивания видео с YouTube.

Сервер слушает только 127.0.0.1. Все изменяющие запросы защищены проверкой
Origin, CSRF-токеном и обязательным JSON Content-Type. Наружу отдаются только
явно перечисленная статика, превью и логотип канала по проверенным ID, а также
JSON-состояния заданий. Готовый файл отдаётся браузеру для сохранения в папку
загрузок и временно хранится на сервере до истечения срока задания.
"""

from __future__ import annotations

import argparse
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
DEFAULT_PORT = 8765
DEFAULT_MAX_FILE_MB = 4096
DEFAULT_JOB_TTL_MIN = 30
DEFAULT_MAX_ACTIVE_JOBS = 2
MAX_BODY_BYTES = 32 * 1024
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SUBTITLE_LANG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,34}$")
MIB = 1024 * 1024
CONTENT_TYPES = {
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "subtitles": "text/plain; charset=utf-8",
}

logger = logging.getLogger("youtube-downloader")


class ClientError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class JobBusyError(Exception):
    pass


class ImageCache:
    """Небольшой кэш изображений, чтобы браузер не обращался к YouTube напрямую."""

    def __init__(
        self,
        fetcher: Callable[[str], tuple[bytes, str] | None],
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 64,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, bytes, str]] = {}

    def get_or_fetch(self, key: str) -> tuple[bytes, str] | None:
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if cached and cached[0] > now:
                return cached[1], cached[2]
            if cached:
                self._values.pop(key, None)

        result = self._fetcher(key)
        if result is None:
            return None
        expires = time.monotonic() + self._ttl
        with self._lock:
            if len(self._values) >= self._max:
                expired = [
                    item_key
                    for item_key, (item_expires, _, _) in self._values.items()
                    if item_expires <= now
                ]
                for item_key in expired:
                    self._values.pop(item_key, None)
                if len(self._values) >= self._max:
                    oldest = min(self._values, key=lambda item_key: self._values[item_key][0])
                    self._values.pop(oldest, None)
            self._values[key] = (expires, result[0], result[1])
        return result


@dataclass(frozen=True)
class AppConfig:
    port: int

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            {
                f"127.0.0.1:{self.port}",
                f"localhost:{self.port}",
            }
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(
            {
                f"http://127.0.0.1:{self.port}",
                f"http://localhost:{self.port}",
            }
        )


@dataclass
class Job:
    job_id: str
    url: str
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


class InfoCache:
    """Небольшой потокобезопасный TTL-кэш метаданных."""

    def __init__(self, ttl_seconds: int = 180, max_entries: int = 32) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._values: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
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

    def set(self, key: str, value: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._values) >= self._max:
                for stale_key, (expires, _) in list(self._values.items()):
                    if expires <= now:
                        self._values.pop(stale_key, None)
                if len(self._values) >= self._max:
                    oldest = min(self._values, key=lambda item: self._values[item][0])
                    self._values.pop(oldest, None)
            self._values[key] = (now + self._ttl, value)


class RateLimiter:
    """Простое ограничение частоты запросов по IP и действию."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}

    def retry_after(self, key: str, *, limit: int, window: int) -> int:
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
    def __init__(
        self,
        runtime_dir: Path,
        *,
        max_file_bytes: int,
        job_ttl_seconds: int,
        max_active_jobs: int,
    ) -> None:
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
        for child in self.jobs_root.iterdir():
            if not child.is_dir() or not JOB_ID_RE.fullmatch(child.name):
                continue
            shutil.rmtree(child, ignore_errors=True)

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(60.0):
            self.cleanup()

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status not in {"ready", "error"}:
                    continue
                if now - job.updated_at <= self.job_ttl_seconds:
                    continue
                self._remove_job_locked(job_id)
            if len(self._jobs) > 200:
                finished = sorted(
                    (job for job in self._jobs.values() if job.status in {"ready", "error"}),
                    key=lambda item: item.updated_at,
                )
                for job in finished[: len(self._jobs) - 180]:
                    self._remove_job_locked(job.job_id)

    def _remove_job_locked(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job is None:
            return
        self._remove_directory(job.output_dir)

    def _remove_directory(self, path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved.parent != self.jobs_root.resolve():
            return
        shutil.rmtree(resolved, ignore_errors=True)

    def create(
        self,
        *,
        url: str,
        mode: str,
        height: int | None,
        subtitle_lang: str | None,
        auto_subtitle: bool,
    ) -> str:
        with self._lock:
            active = sum(job.status in {"queued", "running"} for job in self._jobs.values())
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
                mode=mode,
                height=height,
                subtitle_lang=subtitle_lang,
                auto_subtitle=auto_subtitle,
                output_dir=output_dir,
            )
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id,),
            name=f"download-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return job_id

    def _run_job(self, job_id: str) -> None:
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
                height=job.height,
                subtitle_lang=job.subtitle_lang,
                auto_subtitle=job.auto_subtitle,
                output_dir=job.output_dir,
                max_file_bytes=self.max_file_bytes,
                progress=lambda payload: self._on_progress(job_id, payload),
            )
            resolved = result.path.resolve()
            if resolved.parent != job.output_dir.resolve() or not resolved.is_file():
                raise DownloadError("file_error", "Скачанный файл не найден.", http_status=500)

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
            logger.info("Задание %s завершилось ошибкой %s", job_id, exc.code)
            self._fail_job(job_id, exc.code, exc.message)
        except Exception:
            logger.exception("Непредвиденная ошибка задания %s", job_id)
            self._fail_job(
                job_id,
                "internal_error",
                "Внутренняя ошибка при скачивании. Подробности записаны в журнал.",
            )

    def _on_progress(self, job_id: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != "running":
                return
            phase = payload.get("phase")
            if isinstance(phase, str) and phase:
                job.phase = phase[:80]
            raw_percent = payload.get("percent")
            if isinstance(raw_percent, (int, float)) and not isinstance(raw_percent, bool):
                percent = float(raw_percent)
                if math.isfinite(percent):
                    job.progress = max(job.progress, min(99.0, max(0.0, percent)))
            downloaded = payload.get("downloaded_bytes")
            if isinstance(downloaded, int) and not isinstance(downloaded, bool):
                job.downloaded_bytes = max(0, downloaded)
            total = payload.get("total_bytes")
            if isinstance(total, int) and not isinstance(total, bool):
                job.total_bytes = max(0, total)
            speed = payload.get("speed")
            if isinstance(speed, (int, float)) and not isinstance(speed, bool) and math.isfinite(float(speed)):
                job.speed = max(0.0, float(speed))
            eta = payload.get("eta")
            if isinstance(eta, int) and not isinstance(eta, bool):
                job.eta = max(0, eta)
            job.updated_at = time.time()

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
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
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def get_ready_file(self, job_id: str) -> Job | None:
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
        """Удалить завершённое задание и его временный файл."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in {"ready", "error"}:
                return False
            self._remove_job_locked(job_id)
            return True

    def public(self, job: Job) -> dict[str, Any]:
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
        self._stop_event.set()
        with self._lock:
            self._jobs.clear()


def _load_static() -> dict[str, tuple[bytes, str]]:
    mapping = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
    }
    assets: dict[str, tuple[bytes, str]] = {}
    for route, (filename, content_type) in mapping.items():
        path = (STATIC_DIR / filename).resolve()
        if path.parent != STATIC_DIR.resolve() or not path.is_file():
            raise RuntimeError(f"Не найден статический файл: {filename}")
        assets[route] = (path.read_bytes(), content_type)
    return assets


def _content_disposition(filename: str) -> str:
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._ -]+", "_", fallback).strip(" .")
    if not fallback or fallback.startswith("."):
        suffix = Path(filename).suffix
        suffix = suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) else ""
        fallback = f"download{suffix}"
    fallback = fallback[:150]
    encoded = quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _security_headers() -> dict[str, str]:
    return {
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
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), hid=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Cross-Origin-Embedder-Policy": "require-corp",
    }


class DownloaderServer(ThreadingHTTPServer):
    daemon_threads = True
    # На Windows SO_REUSEADDR позволяет двум процессам слушать один порт.
    # Для локального приложения это недопустимо: второй экземпляр должен
    # завершиться с понятной ошибкой, а не перехватывать часть запросов.
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
        super().__init__(server_address, DownloaderHandler)
        port = int(self.server_address[1])
        max_file_bytes = max_file_mb * MIB
        self.config = AppConfig(port=port)
        self.static = _load_static()
        self.csrf_token = secrets.token_urlsafe(32)
        self.info_cache = InfoCache(ttl_seconds=180)
        self.thumbnail_cache = ImageCache(downloader.fetch_thumbnail)
        self.avatar_cache = ImageCache(
            downloader.fetch_channel_avatar,
            ttl_seconds=6 * 60 * 60,
            max_entries=32,
        )
        self.rate_limiter = RateLimiter()
        self.jobs = JobManager(
            runtime_dir,
            max_file_bytes=max_file_bytes,
            job_ttl_seconds=job_ttl_min * 60,
            max_active_jobs=max_active_jobs,
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.config.port}"

    def server_close(self) -> None:
        self.jobs.shutdown()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            logger.debug("Клиент прервал соединение: %s", exc_type.__name__ if exc_type else "?")
            return
        logger.error("Ошибка обработки соединения: %s", exc, exc_info=True)


class DownloaderHandler(BaseHTTPRequestHandler):
    server: DownloaderServer
    server_version = "YouTubeDownloader"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(30.0)
        except OSError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        """Не выводим каждый HTTP-запрос в консоль."""

    def do_GET(self) -> None:
        self._handle_safely(self._handle_get)

    def do_HEAD(self) -> None:
        self._handle_safely(self._handle_get)

    def do_POST(self) -> None:
        # Тело POST может остаться непрочитанным при раннем отказе; закрываем
        # соединение, чтобы эти байты не стали следующим запросом.
        self.close_connection = True
        self._handle_safely(self._handle_post)

    def do_OPTIONS(self) -> None:
        self._handle_safely(self._handle_options)

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _handle_safely(self, handler: Callable[[], None]) -> None:
        try:
            if not self._host_is_valid():
                self.close_connection = True
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": {"code": "invalid_host", "message": "Неверный адрес сервера."}},
                )
                return
            handler()
        except ClientError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message}},
                retry_after=exc.retry_after,
            )
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Клиент разорвал соединение")
        except Exception:
            logger.exception("Необработанная ошибка HTTP-запроса")
            try:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера."}},
                )
            except Exception:
                logger.debug("Не удалось отправить ответ об ошибке", exc_info=True)

    def _host_is_valid(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.config.allowed_hosts

    def _handle_options(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": {"code": "method_not_allowed", "message": "Метод не поддерживается."}},
            extra_headers={"Allow": "GET, HEAD, POST"},
        )

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": {"code": "method_not_allowed", "message": "Метод не поддерживается."}},
            extra_headers={"Allow": "GET, HEAD, POST"},
        )

    def _handle_get(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "app": "YouTube Downloader"})
            return
        if path == "/api/bootstrap":
            self._send_json(
                HTTPStatus.OK,
                {
                    "csrf_token": self.server.csrf_token,
                },
            )
            return
        thumbnail_prefix = "/api/thumbnail/"
        if path.startswith(thumbnail_prefix):
            video_id = path[len(thumbnail_prefix) :]
            if VIDEO_ID_RE.fullmatch(video_id):
                self._handle_thumbnail(video_id)
                return
        avatar_prefix = "/api/avatar/"
        if path.startswith(avatar_prefix):
            channel_id = path[len(avatar_prefix) :]
            if CHANNEL_ID_RE.fullmatch(channel_id):
                self._handle_avatar(channel_id)
                return
        if path.startswith("/api/jobs/"):
            self._handle_job_get(path)
            return

        asset = self.server.static.get(path)
        if asset is not None:
            body, content_type = asset
            # Локальный интерфейс обновляется вместе с приложением, поэтому
            # не позволяем браузеру час показывать старые CSS/JS.
            self._send_bytes(
                HTTPStatus.OK,
                body,
                content_type,
                cache_control="no-cache",
            )
            return
        if path.startswith("/api/"):
            self._not_found(api=True)
            return
        self._not_found(api=False)

    def _handle_job_get(self, path: str) -> None:
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
        if match:
            job = self.server.jobs.get(match.group(1))
            if job is None:
                self._not_found(api=True)
                return
            self._send_json(HTTPStatus.OK, self.server.jobs.public(job))
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/file", path)
        if match:
            job = self.server.jobs.get_ready_file(match.group(1))
            if job is None:
                self._not_found(api=True)
                return
            self._serve_file(job)
            return

        self._not_found(api=True)

    def _serve_file(self, job: Job) -> None:
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
            for key, value in _security_headers().items():
                self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                logger.debug("Загрузка файла прервана клиентом")
            except OSError:
                logger.warning("Ошибка чтения готового файла")

    def _handle_thumbnail(self, video_id: str) -> None:
        self._serve_image(self.server.thumbnail_cache.get_or_fetch(video_id))

    def _handle_avatar(self, channel_id: str) -> None:
        self._serve_image(self.server.avatar_cache.get_or_fetch(channel_id))

    def _serve_image(self, result: tuple[bytes, str] | None) -> None:
        if result is None:
            self._not_found(api=True)
            return
        body, content_type = result
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type,
            cache_control="public, max-age=3600",
        )

    def _handle_post(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/info", "/api/download", "/api/jobs/delete"}:
            self._not_found(api=True)
            return

        self._require_same_origin()
        self._require_csrf()
        data = self._read_json_body()
        client_ip = self.client_address[0]

        if path == "/api/jobs/delete":
            self._reject_unknown_fields(data, {"job_id"})
            self._check_rate_limit(client_ip, "job-delete", limit=120, window=60)
            job_id = data.get("job_id")
            if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
                raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_job", "Некорректное задание.")
            self._send_json(
                HTTPStatus.OK,
                {"deleted": self.server.jobs.discard(job_id)},
            )
            return

        if path == "/api/info":
            self._reject_unknown_fields(data, {"url"})
            self._check_rate_limit(client_ip, "info", limit=30, window=60)
            raw_url = data.get("url")
            if not isinstance(raw_url, str):
                raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_url", "Вставьте ссылку на видео.")
            try:
                url = downloader.normalize_youtube_url(raw_url)
            except DownloadError as exc:
                raise ClientError(exc.http_status, exc.code, exc.message) from exc

            info = self.server.info_cache.get(url)
            if info is None:
                try:
                    info = downloader.get_video_info(url)
                except DownloadError as exc:
                    raise ClientError(exc.http_status, exc.code, exc.message) from exc
                self.server.info_cache.set(url, info)
            self._send_json(HTTPStatus.OK, info)
            return

        self._reject_unknown_fields(data, {"url", "mode", "height", "subtitle_lang", "auto_subtitle"})
        self._check_rate_limit(client_ip, "download", limit=12, window=60)
        raw_url = data.get("url")
        mode = data.get("mode")
        if not isinstance(raw_url, str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_url", "Вставьте ссылку на видео.")
        if mode not in {"video", "audio", "subtitles"}:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_mode", "Неизвестный режим скачивания.")
        try:
            url = downloader.normalize_youtube_url(raw_url)
        except DownloadError as exc:
            raise ClientError(exc.http_status, exc.code, exc.message) from exc

        height: int | None = None
        subtitle_lang: str | None = None
        auto_subtitle = False

        if mode == "video":
            height = data.get("height")
            if not isinstance(height, int) or isinstance(height, bool) or not 144 <= height <= 4320:
                raise ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_quality",
                    "Выберите доступное качество видео.",
                )
        elif mode == "subtitles":
            raw_lang = data.get("subtitle_lang")
            if not isinstance(raw_lang, str) or not SUBTITLE_LANG_RE.fullmatch(raw_lang):
                raise ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_subtitle",
                    "Выберите язык субтитров.",
                )
            subtitle_lang = raw_lang
            raw_auto = data.get("auto_subtitle", False)
            if not isinstance(raw_auto, bool):
                raise ClientError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_subtitle",
                    "Некорректный тип субтитров.",
                )
            auto_subtitle = raw_auto

        try:
            job_id = self.server.jobs.create(
                url=url,
                mode=mode,
                height=height,
                subtitle_lang=subtitle_lang,
                auto_subtitle=auto_subtitle,
            )
        except JobBusyError as exc:
            raise ClientError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "busy",
                "Уже выполняются другие загрузки. Дождитесь их завершения.",
                retry_after=20,
            ) from exc

        self._send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})

    def _require_same_origin(self) -> None:
        origin = (self.headers.get("Origin") or "").strip()
        if origin not in self.server.config.allowed_origins:
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
        token = self.headers.get("X-CSRF-Token") or ""
        if not token or not hmac.compare_digest(token, self.server.csrf_token):
            raise ClientError(
                HTTPStatus.FORBIDDEN,
                "csrf",
                "Сессия устарела. Обновите страницу.",
            )

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ClientError(
                HTTPStatus.LENGTH_REQUIRED,
                "invalid_body",
                "Передача тела запроса частями не поддерживается.",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ClientError(HTTPStatus.LENGTH_REQUIRED, "invalid_body", "Не указан размер запроса.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_body", "Некорректный размер запроса.") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ClientError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > MAX_BODY_BYTES else HTTPStatus.BAD_REQUEST,
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
        if len(raw) != length:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_body", "Запрос прочитан не полностью.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_json", "Некорректный JSON.") from exc
        if not isinstance(data, dict):
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON должен быть объектом.")
        return data

    @staticmethod
    def _reject_unknown_fields(data: dict[str, Any], allowed: set[str]) -> None:
        unknown = set(data) - allowed
        if unknown:
            raise ClientError(HTTPStatus.BAD_REQUEST, "unknown_field", "Запрос содержит лишние поля.")

    def _check_rate_limit(self, client_ip: str, action: str, *, limit: int, window: int) -> None:
        wait = self.server.rate_limiter.retry_after(f"{client_ip}:{action}", limit=limit, window=window)
        if wait:
            raise ClientError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limited",
                "Слишком много запросов. Сделайте паузу.",
                retry_after=wait,
            )

    def _not_found(self, *, api: bool) -> None:
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

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        retry_after: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
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
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if self.close_connection:
            self.send_header("Connection", "close")
        if retry_after is not None:
            self.send_header("Retry-After", str(max(1, int(retry_after))))
        for key, value in _security_headers().items():
            self.send_header(key, value)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Ожидается целое число") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("Число должно быть положительным")
    return number


def _port(value: str) -> int:
    number = _positive_int(value)
    if not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("Порт должен быть в диапазоне 1024–65535")
    return number


def create_server(
    port: int = 0,
    *,
    runtime_dir: Path | None = None,
    max_file_mb: int = DEFAULT_MAX_FILE_MB,
    job_ttl_min: int = DEFAULT_JOB_TTL_MIN,
    max_active_jobs: int = DEFAULT_MAX_ACTIVE_JOBS,
) -> DownloaderServer:
    if not 0 <= port <= 65535:
        raise ValueError("Некорректный порт")
    if not 16 <= max_file_mb <= 16384:
        raise ValueError("Лимит файла должен быть в диапазоне 16–16384 МБ")
    if not 1 <= job_ttl_min <= 1440:
        raise ValueError("Время хранения должно быть в диапазоне 1–1440 минут")
    if not 1 <= max_active_jobs <= 4:
        raise ValueError("Число одновременных загрузок должно быть 1–4")
    runtime = (runtime_dir or (BASE_DIR / ".runtime")).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    return DownloaderServer(
        ("127.0.0.1", port),
        runtime_dir=runtime,
        max_file_mb=max_file_mb,
        job_ttl_min=job_ttl_min,
        max_active_jobs=max_active_jobs,
    )


def _format_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Переменная %s должна быть числом, используется %s", name, default)
        return default
    if not minimum <= value <= maximum:
        logger.warning(
            "Переменная %s вне диапазона %s–%s, используется %s",
            name,
            minimum,
            maximum,
            default,
        )
        return default
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальный загрузчик YouTube")
    parser.add_argument(
        "--port",
        type=_port,
        default=_format_env_int("VD_PORT", DEFAULT_PORT, 1024, 65535),
        help="локальный порт (по умолчанию 8765)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="открыть интерфейс в браузере после запуска",
    )
    parser.add_argument(
        "--max-file-mb",
        type=_positive_int,
        default=_format_env_int("VD_MAX_FILE_MB", DEFAULT_MAX_FILE_MB, 16, 16384),
        help="максимальный размер одного файла в МБ",
    )
    parser.add_argument(
        "--job-ttl-min",
        type=_positive_int,
        default=_format_env_int("VD_JOB_TTL_MIN", DEFAULT_JOB_TTL_MIN, 1, 1440),
        help="резервный срок хранения временных файлов, минут",
    )
    parser.add_argument(
        "--max-active",
        type=_positive_int,
        default=_format_env_int("VD_MAX_ACTIVE", DEFAULT_MAX_ACTIVE_JOBS, 1, 4),
        help="сколько загрузок выполнять одновременно",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not 16 <= args.max_file_mb <= 16384:
        parser.error("--max-file-mb должен быть в диапазоне 16–16384")
    if not 1 <= args.job_ttl_min <= 1440:
        parser.error("--job-ttl-min должен быть в диапазоне 1–1440")
    if not 1 <= args.max_active <= 4:
        parser.error("--max-active должен быть в диапазоне 1–4")

    try:
        server = create_server(
            args.port,
            max_file_mb=args.max_file_mb,
            job_ttl_min=args.job_ttl_min,
            max_active_jobs=args.max_active,
        )
    except OSError as exc:
        logger.error("Не удалось занять порт %s: %s", args.port, exc)
        print(
            f"Не удалось запустить загрузчик на порту {args.port}. "
            "Возможно, он уже занят. Попробуйте --port 8766.",
            file=sys.stderr,
        )
        return 1

    url = server.base_url + "/"
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
