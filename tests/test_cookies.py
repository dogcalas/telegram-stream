"""Only synthetic cookie data is used. No real browser or YouTube access."""

import argparse
import asyncio
import copy
import json
import subprocess
from http.cookiejar import CookieJar
from pathlib import Path

import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from telegram_stream import cookie_cli
from telegram_stream.config import Settings
from telegram_stream.cookies import (
    COOKIE_FILENAME,
    MAX_COOKIE_BYTES,
    capture_browser,
    normalize_document,
    parse_document,
    summary,
    write_private,
    youtube_diagnostic,
)
from telegram_stream.errors import FatalError
from telegram_stream.sources import SourceResolver

HEADER = "# Netscape HTTP Cookie File\n"
ROW = ".youtube.com\tTRUE\t/\tTRUE\t2200000000\tSID\tdummy-youtube-secret\n"
SESSION = "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tdummy-session-secret\n"
EXPIRED = ".youtube.com\tTRUE\t/\tTRUE\t1\tOLD\tdummy-expired-secret\n"
FOREIGN = ".example.com\tTRUE\t/\tTRUE\t2200000000\tOTHER\tdummy-foreign-secret\n"
VALID = (HEADER + ROW + SESSION).encode()


def arguments(*args: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cookie_cli.configure_parser(parser)
    return parser.parse_args(list(args))


def test_filtering_preserves_session_http_only_and_netscape_compatibility(tmp_path: Path) -> None:
    normalized = normalize_document((HEADER + ROW + SESSION + EXPIRED + FOREIGN).encode())
    assert b"dummy-foreign-secret" not in normalized
    assert b"dummy-expired-secret" not in normalized
    assert b"#HttpOnly_.youtube.com" in normalized
    path = tmp_path / "cookies.txt"
    write_private(path, normalized)
    jar = YoutubeDLCookieJar(str(path))
    jar.load()
    assert len(jar) == 2
    assert next(cookie for cookie in jar if cookie.name == "LOGIN_INFO").expires is None
    assert summary(normalized)["session_cookies"] == 1
    assert summary(normalized)["login_cookies_present"] is True


@pytest.mark.parametrize(
    "document",
    [
        b'{"cookie":"dummy-do-not-log"}',
        (HEADER + "malformed\tdummy-do-not-log").encode(),
        (HEADER + ROW.replace("2200000000", "NaN")).encode(),
        (HEADER + ROW.replace("2200000000", "0.5")).encode(),
        (HEADER + FOREIGN.replace(".example.com", ".youtube.com.example.com")).encode(),
        (HEADER + FOREIGN.replace(".example.com", ".notyoutube.com")).encode(),
        b"#" * (MAX_COOKIE_BYTES + 1),
    ],
)
def test_invalid_expired_and_lookalike_documents_do_not_expose_contents(document: bytes) -> None:
    with pytest.raises(FatalError) as caught:
        normalize_document(document)
    assert "dummy-" not in str(caught.value)


def test_native_capture_exports_only_youtube_without_dependency_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_extract(browser_name: str, profile: str | None, logger: object) -> CookieJar:
        assert browser_name == "firefox"
        assert profile == "dedicated-profile"
        jar = CookieJar()
        for cookie in parse_document(VALID):
            jar.set_cookie(cookie)
        # This unrelated cookie must never be serialized.
        unrelated = next(iter(jar))
        unrelated = copy.copy(unrelated)
        unrelated.domain = ".example.com"
        unrelated.value = "dummy-foreign-secret"
        jar.set_cookie(unrelated)
        return jar

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", fake_extract)
    captured = capture_browser("firefox", "dedicated-profile")
    assert b"dummy-foreign-secret" not in captured
    assert summary(captured)["usable"] == 2
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_capture_error_does_not_include_native_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> CookieJar:
        raise RuntimeError("decryption detail: dummy-private-secret")

    monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", fail)
    with pytest.raises(FatalError, match="decrypted") as caught:
        capture_browser("chrome")
    assert "dummy-private-secret" not in str(caught.value)


def test_private_import_is_atomic_and_auto_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    target = tmp_path / COOKIE_FILENAME
    original = tmp_path / "unrelated.txt"
    original.write_text("unrelated user file")
    target.symlink_to(original)
    result = cookie_cli.import_document(VALID)
    assert result["installed"] and not result["online_verified"]
    assert not target.is_symlink()
    assert original.read_text() == "unrelated user file"
    assert target.stat().st_mode & 0o777 == 0o600
    assert Settings(1, "0" * 32, data_dir=tmp_path).youtube_cookie_file == target
    before = target.read_bytes()
    with pytest.raises(FatalError):
        cookie_cli.import_document(b"invalid dummy-secret")
    assert target.read_bytes() == before


def test_failed_online_check_preserves_previously_installed_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YOUTUBE_COOKIES_FILE", raising=False)
    target = tmp_path / COOKIE_FILENAME
    write_private(target, VALID)

    async def fail(path: Path, url: str) -> None:
        assert path != target and path.stat().st_mode & 0o777 == 0o600
        assert b"dummy-new-secret" in path.read_bytes()
        assert url == "https://www.youtube.com/watch?v=test"
        raise FatalError("YouTube check failed")

    monkeypatch.setattr(cookie_cli, "check_source", fail)
    with pytest.raises(FatalError, match="check failed"):
        cookie_cli.import_document(
            VALID.replace(b"dummy-youtube-secret", b"dummy-new-secret"),
            "https://www.youtube.com/watch?v=test",
        )
    assert target.read_bytes() == VALID
    assert list(tmp_path.glob(".cookie-check-*")) == []


def test_capture_requires_explicit_consent_and_never_exports_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object) -> bytes:
        pytest.fail("A real browser must not be read by this test")

    monkeypatch.setattr(cookie_cli, "capture_browser", forbidden)
    with pytest.raises(FatalError, match="confirmation"):
        cookie_cli.run(arguments("capture", "--browser", "firefox"))
    with pytest.raises(FatalError, match="stdout"):
        cookie_cli.run(arguments("capture", "--browser", "firefox", "--output", "-", "--yes"))


def test_docker_install_sends_credentials_only_over_stdin_and_filters_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}")

    def fake_run(
        command: list[str], *, input: bytes, capture_output: bool, timeout: int, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        assert input == VALID
        assert all("dummy-" not in part for part in command)
        assert command[-2:] == ["cookies", "import"]
        report = {"installed": True, **summary(VALID), "untrusted_extra": "dummy-private-secret"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(report).encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    cookie_cli.install_in_compose(VALID, None)
    output = capsys.readouterr()
    assert json.loads(output.out)["installed"]
    assert "dummy-" not in output.out + output.err


def test_source_uses_private_filtered_copy_of_managed_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        original = (HEADER + ROW + FOREIGN).encode()
        write_private(tmp_path / COOKIE_FILENAME, original)
        copied: list[Path] = []

        async def fake_capture(command: list[str], timeout: float, **kwargs: object) -> bytes:
            path = Path(command[command.index("--cookies") + 1])
            copied.append(path)
            assert path != tmp_path / COOKIE_FILENAME
            assert path.stat().st_mode & 0o777 == 0o600
            assert b"dummy-foreign-secret" not in path.read_bytes()
            return b'{"live_status":"is_live","url":"https://example.invalid/live.m3u8"}'

        monkeypatch.setattr("telegram_stream.sources.capture", fake_capture)
        resolver = SourceResolver(
            Settings(
                1,
                "0" * 32,
                source="https://youtu.be/test",
                source_type="youtube",
                data_dir=tmp_path,
            )
        )
        await resolver._youtube()
        assert copied and not copied[0].exists()
        assert (tmp_path / COOKIE_FILENAME).read_bytes() == original

    asyncio.run(scenario())


def test_cancelled_install_stops_only_its_own_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if len(commands) == 1:
            raise subprocess.TimeoutExpired(command, 300)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(FatalError, match="could not complete"):
        cookie_cli.install_in_compose(VALID, None)
    name = commands[0][commands[0].index("--name") + 1]
    assert name.startswith("telegram-stream-cookie-import-")
    assert commands[1] == ["docker", "stop", "--time", "5", name]


def test_diagnostics_are_actionable_without_relaying_stderr() -> None:
    reason = youtube_diagnostic(
        b"ERROR dummy-private-secret: Sign in to confirm you're not a bot https://secret.invalid"
    )
    assert "cookies check" in reason
    assert "dummy-" not in reason and "secret.invalid" not in reason
    assert "expired or rotated" in youtube_diagnostic(b"cookies are no longer valid")
    assert "PO token" in youtube_diagnostic(b"PO_TOKEN is required")


def test_checks_reject_foreign_sites_before_reading_credentials() -> None:
    with pytest.raises(FatalError, match="YouTube"):
        cookie_cli.validate_youtube_url("https://youtube.com.example.invalid")
