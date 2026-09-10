"""Strict parsed representations of wiki pages and evolving question records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from .frontmatter import FrontmatterDocument, parse_frontmatter
from .markdown import MarkdownScan, scan_markdown


AnswerStatus = Literal["answered", "partial", "unanswered", "conflicted"]
InterpretationDecisionValue = Literal["not_applicable", "unresolved", "preferred"]

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_QUESTION_ID = re.compile(r"^question-[a-z0-9]+(?:-[a-z0-9]+)*$")
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_RELATION = re.compile(r"^- \[([^\]]+)\]\(([^()\s]+)\): ([^\r\n]+)$")


@dataclass(frozen=True)
class SearchTerms:
    discovery: tuple[str, ...]
    expansion: tuple[str, ...]
    verification: tuple[str, ...]


@dataclass(frozen=True)
class InterpretationDecision:
    """The canonical, typed disposition of contradictory question evidence."""

    decision: InterpretationDecisionValue
    preference_citation_id: str | None
    approval_event_id: str | None


@dataclass(frozen=True)
class RelatedTarget:
    destination: str
    description: str


@dataclass(frozen=True)
class WikiPage:
    path: Path
    page_id: str
    title: str
    description: str
    page_type: str
    aliases: tuple[str, ...]
    created: date
    updated: date
    related_pages: tuple[RelatedTarget, ...]
    related_questions: tuple[RelatedTarget, ...]
    body: str


@dataclass(frozen=True)
class QuestionRecord:
    path: Path
    question_id: str
    title: str
    description: str
    canonical_question: str
    prior_phrasings: tuple[str, ...]
    answer_status: AnswerStatus
    corpus_revision: str
    last_researched: date
    search_terms: SearchTerms
    interpretation: InterpretationDecision
    related_pages: tuple[RelatedTarget, ...]
    body: str


def parse_page(path: Path, *, text: str | None = None) -> WikiPage:
    """Parse a page with its own body scan."""

    return _parse_page(path, text=text, scan=None)


def _parse_page_with_document_scan(
    path: Path, *, text: str, scan: MarkdownScan
) -> WikiPage:
    """Internal graph-only path for a scan already derived from exactly *text*."""

    return _parse_page(path, text=text, scan=scan)


def _parse_page(
    path: Path, *, text: str | None, scan: MarkdownScan | None
) -> WikiPage:

    document = parse_frontmatter(path, text=text)
    _exact_frontmatter(
        document,
        {"id", "title", "description", "type", "aliases", "created", "updated"},
        path,
    )
    page_id = _identifier(document.data["id"], "id", path)
    title = _scalar(document.data["title"], "title", path)
    description = _description(document.data["description"], path)
    page_type = _scalar(document.data["type"], "type", path)
    aliases = _string_list(document.data["aliases"], "aliases", path, allow_empty=True)
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"{path}: aliases must be unique")
    created = _date(document.data["created"], "created", path)
    updated = _date(document.data["updated"], "updated", path)
    sections = _sections(
        document.body,
        path,
        scan=scan,
        document_line_offset=_document_line_offset(path, text, document.body)
        if scan is not None
        else 0,
    )
    _require_title(sections, title, path)
    _require_sections(
        sections,
        ("Summary", "Related pages", "Related questions", "Sources"),
        path,
    )
    _require_final_sources(sections, path)
    # A page must teach something beyond metadata and relationship lists.
    if not _section_text(document.body, sections, "Summary").strip():
        raise ValueError(f"{path}: Summary must be nonempty")
    topic_sections = {
        heading
        for heading, level, _line, _end in sections
        if level == 2 and heading not in {"Summary", "Related pages", "Related questions", "Sources"}
    }
    if not topic_sections:
        raise ValueError(f"{path}: page requires a topic section")
    return WikiPage(
        path,
        page_id,
        title,
        description,
        page_type,
        aliases,
        created,
        updated,
        _relations(_section_text(document.body, sections, "Related pages"), path, "Related pages"),
        _relations(_section_text(document.body, sections, "Related questions"), path, "Related questions"),
        document.body,
    )


def parse_question(path: Path, *, text: str | None = None) -> QuestionRecord:
    """Parse a question with its own body scan."""

    return _parse_question(path, text=text, scan=None)


def _parse_question_with_document_scan(
    path: Path, *, text: str, scan: MarkdownScan
) -> QuestionRecord:
    """Internal graph-only path for a scan already derived from exactly *text*."""

    return _parse_question(path, text=text, scan=scan)


def _parse_question(
    path: Path, *, text: str | None, scan: MarkdownScan | None
) -> QuestionRecord:

    document = parse_frontmatter(path, text=text)
    base_fields = {
        "schema_version",
        "id",
        "title",
        "description",
        "canonical_question",
        "prior_phrasings",
        "answer_status",
        "corpus_revision",
        "last_researched",
        "discovery_terms",
        "expansion_terms",
        "verification_terms",
        "interpretation_decision",
    }
    if document.data.get("schema_version") != "2":
        raise ValueError(f"{path}: schema_version must be exact scalar string 2")
    decision_value = document.data.get("interpretation_decision")
    if decision_value == "preferred":
        _exact_frontmatter(
            document,
            base_fields
            | {
                "interpretation_preference_citation_id",
                "interpretation_approval_event_id",
            },
            path,
        )
    else:
        _exact_frontmatter(document, base_fields, path)
    question_id = _scalar(document.data["id"], "id", path)
    if not _QUESTION_ID.fullmatch(question_id):
        raise ValueError(f"{path}: id must be a question identifier")
    title = _scalar(document.data["title"], "title", path)
    description = _description(document.data["description"], path)
    canonical_question = _scalar(document.data["canonical_question"], "canonical_question", path)
    prior_phrasings = _string_list(document.data["prior_phrasings"], "prior_phrasings", path, allow_empty=True)
    if len(set(prior_phrasings)) != len(prior_phrasings):
        raise ValueError(f"{path}: prior_phrasings must be unique")
    status = _scalar(document.data["answer_status"], "answer_status", path)
    if status not in {"answered", "partial", "unanswered", "conflicted"}:
        raise ValueError(f"{path}: unknown answer_status: {status}")
    decision = _scalar(
        document.data["interpretation_decision"], "interpretation_decision", path
    )
    if decision not in {"not_applicable", "unresolved", "preferred"}:
        raise ValueError(f"{path}: unknown interpretation_decision: {decision}")
    preference_citation_id = None
    approval_event_id = None
    if decision == "not_applicable":
        if status not in {"answered", "partial", "unanswered"}:
            raise ValueError(f"{path}: not_applicable cannot have conflicted answer_status")
    elif decision == "unresolved":
        if status != "conflicted":
            raise ValueError(f"{path}: unresolved requires conflicted answer_status")
    else:
        if status not in {"answered", "partial"}:
            raise ValueError(f"{path}: preferred requires answered or partial answer_status")
        preference_citation_id = _scalar(
            document.data["interpretation_preference_citation_id"],
            "interpretation_preference_citation_id",
            path,
        )
        approval_event_id = _scalar(
            document.data["interpretation_approval_event_id"],
            "interpretation_approval_event_id",
            path,
        )
    revision = _scalar(document.data["corpus_revision"], "corpus_revision", path)
    if not _REVISION.fullmatch(revision):
        raise ValueError(f"{path}: corpus_revision must be 64 lowercase hexadecimal characters")
    terms = SearchTerms(
        _string_list(document.data["discovery_terms"], "discovery_terms", path, allow_empty=True),
        _string_list(document.data["expansion_terms"], "expansion_terms", path, allow_empty=True),
        _string_list(document.data["verification_terms"], "verification_terms", path, allow_empty=True),
    )
    sections = _sections(
        document.body,
        path,
        scan=scan,
        document_line_offset=_document_line_offset(path, text, document.body)
        if scan is not None
        else 0,
    )
    _require_title(sections, title, path)
    _require_sections(
        sections,
        ("Current answer", "Supporting evidence", "Contradictory evidence", "Related pages", "Sources"),
        path,
    )
    _require_final_sources(sections, path)
    if status != "unanswered" and not _section_text(document.body, sections, "Current answer").strip():
        raise ValueError(f"{path}: Current answer must be nonempty")
    return QuestionRecord(
        path,
        question_id,
        title,
        description,
        canonical_question,
        prior_phrasings,
        cast(AnswerStatus, status),
        revision,
        _date(document.data["last_researched"], "last_researched", path),
        terms,
        InterpretationDecision(
            cast(InterpretationDecisionValue, decision),
            preference_citation_id,
            approval_event_id,
        ),
        _relations(_section_text(document.body, sections, "Related pages"), path, "Related pages"),
        document.body,
    )


def _exact_frontmatter(document: FrontmatterDocument, expected: set[str], path: Path) -> None:
    actual = set(document.data)
    if actual != expected:
        details = []
        if expected - actual:
            details.append("missing " + ", ".join(sorted(expected - actual)))
        if actual - expected:
            details.append("unknown " + ", ".join(sorted(actual - expected)))
        raise ValueError(f"{path}: frontmatter has " + "; ".join(details) + " fields")


def _scalar(value: object, name: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError(f"{path}: {name} must be a nonempty scalar string")
    return value


def _identifier(value: object, name: str, path: Path) -> str:
    result = _scalar(value, name, path)
    if not _ID.fullmatch(result):
        raise ValueError(f"{path}: {name} must be a canonical identifier")
    return result


def _description(value: object, path: Path) -> str:
    result = _scalar(value, "description", path)
    # Plain one-sentence descriptions are deliberately easy to lint and render.
    if result[-1] not in ".!?" or any(mark in result[:-1] for mark in ".!?"):
        raise ValueError(f"{path}: description must be one sentence")
    return result


def _date(value: object, name: str, path: Path) -> date:
    raw = _scalar(value, name, path)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as error:
        raise ValueError(f"{path}: {name} must be an ISO date") from error
    if parsed.isoformat() != raw:
        raise ValueError(f"{path}: {name} must be an ISO date")
    return parsed


def _string_list(value: object, name: str, path: Path, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, str) or not item.strip() or item.strip() != item for item in value):
        raise ValueError(f"{path}: {name} must be an inline scalar list")
    if not value and not allow_empty:
        raise ValueError(f"{path}: {name} must be nonempty")
    return cast(tuple[str, ...], value)


# (heading text, level, zero-based content line, exclusive zero-based end line)
_Section = tuple[str, int, int, int]


def _physical_lines(markdown: str) -> tuple[str, ...]:
    """Preserve terminators while counting only parser CR/LF boundaries."""
    return tuple(
        match.group()
        for match in re.finditer(r"[^\r\n]*(?:\r\n?|\n|$)", markdown)
        if match.start() != match.end()
    )


def _document_line_offset(path: Path, text: str, body: str) -> int:
    """Count physical lines before a frontmatter body for a full-document scan."""

    if not text.endswith(body):
        raise ValueError(f"{path}: supplied Markdown scan has no matching body")
    prefix = text[: len(text) - len(body)]
    return sum(1 for _match in re.finditer(r"\r\n|\r|\n", prefix))


def _sections(
    body: str,
    path: Path,
    *,
    scan: MarkdownScan | None = None,
    document_line_offset: int = 0,
) -> tuple[_Section, ...]:
    if scan is None:
        scan = scan_markdown(body)
    elif not isinstance(scan, MarkdownScan):
        raise TypeError(f"{path}: scan must be a MarkdownScan")
    if type(document_line_offset) is not int or document_line_offset < 0:
        raise ValueError(f"{path}: Markdown scan line offset is invalid")
    if scan.diagnostics:
        diagnostic = scan.diagnostics[0]
        raise ValueError(
            f"{path}: markdown structure cannot be reconciled at line "
            f"{diagnostic.line}: {diagnostic.message}"
        )
    headings = scan.headings
    if not headings:
        raise ValueError(f"{path}: body requires headings")
    all_headings: set[tuple[int, str]] = set()
    lines = _physical_lines(body)
    sections: list[_Section] = []
    previous_level = 0
    for heading in headings:
        reported_line = (
            heading.line
            if type(heading.line) is int and heading.line > 0
            else 1
        )
        if (
            type(heading.line) is not int
            or type(heading.end_line) is not int
        ):
            raise ValueError(
                f"{path}: markdown heading span cannot be reconciled at line "
                f"{reported_line}"
            )
        local_line = heading.line - document_line_offset
        local_end_line = heading.end_line - document_line_offset
        if not 1 <= local_line <= local_end_line <= len(lines):
            raise ValueError(
                f"{path}: markdown heading span cannot be reconciled at line "
                f"{reported_line}"
            )
    for index, heading in enumerate(headings):
        normalized = heading.text.strip()
        if heading.level > previous_level + 1:
            raise ValueError(f"{path}: heading hierarchy skips a level")
        key = (heading.level, normalized)
        if key in all_headings:
            raise ValueError(f"{path}: duplicate section heading: {normalized}")
        all_headings.add(key)
        local_end_line = heading.end_line - document_line_offset
        end_line = len(lines)
        for successor in headings[index + 1 :]:
            successor_line = successor.line - document_line_offset
            if successor.level <= heading.level:
                end_line = successor_line - 1
                break
        sections.append((normalized, heading.level, local_end_line, end_line))
        previous_level = heading.level
    return tuple(sections)


def _require_title(sections: tuple[_Section, ...], title: str, path: Path) -> None:
    titles = [heading for heading, level, _start, _end in sections if level == 1]
    if titles != [title] or sections[0][:2] != (title, 1):
        raise ValueError(f"{path}: body must have one title heading matching frontmatter title")


def _require_sections(sections: tuple[_Section, ...], required: tuple[str, ...], path: Path) -> None:
    names = [heading for heading, level, _start, _end in sections if level == 2]
    missing = [name for name in required if name not in names]
    if missing:
        raise ValueError(f"{path}: missing section headings: {', '.join(missing)}")


def _require_final_sources(sections: tuple[_Section, ...], path: Path) -> None:
    if sections[-1][:2] != ("Sources", 2):
        raise ValueError(f"{path}: Sources must be the final section")


def _section_text(body: str, sections: tuple[_Section, ...], name: str) -> str:
    lines = _physical_lines(body)
    for heading, _level, start, end in sections:
        if heading == name:
            return "".join(lines[start:end])
    raise AssertionError(f"missing required section: {name}")


def _relations(content: str, path: Path, section: str) -> tuple[RelatedTarget, ...]:
    targets: list[RelatedTarget] = []
    for raw_line in _physical_lines(content):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        match = _RELATION.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}: malformed {section} relationship")
        label, destination, description = match.groups()
        if not label.strip() or not destination or any(char in destination for char in "\\\r\n\0") or not description.strip():
            raise ValueError(f"{path}: malformed {section} relationship")
        targets.append(RelatedTarget(destination, description))
    if len({target.destination for target in targets}) != len(targets):
        raise ValueError(f"{path}: duplicate {section} relationship")
    return tuple(targets)
