import asyncio
from types import SimpleNamespace as NS

import pytest
from telethon import errors, functions, types

from telegram_reader.core import ReaderError
from telegram_reader.reader import Reader


class VoiceClient:
    def __init__(self):
        self.message = NS(voice=True, media=NS(ttl_seconds=None), noforwards=False)
        self.entity = NS(noforwards=False)
        self.result = NS(transcription_id=7, text="готовый текст", pending=False)
        self.updates = []
        self.handlers = []
        self.receiving = []
        self.requests = []
        self.error = None

    async def get_input_entity(self, chat_id):
        return types.InputPeerUser(chat_id, 0)

    async def get_messages(self, peer, ids):
        return self.message

    async def get_entity(self, peer):
        return self.entity

    def add_event_handler(self, handler, event):
        self.handlers.append(handler)

    def remove_event_handler(self, handler, event):
        self.handlers.remove(handler)

    async def set_receive_updates(self, enabled):
        self.receiving.append(enabled)

    async def __call__(self, request):
        assert isinstance(request, functions.messages.TranscribeAudioRequest)
        self.requests.append(request)
        if self.error:
            raise self.error
        # A result update may arrive before the RPC result itself.
        for update in self.updates:
            for handler in self.handlers:
                await handler(update)
        return self.result


@pytest.fixture
def voice_reader(settings, tmp_path):
    reader = Reader(settings, tmp_path)
    reader.client = VoiceClient()
    return reader


def update(peer=12, message_id=1, transcription_id=7, text="весь текст", pending=False):
    return types.UpdateTranscribedAudio(peer=types.PeerUser(peer), msg_id=message_id,
                                       transcription_id=transcription_id, text=text, pending=pending)


async def test_native_transcription_returns_text_without_downloading(voice_reader):
    result = await voice_reader.transcribe(12, 1)
    assert result["text"] == "готовый текст" and not result["pending"]
    assert result["provider"] == "telegram" and not result["stored_on_disk"]
    assert result["source_is_untrusted"]
    assert not voice_reader.client.handlers
    assert voice_reader.client.receiving == [True, False]


async def test_pending_transcription_matches_message_and_early_update(voice_reader):
    client = voice_reader.client
    client.result = NS(transcription_id=7, text="часть", pending=True)
    client.updates = [update(peer=13), update(message_id=2), update()]
    result = await voice_reader.transcribe(12, 1)
    assert result["text"] == "весь текст" and not result["pending"]
    assert len(client.requests) == 1 and not client.handlers


async def test_pending_timeout_is_explicit_and_handler_is_removed(voice_reader, monkeypatch):
    monkeypatch.setattr("telegram_reader.reader.TRANSCRIPTION_WAIT_SECONDS", 0.001)
    client = voice_reader.client
    client.result = NS(transcription_id=7, text="часть", pending=True)
    client.updates = [update(peer=13), update(message_id=2), update(transcription_id=8)]
    result = await voice_reader.transcribe(12, 1)
    assert result["pending"] and result["text"] == "часть"
    assert result["retry_after_seconds"] == 5
    assert not client.handlers and client.receiving[-1] is False


async def test_pending_wait_receives_final_update_and_ignores_other_job(voice_reader):
    client = voice_reader.client
    client.result = NS(transcription_id=7, text="", pending=True)

    async def finish():
        await asyncio.sleep(0)
        for handler in client.handlers:
            await handler(update(transcription_id=8, text="чужая расшифровка"))
            await handler(update(text="часть", pending=True))
        await asyncio.sleep(0)
        for handler in client.handlers:
            await handler(update(text="финал"))

    task = asyncio.create_task(finish())
    result = await voice_reader.transcribe(12, 1)
    await task
    assert result["text"] == "финал" and not result["pending"]
    assert not client.handlers and client.receiving[-1] is False


async def test_transcription_never_reaches_denied_chat(voice_reader):
    with pytest.raises(ReaderError, match="не разрешён"):
        await voice_reader.transcribe(777000, 1)
    assert not voice_reader.client.requests and not voice_reader.client.receiving


@pytest.mark.parametrize("protected", ["message", "chat", "ttl"])
async def test_protected_voice_is_not_transcribed(voice_reader, protected):
    client = voice_reader.client
    if protected == "message":
        client.message.noforwards = True
    elif protected == "chat":
        client.entity.noforwards = True
    else:
        client.message.media.ttl_seconds = 10
    with pytest.raises(ReaderError, match="Защищённые"):
        await voice_reader.transcribe(12, 1)
    assert not client.requests and not client.receiving


@pytest.mark.parametrize("missing", [True, False])
async def test_only_existing_voice_can_be_transcribed(voice_reader, missing):
    voice_reader.client.message = None if missing else NS(voice=None)
    with pytest.raises(ReaderError, match="нет доступного голосового"):
        await voice_reader.transcribe(12, 1)
    assert not voice_reader.client.requests


@pytest.mark.parametrize("error,match", [
    (errors.PremiumAccountRequiredError(None), "Premium"),
    (errors.RPCError(None, "TRANSCRIPTION_FAILED", 400), "не смог"),
])
async def test_telegram_errors_are_safe_and_restore_updates(voice_reader, error, match):
    voice_reader.client.error = error
    with pytest.raises(ReaderError, match=match):
        await voice_reader.transcribe(12, 1)
    assert not voice_reader.client.handlers and voice_reader.client.receiving[-1] is False


async def test_cancelled_request_also_restores_updates(voice_reader):
    voice_reader.client.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await voice_reader.transcribe(12, 1)
    assert not voice_reader.client.handlers and voice_reader.client.receiving[-1] is False
