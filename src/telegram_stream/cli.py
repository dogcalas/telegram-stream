"""Interactive bootstrap and noninteractive service commands."""

import argparse
import asyncio
import getpass
import inspect
import json
import logging
import os
import signal

from telethon import TelegramClient, types

from telegram_stream.config import Settings, data_directory
from telegram_stream.errors import FatalError
from telegram_stream.health import read_status
from telegram_stream.logging import configure, event, redactor
from telegram_stream.service import Service
from telegram_stream.state import InstanceLock
from telegram_stream.telegram import verify_schema

logger = logging.getLogger(__name__)


async def authenticate(settings: Settings) -> None:
    verify_schema()
    client = TelegramClient(str(settings.data_dir / "telegram"), settings.api_id, settings.api_hash)
    try:
        authentication = client.start(
            phone=lambda: input("Telegram phone number (international format): ").strip(),
            code_callback=lambda: getpass.getpass("Telegram login code: "),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
        if not inspect.isawaitable(authentication):
            raise FatalError("Authentication did not run on the service event loop")
        await authentication
        user = await client.get_me()
        if not isinstance(user, types.User) or user.bot:
            raise FatalError("Authenticate with a user account, not a bot")
        # Cache channel access hashes for numeric-ID configuration.
        await client.get_dialogs()
        print("User session saved. You can now start the service.")
    finally:
        await client.disconnect()


async def serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    runner = asyncio.create_task(Service(settings).run())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            await runner
        else:
            event(logger, "shutdown_requested")
    finally:
        stopper.cancel()
        runner.cancel()
        results = await asyncio.gather(runner, stopper, return_exceptions=True)
        for result in results[:1]:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def _report_error(error: BaseException) -> None:
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            _report_error(child)
    elif isinstance(error, FatalError):
        event(logger, "fatal", reason=str(error))
    else:
        # Unexpected dependency exceptions can embed full requests and credentials.
        event(logger, "fatal", error=type(error).__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous Telegram channel Live Stories")
    parser.add_argument("command", choices=["auth", "run", "status", "healthcheck"])
    args = parser.parse_args()
    os.umask(0o077)
    configure()
    if args.command in {"status", "healthcheck"}:
        status = read_status(data_directory())
        if args.command == "status":
            print(json.dumps(status))
        raise SystemExit(0 if status["healthy"] else 1)
    try:
        settings = Settings.from_env(require_stream=args.command == "run")
        redactor.add(settings.api_hash, settings.source, settings.extractor_args)
        with InstanceLock(settings.data_dir):
            asyncio.run(authenticate(settings) if args.command == "auth" else serve(settings))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        _report_error(error)
        raise SystemExit(1) from None
