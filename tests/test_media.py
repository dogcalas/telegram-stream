import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError, TemporaryError
from telegram_stream.ffmpeg import FFmpeg, build_command
from telegram_stream.process import capture, spawn
from telegram_stream.sources import MediaInput, ResolvedSource, SourceResolver


def test_local_file_loops_and_supplies_silent_audio() -> None:
    settings = Settings(123, "a" * 32)
    source = ResolvedSource((MediaInput("/media/video.mp4"),), 0, None, local=True)
    command = build_command(settings, source, "rtmp://example.invalid/live/key")
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert "-re" in command
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in command
    assert "1:a:0" in command
    assert "-shortest" in command
    assert command[-1] == "rtmp://example.invalid/live/key"


def test_network_inputs_have_scoped_headers_and_no_realtime_throttle() -> None:
    settings = Settings(123, "a" * 32)
    source = ResolvedSource(
        (
            MediaInput("https://example.invalid/video.m3u8", {"User-Agent": "test"}),
            MediaInput("https://example.invalid/audio.m3u8"),
        ),
        0,
        1,
    )
    command = build_command(settings, source, "rtmp://example.invalid/live/key")
    assert "-re" not in command
    assert "-stream_loop" not in command
    assert command.index("-headers") < command.index("https://example.invalid/video.m3u8")
    assert "1:a:0" in command
    with pytest.raises(FatalError, match="unsafe"):
        MediaInput("https://example.invalid", {"Header": "value\r\nInjected: true"}).options(15)


def test_youtube_urls_are_refreshed_on_each_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        resolutions = 0

        async def fake_capture(
            command: list[str],
            timeout: float,
            *,
            diagnostic: Callable[[bytes], str] | None = None,
        ) -> bytes:
            nonlocal resolutions
            if command[0] == "yt-dlp":
                resolutions += 1
                return json.dumps(
                    {
                        "live_status": "is_live",
                        "url": f"https://example.invalid/stream?generation={resolutions}",
                        "http_headers": {"User-Agent": "test"},
                    }
                ).encode()
            return b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'

        monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
        resolver = SourceResolver(Settings(123, "a" * 32, source="https://youtu.be/test"))
        first, second = await resolver.resolve(), await resolver.resolve()
        assert first.inputs[0].location != second.inputs[0].location
        assert second.audio_index == 0

    asyncio.run(scenario())


def test_stall_detection_requires_advancing_media_progress() -> None:
    async def scenario() -> None:
        ffmpeg = FFmpeg(Settings(123, "a" * 32, stall_timeout=5))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
        )
        ffmpeg.process = process
        ffmpeg.started_at = time.monotonic() - 60
        ffmpeg.progress_at = time.monotonic() - 10
        try:
            with pytest.raises(TemporaryError, match="stalled"):
                ffmpeg.check()
        finally:
            await ffmpeg.stop()
        assert process.returncode is not None

    asyncio.run(scenario())


def test_advancing_audio_cannot_mask_frozen_video() -> None:
    async def scenario() -> None:
        ffmpeg = FFmpeg(Settings(123, "a" * 32))
        process = await spawn(
            [
                sys.executable,
                "-c",
                "print('frame=1\\nout_time_us=1000\\nframe=1\\nout_time_us=2000')",
            ]
        )
        ffmpeg.process = process
        try:
            await ffmpeg._progress()
            assert ffmpeg.media_time == 1000
        finally:
            await ffmpeg.stop()

    asyncio.run(scenario())


def test_cancellation_during_spawn_reaps_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        original = asyncio.create_subprocess_exec
        created = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        async def delayed_spawn(
            *command: str, stdout: int, stderr: int, start_new_session: bool, limit: int
        ) -> asyncio.subprocess.Process:
            child = await original(
                *command,
                stdout=stdout,
                stderr=stderr,
                start_new_session=start_new_session,
                limit=limit,
            )
            children.append(child)
            created.set()
            await asyncio.sleep(0.05)
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        task = asyncio.create_task(
            spawn(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                ]
            )
        )
        await created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert children[0].returncode is not None

    asyncio.run(scenario())


def test_timed_out_source_process_is_reaped(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "completed"
        command = [
            sys.executable,
            "-c",
            "import pathlib, time; time.sleep(0.3); pathlib.Path(__import__('sys').argv[1])"
            ".write_text('leaked')",
            str(marker),
        ]
        with pytest.raises(TimeoutError):
            await capture(command, timeout=0.05)
        await asyncio.sleep(0.4)
        assert not marker.exists()

    asyncio.run(scenario())
