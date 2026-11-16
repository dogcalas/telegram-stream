"""Private, YouTube-only cookie documents. Never log names, values, or raw lines."""

import math
import os
import tempfile
import time
from collections.abc import Iterable
from http.cookiejar import Cookie
from pathlib import Path

from telegram_stream.errors import FatalError

MAX_COOKIE_BYTES = 2 * 1024 * 1024
COOKIE_FILENAME = "youtube-cookies.txt"
HEADER = "# Netscape HTTP Cookie File\n# YouTube only. Treat this file as a password.\n\n"
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi", "safari")
LOGIN_COOKIE_NAMES = frozenset(
    {
        "SID",
        "SAPISID",
        "LOGIN_INFO",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    }
)


def is_youtube_domain(domain: str) -> bool:
    host = domain.lower().removeprefix(".")
    return host == "youtube.com" or host.endswith(".youtube.com")


def read_document(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_COOKIE_BYTES + 1)
    except OSError:
        raise FatalError(
            "Cannot read cookie file; check its path and private permissions"
        ) from None
    return data


def parse_document(data: bytes) -> list[Cookie]:
    if len(data) > MAX_COOKIE_BYTES:
        raise FatalError("Cookie file exceeds the 2 MiB limit")
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise FatalError("Cookie file must be UTF-8 Netscape format, not JSON") from None
    if not lines or not lines[0].startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise FatalError("Cookie file must have a Netscape/HTTP Cookie File header, not JSON")
    cookies = []
    for line in lines[1:]:
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise FatalError("Malformed Netscape cookie row; cookie contents have been omitted")
        domain, subdomains, path, secure, expiry, name, value = parts
        # Discard unrelated account/site cookies before storing anything.
        if not is_youtube_domain(domain):
            continue
        if (
            subdomains not in {"TRUE", "FALSE"}
            or secure not in {"TRUE", "FALSE"}
            or not path.startswith("/")
            or not name
            or any("\x00" in part for part in parts)
        ):
            raise FatalError(
                "Malformed YouTube cookie attributes; cookie contents have been omitted"
            )
        try:
            expires = float(expiry or "0")
            if not math.isfinite(expires) or expires < 0:
                raise ValueError
        except ValueError:
            raise FatalError(
                "Invalid cookie expiration; cookie contents have been omitted"
            ) from None
        cookies.append(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain.lower(),
                domain_specified=subdomains == "TRUE",
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=secure == "TRUE",
                expires=max(1, int(expires)) if expires else None,
                discard=not expires,
                comment=None,
                comment_url=None,
                rest={"HTTPOnly": ""} if http_only else {},
            )
        )
    return cookies


def usable_cookies(cookies: Iterable[Cookie]) -> list[Cookie]:
    now = time.time()
    return [
        cookie
        for cookie in cookies
        if is_youtube_domain(cookie.domain)
        and bool(cookie.name)
        and bool(cookie.value)
        and (cookie.expires in {None, 0} or cookie.expires > now)
    ]


def serialize_cookies(cookies: Iterable[Cookie]) -> bytes:
    selected = usable_cookies(cookies)
    if not selected:
        raise FatalError(
            "No usable YouTube cookies found; sign in to YouTube in the selected browser/profile"
        )
    lines = [HEADER]
    for cookie in selected:
        domain = cookie.domain.lower()
        if cookie.has_nonstandard_attr("HTTPOnly") or cookie.has_nonstandard_attr("HttpOnly"):
            domain = "#HttpOnly_" + domain
        parts = (
            domain,
            "TRUE" if cookie.domain.startswith(".") else "FALSE",
            cookie.path or "/",
            "TRUE" if cookie.secure else "FALSE",
            str(cookie.expires or 0),
            cookie.name,
            cookie.value or "",
        )
        if any(any(character in part for character in "\t\r\n\x00") for part in parts):
            raise FatalError("Invalid cookie fields; refusing to write credential data")
        lines.append("\t".join(parts) + "\n")
    result = "".join(lines).encode()
    if len(result) > MAX_COOKIE_BYTES:
        raise FatalError("Filtered cookies exceed the 2 MiB limit")
    return result


def normalize_document(data: bytes) -> bytes:
    return serialize_cookies(parse_document(data))


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".youtube-cookies-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp is 0600; replace the directory entry, never follow a target symlink.
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def capture_browser(browser: str, profile: str | None = None) -> bytes:
    # Import lazily: normal health checks must not load browser/decryption tooling.
    from yt_dlp.cookies import YDLLogger, extract_cookies_from_browser

    if browser not in BROWSERS:
        raise FatalError("Unsupported browser")
    try:
        # An unbound YDLLogger emits neither progress nor dependency diagnostics,
        # which may include private browser paths or cookie material.
        jar = extract_cookies_from_browser(browser, profile=profile, logger=YDLLogger())
    except FileNotFoundError:
        raise FatalError(
            "Browser cookie database not found; run on your browser's machine and select --profile"
        ) from None
    except PermissionError:
        raise FatalError(
            "Browser access denied; close it and check OS keychain/profile permissions"
        ) from None
    except Exception:
        raise FatalError(
            "Browser cookies could not be decrypted; try Firefox or check OS keychain permissions"
        ) from None
    return serialize_cookies(jar)


def summary(data: bytes) -> dict[str, object]:
    cookies = parse_document(data)
    valid = usable_cookies(cookies)
    return {
        "usable": len(valid),
        "expired_or_empty": len(cookies) - len(valid),
        "session_cookies": sum(cookie.expires in {None, 0} for cookie in valid),
        "login_cookies_present": any(cookie.name in LOGIN_COOKIE_NAMES for cookie in valid),
        "online_verified": False,
    }


def youtube_diagnostic(stderr: bytes) -> str:
    """Classify untrusted yt-dlp output without returning any of its contents."""
    message = stderr.decode(errors="replace").lower()
    if any(
        phrase in message
        for phrase in (
            "cookies are no longer valid",
            "cookies have expired",
            "cookies have been rotated",
        )
    ):
        return "YouTube cookies expired or rotated; capture and import them again"
    if "not a bot" in message or "sign in to confirm" in message:
        return (
            "YouTube requires authentication for this account/IP; capture cookies from a "
            "signed-in browser and run 'cookies check'"
        )
    if "po token" in message or "po_token" in message:
        return (
            "YouTube requires a PO token for this client/format; cookies alone may not resolve it"
        )
    if "http error 429" in message or "too many requests" in message:
        return "YouTube rate limited this IP; wait before retrying"
    if "unavailable" in message or "private video" in message:
        return "YouTube source is unavailable or restricted for this account/IP"
    return "yt-dlp could not resolve YouTube; check the live URL, account/IP access, and cookies"
