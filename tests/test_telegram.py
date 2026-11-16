"""Verify the actual generated schema and the adapter's outbound requests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

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
