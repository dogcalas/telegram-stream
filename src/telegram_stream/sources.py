"""Resolve fresh input URLs and inspect media without blocking the event loop."""

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError, TemporaryError
from telegram_stream.logging import redactor
from telegram_stream.process import capture


@dataclass(frozen=True)
class MediaInput:
    location: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    def options(self, timeout: int, *, reconnect: bool = False) -> list[str]:
        if urlsplit(self.location).scheme not in {"http", "https"}:
            return []
        options = ["-rw_timeout", str(timeout * 1_000_000)]
        if reconnect:
            options += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_on_network_error",
                "1",
                "-reconnect_on_http_error",
                "5xx",
                "-reconnect_delay_max",
                "5",
            ]
        if self.headers:
            for key, value in self.headers.items():
                if not re.fullmatch(r"[A-Za-z0-9-]+", key) or "\r" in value or "\n" in value:
                    raise FatalError("Extractor returned unsafe HTTP headers")
            options += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())]
        return options


@dataclass(frozen=True)
class ResolvedSource:
    inputs: tuple[MediaInput, ...]
    video_index: int
    audio_index: int | None
    local: bool = False


class SourceResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _youtube(self) -> tuple[MediaInput, ...]:
        command = [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--no-cache-dir",
            "--skip-download",
            "--dump-single-json",
            "--js-runtimes",
            "deno",
            "--format",
            "best[protocol^=m3u8]/best[vcodec!=none][acodec!=none]/bestvideo+bestaudio",
        ]
        if self.settings.extractor_args:
            command += ["--extractor-args", self.settings.extractor_args]
        # yt-dlp may update its cookie jar. Never write back to a mounted secret.
        with tempfile.TemporaryDirectory(prefix="telegram-stream-") as directory:
            if self.settings.cookies_file is not None:
                if not self.settings.cookies_file.is_file():
                    raise FatalError("YOUTUBE_COOKIES_FILE is not a readable file")
                cookies = Path(directory) / "cookies.txt"
                shutil.copyfile(self.settings.cookies_file, cookies)
                cookies.chmod(0o600)
                command += ["--cookies", str(cookies)]
            output = await capture(
                [*command, "--", self.settings.source], self.settings.source_timeout
            )
        try:
            info = json.loads(output)
            if info.get("live_status") != "is_live":
                raise TemporaryError("YouTube source is not currently live")
            formats = info.get("requested_formats") or [info]
            inputs = []
            for media in formats:
                location = media["url"]
                if urlsplit(location).scheme not in {"http", "https"}:
                    raise TemporaryError("YouTube returned an unsupported media protocol")
                headers = {**info.get("http_headers", {}), **media.get("http_headers", {})}
                inputs.append(MediaInput(location, headers))
            if not 1 <= len(inputs) <= 2:
                raise TemporaryError("Unsupported YouTube format selection")
            return tuple(inputs)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TemporaryError("Invalid yt-dlp media metadata") from None

    async def resolve(self) -> ResolvedSource:
        local = self.settings.kind == "file"
        if self.settings.kind == "youtube":
            inputs = await self._youtube()
        elif local:
            path = Path(self.settings.source).expanduser().resolve()
            if not path.is_file():
                raise TemporaryError("Local source file is not available")
            inputs = (MediaInput(str(path)),)
        elif self.settings.kind == "web":
            # The isolated browser sidecar renders HTML into a private HLS feed.
            # Never give the HTML page URL directly to ffprobe/FFmpeg.
            inputs = (MediaInput(self.settings.web_stream_url),)
        else:
            inputs = (MediaInput(self.settings.source),)
        video_index = None
        audio_index = None
        for index, media in enumerate(inputs):
            # URLs are redacted generically; do not retain every expiring URL forever.
            redactor.add(*media.headers.values())
            output = await capture(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    *media.options(self.settings.rpc_timeout),
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    "-i",
                    media.location,
                ],
                self.settings.source_timeout,
            )
            try:
                streams = {stream["codec_type"] for stream in json.loads(output)["streams"]}
            except (ValueError, KeyError, TypeError):
                raise TemporaryError("Invalid ffprobe media metadata") from None
            if "video" in streams and video_index is None:
                video_index = index
            if "audio" in streams and audio_index is None:
                audio_index = index
        if video_index is None:
            raise TemporaryError("Source has no video stream")
        return ResolvedSource(inputs, video_index, audio_index, local)
