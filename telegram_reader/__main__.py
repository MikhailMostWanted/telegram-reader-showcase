"""Setup and Telegram login happen only in this interactive operator CLI."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import getpass
import json
import logging
import os
import secrets
from dataclasses import asdict
from pathlib import Path

from .core import ReaderError, Settings, private_write


def paths():
    return (Path(os.environ.get("READER_CONFIG", "/config/config.json")),
            Path(os.environ.get("READER_DATA", "/data")))


def setup(config: Path):
    if config.exists():
        raise ReaderError("Конфигурация уже существует; init её не перезаписывает.")
    print("API ID / API hash: my.telegram.org → API development tools.")
    print("GitHub OAuth App: callback = https://ТВОЙ-ДОМЕН/auth/callback.")
    public_url = input("Публичный HTTPS-адрес MCP: ").strip()
    api_id = int(input("Telegram api_id: ").strip())
    api_hash = getpass.getpass("Telegram api_hash (скрыт): ").strip()
    github_id = input("GitHub OAuth Client ID: ").strip()
    github_secret = getpass.getpass("GitHub OAuth Client Secret (скрыт): ").strip()
    owner = int(input("Числовой GitHub ID владельца: ").strip())
    all_chats = input("Разрешить чтение всех обычных чатов? [y/N]: ").strip().lower() in ("y", "yes", "да")
    ids = [] if all_chats else [int(x.strip()) for x in input("Разрешённые chat_id через запятую (можно пока пусто): ").split(",") if x.strip()]
    settings = Settings(public_url=public_url, api_id=api_id, api_hash=api_hash,
                        github_client_id=github_id, github_client_secret=github_secret,
                        github_owner_id=owner, signing_key=secrets.token_urlsafe(48),
                        allow_all_chats=all_chats, allowed_chat_ids=ids).validate()
    private_write(config, json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n")
    print("Конфигурация сохранена. Следующий шаг — login. Секреты не выводились.")


async def login(config: Path, data: Path):
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    settings = Settings.load(config)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(data / "telegram.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReaderError("Сначала останови reader. Нельзя открывать одну сессию в двух процессах.") from None
        client = TelegramClient(str(data / "telegram"), settings.api_id, settings.api_hash,
                                receive_updates=False, catch_up=False, flood_sleep_threshold=0,
                                proxy=settings.telegram_proxy, device_model="Telegram Reader MCP")
        try:
            await client.connect()
            if not await client.is_user_authorized():
                phone = getpass.getpass("Телефон Telegram с кодом страны (скрыт): ").strip()
                sent = await client.send_code_request(phone)
                code = getpass.getpass("Код из Telegram / SMS (скрыт): ").strip()
                try:
                    await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
                except SessionPasswordNeededError:
                    await client.sign_in(password=getpass.getpass("Пароль двухэтапной защиты (скрыт): "))
            me = await client.get_me()
            if me.bot:
                raise ReaderError("Нужен личный аккаунт Telegram, а не бот.")
            if settings.telegram_user_id and settings.telegram_user_id != me.id:
                raise ReaderError("Аккаунт не совпал с сохранённым владельцем. Конфигурация не изменена.")
            if input("Подтверждаешь подключение именно своего аккаунта? [y/N]: ").lower() not in ("y", "yes", "да"):
                raise ReaderError("Подключение к MCP не подтверждено. Проверь устройство в настройках Telegram.")
            settings.telegram_user_id = me.id
            private_write(config, json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n")
            print("Telegram подключён. Сессия сохранена только на сервере.")
        finally:
            await client.disconnect()


def main():
    os.umask(0o077)
    # No HTTP request logging / rich tracebacks that could include private values.
    logging.basicConfig(level=logging.ERROR)
    for name in ("telethon", "fastmcp", "httpx", "httpx2", "httpcore", "authlib"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Личный Telegram Reader MCP")
    parser.add_argument("command", choices=["init", "login", "serve", "check"])
    args = parser.parse_args()
    config, data = paths()
    try:
        if args.command == "init":
            setup(config)
        elif args.command == "login":
            asyncio.run(login(config, data))
        elif args.command == "check":
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as response:
                if response.status != 200:
                    raise ReaderError("HTTP-проверка не пройдена.")
        else:
            settings = Settings.load(config)
            if not settings.telegram_user_id or not (data / "telegram.session").is_file():
                raise ReaderError("Сначала выполни login; публичный сервер без сессии не запускается.")
            from .reader import Reader
            from .server import create_server
            server = create_server(settings, Reader(settings, data))
            server.run(transport="http", host="0.0.0.0", port=8000, path="/mcp",
                       stateless_http=True, show_banner=False, log_level="ERROR",
                       uvicorn_config={"access_log": False, "limit_concurrency": 20})
    except (ReaderError, ValueError) as exc:
        # ValueError from int parsing contains operator inputs, so keep it generic.
        text = str(exc) if isinstance(exc, ReaderError) else "Некорректное значение настройки."
        parser.exit(1, text + "\n")
    except KeyboardInterrupt:
        parser.exit(130, "Остановлено.\n")
    except Exception:
        parser.exit(1, "Операция не завершена. Проверь сеть, настройки и доступы. Секреты в лог не записаны.\n")


if __name__ == "__main__":
    main()
