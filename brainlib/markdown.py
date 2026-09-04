"""CommonMark structure with raw-byte scanning for wiki and citation contracts."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, autolink, backtick, image, link


@dataclass(frozen=True)
class MarkdownSourceSpan:
    """An exact raw-source range with one-based physical line coordinates."""

    start: int
    end: int
    line: int
    end_line: int


@dataclass(frozen=True)
class MarkdownVisibleText:
    """A contiguous raw source chunk that CommonMark proved is visible text."""

    text: str
    source_span: MarkdownSourceSpan


@dataclass(frozen=True)
class MarkdownLink:
    destination: str
    text: str
    line: int
    # These additive raw coordinates deliberately do not change the existing
    # positional construction, repr, or equality contract for MarkdownLink.
    source_span: MarkdownSourceSpan | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )
    label_span: MarkdownSourceSpan | None = field(
        default=None, kw_only=True, compare=False, repr=False
    )


@dataclass(frozen=True)
class MarkdownHeading:
    level: int
    text: str
    line: int
    # One-based inclusive last physical source line consumed by this heading.
    # Excluding it from repr/equality preserves the original public value contract.
    end_line: int | None = field(default=None, kw_only=True, compare=False, repr=False)


@dataclass(frozen=True)
class CitationMarker:
    citation_id: str
    line: int


@dataclass(frozen=True)
class MarkdownDiagnostic:
    code: str
    message: str
    line: int


@dataclass(frozen=True)
class MarkdownScan:
    links: tuple[MarkdownLink, ...]
    headings: tuple[MarkdownHeading, ...]
    citation_markers: tuple[CitationMarker, ...]
    citation_definitions: tuple[CitationMarker, ...] = ()
    diagnostics: tuple[MarkdownDiagnostic, ...] = ()
    visible_text_spans: tuple[MarkdownVisibleText, ...] = ()


@dataclass(frozen=True)
class _SourceLine:
    offset: int
    text: str


@dataclass
class _ImageScope:
    links_start: int = 0
    generation: int = 0


@dataclass
class _RawInlineWitness:
    spans: set[tuple[str, int, int]] = field(default_factory=set)
    # Typed absolute source spans identify the local destination/title interiors.
    # Keep these even when an image removes its descendants from graph output.
    nonrendered_intervals: dict[tuple[str, int, int], tuple[int, int]] = field(
        default_factory=dict
    )
    label_delimiters: dict[tuple[str, int, int], tuple[int, int]] = field(
        default_factory=dict
    )
    first_unrepresented_link_line: int | None = None


class _InlineAmbiguity(ValueError):
    def __init__(
        self, line: int, message: str = "Markdown link crosses physical source lines."
    ) -> None:
        self.diagnostic = MarkdownDiagnostic(
            "markdown_reconciliation_ambiguous",
            message,
            line,
        )
        super().__init__(self.diagnostic.message)


class _CommonMarkParser:
    def parse(self, body: str) -> list[object]:
        # Each structural level consumes source characters. A source-sized limit
        # avoids truncation without changing shared parser options between scans.
        parser = MarkdownIt("commonmark", {"maxNesting": len(body) + 1})
        for name, rule, kind in (
            ("link", link, "link_open"),
            ("image", image, "image"),
            ("backticks", backtick, "code_inline"),
            ("autolink", autolink, "link_open"),
        ):
            parser.inline.ruler.at(name, _record_inline_span(rule, kind))
        return parser.parse(body)


def _record_inline_span(
    rule: Callable[[StateInline, bool], bool], kind: str
) -> Callable[[StateInline, bool], bool]:
    """Observe successful CommonMark rules without changing their decisions."""

    def recorded(state: StateInline, silent: bool) -> bool:
        start = state.pos
        token_index = len(state.tokens) + bool(state.pending)
        matched = rule(state, silent)
        if not silent and matched and token_index < len(state.tokens):
            token = state.tokens[token_index]
            if token.type == kind:
                token.meta["raw_span"] = (start, state.pos)
                if kind == "link_open":
                    state.tokens[-1].meta["raw_span"] = (start, state.pos)
        return matched

    return recorded


_PARSER = _CommonMarkParser()
_DEFINITION_START = re.compile(r"^ {0,3}\[\^([^\]\r\n]+)\]:")
_MARKER = re.compile(r"\[\^([A-Za-z0-9][A-Za-z0-9_-]*)\]")


def _escaped(text: str, position: int) -> bool:
    start = position
    while start and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 == 1


def _source_lines(markdown: str) -> tuple[_SourceLine, ...]:
    """Match the parser's physical CR/LF lines without treating Unicode as newlines."""
    return tuple(
        _SourceLine(match.start(), match.group().rstrip("\r\n"))
        for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", markdown)
        if match.start() != match.end()
    )


def _without_complete_frontmatter(
    markdown: str, lines: tuple[_SourceLine, ...]
) -> tuple[str, int]:
    if lines and lines[0].text == "---":
        for index in range(1, len(lines)):
            if lines[index].text == "---":
                offset = (
                    lines[index + 1].offset if index + 1 < len(lines) else len(markdown)
                )
                return markdown[offset:], index + 1
    return markdown, 0


def _valid_source_map(source_map: object, line_count: int) -> bool:
    return (
        isinstance(source_map, (list, tuple))
        and len(source_map) == 2
        and all(type(value) is int for value in source_map)
        and 0 <= source_map[0] < source_map[1] <= line_count
    )


def _range_diagnostic(
    source_map: object, *, line_offset: int, kind: str
) -> MarkdownDiagnostic:
    start = 0
    if (
        isinstance(source_map, (list, tuple))
        and source_map
        and type(source_map[0]) is int
    ):
        start = max(source_map[0], 0)
    return MarkdownDiagnostic(
        "markdown_reconciliation_ambiguous",
        f"Markdown parser returned an invalid {kind} source range.",
        start + line_offset + 1,
    )


def _approved_inline_ranges(
    tokens: Iterable[object], *, body_line_count: int, line_offset: int
) -> tuple[tuple[tuple[int, int], ...], tuple[MarkdownDiagnostic, ...]]:
    """Translate only proven, ordered inline maps; never guess block membership."""
    ranges: list[tuple[int, int]] = []
    previous_end = 0
    for token in tokens:
        if getattr(token, "type", None) != "inline":
            continue
        source_map = getattr(token, "map", None)
        if (
            not _valid_source_map(source_map, body_line_count)
            or source_map[0] < previous_end
        ):
            return (), (
                _range_diagnostic(source_map, line_offset=line_offset, kind="inline"),
            )
        start, end = source_map
        ranges.append((start + line_offset, end + line_offset))
        previous_end = end
    return tuple(ranges), ()


def _headings_from_tokens(
    tokens: list[object], lines: tuple[_SourceLine, ...], line_offset: int
) -> tuple[tuple[MarkdownHeading, ...], tuple[MarkdownDiagnostic, ...]]:
    line_count = len(lines) - line_offset
    headings: list[MarkdownHeading] = []
    previous_end = 0
    for index, token in enumerate(tokens):
        if getattr(token, "type", None) != "heading_open":
            continue
        source_map = getattr(token, "map", None)
        inline = tokens[index + 1] if index + 1 < len(tokens) else None
        closing = tokens[index + 2] if index + 2 < len(tokens) else None
        inline_map = getattr(inline, "map", None)
        tag = getattr(token, "tag", None)
        content = getattr(inline, "content", None)
        if (
            not _valid_source_map(source_map, line_count)
            or source_map[0] < previous_end
            or tag not in ("h1", "h2", "h3", "h4", "h5", "h6")
            or getattr(inline, "type", None) != "inline"
            or getattr(closing, "type", None) != "heading_close"
            or getattr(closing, "tag", None) != tag
            or not _valid_source_map(inline_map, line_count)
            or inline_map[0] != source_map[0]
            or inline_map[1] > source_map[1]
            or not isinstance(content, str)
        ):
            return (), (
                _range_diagnostic(source_map, line_offset=line_offset, kind="heading"),
            )
        # The parser selects the heading and strips its container/heading syntax.
        # Reconcile that display spelling against its mapped original raw lines.
        raw_lines = [
            line.text
            for line in lines[inline_map[0] + line_offset : inline_map[1] + line_offset]
        ]
        text_lines = content.split("\n")
        if len(raw_lines) != len(text_lines) or any(
            text not in raw for text, raw in zip(text_lines, raw_lines)
        ):
            return (), (
                _range_diagnostic(source_map, line_offset=line_offset, kind="heading"),
            )
        text = " ".join(
            raw[raw.index(part) : raw.index(part) + len(part)].strip()
            for part, raw in zip(text_lines, raw_lines)
        )
        headings.append(
            MarkdownHeading(
                int(tag[1]),
                text,
                source_map[0] + line_offset + 1,
                end_line=source_map[1] + line_offset,
            )
        )
        previous_end = source_map[1]
    return tuple(headings), ()


def _destination_ends(block: str) -> dict[int, int]:
    """Index raw destinations once; malformed candidates never rescan a suffix."""
    size = len(block)
    escaped = [False] * size
    parentheses: dict[int, int] = {}
    openers: list[int] = []
    stack: list[int] = []
    index = 0
    while index < size:
        char = block[index]
        if char == "\\" and index + 1 < size and block[index + 1] not in "\r\n":
            escaped[index + 1] = True
            index += 2
            continue
        if char == "(":
            openers.append(index)
            stack.append(index)
        elif char == ")" and stack:
            parentheses[stack.pop()] = index
        index += 1

    # A suffix either reaches its next unescaped delimiter in constant time,
    # jumps over a proven balanced group, or is already known to be invalid.
    stops: list[int | None] = [size] * (size + 1)
    after_space = [size] * (size + 1)
    angles: dict[int, int] = {}
    titles: dict[int, int] = {}
    next_angle: int | None = None
    next_parenthesis: int | None = None
    next_quotes: dict[str, int] = {}
    for index in range(size - 1, -1, -1):
        char = block[index]
        after_space[index] = after_space[index + 1] if char in " \t\r\n" else index
        if char == "\\" and not escaped[index] and index + 1 < size:
            width = 3 if block[index + 1 : index + 3] == "\r\n" else 2
            stops[index] = None if block[index + 1] == " " else stops[index + width]
        elif char == "(" and not escaped[index]:
            closing = parentheses.get(index)
            stops[index] = (
                stops[closing + 1]
                if closing is not None and stops[index + 1] == closing
                else None
            )
        elif char == ")" or char in " \t" or ord(char) < 32 or ord(char) == 127:
            stops[index] = index
        else:
            stops[index] = stops[index + 1]
        if char in "\r\n":
            next_angle = None
        elif not escaped[index]:
            if char in "<>":
                if char == "<" and next_angle is not None and block[next_angle] == ">":
                    angles[index] = next_angle + 1
                next_angle = index
            if char in "()":
                if (
                    char == "("
                    and next_parenthesis is not None
                    and block[next_parenthesis] == ")"
                ):
                    titles[index] = next_parenthesis + 1
                next_parenthesis = index
            if char in "\"'":
                if char in next_quotes:
                    titles[index] = next_quotes[char] + 1
                next_quotes[char] = index

    destinations: dict[int, int] = {}
    for opener in openers:
        start = opener + 1
        position = after_space[start]
        end = (
            angles.get(position)
            if position < size and block[position] == "<"
            else stops[position]
        )
        if end is None:
            continue
        position = after_space[end]
        if position < size and block[position] != ")" and position > end:
            title_end = titles.get(position)
            if title_end is None:
                continue
            position = after_space[title_end]
        if position < size and block[position] == ")":
            destinations[start] = position
    return destinations


def _scan_inline_block(
    block: str,
    *,
    line_offset: int = 0,
    source_offset: int = 0,
    witness: _RawInlineWitness | None = None,
) -> tuple[str, tuple[tuple[int, int, int, int, int], ...]]:
    """Consume code and raw links in order, only inside an approved inline block."""
    masked = list(block)
    if witness is None:
        witness = _RawInlineWitness()
    links: list[tuple[int, int, int, int, int]] = []
    scope = _ImageScope()
    openers: list[tuple[int, bool, int, _ImageScope]] = []
    destinations = _destination_ends(block)
    source_lines = _source_lines(block)
    source_line_at = [0] * len(block)
    for number, source_line in enumerate(source_lines):
        end = (
            source_lines[number + 1].offset
            if number + 1 < len(source_lines)
            else len(block)
        )
        source_line_at[source_line.offset : end] = [number] * (end - source_line.offset)
    code_ends: dict[int, int] = {}
    next_runs: dict[int, re.Match[str]] = {}
    for run in reversed(list(re.finditer(r"`+", block))):
        length = len(run.group())
        escaped_prefix = int(_escaped(block, run.start()))
        opener_length = length - escaped_prefix
        if opener_length in next_runs:
            code_ends[run.start() + escaped_prefix] = next_runs[opener_length].end()
        next_runs[length] = run
    index = 0
    while index < len(block):
        char = block[index]
        if char == "\\":
            index += 1 if block[index + 1 : index + 2] in ("\r", "\n") else 2
            continue
        if index in code_ends:
            end = code_ends[index]
            witness.spans.add(
                ("code_inline", source_offset + index, source_offset + end)
            )
            for offset in range(index, end):
                if block[offset] not in "\r\n":
                    masked[offset] = " "
            index = end
            continue
        if char == "[":
            is_image = (
                index > 0 and block[index - 1] == "!" and not _escaped(block, index - 1)
            )
            openers.append((index, is_image, scope.generation, scope))
            if is_image:
                scope = _ImageScope(links_start=len(links))
        elif char == "]" and openers:
            label_opener, is_image, generation, parent_scope = openers.pop()
            label_start = label_opener + 1
            active = is_image or generation == scope.generation
            end = None
            if block[index + 1 : index + 2] == "(":
                destination_start = index + 2
                end = destinations.get(destination_start)
                if (
                    active
                    and end is None
                    and witness.first_unrepresented_link_line is None
                ):
                    witness.first_unrepresented_link_line = (
                        line_offset + source_line_at[label_opener] + 1
                    )
            if is_image:
                if end is not None:
                    # Image alt text does not contribute graph links. Its inner
                    # links also do not invalidate labels outside the image.
                    del links[scope.links_start :]
                elif scope.generation:
                    # A failed image is ordinary text; expose its actual links.
                    parent_scope.generation += 1
                scope = parent_scope
            if active and end is not None:
                line = source_line_at[label_opener]
                if source_line_at[end] != line:
                    raise _InlineAmbiguity(line_offset + line + 1)
                span = (
                    "image" if is_image else "link_open",
                    source_offset + label_opener - int(is_image),
                    source_offset + end + 1,
                )
                witness.spans.add(span)
                witness.nonrendered_intervals[span] = (destination_start, end)
                witness.label_delimiters[span] = (label_opener, index)
                if not is_image:
                    links.append((label_start, index, destination_start, end, line))
                    scope.generation += 1
                index = end + 1
                continue
        index += 1
    return "".join(masked), tuple(links)


def _inline_projection(
    token: object, lines: tuple[_SourceLine, ...], start: int, end: int
) -> tuple[set[tuple[str, int, int]], tuple[int | None, ...]]:
    """Project parser structure and every inline source character to raw offsets."""
    line = start + 1
    content = getattr(token, "content", None)
    children = getattr(token, "children", None)
    if not isinstance(content, str) or not isinstance(children, (list, tuple)):
        raise _InlineAmbiguity(
            line, "Markdown parser returned invalid inline children."
        )

    # Container stripping is parser-owned. A unique exact substring per mapped
    # line proves the translation without recreating any container grammar.
    # Tab-expanded indentation may have no raw counterpart: leave those prefix
    # positions unmapped and require every semantic span endpoint to be real.
    parts = content.split("\n")
    if len(parts) != end - start:
        raise _InlineAmbiguity(line, "Markdown inline content has an invalid line map.")
    offsets: list[int | None] = []
    for number, part in enumerate(parts, start):
        source = lines[number]
        position = source.text.find(part)
        aligned = part
        if position < 0:
            aligned = part.lstrip(" \t")
            position = source.text.find(aligned) if aligned else -1
        if position < 0 or (aligned and source.text.find(aligned, position + 1) >= 0):
            raise _InlineAmbiguity(
                number + 1, "Markdown inline content cannot be aligned to raw source."
            )
        offsets.extend([None] * (len(part) - len(aligned)))
        offsets.extend(
            range(source.offset + position, source.offset + position + len(aligned))
        )
        if number + 1 < end:
            offsets.append(source.offset + len(source.text))

    pending = [(child, 0, len(content)) for child in reversed(children)]
    spans: set[tuple[str, int, int]] = set()
    open_links: list[tuple[int, int]] = []
    while pending:
        child, base, scope_end = pending.pop()
        kind = getattr(child, "type", None)
        if kind == "html_inline":
            raise _InlineAmbiguity(
                line, "Inline HTML cannot be reconciled with raw Markdown spans."
            )
        if kind not in ("link_open", "link_close", "code_inline", "image"):
            continue
        metadata = getattr(child, "meta", None)
        span = metadata.get("raw_span") if isinstance(metadata, dict) else None
        if not (
            isinstance(span, tuple)
            and len(span) == 2
            and all(type(value) is int for value in span)
            and 0 <= span[0] < span[1] <= scope_end - base
        ):
            raise _InlineAmbiguity(
                line, "Markdown parser returned an invalid inline span."
            )
        left, right = span[0] + base, span[1] + base
        if kind == "link_close":
            if not open_links or open_links.pop() != (left, right):
                raise _InlineAmbiguity(
                    line, "Markdown parser returned unbalanced inline links."
                )
            continue
        if kind == "link_open":
            open_links.append((left, right))
        raw_start, raw_end = offsets[left], offsets[right - 1]
        if raw_start is None or raw_end is None:
            raise _InlineAmbiguity(
                line, "Markdown inline span has no exact raw source endpoints."
            )
        projected = kind, raw_start, raw_end + 1
        if projected in spans:
            raise _InlineAmbiguity(
                line, "Markdown parser returned duplicate inline spans."
            )
        spans.add(projected)
        if kind == "image":
            image_children = getattr(child, "children", None)
            image_content = getattr(child, "content", None)
            if (
                not isinstance(image_content, str)
                or content[left : left + 2] != "!["
                or left + 2 + len(image_content) >= right
                or content[left + 2 + len(image_content)] != "]"
            ):
                raise _InlineAmbiguity(
                    line, "Markdown image content cannot be aligned to raw source."
                )
            if image_children is None and image_content == "":
                continue
            if not isinstance(image_children, (list, tuple)) or (
                image_content and not image_children
            ):
                raise _InlineAmbiguity(
                    line, "Markdown parser returned invalid image children."
                )
            pending.extend(
                (nested, left + 2, left + 2 + len(image_content))
                for nested in reversed(image_children)
            )
    if open_links:
        raise _InlineAmbiguity(
            line, "Markdown parser returned unbalanced inline links."
        )
    return spans, tuple(offsets)


def _inline_expectations(
    token: object, lines: tuple[_SourceLine, ...], start: int, end: int
) -> set[tuple[str, int, int]]:
    """Keep the established parser-span projection seam for existing callers."""
    spans, _ = _inline_projection(token, lines, start, end)
    return spans


def _source_span(
    line_offsets: tuple[int, ...], source_length: int, start: int, end: int
) -> MarkdownSourceSpan:
    """Build a physical CR/LF source span without interpreting Unicode separators."""
    if not line_offsets or not (0 <= start <= end <= source_length):
        raise _InlineAmbiguity(
            1, "Markdown parser produced an invalid raw source position."
        )

    def line_at(offset: int) -> int:
        return max(bisect_right(line_offsets, offset) - 1, 0) + 1

    # End is exclusive. For nonempty spans the final character determines the
    # inclusive end line; an empty link label belongs to its insertion line.
    return MarkdownSourceSpan(
        start,
        end,
        line_at(start),
        line_at(end - 1 if end > start else start),
    )


def _visible_text_from_projection(
    markdown: str,
    line_offsets: tuple[int, ...],
    raw: str,
    raw_start: int,
    offsets: tuple[int | None, ...],
    witness: _RawInlineWitness,
    links: tuple[MarkdownLink, ...],
) -> tuple[MarkdownVisibleText, ...]:
    """Subtract parser-proven hidden syntax from parser-projected raw content."""
    projected = bytearray(len(raw))
    raw_end = raw_start + len(raw)
    for offset in offsets:
        if offset is None:
            continue
        if not raw_start <= offset < raw_end:
            raise _InlineAmbiguity(
                1, "Markdown inline content projected outside its source range."
            )
        projected[offset - raw_start] = 1

    visible = bytearray(projected)

    def clear(start: int, end: int) -> None:
        if not raw_start <= start <= end <= raw_end:
            raise _InlineAmbiguity(
                1, "Markdown inline span projected outside its source range."
            )
        visible[start - raw_start : end - raw_start] = b"\0" * (end - start)

    # A reconciled link contributes only its label. Code and image nodes are
    # fully hidden, including descendants that the raw lexer correctly found
    # while reconciling an image scope.
    for kind, start, end in witness.spans:
        if kind in ("link_open", "code_inline", "image"):
            clear(start, end)

    witness_links = {
        (start, end)
        for kind, start, end in witness.spans
        if kind == "link_open"
    }
    for direct_link in links:
        source_span, label_span = direct_link.source_span, direct_link.label_span
        if source_span is None or label_span is None:
            raise _InlineAmbiguity(
                1, "Markdown link is missing its reconciled raw source span."
            )
        if (
            (source_span.start, source_span.end) not in witness_links
            or not source_span.start <= label_span.start <= label_span.end <= source_span.end
        ):
            raise _InlineAmbiguity(
                1, "Markdown link labels do not match reconciled raw source spans."
            )
        label_start = label_span.start - raw_start
        label_end = label_span.end - raw_start
        visible[label_start:label_end] = projected[label_start:label_end]

    # Restoring a containing direct-link label must never revive inline code or
    # a valid image nested in that label.
    for kind, start, end in witness.spans:
        if kind in ("code_inline", "image"):
            clear(start, end)

    result: list[MarkdownVisibleText] = []
    chunk_start: int | None = None
    for index, character in enumerate(raw):
        if visible[index] and character not in "\r\n":
            if chunk_start is None:
                chunk_start = index
            continue
        if chunk_start is not None:
            start, end = raw_start + chunk_start, raw_start + index
            result.append(
                MarkdownVisibleText(
                    markdown[start:end],
                    _source_span(line_offsets, len(markdown), start, end),
                )
            )
            chunk_start = None
    if chunk_start is not None:
        start, end = raw_start + chunk_start, raw_end
        result.append(
            MarkdownVisibleText(
                markdown[start:end],
                _source_span(line_offsets, len(markdown), start, end),
            )
        )
    return tuple(result)


def _scan_raw_inline_ranges(
    markdown: str,
    lines: tuple[_SourceLine, ...],
    ranges: tuple[tuple[int, int], ...],
    inline_tokens: tuple[object, ...],
) -> tuple[
    tuple[MarkdownLink, ...],
    tuple[CitationMarker, ...],
    tuple[CitationMarker, ...],
    tuple[MarkdownVisibleText, ...],
]:
    links: list[MarkdownLink] = []
    markers: list[CitationMarker] = []
    definitions: list[CitationMarker] = []
    visible_text_spans: list[MarkdownVisibleText] = []
    line_offsets = tuple(source_line.offset for source_line in lines)

    def scan_inline(
        start: int,
        end: int,
        expected: set[tuple[str, int, int]],
        offsets: tuple[int | None, ...],
    ) -> None:
        if start == end:
            return
        block = lines[start:end]
        raw_start = lines[start].offset
        end_offset = lines[end].offset if end < len(lines) else len(markdown)
        raw = markdown[raw_start:end_offset]
        witness = _RawInlineWitness()
        masked, spans = _scan_inline_block(
            raw, line_offset=start, source_offset=raw_start, witness=witness
        )
        if witness.spans != expected:
            raise _InlineAmbiguity(
                witness.first_unrepresented_link_line or start + 1,
                "Markdown parser and raw inline scan disagree about source spans.",
            )
        block_links: list[MarkdownLink] = []
        for label_start, label_end, destination_start, destination_end, line in spans:
            link = MarkdownLink(
                raw[destination_start:destination_end],
                raw[label_start:label_end],
                start + line + 1,
                source_span=_source_span(
                    line_offsets,
                    len(markdown),
                    raw_start + label_start - 1,
                    raw_start + destination_end + 1,
                ),
                label_span=_source_span(
                    line_offsets,
                    len(markdown),
                    raw_start + label_start,
                    raw_start + label_end,
                ),
            )
            links.append(link)
            block_links.append(link)
        visible_text_spans.extend(
            _visible_text_from_projection(
                markdown,
                line_offsets,
                raw,
                raw_start,
                offsets,
                witness,
                tuple(block_links),
            )
        )
        marker_source = list(masked)
        for opening, closing in witness.label_delimiters.values():
            # Only the reconciled outer brackets are syntax; label/alt interiors
            # remain searchable, including their own literal marker brackets.
            marker_source[opening] = marker_source[closing] = " "
        for destination_start, destination_end in witness.nonrendered_intervals.values():
            # Typed span equality above proves every link/image interior before
            # masking, including descendants suppressed from graph output. The
            # lexer consumes each disjoint interior once; labels remain visible
            # and single-physical-line interiors contain no CR/LF characters.
            marker_source[destination_start:destination_end] = " " * (
                destination_end - destination_start
            )
        for number, (source, masked_source) in enumerate(
            zip(block, _source_lines("".join(marker_source))), start + 1
        ):
            original, line = source.text, masked_source.text
            definition = _DEFINITION_START.match(original)
            if definition and line[: definition.end()] != original[: definition.end()]:
                definition = None
            if definition:
                # Definitions are one-line application syntax, recognized only
                # after the complete inline block has established code scope.
                definitions.append(CitationMarker(definition.group(1), number))
            for match in _MARKER.finditer(line):
                if not _escaped(original, match.start()) and not (
                    definition is not None and match.start() < definition.end()
                ):
                    markers.append(CitationMarker(match.group(1), number))

    for (start, end), token in zip(ranges, inline_tokens):
        expected, offsets = _inline_projection(token, lines, start, end)
        scan_inline(start, end, expected, offsets)
    return (
        tuple(links),
        tuple(markers),
        tuple(definitions),
        tuple(visible_text_spans),
    )


def scan_markdown(markdown: str) -> MarkdownScan:
    """Return raw semantic tokens only where CommonMark proves inline structure."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    lines = _source_lines(markdown)
    for number, source_line in enumerate(lines, 1):
        if "\x00" in source_line.text:
            return MarkdownScan(
                (),
                (),
                (),
                diagnostics=(
                    MarkdownDiagnostic(
                        "markdown_reconciliation_ambiguous",
                        "Source NUL cannot be preserved by the Markdown parser.",
                        number,
                    ),
                ),
            )
    body, line_offset = _without_complete_frontmatter(markdown, lines)
    try:
        tokens = _PARSER.parse(body)
    except Exception:
        return MarkdownScan(
            (),
            (),
            (),
            diagnostics=(
                MarkdownDiagnostic(
                    "markdown_reconciliation_ambiguous",
                    "Markdown parser failed to classify the document.",
                    line_offset + 1,
                ),
            ),
        )
    ranges, diagnostics = _approved_inline_ranges(
        tokens, body_line_count=len(lines) - line_offset, line_offset=line_offset
    )
    headings, heading_diagnostics = _headings_from_tokens(tokens, lines, line_offset)
    diagnostics += heading_diagnostics
    if diagnostics:
        return MarkdownScan((), (), (), diagnostics=diagnostics)
    try:
        inline_tokens = tuple(
            token for token in tokens if getattr(token, "type", None) == "inline"
        )
        links, markers, definitions, visible_text_spans = _scan_raw_inline_ranges(
            markdown, lines, ranges, inline_tokens
        )
    except _InlineAmbiguity as error:
        return MarkdownScan((), (), (), diagnostics=(error.diagnostic,))
    return MarkdownScan(
        links,
        headings,
        markers,
        definitions,
        visible_text_spans=visible_text_spans,
    )
