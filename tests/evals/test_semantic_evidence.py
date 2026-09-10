"""Semantic evidence regressions; all writable product work uses temporary copies."""

import copy
import hashlib
import importlib
import io
import json
import shutil
from pathlib import Path

import pytest

from brainlib.cli import main
from brainlib.contracts import SourceRecord, compute_corpus_revision
from brainlib.ledger import representation_for
from brainlib.sync import _representation_data
from tests.evals.test_event_evidence import CapturedEvidence

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sem():
    return importlib.import_module("tests.evals.semantic_evidence")


def capture_source_state(evidence, workspace, command_id, *, result_id=None):
    """Capture actual temporary-workspace bytes for semantic contract tests."""
    records, files, typed = [], [], []
    for path in sorted((workspace / "sources/ledger").glob("src_*.json")):
        artifact = evidence.add(command_id + "-" + path.stem, "source_record", path.read_bytes())
        records.append({"path": path.relative_to(workspace).as_posix(), "artifact_id": artifact})
        typed.append(SourceRecord.from_dict(json.loads(path.read_bytes())))
    paths = [workspace / "config/extractors.toml"]
    for record in typed:
        paths.extend(workspace / "sources/raw" / version.raw_path for version in record.versions.values())
        paths.extend(workspace / item.output_path for item in record.derivations.values())
    for index, path in enumerate(sorted(set(paths))):
        artifact = evidence.add(f"{command_id}-file-{index}", "file_capture", path.read_bytes())
        files.append({"path": path.relative_to(workspace).as_posix(), "artifact_id": artifact})
    pending_id = None
    pending_path = workspace / ".brain/sync-results/pending.json"
    if result_id is not None and pending_path.is_file():
        pending_bytes = pending_path.read_bytes()
        pending = json.loads(pending_bytes)
        if pending.get("command") in {"init", "sync"}:
            pending_id = evidence.add(command_id + "-pending-result", "pending_sync_result", pending_bytes)
    state = {"schema_version": 1, "run_id": evidence.run_id, "execution_id": "main", "command_id": command_id,
             "capture_point": "after_command_before_return", "result_id": result_id,
             "corpus_revision": compute_corpus_revision(typed), "records": records, "files": files,
             "pending_result_id": pending_id}
    state_id = evidence.add(command_id + "-source-state", "source_state", state)
    observation = evidence.obj(command_id, "command_observation")
    observation["source_state_id"] = state_id
    evidence.add(command_id, "command_observation", observation)
    return state_id


@pytest.fixture
def real_sync(tmp_path):
    workspace = tmp_path / "repo"
    shutil.copytree(ROOT / "tests/evals/fixtures/repository-development-not-archived/repo", workspace)
    for name in ("AGENTS.md", "BRAIN.md", "brain", "pyproject.toml"):
        shutil.copyfile(ROOT / name, workspace / name)
    output = io.StringIO()
    assert main(["--json", "sync"], cwd=workspace, stdout=output, stderr=io.StringIO()) == 0
    return json.loads(output.getvalue())


def test_complete_real_sync_response_parses(sem, real_sync):
    assert sem.parse_manifest_response(real_sync, observed_exit_code=0).command == "sync"


@pytest.mark.parametrize("field,value", [("status", False), ("corpus_revision", True),
    ("hashed_path_count", True), ("coverage_gap_count", 0.0), ("decision_counts", []),
    ("sampled_decisions", [True]), ("hashed_paths", [True]), ("handoffs", [{}])])
def test_full_response_rejects_malformed_ignored_fields(sem, real_sync, field, value):
    real_sync["data"][field] = value
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_manifest_response(real_sync, observed_exit_code=0)


def test_manifest_gap_response_requires_accounted_exit_one(sem, real_sync):
    real_sync["data"]["result_manifest"]["event_counts"]["coverage_gap"] = 1
    real_sync["data"]["coverage_gap_count"] = 1
    real_sync["data"]["status"] = "complete_with_gaps"
    real_sync["ok"] = False
    real_sync["errors"] = [{"code": "source_coverage_gaps", "message": "Synchronization completed with unresolved source coverage gaps.", "path": None, "details": {}}]
    assert sem.parse_manifest_response(real_sync, observed_exit_code=1).reference.event_counts["coverage_gap"] == 1
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_manifest_response(real_sync, observed_exit_code=0)


@pytest.mark.parametrize("kind,data", [
    ("new_active_representation", {"source_id": True}),
    ("handoff_source_id", {"source_id": True, "record_sha256": "a" * 64}),
    ("hashed_path", {"path": "../escape"}),
    ("citation_rewrite", {"source_id": "src_" + "1" * 64, "content_sha256": True, "raw_path": "a.txt"}),
    ("coverage_gap", {"code": [], "message": "gap", "path": None, "details": {}}),
    ("coverage_gap", {"code": "gap", "message": "gap", "path": None, "details": {}, "source_id": None}),
])
def test_public_effect_payloads_fail_closed(sem, kind, data):
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_sync_effect(kind, data)


@pytest.fixture
def captured():
    workspace = ROOT / "tests/evals/fixtures/current-wiki-fast-path/repo"
    evidence = CapturedEvidence()
    evidence.add("capture", "command_observation", {"source_state_id": None})
    capture_source_state(evidence, workspace, "capture")
    state_value = evidence.obj("capture-source-state", "source_state")
    return evidence, state_value, workspace


def test_source_capture_validates_real_records_and_activation(sem, captured):
    evidence, value, _ = captured
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    record = next(iter(state.records.values()))
    rep = representation_for(record, record.active_content_sha256, record.active_derivation_id)
    assert sem.require_active_representation(_representation_data(rep), state).source_id == record.source_id
    bad = _representation_data(rep)
    bad["output_sha256"] = "0" * 64
    with pytest.raises(sem.SemanticEvidenceError):
        sem.require_active_representation(bad, state)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(command_id="other"),
    lambda value: value.update(records=[]),
    lambda value: value["records"].append(copy.deepcopy(value["records"][0])),
    lambda value: value.update(schema_version=True),
])
def test_source_capture_rejects_wrong_time_subset_duplicate_or_type(sem, captured, mutation):
    evidence, value, _ = captured
    revision = value["corpus_revision"]
    mutation(value)
    evidence.add("capture-source-state", "source_state", value)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.read_source_state(evidence, "capture", result_id=None, revision=revision, execution_id="main")


def test_handoff_effect_binds_full_record_even_when_revision_unchanged(sem, captured):
    from brainlib.ledger import _canonical_record_payload
    evidence, value, _ = captured
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    record = next(iter(state.records.values()))
    effect = {"kind": "handoff_source_id", "data": {"source_id": record.source_id, "record_sha256": hashlib.sha256(_canonical_record_payload(record)).hexdigest()}}
    effect["data"]["record_sha256"] = "0" * 64
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_stream_effects([effect], state)


def test_web_persistence_rejects_substrings_without_resolving_citation(sem, captured):
    evidence, value, _ = captured
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    record = next(iter(state.records.values()))
    rep = _representation_data(representation_for(record, record.active_content_sha256, record.active_derivation_id))
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_promoted_web_citations("sources/raw/_web/ content_sha256: `" + "0" * 64 + "`", "wiki/questions/test.md", state, rep)


@pytest.fixture
def real_web(repo_root, repo_paths):
    from tests.helpers_extractors import mock_web_transport, run_brain_json, web_test_services
    result = run_brain_json(repo_root, "source", "snapshot-url", "--url", "https://example.test/report",
                           "--description", "fixture", "--approval-event-id", "evt_test", "--approval-scope", "one URL",
                           "--approval-note", "approved", services=web_test_services(lambda: mock_web_transport(repo_paths)))
    assert result["ok"], result
    evidence = CapturedEvidence()
    evidence.add("capture", "command_observation", {"source_state_id": None})
    reference = result["data"]["result_manifest"]
    capture_source_state(evidence, repo_root, "capture", result_id=reference["result_id"])
    return result, evidence, repo_root


def web_document(result):
    from brainlib.citations import encode_markdown_path
    rep = result["data"]["snapshot"]["active_representation"]
    document = Path("/logical/wiki/questions/web.md")
    original = encode_markdown_path(document, Path("/logical/sources/raw") / rep["raw_path"])
    extracted = encode_markdown_path(document, Path("/logical") / rep["extracted_path"])
    anchor = rep["anchors"][0]
    return ("# Web\n\n## Current answer\nA captured fact.[^web]\n\n## Sources\n\n"
            f"[^web]: source_id: `{rep['source_id']}`; content_sha256: `{rep['content_sha256']}`; "
            f"derivation_id: `{rep['derivation_id']}`; anchor: `{anchor['kind']}:{anchor['value']}`; "
            f"[original]({original}); [extracted]({extracted}#{anchor['kind']}:{anchor['value']})\n")


def test_real_snapshot_and_resolving_web_citation(sem, real_web):
    result, evidence, _ = real_web
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    state = sem.read_source_state(evidence, "capture", result_id=parsed.reference.result_id, revision=parsed.reference.corpus_revision, execution_id="main")
    sem.validate_promoted_web_citations(web_document(result), "wiki/questions/web.md", state, result["data"]["snapshot"]["active_representation"])


@pytest.mark.parametrize("mutation", [
    lambda body: body.replace("A captured fact.[^web]", "A captured fact. `[^web]`"),
    lambda body: body.replace("A captured fact.[^web]", "A captured fact. <!-- [^web] -->"),
    lambda body: body.replace("A captured fact.[^web]", "A captured fact. \\[^web]"),
    lambda body: body.replace("A captured fact.[^web]", "A captured fact.[^missing]"),
    lambda body: body.replace("#line:1", "#line:999"),
    lambda body: body.replace("../../sources/raw/", "https://example.test/sources/raw/"),
])
def test_web_citation_requires_real_marker_and_exact_destination(sem, real_web, mutation):
    result, evidence, _ = real_web
    ref = result["data"]["result_manifest"]
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_promoted_web_citations(mutation(web_document(result)), "wiki/questions/web.md", state, result["data"]["snapshot"]["active_representation"])


@pytest.mark.parametrize("field,value", [("state", True), ("derivation", {}), ("attempt", {}), ("diagnostics", [True])])
def test_snapshot_extraction_is_deeply_parsed(sem, real_web, field, value):
    result, _, _ = real_web
    result["data"]["snapshot"]["extraction_result"][field] = value
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_manifest_response(result, observed_exit_code=0)


def test_snapshot_response_must_match_captured_source_record(sem, real_web):
    result, evidence, _ = real_web
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    state = sem.read_source_state(evidence, "capture", result_id=parsed.reference.result_id, revision=parsed.reference.corpus_revision, execution_id="main")
    sem.validate_response_effects(parsed, [], state)
    result["data"]["snapshot"]["source_version"]["first_seen_at"] = "2000-01-01T00:00:00Z"
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_response_effects(parsed, [], state)


def test_source_sample_must_belong_to_complete_stream(sem, real_sync, captured):
    evidence, value, _ = captured
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    real_sync["data"]["corpus_revision"] = value["corpus_revision"]
    real_sync["data"]["result_manifest"]["corpus_revision"] = value["corpus_revision"]
    real_sync["data"]["hashed_paths"] = ["invented.txt"]
    real_sync["data"]["hashed_path_count"] = 1
    real_sync["data"]["result_manifest"]["event_counts"]["hashed_path"] = 1
    parsed = sem.parse_manifest_response(real_sync, observed_exit_code=0)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_response_effects(parsed, [{"kind": "hashed_path", "data": {"path": "actual.txt"}}], state)


@pytest.fixture
def real_pdf(tmp_path):
    from tests.helpers_extractors import web_test_services
    workspace = tmp_path / "pdf-repo"
    shutil.copytree(ROOT / "tests/evals/fixtures/new-binary-before-question/repo", workspace)
    for name in ("AGENTS.md", "BRAIN.md", "brain", "pyproject.toml"):
        shutil.copyfile(ROOT / name, workspace / name)
    output = io.StringIO()
    code = main(["--json", "sync"], cwd=workspace, stdout=output, stderr=io.StringIO(), services=web_test_services())
    assert code == 1, output.getvalue()
    result = json.loads(output.getvalue())
    assert result["data"]["handoff_source_id_count"] == 1, result
    reference = result["data"]["result_manifest"]
    evidence = CapturedEvidence()
    evidence.add("capture", "command_observation", {"source_state_id": None})
    capture_source_state(evidence, workspace, "capture", result_id=reference["result_id"])
    consume_output = io.StringIO()
    assert main(["--json", "source", "consume-sync-result", "--result-id", reference["result_id"]], cwd=workspace,
                stdout=consume_output, stderr=io.StringIO()) == 0
    consumed = json.loads(consume_output.getvalue())
    delivery = json.loads((workspace / consumed["data"]["handoff_delivery"]["path"]).read_bytes())
    records = [json.loads(line) for line in (workspace / reference["path"]).read_bytes().splitlines()]
    effects = [{"kind": row["kind"], "data": row["data"]} for row in records[1:-1]]
    return result, evidence, effects, delivery["items"]


def test_genuine_gap_sync_and_delivery_bind_receipt_time_source(sem, real_pdf):
    result, evidence, effects, items = real_pdf
    parsed = sem.parse_manifest_response(result, observed_exit_code=1)
    state = sem.read_source_state(evidence, "capture", result_id=parsed.reference.result_id, revision=parsed.reference.corpus_revision, execution_id="main")
    sem.validate_stream_effects(effects, state)
    sem.validate_response_effects(parsed, effects, state)
    sem.validate_delivery_sources(effects, items, state)


def test_delivery_rejects_validly_identified_item_with_wrong_recipe(sem, real_pdf):
    from brainlib.extractors.handoff import handoff_id_for
    result, evidence, effects, items = real_pdf
    ref = result["data"]["result_manifest"]
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    items[0]["required_anchor_kinds"] = ["slide"]
    items[0]["handoff_id"] = handoff_id_for({key: value for key, value in items[0].items() if key != "handoff_id"})
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_delivery_sources(effects, items, state)


def test_handoff_same_revision_record_mutation_rejected(sem, real_pdf):
    result, evidence, effects, items = real_pdf
    ref = result["data"]["result_manifest"]
    value = evidence.obj("capture-source-state", "source_state")
    source = items[0]["source_id"]
    artifact = next(entry["artifact_id"] for entry in value["records"] if entry["path"].endswith(source + ".json"))
    record = evidence.obj(artifact, "source_record")
    record["diagnostics"][0]["message"] += " changed"
    evidence.add(artifact, "source_record", record)
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_delivery_sources(effects, items, state)


def test_activation_checks_retained_output_bytes(sem, captured):
    evidence, value, _ = captured
    file_entry = next(entry for entry in value["files"] if entry["path"].startswith("sources/extracted/"))
    evidence.add(file_entry["artifact_id"], "file_capture", b"tampered retained extraction\n")
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    record = next(iter(state.records.values()))
    rep = _representation_data(representation_for(record, record.active_content_sha256, record.active_derivation_id))
    with pytest.raises(sem.SemanticEvidenceError):
        sem.require_active_representation(rep, state)


def test_sorted_source_samples_are_required(sem, real_sync):
    real_sync["data"]["hashed_paths"] = ["z.txt", "a.txt"]
    real_sync["data"]["hashed_path_count"] = 2
    real_sync["data"]["result_manifest"]["event_counts"]["hashed_path"] = 2
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_manifest_response(real_sync, observed_exit_code=0)


def test_decision_sample_byte_bound(sem, real_sync):
    real_sync["data"]["decision_counts"]["retain"] = 1
    real_sync["data"]["sampled_decisions"] = [{"source_id": None, "action": "retain", "reason": "x" * 33000, "item": None}]
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_manifest_response(real_sync, observed_exit_code=0)


def test_sync_cannot_hide_captured_ledger_coverage_gap(sem, real_pdf):
    result, evidence, effects, _ = real_pdf
    ref = result["data"]["result_manifest"]
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    result["data"]["coverage_gap_count"] = ref["event_counts"]["coverage_gap"] = 0
    result["data"]["coverage_gaps"] = []
    result["data"]["status"], result["ok"], result["errors"] = "complete", True, []
    effects = [effect for effect in effects if effect["kind"] != "coverage_gap"]
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_response_effects(parsed, effects, state)


def test_snapshot_cannot_invent_process_diagnostic(sem, real_web):
    result, evidence, _ = real_web
    ref = result["data"]["result_manifest"]
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    result["data"]["snapshot"]["extraction_result"]["diagnostics"].append({"code": "invented", "message": "invented", "path": None, "details": {}})
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_response_effects(parsed, [], state)


def test_declared_anchor_must_exist_visibly_in_captured_extraction(sem, captured):
    evidence, value, _ = captured
    record_entry = value["records"][0]
    record = evidence.obj(record_entry["artifact_id"], "source_record")
    derivation = record["derivations"][record["active_derivation_id"]]
    file_entry = next(entry for entry in value["files"] if entry["path"] == derivation["output_path"])
    body = b'`<a id="line:1"></a>`\n\nFact.\n'
    derivation["output_sha256"], derivation["output_byte_size"] = hashlib.sha256(body).hexdigest(), len(body)
    evidence.add(file_entry["artifact_id"], "file_capture", body)
    evidence.add(record_entry["artifact_id"], "source_record", record)
    state = sem.read_source_state(evidence, "capture", result_id=None, revision=value["corpus_revision"], execution_id="main")
    typed = next(iter(state.records.values()))
    rep = _representation_data(representation_for(typed, typed.active_content_sha256, typed.active_derivation_id))
    with pytest.raises(sem.SemanticEvidenceError):
        sem.require_active_representation(rep, state)


@pytest.mark.parametrize("value", [".", "", True, [], {}, None, "sources/raw/../a.txt"])
def test_hashed_effect_requires_real_raw_relative_path(sem, value):
    with pytest.raises(sem.SemanticEvidenceError):
        sem.parse_sync_effect("hashed_path", {"path": value})


def test_snapshot_handoff_summary_cannot_invent_unrelated_source(sem, real_web):
    result, evidence, _ = real_web
    ref = result["data"]["result_manifest"]
    state = sem.read_source_state(evidence, "capture", result_id=ref["result_id"], revision=ref["corpus_revision"], execution_id="main")
    result["data"]["handoff_manifest"] = "sources/ledger/handoffs/invented.json"
    result["data"]["handoffs"] = [{"handoff_id": "hnd_" + "a" * 64, "kind": "extraction", "source_id": "src_" + "b" * 64, "content_sha256": "c" * 64, "reason": "invented"}]
    parsed = sem.parse_manifest_response(result, observed_exit_code=0)
    with pytest.raises(sem.SemanticEvidenceError):
        sem.validate_response_effects(parsed, [], state)
