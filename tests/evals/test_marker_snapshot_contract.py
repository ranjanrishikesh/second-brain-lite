"""Public-validator regressions for marker-time workspace snapshots.

These tests deliberately mutate an otherwise sealed contract fixture.  They
exercise the public validator rather than the runner, so a later runner
projection cannot quietly substitute a post-marker workspace inventory.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.evals import event_log_contract as contract
from tests.evals.generate_scenarios import SCENARIOS
from tests.evals.test_event_evidence import CapturedEvidence, add_event, encoded, seal_run
from tests.evals.test_workflow_acceptance import build_binary_workflow


def _reseal(evidence, args) -> None:
    """Write changed unit-fixture evidence and its attestation back to disk."""

    log, _, _, context = args
    root = context.control_root / "runs" / log["run_id"]
    for name, (_, raw) in evidence.artifacts.items():
        target = root / evidence.entries[name]["relative_path"]
        if not target.exists() or target.read_bytes() != raw:
            target.write_bytes(raw)
    index = encoded({"schema_version": 1, "run_id": log["run_id"], "entries": list(evidence.entries.values())})
    (root / "evidence-index.json").write_bytes(index)
    log["evidence_index_sha256"] = hashlib.sha256(index).hexdigest()
    unsigned = {key: value for key, value in log.items() if key != "log_attestation_sha256"}
    attestation = encoded({
        "schema_version": 1,
        "run_id": log["run_id"],
        "client": log["client"],
        "scenario_id": log["scenario_id"],
        "fixture_sha256": log["fixture_sha256"],
        "evidence_index_sha256": log["evidence_index_sha256"],
        "log_sha256": contract._canon(unsigned),
    })
    (root / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()


def _minimal_pass(tmp_path):
    """A real sealed public-pass fixture with two marker-diff events."""

    evidence = CapturedEvidence()
    scenario = next(item for item in SCENARIOS if item["id"] == "repository-development-not-archived")
    evidence.add("diff", "diff", {
        "run_id": evidence.run_id,
        "execution_id": "main",
        "before": [],
        "after": [],
        "changed_paths": [],
        "staged_paths": [],
    })
    events = []
    for name in scenario["required_events"]:
        add_event(evidence, events, name, "marker_diff", references={"diff_id": "diff"})
    return evidence, scenario, seal_run(tmp_path, evidence, scenario, events)


def _validate(args, scenario) -> None:
    log, schema, fixture_sha256, context = args
    contract.validate_event_log(log, schema, scenario, fixture_sha256, context)


def _replace_trace(evidence, args, trace) -> None:
    evidence.add("trace", "execution_trace", trace)
    process = evidence.obj("process", "process")
    process["trace_sha256"] = evidence.entries["trace"]["sha256"]
    evidence.add("process", "process", process)
    _reseal(evidence, args)


def _share_first_two_marker_row(evidence, args) -> None:
    """Turn two minimal-fixture marker rows into one ordered assistant record."""

    log = args[0]
    first_event, second_event = log["events"]
    first_marker = evidence.obj(first_event["transcript_marker_id"], "marker")
    second_marker = evidence.obj(second_event["transcript_marker_id"], "marker")
    trace = evidence.obj("trace", "execution_trace")
    transcript = evidence.read("transcript", "transcript")
    first_start, second_start = first_marker["native_record_start"], second_marker["native_record_start"]
    replacements = {}
    for start, marker in ((first_start, first_marker), (second_start, second_marker)):
        entry = next(item for item in trace["records"] if item.get("kind") == "native_record" and item["byte_start"] == start)
        replacements[start] = json.loads(transcript[entry["byte_start"]:entry["byte_end"]])
    first_text = replacements[first_start]["message"]["content"][0]["text"]
    second_text = replacements[second_start]["message"]["content"][0]["text"]
    replacements[first_start]["message"]["content"][0]["text"] = first_text + second_text
    replacements[second_start]["message"]["content"] = []

    rebuilt, offset = [], 0
    for entry in trace["records"]:
        if entry["kind"] != "native_record":
            continue
        old = transcript[entry["byte_start"]:entry["byte_end"]]
        raw = encoded(replacements[entry["byte_start"]]) + b"\n" if entry["byte_start"] in replacements else old
        entry["byte_start"], entry["byte_end"] = offset, offset + len(raw)
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
        snapshot = evidence.obj(entry["workspace_snapshot_id"], "workspace_snapshot")
        snapshot.update(
            native_record_start=entry["byte_start"],
            native_record_end=entry["byte_end"],
            native_record_sha256=entry["sha256"],
        )
        evidence.add(entry["workspace_snapshot_id"], "workspace_snapshot", snapshot)
        rebuilt.append(raw)
        offset += len(raw)
    first_native = next(item for item in trace["records"] if item.get("kind") == "native_record" and item["workspace_snapshot_id"] == evidence.marker_snapshot_ids[first_event["transcript_marker_id"]])
    first_marker.update(
        byte_start=0,
        byte_end=len(first_text.encode()),
        native_record_start=first_native["byte_start"],
        native_record_end=first_native["byte_end"],
        native_record_sha256=first_native["sha256"],
    )
    second_marker.update(
        byte_start=len(first_text.encode()),
        byte_end=len((first_text + second_text).encode()),
        native_record_start=first_native["byte_start"],
        native_record_end=first_native["byte_end"],
        native_record_sha256=first_native["sha256"],
    )
    evidence.add(first_event["transcript_marker_id"], "marker", first_marker)
    evidence.add(second_event["transcript_marker_id"], "marker", second_marker)
    first_record = evidence.obj(first_event["event_record_id"], "event_record")
    second_record = evidence.obj(second_event["event_record_id"], "event_record")
    first_diff = evidence.obj(first_record["diff_id"], "diff")
    second_diff = evidence.obj(second_record["diff_id"], "diff")
    second_diff.update(
        before_snapshot_id=first_diff["before_snapshot_id"],
        after_snapshot_id=first_diff["after_snapshot_id"],
    )
    evidence.add(second_record["diff_id"], "diff", second_diff)
    for assertion in log["repository_assertions"]:
        proof = evidence.obj(assertion["assertion_id"], "assertion")
        proof["snapshot_id"] = first_diff["after_snapshot_id"]
        evidence.add(assertion["assertion_id"], "assertion", proof)
    rebuilt_transcript = b"".join(rebuilt)
    evidence.add("transcript", "transcript", rebuilt_transcript)
    process = evidence.obj("process", "process")
    process["transcript_sha256"] = hashlib.sha256(rebuilt_transcript).hexdigest()
    evidence.add("process", "process", process)
    _replace_trace(evidence, args, trace)


def test_snapshot_bound_minimal_public_pass_is_accepted(tmp_path):
    """The fixture builder must emit a closed snapshot/trace graph."""

    _, scenario, args = _minimal_pass(tmp_path)
    _validate(args, scenario)


def test_public_pass_rejects_native_trace_row_without_workspace_snapshot_binding(tmp_path):
    """Deleting a native row's snapshot reference must invalidate a pass."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    trace = evidence.obj("trace", "execution_trace")
    native = next(entry for entry in trace["records"] if entry["kind"] == "native_record")
    native.pop("workspace_snapshot_id", None)
    _replace_trace(evidence, args, trace)

    with pytest.raises(contract.EventLogContractError, match="workspace snapshot"):
        _validate(args, scenario)


@pytest.mark.parametrize("attack", ["wrong_type", "reused", "wrong_tuple"])
def test_public_pass_rejects_wrong_or_reused_native_snapshot_binding(tmp_path, attack):
    """Trace rows must each name one correctly sealed native snapshot."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    trace = evidence.obj("trace", "execution_trace")
    native = [entry for entry in trace["records"] if entry["kind"] == "native_record"]
    if attack == "wrong_type":
        native[0]["workspace_snapshot_id"] = "diff"
    elif attack == "reused":
        native[1]["workspace_snapshot_id"] = native[0]["workspace_snapshot_id"]
    else:
        snapshot_id = native[0]["workspace_snapshot_id"]
        snapshot = evidence.obj(snapshot_id, "workspace_snapshot")
        snapshot["native_record_end"] += 1
        evidence.add(snapshot_id, "workspace_snapshot", snapshot)
    _replace_trace(evidence, args, trace)

    with pytest.raises(contract.EventLogContractError, match="workspace snapshot"):
        _validate(args, scenario)


def test_public_pass_rejects_an_unreferenced_native_snapshot_artifact(tmp_path):
    """The evidence index cannot hide a detached marker-capable snapshot."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    trace = evidence.obj("trace", "execution_trace")
    snapshot_id = next(entry["workspace_snapshot_id"] for entry in trace["records"] if entry["kind"] == "native_record")
    evidence.add("detached-native-snapshot", "workspace_snapshot", evidence.obj(snapshot_id, "workspace_snapshot"))
    _reseal(evidence, args)

    with pytest.raises(contract.EventLogContractError, match="unreferenced native workspace snapshot"):
        _validate(args, scenario)


def test_two_ordered_markers_in_one_native_row_share_its_snapshot_legitimately(tmp_path):
    """Snapshot uniqueness is per native row, not per semantic event marker."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    _share_first_two_marker_row(evidence, args)
    _validate(args, scenario)


def test_public_pass_rejects_marker_diff_relabelled_to_a_later_native_snapshot(tmp_path):
    """A valid later inventory cannot masquerade as an earlier marker instant."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    log = args[0]
    first, later = log["events"]
    record = evidence.obj(first["event_record_id"], "event_record")
    diff = evidence.obj(record["diff_id"], "diff")
    diff["after_snapshot_id"] = evidence.marker_snapshot_ids[later["transcript_marker_id"]]
    evidence.add(record["diff_id"], "diff", diff)
    _reseal(evidence, args)

    with pytest.raises(contract.EventLogContractError, match="marker diff workspace snapshot"):
        _validate(args, scenario)


def test_public_pass_rejects_phase_initial_snapshot_as_a_marker_diff_endpoint(tmp_path):
    """The phase baseline is a before-only boundary, never marker evidence."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    event = args[0]["events"][0]
    record = evidence.obj(event["event_record_id"], "event_record")
    diff = evidence.obj(record["diff_id"], "diff")
    diff["after_snapshot_id"] = evidence.initial_snapshot_ids["main"]
    evidence.add(record["diff_id"], "diff", diff)
    _reseal(evidence, args)

    with pytest.raises(contract.EventLogContractError, match="workspace snapshot|marker diff"):
        _validate(args, scenario)


def test_public_pass_rejects_packet_without_its_marker_snapshot(tmp_path):
    """A packet must retain the workspace instant that supplied its evidence."""

    workflow = build_binary_workflow(tmp_path)
    args = workflow.validate()
    log = args[0]
    event = next(item for item in log["events"] if item["name"] == "revalidate_underlying_citations")
    packet_id = workflow.evidence.obj(event["event_record_id"], "event_record")["packet_id"]
    packet = workflow.evidence.obj(packet_id, "evidence_packet")
    packet.pop("snapshot_id", None)
    workflow.evidence.add(packet_id, "evidence_packet", packet)
    _reseal(workflow.evidence, args)

    with pytest.raises(contract.EventLogContractError, match="evidence packet workspace snapshot"):
        _validate(args, workflow.scenario)


def test_public_pass_rejects_packet_relabelled_to_a_different_marker_snapshot(tmp_path):
    """A packet cannot borrow a valid snapshot from another marker row."""

    workflow = build_binary_workflow(tmp_path)
    args = workflow.validate()
    log = args[0]
    event = next(item for item in log["events"] if item["name"] == "revalidate_underlying_citations")
    other = next(item for item in log["events"] if item["name"] == "judge_insufficient")
    packet_id = workflow.evidence.obj(event["event_record_id"], "event_record")["packet_id"]
    packet = workflow.evidence.obj(packet_id, "evidence_packet")
    packet["snapshot_id"] = workflow.evidence.marker_snapshot_ids[other["transcript_marker_id"]]
    workflow.evidence.add(packet_id, "evidence_packet", packet)
    _reseal(workflow.evidence, args)

    with pytest.raises(contract.EventLogContractError, match="evidence packet workspace snapshot"):
        _validate(args, workflow.scenario)


def test_public_pass_rejects_a_self_consistent_diff_with_substituted_copied_bytes(tmp_path):
    """Diff arithmetic alone cannot replace the marker snapshot inventory."""

    workflow = build_binary_workflow(tmp_path)
    args = workflow.validate()
    event = next(item for item in args[0]["events"] if item["name"] == "stage_handoff_scoped_extraction")
    diff_id = workflow.evidence.obj(event["event_record_id"], "event_record")["diff_id"]
    diff = workflow.evidence.obj(diff_id, "diff")
    original = diff["after"][0]
    replacement_id = next(
        artifact_id for artifact_id, entry in workflow.evidence.entries.items()
        if entry["type"] == "file_capture" and workflow.evidence.read(artifact_id, "file_capture")
        != workflow.evidence.read(original["content_id"], "file_capture")
    )
    replacement = workflow.evidence.read(replacement_id, "file_capture")
    diff["after"][0] = {
        "path": original["path"],
        "sha256": hashlib.sha256(replacement).hexdigest(),
        "bytes": len(replacement),
        "content_id": replacement_id,
    }
    workflow.evidence.add(diff_id, "diff", diff)
    _reseal(workflow.evidence, args)

    with pytest.raises(contract.EventLogContractError, match="diff workspace snapshot content binding"):
        _validate(args, workflow.scenario)


def test_public_pass_rejects_packet_citation_capture_from_a_later_state(tmp_path):
    """A valid capture is useless unless it is the packet snapshot's document."""

    workflow = build_binary_workflow(tmp_path)
    args = workflow.validate()
    event = next(item for item in args[0]["events"] if item["name"] == "revalidate_underlying_citations")
    packet_id = workflow.evidence.obj(event["event_record_id"], "event_record")["packet_id"]
    packet = workflow.evidence.obj(packet_id, "evidence_packet")
    original_id = packet["citations"][0]["document_id"]
    replacement_id = next(
        artifact_id for artifact_id, entry in workflow.evidence.entries.items()
        if entry["type"] == "file_capture" and artifact_id != original_id
    )
    packet["citations"][0]["document_id"] = replacement_id
    workflow.evidence.add(packet_id, "evidence_packet", packet)
    _reseal(workflow.evidence, args)

    with pytest.raises(contract.EventLogContractError, match="evidence packet workspace citation binding"):
        _validate(args, workflow.scenario)


def test_public_pass_rejects_packet_ledger_bytes_not_in_its_snapshot(tmp_path):
    """Ledger records are revalidated against the marker-time ledger capture."""

    workflow = build_binary_workflow(tmp_path)
    args = workflow.validate()
    event = next(item for item in args[0]["events"] if item["name"] == "revalidate_underlying_citations")
    packet_id = workflow.evidence.obj(event["event_record_id"], "event_record")["packet_id"]
    packet = workflow.evidence.obj(packet_id, "evidence_packet")
    ledger_id = packet["ledger_ids"][0]
    ledger_raw = workflow.evidence.read(ledger_id, "source_record")
    source_id = workflow.evidence.obj(ledger_id, "source_record")["source_id"]
    replacement_id = next(
        artifact_id for artifact_id, entry in workflow.evidence.entries.items()
        if entry["type"] == "file_capture" and workflow.evidence.read(artifact_id, "file_capture") != ledger_raw
    )
    replacement = workflow.evidence.read(replacement_id, "file_capture")
    snapshot = workflow.evidence.obj(packet["snapshot_id"], "workspace_snapshot")
    ledger_entry = next(item for item in snapshot["inventory"] if item["path"] == f"sources/ledger/{source_id}.json")
    ledger_entry.update(content_id=replacement_id, sha256=hashlib.sha256(replacement).hexdigest(), bytes=len(replacement))
    workflow.evidence.add(packet["snapshot_id"], "workspace_snapshot", snapshot)
    _reseal(workflow.evidence, args)

    with pytest.raises(contract.EventLogContractError, match="evidence packet workspace ledger binding"):
        _validate(args, workflow.scenario)


def test_public_pass_rejects_assertion_anchored_to_a_nonterminal_marker(tmp_path):
    """Assertions are tied to the closed semantic terminal, never 'latest'."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    log = args[0]
    nonterminal = log["events"][0]
    assertion = evidence.obj(log["repository_assertions"][0]["assertion_id"], "assertion")
    assertion.update(
        marker_id=nonterminal["transcript_marker_id"],
        snapshot_id=evidence.marker_snapshot_ids[nonterminal["transcript_marker_id"]],
    )
    evidence.add(log["repository_assertions"][0]["assertion_id"], "assertion", assertion)
    _reseal(evidence, args)

    with pytest.raises(contract.EventLogContractError, match="assertion/diff proof"):
        _validate(args, scenario)


def test_public_pass_rejects_assertion_with_a_diff_from_another_marker(tmp_path):
    """An assertion's terminal snapshot must be the selected diff's endpoint."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    log = args[0]
    first = log["events"][0]
    assertion_index = log["repository_assertions"][0]
    proof = evidence.obj(assertion_index["assertion_id"], "assertion")
    first_diff_id = evidence.obj(first["event_record_id"], "event_record")["diff_id"]
    assertion_index["diff_id"] = first_diff_id
    proof["diff_id"] = first_diff_id
    evidence.add(assertion_index["assertion_id"], "assertion", proof)
    _reseal(evidence, args)

    with pytest.raises(contract.EventLogContractError, match="assertion/diff proof"):
        _validate(args, scenario)
