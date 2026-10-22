from pathlib import Path
from zipfile import ZipFile
import asyncio
from types import SimpleNamespace as NS

import pytest
from openpyxl import Workbook

from telegram_reader.core import ReaderError
from telegram_reader.documents import read_document


def original(path, name=None):
    return NS(path=path, info={'name': name or path.name, 'mime_type': 'application/octet-stream',
                              'chat_id': 12, 'message_id': 7, 'sha256': 'synthetic'})


async def collect(path):
    parts, following = [], {'unit': 1, 'offset': 0}
    while following:
        result = await read_document(original(path), **following)
        parts.extend(part['text'] for part in result['parts'])
        following = result['next']
    return parts


async def test_docx_paragraphs_and_table_order(tmp_path):
    path = tmp_path / 'test.docx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>First</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>Last</w:t></w:r></w:p></w:body></w:document>')
    assert await collect(path) == ['First', 'Table cell', 'Last']


async def test_pptx_actual_presentation_order(tmp_path):
    path = tmp_path / 'test.pptx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="urn:p" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="r2"/><p:sldId r:id="r10"/><p:sldId r:id="r1"/></p:sldIdLst></p:presentation>')
        archive.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/><Relationship Id="r10" Target="slides/slide10.xml"/></Relationships>')
        for number in [10, 2, 1]:
            archive.writestr(f'ppt/slides/slide{number}.xml', f'<slide xmlns:a="urn:a"><a:t>Slide {number}</a:t></slide>')
    assert await collect(path) == ['Slide 2', 'Slide 10', 'Slide 1']


async def test_xlsx_rows_sheets_and_formulas_preserved(tmp_path):
    path = tmp_path / 'test.xlsx'
    book = Workbook()
    book.active.title = 'One'
    book.active.append(['Heading', 'Value'])
    book.active.append(['Item', '=2+3'])
    book.create_sheet('Two').append(['Next'])
    book.save(path)
    rows = await collect(path)
    assert len(rows) == 3
    assert 'Heading' in rows[0] and '=2+3' in rows[1] and 'Two' in rows[2]


@pytest.mark.parametrize('suffix', ['zip', 'doc', 'ppt', 'exe'])
async def test_unsupported_format_is_explicit_and_original_kept(tmp_path, suffix):
    path = tmp_path / ('test.' + suffix)
    path.write_bytes(b'not executed')
    with pytest.raises(ReaderError, match='get_media'):
        await read_document(original(path))
    assert path.read_bytes() == b'not executed'


async def test_xml_entities_not_followed(tmp_path):
    path = tmp_path / 'test.docx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///etc/passwd">]><w:document xmlns:w="urn:w"><w:p><w:t>&leak;</w:t></w:p></w:document>')
    with pytest.raises(ReaderError, match='не удалось'):
        await read_document(original(path))


async def test_parser_cancel_kills_child(tmp_path, monkeypatch):
    path = tmp_path / 'test.txt'
    path.write_text('synthetic')
    created = []
    real = asyncio.create_subprocess_exec
    async def subprocess(*args, **kwargs):
        process = await real(args[0], '-c', 'import time; time.sleep(60)', **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr('telegram_reader.documents.asyncio.create_subprocess_exec', subprocess)
    task = asyncio.create_task(read_document(original(path)))
    while not created:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created[0].returncode is not None


async def test_small_units_are_batched_and_single_unit_remains_selectable(tmp_path):
    path = tmp_path / 'test.docx'
    xml = '<w:document xmlns:w="urn:w"><w:body>' + ''.join(f'<w:p><w:r><w:t>Paragraph {n}</w:t></w:r></w:p>' for n in range(101)) + '</w:body></w:document>'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', xml)
    first = await read_document(original(path))
    assert len(first['parts']) == 100
    assert first['next'] == {'unit': 101, 'offset': 0}
    last = await read_document(original(path), **first['next'])
    assert last['parts'][0]['text'] == 'Paragraph 100' and last['next'] is None
    selected = await read_document(original(path), unit=50, max_units=1)
    assert selected['parts'][0]['text'] == 'Paragraph 49' and len(selected['parts']) == 1
    assert selected['next'] == {'unit': 51, 'offset': 0}
