"""Private bounded ephemeral originals. No file bytes are returned as MCP resources."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .core import ReaderError

INLINE_LIMIT = 512 * 1024
TTL = 300


class BoundedFile:
    def __init__(self, file, limit):
        self.file, self.limit = file, limit
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data):
        if self.size + len(data) > self.limit:
            raise ReaderError("Вложение превышает допустимый размер.")
        written = self.file.write(data)
        self.size += written
        self.digest.update(data[:written])
        return written

    def flush(self):
        self.file.flush()


@dataclass
class Original:
    path: Path
    info: dict
    identity: tuple
    expires: float
    users: int = 0


class MediaStore:
    def __init__(self, reader):
        self.reader = reader
        self.directory = reader.data_dir / "media-cache"
        self.lock = asyncio.Lock()
        self.files = {}
        self.grants = {}
        self.lock_file = None

    def start(self):
        if self.lock_file is not None:
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ReaderError("Некорректный каталог временных файлов.")
        os.chmod(self.directory, 0o700)
        lock = open(self.directory / ".lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ReaderError("Временные файлы уже использует другой процесс.") from None
        self.lock_file = lock
        for path in self.directory.glob("original-*"):
            if path.is_file() or path.is_symlink():
                path.unlink()

    def cleanup(self):
        now = time.monotonic()
        self.grants = {key: value for key, value in self.grants.items() if value[2] > now}
        for key, original in list(self.files.items()):
            if original.expires <= now and not original.users:
                original.path.unlink(missing_ok=True)
                del self.files[key]

    async def sweep(self):
        while True:
            await asyncio.sleep(15)
            self.cleanup()

    def close(self):
        for original in self.files.values():
            original.path.unlink(missing_ok=True)
        self.files.clear()
        self.grants.clear()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    async def prepare(self, chat_id, message_id):
        # Reader.call serializes Telegram requests. A fresh check precedes every reuse.
        message, info, identity = await self.reader.media_metadata(chat_id, message_id)
        self.start()
        self.cleanup()
        key = (chat_id, message_id)
        old = self.files.get(key)
        if old and old.identity == identity and old.expires > time.monotonic():
            return old
        if old and old.users:
            raise ReaderError("Предыдущая версия вложения ещё скачивается. Повтори позже.")
        if old:
            old.path.unlink(missing_ok=True)
            del self.files[key]
        while len(self.files) >= 2:
            candidates = [(k, v) for k, v in self.files.items() if not v.users]
            if not candidates:
                raise ReaderError("Два вложения сейчас используются. Повтори позже.")
            k, v = min(candidates, key=lambda item: item[1].expires)
            v.path.unlink(missing_ok=True)
            del self.files[k]
        path = self.directory / ("original-" + secrets.token_hex(24))
        try:
            with path.open("xb") as file:
                os.chmod(path, 0o600)
                buffer = BoundedFile(file, self.reader.settings.max_media_mb * 1024 * 1024)
                options = {}
                if info["kind"] == "photo":
                    # Telethon's default prefers video_sizes on animated photos. Select
                    # the largest original still variant, matching message.file.size.
                    from telethon import utils
                    sizes = getattr(getattr(message.media, "photo", None), "sizes", [])
                    if sizes:
                        largest = max(sizes, key=lambda s: utils._photo_size_byte_count(s) or 0)
                        options["thumb"] = largest.type
                await self.reader.client.download_media(message, file=buffer, **options)
                if not buffer.size:
                    raise ReaderError("Telegram не вернул содержимое вложения.")
                # Stripped photo thumbnails gain a JPEG header in Telethon, so
                # their advertised size is not an integrity check for final JPEG bytes.
                if (info["kind"] == "document" and info["size_bytes"] is not None
                        and info["size_bytes"] != buffer.size):
                    raise ReaderError("Telegram вернул неполное вложение. Повтори запрос.")
            info.update(size_bytes=buffer.size, sha256=buffer.digest.hexdigest(),
                        stored_on_disk=True, source_is_untrusted=True)
            original = Original(path, info, identity, time.monotonic() + TTL)
            self.files[key] = original
            return original
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def issue(self, original, access_token):
        self.cleanup()
        if len(self.grants) >= 64:
            raise ReaderError("Слишком много активных ссылок. Подожди пять минут.")
        secret = secrets.token_urlsafe(32)
        expires = min(original.expires, time.monotonic() + TTL)
        self.grants[hashlib.sha256(secret.encode()).hexdigest()] = (original, access_token, expires)
        return secret, max(0, int(expires - time.monotonic()))

    def grant(self, secret):
        if len(secret) != 43:
            raise ReaderError("Ссылка недоступна или истекла.")
        self.cleanup()
        value = self.grants.get(hashlib.sha256(secret.encode()).hexdigest())
        if not value or value[0] not in self.files.values() or not value[0].path.is_file():
            raise ReaderError("Ссылка недоступна или истекла.")
        return value

    @contextmanager
    def lease(self, original):
        original.users += 1
        try:
            yield original
        finally:
            original.users -= 1
            self.cleanup()
