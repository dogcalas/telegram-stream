"""MTProto boundary. Live Stories are never replaced with channel livestreams."""

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from typing import NoReturn

from telethon import TelegramClient, errors, events, functions, types
from telethon.tl import alltlobjects
from telethon.tl.tlobject import TLObject, TLRequest

from telegram_stream.config import Settings
from telegram_stream.errors import (
    AlreadyLiveError,
    CreationEndedError,
    FatalError,
    RetryAfter,
    TemporaryError,
)
from telegram_stream.logging import redactor


@dataclass(frozen=True)
class LiveStory:
    story_id: int
    call_id: int
    access_hash: int = field(repr=False)
    rtmp: bool = True


@dataclass(frozen=True)
class RtmpCredentials:
    url: str = field(repr=False)
    key: str = field(repr=False)

    @property
    def destination(self) -> str:
        return f"{self.url.rstrip('/')}/{self.key}"


def verify_schema() -> None:
    required = (
        (functions.stories.StartLiveRequest, "rtmp_stream"),
        (functions.phone.GetGroupCallStreamRtmpUrlRequest, "live_story"),
        (types.UpdateGroupCall, "live_story"),
    )
    if alltlobjects.LAYER < 229 or any(
        flag not in inspect.signature(constructor).parameters for constructor, flag in required
    ):
        raise FatalError("Telethon must support Live Stories: install the locked layer-229 build")


def _story(item: object) -> LiveStory | None:
    if isinstance(item, types.StoryItem) and isinstance(item.media, types.MessageMediaVideoStream):
        call = item.media.call
        if not isinstance(call, types.InputGroupCall):
            raise TemporaryError("Live Story has an unexpected call reference")
        return LiveStory(item.id, call.id, call.access_hash, bool(item.media.rtmp_stream))
    return None


def _raise_rpc_error(error: errors.RPCError) -> NoReturn:
    seconds = getattr(error, "seconds", None)
    if error.code == 420 and isinstance(seconds, (int, float)):
        raise RetryAfter(max(1, seconds)) from None
    message = getattr(error, "message", "")
    # Generated Telethon subclasses inherit category messages such as BAD_REQUEST,
    # not their specific RPC names. Never inspect str(error), which embeds requests.
    if type(error).__module__ == "telethon.errors.rpcerrorlist":
        message = re.sub(
            r"(?<!^)(?=[A-Z])", "_", type(error).__name__.removesuffix("Error")
        ).upper()
    match = re.fullmatch(r"STORY_LIVE_ALREADY_(\d+)", message)
    if match:
        raise AlreadyLiveError(int(match[1])) from None
    if error.code in {401, 403} or (
        error.code == 400 and message not in {"GROUPCALL_INVALID", "GROUPCALL_FORBIDDEN"}
    ):
        identifier = re.sub(r"[^A-Z0-9_]", "", message)[:100]
        raise FatalError(f"Telegram rejected the operation: {identifier}") from None
    raise TemporaryError("Telegram RPC temporarily failed") from None


class Telegram:
    def __init__(self, settings: Settings) -> None:
        verify_schema()
        self.settings = settings
        self.client = TelegramClient(
            str(settings.data_dir / "telegram"),
            settings.api_id,
            settings.api_hash,
            request_retries=0,
            connection_retries=3,
            retry_delay=1,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            timeout=settings.rpc_timeout,
        )
        self.peer: types.InputPeerChannel | None = None
        self.channel_id = 0
        self.changed = asyncio.Event()
        self._call_id: int | None = None
        self.client.add_event_handler(self._on_update, events.Raw())
        redactor.add(settings.api_hash, settings.source, settings.extractor_args)

    async def _on_update(self, update: object) -> None:
        if isinstance(update, types.UpdateGroupCall):
            peer_matches = (
                isinstance(update.peer, types.PeerChannel)
                and update.peer.channel_id == self.channel_id
                and update.live_story
            )
            if peer_matches or update.call.id == self._call_id:
                self.changed.set()
        elif (
            isinstance(update, types.UpdateStory)
            and isinstance(update.peer, types.PeerChannel)
            and update.peer.channel_id == self.channel_id
        ):
            self.changed.set()

    @property
    def input_peer(self) -> types.InputPeerChannel:
        if self.peer is None:
            raise FatalError("Telegram is not connected to a channel")
        return self.peer

    async def _request(self, request: TLRequest) -> TLObject:
        try:
            async with asyncio.timeout(self.settings.rpc_timeout):
                result = await self.client(request)
                if not isinstance(result, TLObject):
                    raise TemporaryError("Telegram returned an unexpected response")
                return result
        except errors.RPCError as error:
            _raise_rpc_error(error)

    async def connect(self) -> None:
        try:
            await self._connect()
        except errors.RPCError as error:
            _raise_rpc_error(error)

    async def _connect(self) -> None:
        async with asyncio.timeout(self.settings.rpc_timeout * 2):
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise FatalError("Session is unauthorized; stop the service and run auth")
            user = await self.client.get_me()
            if not isinstance(user, types.User) or user.bot:
                raise FatalError("A Telegram user account, not a bot account, is required")
            channel: str | int = self.settings.channel
            if channel.lstrip("-").isdigit():
                channel = int(channel)
            try:
                peer = await self.client.get_input_entity(channel)
            except ValueError:
                # Numeric channel IDs need an access_hash cached from joined dialogs.
                await self.client.get_dialogs()
                try:
                    peer = await self.client.get_input_entity(channel)
                except ValueError:
                    raise FatalError(
                        "Channel not found; join it with the authenticated user"
                    ) from None
            if not isinstance(peer, types.InputPeerChannel):
                raise FatalError("TELEGRAM_CHANNEL must identify a channel")
            self.peer = peer
            self.channel_id = peer.channel_id
        result = await self._request(
            functions.channels.GetChannelsRequest(
                id=[types.InputChannel(peer.channel_id, peer.access_hash)]
            )
        )
        if not isinstance(result, (types.messages.Chats, types.messages.ChatsSlice)):
            raise TemporaryError("Telegram returned unexpected channel information")
        if not result.chats:
            raise FatalError("Channel is not accessible")
        channel_entity = result.chats[0]
        if not isinstance(channel_entity, types.Channel) or not channel_entity.broadcast:
            raise FatalError("This MVP requires a broadcast channel, not a group")
        rights = channel_entity.admin_rights
        if not channel_entity.creator and (rights is None or not rights.post_stories):
            raise FatalError("The user needs channel administrator post_stories rights")

    async def current(self) -> LiveStory | None:
        response = await self._request(
            functions.stories.GetPeerStoriesRequest(peer=self.input_peer)
        )
        if not isinstance(response, types.stories.PeerStories):
            raise TemporaryError("Telegram returned unexpected story information")
        items = response.stories.stories
        skipped = [item.id for item in items if isinstance(item, types.StoryItemSkipped)]
        if skipped:
            full = await self._request(
                functions.stories.GetStoriesByIDRequest(peer=self.input_peer, id=skipped)
            )
            if not isinstance(full, types.stories.Stories):
                raise TemporaryError("Telegram returned unexpected story details")
            if any(isinstance(item, types.StoryItemSkipped) for item in full.stories):
                raise TemporaryError("Cannot conclusively inspect channel stories")
            if len(full.stories) != len(skipped):
                raise TemporaryError("Channel stories changed while being inspected")
            items = [item for item in items if not isinstance(item, types.StoryItemSkipped)]
            items.extend(
                item for item in full.stories if not isinstance(item, types.StoryItemSkipped)
            )
        live = [story for item in items if (story := _story(item)) is not None]
        if len(live) > 1:
            raise FatalError("Telegram returned multiple active Live Stories; refusing to publish")
        if not live:
            return None
        story = live[0]
        self._call_id = story.call_id
        call = await self._request(
            functions.phone.GetGroupCallRequest(
                call=types.InputGroupCall(story.call_id, story.access_hash), limit=1
            )
        )
        if not isinstance(call, types.phone.GroupCall):
            raise TemporaryError("Telegram returned unexpected call information")
        if isinstance(call.call, types.GroupCallDiscarded):
            return None
        if not isinstance(call.call, types.GroupCall):
            raise TemporaryError("Cannot determine Live Story call state")
        return LiveStory(
            story.story_id,
            story.call_id,
            story.access_hash,
            story.rtmp and bool(call.call.rtmp_stream),
        )

    async def credentials(self) -> RtmpCredentials:
        result = await self._request(
            functions.phone.GetGroupCallStreamRtmpUrlRequest(
                peer=self.input_peer, revoke=False, live_story=True
            )
        )
        if not isinstance(result, types.phone.GroupCallStreamRtmpUrl):
            raise TemporaryError("Telegram returned unexpected publishing information")
        if not result.url.startswith(("rtmp://", "rtmps://")) or not result.key:
            raise FatalError("Telegram returned invalid RTMP credentials")
        redactor.add(result.key, result.url)
        return RtmpCredentials(result.url, result.key)

    async def start(self, random_id: int) -> LiveStory:
        response = await self._request(
            functions.stories.StartLiveRequest(
                peer=self.input_peer,
                privacy_rules=[types.InputPrivacyValueAllowAll()],
                random_id=random_id,
                rtmp_stream=True,
                caption=self.settings.caption or None,
                pinned=False,
                messages_enabled=False,
            )
        )
        updates: list[object] = []
        if isinstance(response, (types.Updates, types.UpdatesCombined)):
            updates.extend(response.updates)
        if isinstance(response, types.UpdateShort):
            updates.append(response.update)
        mapped_ids = {
            update.id
            for update in updates
            if isinstance(update, types.UpdateStoryID) and update.random_id == random_id
        }
        for update in updates:
            if (
                isinstance(update, types.UpdateStory)
                and isinstance(update.peer, types.PeerChannel)
                and update.peer.channel_id == self.input_peer.channel_id
            ):
                story = _story(update.story)
                if story is not None:
                    return story
        # A successful RPC authorizes reconciliation even when UpdatesTooLong is returned.
        current = await self.current()
        if current is None:
            if mapped_ids:
                details = await self._request(
                    functions.stories.GetStoriesByIDRequest(
                        peer=self.input_peer, id=list(mapped_ids)
                    )
                )
                if isinstance(details, types.stories.Stories) and any(
                    isinstance(item, types.StoryItemDeleted)
                    or (
                        isinstance(item, types.StoryItem)
                        and isinstance(item.media, types.MessageMediaDocument)
                    )
                    for item in details.stories
                ):
                    raise CreationEndedError("The previous creation has already ended")
            raise TemporaryError("Created Live Story is not visible yet")
        if mapped_ids and current.story_id not in mapped_ids:
            raise FatalError(
                "Creation replay does not identify the active story; refusing takeover"
            )
        return current

    async def discard(self, story: LiveStory) -> None:
        await self._request(
            functions.phone.DiscardGroupCallRequest(
                call=types.InputGroupCall(story.call_id, story.access_hash)
            )
        )

    async def close(self) -> None:
        async with asyncio.timeout(self.settings.shutdown_timeout):
            await self.client.disconnect()
