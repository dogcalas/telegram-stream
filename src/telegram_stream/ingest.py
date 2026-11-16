"""Credential-scoped RTMP ingest sidecar: one authenticated publisher, one private feed."""

import json
import logging
import os
from pathlib import Path

from telegram_stream.config import stream_key
from telegram_stream.errors import FatalError
from telegram_stream.logging import configure, event, redactor

logger = logging.getLogger(__name__)
SERVER = "/usr/local/bin/mediamtx"
CONFIGURATION = Path("/tmp/mediamtx.yml")
PATH = "live"


def configuration(key: str) -> dict[str, object]:
    """Only RTMP is enabled; every other listener and the control API stay off."""

    def user(name: str, action: str) -> dict[str, object]:
        permissions = [{"action": action, "path": PATH}]
        return {"user": name, "pass": key, "ips": [], "permissions": permissions}

    return {
        "logLevel": "info",
        "logDestinations": ["stdout"],
        "authMethod": "internal",
        "authInternalUsers": [user("publisher", "publish"), user("reader", "read")],
        "rtmp": True,
        "rtmpAddress": ":1935",
        "rtsp": False,
        "hls": False,
        "webrtc": False,
        "srt": False,
        "api": False,
        "metrics": False,
        "pprof": False,
        "playback": False,
        "paths": {PATH: {"source": "publisher"}},
    }


def main() -> None:
    os.umask(0o077)
    configure()
    try:
        key = stream_key(os.environ)
    except FatalError as error:
        event(logger, "rtmp_ingest_failed", reason=str(error))
        raise SystemExit(1) from None
    redactor.add(key)
    # JSON is valid YAML, which avoids quoting mistakes around the secret.
    CONFIGURATION.write_text(json.dumps(configuration(key)))
    event(logger, "rtmp_ingest_starting", path=PATH)
    # Replace this process so the server receives container signals directly.
    os.execv(SERVER, [SERVER, str(CONFIGURATION)])


if __name__ == "__main__":
    main()
