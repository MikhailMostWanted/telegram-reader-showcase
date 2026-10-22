"""Runs with the actual framework; skipped only in an offline environment without dependencies."""
import json
from types import SimpleNamespace as NS
import pytest

pytest.importorskip("fastmcp")
pytest.importorskip("telethon")
from fastmcp import Client
from telegram_reader.reader import Reader
from telegram_reader.server import create_server


async def test_tools_are_readonly_and_unknown_owner_is_denied(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("telegram_reader.server.get_access_token", lambda: NS(claims={"sub": "123"}))
    server = create_server(settings, Reader(settings, tmp_path))
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == {"list_chats", "get_chat_history", "search_messages",
                                         "get_message_context", "get_media", "get_profile", "read_document", "get_message_text", "transcribe_voice"}
        for tool in tools:
            data = tool.annotations.model_dump(by_alias=True)
            assert data["readOnlyHint"] is (tool.name != "transcribe_voice")
            assert data["destructiveHint"] is False
        arguments = {"get_profile": {}, "list_chats": {},
                     "get_chat_history": {"chat_id": 12}, "search_messages": {"query": "synthetic"},
                     "get_message_context": {"chat_id": 12, "message_id": 7},
                     "get_media": {"chat_id": 12, "message_id": 7},
                     "read_document": {"chat_id": 12, "message_id": 7},
                     "get_message_text": {"chat_id": 12, "message_id": 7},
                     "transcribe_voice": {"chat_id": 12, "message_id": 7}}
        for name, args in arguments.items():
            with pytest.raises(Exception, match="владельцу"):
                await client.call_tool(name, args)


async def test_owner_can_access_profile_without_reading_chats(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("telegram_reader.server.get_access_token", lambda: NS(claims={"sub": "2468013579"}))
    server = create_server(settings, Reader(settings, tmp_path))
    async with Client(server) as client:
        result = await client.call_tool("get_profile", {})
        assert json.loads(result.content[0].text)["id"] == "telegram:99"


def test_http_discovery_and_unauthenticated_denial(settings, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    server = create_server(settings, Reader(settings, tmp_path))
    with TestClient(server.http_app(path="/mcp", stateless_http=True)) as http:
        assert http.get("/healthz").status_code == 200
        metadata = http.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert "S256" in metadata.json()["code_challenge_methods_supported"]
        assert metadata.json().get("registration_endpoint")
        denied = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]


async def test_owner_can_request_native_voice_transcription(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("telegram_reader.server.get_access_token", lambda: NS(claims={"sub": "2468013579"}))
    reader = Reader(settings, tmp_path)

    async def call(operation, **kwargs):
        assert operation == reader.transcribe
        assert kwargs == {"chat_id": 12, "message_id": 1}
        return {"text": "тестовая расшифровка", "pending": False, "provider": "telegram"}

    reader.call = call
    server = create_server(settings, reader)
    async with Client(server) as client:
        result = await client.call_tool("transcribe_voice", {"chat_id": 12, "message_id": 1})
        assert json.loads(result.content[0].text)["text"] == "тестовая расшифровка"


def test_http_discovery_and_unauthenticated_denial(settings, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    server = create_server(settings, Reader(settings, tmp_path))
    with TestClient(server.http_app(path="/mcp", stateless_http=True)) as http:
        assert http.get("/healthz").status_code == 200
        metadata = http.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert "S256" in metadata.json()["code_challenge_methods_supported"]
        assert metadata.json().get("registration_endpoint")
        denied = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert denied.status_code == 401
        assert "resource_metadata" in denied.headers["www-authenticate"]
