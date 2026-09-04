"""Claim-level citations to exact retained source versions and derivations."""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias
from urllib.parse import quote, unquote_to_bytes

from .contracts import Anchor, compute_corpus_revision
from .diagnostics import ValidationIssue, ValidationReport
from .inventory import SnapshotNamespace
from .layout import RepoPaths
from .ledger import CitationRewrite, LedgerStore
from .markdown import MarkdownScan, scan_markdown
from .validation import ChecksumCache


MarkdownDocuments: TypeAlias = Mapping[Path, str]
_DEFINITION = re.compile(r"^ {0,3}\[\^([^\]\r\n]+)\]:[ \t]*(.*)$")
_CITATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}")
_CONTENT_SHA = re.compile(r"[0-9a-f]{64}")
_DERIVATION_ID = re.compile(r"drv_[0-9a-f]{64}")
_FIELD = re.compile(
    r"[ \t]*(source_id|content_sha256|derivation_id|anchor):[ \t]*`([^`\r\n]*)`[ \t]*"
)
_LINK = re.compile(r"[ \t]*\[(original|extracted)\]\(([^()\r\n]*)\)[ \t]*")
_ANCHOR_KINDS = frozenset({"line", "page", "slide", "sheet", "section", "row", "block"})


class CitationPathError(ValueError):
    def __init__(
        self, message: str, *, code: str = "citation_destination_noncanonical"
    ) -> None:
        super().__init__(message)
        self.code = code


class CitationParseError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


@dataclass(frozen=True)
class Citation:
    citation_id: str
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor
    original_destination: str
    extracted_destination: str
    definition_line: int


def read_markdown_documents(documents: Iterable[Path]) -> dict[Path, str]:
    """Read each logical Markdown path once; callers can instead supply staged text."""
    return {path: path.read_text(encoding="utf-8") for path in sorted(set(documents))}


def encode_markdown_path(document_path: Path, target_path: Path) -> str:
    """Encode one minimal relative POSIX destination, without a fragment."""
    relative = Path(os.path.relpath(target_path, document_path.parent)).as_posix()
    return "/".join(
        quote(component, safe="-._~", encoding="utf-8", errors="strict")
        for component in relative.split("/")
    )


def resolve_markdown_path(
    paths: RepoPaths, document_path: Path, destination: str
) -> Path:
    """Decode exactly once, prove containment, and demand the canonical spelling."""
    if not isinstance(destination, str) or not destination:
        raise CitationPathError(
            "Citation destination must be a nonempty relative path."
        )
    if (
        destination.startswith("/")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", destination)
        or any(char in destination for char in "\\?#\0")
    ):
        raise CitationPathError(
            "Citation destination cannot contain an authority, scheme, query, fragment, or backslash."
        )
    if re.search(r"%(?![0-9A-F]{2})", destination) or re.search(
        r"%(?:2F|5C|00)", destination
    ):
        raise CitationPathError(
            "Citation destination contains an invalid or forbidden percent escape."
        )
    try:
        decoded = unquote_to_bytes(destination).decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise CitationPathError(
            "Citation destination is not canonical UTF-8."
        ) from error
    components = decoded.split("/")
    if any(component in {"", "."} for component in components):
        raise CitationPathError(
            "Citation destination contains an empty or dot component."
        )
    seen_name = False
    for encoded, component in zip(destination.split("/"), components):
        if component == "..":
            if encoded != ".." or seen_name:
                raise CitationPathError(
                    "Citation destination contains nonminimal traversal."
                )
        else:
            seen_name = True
    try:
        root = paths.root.resolve()
        document_path.relative_to(paths.root)
        target = (document_path.parent / decoded).resolve()
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CitationPathError(
            "Citation destination escapes the repository.",
            code="citation_target_escape",
        ) from error
    if encode_markdown_path(document_path, target) != destination:
        raise CitationPathError(
            "Citation destination does not use the exact minimal encoded path."
        )
    return target


def _issue(
    code: str, message: str, path: Path | PurePosixPath, *, line: int, citation_id: str
) -> ValidationIssue:
    return ValidationIssue(
        "error",
        code,
        message,
        PurePosixPath(path),
        {"line": line, "citation_id": citation_id},
    )


def _scan_diagnostic_issues(
    scan: MarkdownScan, path: Path | PurePosixPath
) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            "error",
            "citation_markdown_ambiguous",
            diagnostic.message,
            PurePosixPath(path),
            {"line": diagnostic.line, "markdown_code": diagnostic.code},
        )
        for diagnostic in scan.diagnostics
    ]


def _physical_lines(markdown: str) -> tuple[str, ...]:
    """Preserve original terminators and count only the parser's CR/LF boundaries."""
    return tuple(
        match.group()
        for match in re.finditer(r"[^\r\n]*(?:\r\n?|\n|$)", markdown)
        if match.start() != match.end()
    )


def _definition_lines(
    markdown: str, scan: MarkdownScan
) -> tuple[tuple[int, re.Match[str]], ...]:
    """Ask the shared scanner which candidate definitions are outside code/frontmatter."""
    lines = _physical_lines(markdown)
    candidates = []
    for definition in scan.citation_definitions:
        match = _DEFINITION.fullmatch(lines[definition.line - 1].rstrip("\r\n"))
        if match is not None:
            candidates.append((definition.line, match))
    return tuple(candidates)


def _parse_definitions(
    markdown: str, path: Path | PurePosixPath, scan: MarkdownScan
) -> tuple[tuple[Citation, ...], list[ValidationIssue]]:
    citations = []
    issues = []
    for line, match in _definition_lines(markdown, scan):
        citation_id, body = match.groups()
        fields = {}
        valid = _CITATION_ID.fullmatch(citation_id) is not None
        # Semicolons inside quoted identity values are not field delimiters.
        parts = re.split(r";(?=(?:[^`]*`[^`]*`)*[^`]*$)", body)
        for part in parts:
            token = _FIELD.fullmatch(part) or _LINK.fullmatch(part)
            if token is None or token.group(1) in fields:
                valid = False
                continue
            fields[token.group(1)] = token.group(2)
        if not valid or set(fields) != {
            "source_id",
            "content_sha256",
            "derivation_id",
            "anchor",
            "original",
            "extracted",
        }:
            issues.append(
                _issue(
                    "citation_definition_invalid",
                    "Citation requires each identity field and evidence link exactly once.",
                    path,
                    line=line,
                    citation_id=citation_id,
                )
            )
            continue
        for field, grammar in (
            ("source_id", _SOURCE_ID),
            ("content_sha256", _CONTENT_SHA),
            ("derivation_id", _DERIVATION_ID),
        ):
            if not grammar.fullmatch(fields[field]):
                issues.append(
                    _issue(
                        "citation_" + field + "_invalid",
                        f"Citation {field} is not a full canonical identifier.",
                        path,
                        line=line,
                        citation_id=citation_id,
                    )
                )
                valid = False
        kind, separator, value = fields["anchor"].partition(":")
        if (
            not separator
            or kind not in _ANCHOR_KINDS
            or not value
            or any(char in value for char in "\r\n\0")
        ):
            issues.append(
                _issue(
                    "citation_anchor_invalid",
                    "Citation anchor must be a canonical kind and nonempty value.",
                    path,
                    line=line,
                    citation_id=citation_id,
                )
            )
            valid = False
        if valid:
            citations.append(
                Citation(
                    citation_id,
                    fields["source_id"],
                    fields["content_sha256"],
                    fields["derivation_id"],
                    Anchor(kind, value),
                    fields["original"],
                    fields["extracted"],
                    line,
                )
            )
    return tuple(citations), issues


def parse_citation_definitions(markdown: str, *, path: Path) -> tuple[Citation, ...]:
    scan = scan_markdown(markdown)
    citations, parse_issues = _parse_definitions(markdown, path, scan)
    issues = _scan_diagnostic_issues(scan, path) + parse_issues
    if issues:
        raise CitationParseError(tuple(issues))
    return citations


def _replace_destinations(
    markdown: str, replacements: Mapping[int, Mapping[str, str]]
) -> str:
    changes = []
    line_start = 0
    for number, line in enumerate(_physical_lines(markdown), 1):
        destinations = replacements.get(number)
        if destinations:
            definition = _DEFINITION.fullmatch(line.rstrip("\r\n"))
            assert definition is not None
            offset = line_start + definition.start(2)
            for part in re.split(r";(?=(?:[^`]*`[^`]*`)*[^`]*$)", definition.group(2)):
                link = _LINK.fullmatch(part)
                if link is not None and link.group(1) in destinations:
                    changes.append(
                        (
                            offset + link.start(2),
                            offset + link.end(2),
                            destinations[link.group(1)],
                        )
                    )
                offset += len(part) + 1
        line_start += len(line)
    for start, end, replacement in reversed(changes):
        markdown = markdown[:start] + replacement + markdown[end:]
    return markdown


def canonicalize_citation_destinations(
    markdown: str, ledger: LedgerStore, *, paths: RepoPaths, document_path: Path
) -> str:
    scan = scan_markdown(markdown)
    issues = _scan_diagnostic_issues(scan, document_path)
    if issues:
        raise CitationParseError(tuple(issues))
    replacements = {}
    citations, _ = _parse_definitions(markdown, document_path, scan)
    for citation in citations:
        representation = ledger.find_representation(
            citation.source_id, citation.content_sha256, citation.derivation_id
        )
        if representation is not None:
            replacements[citation.definition_line] = {
                "original": encode_markdown_path(
                    document_path, paths.raw / representation.raw_path
                ),
                "extracted": encode_markdown_path(
                    document_path, paths.root / representation.extracted_path
                )
                + f"#{citation.anchor.kind}:{citation.anchor.value}",
            }
    return _replace_destinations(markdown, replacements)


def rewrite_historical_original_links(
    markdown: str,
    rewrites: tuple[CitationRewrite, ...],
    *,
    paths: RepoPaths,
    document_path: Path,
) -> str:
    if not isinstance(rewrites, tuple) or any(
        not isinstance(item, CitationRewrite) for item in rewrites
    ):
        raise ValueError("citation rewrites must be a canonical sorted tuple")
    keys = tuple((item.source_id, item.content_sha256) for item in rewrites)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ValueError(
            "citation rewrites must be sorted with unique source/version keys"
        )
    scan = scan_markdown(markdown)
    issues = _scan_diagnostic_issues(scan, document_path)
    if issues:
        raise CitationParseError(tuple(issues))
    targets = {
        (item.source_id, item.content_sha256): item.raw_path for item in rewrites
    }
    replacements = {}
    citations, _ = _parse_definitions(markdown, document_path, scan)
    for citation in citations:
        raw_path = targets.get((citation.source_id, citation.content_sha256))
        if raw_path is not None:
            replacements[citation.definition_line] = {
                "original": encode_markdown_path(document_path, paths.raw / raw_path)
            }
    return _replace_destinations(markdown, replacements)


def validate_citations(
    paths: RepoPaths,
    ledger: LedgerStore,
    documents: MarkdownDocuments | Iterable[Path],
    *,
    full: bool = False,
    checksum_cache: ChecksumCache | None = None,
) -> ValidationReport:
    documents = (
        documents
        if isinstance(documents, Mapping)
        else read_markdown_documents(documents)
    )
    records = ledger.load_all()
    current_revision = compute_corpus_revision(records.values())
    cache = checksum_cache
    if cache is None:
        cache = ChecksumCache()
        cache.begin_transaction()
    issues: list[ValidationIssue] = []
    for document_path, markdown in sorted(documents.items()):
        if (
            not isinstance(document_path, Path)
            or not document_path.is_absolute()
            or not isinstance(markdown, str)
        ):
            raise ValueError(
                "MarkdownDocuments requires absolute logical Path keys and string values"
            )
        try:
            issue_path = document_path.relative_to(paths.root)
            if ".." in issue_path.parts:
                raise ValueError("noncanonical parent traversal")
        except ValueError as error:
            raise ValueError(
                "MarkdownDocuments paths must be within the repository"
            ) from error
        scan = scan_markdown(markdown)
        issues.extend(_scan_diagnostic_issues(scan, issue_path))
        citations, parse_issues = _parse_definitions(markdown, issue_path, scan)
        issues.extend(parse_issues)
        definitions = _definition_lines(markdown, scan)
        definition_counts = Counter(match.group(1) for _, match in definitions)
        definition_lines = {number for number, _ in definitions}
        markers = [
            marker
            for marker in scan.citation_markers
            if marker.line not in definition_lines
        ]
        reference_counts = Counter(marker.citation_id for marker in markers)
        for marker in markers:
            if marker.citation_id not in definition_counts:
                issues.append(
                    _issue(
                        "citation_definition_missing",
                        "Citation marker has no definition in this document.",
                        issue_path,
                        line=marker.line,
                        citation_id=marker.citation_id,
                    )
                )
        sources = [
            heading
            for heading in scan.headings
            if heading.level == 2 and heading.text == "Sources"
        ]
        final_sources = sources[-1] if sources else None
        section_is_final = final_sources is not None and not any(
            heading.line > final_sources.line for heading in scan.headings
        )
        for number, match in definitions:
            citation_id = match.group(1)
            if definition_counts[citation_id] != 1:
                issues.append(
                    _issue(
                        "citation_definition_duplicate",
                        "Citation must have exactly one definition in its document.",
                        issue_path,
                        line=number,
                        citation_id=citation_id,
                    )
                )
            if not section_is_final or number <= final_sources.line:
                issues.append(
                    _issue(
                        "citation_definition_out_of_section",
                        "Citation definition must appear under the final Sources heading.",
                        issue_path,
                        line=number,
                        citation_id=citation_id,
                    )
                )
            if citation_id not in reference_counts:
                issues.append(
                    _issue(
                        "citation_definition_unused",
                        "A source definition needs an adjacent claim marker reference.",
                        issue_path,
                        line=number,
                        citation_id=citation_id,
                    )
                )
        for citation in citations:

            def add(code: str, message: str) -> None:
                issues.append(
                    _issue(
                        code,
                        message,
                        issue_path,
                        line=citation.definition_line,
                        citation_id=citation.citation_id,
                    )
                )

            record = records.get(citation.source_id)
            if record is None:
                add(
                    "citation_source_missing",
                    "Cited source is not retained in the ledger.",
                )
                continue
            version = record.versions.get(citation.content_sha256)
            if version is None:
                add(
                    "citation_version_missing",
                    "Cited content version is not retained in the source record.",
                )
                continue
            representation = ledger.find_representation(
                citation.source_id, citation.content_sha256, citation.derivation_id
            )
            if representation is None:
                add(
                    "citation_derivation_missing",
                    "Cited derivation is not retained for this source version.",
                )
                continue
            derivation = record.derivations[citation.derivation_id]
            if citation.anchor not in representation.anchors:
                add(
                    "citation_anchor_missing",
                    "Cited anchor is absent from the exact retained derivation.",
                )
            original_path, original_separator, _ = (
                citation.original_destination.partition("#")
            )
            extracted_path, extracted_separator, extracted_fragment = (
                citation.extracted_destination.partition("#")
            )
            if original_separator:
                add(
                    "citation_destination_noncanonical",
                    "Original evidence links cannot contain fragments.",
                )
            if (
                not extracted_separator
                or extracted_fragment
                != f"{citation.anchor.kind}:{citation.anchor.value}"
            ):
                add(
                    "citation_extracted_fragment_mismatch",
                    "Extracted fragment must exactly match the cited anchor.",
                )
            namespace = {
                "_versions": SnapshotNamespace.RAW_VERSION,
                "_web": SnapshotNamespace.RAW_WEB,
            }.get(representation.raw_path.parts[0], SnapshotNamespace.RAW_USER)
            expected_namespace = (
                SnapshotNamespace.RAW_WEB
                if record.url_descriptor is not None
                else (
                    SnapshotNamespace.RAW_USER
                    if citation.content_sha256 == record.active_content_sha256
                    else SnapshotNamespace.RAW_VERSION
                )
            )
            wrong_owner = namespace in {
                SnapshotNamespace.RAW_VERSION,
                SnapshotNamespace.RAW_WEB,
            } and representation.raw_path.parts[1:3] != (
                citation.source_id,
                citation.content_sha256,
            )
            wrong_live_path = (
                namespace is SnapshotNamespace.RAW_USER
                and representation.raw_path != record.current_raw_path
            )
            if namespace != expected_namespace or wrong_owner or wrong_live_path:
                cache.poison(namespace, representation.raw_path)
                add(
                    "citation_original_target_mismatch",
                    "Retained original path belongs to a different source/version namespace owner.",
                )
                continue
            if representation.extracted_path.parts[-2:] != (
                citation.content_sha256,
                citation.derivation_id + ".md",
            ):
                cache.poison(SnapshotNamespace.EXTRACTED, representation.extracted_path)
                add(
                    "citation_extracted_target_mismatch",
                    "Retained extracted path belongs to a different content version or derivation.",
                )
                continue
            targets = (
                (
                    "original",
                    original_path,
                    paths.raw / representation.raw_path,
                    namespace,
                    representation.raw_path,
                    version.byte_size,
                    version.fingerprint.mtime_ns,
                    citation.content_sha256,
                ),
                (
                    "extracted",
                    extracted_path,
                    paths.root / representation.extracted_path,
                    SnapshotNamespace.EXTRACTED,
                    representation.extracted_path,
                    derivation.output_byte_size,
                    derivation.output_mtime_ns,
                    representation.output_sha256,
                ),
            )
            for (
                role,
                destination,
                expected,
                target_namespace,
                logical_path,
                byte_size,
                mtime_ns,
                checksum,
            ) in targets * max(1, reference_counts[citation.citation_id]):
                try:
                    target = resolve_markdown_path(paths, document_path, destination)
                except CitationPathError as error:
                    add(error.code, str(error))
                    if destination != encode_markdown_path(document_path, expected):
                        continue
                    # The spelling names the authoritative logical key, but its
                    # live resolution changed. Observe that key anyway: the
                    # namespace cache must retain the failure even if a caller
                    # later restores the original directory or symlink entry.
                    target = None
                if target is not None and target != expected:
                    add(
                        f"citation_{role}_target_mismatch",
                        f"The {role} link does not name the exact retained representation target.",
                    )
                    if (
                        role == "original"
                        and representation.raw_path.parts[0] == "_versions"
                        and target == paths.raw / record.current_raw_path
                    ):
                        add(
                            "citation_original_stale",
                            "Original link still names the live path after an approved version adoption.",
                        )
                    continue
                try:
                    snapshot = cache.observe(
                        paths, target_namespace, logical_path, full=full
                    )
                except (OSError, ValueError) as error:
                    add(
                        f"citation_{role}_missing",
                        f"The {role} file is missing or cannot be observed safely: {error}",
                    )
                    continue
                if snapshot.byte_size != byte_size or snapshot.mtime_ns != mtime_ns:
                    add(
                        f"citation_{role}_fingerprint_mismatch",
                        f"The {role} file metadata differs from the retained evidence.",
                    )
                if full and snapshot.sha256 != checksum:
                    add(
                        f"citation_{role}_checksum_mismatch",
                        f"The {role} bytes differ from the retained evidence checksum.",
                    )
    return ValidationReport(
        checks=("citations",), issues=tuple(issues), corpus_revision=current_revision
    )
