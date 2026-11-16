"""Bounded subprocess operations, including cancellation-safe process-group cleanup."""

import asyncio
import os
import signal
from contextlib import suppress

from telegram_stream.errors import TemporaryError


async def terminate(process: asyncio.subprocess.Process, timeout: float = 10) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def spawn(
    command: list[str],
    *,
    stderr: int = asyncio.subprocess.PIPE,
    stdout: int = asyncio.subprocess.PIPE,
) -> asyncio.subprocess.Process:
    """Do not orphan a process if cancellation races OS process creation."""
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            limit=262144,
        )
    )
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        try:
            process = await creation
        except OSError:
            raise asyncio.CancelledError from None
        drainage = asyncio.create_task(process.communicate())
        try:
            await terminate(process)
        finally:
            await drainage
        raise


async def capture(command: list[str], timeout: float) -> bytes:
    try:
        process = await spawn(command, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        raise TemporaryError(f"Required executable not found: {command[0]}") from None
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await process.communicate()
        if process.returncode:
            raise TemporaryError(f"{command[0]} failed (exit {process.returncode})")
        return stdout
    finally:
        # communicate() may have been cancelled with a full stdout pipe.
        drainage = asyncio.create_task(process.communicate())
        try:
            await terminate(process)
        finally:
            await drainage
