from dataclasses import replace
from pathlib import Path

import pytest

import brainlib.wiki_models as wiki_models
from brainlib.markdown import MarkdownDiagnostic, MarkdownHeading
from brainlib.wiki_models import parse_question
from tests.helpers_knowledge import CONTENT_SHA256, DERIVATION_ID, SOURCE_ID


def test_parse_question_requires_all_three_search_pass_term_sets(tmp_path: Path) -> None:
    record = tmp_path / "topic.md"
    record.write_text(
        f"""---
schema_version: 2
id: question-topic
title: Topic
description: A single sentence.
canonical_question: What is Topic?
prior_phrasings: []
answer_status: answered
interpretation_decision: not_applicable
corpus_revision: {"d" * 64}
last_researched: 2026-09-04
discovery_terms: [Topic]
expansion_terms: [Alpha]
verification_terms: []
---
# Topic
## Current answer
Answer.
## Supporting evidence
[^{SOURCE_ID}-line-1]
## Contradictory evidence
None.
## Related pages
## Sources
[^{SOURCE_ID}-line-1]: source_id: `{SOURCE_ID}`; content_sha256: `{CONTENT_SHA256}`; derivation_id: `{DERIVATION_ID}`; anchor: `line:1`; [original](../../sources/raw/a.txt); [extracted](../../sources/extracted/a.txt/{CONTENT_SHA256}/{DERIVATION_ID}.md#line:1)
""",
        encoding="utf-8",
    )
    parsed = parse_question(record)
    assert parsed.search_terms.verification == ()
    assert parsed.corpus_revision == "d" * 64


def test_parse_question_v2_exposes_a_typed_not_applicable_decision(
    tmp_path: Path,
) -> None:
    """Removing the v2 decision key or accepting a numeric version must fail."""

    record = tmp_path / "topic.md"
    record.write_text(
        _question_text("[Topic]", "[Alpha]", "[]"),
        encoding="utf-8",
    )

    parsed = parse_question(record)

    assert parsed.interpretation == wiki_models.InterpretationDecision(
        "not_applicable", None, None
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda text: text.replace("schema_version: 2\n", ""),
        lambda text: text.replace("schema_version: 2", "schema_version: 1"),
        lambda text: text.replace(
            "interpretation_decision: not_applicable\n",
            "interpretation_decision: not_applicable\n"
            "interpretation_approval_event_id: approval-1\n",
        ),
        lambda text: text.replace(
            "answer_status: answered\ninterpretation_decision: not_applicable",
            "answer_status: answered\ninterpretation_decision: unresolved",
        ),
        lambda text: text.replace(
            "interpretation_decision: not_applicable",
            "interpretation_decision: preferred\n"
            "interpretation_preference_citation_id: chosen",
        ),
    ),
)
def test_parse_question_rejects_noncanonical_v2_interpretation_shapes(
    tmp_path: Path, mutation
) -> None:
    """A missing version, partial companions, or invalid state/status is a bug."""

    record = tmp_path / "topic.md"
    record.write_text(mutation(_question_text("[]", "[]", "[]")), encoding="utf-8")

    with pytest.raises(ValueError):
        parse_question(record)


@pytest.mark.parametrize(
    "status, decision, companion",
    (
        ("unanswered", "not_applicable", ""),
        ("conflicted", "unresolved", ""),
        (
            "partial",
            "preferred",
            "interpretation_preference_citation_id: chosen\n"
            "interpretation_approval_event_id: approval-1\n",
        ),
    ),
)
def test_parse_question_accepts_each_valid_v2_interpretation_shape(
    tmp_path: Path, status: str, decision: str, companion: str
) -> None:
    record = tmp_path / "topic.md"
    record.write_text(
        _question_text("[]", "[]", "[]")
        .replace("answer_status: answered", f"answer_status: {status}")
        .replace(
            "interpretation_decision: not_applicable\n",
            f"interpretation_decision: {decision}\n{companion}",
        ),
        encoding="utf-8",
    )

    assert parse_question(record).interpretation.decision == decision


def test_parse_question_rejects_unknown_status_and_duplicate_sections(tmp_path: Path) -> None:
    record = tmp_path / "topic.md"
    record.write_text(
        "---\nschema_version: 2\nid: question-topic\ntitle: Topic\ndescription: A single sentence.\ncanonical_question: What?\nprior_phrasings: []\nanswer_status: unknown\ninterpretation_decision: not_applicable\ncorpus_revision: "
        + "d" * 64
        + "\nlast_researched: 2026-09-04\ndiscovery_terms: [Topic]\nexpansion_terms: [Alpha]\nverification_terms: [Check]\n---\n# Topic\n## Current answer\nA.\n## Current answer\nB.\n## Supporting evidence\nNone.\n## Contradictory evidence\nNone.\n## Related pages\n## Sources\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        parse_question(record)


def test_parse_question_allows_empty_persisted_terms_for_wiki_only_evidence(
    tmp_path: Path,
) -> None:
    record = tmp_path / "topic.md"
    record.write_text(_question_text("[]", "[]", "[]"), encoding="utf-8")
    parsed = parse_question(record)
    assert parsed.search_terms.discovery == ()
    assert parsed.search_terms.expansion == ()
    assert parsed.search_terms.verification == ()


def test_parse_question_rejects_nested_relationship_content_instead_of_discarding_it(
    tmp_path: Path,
) -> None:
    record = tmp_path / "topic.md"
    record.write_text(
        _question_text("[Topic]", "[Alpha]", "[]").replace(
            "## Sources",
            "### Hidden relationship\n- [Wrong](wrong.md): Must not be skipped.\n## Sources",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Related pages"):
        parse_question(record)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda text: text.replace("# Topic\n", "## Before title\n# Topic\n"),
        lambda text: text + "\n### Late after sources\nNot permitted.\n",
    ),
)
def test_parse_question_requires_first_title_and_terminal_sources(
    tmp_path: Path, mutation
) -> None:
    record = tmp_path / "topic.md"
    record.write_text(mutation(_question_text("[Topic]", "[Alpha]", "[]")), encoding="utf-8")
    with pytest.raises(ValueError):
        parse_question(record)


def test_parse_question_rejects_first_ambiguous_markdown_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "topic.md"
    text = _question_text("[Topic]", "[Alpha]", "[]")
    record.write_text(text, encoding="utf-8")
    real_scan = wiki_models.scan_markdown(text.split("---\n", 2)[2])
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(
            real_scan,
            diagnostics=(
                MarkdownDiagnostic(
                    "markdown_reconciliation_ambiguous",
                    "bad map",
                    4,
                ),
                MarkdownDiagnostic(
                    "markdown_reconciliation_ambiguous",
                    "later bad map",
                    9,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError) as error:
        wiki_models.parse_question(record)

    assert str(error.value) == (
        f"{record}: markdown structure cannot be reconciled at line 4: bad map"
    )


def test_parse_page_rejects_ambiguous_markdown_before_heading_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "topic.md"
    text = _page_text()
    page.write_text(text, encoding="utf-8")
    real_scan = wiki_models.scan_markdown(text.split("---\n", 2)[2])
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(
            real_scan,
            headings=(),
            diagnostics=(
                MarkdownDiagnostic(
                    "markdown_reconciliation_ambiguous",
                    "bad map",
                    4,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError) as error:
        wiki_models.parse_page(page)

    assert str(error.value) == (
        f"{page}: markdown structure cannot be reconciled at line 4: bad map"
    )


def test_parse_page_retains_clean_section_parsing(tmp_path: Path) -> None:
    page = tmp_path / "topic.md"
    page.write_text(_page_text(), encoding="utf-8")

    parsed = wiki_models.parse_page(page)

    assert parsed.title == "Topic"


def test_public_page_parse_refuses_an_unbound_precomputed_scan(tmp_path: Path) -> None:
    page = tmp_path / "topic.md"
    clean = _page_text()
    unsafe = clean.replace("Details.", "unsafe\0details.")
    page.write_text(unsafe, encoding="utf-8")
    precomputed = wiki_models.scan_markdown(clean)

    with pytest.raises(TypeError, match="unexpected keyword argument 'scan'"):
        wiki_models.parse_page(page, text=unsafe, scan=precomputed)  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="Source NUL"):
        wiki_models.parse_page(page, text=unsafe)


def test_internal_page_parser_reuses_a_precomputed_document_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "topic.md"
    text = _page_text()
    page.write_text(text, encoding="utf-8")
    precomputed = wiki_models.scan_markdown(text)

    def unexpected_scan(_body: str):
        raise AssertionError("the supplied scan must be reused")

    monkeypatch.setattr(wiki_models, "scan_markdown", unexpected_scan)

    parsed = wiki_models._parse_page_with_document_scan(
        page, text=text, scan=precomputed
    )

    assert parsed.title == "Topic"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "separator",
    [
        pytest.param(" ", id="space-control"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\x85", id="next-line"),
        pytest.param("\x0b", id="vertical-tab"),
        pytest.param("\x0c", id="form-feed"),
        pytest.param("\x1c", id="file-separator"),
        pytest.param("\x1d", id="group-separator"),
        pytest.param("\x1e", id="record-separator"),
    ],
)
def test_parse_question_uses_only_physical_lines_for_section_slices(
    newline: str, separator: str
) -> None:
    text = _question_document(
        (
            "# What is Alpha?",
            "",
            f"lead{separator}text{separator}more",
            "",
            "## Current answer",
            "Real answer.",
            "Second answer.",
            "Third answer.",
            "## Supporting evidence",
            "None.",
            "## Contradictory evidence",
            "None.",
            "",
            "## Related pages",
            "- [Wrong](wrong.md): Must not be skipped.",
            "## Sources",
        ),
        newline,
    )

    parsed = wiki_models.parse_question(
        Path("wiki/questions/physical-lines.md"), text=text
    )

    assert parsed.related_pages == (
        wiki_models.RelatedTarget("wrong.md", "Must not be skipped."),
    )


@pytest.mark.parametrize(
    "separator",
    [
        pytest.param(" ", id="space-control"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
        pytest.param("\x85", id="next-line"),
        pytest.param("\x0b", id="vertical-tab"),
        pytest.param("\x0c", id="form-feed"),
        pytest.param("\x1c", id="file-separator"),
        pytest.param("\x1d", id="group-separator"),
        pytest.param("\x1e", id="record-separator"),
    ],
)
def test_parse_question_preserves_nonphysical_separators_in_relationships(
    separator: str,
) -> None:
    text = _question_document(
        (
            "# What is Alpha?",
            "## Current answer",
            "Real answer.",
            "## Supporting evidence",
            "None.",
            "## Contradictory evidence",
            "None.",
            "## Related pages",
            f"- [Alpha](alpha.md): First{separator}second.",
            "## Sources",
        ),
        "\n",
    )

    parsed = wiki_models.parse_question(
        Path("wiki/questions/relationship-content.md"), text=text
    )

    assert parsed.related_pages == (
        wiki_models.RelatedTarget("alpha.md", f"First{separator}second."),
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "current_answer_heading",
    [("## Current answer",), ("Current answer", "---")],
    ids=["atx", "setext"],
)
def test_parse_question_requires_content_after_the_complete_heading_span(
    newline: str, current_answer_heading: tuple[str, ...]
) -> None:
    text = _question_document(
        (
            "# What is Alpha?",
            *current_answer_heading,
            "## Supporting evidence",
            "None.",
            "## Contradictory evidence",
            "None.",
            "## Related pages",
            "## Sources",
        ),
        newline,
    )

    with pytest.raises(ValueError, match="Current answer must be nonempty"):
        wiki_models.parse_question(
            Path("wiki/questions/empty-current-answer.md"), text=text
        )


def test_parse_question_rejects_a_heading_without_a_parser_backed_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("wiki/questions/missing-heading-span.md")
    text = _question_text("[Topic]", "[Alpha]", "[]")
    real_scan = wiki_models.scan_markdown(text.split("---\n", 2)[2])
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(
            real_scan,
            headings=tuple(
                MarkdownHeading(heading.level, heading.text, heading.line)
                for heading in real_scan.headings
            ),
        ),
    )

    with pytest.raises(ValueError) as error:
        wiki_models.parse_question(path, text=text)

    assert str(error.value) == (
        f"{path}: markdown heading span cannot be reconciled at line 1"
    )


@pytest.mark.parametrize(
    "line,end_line",
    [(1, True), (1, 1.0), (False, 1), ("bad", 1)],
)
def test_parse_question_rejects_malformed_heading_span_types_deterministically(
    monkeypatch: pytest.MonkeyPatch, line: object, end_line: object
) -> None:
    path = Path("wiki/questions/malformed-heading-span.md")
    text = _question_text("[Topic]", "[Alpha]", "[]")
    real_scan = wiki_models.scan_markdown(text.split("---\n", 2)[2])
    first, *remaining = real_scan.headings
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(
            real_scan,
            headings=(
                MarkdownHeading(
                    first.level,
                    first.text,
                    line,  # type: ignore[arg-type]
                    end_line=end_line,  # type: ignore[arg-type]
                ),
                *remaining,
            ),
        ),
    )

    with pytest.raises(ValueError) as error:
        wiki_models.parse_question(path, text=text)

    assert str(error.value) == (
        f"{path}: markdown heading span cannot be reconciled at line 1"
    )


def test_parse_question_validates_later_heading_spans_before_section_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("wiki/questions/later-malformed-heading-span.md")
    text = _question_text("[Topic]", "[Alpha]", "[]")
    real_scan = wiki_models.scan_markdown(text.split("---\n", 2)[2])
    headings = list(real_scan.headings)
    malformed = headings[2]
    headings[2] = MarkdownHeading(
        malformed.level,
        malformed.text,
        "bad",  # type: ignore[arg-type]
        end_line=malformed.end_line,
    )
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(real_scan, headings=tuple(headings)),
    )

    with pytest.raises(ValueError) as error:
        wiki_models.parse_question(path, text=text)

    assert str(error.value) == (
        f"{path}: markdown heading span cannot be reconciled at line 1"
    )


def _question_text(discovery: str, expansion: str, verification: str) -> str:
    return f"""---
schema_version: 2
id: question-topic
title: Topic
description: A single sentence.
canonical_question: What is Topic?
prior_phrasings: []
answer_status: answered
interpretation_decision: not_applicable
corpus_revision: {"d" * 64}
last_researched: 2026-09-04
discovery_terms: {discovery}
expansion_terms: {expansion}
verification_terms: {verification}
---
# Topic
## Current answer
Answer.
## Supporting evidence
None.
## Contradictory evidence
None.
## Related pages
- [Alpha](alpha.md): The related topic.
## Sources
"""


def _question_document(body: tuple[str, ...], newline: str) -> str:
    return newline.join(
        (
            "---",
            "schema_version: 2",
            "id: question-alpha",
            "title: What is Alpha?",
            "description: A single sentence.",
            "canonical_question: What is Alpha?",
            "prior_phrasings: []",
            "answer_status: answered",
            "interpretation_decision: not_applicable",
            f"corpus_revision: {'d' * 64}",
            "last_researched: 2026-09-04",
            "discovery_terms: [Alpha]",
            "expansion_terms: []",
            "verification_terms: []",
            "---",
            *body,
            "",
        )
    )


def _page_text() -> str:
    return """---
id: topic
title: Topic
description: A single sentence.
type: concept
aliases: []
created: 2026-09-04
updated: 2026-09-06
---
# Topic
## Summary
Summary.
## Details
Details.
## Related pages
## Related questions
## Sources
"""
