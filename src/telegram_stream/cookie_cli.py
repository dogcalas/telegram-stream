"""Explicit local capture and stdin-based Docker import; never contacts Telegram."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from telegram_stream.config import Settings, _number, data_directory
from telegram_stream.cookies import (
    BROWSERS,
    COOKIE_FILENAME,
    MAX_COOKIE_BYTES,
    capture_browser,
    normalize_document,
    read_document,
    summary,
    write_private,
)
from telegram_stream.errors import FatalError, TemporaryError
from telegram_stream.logging import redactor


def configure_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="cookie_command", required=True)
    capture = commands.add_parser("capture", help="Capture YouTube cookies from your local browser")
    capture.add_argument("--browser", required=True, choices=BROWSERS)
    capture.add_argument("--profile", help="Dedicated browser profile name or directory")
    capture.add_argument(
        "--yes", action="store_true", help="Explicitly authorize local browser access"
    )
    destination = capture.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path, help="Private Netscape output file")
    destination.add_argument("--install", action="store_true", help="Import into Compose via stdin")
    capture.add_argument("--check-url", help="Test this live URL in Docker before installing")
    importer = commands.add_parser("import", help="Import a Netscape file from stdin or --file")
    importer.add_argument("--file", type=Path)
    importer.add_argument(
        "--check-url", help="Verify the candidate before replacing stored cookies"
    )
    status = commands.add_parser(
        "status", help="Show counts/expiration status, never cookie values"
    )
    status.add_argument("--file", type=Path)
    check = commands.add_parser("check", help="Probe a YouTube live source without publishing it")
    check.add_argument("--url", required=True)
    check.add_argument("--file", type=Path)


def selected_path() -> Path:
    configured = os.environ.get("YOUTUBE_COOKIES_FILE")
    return Path(configured) if configured else data_directory() / COOKIE_FILENAME


def validate_youtube_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
    }:
        raise FatalError("Cookie checks require an HTTPS YouTube/youtu.be URL")


async def check_source(path: Path, url: str) -> None:
    from telegram_stream.sources import SourceResolver

    validate_youtube_url(url)
    normalize_document(read_document(path))
    # This is a media-only probe. No Telegram client/session is constructed.
    settings = Settings(
        api_id=1,
        api_hash="0" * 32,
        source=url,
        source_type="youtube",
        data_dir=data_directory(),
        cookies_file=path,
        extractor_args=os.environ.get("YOUTUBE_EXTRACTOR_ARGS", ""),
        source_timeout=_number(os.environ, "SOURCE_TIMEOUT", 60, 5, 300),
        rpc_timeout=_number(os.environ, "RPC_TIMEOUT", 15, 5, 120),
    )
    redactor.add(url, settings.extractor_args)
    try:
        await SourceResolver(settings).resolve()
    except (TemporaryError, OSError, TimeoutError) as error:
        reason = str(error) if isinstance(error, TemporaryError) else type(error).__name__
        raise FatalError(f"YouTube source check failed: {reason}") from None


def import_document(data: bytes, check_url: str | None = None) -> dict[str, object]:
    normalized = normalize_document(data)
    target = data_directory() / COOKIE_FILENAME
    if selected_path().resolve() != target.resolve():
        raise FatalError(
            "Clear YOUTUBE_COOKIES_FILE to use managed cookies, or update its external file instead"
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if check_url is not None:
        validate_youtube_url(check_url)
        # Keep staged credentials in ephemeral storage, not the persistent volume.
        with tempfile.TemporaryDirectory(prefix="cookie-check-") as directory:
            candidate = Path(directory) / COOKIE_FILENAME
            write_private(candidate, normalized)
            asyncio.run(check_source(candidate, check_url))
    # A failed parse or online check leaves the previous credential file untouched.
    write_private(target, normalized)
    return {"installed": True, **summary(normalized), "online_verified": check_url is not None}


def install_in_compose(data: bytes, check_url: str | None) -> None:
    if not Path("docker-compose.yml").is_file():
        raise FatalError(
            "--install must be run from the project root containing docker-compose.yml"
        )
    name = f"telegram-stream-cookie-import-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "compose",
        "run",
        "--name",
        name,
        "--rm",
        "--no-deps",
        "-T",
        "streamer",
        "cookies",
        "import",
    ]
    if check_url is not None:
        validate_youtube_url(check_url)
        command += ["--check-url", check_url]
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        _stop_helper(name)
        raise FatalError(
            "Docker import could not complete; check Docker and the updated image"
        ) from None
    except KeyboardInterrupt:
        _stop_helper(name)
        raise
    if result.returncode:
        # Return only our structured fatal reasons, never arbitrary container output.
        for line in result.stderr.decode(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "fatal" and isinstance(record.get("reason"), str):
                raise FatalError(record["reason"])
        raise FatalError("Docker import failed; build the updated streamer image and retry")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise FatalError("Docker import returned an unexpected response") from None
    expected = {
        "installed": bool,
        "usable": int,
        "expired_or_empty": int,
        "session_cookies": int,
        "login_cookies_present": bool,
        "online_verified": bool,
    }
    if not isinstance(report, dict) or any(
        key not in report or type(report[key]) is not kind for key, kind in expected.items()
    ):
        raise FatalError("Docker import returned an unexpected summary") from None
    # Whitelist summary fields; never relay arbitrary stdout from a container.
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "installed",
                    "usable",
                    "expired_or_empty",
                    "session_cookies",
                    "login_cookies_present",
                    "online_verified",
                )
                if key in report
            }
        )
    )


def _stop_helper(name: str) -> None:
    # Cancelling the Docker client alone can leave a live import container behind.
    # Stop only the uniquely named temporary container created by this invocation.
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "stop", "--time", "5", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )


def run(args: argparse.Namespace) -> None:
    if args.cookie_command == "capture":
        if args.output is not None and str(args.output) == "-":
            raise FatalError("Cookie contents cannot be exported to stdout; choose a private file")
        if args.install and not Path("docker-compose.yml").is_file():
            raise FatalError("--install must be run from the project root")
        if args.check_url and not args.install:
            raise FatalError("--check-url requires --install; otherwise use 'cookies check --file'")
        if args.check_url:
            validate_youtube_url(args.check_url)
        if not args.yes:
            if not sys.stdin.isatty():
                raise FatalError(
                    "Local browser access requires confirmation; explicitly pass --yes"
                )
            print(
                "This reads your local browser cookie database. Only unexpired YouTube cookies "
                "will be exported; they grant access to your YouTube session."
            )
            if input("Allow this capture? [y/N] ").strip().lower() not in {"y", "yes"}:
                raise FatalError("Cookie capture cancelled; no browser was read")
        data = capture_browser(args.browser, args.profile)
        if args.install:
            install_in_compose(data, args.check_url)
        else:
            output = args.output or Path("secrets") / COOKIE_FILENAME
            write_private(output, data)
            print(json.dumps({"saved": str(output), **summary(data)}))
    elif args.cookie_command == "import":
        if args.file is None and sys.stdin.isatty():
            raise FatalError(
                "Redirect a Netscape file into stdin or use --file; do not paste cookies"
            )
        data = (
            read_document(args.file) if args.file else sys.stdin.buffer.read(MAX_COOKIE_BYTES + 1)
        )
        print(json.dumps(import_document(data, args.check_url)))
    elif args.cookie_command == "check":
        path = args.file or selected_path()
        asyncio.run(check_source(path, args.url))
        print(json.dumps({"source_check": "passed", "online_verified": True}))
    else:
        path = args.file or selected_path()
        if not path.exists():
            print(json.dumps({"present": False, "online_verified": False}))
            return
        print(json.dumps({"present": True, **summary(read_document(path))}))
