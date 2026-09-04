"""Fixed runtime for optional library converters, launched only with inherited input.

The driver is repository infrastructure, not a configurable converter command.
Optional imports occur exclusively inside the selected static handler.
"""

from __future__ import annotations

import html
import importlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable

if __package__ in {None, ""}:
    # -I deliberately excludes cwd and PYTHONPATH. Only this installed runtime's
    # repository root supplies brainlib; no source/config-supplied module path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brainlib.extractors.native import (
    ExtractedPayload,
    ExtractionQualityError,
    numbered_payload,
    section_payload,
    sheets_payload,
)


class _Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def text(self, value: object) -> str:
        text = "" if value is None else str(value)
        self.remaining -= len(text.encode("utf-8")) + 8
        if self.remaining < 0:
            raise ExtractionQualityError(
                "extraction exceeds byte limit", code="output_too_large"
            )
        return text

    def rows(self, rows: Iterable[Iterable[object]]) -> Iterable[list[str]]:
        for row in rows:
            yield [self.text(cell) for cell in row]


def _pdf(module: object, input_path: Path, budget: _Budget) -> ExtractedPayload:
    document = module.open(str(input_path))
    try:
        if document.is_encrypted:
            raise ExtractionQualityError("encrypted PDF input", code="converter_failed")
        return numbered_payload(
            (budget.text(page.get_text("text")) for page in document), "page"
        )
    finally:
        document.close()


def _table_text(rows: Iterable[Iterable[object]], budget: _Budget) -> str:
    values = [
        [
            html.escape(cell, quote=False).replace("|", "\\|").replace("\n", "<br>")
            for cell in row
        ]
        for row in budget.rows(rows)
    ]
    if not values:
        return ""
    width = max(map(len, values))
    lines = [
        "| " + " | ".join([f"Column {index}" for index in range(1, width + 1)]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend(
        "| " + " | ".join([*row, *([""] * (width - len(row)))]) + " |" for row in values
    )
    return "\n".join(lines)


def _docx(module: object, input_path: Path, budget: _Budget) -> ExtractedPayload:
    parts: list[str] = []
    with input_path.open("rb") as stream:
        document = module.Document(stream)
        for block in document.iter_inner_content():
            if hasattr(block, "text"):
                text = budget.text(block.text)
                style = getattr(getattr(block, "style", None), "name", "")
                match = re.fullmatch(r"Heading ([1-6])", style or "")
                parts.append(("#" * int(match[1]) + " " if match else "") + text)
            else:
                parts.append(
                    _table_text(
                        ([cell.text for cell in row.cells] for row in block.rows),
                        budget,
                    )
                )
    return section_payload("\n\n".join(parts))


def _pptx(module: object, input_path: Path, budget: _Budget) -> ExtractedPayload:
    def shape_text(shapes: Iterable[object]) -> Iterable[str]:
        for shape in shapes:
            if getattr(shape, "has_text_frame", False):
                yield budget.text(shape.text)
            if getattr(shape, "has_table", False):
                yield _table_text(
                    ([cell.text for cell in row.cells] for row in shape.table.rows),
                    budget,
                )
            if hasattr(shape, "shapes"):
                yield from shape_text(shape.shapes)

    with input_path.open("rb") as stream:
        presentation = module.Presentation(stream)
        return numbered_payload(
            ("\n\n".join(shape_text(slide.shapes)) for slide in presentation.slides),
            "slide",
        )


def _xlsx(module: object, input_path: Path, budget: _Budget) -> ExtractedPayload:
    with input_path.open("rb") as stream:
        workbook = module.load_workbook(stream, read_only=True, data_only=False)
        try:
            return sheets_payload(
                (
                    budget.text(sheet.title),
                    budget.rows(sheet.iter_rows(values_only=True)),
                )
                for sheet in workbook.worksheets
            )
        finally:
            workbook.close()


@dataclass(frozen=True)
class PythonAdapter:
    distribution: str
    module: str
    handler: Callable[[object, Path, _Budget], ExtractedPayload]


PYTHON_ADAPTERS = MappingProxyType(
    {
        "python.pymupdf": PythonAdapter("PyMuPDF", "fitz", _pdf),
        "python.python-docx": PythonAdapter("python-docx", "docx", _docx),
        "python.python-pptx": PythonAdapter("python-pptx", "pptx", _pptx),
        "python.openpyxl": PythonAdapter("openpyxl", "openpyxl", _xlsx),
    }
)


def extract_python_payload(
    converter_id: str, input_path: Path, *, max_output_bytes: int
) -> ExtractedPayload:
    adapter = PYTHON_ADAPTERS.get(converter_id)
    if (
        adapter is None
        or os.name != "posix"
        or re.fullmatch(r"/dev/fd/[0-9]+", str(input_path)) is None
    ):
        raise ExtractionQualityError(
            "unsupported converter or unpinned input", code="unsupported_converter"
        )
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ExtractionQualityError("invalid byte limit", code="output_too_large")
    if not stat.S_ISREG(os.fstat(int(input_path.name)).st_mode):
        raise ExtractionQualityError(
            "input is not an inherited regular file", code="malformed_input"
        )
    module = importlib.import_module(adapter.module)
    payload = adapter.handler(module, input_path, _Budget(max_output_bytes))
    if len(payload.markdown.encode("utf-8")) > max_output_bytes:
        raise ExtractionQualityError(
            "extraction exceeds byte limit", code="output_too_large"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) != 4:
            raise ValueError(
                "expected converter, inherited input, staging output, byte limit"
            )
        converter_id, input_name, output_name, limit = arguments
        output = Path(output_name)
        if not output.is_absolute() or output.name != "converted.md":
            raise ValueError("invalid staging output")
        payload = extract_python_payload(
            converter_id, Path(input_name), max_output_bytes=int(limit)
        )
        parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                output.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload.markdown.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(parent)
        return 0
    except Exception as error:
        # Library exceptions can contain arbitrarily large source data. Emit only
        # a bounded type label; the parent supplies the stable diagnostic code.
        sys.stderr.write(f"Python converter failed: {type(error).__name__[:120]}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
