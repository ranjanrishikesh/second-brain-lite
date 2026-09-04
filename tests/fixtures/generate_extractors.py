#!/usr/bin/env python3
"""Build the checked-in extractor fixtures without external tools.

The binary fixtures deliberately use only fixed metadata and uncompressed ZIP/
DEFLATE payloads, so their bytes are reproducible across supported Python
versions and hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import struct
import tempfile
import zlib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath


FIXED_ZIP_TIME = (2000, 1, 1, 0, 0, 0)
_MANIFEST_PATH = PurePosixPath("extractors/manifest.json")


def zip_document(entries: Mapping[str, bytes]) -> bytes:
    """Return a byte-stable, stored-entry ZIP document."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, entries[name], compress_type=zipfile.ZIP_STORED)
    return stream.getvalue()


def minimal_pdf(text: str) -> bytes:
    """Create a small one-page PDF with deterministic object offsets."""

    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(b"xref\n0 6\n0000000000 65535 f\n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n\n".encode("ascii"))
    document.extend(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    return bytes(document)


def zlib_stored(payload: bytes) -> bytes:
    """Encode *payload* with RFC 1950 plus uncompressed DEFLATE blocks."""

    blocks = bytearray(b"\x78\x01")
    chunks = [
        payload[index : index + 65_535] for index in range(0, len(payload), 65_535)
    ]
    if not chunks:
        chunks = [b""]
    for index, chunk in enumerate(chunks):
        blocks.append(1 if index + 1 == len(chunks) else 0)
        length = len(chunk)
        blocks.extend(struct.pack("<HH", length, (~length) & 0xFFFF))
        blocks.extend(chunk)
    blocks.extend(struct.pack(">I", zlib.adler32(payload) & 0xFFFFFFFF))
    return bytes(blocks)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def minimal_png() -> bytes:
    """Create a stable, one-pixel RGBA PNG without zlib implementation output."""

    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib_stored(b"\x00\x20\x40\x60\xff")),
            _png_chunk(b"IEND", b""),
        )
    )


def _docx() -> bytes:
    return zip_document(
        {
            "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
            "word/document.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Fixture document paragraph</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
        }
    )


def _pptx() -> bytes:
    return zip_document(
        {
            "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>',
            "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
            "ppt/_rels/presentation.xml.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>',
            "ppt/presentation.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/slides/slide1.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Fixture presentation slide</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
        }
    )


def _xlsx() -> bytes:
    return zip_document(
        {
            "[Content_Types].xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
            "_rels/.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/_rels/workbook.xml.rels": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
            "xl/sharedStrings.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1"><si><t>Fixture value</t></si></sst>',
            "xl/workbook.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Fixture sheet" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/worksheets/sheet1.xml": b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>',
        }
    )


def build_fixtures() -> Mapping[PurePosixPath, bytes]:
    """Return every generated fixture except its checksum manifest."""

    fixtures = {
        PurePosixPath(
            "extractors/core/sample.md"
        ): b"# Fixture markdown\n\nA stable fact.\n",
        PurePosixPath("extractors/core/sample.txt"): b"Fixture plain text.\n",
        PurePosixPath("extractors/core/sample.csv"): b"name,value\nFixture,42\n",
        PurePosixPath("extractors/core/sample.tsv"): b"name\tvalue\nFixture\t42\n",
        PurePosixPath(
            "extractors/core/sample.json"
        ): b'{"fact":"fixture","value":42}\n',
        PurePosixPath(
            "extractors/core/sample.html"
        ): b"<!doctype html><html><body><h1>Fixture HTML</h1><p>A stable fact.</p></body></html>\n",
        PurePosixPath("extractors/core/sample.pdf"): minimal_pdf("Fixture PDF"),
        PurePosixPath("extractors/core/sample.docx"): _docx(),
        PurePosixPath("extractors/core/sample.pptx"): _pptx(),
        PurePosixPath("extractors/core/sample.xlsx"): _xlsx(),
        PurePosixPath("extractors/core/sample.png"): minimal_png(),
        PurePosixPath("extractors/failures/empty.txt"): b"",
        PurePosixPath("extractors/failures/malformed.docx"): b"PK\x03\x04\x14\x00",
        PurePosixPath(
            "extractors/failures/encrypted.pdf"
        ): b"%PDF-1.4\n1 0 obj\n<< /Encrypt 2 0 R >>\nendobj\ntrailer\n<< /Encrypt 2 0 R >>\n%%EOF\n",
        PurePosixPath(
            "extractors/failures/unsupported.bin"
        ): b"fixture\x00unsupported\n",
        PurePosixPath(
            "extractors/expected/README.md"
        ): b"# Extractor fixture expectations\n\nThese bytes are generated by `tests/fixtures/generate_extractors.py`. The integration matrix asserts the retained source state and required anchor kinds.\n",
        PurePosixPath(
            "web/static-page.html"
        ): b"<!doctype html><html><body><h1>Static fixture</h1><p>Retained web fact.</p></body></html>\n",
        PurePosixPath(
            "web/rendered-page-marker.html"
        ): b'<!doctype html><html><body><main data-rendered="true">Rendered fixture marker</main></body></html>\n',
    }
    return dict(sorted(fixtures.items(), key=lambda item: item[0].as_posix()))


def build_manifest(fixtures: Mapping[PurePosixPath, bytes]) -> bytes:
    """Return the compact checksum manifest for generated fixture bytes."""

    files = {
        path.as_posix(): {
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in sorted(
            fixtures.items(), key=lambda item: item[0].as_posix()
        )
    }
    return json.dumps(
        {"schema_version": 1, "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _expected_files() -> Mapping[PurePosixPath, bytes]:
    fixtures = build_fixtures()
    return {
        **fixtures,
        _MANIFEST_PATH: build_manifest(fixtures),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_fixtures(root: Path) -> None:
    """Atomically publish every owned deterministic fixture beneath *root*."""

    for relative, payload in sorted(
        _expected_files().items(), key=lambda item: item[0].as_posix()
    ):
        _atomic_write(root / relative, payload)


def check_fixtures(root: Path) -> tuple[str, ...]:
    """Return sorted discrepancies for the generator-owned fixture surface."""

    expected = _expected_files()
    differences: list[str] = []
    for relative, payload in sorted(
        expected.items(), key=lambda item: item[0].as_posix()
    ):
        path = root / relative
        if not path.is_file():
            differences.append(f"missing: {relative.as_posix()}")
        elif path.read_bytes() != payload:
            differences.append(f"nonmatching: {relative.as_posix()}")
    extractor_root = root / "extractors"
    if extractor_root.is_dir():
        for path in extractor_root.rglob("*"):
            if path.is_file():
                relative = PurePosixPath(path.relative_to(root).as_posix())
                if relative not in expected:
                    differences.append(f"extra: {relative.as_posix()}")
    return tuple(sorted(differences))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write generated fixtures")
    mode.add_argument("--check", action="store_true", help="check generated fixtures")
    arguments = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    if arguments.write:
        write_fixtures(root)
        return 0
    differences = check_fixtures(root)
    for difference in differences:
        print(difference)
    return int(bool(differences))


if __name__ == "__main__":
    raise SystemExit(main())
