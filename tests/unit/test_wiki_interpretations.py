from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from brainlib.wiki_models import parse_question


SOURCE_ID = "src_" + "a" * 64
CONTENT_SHA = "b" * 64
DERIVATION_ID = "drv_" + "c" * 64


def test_preferred_interpretation_requires_the_selected_citation_in_both_sections(
    tmp_path: Path,
) -> None:
    """Dropping either marker must make the typed preferred state invalid."""

    path = tmp_path / "topic.md"
    text = _question_text(
        decision="preferred",
        status="answered",
        current="The chosen reading is supported.[^chosen]",
        contradiction="Two readings conflict.[^chosen][^other]",
        preference="chosen",
        approval="approval-1",
    )
    path.write_text(text, encoding="utf-8")
    record = parse_question(path)

    interpretations = importlib.import_module("brainlib.wiki_interpretations")
    issues = interpretations.validate_interpretation_document(path, text, record)

    assert issues == ()


def test_routine_transaction_cannot_create_a_preferred_interpretation() -> None:
    """Changing a preferred choice must require the resolution approval route."""

    path = Path("/repo/wiki/questions/topic.md")
    before = _question_text(
        decision="not_applicable",
        status="answered",
        current="No conflict.",
        contradiction="None.",
    )
    after = _question_text(
        decision="preferred",
        status="answered",
        current="The chosen reading is supported.[^chosen]",
        contradiction="Two readings conflict.[^chosen][^other]",
        preference="chosen",
        approval="approval-1",
    )
    interpretations = importlib.import_module("brainlib.wiki_interpretations")

    with pytest.raises(ValueError, match="resolve_contradiction"):
        interpretations.validate_interpretation_transitions(
            before={path: before},
            after={path: after},
            changed_paths={path},
            change_intent="routine",
            approval_event_id=None,
        )


def test_transition_rejects_rebinding_the_retained_preferred_citation_id() -> None:
    """A label swap cannot silently change the evidence selected by a preference."""

    path = Path("/repo/wiki/questions/topic.md")
    before = _question_text(
        decision="preferred",
        status="answered",
        current="The chosen reading is supported.[^chosen]",
        contradiction="Two readings conflict.[^chosen][^other]",
        preference="chosen",
        approval="approval-1",
    )
    after = (
        before.replace("[^chosen]:", "[^temporary]:")
        .replace("[^other]:", "[^chosen]:")
        .replace("[^temporary]:", "[^other]:")
    )
    interpretations = importlib.import_module("brainlib.wiki_interpretations")

    with pytest.raises(ValueError, match="selected citation.*identity"):
        interpretations.validate_interpretation_transitions(
            before={path: before},
            after={path: after},
            changed_paths={path},
            change_intent="routine",
            approval_event_id=None,
        )


def test_transition_rejects_reusing_an_approval_for_a_different_preference() -> None:
    """Selecting a different preferred citation needs a new approval event."""

    path = Path("/repo/wiki/questions/topic.md")
    before = _question_text(
        decision="preferred",
        status="answered",
        current="The chosen reading is supported.[^chosen]",
        contradiction="Two readings conflict.[^chosen][^other]",
        preference="chosen",
        approval="approval-1",
    )
    after = _question_text(
        decision="preferred",
        status="answered",
        current="The other reading is now preferred.[^other]",
        contradiction="Two readings conflict.[^chosen][^other]",
        preference="other",
        approval="approval-1",
    )
    interpretations = importlib.import_module("brainlib.wiki_interpretations")

    with pytest.raises(ValueError, match="new approval_event_id"):
        interpretations.validate_interpretation_transitions(
            before={path: before},
            after={path: after},
            changed_paths={path},
            change_intent="resolve_contradiction",
            approval_event_id="approval-1",
        )


def _question_text(
    *,
    decision: str,
    status: str,
    current: str,
    contradiction: str,
    preference: str | None = None,
    approval: str | None = None,
) -> str:
    companion = ""
    if preference is not None:
        companion += f"interpretation_preference_citation_id: {preference}\n"
    if approval is not None:
        companion += f"interpretation_approval_event_id: {approval}\n"
    return f"""---
schema_version: 2
id: question-topic
title: Topic
description: A single sentence.
canonical_question: What is Topic?
prior_phrasings: []
answer_status: {status}
corpus_revision: {'d' * 64}
last_researched: 2026-09-04
discovery_terms: [Topic]
expansion_terms: [Alpha]
verification_terms: [Check]
interpretation_decision: {decision}
{companion}---
# Topic
## Current answer
{current}
## Supporting evidence
None.
## Contradictory evidence
{contradiction}
## Related pages
## Sources
[^chosen]: source_id: `{SOURCE_ID}`; content_sha256: `{CONTENT_SHA}`; derivation_id: `{DERIVATION_ID}`; anchor: `line:1`; [original](../../sources/raw/a.txt); [extracted](../../sources/extracted/a.txt/{CONTENT_SHA}/{DERIVATION_ID}.md#line:1)
[^other]: source_id: `src_{'e' * 64}`; content_sha256: `{'f' * 64}`; derivation_id: `drv_{'1' * 64}`; anchor: `line:2`; [original](../../sources/raw/b.txt); [extracted](../../sources/extracted/b.txt/{'f' * 64}/drv_{'1' * 64}.md#line:2)
"""
