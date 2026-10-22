"""Безопасный локальный загрузчик YouTube.

Модуль намеренно не принимает от пользователя произвольные параметры yt-dlp.
Наружу выставлены только три режима (видео, звук, субтитры), числовое качество
и проверенный код языка субтитров. Все остальные настройки зашиты в коде.
"""

from __future__ import annotations

import functools
import html
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 200
MAX_VIDEO_HEIGHT = 4320
MAX_SUBTITLE_OPTIONS = 200
MIN_FREE_DISK_BYTES = 256 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
MAX_AVATAR_BYTES = 512 * 1024
MIB = 1024 * 1024
THUMBNAIL_URL_TEMPLATE = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
_AVATAR_HOSTS = frozenset({"yt3.googleusercontent.com", "yt3.ggpht.com"})

_ALLOWED_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
        "www.youtu.be",
    }
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_SUBTITLE_LANG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,34}$")
_SHORT_PATH_RE = re.compile(r"^/(shorts|embed|live|v)/([A-Za-z0-9_-]{11})/?$")
_WATCH_PATHS = frozenset({"/watch", "/watch/"})
_CONTROL_OR_FORMAT_RE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069]")
_WINDOWS_RESERVED_RE = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE
)
_TIMESTAMP_RE = re.compile(
    r"^\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3}"
)
_TAG_RE = re.compile(r"<[^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


class DownloadError(Exception):
    """Ожидаемая ошибка, которую можно безопасно показать пользователю."""

    def __init__(self, code: str, message: str, *, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    filename: str
    size: int


class _QuietLogger:
    """Не даём yt-dlp печатать технические подробности в консоль."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.debug("yt-dlp warning: %s", message)

    def error(self, message: str) -> None:
        logger.debug("yt-dlp error: %s", message)


def normalize_youtube_url(raw_url: object) -> str:
    """Проверить и нормализовать ссылку до одного канонического адреса видео.

    Разрешены только https-ссылки YouTube на конкретное видео. Плейлисты,
    каналы, произвольные хосты, порты и учётные данные отклоняются. Это
    одновременно защищает от SSRF и не даёт превратить загрузчик в средство
    доступа к произвольным сетевым адресам.
    """

    if not isinstance(raw_url, str):
        raise DownloadError("invalid_url", "Вставьте ссылку на видео YouTube.", http_status=400)

    value = raw_url.strip()
    if not value:
        raise DownloadError("invalid_url", "Вставьте ссылку на видео YouTube.", http_status=400)
    if len(value) > MAX_URL_LENGTH:
        raise DownloadError("invalid_url", "Ссылка слишком длинная.", http_status=400)
    if _CONTROL_OR_FORMAT_RE.search(value):
        raise DownloadError("invalid_url", "Ссылка содержит недопустимые символы.", http_status=400)

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("invalid_url", "Не удалось разобрать ссылку.", http_status=400) from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise DownloadError("invalid_url", "Поддерживаются только ссылки http(s).", http_status=400)
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise DownloadError("invalid_url", "Ссылка содержит недопустимые части.", http_status=400)

    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in _ALLOWED_HOSTS:
        raise DownloadError(
            "unsupported_host",
            "Поддерживаются только ссылки на youtube.com и youtu.be.",
            http_status=400,
        )

    video_id = ""
    if host in {"youtu.be", "www.youtu.be"}:
        match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parsed.path)
        if match:
            video_id = match.group(1)
    else:
        if parsed.path in _WATCH_PATHS:
            query = parse_qs(parsed.query, keep_blank_values=True)
            ids = query.get("v", [])
            if len(ids) == 1 and _VIDEO_ID_RE.fullmatch(ids[0]):
                video_id = ids[0]
        else:
            match = _SHORT_PATH_RE.fullmatch(parsed.path)
            if match:
                video_id = match.group(2)

    if not video_id:
        raise DownloadError(
            "invalid_url",
            "Нужна ссылка на конкретное видео YouTube, а не на плейлист или канал.",
            http_status=400,
        )

    return f"https://www.youtube.com/watch?v={video_id}"


def sanitize_filename(value: object, *, fallback: str = "youtube-video") -> str:
    """Сделать безопасное отображаемое имя файла (не путь)."""

    if not isinstance(value, str):
        return fallback

    text = unicodedata.normalize("NFC", value)
    text = _CONTROL_OR_FORMAT_RE.sub("", text)
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip(" .")
    if not text:
        return fallback

    if len(text) > 160:
        text = text[:160].rstrip(" .")

    stem = text.split(".", 1)[0]
    if _WINDOWS_RESERVED_RE.match(stem):
        text = f"_{text}"

    return text or fallback


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL_OR_FORMAT_RE.sub("", value)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def _safe_int(value: object, *, minimum: int = 0, maximum: int = 10**9) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if number < minimum or number > maximum:
        return None
    return number


def _safe_float(value: object, *, minimum: float = 0.0, maximum: float = 10**9) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < minimum or number > maximum:
        return None
    return number


def _file_size(format_info: dict[str, Any], duration: int | None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = _safe_int(format_info.get(key), minimum=1, maximum=10**12)
        if value:
            return value
    bitrate = _safe_float(format_info.get("tbr"), maximum=10**6)
    if bitrate and duration:
        return max(1, int(bitrate * 1000 / 8 * duration))
    return None


def _best_audio_size(formats: list[Any], duration: int | None) -> int | None:
    best: int | None = None
    for item in formats:
        if not isinstance(item, dict):
            continue
        if (item.get("acodec") or "none") == "none" or (item.get("vcodec") or "none") != "none":
            continue
        size = _file_size(item, duration)
        if size and (best is None or size > best):
            best = size
    return best


def _estimate_mode_size(info: dict[str, Any], mode: str, height: int | None) -> int | None:
    formats = info.get("formats") or []
    duration = _safe_int(info.get("duration"), maximum=48 * 60 * 60)
    if mode == "audio":
        return _best_audio_size(formats, duration)
    if mode == "video" and height:
        for quality in _collect_qualities(info):
            if quality["height"] == height:
                return quality.get("filesize")
    return None


def _collect_qualities(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Вернуть все доступные разрешения без дублей по высоте."""

    duration = _safe_int(info.get("duration"), maximum=48 * 60 * 60)
    formats = info.get("formats") or []
    audio_size = _best_audio_size(formats, duration)
    by_height: dict[int, tuple[tuple[int, int, float, int], dict[str, Any]]] = {}

    for item in formats:
        if not isinstance(item, dict):
            continue
        vcodec = str(item.get("vcodec") or "none")
        if vcodec == "none":
            continue
        if item.get("has_drm") or item.get("drm"):
            continue
        format_id = str(item.get("format_id") or "")
        if format_id.startswith("sb"):
            continue
        ext = str(item.get("ext") or "")
        if ext not in {"mp4", "webm"}:
            continue
        height = _safe_int(item.get("height"), minimum=144, maximum=MAX_VIDEO_HEIGHT)
        if height is None:
            continue

        fps = _safe_float(item.get("fps"), maximum=240) or 0.0
        codec = "H.264" if vcodec.startswith("avc1") else "AV1" if vcodec.startswith("av01") else "VP9" if vcodec.startswith(("vp9", "vp09")) else vcodec.upper()
        video_size = _file_size(item, duration)
        hdr = str(item.get("dynamic_range") or "SDR").upper() != "SDR"
        estimate = (video_size + audio_size) if video_size and audio_size else video_size or audio_size

        # Приоритет: MP4, H.264, затем больший fps/размер. Это даёт наиболее
        # совместимый файл, который потом без перекодирования упаковывается в MP4.
        score = (1 if ext == "mp4" else 0, 1 if vcodec.startswith("avc1") else 0, fps, video_size or 0)
        candidate = {
            "height": height,
            "fps": max(1, int(round(fps))) if fps else None,
            "filesize": estimate,
            "hdr": hdr,
            "codec": codec,
        }
        previous = by_height.get(height)
        if previous is None or score > previous[0]:
            by_height[height] = (score, candidate)

    return [entry[1] for entry in sorted(by_height.values(), key=lambda pair: pair[1]["height"], reverse=True)]


def _collect_subtitles(info: dict[str, Any]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    sources = (
        (info.get("subtitles"), False),
        (info.get("automatic_captions"), True),
    )
    for source, automatic in sources:
        if not isinstance(source, dict):
            continue
        for code in source:
            if not isinstance(code, str) or not _SUBTITLE_LANG_RE.fullmatch(code):
                continue
            if code in result:
                continue
            result[code] = {"code": code, "auto": automatic}
            if len(result) >= MAX_SUBTITLE_OPTIONS:
                return sorted(result.values(), key=lambda item: (item["auto"], item["code"]))
    return sorted(result.values(), key=lambda item: (item["auto"], item["code"]))


def _ensure_playable(info: dict[str, Any]) -> None:
    if info.get("_type") == "playlist":
        raise DownloadError("playlist", "Плейлисты не поддерживаются. Вставьте ссылку на видео.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise DownloadError("live", "Прямые трансляции и премьеры пока не поддерживаются.")
    availability = str(info.get("availability") or "").lower()
    if availability in {"private", "premium", "subscriber_only", "needs_auth"}:
        raise DownloadError("not_available", "Это видео недоступно без входа в аккаунт.")


def _base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreconfig": True,
        "cachedir": False,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 1,
        "nocheckcertificate": False,
        "windowsfilenames": True,
        "trim_file_name": 120,
        "overwrites": True,
        "concurrent_fragment_downloads": 4,
        "logger": _QuietLogger(),
    }


def _map_ytdlp_error(exc: BaseException, *, context: str) -> DownloadError:
    raw = str(exc)
    lower = raw.casefold()
    logger.warning("Ошибка yt-dlp (%s): %s", context, raw[:1500])

    if "not enough free disk space" in lower:
        return DownloadError(
            "disk_full",
            "Недостаточно свободного места на диске для этого файла.",
            http_status=507,
        )
    if "live streams are not supported" in lower:
        return DownloadError("live", "Прямые трансляции и премьеры пока не поддерживаются.")
    if "file is larger than max-filesize" in lower or "file is larger than" in lower:
        return DownloadError("file_too_large", "Файл превышает установленный лимит размера.", http_status=413)
    if "private video" in lower or "video is private" in lower:
        return DownloadError("not_available", "Это приватное видео. Доступ к нему закрыт.")
    if "age" in lower and ("restricted" in lower or "confirm" in lower):
        return DownloadError(
            "age_restricted",
            "Видео с возрастным ограничением требует входа в аккаунт и здесь недоступно.",
        )
    if "not a bot" in lower or ("confirm you" in lower and "bot" in lower):
        return DownloadError(
            "bot_check",
            "YouTube запросил проверку «я не робот». Попробуйте позже или другое видео.",
            http_status=503,
        )
    if "requested format is not available" in lower:
        return DownloadError(
            "format_unavailable",
            "Выбранное качество больше недоступно. Обновите список вариантов.",
        )
    if "http error 429" in lower or "too many requests" in lower:
        return DownloadError(
            "upstream_rate_limit",
            "YouTube временно ограничил частоту запросов. Подождите несколько минут.",
            http_status=429,
        )
    if "http error 403" in lower:
        return DownloadError(
            "youtube_forbidden",
            "YouTube отклонил загрузку. Повторите попытку позже.",
            http_status=502,
        )
    if "subtitles" in lower and ("unable to download" in lower or "no subtitles" in lower):
        return DownloadError(
            "subtitles_unavailable",
            "Не удалось получить субтитры для этого видео. Попробуйте другой язык.",
        )
    if "timed out" in lower or "timeout" in lower:
        return DownloadError(
            "timeout",
            "YouTube не ответил вовремя. Проверьте интернет и повторите попытку.",
            http_status=504,
        )
    if "no space left" in lower or "errno 28" in lower:
        return DownloadError(
            "disk_full",
            "На диске закончилось свободное место.",
            http_status=507,
        )
    if "ffmpeg" in lower and ("not found" in lower or "not installed" in lower):
        return DownloadError(
            "ffmpeg_missing",
            "Не найден ffmpeg. Переустановите зависимости приложения.",
            http_status=500,
        )
    if "unable to download webpage" in lower or "failed to resolve" in lower or "connection" in lower:
        return DownloadError(
            "network",
            "Не удалось подключиться к YouTube. Проверьте интернет-соединение.",
            http_status=502,
        )
    if "video unavailable" in lower or "not available" in lower or "does not exist" in lower:
        return DownloadError("not_available", "Видео недоступно или было удалено.", http_status=404)

    return DownloadError(
        "download_failed",
        "Не удалось скачать файл. Попробуйте ещё раз.",
        http_status=500,
    )


@functools.lru_cache(maxsize=1)
def _ffmpeg_executable() -> str:
    override = os.environ.get("VD_FFMPEG", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise DownloadError(
            "ffmpeg_missing",
            "Путь VD_FFMPEG указывает на несуществующий файл.",
            http_status=500,
        )

    try:
        import imageio_ffmpeg

        candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if candidate.is_file():
            return str(candidate.resolve())
    except Exception:
        logger.debug("Не удалось получить ffmpeg из imageio-ffmpeg", exc_info=True)

    candidate = shutil.which("ffmpeg")
    if candidate and Path(candidate).is_file():
        return str(Path(candidate).resolve())

    raise DownloadError(
        "ffmpeg_missing",
        "Не найден ffmpeg. Установите зависимости командой из README.",
        http_status=500,
    )


def _extract_info(url: str) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(_base_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except YtDlpDownloadError as exc:
        raise _map_ytdlp_error(exc, context="metadata") from exc
    except Exception as exc:
        logger.exception("Непредвиденная ошибка при получении метаданных")
        raise DownloadError("metadata_failed", "Не удалось получить сведения о видео.", http_status=502) from exc

    if not isinstance(info, dict) or info.get("_type") == "playlist":
        raise DownloadError("playlist", "Плейлисты не поддерживаются. Вставьте ссылку на видео.")
    _ensure_playable(info)
    return info


def get_video_info(raw_url: object) -> dict[str, Any]:
    """Получить только безопасную часть метаданных для интерфейса."""

    url = normalize_youtube_url(raw_url)
    info = _extract_info(url)
    title = _clean_text(info.get("title"), 300) or "Видео YouTube"
    channel = _clean_text(info.get("uploader") or info.get("channel"), 160)
    duration = _safe_int(info.get("duration"), maximum=48 * 60 * 60)
    video_id = str(info.get("id") or "")
    if not _VIDEO_ID_RE.fullmatch(video_id):
        video_id = parse_qs(urlsplit(url).query).get("v", [""])[0]

    qualities = _collect_qualities(info)
    subtitles = _collect_subtitles(info)
    formats = info.get("formats") or []
    has_audio = any(
        isinstance(item, dict) and (item.get("acodec") or "none") != "none" for item in formats
    )

    channel_id_value = info.get("channel_id")
    channel_id = channel_id_value.strip() if isinstance(channel_id_value, str) else ""
    if not _CHANNEL_ID_RE.fullmatch(channel_id):
        channel_id = ""

    thumbnail_url = f"/api/thumbnail/{video_id}" if _VIDEO_ID_RE.fullmatch(video_id) else None
    channel_avatar_url = f"/api/avatar/{channel_id}" if channel_id else None
    return {
        "url": url,
        "video_id": video_id,
        "title": title,
        "channel": channel,
        "channel_id": channel_id,
        "channel_avatar_url": channel_avatar_url,
        "duration": duration,
        "thumbnail_url": thumbnail_url,
        "qualities": qualities,
        "subtitles": subtitles,
        "has_audio": has_audio,
    }


def fetch_thumbnail(video_id: str) -> tuple[bytes, str] | None:
    """Получить превью с фиксированного хоста YouTube (без произвольных URL)."""

    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        return None
    url = THUMBNAIL_URL_TEMPLATE.format(video_id=video_id)
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "image/jpeg",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) YouTubeDownloader/1.0",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=8) as response:
            final_host = (urlsplit(response.geturl()).hostname or "").lower()
            if final_host not in {"i.ytimg.com", "img.youtube.com"}:
                return None
            content_type = (response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/jpeg"):
                return None
            data = response.read(MAX_THUMBNAIL_BYTES + 1)
    except (urllib_error.URLError, OSError, ValueError):
        logger.debug("Не удалось загрузить превью %s", video_id, exc_info=True)
        return None
    if not data or len(data) > MAX_THUMBNAIL_BYTES or not data.startswith(b"\xff\xd8\xff"):
        return None
    return data, "image/jpeg"


def _is_safe_avatar_url(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return False
    if port is not None:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    return host in _AVATAR_HOSTS and bool(parsed.path)


def _small_avatar_url(url: str) -> str:
    """Запросить у YouTube уменьшенную версию аватарки, а не 900×900."""

    if re.search(r"=s\d+", url):
        return re.sub(r"=s\d+.*$", "=s176-c-k-c0x00ffffff-no-rj", url, count=1)
    return f"{url}=s176-c-k-c0x00ffffff-no-rj"


def _pick_channel_avatar_url(info: dict[str, Any]) -> str | None:
    """Выбрать квадратную аватарку канала, а не баннер."""

    thumbnails = info.get("thumbnails")
    if not isinstance(thumbnails, list):
        return None

    preferred: list[str] = []
    square: list[str] = []
    for item in thumbnails:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not _is_safe_avatar_url(url):
            continue
        identifier = str(item.get("id") or "")
        if identifier in {"7", "avatar_uncropped"}:
            preferred.append(url)
            continue
        width = _safe_int(item.get("width"), maximum=10000)
        height = _safe_int(item.get("height"), maximum=10000)
        if width and height and width == height and width >= 48:
            square.append(url)

    candidates = preferred or square
    if not candidates:
        return None
    # "7" — нормальная квадратная версия 900×900; avatar_uncropped — исходник.
    ordered = sorted(
        candidates,
        key=lambda value: 0 if "=s900" in value else 1,
    )
    return _small_avatar_url(ordered[0])


def _sniff_image_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    return None


def fetch_channel_avatar(channel_id: str) -> tuple[bytes, str] | None:
    """Получить логотип канала по проверенному ID, без произвольных адресов."""

    if not isinstance(channel_id, str) or not _CHANNEL_ID_RE.fullmatch(channel_id):
        return None

    options = _base_options()
    options.update({"skip_download": True, "playlistend": 0})
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            channel_info = ydl.extract_info(
                f"https://www.youtube.com/channel/{channel_id}",
                download=False,
            )
    except (YtDlpDownloadError, OSError, ValueError) as exc:
        logger.debug("Не удалось получить данные канала %s: %s", channel_id, exc)
        return None
    except Exception:
        logger.debug("Непредвиденная ошибка при получении канала %s", channel_id, exc_info=True)
        return None

    if not isinstance(channel_info, dict):
        return None
    avatar_url = _pick_channel_avatar_url(channel_info)
    if avatar_url is None:
        return None

    request = urllib_request.Request(
        avatar_url,
        headers={
            "Accept": "image/webp,image/png,image/jpeg",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) YouTubeDownloader/1.0",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=8) as response:
            final_url = response.geturl()
            if not _is_safe_avatar_url(final_url):
                return None
            data = response.read(MAX_AVATAR_BYTES + 1)
    except (urllib_error.URLError, OSError, ValueError):
        logger.debug("Не удалось загрузить аватар канала %s", channel_id, exc_info=True)
        return None
    if not data or len(data) > MAX_AVATAR_BYTES:
        return None
    content_type = _sniff_image_type(data)
    if content_type is None:
        return None
    return data, content_type


def _match_filter_for_request(
    output_dir: Path,
    mode: str,
    height: int | None,
    max_file_bytes: int,
) -> Callable[..., str | None]:
    def match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        if info.get("_type") == "playlist":
            return "Playlists are not supported"
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            return "Live streams are not supported"

        estimate = _estimate_mode_size(info, mode, height)
        if estimate and estimate > max_file_bytes:
            return "File is larger than max-filesize"
        required = max(MIN_FREE_DISK_BYTES, int((estimate or 0) * 1.5) + 128 * MIB)
        try:
            if shutil.disk_usage(output_dir).free < required:
                return "Not enough free disk space"
        except OSError:
            return "Not enough free disk space"
        return None

    return match_filter


def _video_format_selector(height: int) -> str:
    if not 144 <= height <= MAX_VIDEO_HEIGHT:
        raise DownloadError("invalid_quality", "Недопустимое качество видео.", http_status=400)
    # Строка собрана только из проверенного целого числа. Никакие данные
    # пользователя сюда не попадают.
    return (
        f"bv*[height={height}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height={height}]+ba/"
        f"b[height={height}][ext=mp4]/"
        f"b[height={height}]"
    )


def _progress_hook(mode: str, callback: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    phase = {
        "video": "Скачивание видео",
        "audio": "Скачивание звука",
        "subtitles": "Скачивание субтитров",
    }.get(mode, "Скачивание")

    def hook(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        status = payload.get("status")
        if status == "downloading":
            downloaded = _safe_int(payload.get("downloaded_bytes"), maximum=10**13) or 0
            total = _safe_int(payload.get("total_bytes") or payload.get("total_bytes_estimate"), maximum=10**13)
            percent = (downloaded / total * 100.0) if total else None
            try:
                callback(
                    {
                        "phase": phase,
                        "percent": percent,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed": _safe_float(payload.get("speed"), maximum=10**12),
                        "eta": _safe_int(payload.get("eta"), maximum=7 * 24 * 60 * 60),
                    }
                )
            except Exception:
                logger.debug("Ошибка в progress callback", exc_info=True)
        elif status == "finished":
            try:
                callback({"phase": "Обработка файла", "percent": 88.0})
            except Exception:
                logger.debug("Ошибка в progress callback", exc_info=True)

    return hook


def _postprocessor_hook(callback: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    labels = {
        "FFmpegMerger": "Объединение видео и звука",
        "FFmpegVideoRemuxer": "Подготовка MP4",
        "FFmpegExtractAudio": "Конвертация в MP3",
        "FFmpegVideoConvertor": "Конвертация видео",
    }

    def hook(payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or payload.get("status") != "started":
            return
        name = str(payload.get("postprocessor") or "")
        label = labels.get(name, "Обработка файла")
        try:
            callback({"phase": label, "percent": 92.0})
        except Exception:
            logger.debug("Ошибка в postprocessor callback", exc_info=True)

    return hook


def _download_options(
    mode: str,
    output_dir: Path,
    *,
    height: int | None,
    subtitle_lang: str | None,
    auto_subtitle: bool,
    max_file_bytes: int,
    progress: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    options = _base_options()
    options.update(
        {
            "paths": {"home": str(output_dir), "temp": str(output_dir)},
            "outtmpl": "media.%(ext)s",
            "max_filesize": max_file_bytes,
            "match_filter": _match_filter_for_request(
                output_dir,
                mode,
                height,
                max_file_bytes,
            ),
        }
    )

    if progress is not None:
        options["progress_hooks"] = [_progress_hook(mode, progress)]
        options["postprocessor_hooks"] = [_postprocessor_hook(progress)]

    if mode == "video":
        if height is None:
            raise DownloadError("invalid_quality", "Выберите качество видео.", http_status=400)
        options.update(
            {
                "format": _video_format_selector(height),
                "merge_output_format": "mp4",
                "ffmpeg_location": _ffmpeg_executable(),
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
            }
        )
    elif mode == "audio":
        options.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "ffmpeg_location": _ffmpeg_executable(),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    elif mode == "subtitles":
        if not subtitle_lang or not _SUBTITLE_LANG_RE.fullmatch(subtitle_lang):
            raise DownloadError("invalid_subtitle", "Выберите язык субтитров.", http_status=400)
        options.update(
            {
                "skip_download": True,
                "writesubtitles": not auto_subtitle,
                "writeautomaticsub": auto_subtitle,
                "subtitleslangs": [subtitle_lang],
                "subtitlesformat": "vtt",
            }
        )
    else:
        raise DownloadError("invalid_mode", "Неизвестный режим скачивания.", http_status=400)

    return options


def vtt_to_text(content: str) -> str:
    """Преобразовать WebVTT (или SRT) в аккуратный обычный текст."""

    text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""

    lines: list[str] = []
    for block in re.split(r"\n[ \t]*\n", text):
        raw_lines = [line.strip("\ufeff") for line in block.split("\n") if line.strip()]
        if not raw_lines:
            continue
        first = raw_lines[0].strip()
        if first.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        timestamp_index = next(
            (index for index, line in enumerate(raw_lines) if "-->" in line and _TIMESTAMP_RE.match(line)),
            None,
        )
        if timestamp_index is None:
            continue

        cue_lines = raw_lines[timestamp_index + 1 :]
        cleaned_parts: list[str] = []
        for cue_line in cue_lines:
            cue_line = _BR_RE.sub(" ", cue_line)
            cue_line = _TAG_RE.sub("", cue_line)
            cue_line = html.unescape(cue_line)
            cue_line = _CONTROL_OR_FORMAT_RE.sub("", cue_line)
            cue_line = _WHITESPACE_RE.sub(" ", cue_line).strip()
            if cue_line:
                cleaned_parts.append(cue_line)

        cue_text = " ".join(cleaned_parts).strip()
        if cue_text and (not lines or lines[-1] != cue_text):
            lines.append(cue_text)

    return "\n".join(lines)


def _find_output_file(output_dir: Path, extensions: set[str]) -> Path | None:
    root = output_dir.resolve()
    candidates: list[Path] = []
    try:
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.parent != root:
                continue
            candidates.append(resolved)
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_size)


def _display_filename(title: str, video_id: str, suffix: str, extension: str) -> str:
    base = f"{title} [{video_id}]"
    if suffix:
        base = f"{base} {suffix}"
    return sanitize_filename(f"{base}.{extension}", fallback=f"youtube-{video_id}.{extension}")


def _finalize_file(output_dir: Path, source: Path, target_name: str, max_file_bytes: int) -> Path:
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise DownloadError("download_failed", "Скачанный файл не найден.", http_status=500) from exc
    if size <= 0:
        raise DownloadError("empty_file", "Получился пустой файл. Попробуйте другой вариант.", http_status=422)
    if size > max_file_bytes:
        raise DownloadError("file_too_large", "Файл превышает установленный лимит размера.", http_status=413)

    target = output_dir / target_name
    try:
        os.replace(source, target)
    except OSError as exc:
        raise DownloadError("file_error", "Не удалось сохранить файл.", http_status=500) from exc
    return target


def _convert_subtitles(output_dir: Path) -> Path:
    source = _find_output_file(output_dir, {".vtt", ".srt"})
    if source is None:
        raise DownloadError(
            "subtitles_unavailable",
            "У этого видео нет субтитров на выбранном языке.",
        )
    try:
        content = source.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise DownloadError("subtitles_unavailable", "Не удалось прочитать субтитры.", http_status=500) from exc

    text = vtt_to_text(content)
    if not text:
        raise DownloadError("subtitles_empty", "Субтитры оказались пустыми. Выберите другой язык.")
    target = output_dir / "subtitles.txt"
    try:
        with target.open("w", encoding="utf-8-sig", newline="\n") as handle:
            handle.write(text + "\n")
    except OSError as exc:
        raise DownloadError("file_error", "Не удалось сохранить субтитры.", http_status=500) from exc

    try:
        source.unlink(missing_ok=True)
    except OSError:
        logger.debug("Не удалось удалить исходный VTT-файл", exc_info=True)
    return target


def download(
    raw_url: object,
    mode: str,
    *,
    height: int | None = None,
    subtitle_lang: str | None = None,
    auto_subtitle: bool = False,
    output_dir: Path,
    max_file_bytes: int = 4 * 1024 * MIB,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> DownloadResult:
    """Скачать один файл в заранее созданный каталог задания."""

    if mode not in {"video", "audio", "subtitles"}:
        raise DownloadError("invalid_mode", "Неизвестный режим скачивания.", http_status=400)
    if mode == "video":
        if not isinstance(height, int) or isinstance(height, bool):
            raise DownloadError("invalid_quality", "Выберите качество видео.", http_status=400)
        if not 144 <= height <= MAX_VIDEO_HEIGHT:
            raise DownloadError("invalid_quality", "Недопустимое качество видео.", http_status=400)
    if mode == "subtitles":
        if not isinstance(subtitle_lang, str) or not _SUBTITLE_LANG_RE.fullmatch(subtitle_lang):
            raise DownloadError("invalid_subtitle", "Выберите язык субтитров.", http_status=400)
        if not isinstance(auto_subtitle, bool):
            auto_subtitle = False

    url = normalize_youtube_url(raw_url)
    output_dir = output_dir.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError("file_error", "Не удалось создать папку для загрузки.", http_status=500) from exc

    try:
        free = shutil.disk_usage(output_dir).free
    except OSError as exc:
        raise DownloadError(
            "disk_unavailable",
            "Не удалось проверить свободное место на диске.",
            http_status=500,
        ) from exc
    if free < MIN_FREE_DISK_BYTES:
        raise DownloadError(
            "disk_full",
            "На диске слишком мало свободного места.",
            http_status=507,
        )

    if progress is not None:
        try:
            progress({"phase": "Получение информации", "percent": 2.0})
        except Exception:
            logger.debug("Ошибка в progress callback", exc_info=True)

    options = _download_options(
        mode,
        output_dir,
        height=height,
        subtitle_lang=subtitle_lang,
        auto_subtitle=auto_subtitle,
        max_file_bytes=max_file_bytes,
        progress=progress,
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except YtDlpDownloadError as exc:
        raise _map_ytdlp_error(exc, context=mode) from exc
    except DownloadError:
        raise
    except Exception as exc:
        logger.exception("Непредвиденная ошибка при скачивании (%s)", mode)
        raise DownloadError("download_failed", "Не удалось скачать файл. Попробуйте ещё раз.", http_status=500) from exc

    if not isinstance(info, dict):
        raise DownloadError("download_failed", "YouTube не вернул данные о видео.", http_status=502)
    _ensure_playable(info)

    title = _clean_text(info.get("title"), MAX_TITLE_LENGTH) or "Видео YouTube"
    video_id = str(info.get("id") or "")
    if not _VIDEO_ID_RE.fullmatch(video_id):
        video_id = parse_qs(urlsplit(url).query).get("v", [""])[0]

    if mode == "video":
        source = _find_output_file(output_dir, {".mp4"})
        if source is None:
            raise DownloadError(
                "conversion_failed",
                "Не удалось подготовить MP4. Попробуйте другое качество.",
                http_status=500,
            )
        path = _finalize_file(output_dir, source, "video.mp4", max_file_bytes)
        filename = _display_filename(title, video_id, f"{height}p", "mp4")
    elif mode == "audio":
        source = _find_output_file(output_dir, {".mp3"})
        if source is None:
            raise DownloadError(
                "conversion_failed",
                "Не удалось подготовить MP3. Попробуйте ещё раз.",
                http_status=500,
            )
        path = _finalize_file(output_dir, source, "audio.mp3", max_file_bytes)
        filename = _display_filename(title, video_id, "", "mp3")
    else:
        if progress is not None:
            try:
                progress({"phase": "Сохранение субтитров", "percent": 80.0})
            except Exception:
                logger.debug("Ошибка в progress callback", exc_info=True)
        path = _convert_subtitles(output_dir)
        filename = _display_filename(title, video_id, f"[{subtitle_lang}]", "txt")

    size = path.stat().st_size
    return DownloadResult(path=path, filename=filename, size=size)
