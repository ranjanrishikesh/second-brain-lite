from pathlib import Path

from brainlib.instructions import load_skill


ROOT = Path(__file__).resolve().parents[2]


def body(name: str) -> str:
    return load_skill(ROOT, ROOT / ".agents/skills" / name / "SKILL.md").body


def test_web_skill_asks_before_access_and_localizes_used_evidence() -> None:
    text = body("brain-web-research")
    assert text.index("Ask the user") < text.index("Access public-web tools")
    for required in (
        "approval event ID",
        "snapshot-url",
        "Unused candidates",
        "Pass 1",
        "Pass 2",
        "Pass 3",
    ):
        assert required in text
    assert "snapshot-url --source-id" in text
    assert "snapshot-url --url" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert './brain --json search --cursor "$next_cursor"' in text


def test_web_skill_drains_and_acknowledges_each_result_before_next_mutation() -> None:
    text = body("brain-web-research")
    assert "result_manifest" in text
    assert './brain --json source consume-sync-result --result-id "$result_id"' in text
    assert "manifest_path" in text
    assert "effect digest" in text
    assert "durably deduplicate" in text
    assert "source acknowledge-sync-result" in text
    assert "before another snapshot, registration, or source mutation" in text
    assert "SyncResultStore.verify" not in text
    assert "iter_events" not in text


def test_wiki_skill_names_every_approval_gate() -> None:
    text = body("brain-wiki-maintenance")
    for required in (
        "deleting",
        "merging",
        "materially splitting",
        "Removing a sourced claim",
        "contradictory",
        "ambiguous rename",
    ):
        assert required in text
    assert "global string replacement" in text
    assert "reciprocal" in text
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "Never write directly to `wiki/`" in text
    assert './brain --json links candidates --cursor "$next_cursor"' in text
    assert "link_candidate_runs" in text
    assert "./brain --json validate --full" in text


def test_wiki_skill_treats_empty_complete_results_as_normal_but_fails_closed() -> None:
    text = body("brain-wiki-maintenance")
    assert "empty, complete wiki or candidate-search result is normal insufficiency" in text
    assert "malformed, stale, expired, tampered, or incomplete" in text
    assert "fail closed" in text


def test_validate_skill_requires_full_validation_at_handoff() -> None:
    text = body("brain-validate")
    assert "python3 -m pytest -v" in text
    assert "./brain --json validate --full" in text
    assert "exit code alone" in text
    assert "Warnings are acceptable only" in text
