from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable

from ..contracts import Anchor
from ..diagnostics import Diagnostic


ANCHOR_KINDS = frozenset({"line", "page", "slide", "sheet", "section", "row", "block"})
_ANCHOR_VALUE = re.compile(r"[A-Za-z0-9._:/-]+")
MARKER = re.compile(
    r'<a id="(line|page|slide|sheet|section|row|block):([A-Za-z0-9._:/-]+)"></a>'
)


class ExtractionQualityError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_anchors") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExtractedPayload:
    markdown: str
    anchors: tuple[Anchor, ...]
    warning: Diagnostic | None


def anchor_html_id(anchor: Anchor) -> str:
    if (
        not isinstance(anchor, Anchor)
        or anchor.kind not in ANCHOR_KINDS
        or type(anchor.value) is not str
        or _ANCHOR_VALUE.fullmatch(anchor.value) is None
    ):
        raise ExtractionQualityError("invalid anchor value or kind")
    return f'<a id="{anchor.kind}:{anchor.value}"></a>'


def decode_utf8(body: bytes, *, max_output_bytes: int) -> str:
    if len(body) > max_output_bytes:
        raise ExtractionQualityError(
            "extraction exceeds byte limit", code="output_too_large"
        )
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExtractionQualityError(
            "extraction is not UTF-8", code="invalid_utf8"
        ) from error
    if "\0" in text:
        raise ExtractionQualityError(
            "extraction contains NUL bytes", code="malformed_input"
        )
    return text


def require_text(text: str) -> None:
    if not text.strip():
        raise ExtractionQualityError("extraction is empty", code="empty_extraction")


def joined_payload(parts: Iterable[str], anchors: Iterable[Anchor]) -> ExtractedPayload:
    return ExtractedPayload("\n\n".join(parts).rstrip() + "\n", tuple(anchors), None)


def text_payload(text: str) -> ExtractedPayload:
    require_text(text)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    anchors = tuple(Anchor("line", str(index)) for index in range(1, len(lines) + 1))
    # Source text may quote an exact generated anchor. Preserve it as visible
    # evidence without allowing it to masquerade as extraction metadata.
    normalized = MARKER.sub(
        lambda match: html.escape(match[0], quote=False), normalized
    )
    # Physical lines may belong to tables, lists, code spans, or other multiline
    # Markdown structures. Keep their body intact and expose stable navigable
    # line identifiers in a top-level prelude instead of splicing into syntax.
    markers = "\n".join(anchor_html_id(anchor) for anchor in anchors)
    return ExtractedPayload(f"{markers}\n\n{normalized}", anchors, None)


def table_payload(
    rows: Iterable[Iterable[object]], *, prefix: str = ""
) -> ExtractedPayload:
    values = [["" if cell is None else str(cell) for cell in row] for row in rows]
    if not values or not any(cell.strip() for row in values for cell in row):
        raise ExtractionQualityError("table is empty", code="empty_extraction")
    width = max(len(row) for row in values)
    anchors = tuple(
        Anchor("row", f"{prefix}{index}") for index in range(1, len(values) + 1)
    )
    lines = [
        "| "
        + " | ".join(
            [f"Column {index}" for index in range(1, width + 1)] + ["Source row"]
        )
        + " |",
        "| " + " | ".join(["---"] * (width + 1)) + " |",
    ]
    for row, anchor in zip(values, anchors):
        cells = [
            html.escape(cell, quote=False)
            .replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("\r\n", "<br>")
            .replace("\n", "<br>")
            .replace("\r", "<br>")
            for cell in row
        ]
        cells.extend([""] * (width - len(row)))
        lines.append("| " + " | ".join([*cells, anchor_html_id(anchor)]) + " |")
    return ExtractedPayload("\n".join(lines) + "\n", anchors, None)


def tabular_payload(
    text: str, *, delimiter: str = ",", prefix: str = ""
) -> ExtractedPayload:
    require_text(text)
    try:
        return table_payload(
            csv.reader(io.StringIO(text), delimiter=delimiter, strict=True),
            prefix=prefix,
        )
    except csv.Error as error:
        raise ExtractionQualityError(
            "malformed delimited input", code="malformed_input"
        ) from error


def sheets_payload(
    sheets: Iterable[tuple[str, Iterable[Iterable[object]]]],
) -> ExtractedPayload:
    parts: list[str] = []
    anchors: list[Anchor] = []
    for index, (name, rows) in enumerate(sheets, 1):
        anchor = Anchor("sheet", str(index))
        anchors.append(anchor)
        parts.append(f"{anchor_html_id(anchor)}\n\n## {html.escape(name, quote=False)}")
        try:
            table = table_payload(rows, prefix=f"{index}/")
        except ExtractionQualityError as error:
            if error.code != "empty_extraction":
                raise
            parts.append("(Empty sheet)")
        else:
            parts.append(table.markdown.rstrip())
            anchors.extend(table.anchors)
    return joined_payload(parts, anchors)


def json_payload(text: str, *, lines: bool = False) -> ExtractedPayload:
    require_text(text)

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON number: {value}")

    try:
        values = (
            [
                json.loads(line, parse_constant=reject_constant)
                for line in text.splitlines()
                if line.strip()
            ]
            if lines
            else [json.loads(text, parse_constant=reject_constant)]
        )
        bodies = [
            json.dumps(
                value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
            )
            for value in values
        ]
    except (ValueError, RecursionError) as error:
        raise ExtractionQualityError(
            "malformed JSON input", code="malformed_input"
        ) from error
    anchors = tuple(Anchor("block", str(index)) for index in range(1, len(bodies) + 1))
    parts = []
    for anchor, body in zip(anchors, bodies):
        fence = "`" * max(
            3, max((len(match[0]) + 1 for match in re.finditer(r"`+", body)), default=3)
        )
        parts.append(f"{anchor_html_id(anchor)}\n\n{fence}json\n{body}\n{fence}")
    return joined_payload(parts, anchors)


class SafeHTMLParser(HTMLParser):
    """Retain visible text/structure only; attributes and active content are discarded."""

    _HIDDEN = frozenset(
        {
            "head",
            "script",
            "style",
            "noscript",
            "template",
            "iframe",
            "object",
            "svg",
            "math",
        }
    )
    _BLOCKS = frozenset(
        {
            "p",
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "tr",
            "pre",
            "blockquote",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        if self.hidden:
            return
        if re.fullmatch("h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self._BLOCKS:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag in self._BLOCKS or re.fullmatch("h[1-6]", tag):
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data.replace("<", "&lt;").replace(">", "&gt;"))

    def markdown(self) -> str:
        return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(self.parts)).strip()


def html_text(text: str) -> str:
    parser = SafeHTMLParser()
    parser.feed(text)
    parser.close()
    return parser.markdown()


def section_payload(text: str) -> ExtractedPayload:
    require_text(text)
    sections = [
        value.strip() for value in re.split(r"(?m)(?=^#{1,6} )", text) if value.strip()
    ]
    return numbered_payload(sections, "section")


def numbered_payload(parts: Iterable[str], kind: str) -> ExtractedPayload:
    bodies = [
        MARKER.sub(lambda match: html.escape(match[0], quote=False), body)
        for body in parts
    ]
    require_text("".join(bodies))
    anchors = tuple(Anchor(kind, str(index)) for index in range(1, len(bodies) + 1))
    return joined_payload(
        (
            f"{anchor_html_id(anchor)}\n\n{body.strip()}"
            for anchor, body in zip(anchors, bodies)
        ),
        anchors,
    )
