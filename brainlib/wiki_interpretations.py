"""Typed contradictory-evidence checks for canonical question records."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath

from .citations import Citation, CitationParseError, parse_citation_definitions
from .diagnostics import ValidationIssue
from .markdown import MarkdownScan, scan_markdown
from .wiki_models import QuestionRecord, parse_question


def _issue(path: Path, code: str, message: str) -> ValidationIssue:
    return ValidationIssue("error", code, message, PurePosixPath(path.as_posix()))


def _section_marker_ids(scan: MarkdownScan, section: str) -> frozenset[str]:
    """Return parser-proven citation markers within one exact level-two section."""

    heading = next(
        (item for item in scan.headings if item.level == 2 and item.text == section),
        None,
    )
    if heading is None or heading.end_line is None:
        return frozenset()
    end = next(
        (
            item.line
            for item in scan.headings
            if item.line > heading.line and item.level <= heading.level
        ),
        None,
    )
    definition_lines = {item.line for item in scan.citation_definitions}
    return frozenset(
        marker.citation_id
        for marker in scan.citation_markers
        if marker.line > heading.end_line
        and (end is None or marker.line < end)
        and marker.line not in definition_lines
    )


def _identity(citation: Citation) -> tuple[str, str, str, object]:
    """The retained source/version/derivation/anchor identity, never its label."""

    return (
        citation.source_id,
        citation.content_sha256,
        citation.derivation_id,
        citation.anchor,
    )


def _document_citations(path: Path, text: str) -> tuple[MarkdownScan, dict[str, Citation]]:
    scan = scan_markdown(text)
    if scan.diagnostics:
        raise ValueError("Markdown structure is ambiguous")
    citations = parse_citation_definitions(text, path=path)
    return scan, {citation.citation_id: citation for citation in citations}


def validate_interpretation_document(
    path: Path, text: str, record: QuestionRecord,
) -> tuple[ValidationIssue, ...]:
    """Validate section-local evidence required by a typed interpretation state."""

    try:
        scan, definitions = _document_citations(path, text)
    except (CitationParseError, ValueError) as error:
        return (_issue(path, "interpretation_citations_invalid", str(error)),)
    decision = record.interpretation
    contradictory = _section_marker_ids(scan, "Contradictory evidence")
    current = _section_marker_ids(scan, "Current answer")
    if decision.decision == "not_applicable":
        return ()
    if decision.decision == "unresolved":
        identities = {
            _identity(definitions[citation_id])
            for citation_id in contradictory
            if citation_id in definitions
        }
        if len(identities) < 2:
            return (
                _issue(
                    path,
                    "interpretation_unresolved_evidence_insufficient",
                    "An unresolved interpretation requires two distinct exact citations in Contradictory evidence.",
                ),
            )
        return ()
    assert decision.preference_citation_id is not None
    selected = decision.preference_citation_id
    if selected not in definitions:
        return (
            _issue(
                path,
                "interpretation_preference_citation_missing",
                "The preferred interpretation citation must resolve to a local definition.",
            ),
        )
    missing_sections = [
        name
        for name, markers in (
            ("Contradictory evidence", contradictory),
            ("Current answer", current),
        )
        if selected not in markers
    ]
    if missing_sections:
        return (
            _issue(
                path,
                "interpretation_preference_citation_not_cited",
                "The preferred interpretation citation must occur in "
                + " and ".join(missing_sections)
                + ".",
            ),
        )
    return ()


def validate_interpretation_transitions(
    *,
    before: Mapping[Path, str],
    after: Mapping[Path, str],
    changed_paths: Collection[Path],
    change_intent: str,
    approval_event_id: str | None,
) -> None:
    """Require an approved, single-record route for typed decision changes."""

    transitions = 0
    for path in sorted(set(changed_paths), key=lambda item: item.as_posix()):
        if path.parent.name != "questions":
            continue
        prior_text = before.get(path)
        next_text = after.get(path)
        if next_text is None:
            continue
        try:
            next_record = parse_question(path, text=next_text)
            prior_record = (
                None if prior_text is None else parse_question(path, text=prior_text)
            )
        except ValueError as error:
            raise ValueError(f"invalid QuestionRecord transition at {path}: {error}") from error

        if prior_record is not None and next_record.interpretation.decision in {
            "unresolved",
            "preferred",
        }:
            try:
                _scan, prior_citations = _document_citations(path, prior_text)
                _scan, next_citations = _document_citations(path, next_text)
            except (CitationParseError, ValueError) as error:
                raise ValueError(f"cannot retain prior citation identities at {path}: {error}") from error
            retained = {_identity(citation) for citation in next_citations.values()}
            if not {_identity(citation) for citation in prior_citations.values()}.issubset(retained):
                raise ValueError(
                    "An unresolved or preferred QuestionRecord must retain every prior exact citation identity."
                )
            selected_citation_ids = {
                citation_id
                for citation_id in (
                    prior_record.interpretation.preference_citation_id,
                    next_record.interpretation.preference_citation_id,
                )
                if citation_id is not None
            }
            for citation_id in selected_citation_ids:
                prior_citation = prior_citations.get(citation_id)
                next_citation = next_citations.get(citation_id)
                if (
                    prior_citation is not None
                    and (
                        next_citation is None
                        or _identity(prior_citation) != _identity(next_citation)
                    )
                ):
                    raise ValueError(
                        "A selected citation ID must retain its exact citation identity across a transition."
                    )

        prior_decision = None if prior_record is None else prior_record.interpretation
        next_decision = next_record.interpretation
        changed = (
            next_decision.decision != "not_applicable"
            if prior_decision is None
            else prior_decision != next_decision
        )
        if not changed:
            continue
        transitions += 1
        prior_value = None if prior_decision is None else prior_decision.decision
        next_value = next_decision.decision

        if prior_value in {"unresolved", "preferred"} and next_value == "not_applicable":
            raise ValueError(
                "An unresolved or preferred interpretation cannot be withdrawn to not_applicable."
            )
        if next_value == "preferred":
            if change_intent != "resolve_contradiction":
                raise ValueError(
                    "A preferred interpretation requires resolve_contradiction intent."
                )
            if not approval_event_id:
                raise ValueError("A preferred interpretation requires a nonempty approval_event_id.")
            if next_decision.approval_event_id != approval_event_id:
                raise ValueError(
                    "The preferred interpretation approval must exactly match the manifest approval_event_id."
                )
            if (
                prior_decision is not None
                and prior_decision.decision == "preferred"
                and prior_decision != next_decision
                and next_decision.approval_event_id == prior_decision.approval_event_id
            ):
                raise ValueError(
                    "A different preferred interpretation requires a new approval_event_id."
                )
        elif prior_value in {None, "not_applicable"} and next_value == "unresolved":
            if change_intent != "routine" or approval_event_id is not None:
                raise ValueError(
                    "New unresolved conflict discovery requires routine intent and null approval_event_id."
                )
        elif prior_value == "preferred" and next_value == "unresolved":
            if change_intent != "resolve_contradiction" or not approval_event_id:
                raise ValueError(
                    "Changing preferred to unresolved requires resolve_contradiction and a nonempty approval_event_id."
                )

    if change_intent == "resolve_contradiction" and transitions != 1:
        raise ValueError(
            "A resolve_contradiction manifest requires exactly one QuestionRecord decision transition."
        )
