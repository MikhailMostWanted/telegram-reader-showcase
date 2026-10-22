"""Small, dependency-free policy and input-validation layer."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class ReaderError(Exception):
    """A safe, user-facing error; never put credentials or raw RPC errors here."""


def bounded(value: int, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ReaderError(f"{name}: допустимо от {low} до {high}.")
    return value


def date_range(start: str | None, end: str | None, tz: str):
    def parse(value):
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=ZoneInfo(tz))
            return result.astimezone(timezone.utc)
        except (ValueError, TypeError):
            raise ReaderError("Дата должна быть ISO 8601 или ГГГГ-ММ-ДД.") from None
    first, last = parse(start), parse(end)
    if first and last and first >= last:
        raise ReaderError("date_from должна быть раньше date_to; date_to не включается.")
    return first, last


def owner_matches(claims: dict, owner_id: int) -> bool:
    # Check the immutable numeric GitHub id, never a renameable login/display name.
    return str(claims.get("sub", "")) == str(owner_id)


@dataclass(repr=False)
class Settings:
    public_url: str
    api_id: int
    api_hash: str
    github_client_id: str
    github_client_secret: str
    github_owner_id: int
    signing_key: str
    telegram_user_id: int = 0
    allow_all_chats: bool = False
    allowed_chat_ids: list[int] = field(default_factory=list)
    blocked_chat_ids: list[int] = field(default_factory=lambda: [777000, 424000])
    timezone: str = "Europe/Moscow"
    max_media_mb: int = 50
    telegram_proxy: dict | None = None
    client_redirect_uris: list[str] = field(default_factory=lambda: [
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/*",
    ])

    def validate(self):
        u = urlsplit(self.public_url)
        if (u.scheme != "https" or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path not in ("", "/")):
            raise ReaderError("public_url: нужен HTTPS-адрес отдельного домена без пути.")
        self.public_url = self.public_url.rstrip("/")
        bounded(self.api_id, 1, 2**31 - 1, "api_id")
        bounded(self.github_owner_id, 1, 2**63 - 1, "github_owner_id")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.api_hash):
            raise ReaderError("api_hash: ожидаются 32 шестнадцатеричных символа.")
        if not self.github_client_id or not self.github_client_secret or len(self.signing_key) < 43:
            raise ReaderError("Не заполнены параметры OAuth или ключ подписи.")
        if type(self.allow_all_chats) is not bool:
            raise ReaderError("allow_all_chats должен быть true или false.")
        for ids in (self.allowed_chat_ids, self.blocked_chat_ids):
            if not isinstance(ids, list) or any(type(i) is not int or i == 0 for i in ids):
                raise ReaderError("Списки чатов должны содержать числовые ID.")
        bounded(self.max_media_mb, 1, 50, "max_media_mb")
        bounded(self.telegram_user_id, 0, 2**63 - 1, "telegram_user_id")
        ZoneInfo(self.timezone)
        if not self.client_redirect_uris or any(not s.startswith("https://") for s in self.client_redirect_uris):
            raise ReaderError("Нужен непустой список HTTPS callback-адресов MCP-клиента.")
        if self.telegram_proxy is not None:
            p = self.telegram_proxy
            if not isinstance(p, dict) or p.get("proxy_type") not in ("socks5", "socks4", "http"):
                raise ReaderError("Некорректные настройки прокси Telegram.")
            bounded(p.get("port"), 1, 65535, "proxy.port")
            if not p.get("addr"):
                raise ReaderError("Не указан адрес прокси Telegram.")
        return self

    @classmethod
    def load(cls, path: Path):
        try:
            if path.stat().st_mode & 0o077:
                raise ReaderError("Закрой доступ к config.json: chmod 600.")
            return cls(**json.loads(path.read_text())).validate()
        except (OSError, TypeError, ValueError, KeyError):
            raise ReaderError("Не удалось прочитать config.json. Выполни команду init.") from None

    def allows(self, chat_id: int) -> bool:
        return (type(chat_id) is int and chat_id not in self.blocked_chat_ids
                and (self.allow_all_chats or chat_id in self.allowed_chat_ids))

    def require_chat(self, chat_id: int):
        if not self.allows(chat_id):
            raise ReaderError("Этот чат не разрешён настройками сервера.")


class Cursors:
    def __init__(self, key: str):
        self.key = key.encode()

    def encode(self, scope: str, data: dict) -> str:
        body = json.dumps({"scope": scope, "exp": int(time.time()) + 3600, "data": data},
                          separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.digest(self.key, body, "sha256")
        return base64.urlsafe_b64encode(signature + body).decode().rstrip("=")

    def decode(self, token: str | None, scope: str) -> dict:
        if not token:
            return {}
        try:
            if len(token) > 2048:
                raise ValueError
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            signature, body = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, body, "sha256")):
                raise ValueError
            item = json.loads(body)
            if item["scope"] != scope or item["exp"] < time.time() or not isinstance(item["data"], dict):
                raise ValueError
            return item["data"]
        except (ValueError, KeyError, TypeError):
            raise ReaderError("Курсор устарел или не относится к этому поиску. Начни поиск заново.") from None

    @staticmethod
    def scope(*values) -> str:
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ReaderError("Вложение превышает допустимый размер. Открой его в Telegram.")
        return super().write(data)


def private_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
