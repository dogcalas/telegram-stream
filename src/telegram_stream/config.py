"""Validated environment configuration; secrets never appear in reprs."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from telegram_stream.errors import FatalError


def _number(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise FatalError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise FatalError(f"{name} must be between {low} and {high}")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, str(default)).lower()
    if value not in {"true", "false", "1", "0"}:
        raise FatalError(f"{name} must be true or false")
    return value in {"true", "1"}


def _bitrate(value: str, name: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*[kKmM]?", value):
        raise FatalError(f"{name} must be a positive bitrate, for example 2500k")
    multiplier = {"k": 1000, "m": 1_000_000}.get(value[-1].lower(), 1)
    return int(value[:-1] if multiplier != 1 else value) * multiplier


STREAM_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")


def stream_key(env: Mapping[str, str]) -> str:
    """The key travels in RTMP URLs, so restrict it to URL-safe characters."""
    key = env.get("RTMP_STREAM_KEY", "")
    if not STREAM_KEY.fullmatch(key):
        raise FatalError("RTMP_STREAM_KEY must be 16-128 characters from A-Z, a-z, 0-9, _ or -")
    return key


def data_directory() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    channel: str = ""
    source: str = field(default="", repr=False)
    source_type: str = "auto"
    data_dir: Path = field(default_factory=data_directory)
    width: int = 1280
    height: int = 720
    fps: int = 30
    video_bitrate: int = 2_500_000
    audio_bitrate: int = 128_000
    preset: str = "veryfast"
    poll_interval: int = 30
    rpc_timeout: int = 15
    source_timeout: int = 60
    stall_timeout: int = 45
    startup_timeout: int = 60
    retry_initial: int = 2
    retry_max: int = 120
    shutdown_timeout: int = 10
    caption: str = ""
    adopt_existing: bool = False
    end_on_shutdown: bool = False
    cookies_file: Path | None = field(default=None, repr=False)
    extractor_args: str = field(default="", repr=False)
    web_stream_url: str = field(default="http://web:8080/index.m3u8", repr=False)
    rtmp_ingest_url: str = field(default="rtmp://ingest:1935/live", repr=False)
    rtmp_stream_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, require_stream: bool = True) -> Self:
        env = os.environ if env is None else env
        api_id = _number(env, "TELEGRAM_API_ID", 0, 1, 2**31 - 1)
        api_hash = env.get("TELEGRAM_API_HASH", "")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise FatalError("TELEGRAM_API_HASH must be the 32-character API hash")
        kind = env.get("SOURCE_TYPE", "auto").lower()
        if kind not in {"auto", "youtube", "hls", "file", "web", "rtmp"}:
            raise FatalError("SOURCE_TYPE must be auto, youtube, hls, file, web, or rtmp")
        preset = env.get("ENCODER_PRESET", "veryfast")
        if preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}:
            raise FatalError("Unsupported ENCODER_PRESET")
        settings = cls(
            api_id=api_id,
            api_hash=api_hash,
            channel=env.get("TELEGRAM_CHANNEL", ""),
            source=env.get("SOURCE", ""),
            source_type=kind,
            data_dir=Path(env.get("DATA_DIR", "./data")).expanduser().resolve(),
            width=_number(env, "VIDEO_WIDTH", 1280, 128, 4096),
            height=_number(env, "VIDEO_HEIGHT", 720, 128, 4096),
            fps=_number(env, "VIDEO_FPS", 30, 1, 60),
            video_bitrate=_bitrate(env.get("VIDEO_BITRATE", "2500k"), "VIDEO_BITRATE"),
            audio_bitrate=_bitrate(env.get("AUDIO_BITRATE", "128k"), "AUDIO_BITRATE"),
            preset=preset,
            poll_interval=_number(env, "TELEGRAM_POLL_INTERVAL", 30, 5, 3600),
            rpc_timeout=_number(env, "RPC_TIMEOUT", 15, 5, 120),
            source_timeout=_number(env, "SOURCE_TIMEOUT", 60, 5, 300),
            stall_timeout=_number(env, "STALL_TIMEOUT", 45, 5, 300),
            startup_timeout=_number(env, "STARTUP_TIMEOUT", 60, 5, 300),
            retry_initial=_number(env, "RETRY_INITIAL", 2, 1, 300),
            retry_max=_number(env, "RETRY_MAX", 120, 1, 3600),
            shutdown_timeout=_number(env, "SHUTDOWN_TIMEOUT", 10, 1, 60),
            caption=env.get("LIVE_STORY_CAPTION", ""),
            adopt_existing=_boolean(env, "ADOPT_EXISTING_LIVE_STORY"),
            end_on_shutdown=_boolean(env, "END_LIVE_STORY_ON_SHUTDOWN"),
            cookies_file=(
                Path(env["YOUTUBE_COOKIES_FILE"]) if env.get("YOUTUBE_COOKIES_FILE") else None
            ),
            extractor_args=env.get("YOUTUBE_EXTRACTOR_ARGS", ""),
            web_stream_url=env.get("WEB_STREAM_URL", "http://web:8080/index.m3u8"),
            rtmp_ingest_url=env.get("RTMP_INGEST_URL", "rtmp://ingest:1935/live"),
            rtmp_stream_key=env.get("RTMP_STREAM_KEY", "") if kind == "rtmp" else "",
        )
        if settings.width % 2 or settings.height % 2:
            raise FatalError("VIDEO_WIDTH and VIDEO_HEIGHT must be even")
        if settings.retry_initial > settings.retry_max:
            raise FatalError("RETRY_INITIAL cannot exceed RETRY_MAX")
        if settings.kind == "rtmp":
            # The publisher pushes to the ingest sidecar; SOURCE is not used.
            ingest = urlsplit(settings.rtmp_ingest_url)
            if ingest.scheme != "rtmp" or not ingest.hostname or ingest.query or ingest.username:
                raise FatalError("RTMP_INGEST_URL must be a plain rtmp:// URL without credentials")
            if require_stream:
                stream_key(env)
                if not settings.channel:
                    raise FatalError("TELEGRAM_CHANNEL is required")
            return settings
        if require_stream and (not settings.channel or not settings.source):
            raise FatalError("TELEGRAM_CHANNEL and SOURCE are required")
        if require_stream and settings.kind != "file":
            parsed = urlsplit(settings.source)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise FatalError("Network sources must be HTTP or HTTPS URLs")
        if settings.kind == "web":
            stream = urlsplit(settings.web_stream_url)
            if stream.scheme not in {"http", "https"} or not stream.hostname:
                raise FatalError("WEB_STREAM_URL must be an HTTP or HTTPS HLS endpoint")
        return settings

    @property
    def youtube_cookie_file(self) -> Path | None:
        if self.cookies_file is not None:
            return self.cookies_file
        managed = self.data_dir / "youtube-cookies.txt"
        return managed if managed.is_file() else None

    @property
    def kind(self) -> str:
        if self.source_type != "auto":
            return self.source_type
        host = (urlsplit(self.source).hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            return "youtube"
        return "hls" if urlsplit(self.source).scheme in {"http", "https"} else "file"

    @property
    def rtmp_read_url(self) -> str:
        return f"{self.rtmp_ingest_url}?user=reader&pass={self.rtmp_stream_key}"

    @property
    def source_retry_max(self) -> int:
        # A pushed feed starts whenever the operator goes live: keep polling briskly
        # instead of backing off for minutes while the ingest is idle.
        if self.kind == "rtmp":
            return max(self.retry_initial, min(self.retry_max, 10))
        return self.retry_max

    @property
    def watchdog_timeout(self) -> int:
        # One reconciliation can perform several bounded RPCs plus source resolution.
        return max(180, self.rpc_timeout * 8 + self.source_timeout * 3)
