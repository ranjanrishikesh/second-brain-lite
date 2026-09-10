from __future__ import annotations

import ctypes
import errno
import json
import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath

import pytest

import brainlib.wiki_transaction as wiki_transaction
from brainlib.graph import LinkCandidateRunProof
from brainlib.ledger import CitationRewrite
from brainlib.wiki_transaction import (
    ApprovalRequired,
    CorpusRevisionChanged,
    InvalidWikiJournal,
    LinkCandidateCoverageError,
    WikiTransactionError,
    WikiPreflightError,
    WikiChange,
    WikiManifest,
    apply_wiki_manifest,
    load_wiki_manifest,
    recover_wiki_transaction,
    validate_wiki_transaction_state,
)
from tests.helpers_knowledge import (
    manifest_for,
    stage_wiki_write,
    write_wiki_manifest,
)


def test_manifest_codec_round_trips_the_canonical_versioned_shape() -> None:
    rewrite = CitationRewrite(
        "src_" + "a" * 64,
        "b" * 64,
        PurePosixPath("_versions/src_" + "a" * 64 + "/" + "b" * 64 + "/original.pdf"),
    )
    proof = LinkCandidateRunProof(
        "srch_" + "c" * 32,
        "d" * 64,
        PurePosixPath("wiki/pages/alpha.md"),
        ("Alpha", "A. Example"),
        "e" * 64,
        3,
        201,
    )
    change = WikiChange.write(
        PurePosixPath("wiki/pages/alpha.md"),
        PurePosixPath(
            ".brain/wiki-staging/wstg_" + "f" * 32 + "/files/wiki/pages/alpha.md"
        ),
        "0" * 64,
    )
    manifest = WikiManifest(
        1,
        "1" * 64,
        "routine",
        None,
        (rewrite,),
        (proof,),
        (change,),
    )

    encoded = manifest.to_json()

    assert encoded.endswith("\n")
    assert WikiManifest.from_json(encoded) == manifest


def test_manifest_codec_rejects_unknown_duplicate_and_cross_run_staging() -> None:
    payload = json.loads(
        WikiManifest(
            1,
            "1" * 64,
            "routine",
            None,
            (),
            (),
            (),
        ).to_json()
    )
    payload["unknown"] = True

    with pytest.raises(ValueError, match="unknown"):
        WikiManifest.from_json(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate JSON key"):
        WikiManifest.from_json(
            '{"schema_version":1,"schema_version":1,"expected_corpus_revision":"'
            + "1" * 64
            + '","change_intent":"routine","approval_event_id":null,'
            '"citation_rewrites":[],"link_candidate_runs":[],"changes":[]}'
        )
    payload.pop("unknown")
    payload["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        WikiManifest.from_json(json.dumps(payload))


def test_manifest_codec_rejects_malformed_types_as_value_errors() -> None:
    payload = json.loads(
        WikiManifest(
            1,
            "1" * 64,
            "routine",
            None,
            (),
            (),
            (),
        ).to_json()
    )
    payload["change_intent"] = []

    with pytest.raises(ValueError, match="change_intent"):
        WikiManifest.from_json(json.dumps(payload))

    proof = {
        "run_id": "srch_" + "a" * 32,
        "corpus_revision": "b" * 64,
        "page_path": "wiki/pages/alpha.md",
        "terms": ["Alpha"],
        "candidate_manifest_sha256": "c" * 64,
        "page_count": 1,
        "candidate_count": 0,
    }
    payload["change_intent"] = "routine"
    payload["link_candidate_runs"] = [proof]

    proof["run_id"] = ["not text"]
    with pytest.raises(ValueError, match="run_id"):
        WikiManifest.from_json(json.dumps(payload))

    proof["run_id"] = "srch_" + "a" * 32
    proof["terms"] = [["not hashable"]]
    with pytest.raises(ValueError, match="terms"):
        WikiManifest.from_json(json.dumps(payload))


def test_manifest_wire_codec_requires_the_approval_pair_without_changing_apply_gate() -> None:
    destructive_without_approval = WikiManifest(
        1,
        "1" * 64,
        "delete",
        None,
        (),
        (),
        (),
    )
    routine_with_approval = WikiManifest(
        1,
        "1" * 64,
        "routine",
        "evt_approved",
        (),
        (),
        (),
    )

    with pytest.raises(ValueError, match="approval_event_id"):
        destructive_without_approval.to_json()
    with pytest.raises(ValueError, match="approval_event_id"):
        routine_with_approval.to_json()

    destructive_payload = destructive_without_approval.to_dict()
    routine_payload = routine_with_approval.to_dict()
    with pytest.raises(ValueError, match="approval_event_id"):
        WikiManifest.from_json(json.dumps(destructive_payload))
    with pytest.raises(ValueError, match="approval_event_id"):
        WikiManifest.from_json(json.dumps(routine_payload))


def test_loaded_manifest_requires_anchored_same_run_regular_single_link_staging(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md", alpha.read_text(encoding="utf-8")
    )
    manifest = manifest_for(scenario, (change,))
    manifest_path = write_wiki_manifest(scenario, manifest)

    assert load_wiki_manifest(scenario.paths, manifest_path) == manifest

    staged = scenario.root / change.staging_path
    linked = staged.with_name("other.md")
    os.link(staged, linked)
    with pytest.raises(ValueError, match="single-link"):
        load_wiki_manifest(scenario.paths, manifest_path)
    linked.unlink()
    outside = scenario.root / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    staged.unlink()
    staged.symlink_to(outside)
    with pytest.raises(ValueError, match="regular"):
        load_wiki_manifest(scenario.paths, manifest_path)


def test_pending_transaction_state_is_read_only_and_requires_recovery(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    revision = "a" * 64

    clean = validate_wiki_transaction_state(
        scenario.paths, corpus_revision=revision
    )
    assert clean.checks == ("wiki-transaction",)
    assert clean.ok

    journal = scenario.root / ".brain/wiki-transaction.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not a journal", encoding="utf-8")
    pending = validate_wiki_transaction_state(
        scenario.paths, corpus_revision=revision
    )

    assert {issue.code for issue in pending.issues} == {"wiki_transaction_pending"}
    assert journal.read_text(encoding="utf-8") == "not a journal"


def test_stale_corpus_revision_and_preflight_graph_failure_touch_no_live_bytes(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    valid_change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md", alpha.read_text(encoding="utf-8")
    )
    baseline = manifest_for(scenario, (valid_change,))
    stale = replace(baseline, expected_corpus_revision="0" * 64)

    with pytest.raises(CorpusRevisionChanged):
        apply_wiki_manifest(scenario.paths, scenario.ledger, stale)
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()

    broken_text = "---\nid: alpha\n---\nnot a wiki record\n"
    broken_change = stage_wiki_write(scenario, "wiki/pages/alpha.md", broken_text)
    preflight_broken = WikiManifest(
        1,
        baseline.expected_corpus_revision,
        "routine",
        None,
        (),
        baseline.link_candidate_runs,
        (broken_change,),
    )

    with pytest.raises(WikiPreflightError):
        apply_wiki_manifest(scenario.paths, scenario.ledger, preflight_broken)
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_tampered_retained_proof_fails_before_live_write(scenario_repo) -> None:
    scenario = scenario_repo("graph/candidates")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- transaction update -->\n",
    )
    manifest = manifest_for(scenario, (change,))
    tampered = replace(
        manifest,
        link_candidate_runs=(
            replace(manifest.link_candidate_runs[0], candidate_count=999_999),
        ),
    )

    with pytest.raises(LinkCandidateCoverageError) as raised:
        apply_wiki_manifest(scenario.paths, scenario.ledger, tampered)

    assert raised.value.diagnostic.code == "link_candidate_search_incomplete"
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_preflight_rejects_a_routine_manifest_that_creates_preferred_state(
    scenario_repo,
) -> None:
    """The canonicalized full postimage must run the decision transition gate."""

    scenario = scenario_repo("workflow/answer")
    question = scenario.paths.wiki_questions / "what-is-alpha.md"
    replacement = (
        question.read_text(encoding="utf-8")
        .replace(
            "interpretation_decision: not_applicable",
            "interpretation_decision: preferred\n"
            "interpretation_preference_citation_id: cite-alpha-exception-line-4\n"
            "interpretation_approval_event_id: approval-1",
        )
    )
    change = stage_wiki_write(scenario, "wiki/questions/what-is-alpha.md", replacement)

    with pytest.raises(WikiPreflightError, match="resolve_contradiction"):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, (change,)))


@pytest.mark.parametrize(
    ("replacement", "intent", "approval_event_id", "error_code"),
    (
        (
            (
                ("answer_status: answered", "answer_status: conflicted"),
                ("interpretation_decision: not_applicable", "interpretation_decision: unresolved"),
            ),
            "routine",
            None,
            "interpretation_unresolved_evidence_insufficient",
        ),
        (
            (
                (
                    "interpretation_decision: not_applicable",
                    "interpretation_decision: preferred\n"
                    "interpretation_preference_citation_id: cite-not-defined\n"
                    "interpretation_approval_event_id: approval-2",
                ),
            ),
            "resolve_contradiction",
            "approval-2",
            "interpretation_preference_citation_missing",
        ),
    ),
)
def test_preflight_rejects_semantically_invalid_interpretation_documents(
    scenario_repo,
    replacement: tuple[tuple[str, str], ...],
    intent: str,
    approval_event_id: str | None,
    error_code: str,
) -> None:
    """Publication checks canonical question semantics, not just citations/transitions."""

    scenario = scenario_repo("workflow/answer")
    question = scenario.paths.wiki_questions / "what-is-alpha.md"
    replacement_text = question.read_text(encoding="utf-8")
    for old, new in replacement:
        replacement_text = replacement_text.replace(old, new)
    change = stage_wiki_write(
        scenario, "wiki/questions/what-is-alpha.md", replacement_text
    )

    with pytest.raises(WikiPreflightError, match=error_code):
        apply_wiki_manifest(
            scenario.paths,
            scenario.ledger,
            manifest_for(
                scenario,
                (change,),
                intent=intent,
                approval_event_id=approval_event_id,
            ),
        )


def test_missing_in_memory_proof_is_a_coverage_gate_before_live_write(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- update -->\n",
    )
    manifest = replace(manifest_for(scenario, (change,)), link_candidate_runs=())

    with pytest.raises(LinkCandidateCoverageError) as raised:
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest)

    assert raised.value.diagnostic.code == "link_candidate_search_incomplete"
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_destructive_intent_and_routine_record_removal_require_approval(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    manifest = manifest_for(scenario, (), intent="delete")

    with pytest.raises(ApprovalRequired):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest)


def test_approved_removal_reconciles_all_inbound_records_and_generated_index(
    scenario_repo,
) -> None:
    scenario = scenario_repo("removal/approved")
    beta_after = (
        scenario.root
        / "tests/fixtures/wiki/scenarios/removal/approved/beta-after-removal.md"
    ).read_text(encoding="utf-8")
    inbound_after = (
        scenario.root
        / "tests/fixtures/wiki/scenarios/removal/approved/zzz-inbound-after-removal.md"
    ).read_text(encoding="utf-8")
    changes = (
        WikiChange.delete(PurePosixPath("wiki/pages/alpha.md")),
        stage_wiki_write(scenario, "wiki/pages/beta.md", beta_after),
        stage_wiki_write(scenario, "wiki/pages/zzz-inbound.md", inbound_after),
    )
    routine = manifest_for(scenario, changes)

    with pytest.raises(ApprovalRequired):
        apply_wiki_manifest(scenario.paths, scenario.ledger, routine)

    approved = manifest_for(
        scenario,
        changes,
        intent="delete",
        approval_event_id="approval-removal-2026-09-06",
    )
    result = apply_wiki_manifest(scenario.paths, scenario.ledger, approved)

    assert result.changed_paths == (
        PurePosixPath("wiki/index.md"),
        PurePosixPath("wiki/pages/alpha.md"),
        PurePosixPath("wiki/pages/beta.md"),
        PurePosixPath("wiki/pages/zzz-inbound.md"),
    )
    assert not (scenario.paths.wiki_pages / "alpha.md").exists()
    assert "alpha.md" not in (scenario.root / "wiki/index.md").read_text(
        encoding="utf-8"
    )


def test_interrupted_publish_retains_journal_and_recovery_restores_preimage(
    scenario_repo,
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    beta = scenario.paths.wiki_pages / "beta.md"
    before = {alpha: alpha.read_bytes(), beta: beta.read_bytes()}
    changes = (
        stage_wiki_write(
            scenario,
            "wiki/pages/alpha.md",
            alpha.read_text(encoding="utf-8") + "\n<!-- update alpha -->\n",
        ),
        stage_wiki_write(
            scenario,
            "wiki/pages/beta.md",
            beta.read_text(encoding="utf-8") + "\n<!-- update beta -->\n",
        ),
    )
    replacements = 0

    def interrupting_replace(source: Path, target: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated interruption")
        os.replace(source, target)

    with pytest.raises(OSError, match="simulated interruption"):
        apply_wiki_manifest(
            scenario.paths,
            scenario.ledger,
            manifest_for(scenario, changes),
            replace_file=interrupting_replace,
        )

    assert (scenario.root / ".brain/wiki-transaction.json").is_file()
    recovery = recover_wiki_transaction(scenario.paths)
    assert recovery.recovered is True
    assert {path: path.read_bytes() for path in before} == before
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_invalid_journal_fails_closed_without_mutating_live_records(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    journal = scenario.root / ".brain/wiki-transaction.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n", encoding="utf-8")

    with pytest.raises(InvalidWikiJournal):
        recover_wiki_transaction(scenario.paths)

    assert alpha.read_bytes() == original
    assert journal.is_file()


def test_recovery_rejects_a_third_state_before_restoring_any_entry(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    question = scenario.paths.wiki_questions / "what-is-alpha.md"
    original_question = question.read_bytes()
    changes = (
        stage_wiki_write(
            scenario,
            "wiki/pages/alpha.md",
            alpha.read_text(encoding="utf-8") + "\n<!-- alpha update -->\n",
        ),
        stage_wiki_write(
            scenario,
            "wiki/questions/what-is-alpha.md",
            question.read_text(encoding="utf-8") + "\n<!-- question update -->\n",
        ),
    )
    replacements = 0

    def interrupt_after_first(source: Path, target: Path) -> None:
        nonlocal replacements
        replacements += 1
        if replacements == 2:
            raise OSError("simulated interruption")
        os.replace(source, target)

    with pytest.raises(OSError, match="simulated interruption"):
        apply_wiki_manifest(
            scenario.paths,
            scenario.ledger,
            manifest_for(scenario, changes),
            replace_file=interrupt_after_first,
        )
    alpha.write_text("third state", encoding="utf-8")

    with pytest.raises(InvalidWikiJournal, match="unexpected"):
        recover_wiki_transaction(scenario.paths)

    assert question.read_bytes() == original_question
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()


def test_recovery_preserves_successor_after_prior_absence_validation(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    (scenario.root / ".brain").mkdir()
    logical_path = PurePosixPath("wiki/pages/gamma.md")
    successor = b"unrelated successor"
    entry = wiki_transaction._JournalEntry(
        logical_path,
        False,
        None,
        hashlib.sha256(b"intended transaction bytes").hexdigest(),
        None,
    )
    wiki_transaction._write_journal(scenario.paths, (entry,))
    gamma = scenario.paths.wiki_pages / logical_path.name
    real_validate = wiki_transaction._validate_journal_current_entry
    validations = 0

    def add_after_absence_validation(paths, journal_entry):
        nonlocal validations
        result = real_validate(paths, journal_entry)
        validations += 1
        if validations == 2:
            gamma.write_bytes(successor)
        return result

    monkeypatch.setattr(
        wiki_transaction,
        "_validate_journal_current_entry",
        add_after_absence_validation,
    )

    with pytest.raises(InvalidWikiJournal, match="appeared"):
        recover_wiki_transaction(scenario.paths)

    assert gamma.read_bytes() == successor
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()


def test_a_racing_preexisting_journal_never_allows_a_second_publication(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- update -->\n",
    )
    real_write_journal = wiki_transaction._write_journal

    def racing_write_journal(paths, entries) -> None:
        journal = paths.root / ".brain/wiki-transaction.json"
        journal.write_bytes(wiki_transaction._journal_payload(entries))
        return real_write_journal(paths, entries)

    monkeypatch.setattr(wiki_transaction, "_write_journal", racing_write_journal)

    with pytest.raises(InvalidWikiJournal):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == original
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()


def test_snapshot_refuses_a_live_successor_after_postimage_preflight(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    concurrent = original + b"\n<!-- concurrent successor -->\n"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_snapshot = wiki_transaction._snapshot_journal_entries

    def successor_snapshot(paths, planned, preimages):
        alpha.write_bytes(concurrent)
        return real_snapshot(paths, planned, preimages)

    monkeypatch.setattr(
        wiki_transaction, "_snapshot_journal_entries", successor_snapshot
    )

    with pytest.raises(WikiTransactionError, match="changed during preflight"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == concurrent
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_apply_rechecks_staging_hashes_immediately_before_journaling(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    staged = scenario.root / change.staging_path
    real_snapshot = wiki_transaction._snapshot_journal_entries

    def tamper_after_preimage_snapshot(paths, planned, preimages):
        entries = real_snapshot(paths, planned, preimages)
        staged.write_text("tampered staging bytes", encoding="utf-8")
        return entries

    monkeypatch.setattr(
        wiki_transaction,
        "_snapshot_journal_entries",
        tamper_after_preimage_snapshot,
    )

    with pytest.raises(ValueError, match="checksum"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_snapshot_refuses_an_untouched_live_record_change_after_preflight(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    question = scenario.paths.wiki_questions / "what-is-alpha.md"
    alpha_before = alpha.read_bytes()
    question_successor = question.read_bytes() + b"\n<!-- concurrent successor -->\n"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_snapshot = wiki_transaction._snapshot_journal_entries

    def successor_snapshot(paths, planned, preimages):
        question.write_bytes(question_successor)
        return real_snapshot(paths, planned, preimages)

    monkeypatch.setattr(
        wiki_transaction, "_snapshot_journal_entries", successor_snapshot
    )

    with pytest.raises(WikiTransactionError, match="changed during preflight"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == alpha_before
    assert question.read_bytes() == question_successor
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_snapshot_refuses_a_new_logical_record_after_postimage_preflight(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    gamma = scenario.paths.wiki_pages / "gamma.md"
    gamma_text = """\
---
id: gamma
title: Gamma
description: Gamma is a documented topic.
type: concept
aliases: []
created: 2026-09-04
updated: 2026-09-04
---
# Gamma

## Summary
Gamma is a concurrently added valid topic.

## Details
Gamma has no relationships.

## Related pages

## Related questions

## Sources
"""
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_snapshot = wiki_transaction._snapshot_journal_entries

    def successor_snapshot(paths, planned, preimages):
        gamma.write_text(gamma_text, encoding="utf-8")
        return real_snapshot(paths, planned, preimages)

    monkeypatch.setattr(
        wiki_transaction, "_snapshot_journal_entries", successor_snapshot
    )

    with pytest.raises(WikiTransactionError, match="changed during preflight"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == original
    assert gamma.read_text(encoding="utf-8") == gamma_text
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_rechecks_candidate_proof_against_the_preflight_live_mapping(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    gamma = scenario.paths.wiki_pages / "gamma.md"
    gamma_text = """\
---
id: gamma
title: Gamma
description: Gamma is a documented topic.
type: concept
aliases: []
created: 2026-09-04
updated: 2026-09-04
---
# Gamma

## Summary
Gamma is a concurrently added valid topic.

## Details
Gamma has no relationships.

## Related pages

## Related questions

## Sources
"""
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_preflight = wiki_transaction._preflight_postimage

    def add_after_preflight(*args, **kwargs):
        result = real_preflight(*args, **kwargs)
        gamma.write_text(gamma_text, encoding="utf-8")
        return result

    monkeypatch.setattr(wiki_transaction, "_preflight_postimage", add_after_preflight)

    with pytest.raises(LinkCandidateCoverageError, match="link-candidate search"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert gamma.read_text(encoding="utf-8") == gamma_text
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_recovery_refuses_forged_repo_paths_before_touching_outside_target(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/valid")
    outside = scenario.root / "outside-pages"
    outside.mkdir()
    successor = outside / "alpha.md"
    successor.write_bytes(b"outside successor")
    entry = wiki_transaction._JournalEntry(
        PurePosixPath("wiki/pages/alpha.md"),
        False,
        None,
        hashlib.sha256(successor.read_bytes()).hexdigest(),
        None,
    )
    journal = scenario.root / ".brain"
    journal.mkdir(exist_ok=True)
    (journal / "wiki-transaction.json").write_bytes(
        wiki_transaction._journal_payload((entry,))
    )
    forged = replace(scenario.paths, wiki_pages=outside)

    with pytest.raises(ValueError, match="wiki roots"):
        recover_wiki_transaction(forged)

    assert successor.read_bytes() == b"outside successor"
    assert (journal / "wiki-transaction.json").is_file()


def test_delete_claim_refuses_a_swapped_successor_without_unlinking_it(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    successor = b"concurrent successor"
    real_read = wiki_transaction._read_regular_at
    swapped = False

    def swapping_read(directory_fd, name, **kwargs):
        nonlocal swapped
        result = real_read(directory_fd, name, **kwargs)
        if (
            name == "alpha.md"
            and kwargs.get("label") == "wiki target"
            and not swapped
        ):
            swapped = True
            alpha.write_bytes(successor)
        return result

    monkeypatch.setattr(wiki_transaction, "_read_regular_at", swapping_read)

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        wiki_transaction._delete_live_target(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            expected_bytes=original,
            expected_sha256=None,
            allow_absent=False,
        )

    assert alpha.read_bytes() == successor


def test_write_claim_refuses_a_swapped_successor_without_overwriting_it(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    successor = b"concurrent successor"
    replacement = original + b"\n<!-- planned replacement -->\n"
    real_read = wiki_transaction._read_regular_at
    swapped = False

    def swapping_read(directory_fd, name, **kwargs):
        nonlocal swapped
        result = real_read(directory_fd, name, **kwargs)
        if (
            name == "alpha.md"
            and kwargs.get("label") == "wiki target"
            and not swapped
        ):
            swapped = True
            alpha.write_bytes(successor)
        return result

    monkeypatch.setattr(wiki_transaction, "_read_regular_at", swapping_read)

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            replacement,
            replace_file=os.replace,
            expected_current=original,
        )

    assert alpha.read_bytes() == successor


def test_recovery_restores_an_absent_target_left_in_the_claim_gap(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_claim = wiki_transaction._claim_regular
    interrupted = False

    def interrupt_after_claim(*args, **kwargs):
        nonlocal interrupted
        quarantine = real_claim(*args, **kwargs)
        if kwargs.get("label") == "wiki target" and not interrupted:
            interrupted = True
            raise OSError("simulated crash after target claim")
        return quarantine

    monkeypatch.setattr(wiki_transaction, "_claim_regular", interrupt_after_claim)

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert not alpha.exists()
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()
    monkeypatch.setattr(wiki_transaction, "_claim_regular", real_claim)

    recovery = recover_wiki_transaction(scenario.paths)

    assert recovery.recovered is True
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_recovery_finds_a_canonical_journal_left_by_interrupted_tombstone_move(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_rename = wiki_transaction._rename_no_replace_at
    interrupted = False

    def interrupt_before_final_tombstone_move(directory_fd, source, target):
        nonlocal interrupted
        if (
            source == wiki_transaction._JOURNAL_NAME
            and target.startswith(wiki_transaction._JOURNAL_TOMBSTONE_PREFIX)
            and not interrupted
        ):
            interrupted = True
            raise OSError("simulated crash before tombstone move")
        return real_rename(directory_fd, source, target)

    monkeypatch.setattr(
        wiki_transaction,
        "_rename_no_replace_at",
        interrupt_before_final_tombstone_move,
    )

    with pytest.raises(WikiTransactionError, match="failed before commit"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert (scenario.root / ".brain/wiki-transaction.json").is_file()
    monkeypatch.setattr(wiki_transaction, "_rename_no_replace_at", real_rename)

    recovery = recover_wiki_transaction(scenario.paths)

    assert recovery.recovered is True
    assert alpha.read_bytes() == original
    assert not (scenario.root / ".brain/wiki-transaction.json").exists()


def test_default_publication_uses_no_hardlink_transition(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )

    def hardlink_is_forbidden(*_args, **_kwargs):
        raise AssertionError("publication must not create a hard-link transition")

    monkeypatch.setattr(wiki_transaction.os, "link", hardlink_is_forbidden)

    result = apply_wiki_manifest(
        scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
    )

    assert PurePosixPath("wiki/pages/alpha.md") in result.changed_paths


def test_completed_retained_journal_does_not_block_future_apply_or_recovery(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    first_text = alpha.read_text(encoding="utf-8") + "\n<!-- first update -->\n"
    first = stage_wiki_write(scenario, "wiki/pages/alpha.md", first_text)

    apply_wiki_manifest(
        scenario.paths, scenario.ledger, manifest_for(scenario, (first,))
    )

    assert recover_wiki_transaction(scenario.paths).recovered is False
    assert alpha.read_text(encoding="utf-8") == first_text
    second_text = first_text + "\n<!-- second update -->\n"
    second = stage_wiki_write(scenario, "wiki/pages/alpha.md", second_text)

    apply_wiki_manifest(
        scenario.paths, scenario.ledger, manifest_for(scenario, (second,))
    )

    assert alpha.read_text(encoding="utf-8") == second_text
    assert not wiki_transaction._journal_exists(scenario.paths)


def test_apply_refuses_a_published_target_swap_before_journal_cleanup(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha_path = PurePosixPath("wiki/pages/alpha.md")
    alpha = scenario.paths.wiki_pages / alpha_path.name
    original = alpha.read_bytes()
    successor = original + b"\n<!-- concurrent successor -->\n"
    change = stage_wiki_write(
        scenario,
        alpha_path.as_posix(),
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )

    def swap_then_check(paths, expected, *, error_type):
        alpha.write_bytes(successor)
        if wiki_transaction._read_target_bytes(paths, alpha_path) != expected[alpha_path]:
            raise error_type("A live wiki target changed during publication.")

    monkeypatch.setattr(
        wiki_transaction,
        "_verify_live_postimages",
        swap_then_check,
        raising=False,
    )

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        apply_wiki_manifest(
            scenario.paths, scenario.ledger, manifest_for(scenario, (change,))
        )

    assert alpha.read_bytes() == successor
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()


def test_recovery_refuses_a_restored_target_swap_before_journal_cleanup(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )

    def interrupted_replace(source: Path, target: Path) -> None:
        os.replace(source, target)
        raise OSError("simulated interruption")

    with pytest.raises(OSError, match="simulated interruption"):
        apply_wiki_manifest(
            scenario.paths,
            scenario.ledger,
            manifest_for(scenario, (change,)),
            replace_file=interrupted_replace,
        )

    successor = original + b"\n<!-- concurrent successor -->\n"
    alpha_path = PurePosixPath("wiki/pages/alpha.md")

    def swap_then_check(paths, expected, *, error_type):
        alpha.write_bytes(successor)
        if wiki_transaction._read_target_bytes(paths, alpha_path) != expected[alpha_path]:
            raise error_type("A live wiki target changed during recovery.")

    monkeypatch.setattr(
        wiki_transaction,
        "_verify_live_postimages",
        swap_then_check,
        raising=False,
    )

    with pytest.raises(InvalidWikiJournal, match="changed during recovery"):
        recover_wiki_transaction(scenario.paths)

    assert alpha.read_bytes() == successor
    assert (scenario.root / ".brain/wiki-transaction.json").is_file()


def test_write_rejects_a_hardlinked_live_target(scenario_repo) -> None:
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    alias = scenario.paths.wiki_pages / ".alpha-transaction-alias"
    os.link(alpha, alias)
    original = alpha.read_bytes()

    with pytest.raises(ValueError, match="single-link"):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- candidate update -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert alpha.read_bytes() == alias.read_bytes() == original


def test_default_live_write_fails_closed_without_fd_bound_clone(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    monkeypatch.setattr(
        wiki_transaction, "_NATIVE_FCLONE_FILE_AT", None, raising=False
    )

    with pytest.raises(
        wiki_transaction.UnsafeFilesystemError, match="descriptor-bound"
    ):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert alpha.read_bytes() == original


def test_default_live_write_clones_only_an_anonymous_private_source_fd(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    replacement = original + b"\n<!-- planned replacement -->\n"
    real_clone = wiki_transaction._fclone_from_descriptor
    observed_unlinked = False

    def assert_anonymous_source_fd(source_fd, *args, **kwargs):
        nonlocal observed_unlinked
        assert os.fstat(source_fd).st_nlink == 0
        observed_unlinked = True
        return real_clone(source_fd, *args, **kwargs)

    monkeypatch.setattr(
        wiki_transaction, "_fclone_from_descriptor", assert_anonymous_source_fd
    )

    wiki_transaction._write_live_bytes(
        scenario.paths,
        PurePosixPath("wiki/pages/alpha.md"),
        replacement,
        replace_file=os.replace,
        expected_current=original,
    )

    assert observed_unlinked
    assert alpha.read_bytes() == replacement


def test_default_live_write_rejects_an_existing_nonprivate_claim_area_before_mutation(
    scenario_repo,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir()
    claims = brain / wiki_transaction._CLAIM_DIRECTORY
    claims.mkdir(mode=0o700)
    os.chmod(claims, 0o755)
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()

    with pytest.raises(wiki_transaction.UnsafeFilesystemError, match="claim"):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert alpha.read_bytes() == original


def test_default_live_write_rejects_hardlinked_temp_mutation_before_fd_clone(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    attacker_bytes = b"ATTACKER HARDLINK PAYLOAD"
    claims = scenario.root / ".brain" / wiki_transaction._CLAIM_DIRECTORY
    real_unlink = wiki_transaction._unlink_private_payload
    injected = False

    def hardlink_before_private_unlink(claims_dir, temporary, descriptor):
        nonlocal injected
        if not injected:
            alias = claims / "attacker-hardlink-alias"
            os.link(claims / temporary, alias)
            alias.write_bytes(attacker_bytes)
            injected = True
        return real_unlink(claims_dir, temporary, descriptor)

    monkeypatch.setattr(
        wiki_transaction, "_unlink_private_payload", hardlink_before_private_unlink
    )

    with pytest.raises(wiki_transaction.UnsafeFilesystemError):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert injected
    assert alpha.read_bytes() == original
    assert alpha.read_bytes() != attacker_bytes


def test_default_live_write_rejects_a_live_symlink_at_fd_clone_time(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    outside = scenario.root / "outside-target"
    outside.write_bytes(b"outside bytes must remain unchanged")
    native = getattr(wiki_transaction, "_NATIVE_FCLONE_FILE_AT", None)
    assert native is not None
    injected = False

    def place_symlink_before_fd_clone(source_fd, target_dir_fd, target, flags):
        nonlocal injected
        assert flags & 0x0008  # CLONE_NOFOLLOW_ANY
        alpha.symlink_to(outside)
        injected = True
        return native(source_fd, target_dir_fd, target, flags)

    monkeypatch.setattr(
        wiki_transaction, "_NATIVE_FCLONE_FILE_AT", place_symlink_before_fd_clone
    )

    with pytest.raises(OSError):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert injected
    assert alpha.is_symlink()
    assert outside.read_bytes() == b"outside bytes must remain unchanged"


def test_default_live_write_preserves_regular_successor_at_fd_clone_time(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    successor = b"concurrent regular successor"
    native = getattr(wiki_transaction, "_NATIVE_FCLONE_FILE_AT", None)
    assert native is not None
    injected = False

    def place_successor_before_fd_clone(source_fd, target_dir_fd, target, flags):
        nonlocal injected
        alpha.write_bytes(successor)
        injected = True
        return native(source_fd, target_dir_fd, target, flags)

    monkeypatch.setattr(
        wiki_transaction,
        "_NATIVE_FCLONE_FILE_AT",
        place_successor_before_fd_clone,
    )

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert injected
    assert alpha.read_bytes() == successor
    claims = scenario.root / ".brain" / wiki_transaction._CLAIM_DIRECTORY
    assert any(
        retained.read_bytes() == original
        for retained in claims.glob(".brain-tmp-claim-*")
    )


def test_default_claim_mismatch_never_restores_a_swapped_quarantine(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    attacker_bytes = b"ATTACKER CLAIM QUARANTINE PAYLOAD"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- candidate update -->\n",
    )
    real_move = wiki_transaction._move_no_replace
    swapped = False

    def swap_claimed_predecessor(source_parent, source, target_parent, target):
        nonlocal swapped
        result = real_move(source_parent, source, target_parent, target)
        if (
            source == "alpha.md"
            and target.startswith(".brain-tmp-claim-")
            and not swapped
        ):
            claims = scenario.root / ".brain" / wiki_transaction._CLAIM_DIRECTORY
            attacker = claims / "attacker-claimed-predecessor"
            attacker.write_bytes(attacker_bytes)
            os.replace(attacker, claims / target)
            swapped = True
        return result

    monkeypatch.setattr(wiki_transaction, "_move_no_replace", swap_claimed_predecessor)

    with pytest.raises(WikiTransactionError, match="changed during publication"):
        apply_wiki_manifest(
            scenario.paths,
            scenario.ledger,
            manifest_for(scenario, (change,)),
        )

    assert swapped
    if alpha.exists():
        assert alpha.read_bytes() == original
    else:
        assert (scenario.root / ".brain/wiki-transaction.json").is_file()
    assert not alpha.exists() or alpha.read_bytes() != attacker_bytes


def test_failed_fd_publication_restores_snapshot_without_path_claim_restore(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a Darwin-only safe publication primitive")
    scenario = scenario_repo("graph/valid")
    (scenario.root / ".brain").mkdir()
    alpha = scenario.paths.wiki_pages / "alpha.md"
    original = alpha.read_bytes()
    native = getattr(wiki_transaction, "_NATIVE_FCLONE_FILE_AT", None)
    assert native is not None
    claims = scenario.root / ".brain" / wiki_transaction._CLAIM_DIRECTORY
    clone_calls = 0
    real_restore = wiki_transaction._restore_claimed_regular

    def fail_first_fd_clone(source_fd, target_dir_fd, target, flags):
        nonlocal clone_calls
        clone_calls += 1
        if clone_calls == 1:
            attacker = claims / "attacker-claimed-predecessor"
            attacker.write_bytes(b"ATTACKER CLAIM PAYLOAD")
            os.replace(attacker, next(claims.glob(".brain-tmp-claim-*")))
            ctypes.set_errno(errno.EIO)
            return -1
        return native(source_fd, target_dir_fd, target, flags)

    def reject_live_path_restore(claim_parent, quarantine, target_parent, canonical):
        if canonical == "alpha.md":
            raise AssertionError("live target restoration used a mutable claim path")
        return real_restore(claim_parent, quarantine, target_parent, canonical)

    monkeypatch.setattr(
        wiki_transaction, "_NATIVE_FCLONE_FILE_AT", fail_first_fd_clone
    )
    monkeypatch.setattr(
        wiki_transaction, "_restore_claimed_regular", reject_live_path_restore
    )

    with pytest.raises(OSError):
        wiki_transaction._write_live_bytes(
            scenario.paths,
            PurePosixPath("wiki/pages/alpha.md"),
            original + b"\n<!-- planned replacement -->\n",
            replace_file=os.replace,
            expected_current=original,
        )

    assert clone_calls >= 2
    assert alpha.read_bytes() == original


def _journal_entry(*, checksum: str = "a" * 64) -> wiki_transaction._JournalEntry:
    return wiki_transaction._JournalEntry(
        PurePosixPath("wiki/pages/alpha.md"),
        False,
        None,
        checksum,
        None,
    )


def test_journal_cleanup_moves_the_canonical_journal_directly_to_a_retained_tombstone(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    real_rename = wiki_transaction._rename_no_replace_at
    moves: list[tuple[str, str]] = []

    def record_move(directory_fd, source, target):
        moves.append((source, target))
        return real_rename(directory_fd, source, target)

    monkeypatch.setattr(wiki_transaction, "_rename_no_replace_at", record_move)

    wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert len(moves) == 1
    assert moves[0][0] == wiki_transaction._JOURNAL_NAME
    assert moves[0][1].startswith(wiki_transaction._JOURNAL_TOMBSTONE_PREFIX)
    assert not (brain / wiki_transaction._JOURNAL_NAME).exists()
    tombstones = list(brain.glob(wiki_transaction._JOURNAL_TOMBSTONE_PREFIX + "*"))
    assert len(tombstones) == 1
    assert not wiki_transaction._journal_exists(scenario.paths)
    assert wiki_transaction._read_journal(scenario.paths) is None


def test_journal_tombstone_cleanup_suppresses_post_commit_fsync_error(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    real_rename = wiki_transaction._rename_no_replace_at
    moved = False
    post_move_fsyncs = 0

    def record_final_move(directory_fd, source, target):
        nonlocal moved
        result = real_rename(directory_fd, source, target)
        if source == wiki_transaction._JOURNAL_NAME and target.startswith(
            wiki_transaction._JOURNAL_TOMBSTONE_PREFIX
        ):
            moved = True
        return result

    def fail_post_move_fsync(_descriptor):
        nonlocal post_move_fsyncs
        if moved:
            post_move_fsyncs += 1
            raise OSError("simulated post-tombstone fsync failure")

    monkeypatch.setattr(wiki_transaction, "_rename_no_replace_at", record_final_move)
    monkeypatch.setattr(wiki_transaction, "_fsync_directory", fail_post_move_fsync)

    wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert moved
    assert post_move_fsyncs >= 1
    assert not wiki_transaction._journal_exists(scenario.paths)


def test_journal_tombstone_cleanup_suppresses_post_commit_close_error(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    real_rename = wiki_transaction._rename_no_replace_at
    real_close = wiki_transaction._PinnedDirectory.close
    moved = False
    injected = False

    def record_final_move(directory_fd, source, target):
        nonlocal moved
        result = real_rename(directory_fd, source, target)
        if source == wiki_transaction._JOURNAL_NAME and target.startswith(
            wiki_transaction._JOURNAL_TOMBSTONE_PREFIX
        ):
            moved = True
        return result

    def fail_close_after_commit(self):
        nonlocal injected
        real_close(self)
        if moved and not injected:
            injected = True
            raise OSError("simulated post-tombstone-close failure")

    monkeypatch.setattr(wiki_transaction, "_rename_no_replace_at", record_final_move)
    monkeypatch.setattr(wiki_transaction._PinnedDirectory, "close", fail_close_after_commit)

    wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert moved
    assert injected
    assert not wiki_transaction._journal_exists(scenario.paths)


def test_journal_tombstone_move_failure_keeps_the_canonical_journal_pending(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))

    def fail_final_move(*_args, **_kwargs):
        raise OSError("simulated tombstone move failure")

    monkeypatch.setattr(wiki_transaction, "_rename_no_replace_at", fail_final_move)

    with pytest.raises(WikiTransactionError, match="failed before commit"):
        wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert (brain / wiki_transaction._JOURNAL_NAME).is_file()
    assert wiki_transaction._journal_exists(scenario.paths)
    assert wiki_transaction._read_journal(scenario.paths) is not None


def test_journal_tombstone_move_that_reports_eio_after_success_is_clean(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    real_rename = wiki_transaction._rename_no_replace_at
    injected = False

    def rename_then_report_eio(directory_fd, source, target):
        nonlocal injected
        result = real_rename(directory_fd, source, target)
        if source == wiki_transaction._JOURNAL_NAME and target.startswith(
            wiki_transaction._JOURNAL_TOMBSTONE_PREFIX
        ):
            injected = True
            raise OSError(errno.EIO, "simulated ambiguous tombstone move")
        return result

    monkeypatch.setattr(
        wiki_transaction, "_rename_no_replace_at", rename_then_report_eio
    )

    wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert injected
    assert not (brain / wiki_transaction._JOURNAL_NAME).exists()
    assert not wiki_transaction._journal_exists(scenario.paths)
    assert wiki_transaction._read_journal(scenario.paths) is None


def test_journal_tombstone_eio_with_an_unreadable_canonical_probe_fails_closed(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    entered_final_move = False
    real_stat = wiki_transaction.os.stat

    def report_eio_before_final_move(*_args, **_kwargs):
        nonlocal entered_final_move
        entered_final_move = True
        raise OSError(errno.EIO, "simulated pre-move EIO")

    def fail_only_the_outcome_probe(name, *args, **kwargs):
        if entered_final_move and name == wiki_transaction._JOURNAL_NAME:
            raise OSError(errno.EIO, "simulated unreadable canonical probe")
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(
        wiki_transaction, "_rename_no_replace_at", report_eio_before_final_move
    )
    monkeypatch.setattr(wiki_transaction.os, "stat", fail_only_the_outcome_probe)

    with pytest.raises(WikiTransactionError, match="outcome is uncertain"):
        wiki_transaction._remove_journal(scenario.paths, snapshot)

    monkeypatch.setattr(wiki_transaction.os, "stat", real_stat)
    assert (brain / wiki_transaction._JOURNAL_NAME).is_file()
    assert wiki_transaction._journal_exists(scenario.paths)
    assert wiki_transaction._read_journal(scenario.paths) is not None


def test_journal_tombstone_successor_after_applied_eio_is_not_reread(
    scenario_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    snapshot = wiki_transaction._write_journal(scenario.paths, (_journal_entry(),))
    real_rename = wiki_transaction._rename_no_replace_at
    moved_tombstone: str | None = None
    successor_bytes = b"internal tombstone successor"

    def rename_then_report_eio(directory_fd, source, target):
        nonlocal moved_tombstone
        result = real_rename(directory_fd, source, target)
        if source == wiki_transaction._JOURNAL_NAME and target.startswith(
            wiki_transaction._JOURNAL_TOMBSTONE_PREFIX
        ):
            moved_tombstone = target
            successor = brain / "tombstone-successor"
            successor.write_bytes(successor_bytes)
            os.replace(successor, brain / target)
            raise OSError(errno.EIO, "simulated applied-then-EIO")
        return result

    real_read = wiki_transaction._read_regular_at

    def reject_any_post_commit_tombstone_read(directory_fd, name, **kwargs):
        if name == moved_tombstone:
            raise AssertionError("cleanup reread its post-commit tombstone")
        return real_read(directory_fd, name, **kwargs)

    monkeypatch.setattr(
        wiki_transaction, "_rename_no_replace_at", rename_then_report_eio
    )
    monkeypatch.setattr(
        wiki_transaction,
        "_read_regular_at",
        reject_any_post_commit_tombstone_read,
    )

    wiki_transaction._remove_journal(scenario.paths, snapshot)

    assert moved_tombstone is not None
    assert not (brain / wiki_transaction._JOURNAL_NAME).exists()
    assert (brain / moved_tombstone).read_bytes() == successor_bytes
    assert not wiki_transaction._journal_exists(scenario.paths)
    assert wiki_transaction._read_journal(scenario.paths) is None


def test_journal_tombstone_keeps_successful_cleanup_out_of_future_recovery(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/valid")
    brain = scenario.root / ".brain"
    brain.mkdir(exist_ok=True)
    first_snapshot = wiki_transaction._write_journal(
        scenario.paths, (_journal_entry(checksum="a" * 64),)
    )
    wiki_transaction._remove_journal(scenario.paths, first_snapshot)
    second = (_journal_entry(checksum="b" * 64),)
    second_snapshot = wiki_transaction._write_journal(scenario.paths, second)

    assert wiki_transaction._read_journal(scenario.paths) == (second, second_snapshot)
    assert wiki_transaction._journal_exists(scenario.paths)
    wiki_transaction._remove_journal(scenario.paths, second_snapshot)
    assert not wiki_transaction._journal_exists(scenario.paths)
