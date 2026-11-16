"""Single-owner supervision with separate Telegram and source retry schedules."""

import asyncio
import logging
import time

from telegram_stream.config import Settings
from telegram_stream.errors import FatalError, RetryAfter, TemporaryError
from telegram_stream.ffmpeg import FFmpeg
from telegram_stream.lifecycle import Reconciler
from telegram_stream.logging import event
from telegram_stream.retry import Backoff
from telegram_stream.sources import SourceResolver
from telegram_stream.state import StateStore, atomic_json
from telegram_stream.telegram import LiveStory, RtmpCredentials, Telegram

logger = logging.getLogger(__name__)
RETRYABLE = (TemporaryError, OSError, TimeoutError, ConnectionError)


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.telegram = Telegram(settings)
        self.ffmpeg = FFmpeg(settings)
        self.resolver = SourceResolver(settings)
        self.story: LiveStory | None = None
        self.credentials: RtmpCredentials | None = None
        self.phase = "starting"
        self.last_tick = time.time()
        self.last_tick_monotonic = time.monotonic()
        self.last_telegram_success = 0.0
        self.telegram_backoff = Backoff(settings.retry_initial, settings.retry_max)
        self.source_backoff = Backoff(settings.retry_initial, settings.retry_max)
        self.next_source_attempt = 0.0

    def _write_health(self) -> None:
        progressing = (
            self.ffmpeg.process is not None
            and self.ffmpeg.process.returncode is None
            and self.ffmpeg.progress_at > 0
            and time.monotonic() - self.ffmpeg.progress_at <= self.settings.stall_timeout
        )
        telegram_fresh = (
            time.time() - self.last_telegram_success
            <= self.settings.poll_interval * 3 + self.settings.rpc_timeout * 2
        )
        atomic_json(
            self.settings.data_dir / "health.json",
            {
                "phase": self.phase,
                "ready": progressing and telegram_fresh and self.story is not None,
                "last_tick": self.last_tick,
                "watchdog_timeout": self.settings.watchdog_timeout,
                "last_telegram_success": self.last_telegram_success or None,
                "media_progress_age": (
                    round(time.monotonic() - self.ffmpeg.progress_at, 2)
                    if self.ffmpeg.progress_at
                    else None
                ),
                "story_id": self.story.story_id if self.story else None,
                "ffmpeg_pid": self.ffmpeg.process.pid if self.ffmpeg.process else None,
            },
        )

    async def _heartbeat(self) -> None:
        while True:
            self._write_health()
            if time.monotonic() - self.last_tick_monotonic > self.settings.watchdog_timeout:
                raise FatalError("Supervisor watchdog expired; exiting for container restart")
            await asyncio.sleep(5)

    def _tick(self) -> None:
        self.last_tick = time.time()
        self.last_tick_monotonic = time.monotonic()

    async def _absent(self) -> None:
        # Stop the old publisher BEFORE creating a replacement story with the same key.
        self.story = None
        self.credentials = None
        await self.ffmpeg.stop()
        self.next_source_attempt = 0
        self.phase = "recovering"

    async def _connect(self) -> None:
        while True:
            self._tick()
            try:
                await self.telegram.connect()
                self.telegram_backoff.reset()
                return
            except RETRYABLE as error:
                delay = self._telegram_delay(error)
                event(logger, "telegram_connect_retry", error=type(error).__name__, delay=delay)
                # Tick while respecting even multi-hour FloodWaits.
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self._tick()
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))

    def _telegram_delay(self, error: Exception) -> float:
        delay = self.telegram_backoff.next_delay()
        return max(delay, error.seconds) if isinstance(error, RetryAfter) else delay

    async def _supervise(self) -> None:
        await self._connect()
        reconciler = Reconciler(
            self.telegram,
            StateStore(self.settings.data_dir),
            adopt_existing=self.settings.adopt_existing,
        )
        next_poll = 0.0
        flood_until = 0.0
        while True:
            self._tick()
            now = time.monotonic()
            if now >= flood_until and (now >= next_poll or self.telegram.changed.is_set()):
                self.telegram.changed.clear()
                try:
                    story, credentials = await reconciler.ensure(self._absent)
                    if self.story is None or self.story.call_id != story.call_id:
                        await self.ffmpeg.stop()
                        self.next_source_attempt = 0
                        event(logger, "live_story_ready", story_id=story.story_id)
                    self.story, self.credentials = story, credentials
                    self.last_telegram_success = time.time()
                    self.telegram_backoff.reset()
                    next_poll = time.monotonic() + self.settings.poll_interval
                except RETRYABLE as error:
                    delay = self._telegram_delay(error)
                    next_poll = time.monotonic() + delay
                    # Update events must not bypass mandatory waits or retry backoff.
                    flood_until = next_poll
                    event(logger, "telegram_retry", error=type(error).__name__, delay=delay)
            try:
                self.ffmpeg.check()
            except TemporaryError as error:
                event(logger, "source_interrupted", reason=str(error))
                await self.ffmpeg.stop()
                delay = self.source_backoff.next_delay()
                self.next_source_attempt = time.monotonic() + delay
                event(logger, "source_retry_scheduled", delay=delay)
            if (
                self.story is not None
                and self.credentials is not None
                and self.ffmpeg.process is None
                and time.monotonic() >= self.next_source_attempt
            ):
                try:
                    source = await self.resolver.resolve()
                    await self.ffmpeg.start(source, self.credentials.destination)
                except RETRYABLE as error:
                    delay = self.source_backoff.next_delay()
                    self.next_source_attempt = time.monotonic() + delay
                    event(logger, "source_retry", reason=str(error), delay=delay)
            if (
                self.ffmpeg.process is not None
                and self.ffmpeg.progress_at
                and time.monotonic() - self.ffmpeg.started_at >= 60
                and time.monotonic() - self.ffmpeg.progress_at < self.settings.stall_timeout
            ):
                self.source_backoff.reset()
            self.phase = "running" if self.ffmpeg.process is not None else "recovering"
            await asyncio.sleep(1)

    async def run(self) -> None:
        self._write_health()
        failed = False
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._heartbeat())
                tasks.create_task(self._supervise())
        except asyncio.CancelledError:
            raise
        except BaseException:
            failed = True
            raise
        finally:
            await self.ffmpeg.stop()
            if not failed and self.settings.end_on_shutdown and self.story is not None:
                try:
                    # Never discard a call which has been replaced by another admin.
                    current = await self.telegram.current()
                    if current is not None and current.call_id == self.story.call_id:
                        await self.telegram.discard(current)
                except (FatalError, *RETRYABLE) as error:
                    event(logger, "shutdown_discard_failed", error=type(error).__name__)
            try:
                await self.telegram.close()
            except (OSError, TimeoutError):
                event(logger, "telegram_disconnect_timeout")
            self.phase = "fatal" if failed else "stopped"
            self._tick()
            self._write_health()
