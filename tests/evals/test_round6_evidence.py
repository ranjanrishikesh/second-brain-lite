"""Round-five adversarial reproductions, using the real current-wiki baseline."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from tests.evals import event_log_contract as contract
from tests.evals import test_event_evidence as fixtures


@pytest.fixture
def receipt_case(tmp_path, request):
    return fixtures.receipt_case.__wrapped__(tmp_path, request)


@pytest.fixture
def current_run(tmp_path, monkeypatch):
    captured = {}
    original = fixtures.seal_run

    def seal(root, evidence, scenario, events, receipts=(), deliveries=()):
        args = original(root, evidence, scenario, events, receipts, deliveries)
        captured.update(evidence=evidence, scenario=scenario, args=args)
        return args

    monkeypatch.setattr(fixtures, "seal_run", seal)
    fixtures.test_complete_current_wiki_scenario_uses_real_cli_receipts_search_and_publish(tmp_path)
    captured["workspace"] = tmp_path / "fixtures/current-wiki-fast-path/repo"
    return captured


def reseal(run):
    evidence = run["evidence"]
    log, _, _, context = run["args"]
    root = context.control_root / "runs" / log["run_id"]
    for name, (_, raw) in evidence.artifacts.items():
        target = root / evidence.entries[name]["relative_path"]
        if not target.exists() or target.read_bytes() != raw:
            target.write_bytes(raw)
    index = fixtures.encoded({"schema_version": 1, "run_id": log["run_id"], "entries": list(evidence.entries.values())})
    (root / "evidence-index.json").write_bytes(index)
    log["evidence_index_sha256"] = hashlib.sha256(index).hexdigest()
    unsigned = {key: value for key, value in log.items() if key != "log_attestation_sha256"}
    attestation = fixtures.encoded({"schema_version": 1, "run_id": log["run_id"], "client": log["client"],
                                  "scenario_id": log["scenario_id"], "fixture_sha256": log["fixture_sha256"],
                                  "evidence_index_sha256": log["evidence_index_sha256"], "log_sha256": contract._canon(unsigned)})
    (root / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()


def validate(run):
    log, schema, fixture_hash, context = run["args"]
    contract.validate_event_log(log, schema, run["scenario"], fixture_hash, context)


def replace_command(run, cid, result):
    evidence = run["evidence"]
    observation = evidence.obj(cid, "command_observation")
    evidence.command(cid, observation["argv"], result)
    for event in run["args"][0]["events"]:
        record = evidence.obj(event["event_record_id"], "event_record")
        if record.get("command_id") == cid:
            record["result_sha256"] = contract._canon(result)
            evidence.add(event["event_record_id"], "event_record", record)


def test_extra_real_sync_with_pending_receipt_cannot_pass(current_run):
    output, error = io.StringIO(), io.StringIO()
    assert fixtures.main(["--json", "sync"], cwd=current_run["workspace"], stdout=output, stderr=error) == 0
    assert (current_run["workspace"] / ".brain/sync-results/pending.json").exists()
    current_run["evidence"].command("extra-sync", ["./brain", "--json", "sync"], json.loads(output.getvalue()))
    trace_extra_command(current_run, "extra-sync")
    with pytest.raises(contract.EventLogContractError, match="unaccounted manifest-producing"):
        validate(current_run)


@pytest.mark.parametrize("cid", ["apply", "reconcile", "consume", "ack"])
def test_unreferenced_successful_mutation_cannot_pass(current_run, cid):
    evidence = current_run["evidence"]
    observation = evidence.obj(cid, "command_observation")
    evidence.command("extra-" + cid, observation["argv"], evidence.obj(observation["result_id"], "command_result"))
    trace_extra_command(current_run, "extra-" + cid)
    with pytest.raises(contract.EventLogContractError, match="unreferenced product command"):
        validate(current_run)


def test_indexed_source_state_without_owning_observation_cannot_pass(current_run):
    evidence = current_run["evidence"]
    state_id = evidence.obj("sync", "command_observation")["source_state_id"]
    evidence.add("orphan-state", "source_state", evidence.obj(state_id, "source_state"))
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="unaccounted source state"):
        validate(current_run)


def make_claude(run, *, mcp=b'{"mcpServers":{}}\n', path_kind="indexed"):
    evidence = run["evidence"]
    log, _, _, context = run["args"]
    root = context.control_root / "runs" / log["run_id"]
    evidence.add("mcp", "mcp_config", mcp)
    (root / "mcp").write_bytes(mcp)
    identity = contract._file_identity((root / "mcp").stat())
    mcp_path = root / ("mcp" if path_kind == "indexed" else "does-not-exist.json")
    if path_kind == "alternate":
        mcp_path.write_bytes(mcp)
    policy = evidence.obj("policy", "policy")
    executable_path = policy["argv"][0]
    phase_prompt = policy["argv"][-1]
    argv = [executable_path, "--print", "--output-format", "stream-json", "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp_path), "--no-chrome", "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)", "--verbose", phase_prompt]
    policy.update(client="claude", profile="claude-restricted-tool-surface-v1", network_attestation="mock-only", argv=argv,
                  argv_sha256=contract._canon(argv), mcp_config_id="mcp", mcp_config_sha256=hashlib.sha256(mcp).hexdigest(), mcp_path=str(mcp_path), mcp_identity=identity)
    evidence.add("policy", "policy", policy)
    process = evidence.obj("process", "process")
    process.update(client="claude", argv=argv, argv_sha256=contract._canon(argv), mcp_identity=identity)
    evidence.add("process", "process", process)
    manifest = json.loads((root / "run-manifest.json").read_bytes())
    manifest.update(client="claude", policy_profiles=[policy["profile"]])
    (root / "run-manifest.json").write_bytes(fixtures.encoded(manifest))
    log["client"] = "claude"
    reseal(run)


@pytest.mark.parametrize("mcp,path_kind", [
    (b'{"mcpServers":{"unapproved":{"command":"arbitrary-executable","args":["--network"]}}}', "indexed"),
    (b'{"mcpServers":{}}\n', "missing"),
    (b'{"mcpServers":{}}\n', "alternate"),
    (b'{"mcpServers":{}}', "indexed"),
])
def test_claude_requires_exact_empty_mcp_at_indexed_path(current_run, mcp, path_kind):
    make_claude(current_run, mcp=mcp, path_kind=path_kind)
    with pytest.raises(contract.EventLogContractError, match="MCP"):
        validate(current_run)


def test_exact_claude_empty_mcp_public_pass(current_run):
    make_claude(current_run)
    validate(current_run)


@pytest.mark.parametrize("attack", ["reverse", "shared", "substring"])
def test_native_transcript_spans_are_exact_unique_and_ordered(current_run, attack):
    evidence, log = current_run["evidence"], current_run["args"][0]
    events = list(reversed(log["events"]))
    trace = evidence.obj("trace", "execution_trace")
    native_count = sum(entry["kind"] == "native_record" for entry in trace["records"])
    transcript = fixtures.native_init(current_run["workspace"])
    native_spans = [{"byte_start": 0, "byte_end": len(transcript), "sha256": hashlib.sha256(transcript).hexdigest()}]
    shared_text = "".join("EVENT:" + event["name"] + "\n" for event in events)
    for index, event in enumerate(events):
        marker = evidence.obj(event["transcript_marker_id"], "marker")
        text = ("EVENT:" + event["name"] + "\n").encode()
        if attack == "substring":
            text = b"Not evidence: " + text
        if attack == "shared":
            text = shared_text.encode()
        native = fixtures.encoded({"type": "assistant", "message": {"model": "contract-test", "content": [{"type": "text", "text": text.decode()}]}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": f"reversed-{index}"}) + b"\n"
        span = {"byte_start": len(transcript), "byte_end": len(transcript) + len(native), "sha256": hashlib.sha256(native).hexdigest()}
        marker.update(byte_start=0, byte_end=len(text), marker=text.decode(), excerpt_sha256=hashlib.sha256(text).hexdigest(), native_record_start=span["byte_start"], native_record_end=span["byte_end"], native_record_sha256=span["sha256"])
        if attack == "shared" and index:
            first = evidence.obj(events[0]["transcript_marker_id"], "marker")
            marker.update(native_record_start=first["native_record_start"], native_record_end=first["native_record_end"], native_record_sha256=first["native_record_sha256"])
        evidence.add(event["transcript_marker_id"], "marker", marker)
        transcript += native
        native_spans.append(span)
    # The valid fixture now inserts unmarked baseline native rows before
    # marker-time captures.  Preserve a complete, parseable trace while this
    # test mutates only the marker ordering/provenance it is meant to attack.
    for index in range(native_count - len(native_spans) - 1):
        native = fixtures.encoded({"type": "assistant", "message": {"model": "contract-test", "content": []},
                                   "parent_tool_use_id": None, "session_id": "contract-test",
                                   "uuid": f"unmarked-{index}"}) + b"\n"
        native_spans.append({"byte_start": len(transcript), "byte_end": len(transcript) + len(native),
                             "sha256": hashlib.sha256(native).hexdigest()})
        transcript += native
    completion = fixtures.native_result()
    native_spans.append({"byte_start": len(transcript), "byte_end": len(transcript) + len(completion), "sha256": hashlib.sha256(completion).hexdigest()})
    transcript += completion
    evidence.add("transcript", "transcript", transcript)
    spans = iter(native_spans)
    for entry in trace["records"]:
        if entry["kind"] == "native_record":
            span = next(spans)
            entry.update(span)
            snapshot = evidence.obj(entry["workspace_snapshot_id"], "workspace_snapshot")
            snapshot.update(trace_sequence=entry["sequence"], transcript_id="transcript",
                            native_record_start=span["byte_start"], native_record_end=span["byte_end"],
                            native_record_sha256=span["sha256"])
            evidence.add(entry["workspace_snapshot_id"], "workspace_snapshot", snapshot)
    evidence.add("trace", "execution_trace", trace)
    process = evidence.obj("process", "process")
    process["transcript_sha256"] = hashlib.sha256(transcript).hexdigest()
    process["trace_sha256"] = evidence.entries["trace"]["sha256"]
    evidence.add("process", "process", process)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="marker|transcript|ordering"):
        validate(current_run)


def rewrite_trace(run, trace):
    evidence = run["evidence"]
    for index, entry in enumerate(trace["records"], 1):
        entry["sequence"] = index
        if entry["kind"] in {"command_start", "command_end"}:
            observation = evidence.obj(entry["command_id"], "command_observation")
            observation["start_sequence" if entry["kind"] == "command_start" else "end_sequence"] = index
            evidence.add(entry["command_id"], "command_observation", observation)
        elif entry["kind"] == "native_record":
            snapshot = evidence.obj(entry["workspace_snapshot_id"], "workspace_snapshot")
            snapshot["trace_sequence"] = index
            evidence.add(entry["workspace_snapshot_id"], "workspace_snapshot", snapshot)
    evidence.add("trace", "execution_trace", trace)
    process = evidence.obj("process", "process")
    process["trace_sha256"] = evidence.entries["trace"]["sha256"]
    evidence.add("process", "process", process)
    reseal(run)


def trace_extra_command(run, cid):
    """Keep the trace complete so the global audit, not a missing row, rejects."""
    trace = run["evidence"].obj("trace", "execution_trace")
    trace["records"][-1:-1] = [{"kind": kind, "command_id": cid} for kind in ("command_start", "command_end")]
    rewrite_trace(run, trace)


@pytest.mark.parametrize("attack", ["receipt_reverse", "marker_before_end", "missing_end", "duplicate_pair", "missing_state", "state_after_end"])
def test_host_trace_requires_actual_completed_command_and_state_order(current_run, attack):
    trace = current_run["evidence"].obj("trace", "execution_trace")
    entries = trace["records"]
    if attack == "receipt_reverse":
        for entry in entries:
            if entry.get("command_id") in {"sync", "consume"}:
                entry["command_id"] = "consume" if entry["command_id"] == "sync" else "sync"
                if "source_state_id" in entry:
                    entry["source_state_id"] = entry["command_id"] + "-source-state"
    elif attack == "marker_before_end":
        end = next(index for index, entry in enumerate(entries) if entry["kind"] == "command_end")
        marker = next(index for index, entry in enumerate(entries) if entry["kind"] == "native_record")
        entries[end], entries[marker] = entries[marker], entries[end]
    elif attack == "missing_end":
        entries.pop(next(index for index, entry in enumerate(entries) if entry["kind"] == "command_end"))
    elif attack == "duplicate_pair":
        entries[:0] = copy.deepcopy(entries[:3])
    elif attack == "missing_state":
        entries.pop(next(index for index, entry in enumerate(entries) if entry["kind"] == "source_state"))
    else:
        state = next(index for index, entry in enumerate(entries) if entry["kind"] == "source_state")
        entries[state], entries[state + 1] = entries[state + 1], entries[state]
    rewrite_trace(current_run, trace)
    with pytest.raises(contract.EventLogContractError, match="order|trace|marker|state"):
        validate(current_run)


@pytest.mark.parametrize("attack", ["unindexed", "duplicate_sequence", "boolean_sequence"])
def test_host_trace_cannot_hide_unindexed_command_or_change_sequence(current_run, attack):
    evidence = current_run["evidence"]
    trace = evidence.obj("trace", "execution_trace")
    if attack == "unindexed":
        trace["records"][0]["command_id"] = "unindexed-command"
    else:
        trace["records"][1]["sequence"] = 1 if attack == "duplicate_sequence" else True
    evidence.add("trace", "execution_trace", trace)
    process = evidence.obj("process", "process")
    process["trace_sha256"] = evidence.entries["trace"]["sha256"]
    evidence.add("process", "process", process)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="trace|ordering"):
        validate(current_run)


def test_control_root_ancestor_replacement_into_workspace_is_rejected(current_run, tmp_path, monkeypatch):
    log, _, _, original_context = current_run["args"]
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    root = parent / "control"
    original_context.control_root.rename(root)
    workspace = tmp_path / "pinned-workspace"
    workspace.mkdir(mode=0o700)
    context = contract.TrustedRunContext(root, workspace)
    original_open, fired = os.open, []

    def race(path, flags, *args, **kwargs):
        if not fired and (os.fspath(path) == str(root) or os.fspath(path) == "control"):
            fired.append(True)
            parent.rename(workspace / "moved-parent")
            parent.symlink_to(workspace / "moved-parent", target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(contract.os, "open", race)
    with pytest.raises(contract.EventLogContractError, match="ancestor|directory|changed|replaced"):
        control = contract._Control(context, log)
        try:
            control.read("final-diff", "diff")
        finally:
            control.close()
    assert fired


@pytest.mark.parametrize("field,value", [("corpus_revision", True), ("status", False), ("hashed_path_count", "invented"), ("decision_counts", True), ("sampled_decisions", [{}])])
def test_full_sync_result_data_is_typed(current_run, field, value):
    result = current_run["evidence"].obj("sync-result", "command_result")
    result["data"][field] = value
    replace_command(current_run, "sync", result)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError):
        validate(current_run)


@pytest.mark.parametrize("attack", ["source_id", "path", "byte_size", "decision_count", "duplicate", "media_type", "extension", "action", "action_missing", "action_integrity", "action_handoff", "reason", "mtime_ns", "sha256"])
def test_public_sync_decisions_must_match_captured_source_state(current_run, attack):
    result = current_run["evidence"].obj("sync-result", "command_result")
    decision = result["data"]["sampled_decisions"][0]
    if attack == "source_id":
        decision["source_id"] = "src_" + "f" * 64
    elif attack in {"path", "byte_size", "mtime_ns"}:
        decision["item"]["fingerprint"][attack] = "nonexistent.txt" if attack == "path" else 999
    elif attack == "decision_count":
        result["data"]["decision_counts"]["retain"] = 999
    elif attack == "duplicate":
        result["data"]["sampled_decisions"].append(copy.deepcopy(decision))
        result["data"]["decision_counts"]["retain"] = 2
    elif attack.startswith("action"):
        decision["action"] = {"action": "create", "action_missing": "mark_missing", "action_integrity": "mark_integrity_error", "action_handoff": "queue_needs_agent"}[attack]
        result["data"]["decision_counts"]["retain"] = 0
        result["data"]["decision_counts"][decision["action"]] = 1
        decision["reason"] = "alpha.txt: " + decision["action"]
    elif attack == "reason":
        decision["reason"] = "invented action rationale"
    else:
        decision["item"][attack] = {"media_type": "application/pdf", "extension": ".pdf", "sha256": "f" * 64}[attack]
    replace_command(current_run, "sync", result)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="decision"):
        validate(current_run)


@pytest.mark.parametrize("attack", ["missing", "wrong_command", "wrong_time", "boolean_version", "boolean_count", "orphan"])
def test_pending_result_capture_must_bind_origin_and_stream(current_run, attack):
    evidence = current_run["evidence"]
    state = evidence.obj("sync-source-state", "source_state")
    pending_id = state["pending_result_id"]
    pending = evidence.obj(pending_id, "pending_sync_result")
    if attack == "missing":
        state["pending_result_id"] = None
        evidence.add("sync-source-state", "source_state", state)
    elif attack == "orphan":
        evidence.add("orphan-pending", "pending_sync_result", pending)
    else:
        if attack == "wrong_command":
            pending["command"] = "init"
        elif attack == "wrong_time":
            pending["generated_at"] = "2026-09-07T23:59:59+00:00"
        elif attack == "boolean_version":
            pending["schema_version"] = True
        else:
            pending["result_data"]["hashed_path_count"] = False
        evidence.add(pending_id, "pending_sync_result", pending)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="pending"):
        validate(current_run)


@pytest.mark.parametrize("attack", ["boolean_count", "decision_count", "command", "time", "extra_data"])
def test_later_pending_capture_must_equal_validated_origin_checkpoint(current_run, attack):
    evidence = current_run["evidence"]
    pending_id = evidence.obj("consume-source-state", "source_state")["pending_result_id"]
    pending = evidence.obj(pending_id, "pending_sync_result")
    if attack == "boolean_count":
        pending["result_data"]["hashed_path_count"] = False
    elif attack == "decision_count":
        pending["result_data"]["decision_counts"]["retain"] = 999
    elif attack == "command":
        pending["command"] = "init"
    elif attack == "time":
        pending["generated_at"] = "2026-09-07T23:59:59+00:00"
    else:
        pending["result_data"]["extra"] = "not captured by the originating command"
    evidence.add(pending_id, "pending_sync_result", pending)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="pending.*checkpoint"):
        validate(current_run)


@pytest.mark.parametrize("attack", ["timestamp_z", "timestamp_space", "negative_zero", "whitespace"])
def test_all_pending_copies_require_exact_producer_serialization(tmp_path, attack):
    from tests.evals.test_workflow_acceptance import build_binary_workflow
    flow = build_binary_workflow(tmp_path)
    flow.validate()
    pending_ids = [key for key, entry in flow.evidence.entries.items() if entry["type"] == "pending_sync_result"]
    assert len(pending_ids) == 6  # Two genuine sync receipts, each with origin/consume/replay.
    for pending_id in pending_ids:
        raw = flow.evidence.read(pending_id, "pending_sync_result")
        if attack.startswith("timestamp"):
            value = json.loads(raw)
            original = value["generated_at"]
            assert original.endswith("+00:00") and "T" in original
            value["generated_at"] = original.removesuffix("+00:00") + "Z" if attack == "timestamp_z" else original.replace("T", " ", 1)
            changed = fixtures.encoded(value)
        elif attack == "negative_zero":
            changed = raw.replace(b":0,", b":-0,", 1)
            assert json.loads(changed) == json.loads(raw)
        else:
            changed = raw.replace(b"{", b"{ ", 1)
            assert json.loads(changed) == json.loads(raw)
        assert changed != raw
        flow.evidence.add(pending_id, "pending_sync_result", changed)
    with pytest.raises(contract.EventLogContractError, match="pending.*(bytes|serialization)"):
        flow.validate()


@pytest.mark.parametrize("attack", ["unrelated_reference", "different_receipt", "changed_payload", "ack_retains_pending"])
def test_replay_source_checkpoint_authority_is_owning_receipt_not_self_fields(tmp_path, attack):
    from tests.evals.test_workflow_acceptance import build_binary_workflow
    flow = build_binary_workflow(tmp_path)
    cid = "initial_sync-durable"
    state_id = cid + "-source-state"
    state = flow.evidence.obj(state_id, "source_state")
    pending_id = state["pending_result_id"]
    pending = flow.evidence.obj(pending_id, "pending_sync_result")
    if attack == "different_receipt":
        other = flow.evidence.obj("post-sync-source-state", "source_state")
        state.update({key: other[key] for key in ("result_id", "corpus_revision", "records", "files")})
        pending = flow.evidence.obj(other["pending_result_id"], "pending_sync_result")
    elif attack == "unrelated_reference":
        state["result_id"] = "sync_" + "f" * 64
        reference = pending["reference"]
        reference.update(result_id=state["result_id"], sha256="f" * 64,
                         path=".brain/sync-results/" + state["result_id"] + ".jsonl")
        pending["result_data"]["result_manifest"] = reference
    elif attack == "changed_payload":
        pending["result_data"]["decision_counts"]["retain"] = 999
    else:
        cid = "initial_sync-ack"
        state_id = cid + "-source-state"
        pending_id = cid + "-pending-result"
        state.update(command_id=cid, pending_result_id=pending_id)
        observation = flow.evidence.obj(cid, "command_observation")
        observation["source_state_id"] = state_id
        flow.evidence.add(cid, "command_observation", observation)
    flow.evidence.add(state_id, "source_state", state)
    flow.evidence.add(pending_id, "pending_sync_result", pending)
    with pytest.raises(contract.EventLogContractError, match="source-state.*binding|pending.*checkpoint"):
        flow.validate()


@pytest.mark.parametrize("role", ["consume", "durable", "ack"])
def test_optional_receipt_pending_copy_can_be_absent(tmp_path, role):
    from tests.evals.test_workflow_acceptance import build_binary_workflow
    flow = build_binary_workflow(tmp_path)
    cid = "initial_sync-" + role
    state_id = cid + "-source-state"
    if role == "ack":
        state = flow.evidence.obj("initial_sync-durable-source-state", "source_state")
        state["command_id"] = cid
        observation = flow.evidence.obj(cid, "command_observation")
        observation["source_state_id"] = state_id
        flow.evidence.add(cid, "command_observation", observation)
    else:
        state = flow.evidence.obj(state_id, "source_state")
        pending_id = state["pending_result_id"]
        del flow.evidence.artifacts[pending_id]
        del flow.evidence.entries[pending_id]
    state["pending_result_id"] = None
    flow.evidence.add(state_id, "source_state", state)
    flow.validate()


@pytest.mark.parametrize("command_id", ["static", "rendered"])
def test_snapshot_origin_cannot_establish_pending_sync_authority(tmp_path, command_id):
    from tests.evals.test_workflow_acceptance import build_web_workflow
    flow = build_web_workflow(tmp_path)
    state_id = command_id + "-source-state"
    state = flow.evidence.obj(state_id, "source_state")
    result = flow.command_results[command_id]
    if command_id == "static":
        result = result["data"]["product_result"]
    reference = result["data"]["result_manifest"]
    data = {"corpus_revision": reference["corpus_revision"], "result_manifest": reference,
            "status": "complete_with_gaps" if reference["event_counts"]["coverage_gap"] else "complete",
            "sampled_decisions": [{"invented": True}]}
    for field, kind in (("hashed_path_count", "hashed_path"), ("new_active_representation_count", "new_active_representation"),
                        ("citation_rewrite_count", "citation_rewrite"), ("handoff_source_id_count", "handoff_source_id"),
                        ("coverage_gap_count", "coverage_gap")):
        data[field] = reference["event_counts"][kind]
    state["pending_result_id"] = flow.evidence.add(command_id + "-invented-pending", "pending_sync_result", {
        "schema_version": 1, "command": "sync", "generated_at": "2026-09-07T23:59:59+00:00",
        "reference": reference, "result_data": data})
    flow.evidence.add(state_id, "source_state", state)
    with pytest.raises(contract.EventLogContractError, match="snapshot.*pending|pending.*snapshot"):
        flow.validate()


def test_compacted_pending_handoff_digest_must_match_full_public_response(tmp_path):
    from tests.evals.test_workflow_acceptance import build_binary_workflow
    flow = build_binary_workflow(tmp_path)
    pending_id = flow.evidence.obj("sync-source-state", "source_state")["pending_result_id"]
    pending = flow.evidence.obj(pending_id, "pending_sync_result")
    assert "handoffs" not in pending["result_data"]
    pending["result_data"]["handoff_response"] = {"path": ".brain/sync-results/handoffs_" + "f" * 64 + ".json", "sha256": "f" * 64}
    flow.evidence.add(pending_id, "pending_sync_result", pending)
    with pytest.raises(contract.EventLogContractError, match="pending"):
        flow.validate()


def test_generated_index_change_must_be_present_in_captured_publication_diff(current_run):
    result = current_run["evidence"].obj("apply-result", "command_result")
    result["data"]["changed_paths"] = sorted([*result["data"]["changed_paths"], "wiki/index.md"])
    replace_command(current_run, "apply", result)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="promoted paths"):
        validate(current_run)


def test_generated_index_alone_cannot_prove_question_publication():
    result = fixtures.envelope("wiki apply", {"corpus_revision": "a" * 64, "changed_paths": ["wiki/index.md"], "index_path": "wiki/index.md", "recovered": False})
    with pytest.raises(contract.EventLogContractError, match="no changes"):
        contract._event_command("wiki_apply", ["./brain", "--json", "wiki", "apply", "--manifest", ".brain/wiki-staging/run/manifest.json"], result)


def test_agent_manifest_cannot_target_generated_index(current_run):
    evidence = current_run["evidence"]
    capture = evidence.obj("apply-manifest", "wiki_manifest")
    manifest = json.loads(evidence.read(capture["content_id"], "file_capture"))
    manifest["changes"][0]["path"] = "wiki/index.md"
    raw = fixtures.encoded(manifest)
    evidence.add(capture["content_id"], "file_capture", raw)
    capture["sha256"] = hashlib.sha256(raw).hexdigest()
    evidence.add("apply-manifest", "wiki_manifest", capture)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="wiki manifest"):
        validate(current_run)


def test_sync_result_data_cannot_omit_required_fields(current_run):
    result = current_run["evidence"].obj("sync-result", "command_result")
    result["data"] = {"result_manifest": result["data"]["result_manifest"], "corpus_revision": True, "status": False, "hashed_path_count": "invented"}
    replace_command(current_run, "sync", result)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError):
        validate(current_run)


def test_web_prose_substrings_cannot_prove_resolving_citation():
    evidence = fixtures.CapturedEvidence()
    body = b"No citation is present. sources/raw/_web/ is just text. content_sha256: `" + b"0" * 64 + b"`\n"
    evidence.add("body", "file_capture", body)
    path, staging = "wiki/questions/web.md", ".brain/wiki-staging/run/files/wiki/questions/web.md"
    item = {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "content_id": "body"}
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [{**item, "path": staging}], "after": [item], "changed_paths": sorted([path, staging]), "staged_paths": []})
    evidence.add("capability", "fixture_capability", {"descriptor_id": "descriptor"})
    evidence.add("descriptor", "fixture_descriptor", {"unused_sha256": "f" * 64})
    manifest = {"changes": [{"operation": "write", "path": path, "staging_path": staging, "sha256": item["sha256"]}], "link_candidate_runs": []}
    with pytest.raises(contract.EventLogContractError, match="citation|rendered|ledger"):
        contract._applied_wiki(evidence, {"diff_id": "diff"}, manifest, {"data": {"changed_paths": [path]}}, {}, {}, {},
                               {"scenario_id": "web-approval-and-capture", "_manifest": {"fixture_capability_id": "capability"}})


@pytest.mark.parametrize("kind,data", [
    ("new_active_representation", {"source_id": True}),
    ("hashed_path", {"path": True}),
    ("hashed_path", {"path": "../escape"}),
    ("citation_rewrite", {"source_id": True, "content_sha256": "a" * 64, "raw_path": "alpha.txt"}),
    ("coverage_gap", {"code": False}),
    ("handoff_source_id", {"source_id": True, "record_sha256": "b" * 64}),
])
def test_finalized_effect_payloads_are_deeply_typed(kind, data):
    from brainlib.sync_results import SyncResultReference

    evidence = fixtures.CapturedEvidence()
    counts = {name: int(name == kind) for name in ("hashed_path", "new_active_representation", "citation_rewrite", "handoff_source_id", "coverage_gap")}
    event = fixtures.encoded({"type": "event", "sequence": 1, "kind": kind, "data": data}) + b"\n"
    raw = fixtures.encoded({"type": "header", "schema_version": 1, "command": "sync", "generated_at": "2026-09-04T12:00:00Z"}) + b"\n" + event
    raw += fixtures.encoded({"type": "trailer", "event_count": 1, "event_counts": counts, "events_sha256": hashlib.sha256(event).hexdigest(), "corpus_revision": "c" * 64}) + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    reference = SyncResultReference.from_dict({"result_id": "sync_" + digest, "path": ".brain/sync-results/sync_" + digest + ".jsonl", "sha256": digest, "corpus_revision": "c" * 64, "event_counts": counts})
    evidence.add("stream", "sync_stream", raw)
    with pytest.raises(contract.EventLogContractError):
        contract._sync_stream(evidence, "stream", reference, "sync")


def test_command_timestamps_cannot_reverse_receipt_order(current_run):
    evidence = current_run["evidence"]
    for index, cid in enumerate(("ack", "durable", "consume", "sync"), 1):
        observation = evidence.obj(cid, "command_observation")
        observation["timestamp"] = f"2026-09-04T12:00:0{index}Z"
        evidence.add(cid, "command_observation", observation)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="order|timestamp"):
        validate(current_run)


def test_claude_same_bytes_replacement_inode_is_rejected(current_run):
    make_claude(current_run)
    log, _, _, context = current_run["args"]
    path = context.control_root / "runs" / log["run_id"] / "mcp"
    path.rename(path.with_name("previous-mcp"))
    path.write_bytes(contract.EMPTY_MCP_BYTES)
    with pytest.raises(contract.EventLogContractError, match="MCP"):
        validate(current_run)


def test_index_alias_cannot_reuse_one_physical_marker(current_run):
    evidence = current_run["evidence"]
    entry = copy.deepcopy(evidence.entries["marker-1"])
    entry["id"] = "aliased-marker"
    evidence.entries[entry["id"]] = entry
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="alias"):
        validate(current_run)


def test_directory_permission_change_during_read_is_rejected(current_run, monkeypatch):
    context = current_run["args"][3]
    original_read, changed = os.read, []

    def chmod_on_read(fd, count):
        result = original_read(fd, count)
        if not changed:
            changed.append(True)
            context.control_root.chmod(0o777)
        return result

    monkeypatch.setattr(contract.os, "read", chmod_on_read)
    with pytest.raises(contract.EventLogContractError, match="ancestor|directory"):
        validate(current_run)


def test_constructor_failure_closes_every_opened_descriptor(current_run, monkeypatch):
    context = current_run["args"][3]
    original_open, original_close, held = os.open, os.close, set()

    def tracked_open(path, flags, *args, **kwargs):
        if os.fspath(path) == current_run["args"][0]["run_id"]:
            raise OSError("injected run-open failure")
        fd = original_open(path, flags, *args, **kwargs)
        held.add(fd)
        return fd

    def tracked_close(fd):
        held.discard(fd)
        return original_close(fd)

    monkeypatch.setattr(contract.os, "open", tracked_open)
    monkeypatch.setattr(contract.os, "close", tracked_close)
    with pytest.raises(contract.EventLogContractError):
        contract._Control(context, current_run["args"][0])
    assert not held


@pytest.mark.parametrize("attack", ["wrong_command", "wrong_revision", "missing_record", "duplicate_record", "wrong_file", "boolean_version"])
def test_public_receipt_state_cannot_be_substituted_or_partial(current_run, attack):
    evidence = current_run["evidence"]
    state = evidence.obj("sync-source-state", "source_state")
    if attack == "wrong_command":
        state["command_id"] = "consume"
    elif attack == "wrong_revision":
        state["corpus_revision"] = "f" * 64
    elif attack == "missing_record":
        state["records"] = []
    elif attack == "duplicate_record":
        state["records"].append(copy.deepcopy(state["records"][0]))
    elif attack == "wrong_file":
        state["files"][-1]["path"] = "sources/raw/unrelated.txt"
    else:
        state["schema_version"] = True
    evidence.add("sync-source-state", "source_state", state)
    reseal(current_run)
    with pytest.raises(contract.EventLogContractError, match="state|semantic"):
        validate(current_run)


@pytest.mark.parametrize("source_field", ["updated_at", "inspected_at"])
def test_receipt_handoff_digest_joins_full_record_not_only_revision(receipt_case, source_field):
    evidence, log = fixtures.delivery_case(receipt_case)
    for cid in ("verify", "consume"):
        state = evidence.obj(cid + "-source-state", "source_state")
        for entry in state["records"]:
            record = evidence.obj(entry["artifact_id"], "source_record")
            record[source_field] = "2026-09-07T12:00:00Z"
            evidence.add(entry["artifact_id"], "source_record", record)
    with pytest.raises(contract.EventLogContractError, match="checkpoint|digest"):
        contract._validate_receipts(evidence, log, log["events"])


def native_candidate(*records):
    return fixtures.native_init(Path("/workspace")) + b"".join(fixtures.encoded(record) + b"\n" for record in records) + fixtures.native_result()


@pytest.mark.parametrize("record", [
    {"type": "assistant", "message": {"model": "test", "content": [{"type": "text", "text": "EVENT:validate\n"}]}, "parent_tool_use_id": "subagent", "session_id": "contract-test", "uuid": "assistant"},
    {"type": "assistant", "message": {"model": "test", "content": [{"type": "text", "text": "EVENT:validate\n"}]}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "assistant", "supersedes": []},
    {"type": "assistant", "message": {"model": "test", "content": [{"type": "thinking", "thinking": "EVENT:validate\n"}]}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "assistant"},
    {"type": "tool_result", "session_id": "contract-test", "uuid": "tool", "content": "EVENT:validate\n"},
    {"type": "system", "subtype": "informational", "session_id": "contract-test", "uuid": "info", "content": "EVENT:validate\n", "level": "info", "prevent_continuation": True},
])
def test_native_adapter_rejects_unsupported_or_retracted_marker_sources(record):
    with pytest.raises(contract.EventLogContractError):
        contract._native_transcript(native_candidate(record), "claude", "2.1.251", "claude-stream-json-2.1.251-v1")


@pytest.mark.parametrize("record", [
    {"type": "user", "message": {"role": "user", "content": "EVENT:validate\n"}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "user"},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "EVENT:validate\n"}]}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "tool"},
    {"type": "system", "subtype": "informational", "session_id": "contract-test", "uuid": "info", "content": "EVENT:validate\n", "level": "info"},
    {"type": "tool_use_summary", "session_id": "contract-test", "uuid": "summary", "summary": "EVENT:validate\n", "preceding_tool_use_ids": ["tool"]},
])
def test_native_nonassistant_payload_never_yields_marker_text(record):
    parsed = contract._native_transcript(native_candidate(record), "claude", "2.1.251", "claude-stream-json-2.1.251-v1")
    assert all(not entry["texts"] for entry in parsed.values())


@pytest.mark.parametrize("field,value", [("is_error", True), ("duration_ms", False), ("subtype", "error_max_turns"), ("permission_denials", [{"tool_name": "Bash"}]), ("session_id", "other"), ("result", None), ("permission_denials", {}), ("user_message_uuid", True), ("stop_reason", "refusal")])
def test_native_completion_requires_matching_typed_success(field, value):
    result = json.loads(fixtures.native_result())
    result[field] = value
    with pytest.raises(contract.EventLogContractError):
        contract._native_transcript(fixtures.native_init(Path("/workspace")) + fixtures.encoded(result) + b"\n", "claude", "2.1.251", "claude-stream-json-2.1.251-v1")


@pytest.mark.parametrize("field,value", [("type", True), ("id", False), ("stop_reason", "refusal"), ("stop_sequence", False), ("usage", True)])
def test_native_assistant_metadata_cannot_hide_refusal_or_malformed_fields(field, value):
    record = {"type": "assistant", "message": {"model": "test", "content": [{"type": "text", "text": "EVENT:validate\n"}], field: value},
              "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "assistant"}
    with pytest.raises(contract.EventLogContractError):
        contract._native_transcript(native_candidate(record), "claude", "2.1.251", "claude-stream-json-2.1.251-v1")


def test_only_typed_informational_tail_is_allowed_after_completion():
    tail = {"type": "system", "subtype": "informational", "session_id": "contract-test", "uuid": "tail", "content": "Done.", "level": "info"}
    raw = native_candidate() + fixtures.encoded(tail) + b"\n"
    assert len(contract._native_transcript(raw, "claude", "2.1.251", "claude-stream-json-2.1.251-v1")) == 3
    with pytest.raises(contract.EventLogContractError):
        contract._native_transcript(raw + fixtures.native_result(), "claude", "2.1.251", "claude-stream-json-2.1.251-v1")


def test_codex_without_sourced_native_adapter_is_explicitly_incomplete():
    with pytest.raises(contract.EventLogContractError, match="unsupported_native_format"):
        contract._native_transcript(b"{}\n", "codex", "0.153.0", "codex-json-v1")


def test_support_hash_observed_after_publication_cannot_prove_staging(tmp_path):
    from tests.evals.test_workflow_acceptance import build_web_workflow
    flow = build_web_workflow(tmp_path)
    _, manifest = contract._wiki_manifest(flow.evidence, "apply-manifest")
    change = manifest["changes"][0]
    flow.record("late-support-hash", ["eval", "sha256", "--path", change["staging_path"]],
                fixtures.envelope("eval sha256", {"path": change["staging_path"], "sha256": change["sha256"]}))
    with pytest.raises(contract.EventLogContractError, match="support.*ordering"):
        flow.validate()


def test_native_optional_user_message_identity_must_match_when_present():
    assistant = {"type": "assistant", "message": {"model": "test", "content": [{"type": "text", "text": "EVENT:validate\n"}]},
                 "parent_tool_use_id": None, "session_id": "contract-test", "uuid": "assistant", "user_message_uuid": "prompt-id"}
    result = json.loads(fixtures.native_result())
    result["user_message_uuid"] = "prompt-id"
    raw = fixtures.native_init(Path("/workspace")) + fixtures.encoded(assistant) + b"\n"
    assert len(contract._native_transcript(raw + fixtures.encoded(result) + b"\n", "claude", "2.1.251", "claude-stream-json-2.1.251-v1")) == 3
    result["user_message_uuid"] = "different-prompt"
    with pytest.raises(contract.EventLogContractError, match="user message identity mismatch"):
        contract._native_transcript(raw + fixtures.encoded(result) + b"\n", "claude", "2.1.251", "claude-stream-json-2.1.251-v1")
