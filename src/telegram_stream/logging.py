"""JSON logging without credentials, subprocess command lines, or URL tokens."""

import json
import logging
import re
from datetime import UTC, datetime


class Redactor:
    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def add(self, *values: str) -> None:
        self.secrets.update(value for value in values if value)

    def clean(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return re.sub(r"(?i)(?:https?|rtmps?)://[^\s]+", "[url]", text)


redactor = Redactor()


class JsonFormatter(logging.Formatter):
    def _clean(self, value: object) -> object:
        if isinstance(value, str):
            return redactor.clean(value)
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(item) for item in value]
        return value

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            **fields,
        }
        # Do not format exc_info: dependency exceptions can contain complete credentials.
        return json.dumps(self._clean(payload), default=str)


def configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def event(logger: logging.Logger, name: str, **fields: object) -> None:
    logger.info(name, extra={"fields": fields})
