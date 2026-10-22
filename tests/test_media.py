"""Synthetic originals only; no production Telegram session or document required."""
import asyncio
import hashlib
import io
import json
import re
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace as NS

import httpx2 as httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.utilities.types import File
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerUser

from telegram_reader.core import ReaderError
from telegram_reader.documents import read_document
from telegram_reader.media import MediaStore, TTL
from telegram_reader.reader import Reader
from telegram_reader.server import create_server


def synthetic_pdf(path, size=0, text="Synthetic document"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 20 200 Td (' + text + ') Tj ET').encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    base = output.getvalue()
    offset = re.search(rb'startxref\s+(\d+)', base)[1]
    tail = b'\nstartxref\n' + offset + b'\n%%EOF\n'
    target = max(size, len(base) + len(tail))
    with path.open('wb') as file:
        file.write(base)
        remaining = target - len(base) - len(tail)
        while remaining:
            chunk = b'%' * min(remaining, 65536)
            file.write(chunk)
            remaining -= len(chunk)
        file.write(tail)
    return target


class MediaClient:
    def __init__(self, path, name="test.pdf", mime="application/pdf", declared=None):
        self.path = path
        self.downloads = 0
        self.failure = None
        self.message = NS(id=7, message="synthetic", date=NS(isoformat=lambda: "2026-10-12T12:00:00+00:00"),
            edit_date=None, peer_id=PeerUser(12),
            media=type("MessageMediaDocument", (), {} )(),
            file=NS(name=name, mime_type=mime, size=path.stat().st_size if declared is None else declared, duration=None))
        self.message.media.document = NS(id=42)
        self.entity = NS(noforwards=False)

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_entity(self, peer):
        return self.entity

    async def get_messages(self, peer, ids):
        return self.message

    async def download_media(self, message, file, **kwargs):
        self.downloads += 1
        with self.path.open('rb') as source:
            while chunk := source.read(65536):
                file.write(chunk)
                if self.failure:
                    raise self.failure
                await asyncio.sleep(0)


@pytest.fixture
def media_reader(settings, tmp_path):
    source = tmp_path / "source.pdf"
    synthetic_pdf(source)
    reader = Reader(settings, tmp_path / "data")
    reader.client = MediaClient(source)
    async def connect():
        pass
    async def close():
        pass
    reader.connect, reader.close = connect, close
    return reader


@pytest.mark.parametrize("size", [0, 5000000, 25 * 1024 * 1024, 50 * 1024 * 1024])
async def test_original_sizes_hash_and_bounded_allocations(media_reader, size):
    source = media_reader.client.path
    expected = synthetic_pdf(source, size)
    assert len(PdfReader(source).pages) == 1
    media_reader.client.message.file.size = expected
    store = MediaStore(media_reader)
    tracemalloc.start()
    original = await store.prepare(12, 7)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    with source.open('rb') as file:
        expected_hash = hashlib.file_digest(file, 'sha256').hexdigest()
    with original.path.open('rb') as file:
        assert hashlib.file_digest(file, 'sha256').hexdigest() == expected_hash
    assert original.info['sha256'] == expected_hash
    assert original.info['size_bytes'] == expected
    assert original.path.stat().st_mode & 0o077 == 0
    assert store.directory.stat().st_mode & 0o077 == 0
    assert await store.prepare(12, 7) is original
    assert media_reader.client.downloads == 1
    store.close()
    assert not original.path.exists()


async def test_oversize_metadata_and_actual_stream(media_reader):
    store = MediaStore(media_reader)
    media_reader.client.message.file.size = 50 * 1024 * 1024 + 1
    with pytest.raises(ReaderError, match="50"):
        await store.prepare(12, 7)
    assert media_reader.client.downloads == 0
    synthetic_pdf(media_reader.client.path, 50 * 1024 * 1024 + 1)
    media_reader.client.message.file.size = None
    with pytest.raises(ReaderError, match="размер"):
        await store.prepare(12, 7)
    assert not list(store.directory.glob('original-*'))
    store.close()


@pytest.mark.parametrize('failure', [OSError('private details'), TimeoutError(), asyncio.CancelledError()])
async def test_failed_download_cleans_and_next_request_works(media_reader, failure):
    store = MediaStore(media_reader)
    media_reader.client.failure = failure
    with pytest.raises(type(failure)):
        await store.prepare(12, 7)
    assert not list(store.directory.glob('original-*'))
    media_reader.client.failure = None
    assert (await store.prepare(12, 7)).info['size_bytes'] > 0
    store.close()


async def test_incomplete_protected_missing_blocked(media_reader):
    store = MediaStore(media_reader)
    media_reader.client.message.file.size += 1
    with pytest.raises(ReaderError, match='неполное'):
        await store.prepare(12, 7)
    media_reader.client.message.file.size -= 1
    media_reader.client.entity.noforwards = True
    with pytest.raises(ReaderError, match='Защищённые'):
        await store.prepare(12, 7)
    media_reader.client.entity.noforwards = False
    with pytest.raises(ReaderError):
        await store.prepare(777000, 7)
    media_reader.client.message = None
    with pytest.raises(ReaderError):
        await store.prepare(12, 7)
    store.close()


async def test_grants_expiry_cleanup_eviction_and_active_lease(media_reader, monkeypatch):
    now = time.monotonic()
    monkeypatch.setattr('telegram_reader.media.time.monotonic', lambda: now)
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    secret, seconds = store.issue(original, 'synthetic-token')
    assert seconds <= TTL and store.grant(secret)[0] is original
    with pytest.raises(ReaderError):
        store.grant('x' * 43)
    with store.lease(original):
        now += TTL + 1
        store.cleanup()
        assert original.path.exists()
        with pytest.raises(ReaderError):
            store.grant(secret)
    assert not original.path.exists()
    now += 1
    first = await store.prepare(12, 7)
    now += 1
    await store.prepare(12, 8)
    now += 1
    await store.prepare(12, 9)
    assert len(store.files) == 2 and not first.path.exists()
    with store.lease(store.files[(12, 8)]), store.lease(store.files[(12, 9)]):
        with pytest.raises(ReaderError, match='Два'):
            await store.prepare(12, 10)
    store.close()


async def test_parser_pdf_text_and_long_parts(media_reader):
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    result = await read_document(original)
    assert result['parts'][0]['text'] == 'Synthetic document'
    assert result['next'] is None
    text = 'А' * 30001
    media_reader.client.path.write_text(text)
    media_reader.client.message.file.name = 'test.txt'
    media_reader.client.message.file.mime_type = 'text/plain'
    media_reader.client.message.file.size = media_reader.client.path.stat().st_size
    original = await store.prepare(12, 7)
    parts = []
    continuation = {'unit': 1, 'offset': 0}
    while continuation:
        result = await read_document(original, **continuation)
        parts.extend(part['text'] for part in result['parts'])
        continuation = result['next']
    assert ''.join(parts) == text
    store.close()


async def test_pdf_long_page_offset_no_loss(media_reader):
    text = 'Z' * 26001
    synthetic_pdf(media_reader.client.path, text=text)
    media_reader.client.message.file.size = media_reader.client.path.stat().st_size
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    parts, continuation = [], {'unit': 1, 'offset': 0}
    while continuation:
        result = await read_document(original, **continuation)
        parts.extend(part['text'] for part in result['parts'])
        continuation = result['next']
    assert ''.join(parts) == text
    store.close()


@pytest.fixture
def owner_token(settings, monkeypatch):
    token = NS(claims={'sub': str(settings.github_owner_id)}, token='synthetic-owner-token',
              scopes=['read:user'], expires_at=int(time.time()) + 3600)
    monkeypatch.setattr('telegram_reader.server.get_http_headers', lambda **kwargs: {'authorization': 'Bearer ' + token.token})
    return token


@pytest.mark.parametrize('size', [0, 5000000, 25 * 1024 * 1024, 50 * 1024 * 1024])
async def test_mcp_small_response_and_http_original(media_reader, owner_token, monkeypatch, size, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    monkeypatch.setattr('telegram_reader.server.get_access_token', lambda: owner_token)
    async def verify(self, token):
        return owner_token if token == owner_token.token else None
    monkeypatch.setattr(GitHubProvider, 'verify_token', verify)
    expected = synthetic_pdf(media_reader.client.path, size)
    media_reader.client.message.file.size = expected
    server = create_server(media_reader.settings, media_reader)
    async with Client(server) as client:
        result = await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7})
        assert len(result.content) == 2
        assert result.content[1].type == 'resource_link'
        assert len(result.content[0].text) < 3000
        assert 'blob' not in result.content[0].text
        info = json.loads(result.content[0].text)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.http_app()), base_url='https://reader.example.com') as http:
            response = await http.get(info['download_url'])
            assert response.status_code == 200
            assert len(response.content) == expected
            assert hashlib.sha256(response.content).hexdigest() == info['sha256']
            assert response.headers['cache-control'] == 'no-store, private'
            assert response.headers['content-disposition'].startswith('attachment;')
            assert response.headers['x-content-type-options'] == 'nosniff'
            assert (await http.get('/media/' + 'x' * 43)).status_code == 404
            owner_token.claims = {'sub': '123'}
            assert (await http.get(info['download_url'])).status_code == 404
    assert not list((media_reader.data_dir / 'media-cache').glob('original-*'))


async def test_mcp_read_document_reads_pdf_and_continues_long_page(media_reader, owner_token, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    monkeypatch.setattr('telegram_reader.server.get_access_token', lambda: owner_token)
    text = 'Z' * 26001
    synthetic_pdf(media_reader.client.path, text=text)
    media_reader.client.message.file.size = media_reader.client.path.stat().st_size
    server = create_server(media_reader.settings, media_reader)
    parts, continuation = [], {'unit': 1, 'offset': 0}
    async with Client(server) as client:
        while continuation:
            result = await client.call_tool('read_document', {
                'chat_id': 12, 'message_id': 7, 'max_units': 1, **continuation})
            data = json.loads(result.content[0].text)
            assert sum(len(part['text']) for part in data['parts']) <= 12000
            parts.extend(part['text'] for part in data['parts'])
            continuation = data['next']
        assert ''.join(parts) == text
        assert media_reader.client.downloads == 1
    assert not list((media_reader.data_dir / 'media-cache').glob('original-*'))


@pytest.mark.parametrize('mime,kind', [('image/png', 'image'), ('audio/ogg', 'audio')])
async def test_inline_photo_audio_are_bounded(media_reader, owner_token, monkeypatch, tmp_path, mime, kind):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    monkeypatch.setattr('telegram_reader.server.get_access_token', lambda: owner_token)
    import base64
    if kind == 'image':
        data = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1sAAAAASUVORK5CYII=')
    else:
        # Valid small Ogg Opus stream: headers, tags and one silence packet.
        def page(packet, sequence, flags, granule):
            import struct
            header = b'OggS' + bytes([0, flags]) + struct.pack('<QIII', granule, 42, sequence, 0) + bytes([1, len(packet)])
            page_bytes = bytearray(header + packet)
            crc = 0
            for value in page_bytes:
                crc ^= value << 24
                for _ in range(8):
                    crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xffffffff
            page_bytes[22:26] = struct.pack('<I', crc)
            return bytes(page_bytes)
        data = (page(b'OpusHead' + bytes([1, 1]) + b'\x00\x00\x80\xbb\x00\x00\x00\x00\x00', 0, 2, 0)
                + page(b'OpusTags' + b'\x00' * 8, 1, 0, 0) + page(b'\xf8\xff\xfe', 2, 4, 960))
    media_reader.client.path.write_bytes(data)
    media_reader.client.message.file.size = len(data)
    media_reader.client.message.file.mime_type = mime
    server = create_server(media_reader.settings, media_reader)
    async with Client(server) as client:
        result = await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7})
        assert result.content[2].type == kind
        assert base64.b64decode(result.content[2].data) == data
        result = await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7, 'mode': 'original'})
        assert len(result.content) == 2
        media_reader.client.path.write_bytes(b'x' * (512 * 1024 + 1))
        media_reader.client.message.file.size = media_reader.client.path.stat().st_size
        result = await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7})
        assert len(result.content) == 2


def test_old_fastmcp_serialization_is_large_embedded_base64():
    data = b'x' * 5000000
    content = File(data=data, format='pdf', name='synthetic.pdf').to_resource_content()
    assert content.type == 'resource'
    assert len(content.resource.blob) == 6666668


async def test_reader_timeout_flood_and_cancel_release_lock(media_reader):
    async def hang():
        await asyncio.sleep(2)
    with pytest.raises(ReaderError):
        await media_reader.call(hang, request_timeout=0.01)
    async def flood():
        raise FloodWaitError(request=None, capture=2)
    with pytest.raises(ReaderError, match='подожди'):
        await media_reader.call(flood)
    with pytest.raises(ReaderError, match='подождать'):
        await media_reader.call(hang)
    media_reader.cooldown_until = 0
    task = asyncio.create_task(media_reader.call(hang))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not media_reader.lock.locked()


async def test_http_rechecks_revocation_scope_chat_and_media(media_reader, owner_token, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    monkeypatch.setattr('telegram_reader.server.get_access_token', lambda: owner_token)
    verified = owner_token
    async def verify(self, token):
        return verified
    monkeypatch.setattr(GitHubProvider, 'verify_token', verify)
    server = create_server(media_reader.settings, media_reader)
    async with Client(server) as client:
        metadata = await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7, 'mode': 'metadata'})
        assert media_reader.client.downloads == 0
        info = json.loads((await client.call_tool('get_media', {'chat_id': 12, 'message_id': 7})).content[0].text)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.http_app()), base_url=media_reader.settings.public_url) as http:
            verified = None
            assert (await http.get(info['download_url'])).status_code == 404
            verified = owner_token
            owner_token.scopes = []
            assert (await http.get(info['download_url'])).status_code == 404
            owner_token.scopes = ['read:user']
            owner_token.expires_at = int(time.time()) - 1
            assert (await http.get(info['download_url'])).status_code == 404
            owner_token.expires_at = int(time.time()) + 3600
            media_reader.settings.blocked_chat_ids.append(12)
            assert (await http.get(info['download_url'])).status_code == 404
            media_reader.settings.blocked_chat_ids.remove(12)
            media_reader.client.entity.noforwards = True
            assert (await http.get(info['download_url'])).status_code == 404
            media_reader.client.entity.noforwards = False
            media_reader.client.message.media.document.id += 1
            assert (await http.get(info['download_url'])).status_code == 404
            media_reader.client.message.media.document.id -= 1
            assert (await http.get(info['download_url'])).status_code == 200
            media_reader.client.message = None
            assert (await http.get(info['download_url'])).status_code == 404


async def test_stream_disconnect_before_body_releases_lease(media_reader):
    from telegram_reader.server import LeasedStreamingResponse
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    lease = store.lease(original)
    lease.__enter__()
    async def stream():
        yield b'test'
    response = LeasedStreamingResponse(stream(), lease=lease)
    async def send(message):
        raise asyncio.CancelledError()
    async def receive():
        return {'type': 'http.disconnect'}
    with pytest.raises(asyncio.CancelledError):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    assert original.users == 0
    store.close()


@pytest.mark.parametrize("size", [5000000, 25 * 1024 * 1024, 50 * 1024 * 1024])
async def test_parser_reads_large_original_with_small_answer(media_reader, size):
    size = synthetic_pdf(media_reader.client.path, size)
    media_reader.client.message.file.size = size
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    result = await read_document(original)
    assert result['parts'][0]['text'] == 'Synthetic document'
    assert len(json.dumps(result)) < 3000
    store.close()


async def test_photo_largest_still_not_animated_video(media_reader):
    from telethon.tl.types import PhotoSize
    size = media_reader.client.path.stat().st_size
    media_reader.client.message.media = type('MessageMediaPhoto', (), {})()
    media_reader.client.message.media.photo = NS(id=42, sizes=[PhotoSize(type='s', w=10, h=10, size=10),
                                                               PhotoSize(type='x', w=1000, h=1000, size=size)])
    captured = []
    real = media_reader.client.download_media
    async def download(message, file, **kwargs):
        captured.append(kwargs)
        return await real(message, file, **kwargs)
    media_reader.client.download_media = download
    store = MediaStore(media_reader)
    await store.prepare(12, 7)
    assert captured == [{'thumb': 'x'}]
    store.close()


async def test_startup_cleans_crash_files_without_waiting_for_a_tool(media_reader, owner_token, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    directory = media_reader.data_dir / 'media-cache'
    directory.mkdir(parents=True)
    leftover = directory / ('original-' + 'a' * 48)
    leftover.write_bytes(b'synthetic incomplete original')
    unrelated = directory / 'operator-note'
    unrelated.write_text('preserve')
    server = create_server(media_reader.settings, media_reader)
    async with Client(server):
        assert not leftover.exists()
        assert unrelated.read_text() == 'preserve'


async def test_cached_photo_size_may_expand_to_a_jpeg(media_reader):
    from telethon.tl.types import PhotoStrippedSize
    media_reader.client.message.media = type('MessageMediaPhoto', (), {})()
    media_reader.client.message.media.photo = NS(id=42, sizes=[PhotoStrippedSize(type='i', bytes=b'\x01\x01\x01')])
    media_reader.client.message.file.mime_type = 'image/jpeg'
    media_reader.client.message.file.size = 3
    store = MediaStore(media_reader)
    original = await store.prepare(12, 7)
    assert original.info['size_bytes'] == media_reader.client.path.stat().st_size
    store.close()


def test_real_http_token_swap_uses_incoming_bearer(media_reader, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from mcp.server.auth.provider import AccessToken
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    incoming = 'synthetic-fastmcp-jwt'
    upstream = AccessToken(token='synthetic-upstream-github-token', client_id='synthetic',
        scopes=['read:user'], claims={'sub': str(media_reader.settings.github_owner_id)})
    seen = []
    async def verify(self, token):
        seen.append(token)
        return upstream if token == incoming else None
    monkeypatch.setattr(GitHubProvider, 'verify_token', verify)
    server = create_server(media_reader.settings, media_reader)
    with TestClient(server.http_app(path='/mcp', stateless_http=True)) as http:
        response = http.post('/mcp', headers={'Authorization':'Bearer '+incoming,
            'Accept':'application/json, text/event-stream'}, json={
                'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'get_media','arguments':{'chat_id':12,'message_id':7}}})
        assert response.status_code == 200
        if response.headers['content-type'].startswith('text/event-stream'):
            payload=json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
        else:
            payload=response.json()
        assert not payload['result'].get('isError')
        info=json.loads(payload['result']['content'][0]['text'])
        downloaded=http.get(info['download_url'])
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).hexdigest()==info['sha256']
        assert seen == [incoming, incoming]
        assert upstream.token not in payload['result']['content'][0]['text']


async def test_link_requires_incoming_http_bearer(media_reader, owner_token, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'oauth'))
    monkeypatch.setattr('telegram_reader.server.get_access_token', lambda: owner_token)
    monkeypatch.setattr('telegram_reader.server.get_http_headers', lambda **kwargs: {})
    server=create_server(media_reader.settings,media_reader)
    async with Client(server) as client:
        with pytest.raises(Exception, match='OAuth'):
            await client.call_tool('get_media',{'chat_id':12,'message_id':7})
    assert media_reader.client.downloads == 0
