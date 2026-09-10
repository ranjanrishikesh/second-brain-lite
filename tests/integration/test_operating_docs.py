from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

POLICY_ASSERTIONS = {
    "docs/brain/policies/citations.md": (
        "## Immutable citation identity",
        "source_id",
        "content_sha256",
        "derivation_id",
        "## Sources",
    ),
    "docs/brain/policies/wiki.md": (
        "## Page qualification",
        "first meaningful occurrence",
        "reciprocal",
        "approval",
    ),
    "docs/brain/policies/web.md": (
        "## Approval boundary",
        "sources/raw/_web/",
        "unused search results",
        "three source passes",
    ),
}

BRIEF_WRITE_SCOPES = {
    "source-researcher.md": "Write scope: none",
    "source-ingester.md": "Write scope: source artifacts only",
    "wiki-curator.md": "Write scope: wiki artifacts only",
    "brain-auditor.md": "Write scope: none",
}


def test_policies_state_the_non_negotiable_rules() -> None:
    for relative, phrases in POLICY_ASSERTIONS.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in text, f"{relative} must contain {phrase!r}"


def test_agent_briefs_declare_non_overlapping_write_scopes() -> None:
    brief_dir = ROOT / "docs/brain/agent-briefs"
    for filename, declaration in BRIEF_WRITE_SCOPES.items():
        text = (brief_dir / filename).read_text(encoding="utf-8")
        assert declaration in text
        assert "## Required output" in text
        assert "## Stop conditions" in text


def test_answer_workflow_has_one_ordered_pipeline() -> None:
    text = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    headings = [
        "## 1. Classify the request",
        "## 2. Synchronize",
        "## 3. Search the wiki",
        "## 4. Judge sufficiency",
        "## 5. Search sources three times",
        "## 6. Synthesize and persist",
        "## 7. Validate and answer",
    ]
    offsets = [text.index(heading) for heading in headings]
    assert offsets == sorted(offsets)


def test_answer_workflow_uses_exact_cli_pipeline_and_applies_sync_rewrites_first() -> None:
    text = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    commands = (
        "./brain --json sync",
        "./brain --json wiki apply --manifest",
        "./brain --json search --scope sources --freshness",
        "./brain --json search --scope wiki",
        "./brain --json search --scope sources --pass discovery",
        "./brain --json search --scope sources --pass expansion",
        "./brain --json search --scope sources --pass verification",
        "./brain --json validate --full",
    )
    for command in commands:
        assert command in text
    offsets = [text.index(command) for command in commands[:4]]
    assert offsets == sorted(offsets)
    assert text.index("citation_rewrites") < text.index("## 4. Judge sufficiency")
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert "./brain --json search --cursor \"$next_cursor\"" in text
    assert "WikiEvidencePacket" in text
    assert "one evolving topic Q&A record" in text


def test_workflows_route_typed_handoffs_without_crossing_registration_paths() -> None:
    answer = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    web = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    for text in (answer, web):
        assert 'handoff.kind == "extraction"' in text
        assert 'handoff.kind == "rendered_web_capture"' in text
        assert "--rendered-staging-path" in text
        assert "SnapshotResult.active_representation" in text
        assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text


def test_role_briefs_never_allow_direct_live_wiki_edits() -> None:
    text = (ROOT / "docs/brain/agent-briefs/wiki-curator.md").read_text(encoding="utf-8")
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "Never write directly to `wiki/`" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "link_candidate_runs" in text


def test_web_workflow_has_separate_capture_forms_and_exact_renewed_passes() -> None:
    text = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    assert "snapshot-url --source-id" in text
    assert "snapshot-url --url" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert "./brain --json search --cursor \"$next_cursor\"" in text


def test_wiki_workflow_stages_and_applies_instead_of_editing_live_files() -> None:
    text = (ROOT / "docs/brain/workflows/wiki-maintenance.md").read_text(encoding="utf-8")
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "never edit live `wiki/` files directly" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "link_candidate_runs" in text
    assert "./brain --json validate --full" in text


def test_snapshot_receipts_are_consumed_and_acknowledged_before_follow_up_work() -> None:
    text = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    assert "## Capture receipt boundary" in text
    boundary = text.split("## Capture receipt boundary", 1)[1].split("## Research and capture", 1)[0]
    assert "before processing handoffs, registration, or another snapshot" in boundary
    steps = (
        './brain --json source consume-sync-result --result-id "$result_id"',
        "manifest_path",
        "effect digest",
        "deduplicate",
        "durably record",
        './brain --json source acknowledge-sync-result --result-id "$result_id"',
    )
    offsets = [boundary.index(step) for step in steps]
    assert offsets == sorted(offsets)
    assert "exact event counts" in boundary
    assert "snapshot revision" in boundary
    assert "Do not acknowledge" in boundary
    assert "same snapshot arguments" in boundary
    assert text.index("## Capture receipt boundary") < text.index('handoff.kind == "extraction"')
    assert "including rendered captures" in text
    assert "SyncResultStore.verify" not in boundary
    assert "iter_events" not in boundary
    answer = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    assert "capture receipt boundary" in answer
    assert "before processing handoffs, registration, or another snapshot" in answer


def test_detailed_receipt_docs_expose_the_client_cli_not_internal_python() -> None:
    client_docs = (
        ROOT / "BRAIN.md",
        ROOT / "README.md",
        ROOT / "docs/brain/workflows/initialize.md",
        ROOT / "docs/brain/workflows/synchronize.md",
        ROOT / "docs/brain/workflows/answer.md",
        ROOT / "docs/brain/workflows/web-research.md",
    )
    command = './brain --json source consume-sync-result --result-id "$result_id"'
    for path in client_docs:
        text = path.read_text(encoding="utf-8")
        assert command in text
        assert "manifest_path" in text
    policy = (ROOT / "docs/brain/policies/source-handling.md").read_text(encoding="utf-8")
    citation = (ROOT / "docs/brain/schemas/citation.md").read_text(encoding="utf-8")
    assert "SyncResultStore.verify" in policy
    assert "iter_events" in policy
    assert "implementation behind" in policy
    assert command in citation
    assert "implementation behind" in citation


def test_evidence_packet_routes_snapshot_handoffs_only_through_receipt_delivery() -> None:
    text = (ROOT / "docs/brain/schemas/evidence-packet.md").read_text(
        encoding="utf-8"
    )
    steps = (
        './brain --json source consume-sync-result --result-id "$result_id"',
        "manifest_path",
        "handoff_delivery",
        "map typed delivery items to exact manifest effects",
        "apply/deduplicate",
        './brain --json source acknowledge-sync-result --result-id "$result_id"',
        "only then dispatch or register",
    )
    offsets = [text.index(step) for step in steps]
    assert offsets == sorted(offsets)
    for prohibited in (
        "handoff_manifest",
        "`handoffs` summaries",
        "payload.data.handoffs",
    ):
        assert prohibited not in text


def test_capture_completion_accepts_direct_and_registered_representations() -> None:
    for relative in ("answer.md", "web-research.md"):
        text = (ROOT / "docs/brain/workflows" / relative).read_text(encoding="utf-8")
        assert "SnapshotResult.active_representation" in text
        assert "data.registration.active_representation" in text
        assert "data.registration.corpus_revision" in text
        assert "original `SnapshotResult` remains unchanged" in text
    web = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    assert "For a capture completed directly" in web
    assert "After `register-extraction` succeeds" in web
    assert "revalidate every used representation against the latest corpus revision" in web


def test_no_supported_wiki_evidence_routes_to_source_passes_without_a_packet() -> None:
    text = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    sufficiency = text.split("## 4. Judge sufficiency", 1)[1].split("## 5. Search sources three times", 1)[0]
    assert "complete_search_run(search_pages)" in sufficiency
    assert "verify_search_run_proof(...)" in sufficiency
    assert "no matches" in sufficiency
    assert "no supporting citations" in sufficiency
    assert "go directly to Section 5 without calling `build_wiki_evidence_packet(...)`" in sufficiency
    assert "malformed nonempty candidate" in sufficiency
    assert "fail closed" in sufficiency
    assert "Never treat these failures as an empty result" in sufficiency
    sources = text.split("## 5. Search sources three times", 1)[1].split("## 6. Synthesize and persist", 1)[0]
    assert "When Section 4 finds no supported wiki evidence" in sources
    assert "validated `WikiEvidencePacket` is insufficient" in sources
    assert sources.count("--pass discovery") == 1
    assert sources.count("--pass expansion") == 1
    assert sources.count("--pass verification") == 1
