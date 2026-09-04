"""Authenticated link candidates and deterministic Markdown wiki-graph checks.

This module deliberately treats a supplied ``MarkdownDocuments`` mapping as a
complete postimage.  It never fills a missing mapping entry from the live tree:
that boundary lets the wiki transaction validate staged writes and deletions
without accidentally certifying an old live record.
"""

from __future__ import annotations

import os
import re
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import quote, unquote_to_bytes

from .citations import MarkdownDocuments, encode_markdown_path, resolve_markdown_path
from .contracts import compute_corpus_revision
from .diagnostics import Diagnostic, ValidationIssue, ValidationReport
from .layout import RepoPaths
from .ledger import LedgerStore, _PinnedDirectory, _read_regular_at
from .markdown import MarkdownLink, MarkdownScan, scan_markdown
from .search import (
    SearchOperandError,
    canonical_wiki_logical_path,
    is_canonical_wiki_record_name,
)
from .search_runs import (
    _LinkCandidate,
    _LinkCandidatePage,
    _LinkCandidateRunProof,
    _complete_link_candidate_run,
    _resume_link_candidate_run,
    _start_link_candidate_run,
)
from .wiki_models import (
    QuestionRecord,
    RelatedTarget,
    WikiPage,
    _parse_page_with_document_scan,
    _parse_question_with_document_scan,
)


RecordKind = Literal["page", "question"]


@dataclass(frozen=True)
class LinkCandidate:
    path: PurePosixPath
    line: int
    matched_term: str
    context: str
    kind: RecordKind


@dataclass(frozen=True)
class LinkCandidateResult:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    page_index: int
    request_cursor: str | None
    next_cursor: str | None
    complete: bool
    candidate_count: int
    candidate_manifest_sha256: str
    result_sha256: str
    candidates: tuple[LinkCandidate, ...]
    coverage_gaps: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class LinkCandidateRunProof:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    candidate_manifest_sha256: str
    page_count: int
    candidate_count: int


@dataclass(frozen=True)
class _Record:
    logical_path: PurePosixPath
    path: Path
    text: str
    kind: RecordKind
    model: WikiPage | QuestionRecord
    scan: MarkdownScan


@dataclass(frozen=True)
class _ResolvedLink:
    source: _Record
    target: _Record
    link: MarkdownLink


@dataclass(frozen=True)
class _Relationship:
    source: _Record
    target: _Record
    target_kind: RecordKind


_PAGE_STRUCTURAL_SECTIONS = frozenset(
    {"Related pages", "Related questions", "Sources"}
)
_QUESTION_CONTENT_SECTIONS = frozenset(
    {"Current answer", "Supporting evidence", "Contradictory evidence"}
)
_HEX_REVISION = re.compile(r"[0-9a-f]{64}")


def find_link_candidates(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    page_path: Path,
    terms: tuple[str, ...],
    page_size: int = 100,
    max_run_bytes: int = 1_073_741_824,
) -> LinkCandidateResult:
    """Start a purpose-bound candidate run and expose only its first page."""

    return _candidate_result(
        _start_link_candidate_run(
            paths,
            ledger,
            page_path=page_path,
            terms=terms,
            page_size=page_size,
            max_run_bytes=max_run_bytes,
        )
    )


def resume_link_candidates(
    paths: RepoPaths, ledger: LedgerStore, cursor: str
) -> LinkCandidateResult:
    """Resume only a retained candidate-purpose cursor."""

    return _candidate_result(_resume_link_candidate_run(paths, ledger, cursor))


def complete_link_candidate_run(
    pages: Sequence[LinkCandidateResult],
) -> LinkCandidateRunProof:
    """Require every public page before adapting the retained proof."""

    retained = tuple(_retained_candidate_page(page) for page in pages)
    return _candidate_proof(_complete_link_candidate_run(retained))


def _candidate_result(value: _LinkCandidatePage) -> LinkCandidateResult:
    if not isinstance(value, _LinkCandidatePage):
        raise ValueError("candidate run returned an invalid retained page")
    return LinkCandidateResult(
        value.run_id,
        value.corpus_revision,
        value.page_path,
        value.terms,
        value.page_index,
        value.request_cursor,
        value.next_cursor,
        value.complete,
        value.candidate_count,
        value.candidate_manifest_sha256,
        value.result_sha256,
        tuple(
            LinkCandidate(
                candidate.path,
                candidate.line,
                candidate.matched_term,
                candidate.context,
                candidate.kind,
            )
            for candidate in value.candidates
        ),
        value.coverage_gaps,
    )


def _retained_candidate_page(value: LinkCandidateResult) -> _LinkCandidatePage:
    if not isinstance(value, LinkCandidateResult):
        raise ValueError("candidate page has an invalid type")
    if not isinstance(value.candidates, tuple) or any(
        not isinstance(candidate, LinkCandidate) for candidate in value.candidates
    ):
        raise ValueError("candidate record has an invalid type")
    return _LinkCandidatePage(
        value.run_id,
        value.corpus_revision,
        value.page_path,
        value.terms,
        value.page_index,
        value.request_cursor,
        value.next_cursor,
        value.complete,
        value.candidate_count,
        value.candidate_manifest_sha256,
        value.result_sha256,
        tuple(
            _LinkCandidate(
                candidate.path,
                candidate.line,
                candidate.matched_term,
                candidate.context,
                candidate.kind,
            )
            for candidate in value.candidates
        ),
        value.coverage_gaps,
    )


def _candidate_proof(value: _LinkCandidateRunProof) -> LinkCandidateRunProof:
    if not isinstance(value, _LinkCandidateRunProof):
        raise ValueError("candidate run returned an invalid retained proof")
    return LinkCandidateRunProof(
        value.run_id,
        value.corpus_revision,
        value.page_path,
        value.terms,
        value.candidate_manifest_sha256,
        value.page_count,
        value.candidate_count,
    )


def render_wiki_index(paths: RepoPaths, documents: MarkdownDocuments) -> str:
    """Render the sole deterministic index text for a complete logical mapping."""

    try:
        normalized = _normalise_documents(paths, documents)
    except ValueError as error:
        raise ValueError(f"cannot render wiki index from invalid documents: {error}") from error
    records, issues = _parse_records(normalized)
    if issues:
        raise ValueError("cannot render wiki index from invalid wiki records")
    return _render_index(paths, records)


def validate_graph(
    paths: RepoPaths,
    *,
    documents: MarkdownDocuments | None = None,
    index_text: str | None = None,
    corpus_revision: str | None = None,
) -> ValidationReport:
    """Validate one complete live tree or an authoritative staged postimage."""

    issues: list[ValidationIssue] = []
    revision = _graph_revision(paths, corpus_revision, issues)
    try:
        normalized = (
            _load_live_documents(paths)
            if documents is None
            else _normalise_documents(paths, documents)
        )
    except (OSError, UnicodeError, ValueError) as error:
        issues.append(
            _issue(
                "wiki_documents_invalid",
                f"Logical wiki documents cannot be read safely: {error}",
            )
        )
        return ValidationReport(("wiki-graph",), tuple(issues), revision)

    records, record_issues = _parse_records(normalized)
    issues.extend(record_issues)
    issues.extend(_unique_identity_issues(records))
    display_names, display_issues = _display_names(records)
    issues.extend(display_issues)

    # ``resolve_markdown_path`` returns a canonical, resolved repository path.
    # Mapping paths have already been proved direct canonical paths under the
    # resolved repository root, so retain those lexical values here.  Resolving
    # them again after the mapping/load boundary could follow a file swapped to
    # a symlink while validation is in progress.
    known_paths = {record.path: record for record in records}
    all_paths = {path for path, _text in normalized.values()}
    links = _validate_ordinary_links(paths, records, known_paths, all_paths, issues)
    relationships = _validate_relationships(
        paths, records, known_paths, all_paths, issues
    )
    _validate_reciprocity(relationships, issues)
    _validate_first_occurrences(relationships, display_names, links, issues)

    # A malformed record has no trustworthy title or graph position.  Avoid a
    # second stale-index error that would only be a consequence of that first
    # structural error.
    if len(records) == len(normalized):
        expected_index = _render_index(paths, records)
        if index_text is None:
            try:
                observed_index = _read_live_index(paths)
            except FileNotFoundError:
                issues.append(
                    _issue("wiki_index_missing", "wiki/index.md is missing.", PurePosixPath("wiki/index.md"))
                )
            except (OSError, UnicodeError, ValueError) as error:
                issues.append(
                    _issue(
                        "wiki_index_invalid",
                        f"wiki/index.md cannot be read safely: {error}",
                        PurePosixPath("wiki/index.md"),
                    )
                )
            else:
                if observed_index != expected_index:
                    issues.append(
                        _issue(
                            "wiki_index_stale",
                            "wiki/index.md does not match the complete generated graph index.",
                            PurePosixPath("wiki/index.md"),
                        )
                    )
        elif not isinstance(index_text, str):
            issues.append(
                _issue(
                    "wiki_index_invalid",
                    "The supplied generated index must be text.",
                    PurePosixPath("wiki/index.md"),
                )
            )
        elif index_text != expected_index:
            issues.append(
                _issue(
                    "wiki_index_stale",
                    "wiki/index.md does not match the complete generated graph index.",
                    PurePosixPath("wiki/index.md"),
                )
            )

    return ValidationReport(("wiki-graph",), tuple(issues), revision)


def _graph_revision(
    paths: RepoPaths,
    supplied: str | None,
    issues: list[ValidationIssue],
) -> str | None:
    if supplied is not None:
        if not isinstance(supplied, str) or not _HEX_REVISION.fullmatch(supplied):
            issues.append(
                _issue(
                    "corpus_revision_invalid",
                    "The graph corpus revision must be 64 lowercase hexadecimal characters.",
                )
            )
            return None
        return supplied
    try:
        return compute_corpus_revision(LedgerStore(paths).load_all().values())
    except (OSError, ValueError) as error:
        issues.append(
            _issue(
                "corpus_revision_unavailable",
                f"The graph corpus revision cannot be read safely: {error}",
            )
        )
        return None


def _normalise_documents(
    paths: RepoPaths, documents: MarkdownDocuments
) -> dict[PurePosixPath, tuple[Path, str]]:
    if not isinstance(documents, Mapping):
        raise ValueError("MarkdownDocuments must be a mapping")
    normalized: dict[PurePosixPath, tuple[Path, str]] = {}
    try:
        items = documents.items()
    except (AttributeError, TypeError) as error:
        raise ValueError("MarkdownDocuments must expose mapping items") from error
    for path, text in items:
        if not isinstance(path, Path) or not path.is_absolute() or not isinstance(text, str):
            raise ValueError(
                "MarkdownDocuments requires absolute Path keys and string values"
            )
        try:
            relative = path.relative_to(paths.root)
            if (
                any(component in {"", ".", ".."} for component in relative.parts)
                or "\\" in relative.as_posix()
                or "\0" in relative.as_posix()
            ):
                raise ValueError("logical wiki mapping path is noncanonical")
            logical = canonical_wiki_logical_path(paths, relative.as_posix(), allow_absent=True)
            absolute = paths.root / logical.as_posix()
            if path != absolute:
                raise ValueError("logical wiki mapping path is noncanonical")
        except (OSError, SearchOperandError, ValueError) as error:
            raise ValueError(f"invalid logical wiki mapping path {path!s}: {error}") from error
        if logical in normalized:
            raise ValueError("logical wiki mapping contains duplicate paths")
        normalized[logical] = (absolute, text)
    return normalized


def _load_live_documents(paths: RepoPaths) -> dict[PurePosixPath, tuple[Path, str]]:
    expected = (
        (paths.wiki_pages, ("wiki", "pages")),
        (paths.wiki_questions, ("wiki", "questions")),
    )
    result: dict[PurePosixPath, tuple[Path, str]] = {}
    for directory, prefix in expected:
        if directory != paths.root / "/".join(prefix):
            raise ValueError("wiki roots must use canonical repository paths")
        with _PinnedDirectory.open(directory) as pinned:
            for name in sorted(os.listdir(pinned.descriptor)):
                observed = os.stat(name, dir_fd=pinned.descriptor, follow_symlinks=False)
                if name == ".gitkeep" and stat.S_ISREG(observed.st_mode):
                    continue
                if not is_canonical_wiki_record_name(name):
                    raise ValueError("wiki record tree contains a non-logical entry")
                if not stat.S_ISREG(observed.st_mode):
                    raise ValueError("wiki record tree contains an unsafe logical entry")
                payload, _metadata = _read_regular_at(
                    pinned.descriptor, name, label="wiki record"
                )
                try:
                    text = payload.decode("utf-8", errors="strict")
                except UnicodeDecodeError as error:
                    raise ValueError("wiki record is not strict UTF-8") from error
                logical = PurePosixPath(*prefix, name)
                result[logical] = (directory / name, text)
            pinned.validate()
    return result


def _read_live_index(paths: RepoPaths) -> str:
    with _PinnedDirectory.open(paths.root / "wiki") as pinned:
        payload, _metadata = _read_regular_at(
            pinned.descriptor, "index.md", label="wiki index"
        )
        pinned.validate()
    return payload.decode("utf-8", errors="strict")


def _parse_records(
    documents: Mapping[PurePosixPath, tuple[Path, str]],
) -> tuple[tuple[_Record, ...], tuple[ValidationIssue, ...]]:
    records: list[_Record] = []
    issues: list[ValidationIssue] = []
    for logical_path in sorted(documents, key=lambda item: item.as_posix()):
        path, text = documents[logical_path]
        scan = scan_markdown(text)
        if scan.diagnostics:
            issues.extend(
                _issue(
                    "graph_markdown_ambiguous",
                    diagnostic.message,
                    logical_path,
                    line=diagnostic.line,
                    markdown_code=diagnostic.code,
                )
                for diagnostic in scan.diagnostics
            )
            continue
        kind: RecordKind = "page" if logical_path.parts[1] == "pages" else "question"
        try:
            model = (
                _parse_page_with_document_scan(path, text=text, scan=scan)
                if kind == "page"
                else _parse_question_with_document_scan(path, text=text, scan=scan)
            )
        except (UnicodeError, ValueError) as error:
            issues.append(
                _issue(
                    "wiki_record_invalid",
                    f"Logical wiki record is invalid: {error}",
                    logical_path,
                )
            )
            continue
        records.append(_Record(logical_path, path, text, kind, model, scan))
    return tuple(records), tuple(issues)


def _unique_identity_issues(records: Sequence[_Record]) -> tuple[ValidationIssue, ...]:
    identifiers: dict[str, list[_Record]] = defaultdict(list)
    slugs: dict[str, list[_Record]] = defaultdict(list)
    for record in records:
        identifiers[_record_id(record)].append(record)
        slugs[record.logical_path.stem].append(record)
    issues: list[ValidationIssue] = []
    for identifier, owned in sorted(identifiers.items()):
        if len(owned) > 1:
            for record in owned:
                issues.append(
                    _issue(
                        "wiki_record_id_duplicate",
                        f"Wiki record ID {identifier!r} is not globally unique.",
                        record.logical_path,
                        identifier=identifier,
                    )
                )
    for slug, owned in sorted(slugs.items()):
        if len(owned) > 1:
            for record in owned:
                issues.append(
                    _issue(
                        "wiki_record_slug_duplicate",
                        f"Wiki record slug {slug!r} is not globally unique.",
                        record.logical_path,
                        slug=slug,
                    )
                )
    return tuple(issues)


def _record_id(record: _Record) -> str:
    return record.model.page_id if record.kind == "page" else record.model.question_id


def _display_names(
    records: Sequence[_Record],
) -> tuple[dict[str, frozenset[PurePosixPath]], tuple[ValidationIssue, ...]]:
    owners: dict[str, set[PurePosixPath]] = defaultdict(set)
    for record in records:
        for name in _record_display_names(record):
            owners[name].add(record.logical_path)
    result = {name: frozenset(paths) for name, paths in owners.items()}
    issues = []
    for name, paths in sorted(result.items()):
        if len(paths) > 1:
            issues.append(
                _issue(
                    "display_name_ambiguous",
                    f"Display name {name!r} resolves to more than one wiki record.",
                    details={
                        "display_name": name,
                        "paths": [path.as_posix() for path in sorted(paths, key=lambda item: item.as_posix())],
                    },
                )
            )
    return result, tuple(issues)


def _record_display_names(record: _Record) -> tuple[str, ...]:
    if record.kind == "page":
        values = (record.model.title, *record.model.aliases)
    else:
        values = (
            record.model.title,
            record.model.canonical_question,
            *record.model.prior_phrasings,
        )
    return tuple(dict.fromkeys(values))


def _validate_ordinary_links(
    paths: RepoPaths,
    records: Sequence[_Record],
    known_paths: Mapping[Path, _Record],
    all_paths: set[Path],
    issues: list[ValidationIssue],
) -> tuple[_ResolvedLink, ...]:
    resolved: list[_ResolvedLink] = []
    for record in records:
        definition_lines = {definition.line for definition in record.scan.citation_definitions}
        for link in record.scan.links:
            if link.line in definition_lines:
                continue
            raw_ok = _raw_link_has_no_optional_title(record, link)
            if not raw_ok:
                issues.append(
                    _issue(
                        "graph_link_destination_invalid",
                        "Graph links cannot use optional Markdown titles or unreconciled source spans.",
                        record.logical_path,
                        line=link.line,
                    )
                )
                continue
            destination = link.destination
            pieces = destination.split("#")
            if len(pieces) > 2 or (len(pieces) == 2 and (not pieces[0] or not pieces[1])):
                issues.append(
                    _issue(
                        "graph_link_fragment_invalid",
                        "Graph link fragments must be one nonempty canonical heading fragment.",
                        record.logical_path,
                        line=link.line,
                    )
                )
                continue
            path_text, fragment = pieces[0], pieces[1] if len(pieces) == 2 else None
            try:
                target_path = resolve_markdown_path(paths, record.path, path_text)
            except (OSError, ValueError) as error:
                issues.append(
                    _issue(
                        "graph_link_destination_invalid",
                        f"Graph link destination is not canonical: {error}",
                        record.logical_path,
                        line=link.line,
                    )
                )
                continue
            target = known_paths.get(target_path)
            if target is None:
                if target_path not in all_paths:
                    issues.append(
                        _issue(
                            "graph_link_target_missing",
                            "Graph link target is not a logical wiki record in this mapping.",
                            record.logical_path,
                            line=link.line,
                        )
                    )
                continue
            fragment_ok = True
            if fragment is not None:
                try:
                    decoded = unquote_to_bytes(fragment).decode("utf-8", errors="strict")
                except UnicodeError:
                    fragment_ok = False
                else:
                    fragment_ok = quote(decoded, safe="-._~", encoding="utf-8", errors="strict") == fragment
                    fragment_ok = fragment_ok and sum(
                        heading.text == decoded for heading in target.scan.headings
                    ) == 1
                if not fragment_ok:
                    issues.append(
                        _issue(
                            "graph_link_fragment_invalid",
                            "Graph link fragment must name one exact, canonically encoded target heading.",
                            record.logical_path,
                            line=link.line,
                        )
                    )
            if target.logical_path == record.logical_path:
                issues.append(
                    _issue(
                        "graph_link_self",
                        "Graph links cannot target their own logical record.",
                        record.logical_path,
                        line=link.line,
                    )
                )
            if fragment_ok and target.logical_path != record.logical_path:
                resolved.append(_ResolvedLink(record, target, link))
    return tuple(resolved)


def _raw_link_has_no_optional_title(record: _Record, link: MarkdownLink) -> bool:
    source_span, label_span = link.source_span, link.label_span
    if source_span is None or label_span is None:
        return False
    if not (
        type(source_span.start) is int
        and type(source_span.end) is int
        and type(label_span.start) is int
        and type(label_span.end) is int
        and 0 <= source_span.start <= label_span.start <= label_span.end <= source_span.end <= len(record.text)
    ):
        return False
    raw = record.text[source_span.start : source_span.end]
    label = record.text[label_span.start : label_span.end]
    return raw == f"[{label}]({link.destination})"


def _validate_relationships(
    paths: RepoPaths,
    records: Sequence[_Record],
    known_paths: Mapping[Path, _Record],
    all_paths: set[Path],
    issues: list[ValidationIssue],
) -> tuple[_Relationship, ...]:
    result: list[_Relationship] = []
    for record in records:
        for related, target_kind in _declared_relationships(record):
            target = _resolve_relationship(
                paths, record, related, target_kind, known_paths, all_paths, issues
            )
            if target is not None:
                result.append(_Relationship(record, target, target_kind))
    return tuple(result)


def _declared_relationships(record: _Record) -> tuple[tuple[RelatedTarget, RecordKind], ...]:
    if record.kind == "page":
        return tuple((item, "page") for item in record.model.related_pages) + tuple(
            (item, "question") for item in record.model.related_questions
        )
    return tuple((item, "page") for item in record.model.related_pages)


def _resolve_relationship(
    paths: RepoPaths,
    source: _Record,
    related: RelatedTarget,
    target_kind: RecordKind,
    known_paths: Mapping[Path, _Record],
    all_paths: set[Path],
    issues: list[ValidationIssue],
) -> _Record | None:
    if "#" in related.destination:
        issues.append(
            _issue(
                "relationship_fragment_invalid",
                "Relationship destinations cannot contain heading fragments.",
                source.logical_path,
            )
        )
        return None
    try:
        target_path = resolve_markdown_path(paths, source.path, related.destination)
    except (OSError, ValueError) as error:
        issues.append(
            _issue(
                "relationship_destination_invalid",
                f"Relationship destination is not canonical: {error}",
                source.logical_path,
            )
        )
        return None
    target = known_paths.get(target_path)
    if target is None:
        if target_path not in all_paths:
            issues.append(
                _issue(
                    "relationship_target_missing",
                    "Relationship target is not a logical wiki record in this mapping.",
                    source.logical_path,
                )
            )
        return None
    if target.logical_path == source.logical_path:
        issues.append(
            _issue(
                "relationship_self",
                "Relationships cannot target their own logical record.",
                source.logical_path,
            )
        )
        return None
    if target.kind != target_kind:
        issues.append(
            _issue(
                "relationship_target_kind_invalid",
                "Relationship destination has the wrong logical record kind.",
                source.logical_path,
            )
        )
        return None
    return target


def _validate_reciprocity(
    relationships: Sequence[_Relationship], issues: list[ValidationIssue]
) -> None:
    edges = {(item.source.logical_path, item.target.logical_path) for item in relationships}
    for relationship in relationships:
        if (relationship.target.logical_path, relationship.source.logical_path) not in edges:
            issues.append(
                _issue(
                    "relationship_not_reciprocal",
                    "Every declared page or question relationship requires a reverse relationship.",
                    relationship.source.logical_path,
                    target=relationship.target.logical_path.as_posix(),
                )
            )


def _validate_first_occurrences(
    relationships: Sequence[_Relationship],
    display_names: Mapping[str, frozenset[PurePosixPath]],
    links: Sequence[_ResolvedLink],
    issues: list[ValidationIssue],
) -> None:
    for relationship in relationships:
        names = tuple(
            sorted(
                (
                    name
                    for name in _record_display_names(relationship.target)
                    if display_names.get(name) == frozenset({relationship.target.logical_path})
                ),
                key=lambda item: (-len(item), item),
            )
        )
        if not names:
            continue
        for section, start, end in _content_sections(relationship.source):
            occurrence = _first_occurrence(relationship.source, names, start, end)
            if occurrence is None:
                continue
            raw_start, raw_end, line = occurrence
            linked = any(
                link.source.logical_path == relationship.source.logical_path
                and link.target.logical_path == relationship.target.logical_path
                and link.link.label_span is not None
                and link.link.label_span.start <= raw_start
                and raw_end <= link.link.label_span.end
                for link in links
            )
            if not linked:
                issues.append(
                    _issue(
                        "related_topic_first_occurrence_unlinked",
                        "The first meaningful related-topic occurrence in this section must be a link to its declared target.",
                        relationship.source.logical_path,
                        line=line,
                        section=section,
                        target=relationship.target.logical_path.as_posix(),
                    )
                )


def _content_sections(record: _Record) -> tuple[tuple[str, int, int], ...]:
    starts = _line_starts(record.text)
    headings = record.scan.headings
    sections: list[tuple[str, int, int]] = []
    for index, heading in enumerate(headings):
        if heading.level != 2:
            continue
        if type(heading.line) is not int or type(heading.end_line) is not int:
            return ()
        if not 1 <= heading.line <= heading.end_line <= len(starts):
            return ()
        name = heading.text.strip()
        if record.kind == "page":
            included = name == "Summary" or name not in _PAGE_STRUCTURAL_SECTIONS
        else:
            included = name in _QUESTION_CONTENT_SECTIONS
        if not included:
            continue
        following = len(record.text)
        for successor in headings[index + 1 :]:
            if successor.level <= 2:
                if type(successor.line) is not int or not 1 <= successor.line <= len(starts):
                    return ()
                following = starts[successor.line - 1]
                break
        content_start = starts[heading.end_line] if heading.end_line < len(starts) else len(record.text)
        sections.append((name, content_start, following))
    return tuple(sections)


def _line_starts(text: str) -> tuple[int, ...]:
    starts = [0]
    for match in re.finditer(r"\r\n|\r|\n", text):
        starts.append(match.end())
    return tuple(starts)


def _first_occurrence(
    record: _Record,
    names: Sequence[str],
    section_start: int,
    section_end: int,
) -> tuple[int, int, int] | None:
    choices: list[tuple[int, int, str, int]] = []
    scan = record.scan
    definition_lines = {item.line for item in scan.citation_definitions}
    heading_lines = tuple(
        (heading.line, heading.end_line)
        for heading in scan.headings
        if type(heading.line) is int and type(heading.end_line) is int
    )
    for chunk in scan.visible_text_spans:
        span = chunk.source_span
        if span.start < section_start or span.end > section_end:
            continue
        if any(
            heading_start <= span.end_line and span.line <= heading_end
            for heading_start, heading_end in heading_lines
        ):
            continue
        if any(line in definition_lines for line in range(span.line, span.end_line + 1)):
            continue
        for name in names:
            start = 0
            while True:
                position = chunk.text.find(name, start)
                if position < 0:
                    break
                end = position + len(name)
                if _whole_token(chunk.text, position, end):
                    choices.append((span.start + position, -len(name), name, span.line))
                start = position + 1
    if not choices:
        return None
    raw_start, negative_length, _name, line = min(choices)
    return raw_start, raw_start - negative_length, line


def _whole_token(text: str, start: int, end: int) -> bool:
    return (
        (start == 0 or not _token_character(text[start - 1]))
        and (end == len(text) or not _token_character(text[end]))
    )


def _token_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def _render_index(paths: RepoPaths, records: Sequence[_Record]) -> str:
    source = paths.root / "wiki/index.md"
    pages = sorted(
        (record for record in records if record.kind == "page"),
        key=lambda item: item.logical_path.as_posix(),
    )
    questions = sorted(
        (record for record in records if record.kind == "question"),
        key=lambda item: item.logical_path.as_posix(),
    )
    lines = ["# Second Brain Lite", "", "## Pages", ""]
    lines.extend(_index_line(source, record) for record in pages)
    if pages:
        lines.append("")
    lines.extend(("## Questions",))
    if questions:
        lines.append("")
        lines.extend(_index_line(source, record) for record in questions)
    return "\n".join(lines) + "\n"


def _index_line(source: Path, record: _Record) -> str:
    title = record.model.title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"- [{title}]({encode_markdown_path(source, record.path)})"


def _issue(
    code: str,
    message: str,
    path: PurePosixPath | None = None,
    **details: object,
) -> ValidationIssue:
    if "details" in details:
        supplied = details.pop("details")
        if details or not isinstance(supplied, dict):
            raise ValueError("graph diagnostic details must be a mapping")
        details = supplied
    return ValidationIssue("error", code, message, path, details)
