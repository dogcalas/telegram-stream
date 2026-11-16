"""Browser isolation, render configuration, and HTML/HLS routing regressions."""

import asyncio
import json
from pathlib import Path

import pytest

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError
from telegram_stream.sources import SourceResolver
from telegram_stream.web import WebSettings, capture_command, chromium_command


def test_mobile_capture_does_not_crop_chromes_minimum_window() -> None:
    settings = WebSettings("https://example.invalid", width=720, height=1280)
    assert (settings.capture_width, settings.capture_height) == (1080, 1920)
    chrome = chromium_command(settings)
    assert "--window-size=540,960" in chrome
    assert "--force-device-scale-factor=2" in chrome
    assert "--remote-debugging-address=127.0.0.1" in chrome
    assert "--mute-audio" in chrome
    assert not any("telegram" in argument for argument in chrome)


def test_capture_is_silent_hls_not_a_second_telegram_publisher(tmp_path: Path) -> None:
    command = capture_command(WebSettings("https://example.invalid"), tmp_path)
    assert command[command.index("-f") + 1] == "x11grab"
    assert "-re" not in command
    assert "-an" in command
    assert "epoch_us" in command
    assert command[-1] == str(tmp_path / "index.m3u8")
    assert not any(argument.startswith(("rtmp://", "rtmps://")) for argument in command)


def test_web_source_probes_rendered_hls_instead_of_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        commands: list[list[str]] = []

        async def capture(command: list[str], timeout: float) -> bytes:
            commands.append(command)
            return json.dumps({"streams": [{"codec_type": "video"}]}).encode()

        monkeypatch.setattr("telegram_stream.sources.capture", capture)
        settings = Settings(123, "a" * 32, source_type="web", source="https://example.invalid")
        resolved = await SourceResolver(settings).resolve()
        assert resolved.inputs[0].location == settings.web_stream_url
        assert resolved.audio_index is None  # Main FFmpeg supplies silent AAC.
        assert not resolved.local
        assert settings.source not in commands[0]
        assert commands[0][-1] == settings.web_stream_url

    asyncio.run(scenario())


def test_browser_settings_require_a_real_http_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGE_URL", "file:///data/telegram.session")
    with pytest.raises(FatalError, match="PAGE_URL"):
        WebSettings.from_env()
    monkeypatch.setenv("PAGE_URL", "https://example.invalid")
    monkeypatch.setenv("WEB_WIDTH", "721")
    with pytest.raises(FatalError, match="even"):
        WebSettings.from_env()


def test_web_mode_does_not_change_auto_detection() -> None:
    env = {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "a" * 32,
        "TELEGRAM_CHANNEL": "@test",
        "SOURCE_TYPE": "web",
        "SOURCE": "https://example.invalid",
    }
    assert Settings.from_env(env).kind == "web"
    env["WEB_STREAM_URL"] = "file:///data/telegram.session"
    with pytest.raises(FatalError, match="WEB_STREAM_URL"):
        Settings.from_env(env)
    assert Settings(123, "a" * 32, source="https://example.invalid").kind == "hls"
