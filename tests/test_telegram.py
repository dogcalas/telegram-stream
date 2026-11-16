"""Verify the actual generated schema and the adapter's outbound requests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types
from telethon.tl.tlobject import TLObject, TLRequest

from telegram_stream.config import Settings
from telegram_stream.errors import AlreadyLiveError, FatalError, RetryAfter, TemporaryError
from telegram_stream.telegram import Telegram, _raise_rpc_error, verify_schema


def test_pinned_schema_serializes_live_story_flags() -> None:
    verify_schema()
    credentials = functions.phone.GetGroupCallStreamRtmpUrlRequest(
        peer=types.InputPeerChannel(42, 43), revoke=False, live_story=True
    )
    start = functions.stories.StartLiveRequest(
        peer=types.InputPeerChannel(42, 43),
        privacy_rules=[types.InputPrivacyValueAllowAll()],
        random_id=123,
        rtmp_stream=True,
    )
    assert int.from_bytes(bytes(credentials)[4:8], "little") & 1
    assert int.from_bytes(bytes(start)[4:8], "little") & (1 << 5)


def test_adapter_uses_story_creation_and_story_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        gateway = Telegram(Settings(123, "a" * 32, data_dir=tmp_path))
        gateway.peer = types.InputPeerChannel(42, 43)
        item = types.StoryItem(
            id=7,
            date=None,
            expire_date=None,
            media=types.MessageMediaVideoStream(types.InputGroupCall(70, 700), rtmp_stream=True),
        )
        request = AsyncMock(
            side_effect=[
                types.phone.GroupCallStreamRtmpUrl("rtmp://example.invalid/live", "key"),
                types.Updates([types.UpdateStory(types.PeerChannel(42), item)], [], [], None, 0),
            ]
        )
        monkeypatch.setattr(gateway, "_request", request)
        try:
            await gateway.credentials()
            story = await gateway.start(123)
            assert story.story_id == 7
            credential_request = request.call_args_list[0].args[0]
            assert isinstance(credential_request, functions.phone.GetGroupCallStreamRtmpUrlRequest)
            assert credential_request.live_story and not credential_request.revoke
            start_request = request.call_args_list[1].args[0]
            assert isinstance(start_request, functions.stories.StartLiveRequest)
            assert start_request.rtmp_stream and start_request.random_id == 123
        finally:
            await gateway.close()

    asyncio.run(scenario())


def test_unknown_already_live_rpc_is_classified() -> None:
    with pytest.raises(AlreadyLiveError) as caught:
        _raise_rpc_error(errors.RPCError(None, "STORY_LIVE_ALREADY_123", 400))
    assert caught.value.story_id == 123


def test_caption_rejection_retries_same_id_without_caption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        gateway = Telegram(Settings(123, "a" * 32, data_dir=tmp_path, caption="Live"))
        gateway.peer = types.InputPeerChannel(42, 43)
        attempts: list[tuple[int, str | None, bool | None]] = []
        item = types.StoryItem(
            id=7,
            date=None,
            expire_date=None,
            media=types.MessageMediaVideoStream(types.InputGroupCall(70, 700), rtmp_stream=True),
        )

        async def reject_caption_once(request: TLRequest) -> TLObject:
            assert isinstance(request, functions.stories.StartLiveRequest)
            attempts.append((request.random_id, request.caption, request.rtmp_stream))
            if len(attempts) == 1:
                _raise_rpc_error(errors.RPCError(None, "CAPTION_NOT_EMPTY", 400))
            assert not int.from_bytes(bytes(request)[4:8], "little") & 1
            return types.Updates([types.UpdateStory(types.PeerChannel(42), item)], [], [], None, 0)

        monkeypatch.setattr(gateway, "_request", reject_caption_once)
        try:
            assert (await gateway.start(123)).story_id == 7
            assert (await gateway.start(124)).story_id == 7
            assert attempts == [(123, "Live", True), (123, None, True), (124, None, True)]
        finally:
            await gateway.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("caption", "rpc_error"),
    [("", "CAPTION_NOT_EMPTY"), ("Live", "CAPTION_TOO_LONG"), ("Live", "CHAT_ADMIN_REQUIRED")],
)
def test_caption_fallback_does_not_hide_other_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caption: str, rpc_error: str
) -> None:
    async def scenario() -> None:
        gateway = Telegram(Settings(123, "a" * 32, data_dir=tmp_path, caption=caption))
        gateway.peer = types.InputPeerChannel(42, 43)
        attempts = 0

        async def reject(request: TLRequest) -> TLObject:
            nonlocal attempts
            attempts += 1
            _raise_rpc_error(errors.RPCError(None, rpc_error, 400))

        monkeypatch.setattr(gateway, "_request", reject)
        try:
            with pytest.raises(FatalError, match=rpc_error):
                await gateway.start(123)
            assert attempts == 1
        finally:
            await gateway.close()

    asyncio.run(scenario())


def test_floodwait_preserves_server_delay() -> None:
    with pytest.raises(RetryAfter) as caught:
        _raise_rpc_error(errors.FloodWaitError(None, capture=1800))
    assert caught.value.seconds == 1800


def test_generated_rpc_errors_use_specific_names_not_category_messages() -> None:
    with pytest.raises(FatalError, match="CHAT_ADMIN_REQUIRED"):
        _raise_rpc_error(errors.ChatAdminRequiredError(None))
    with pytest.raises(TemporaryError):
        _raise_rpc_error(errors.GroupcallInvalidError(None))


def test_discard_update_requests_reconciliation(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = Telegram(Settings(123, "a" * 32, data_dir=tmp_path))
        gateway.channel_id = 42
        try:
            await gateway._on_update(
                types.UpdateGroupCall(
                    types.GroupCallDiscarded(70, 700, 3600),
                    live_story=True,
                    peer=types.PeerChannel(42),
                )
            )
            assert gateway.changed.is_set()
        finally:
            await gateway.close()

    asyncio.run(scenario())
