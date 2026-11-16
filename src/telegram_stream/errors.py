"""Application errors with deliberately credential-free messages."""


class FatalError(Exception):
    """Requires operator intervention, rather than automatic retries."""


class TelegramRejectedError(FatalError):
    """A permanent RPC rejection, without request arguments or credentials."""

    def __init__(self, identifier: str) -> None:
        super().__init__(f"Telegram rejected the operation: {identifier}")
        self.identifier = identifier


class TemporaryError(Exception):
    """A bounded operation failed and can be retried."""


class AlreadyLiveError(TemporaryError):
    """Telegram reports an existing live story."""

    def __init__(self, story_id: int) -> None:
        super().__init__("Telegram already has an active Live Story")
        self.story_id = story_id


class CreationEndedError(TemporaryError):
    """An idempotent creation replay identifies a story which has already ended."""


class RetryAfter(TemporaryError):
    """Telegram's mandatory minimum retry delay."""

    def __init__(self, seconds: float) -> None:
        super().__init__("Telegram requested a retry delay")
        self.seconds = seconds
