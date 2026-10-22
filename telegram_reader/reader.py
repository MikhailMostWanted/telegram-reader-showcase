"""Bounded on-demand reads. No event handlers, writes, read receipts, or archives."""
from __future__ import annotations

import asyncio
import fcntl
import time
from pathlib import Path

from .core import Cursors, ReaderError, Settings, bounded, date_range


TRANSCRIPTION_WAIT_SECONDS = 15


def peer_id(peer):
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    if hasattr(peer, "user_id"):
        return peer.user_id
    if hasattr(peer, "channel_id"):
        return -(1000000000000 + peer.channel_id)
    if hasattr(peer, "chat_id"):
        return -peer.chat_id
    return None


def display(entity):
    if entity is None:
        return None
    return (getattr(entity, "title", None)
            or " ".join(filter(None, [getattr(entity, "first_name", None),
                                      getattr(entity, "last_name", None)]))
            or getattr(entity, "username", None))


def media_info(message):
    media = getattr(message, "media", None)
    if media is None or type(media).__name__ not in ("MessageMediaPhoto", "MessageMediaDocument"):
        return None
    file = getattr(message, "file", None)
    if file is None:
        return None
    return {"name": getattr(file, "name", None), "mime_type": getattr(file, "mime_type", None),
            "size_bytes": getattr(file, "size", None),
            "duration_seconds": getattr(file, "duration", None),
            "kind": "photo" if type(media).__name__ == "MessageMediaPhoto" else "document"}


def serialize(message, entities=None, text_limit=16000):
    entities = entities or {}
    chat = peer_id(getattr(message, "peer_id", None))
    sender = peer_id(getattr(message, "from_id", None))
    reply = getattr(message, "reply_to", None)
    text = getattr(message, "message", "") or ""
    sent = getattr(message, "date", None)
    edit = getattr(message, "edit_date", None)
    link = f"https://t.me/c/{-chat - 1000000000000}/{message.id}" if chat and chat < -1000000000000 else None
    return {
        "id": f"{chat}:{message.id}", "chat_id": chat, "message_id": message.id,
        "date": sent.isoformat() if sent else None,
        "edit_date": edit.isoformat() if edit else None,
        "sender_id": sender, "sender_name": display(entities.get(sender) or getattr(message, "sender", None)),
        "chat_title": display(entities.get(chat) or getattr(message, "chat", None)),
        "text": text[:text_limit], "text_truncated": len(text) > text_limit,
        "outgoing": bool(getattr(message, "out", False)),
        "reply_to_message_id": getattr(reply, "reply_to_msg_id", None),
        "topic_id": getattr(reply, "reply_to_top_id", None),
        "album_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "forwarded": getattr(message, "fwd_from", None) is not None,
        "media": media_info(message), "url": link,
        "source_is_untrusted": True,
    }


class Reader:
    def __init__(self, settings: Settings, data_dir: Path):
        self.settings = settings
        self.data_dir = data_dir
        self.client = None
        self.authorized = False
        self.lock_file = None
        self.lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.next_request = 0.0
        self.dialogs = []
        self.dialogs_time = 0.0
        self.catalog_truncated = False
        self.cursors = Cursors(settings.signing_key)

    async def connect(self):
        if self.client is not None and self.client.is_connected() and self.authorized:
            return
        from telethon import TelegramClient
        if not self.settings.telegram_user_id or not (self.data_dir / "telegram.session").is_file():
            raise ReaderError("Telegram ещё не подключён. Выполни login на сервере.")
        if self.lock_file is None:
            self.lock_file = open(self.data_dir / "telegram.lock", "a")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock_file.close()
                self.lock_file = None
                raise ReaderError("Эту Telegram-сессию уже использует другой процесс.") from None
        if self.client is None:
            self.client = TelegramClient(
                str(self.data_dir / "telegram"), self.settings.api_id, self.settings.api_hash,
                receive_updates=False, catch_up=False, flood_sleep_threshold=0,
                request_retries=1, connection_retries=2, retry_delay=1,
                timeout=10, proxy=self.settings.telegram_proxy,
                device_model="Telegram Reader MCP", app_version="0.1.0",
            )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.close()
            raise ReaderError("Telegram-сессия отозвана. Повтори login вручную.")
        me = await self.client.get_me()
        if me is None or me.id != self.settings.telegram_user_id:
            await self.close()
            raise ReaderError("Сессия не соответствует владельцу, выбранному при login.")
        self.authorized = True

    async def close(self):
        try:
            if self.client is not None:
                await self.client.disconnect()
        finally:
            self.client = None
            self.authorized = False
            if self.lock_file:
                self.lock_file.close()
                self.lock_file = None

    async def call(self, operation, request_timeout=50, **kwargs):
        """One operation at a time; Telegram flood waits are returned, not bypassed."""
        try:
            async with asyncio.timeout(request_timeout):
                async with self.lock:
                    if self.cooldown_until > time.monotonic():
                        seconds = int(self.cooldown_until - time.monotonic()) + 1
                        raise ReaderError(f"Telegram просит подождать {seconds} сек. Не повторяй запрос раньше.")
                    await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + 0.5
                    await self.connect()
                    return await operation(**kwargs)
        except ReaderError:
            raise
        except TimeoutError:
            raise ReaderError("Запрос занял слишком много времени. Уменьши диапазон или число сообщений.") from None
        except Exception as exc:
            from telethon.errors import FloodWaitError
            if isinstance(exc, FloodWaitError):
                self.cooldown_until = time.monotonic() + exc.seconds
                raise ReaderError(f"Лимит Telegram: подожди {exc.seconds} сек.") from None
            # Never return raw exception strings, RPC bodies, or tracebacks to the model.
            raise ReaderError("Не удалось прочитать Telegram. Проверь соединение, доступ к чату и сессию.") from None

    async def catalog(self):
        if self.dialogs_time and time.monotonic() - self.dialogs_time < 60:
            return
        found = []
        async for dialog in self.client.iter_dialogs(limit=3001):
            # Store metadata only, never dialog.message / last-message text.
            found.append({"chat_id": dialog.id, "title": dialog.name,
                          "type": "group" if dialog.is_group else "channel" if dialog.is_channel else "private"})
        self.catalog_truncated = len(found) > 3000
        self.dialogs = found[:3000]
        self.dialogs_time = time.monotonic()

    async def list_chats(self, query="", limit=30, offset=0):
        bounded(limit, 1, 100, "limit")
        bounded(offset, 0, 3000, "offset")
        if len(query) > 200:
            raise ReaderError("Слишком длинное название чата.")
        await self.catalog()
        allowed = [d for d in self.dialogs if self.settings.allows(d["chat_id"])
                   and query.casefold() in (d["title"] or "").casefold()]
        end = offset + limit
        return {"chats": allowed[offset:end], "next_offset": end if end < len(allowed) else None,
                "catalog_truncated": self.catalog_truncated,
                "note": "Список названий, не содержимое переписок. Кеш метаданных — до 60 секунд."}

    async def resolve(self, chat_id):
        self.settings.require_chat(chat_id)
        try:
            return await self.client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            await self.catalog()
            try:
                return await self.client.get_input_entity(chat_id)
            except (ValueError, TypeError):
                raise ReaderError("Чат не найден. Получи его числовой chat_id через list_chats.") from None

    async def history(self, chat_id, limit=30, before_id=0, date_from=None, date_to=None, query=None):
        bounded(limit, 1, 100, "limit")
        bounded(before_id, 0, 2**31 - 1, "before_id")
        first, last = date_range(date_from, date_to, self.settings.timezone)
        peer = await self.resolve(chat_id)
        rows = []
        exhausted_by_date = False
        async for message in self.client.iter_messages(
            peer, limit=limit + 1, offset_id=before_id, offset_date=last, search=query,
        ):
            if first and message.date < first:
                exhausted_by_date = True
                break
            rows.append(message)
        more = len(rows) > limit and not exhausted_by_date
        page = rows[:limit]
        items = sorted((serialize(m, text_limit=max(240, 24000 // limit)) for m in page), key=lambda m: (m["date"] or "", m["message_id"]))
        return {"messages": items, "next_before_id": page[-1].id if more else None,
                "order": "oldest_to_newest_within_page", "timezone": self.settings.timezone,
                "date_to_exclusive": True}

    async def search(self, query, chat_id=None, limit=30, before_id=0,
                     date_from=None, date_to=None, cursor=None):
        query = query.strip()
        if not 1 <= len(query) <= 200:
            raise ReaderError("query: от 1 до 200 символов.")
        bounded(limit, 1, 100, "limit")
        if chat_id is not None:
            if cursor:
                raise ReaderError("Для поиска в одном чате используй next_before_id, а не cursor.")
            return await self.history(chat_id, limit, before_id, date_from, date_to, query)
        if not self.settings.allow_all_chats:
            raise ReaderError("Глобальный поиск отключён при ограниченном доступе. Укажи разрешённый chat_id.")
        if before_id:
            raise ReaderError("Для глобального поиска используй cursor, а не before_id.")
        from telethon import functions, types
        first, last = date_range(date_from, date_to, self.settings.timezone)
        scope = self.cursors.scope(query, date_from, date_to)
        state = self.cursors.decode(cursor, scope)
        offset_peer = await self.client.get_input_entity(state["peer"]) if state else types.InputPeerEmpty()
        result = await self.client(functions.messages.SearchGlobalRequest(
            q=query, filter=types.InputMessagesFilterEmpty(), min_date=first, max_date=last,
            offset_rate=state.get("rate", 0), offset_peer=offset_peer,
            offset_id=state.get("id", 0), limit=limit,
        ))
        entities = {peer_id_of_entity(e): e for e in [*result.users, *result.chats]}
        raw = [m for m in result.messages if getattr(m, "date", None) is not None]
        items = [serialize(m, entities, text_limit=max(240, 24000 // limit)) for m in raw
                 if self.settings.allows(peer_id(m.peer_id))
                 and (first is None or m.date >= first) and (last is None or m.date < last)]
        next_cursor = None
        if raw:
            tail = raw[-1]
            new_state = {"peer": peer_id(tail.peer_id), "id": tail.id,
                         "rate": getattr(result, "next_rate", 0) or 0}
            if new_state != state:
                next_cursor = self.cursors.encode(scope, new_state)
        return {"messages": items, "next_cursor": next_cursor,
                "note": "Поиск Telegram по словам, не смысловой индекс. Последняя страница может быть пустой.",
                "order": "telegram_search_order", "date_to_exclusive": True}

    async def context(self, chat_id, message_id, before=10, after=10):
        bounded(message_id, 1, 2**31 - 1, "message_id")
        bounded(before, 0, 30, "before")
        bounded(after, 0, 30, "after")
        peer = await self.resolve(chat_id)
        center = await self.client.get_messages(peer, ids=message_id)
        if center is None or getattr(center, "date", None) is None:
            raise ReaderError("Сообщение удалено или недоступно. Контекст не восстановлен.")
        older = [m async for m in self.client.iter_messages(peer, offset_id=message_id, limit=before)] if before else []
        newer = [m async for m in self.client.iter_messages(peer, min_id=message_id, reverse=True, limit=after)] if after else []
        rows = {m.id: serialize(m, text_limit=24000 // (before + after + 1)) for m in [*older, center, *newer]}
        return {"anchor_message_id": message_id,
                "messages": sorted(rows.values(), key=lambda m: (m["date"] or "", m["message_id"])),
                "note": "Соседние сообщения по хронологии; это не автоматически вся ветка ответов."}

    async def transcribe(self, chat_id, message_id):
        """Ask Telegram to transcribe one voice; listen only while this request is active."""
        from telethon import errors, events, functions, types

        bounded(message_id, 1, 2**31 - 1, "message_id")
        peer = await self.resolve(chat_id)
        message = await self.client.get_messages(peer, ids=message_id)
        if not getattr(message, "voice", None):
            raise ReaderError("В этом сообщении нет доступного голосового.")
        entity = await self.client.get_entity(peer)
        if (getattr(message, "noforwards", False) or getattr(entity, "noforwards", False)
                or getattr(message.media, "ttl_seconds", None)):
            raise ReaderError("Защищённые и исчезающие голосовые не расшифровываются.")

        changed = asyncio.Event()
        latest = None
        transcription_id = None

        async def on_update(update):
            nonlocal latest
            if (peer_id(update.peer) == chat_id and update.msg_id == message_id
                    and (transcription_id is None or update.transcription_id == transcription_id)):
                latest = update
                changed.set()

        event = events.Raw(types.UpdateTranscribedAudio)
        self.client.add_event_handler(on_update, event)
        try:
            await self.client.set_receive_updates(True)
            try:
                result = await self.client(functions.messages.TranscribeAudioRequest(
                    peer=peer, msg_id=message_id))
            except errors.PremiumAccountRequiredError:
                raise ReaderError("Telegram требует Premium или доступную квоту расшифровки.") from None
            except errors.RPCError as exc:
                messages = {
                    "MSG_VOICE_TOO_LONG": "Голосовое слишком длинное для расшифровки Telegram.",
                    "MSG_VOICE_MISSING": "Telegram не нашёл голосовое в этом сообщении.",
                    "TRANSCRIPTION_FAILED": "Telegram не смог расшифровать это голосовое.",
                }
                if exc.message in messages:
                    raise ReaderError(messages[exc.message]) from None
                raise
            transcription_id = result.transcription_id
            deadline = time.monotonic() + TRANSCRIPTION_WAIT_SECONDS
            while getattr(result, "pending", False):
                if latest is not None and latest.transcription_id == transcription_id:
                    result = latest
                    if not getattr(result, "pending", False):
                        break
                changed.clear()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(changed.wait(), remaining)
                except TimeoutError:
                    break
            pending = bool(getattr(result, "pending", False))
            return {"chat_id": chat_id, "message_id": message_id,
                    "transcription_id": str(transcription_id), "text": result.text or "",
                    "pending": pending, "retry_after_seconds": 5 if pending else None,
                    "provider": "telegram", "stored_on_disk": False, "source_is_untrusted": True}
        finally:
            self.client.remove_event_handler(on_update, event)
            await self.client.set_receive_updates(False)

    async def message_text(self, chat_id, message_id, offset=0):
        bounded(message_id, 1, 2**31 - 1, "message_id")
        bounded(offset, 0, 1000000000, "offset")
        peer = await self.resolve(chat_id)
        message = await self.client.get_messages(peer, ids=message_id)
        if message is None or getattr(message, "date", None) is None:
            raise ReaderError("Сообщение удалено или недоступно.")
        text = message.message or ""
        if offset > len(text):
            raise ReaderError("offset выходит за пределы сообщения.")
        part = text[offset:offset + 12000]
        return {"chat_id": chat_id, "message_id": message_id, "text": part,
                "offset": offset, "next_offset": offset + len(part) if offset + len(part) < len(text) else None,
                "source_is_untrusted": True}

    async def media_metadata(self, chat_id, message_id):
        bounded(message_id, 1, 2**31 - 1, "message_id")
        peer = await self.resolve(chat_id)
        message = await self.client.get_messages(peer, ids=message_id)
        info = media_info(message)
        if not info:
            raise ReaderError("Доступного фото или файла в этом сообщении нет.")
        entity = await self.client.get_entity(peer)
        if (getattr(message, "noforwards", False) or getattr(entity, "noforwards", False)
                or getattr(message.media, "ttl_seconds", None)):
            raise ReaderError("Защищённые и исчезающие вложения не выгружаются.")
        maximum = self.settings.max_media_mb * 1024 * 1024
        if info["size_bytes"] is not None and info["size_bytes"] > maximum:
            raise ReaderError(f"Файл больше {self.settings.max_media_mb} МиБ. Открой его в Telegram.")
        info.update(chat_id=chat_id, message_id=message_id, source_is_untrusted=True,
                    date=message.date.isoformat() if getattr(message, "date", None) else None)
        from pathlib import Path
        import mimetypes
        raw_name = info.get("name") or "attachment" + (mimetypes.guess_extension(info.get("mime_type") or "") or ".bin")
        info["name"] = "".join(c for c in Path(raw_name.replace("\\", "/")).name if c.isprintable())[:200] or "attachment.bin"
        media = getattr(message.media, "document", None) or getattr(message.media, "photo", None)
        identity = (getattr(media, "id", None), getattr(message, "edit_date", None), info["size_bytes"])
        return message, info, identity


def peer_id_of_entity(entity):
    name = type(entity).__name__
    if name in ("Channel", "ChannelForbidden"):
        return -(1000000000000 + entity.id)
    if name in ("Chat", "ChatForbidden", "ChatEmpty"):
        return -entity.id
    return entity.id
