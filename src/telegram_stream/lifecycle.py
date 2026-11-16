"""Durable, serialized reconciliation independent of the media process."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

from telegram_stream.errors import AlreadyLiveError, CreationEndedError, FatalError, TemporaryError
from telegram_stream.state import StateStore, StoryState
from telegram_stream.telegram import LiveStory, RtmpCredentials


class Gateway(Protocol):
    channel_id: int

    async def current(self) -> LiveStory | None: ...

    async def credentials(self) -> RtmpCredentials: ...

    async def start(self, random_id: int) -> LiveStory: ...


class Reconciler:
    def __init__(
        self, gateway: Gateway, store: StateStore, *, adopt_existing: bool = False
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.state = store.load(gateway.channel_id)
        self.adopt_existing = adopt_existing

    def _save_live(self, story: LiveStory) -> None:
        if not story.rtmp:
            raise FatalError("The active Live Story is not in RTMP mode; refusing to replace it")
        self.state = StoryState(
            channel_id=self.gateway.channel_id,
            random_id=self.state.random_id,
            story_id=story.story_id,
            call_id=story.call_id,
            call_access_hash=story.access_hash,
        )
        self.store.save(self.state)

    async def ensure(
        self, on_absent: Callable[[], Awaitable[None]]
    ) -> tuple[LiveStory, RtmpCredentials]:
        current = await self.gateway.current()
        if current is not None:
            known = current.call_id == self.state.call_id or current.story_id == self.state.story_id
            if not known and not self.adopt_existing:
                if self.state.random_id is None or self.state.call_id is not None:
                    raise FatalError(
                        "Unowned Live Story is active; stop it or explicitly enable adoption"
                    )
                # A previous creation may have succeeded with a lost response. Retry ONLY
                # its durable random_id, never a newly generated one.
                try:
                    confirmed = await self.gateway.start(self.state.random_id)
                except AlreadyLiveError:
                    raise FatalError(
                        "Creation outcome is ambiguous; verify the active story and enable adoption"
                    ) from None
                if confirmed.call_id != current.call_id:
                    raise TemporaryError("Live Story changed during reconciliation")
                current = confirmed
            self._save_live(current)
            return current, await self.gateway.credentials()

        # Only a successful, conclusive server query reaches this branch.
        await on_absent()
        if self.state.call_id is not None:
            self.state = StoryState(self.gateway.channel_id)
            self.store.save(self.state)
        if self.state.random_id is None:
            self.state = StoryState(self.gateway.channel_id, secrets.randbits(63))
            self.store.save(self.state)
        credentials = await self.gateway.credentials()
        random_id = self.state.random_id
        assert random_id is not None  # Persisted above, before any creation request.
        try:
            story = await self.gateway.start(random_id)
        except CreationEndedError:
            # Telegram has positively identified the old generation as finished.
            self.state = StoryState(self.gateway.channel_id)
            self.store.save(self.state)
            raise
        except AlreadyLiveError:
            # Another admin raced our query. Do not rotate credentials or take over.
            raise TemporaryError(
                "A Live Story appeared during creation; reconciling again"
            ) from None
        self._save_live(story)
        return story, credentials
