"""Isolated, local document reader: one bounded response, no external services."""
from __future__ import annotations

import asyncio
import json
import sys

from .core import ReaderError, bounded


async def read_document(original, unit=1, offset=0, max_units=100):
    bounded(unit, 1, 1000000, "unit")
    bounded(offset, 0, 1000000000, "offset")
    bounded(max_units, 1, 100, "max_units")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "telegram_reader.document_worker", str(original.path),
        original.info.get("name") or "", original.info.get("mime_type") or "",
        str(unit), str(offset), str(max_units), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(20):
            output, _ = await process.communicate()
        if process.returncode or len(output) > 100000:
            raise ReaderError("Документ не удалось разобрать в пределах ресурсов. Оригинал доступен через get_media.")
        result = json.loads(output)
        if "error" in result:
            raise ReaderError(result["error"])
        return {**result, "chat_id": original.info["chat_id"],
                "message_id": original.info["message_id"], "sha256": original.info["sha256"],
                "source_is_untrusted": True}
    except (ValueError, TimeoutError):
        raise ReaderError("Чтение документа прервано. Оригинал доступен через get_media.") from None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
