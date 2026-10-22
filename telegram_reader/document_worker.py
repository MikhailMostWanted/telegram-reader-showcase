"""Disposable parser process; its CLI is internal and never exposed through MCP."""
from __future__ import annotations

import itertools
import json
import re
import resource
import sys
from pathlib import Path

LIMIT = 12000


def units(path, name, mime):
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf" or mime == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Зашифрованный PDF: скачай оригинал для открытия с паролем.")
        for page in reader.pages:
            yield page.extract_text() or ""
    elif suffix in (".txt", ".csv", ".tsv") or mime.startswith("text/"):
        import codecs
        # UTF-8 and BOM-marked UTF-16. Invalid encodings are reported, never silently replaced.
        with open(path, "rb") as file:
            marker = file.read(3)
        encoding = "utf-16" if marker.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        with open(path, encoding=encoding) as file:
            while chunk := file.read(LIMIT):
                yield chunk
    elif suffix == ".docx" or mime.endswith("wordprocessingml.document"):
        from zipfile import ZipFile
        from defusedxml.ElementTree import iterparse
        with ZipFile(path) as archive:
            validate_zip(archive)
            with archive.open("word/document.xml") as xml:
                for _, element in iterparse(xml, events=("end",)):
                    if element.tag.endswith("}p"):
                        yield "".join(e.text or "" for e in element.iter() if e.tag.endswith("}t"))
                        element.clear()
    elif suffix == ".pptx" or mime.endswith("presentationml.presentation"):
        from zipfile import ZipFile
        from defusedxml.ElementTree import parse
        with ZipFile(path) as archive:
            validate_zip(archive)
            # File numbering is not presentation order after a user reorders slides.
            import posixpath
            with archive.open("ppt/_rels/presentation.xml.rels") as xml:
                relationships = parse(xml).getroot()
            targets = {e.attrib.get("Id"): posixpath.normpath("ppt/" + e.attrib.get("Target", ""))
                       for e in relationships if e.attrib.get("TargetMode") != "External"}
            with archive.open("ppt/presentation.xml") as xml:
                presentation = parse(xml).getroot()
            names = []
            for element in presentation.iter():
                if element.tag.endswith("}sldId"):
                    identifier = element.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    target = targets.get(identifier, "")
                    if not re.fullmatch(r"ppt/slides/slide\d+\.xml", target):
                        raise ValueError("Часть презентации недоступна. Получи оригинал.")
                    names.append(target)
            for slide in names:
                with archive.open(slide) as xml:
                    root = parse(xml).getroot()
                yield "\n".join(e.text or "" for e in root.iter() if e.tag.endswith("}t"))
    elif suffix == ".xlsx" or mime.endswith("spreadsheetml.sheet"):
        from zipfile import ZipFile
        from openpyxl import load_workbook
        with ZipFile(path) as archive:
            validate_zip(archive)
        book = load_workbook(path.open("rb"), read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in book:
                for row in sheet.iter_rows(values_only=True):
                    yield json.dumps({"sheet": sheet.title, "cells": row}, ensure_ascii=False, default=str)
        finally:
            book.close()
    elif suffix == ".xls":
        import xlrd
        book = xlrd.open_workbook(str(path), on_demand=True)
        try:
            for sheet in book.sheets():
                for row in range(sheet.nrows):
                    yield json.dumps({"sheet": sheet.name, "cells": sheet.row_values(row)}, ensure_ascii=False)
        finally:
            book.release_resources()
    else:
        raise ValueError("Локальное чтение поддерживает PDF, TXT/CSV/TSV, DOCX, XLS/XLSX, PPTX. Для этого формата получи оригинал через get_media; OCR и распознавание аудио не выполняются.")


def validate_zip(archive):
    # Inspect only known Office members, never extract files or run macros/programs.
    members = archive.infolist()
    if len(members) > 10000 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
        raise ValueError("Office-документ слишком велик после декодирования. Получи оригинал.")
    if any(m.file_size > 16 * 1024 * 1024 for m in members):
        raise ValueError("Часть Office-документа слишком велика для локального чтения.")


def main():
    # macOS does not implement a usable RLIMIT_AS; Linux production must enforce it.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    try:
        path, name, mime, unit, offset, max_units = sys.argv[1:]
        unit, offset, max_units = int(unit), int(offset), int(max_units)
        stream = iter(units(Path(path), name, mime))
        text = next(itertools.islice(stream, unit - 1, unit), None)
        if text is None:
            raise ValueError("Часть документа не существует.")
        if offset > len(text):
            raise ValueError("offset выходит за пределы части.")
        parts, remaining, following = [], LIMIT, None
        while text is not None:
            part = text[offset:offset + remaining]
            parts.append({"unit": unit, "offset": offset, "text": part, "empty_text": not text})
            remaining -= len(part)
            if offset + len(part) < len(text):
                following = {"unit": unit, "offset": offset + len(part)}
                break
            text = next(stream, None)
            unit, offset = unit + 1, 0
            if text is not None and (remaining == 0 or len(parts) >= max_units):
                following = {"unit": unit, "offset": 0}
                break
        result = {"parts": parts, "next": following,
                  "note": "PDF: страницы; DOCX: абзацы с таблицами; PPTX: слайды; XLS/XLSX: строки с именем листа; текст: блоки. Извлечение текста не передаёт изображения, оформление, комментарии или вложенные объекты. Пустая PDF-страница может требовать OCR."}
    except ValueError as exc:
        # Only our explicit messages; raw parser errors are suppressed below.
        known = str(exc)
        if not known.startswith(("Зашифрованный", "Локальное", "Office-", "Часть", "offset")):
            known = "Документ не удалось разобрать. Получи оригинал через get_media."
        result = {"error": known}
    except Exception:
        result = {"error": "Документ не удалось разобрать. Получи оригинал через get_media."}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
