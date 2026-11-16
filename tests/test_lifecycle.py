"""Regression tests for ownership and creation ambiguity, without Telegram access."""

import asyncio
from pathlib import Path

import pytest

from telegram_stream.errors import AlreadyLiveError, CreationEndedError, FatalError, TemporaryError
from telegram_stream.lifecycle import Reconciler
from telegram_stream.state import StateStore, StoryState
from telegram_stream.telegram import LiveStory, RtmpCredentials

LIVE = LiveStory(7, 70, 700)


async def absent() -> None:
    pass


class FakeGateway:
    channel_id = 42

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.live: LiveStory | None = None
        self.starts: list[int] = []
        self.lose_response = False
        self.conflict = False
        self.query_error = False
        self.creation_ended = False

    async def current(self) -> LiveStory | None:
        if self.query_error:
            raise TemporaryError("Network unavailable")
        return self.live

    async def credentials(self) -> RtmpCredentials:
        return RtmpCredentials("rtmp://example.invalid/live", "test-key")

    async def start(self, random_id: int) -> LiveStory:
        # A creation request may NEVER happen before its deduplication ID is durable.
        assert self.store.load(self.channel_id).random_id == random_id
        self.starts.append(random_id)
        if self.creation_ended:
            raise CreationEndedError("The previous creation has already ended")
        if self.conflict:
            raise AlreadyLiveError(LIVE.story_id)
        self.live = LIVE
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError
        return self.live


def test_creation_is_durable_and_restart_resumes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        gateway = FakeGateway(store)
        first = Reconciler(gateway, store)
        story, _ = await first.ensure(absent)
        assert story == LIVE
        restarted = Reconciler(gateway, store)
        assert (await restarted.ensure(absent))[0] == LIVE
        assert len(gateway.starts) == 1
        assert store.load(42).call_id == LIVE.call_id

    asyncio.run(scenario())


def test_lost_response_reuses_same_id_across_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        gateway = FakeGateway(store)
        gateway.lose_response = True
        with pytest.raises(TimeoutError):
            await Reconciler(gateway, store).ensure(absent)
        pending = store.load(42).random_id
        assert pending is not None
        assert (await Reconciler(gateway, store).ensure(absent))[0] == LIVE
        assert gateway.starts == [pending, pending]

    asyncio.run(scenario())


def test_terminated_story_stops_publisher_before_recreation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        store.save(StoryState(42, 123, 6, 60, 600))
        gateway = FakeGateway(store)
        stopped = False

        async def stop() -> None:
            nonlocal stopped
            assert not gateway.starts
            stopped = True

        await Reconciler(gateway, store).ensure(stop)
        assert stopped
        assert len(gateway.starts) == 1
        assert gateway.starts[0] != 123

    asyncio.run(scenario())


def test_network_failure_is_not_termination(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        original = StoryState(42, 123, 7, 70, 700)
        store.save(original)
        gateway = FakeGateway(store)
        gateway.query_error = True
        with pytest.raises(TemporaryError):
            await Reconciler(gateway, store).ensure(absent)
        assert store.load(42) == original
        assert not gateway.starts

    asyncio.run(scenario())


def test_ended_creation_replay_clears_only_the_confirmed_old_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        store.save(StoryState(42, 123))
        gateway = FakeGateway(store)
        gateway.creation_ended = True
        with pytest.raises(CreationEndedError):
            await Reconciler(gateway, store).ensure(absent)
        assert store.load(42) == StoryState(42)
        gateway.creation_ended = False
        await Reconciler(gateway, store).ensure(absent)
        assert gateway.starts[0] == 123
        assert gateway.starts[1] != 123

    asyncio.run(scenario())


def test_unknown_active_story_is_not_taken_over(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        gateway = FakeGateway(store)
        gateway.live = LIVE
        with pytest.raises(FatalError, match="Unowned"):
            await Reconciler(gateway, store).ensure(absent)
        assert not gateway.starts

    asyncio.run(scenario())


def test_ambiguous_creation_requires_explicit_adoption(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        store.save(StoryState(42, 123))
        gateway = FakeGateway(store)
        gateway.live, gateway.conflict = LIVE, True
        with pytest.raises(FatalError, match="ambiguous"):
            await Reconciler(gateway, store).ensure(absent)
        assert gateway.starts == [123]
        assert (await Reconciler(gateway, store, adopt_existing=True).ensure(absent))[0] == LIVE
        assert gateway.starts == [123]

    asyncio.run(scenario())


def test_never_adopt_non_rtmp_story(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = StateStore(tmp_path)
        gateway = FakeGateway(store)
        gateway.live = LiveStory(7, 70, 700, rtmp=False)
        with pytest.raises(FatalError, match="not in RTMP"):
            await Reconciler(gateway, store, adopt_existing=True).ensure(absent)
        assert not gateway.starts

    asyncio.run(scenario())
