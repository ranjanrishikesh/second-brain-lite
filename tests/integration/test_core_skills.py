from pathlib import Path

from brainlib.instructions import load_skill


ROOT = Path(__file__).resolve().parents[2]


def skill(name: str) -> str:
    return load_skill(ROOT, ROOT / ".agents/skills" / name / "SKILL.md").body


def ordered(text: str, phrases: tuple[str, ...]) -> bool:
    offsets = [text.index(phrase) for phrase in phrases]
    return offsets == sorted(offsets)


def test_initialize_skill_is_resumable_and_drains_agent_handoffs() -> None:
    text = skill("brain-initialize")
    assert ordered(
        text,
        (
            "./brain --json doctor",
            "./brain --json init",
            "needs_agent",
            "./brain --json status",
            "./brain --json validate --full",
        ),
    )
    assert "Ask before installing" in text
    assert "Ask before every public-web research event" in text
    assert "complete_with_gaps" in text
    assert "handoff_delivery" in text
    assert "payload.data.handoffs[]" not in text
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert "python3 -m pytest" not in text


def test_initialize_skill_retains_evidence_and_requires_separate_install_approval() -> None:
    text = skill("brain-initialize")
    assert "Never discard sources or ledger records" in text
    assert "separate approval" in text
    assert "resume with `./brain --json doctor`" in text


def test_initialize_skill_acknowledges_each_manifest_before_mutating_or_dispatching() -> None:
    text = skill("brain-initialize")
    assert ordered(
        text,
        (
            "./brain --json init",
            "Every `init` or `source snapshot-url` response carrying `result_manifest`",
            "source consume-sync-result --result-id \"$result_id\"",
            "manifest_path",
            "handoff_delivery",
            "Durably apply/deduplicate those effects",
            "source acknowledge-sync-result",
        ),
    )
    assert "before another source mutation" in text
    assert "never glob or derive an ID" in text
    assert "`source register-extraction` has no result manifest" in text
    assert "SyncResultStore.verify" not in text
    assert "iter_events" not in text


def test_answer_skill_enforces_sync_wiki_first_and_three_passes() -> None:
    text = skill("brain-answer")
    assert ordered(
        text,
        (
            "./brain --json sync",
            "citation_rewrites",
            "./brain --json wiki apply --manifest",
            "--freshness",
            "Search the wiki",
            "Judge sufficiency",
            "--pass discovery",
            "--pass expansion",
            "--pass verification",
            "Persist",
            "./brain --json validate --full",
        ),
    )
    assert "repository-development request" in text
    assert "Do not create a question record" in text
    assert "corpus revision changes" in text
    assert "after every ingestion mutation until stable" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert ".brain/wiki-staging/" in text
    assert "Never write directly to `wiki/`" in text
    assert "WikiEvidencePacket" in text
    assert "one evolving topic Q&A record" in text
    assert './brain --json search --cursor "$next_cursor"' in text
    assert './brain --json links candidates --cursor "$next_cursor"' in text
    assert "If sufficient, skip the three source passes" in text


def test_answer_skill_batches_freshness_and_routes_typed_handoffs() -> None:
    text = skill("brain-answer")
    assert "one logical freshness run" in text
    assert "do not create one run per source/term pair" in text
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text


def test_answer_skill_maps_singular_rewrite_events_to_the_plural_manifest_array() -> None:
    text = skill("brain-answer")
    assert "exact streamed `citation_rewrite` events" in text
    assert "sorted `citation_rewrites` manifest array" in text
    assert "`citation_rewrites` events" not in text


def test_answer_skill_acknowledges_sync_and_snapshot_manifests_before_handoffs() -> None:
    text = skill("brain-answer")
    assert ordered(
        text,
        (
            "./brain --json sync",
            "If a `sync` response carries `result_manifest`",
            "source consume-sync-result --result-id \"$result_id\"",
            "manifest_path",
            "handoff_delivery",
            "Durably apply/deduplicate those effects",
            "source acknowledge-sync-result",
            "For each exact typed item in the consumed `handoff_delivery`",
        ),
    )
    assert "After a `source snapshot-url` response carrying `result_manifest`" in text
    assert "never route from response summaries" in text
    assert "separate acknowledgement before processing its handoffs or making another mutation" in text
    assert "`source register-extraction` itself has no result manifest" in text
    assert "SyncResultStore.verify" not in text
    assert "iter_events" not in text


def test_skills_verify_agent_registration_not_the_pre_registration_snapshot() -> None:
    for name in ("brain-initialize", "brain-answer"):
        text = skill(name)
        assert "data.registration.active_representation" in text
        assert "not the immutable pre-registration `SnapshotResult.active_representation`" in text


def test_answer_skill_routes_a_complete_empty_wiki_run_to_source_research() -> None:
    text = skill("brain-answer")
    no_match = "If the fully drained wiki result has no match or no supported record"
    assert no_match in text
    assert "do not call `build_wiki_evidence_packet(...)`" in text
    assert "proceed directly to exactly the three source passes" in text
    assert text.index(no_match) < text.index("## Search sources")
    assert "incomplete, stale, expired, tampered, or corrupt wiki run" in text


def test_answer_skill_never_trades_evidence_for_speed() -> None:
    text = skill("brain-answer")
    assert "Do not skip synchronization or search for urgency" in text
    assert "partial/unanswered" in text
