import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest
from telegram_reader.core import BoundedBuffer, Cursors, ReaderError, Settings, bounded, date_range, owner_matches, private_write


def test_scope_and_blocked_service_chat(settings):
    assert settings.allows(-1000000000010)
    assert not settings.allows(777000)
    settings.allow_all_chats = False
    settings.allowed_chat_ids = [12]
    assert settings.allows(12)
    assert not settings.allows(13)
    assert not settings.allows(True)


@pytest.mark.parametrize("claims", [{}, {"sub": "123"}, {"login": "example-owner"}, {"sub": None}])
def test_owner_cannot_be_spoofed_by_login(claims):
    assert not owner_matches(claims, 2468013579)


def test_owner_numeric_id():
    assert owner_matches({"sub": "2468013579", "login": "renamed-account"}, 2468013579)


def test_dates_are_moscow_and_end_exclusive():
    a, b = date_range("2026-10-07", "2026-10-08", "Europe/Moscow")
    assert a == datetime(2026, 10, 6, 21, tzinfo=timezone.utc)
    assert (b - a).total_seconds() == 86400
    with pytest.raises(ReaderError):
        date_range("2026-10-08", "2026-10-07", "Europe/Moscow")


def test_cursor_integrity_scope_and_expiry(monkeypatch):
    codec = Cursors("synthetic")
    token = codec.encode("search-a", {"id": 17})
    assert codec.decode(token, "search-a") == {"id": 17}
    with pytest.raises(ReaderError):
        codec.decode(token, "search-b")
    with pytest.raises(ReaderError):
        codec.decode("!" + token[1:], "search-a")
    monkeypatch.setattr("telegram_reader.core.time.time", lambda: 10**12)
    with pytest.raises(ReaderError):
        codec.decode(token, "search-a")


def test_download_limit_enforced_during_write():
    with BoundedBuffer(4) as stream:
        stream.write(b"1234")
        with pytest.raises(ReaderError):
            stream.write(b"5")
        assert stream.getvalue() == b"1234"


def test_private_config_permissions(tmp_path, settings):
    path = tmp_path / "config.json"
    private_write(path, json.dumps(asdict(settings)))
    assert path.stat().st_mode & 0o777 == 0o600
    assert Settings.load(path).api_id == settings.api_id
    path.chmod(0o644)
    with pytest.raises(ReaderError):
        Settings.load(path)


@pytest.mark.parametrize("url", ["http://reader.example.com", "https://user:pass@example.com", "https://reader.example.com/path"])
def test_reject_unsafe_public_url(settings, url):
    settings.public_url = url
    with pytest.raises(ReaderError):
        settings.validate()


@pytest.mark.parametrize("value", [True, 0, 101, -1])
def test_bounded_limits(value):
    with pytest.raises(ReaderError):
        bounded(value, 1, 100, "limit")
