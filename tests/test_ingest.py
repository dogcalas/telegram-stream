"""RTMP ingest configuration, credential scoping, and source routing regressions."""

import asyncio
import json

import pytest

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError
from telegram_stream.ffmpeg import build_command
from telegram_stream.ingest import configuration
from telegram_stream.sources import MediaInput, ResolvedSource, SourceResolver

KEY = "k" * 32
ENV = {
    "TELEGRAM_API_ID": "123",
    "TELEGRAM_API_HASH": "a" * 32,
    "TELEGRAM_CHANNEL": "@channel",
    "SOURCE_TYPE": "rtmp",
    "RTMP_STREAM_KEY": KEY,
}


def test_rtmp_source_needs_a_key_but_no_source_url() -> None:
    settings = Settings.from_env(ENV)
    assert settings.kind == "rtmp"
    assert settings.rtmp_read_url == f"rtmp://ingest:1935/live?user=reader&pass={KEY}"
    assert KEY not in repr(settings)
    for key in ("", "short", "k" * 129, "has space and symbols!!", "k" * 15 + "&"):
        with pytest.raises(FatalError, match="RTMP_STREAM_KEY"):
            Settings.from_env({**ENV, "RTMP_STREAM_KEY": key})
    # Authentication must not depend on stream-only settings.
    Settings.from_env({**ENV, "RTMP_STREAM_KEY": ""}, require_stream=False)


def test_ingest_url_cannot_carry_credentials_or_other_protocols() -> None:
    for url in ("http://ingest/live", "rtmp://user:pass@ingest/live", "rtmp://ingest/live?a=b"):
        with pytest.raises(FatalError, match="RTMP_INGEST_URL"):
            Settings.from_env({**ENV, "RTMP_INGEST_URL": url})


def test_idle_ingest_is_polled_without_long_backoff() -> None:
    assert Settings.from_env(ENV).source_retry_max == 10
    assert Settings.from_env({**ENV, "RETRY_MAX": "4"}).source_retry_max == 4
    assert Settings(123, "a" * 32, source_type="hls").source_retry_max == 120


def test_ingest_enables_only_authenticated_rtmp() -> None:
    config = configuration(KEY)
    json.dumps(config)
    assert config["rtmp"] is True
    for listener in ("rtsp", "hls", "webrtc", "srt", "api", "metrics", "pprof", "playback"):
        assert config[listener] is False
    users = config["authInternalUsers"]
    assert isinstance(users, list)
    assert [(user["user"], user["permissions"]) for user in users] == [
        ("publisher", [{"action": "publish", "path": "live"}]),
        ("reader", [{"action": "read", "path": "live"}]),
    ]
    assert all(user["pass"] == KEY for user in users)
    assert not any(user["user"] == "any" for user in users)


def test_rtmp_input_times_out_without_http_reconnect_options() -> None:
    settings = Settings.from_env(ENV)
    media = MediaInput(settings.rtmp_read_url)
    assert media.options(15, reconnect=True) == ["-rw_timeout", "15000000"]
    command = build_command(settings, ResolvedSource((media,), 0, 0), "rtmp://example.invalid/key")
    assert "-reconnect" not in command
    assert "-re" not in command


def test_rtmp_source_probes_the_ingest_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        commands: list[list[str]] = []

        async def fake_capture(command: list[str], timeout: float, **_: object) -> bytes:
            commands.append(command)
            return json.dumps({"streams": [{"codec_type": "video"}]}).encode()

        monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
        settings = Settings.from_env(ENV)
        source = await SourceResolver(settings).resolve()
        assert [media.location for media in source.inputs] == [settings.rtmp_read_url]
        assert (source.video_index, source.audio_index, source.local) == (0, None, False)
        assert commands[0][0] == "ffprobe"
        assert commands[0][-1] == settings.rtmp_read_url

    asyncio.run(scenario())
