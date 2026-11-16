"""Small, atomic durable state and a single-writer lock (Linux/macOS)."""

import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from telegram_stream.errors import FatalError


def atomic_json(path: Path, value: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class InstanceLock:
    """Also used by authentication, so the SQLite session has only one writer."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.handle: IO[str] | None = None

    def __enter__(self) -> Self:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = (self.directory / "service.lock").open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise FatalError("Another service or authentication process owns DATA_DIR") from None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.handle is not None:
            self.handle.close()


@dataclass(frozen=True)
class StoryState:
    channel_id: int
    random_id: int | None = None
    story_id: int | None = None
    call_id: int | None = None
    call_access_hash: int | None = None


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "story.json"

    def load(self, channel_id: int) -> StoryState:
        if not self.path.exists():
            return StoryState(channel_id)
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict) or raw.pop("version", None) != 1:
                raise ValueError
            for key, value in raw.items():
                if key not in StoryState.__dataclass_fields__:
                    raise ValueError
                if value is not None and type(value) is not int:
                    raise ValueError
            state = StoryState(**raw)
            if state.channel_id != channel_id:
                raise FatalError("DATA_DIR belongs to another channel; use a separate volume")
            if (state.call_id is None) != (state.call_access_hash is None):
                raise ValueError
            if state.call_id is not None and state.story_id is None:
                raise ValueError
            return state
        except (ValueError, TypeError, KeyError):
            raise FatalError(
                "Invalid durable story state; restore a backup before running"
            ) from None

    def save(self, state: StoryState) -> None:
        atomic_json(self.path, {"version": 1, **asdict(state)})
