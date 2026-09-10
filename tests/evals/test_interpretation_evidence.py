"""Canonical interpretation decisions require terminal, joined host evidence."""

import hashlib
import json

import pytest

from tests.evals import event_log_contract as contract
from tests.evals.test_event_evidence import encoded
from tests.evals.test_marker_snapshot_contract import _replace_trace, _reseal, _validate
from tests.evals.test_workflow_acceptance import build_contradictory_workflow


@pytest.fixture
def contradictory_run(tmp_path):
    flow = build_contradictory_workflow(tmp_path)
    return flow, flow.validate()


def _reject(flow, args):
    _reseal(flow.evidence, args)
    with pytest.raises(contract.EventLogContractError):
        _validate(args, flow.scenario)


def _remove_artifact(evidence, args, artifact_id):
    root = args[3].control_root / "runs" / evidence.run_id
    (root / evidence.entries[artifact_id]["relative_path"]).unlink()
    del evidence.artifacts[artifact_id]
    del evidence.entries[artifact_id]


def _terminal_inventory(flow):
    decision = flow.evidence.obj("interpretation-decision", "interpretation_decision")
    snapshot = flow.evidence.obj(decision["terminal_snapshot_id"], "workspace_snapshot")
    return {entry["path"]: entry for entry in snapshot["inventory"]}


def _replace_terminal_capture(flow, path, raw):
    """Reseal terminal capture/diff joins without changing command checkpoints."""
    evidence = flow.evidence
    capture_id = _terminal_inventory(flow)[path]["content_id"]
    evidence.add(capture_id, "file_capture", raw)
    for name, (kind, body) in list(evidence.artifacts.items()):
        if kind not in {"workspace_snapshot", "diff"}:
            continue
        value = json.loads(body)
        inventories = [value["inventory"]] if kind == "workspace_snapshot" else [value["before"], value["after"]]
        for inventory in inventories:
            for entry in inventory:
                if entry["content_id"] == capture_id:
                    entry.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        if kind == "diff":
            before = {entry["path"]: entry["sha256"] for entry in value["before"]}
            after = {entry["path"]: entry["sha256"] for entry in value["after"]}
            value["changed_paths"] = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        evidence.add(name, kind, value)


@pytest.mark.parametrize("attack", ["integrity-error", "changed-extraction"])
def test_public_decision_rejects_resealed_terminal_sources_diverging_from_apply_checkpoint(contradictory_run, attack):
    flow, args = contradictory_run
    evidence = flow.evidence
    checkpoint = evidence.obj("apply-source-state", "source_state")
    checkpoint_ids = ["apply-source-state", *(entry["artifact_id"] for field in ("records", "files") for entry in checkpoint[field])]
    unchanged_ids = checkpoint_ids + ["interpretation-decision", "interpretation-approval"]
    decision = evidence.obj("interpretation-decision", "interpretation_decision")
    unchanged_ids.append(decision["question_capture_id"])
    unchanged = {name: evidence.artifacts[name] for name in unchanged_ids}
    inventory = _terminal_inventory(flow)
    ledger_path, record = next((path, json.loads(evidence.read(entry["content_id"], "file_capture")))
                              for path, entry in inventory.items() if path.startswith("sources/ledger/src_")
                              and json.loads(evidence.read(entry["content_id"], "file_capture"))["current_raw_path"] == "alpha-2024.txt")
    identities = record["source_id"], record["active_content_sha256"], record["active_derivation_id"]
    if attack == "integrity-error":
        record["state"] = "integrity_error"
    else:
        derivation = record["derivations"][record["active_derivation_id"]]
        raw = evidence.read(inventory[derivation["output_path"]]["content_id"], "file_capture")
        assert b"limit is 10" in raw
        modified = raw.replace(b"limit is 10", b"limit is 99")
        _replace_terminal_capture(flow, derivation["output_path"], modified)
        derivation.update(output_sha256=hashlib.sha256(modified).hexdigest(), output_byte_size=len(modified))
    _replace_terminal_capture(flow, ledger_path, encoded(record))
    assert (record["source_id"], record["active_content_sha256"], record["active_derivation_id"]) == identities
    assert {name: evidence.artifacts[name] for name in unchanged_ids} == unchanged
    _reject(flow, args)


@pytest.mark.parametrize("field,value", [
    ("output_sha256", "f" * 64), ("output_byte_size", 1), ("output_mtime_ns", 1),
    ("output_path", "sources/extracted/other.md"), ("anchors", [{"kind": "line", "value": "99"}]),
    ("quality_state", "warning"), ("extractor_version", "other-version"), ("config_sha256", "f" * 64),
    ("created_at", "2026-09-05T12:00:00Z"),
    ("method_metadata", {"converter_id": "builtin.text", "converter_version": "other-version"}),
])
def test_terminal_decision_derivation_metadata_must_equal_final_apply_checkpoint(contradictory_run, field, value):
    flow, args = contradictory_run
    path, capture = next((path, entry) for path, entry in _terminal_inventory(flow).items()
                         if path.startswith("sources/ledger/src_"))
    record = json.loads(flow.evidence.read(capture["content_id"], "file_capture"))
    record["derivations"][record["active_derivation_id"]][field] = value
    _replace_terminal_capture(flow, path, encoded(record))
    _reject(flow, args)


def _resequence_trace(evidence, args, trace):
    """Keep unrelated command/native joins sound when moving a checkpoint row."""
    for sequence, entry in enumerate(trace["records"], 1):
        entry["sequence"] = sequence
        if entry["kind"] in {"command_start", "command_end"}:
            command = evidence.obj(entry["command_id"], "command_observation")
            command["start_sequence" if entry["kind"] == "command_start" else "end_sequence"] = sequence
            evidence.add(entry["command_id"], "command_observation", command)
        elif entry["kind"] == "native_record":
            snapshot = evidence.obj(entry["workspace_snapshot_id"], "workspace_snapshot")
            snapshot["trace_sequence"] = sequence
            evidence.add(entry["workspace_snapshot_id"], "workspace_snapshot", snapshot)
    _replace_trace(evidence, args, trace)


@pytest.mark.parametrize("attack", ["absent", "earlier", "later", "earlier-equal-revision", "later-equal-revision"])
def test_public_decision_requires_final_apply_checkpoint_not_a_substitute(contradictory_run, attack):
    flow, args = contradictory_run
    evidence = flow.evidence
    trace = evidence.obj("trace", "execution_trace")
    row = next(entry for entry in trace["records"] if entry.get("source_state_id") == "apply-source-state")
    if attack == "absent":
        observation = evidence.obj("apply", "command_observation")
        observation["source_state_id"] = None
        evidence.add("apply", "command_observation", observation)
        del evidence.artifacts["apply-source-state"]
        del evidence.entries["apply-source-state"]
        trace["records"].remove(row)
    elif attack in {"earlier", "later"}:
        trace["records"].remove(row)
        boundary = next(index for index, entry in enumerate(trace["records"])
                        if entry.get("command_id") == "apply"
                        and entry["kind"] == ("command_start" if attack == "earlier" else "command_end"))
        trace["records"].insert(boundary + (attack == "later"), row)
    else:
        # Same revision and retained bytes do not make a different command's
        # checkpoint the final wiki_apply command's checkpoint.
        state = evidence.obj("reconcile-source-state" if attack == "earlier-equal-revision" else "apply-source-state", "source_state")
        assert state["corpus_revision"] == flow.revision
        state["command_id"] = "reconcile" if attack == "earlier-equal-revision" else "validate"
        evidence.add("substitute-source-state", "source_state", state)
        observation = evidence.obj("apply", "command_observation")
        observation["source_state_id"] = "substitute-source-state"
        evidence.add("apply", "command_observation", observation)
        row["source_state_id"] = "substitute-source-state"
        del evidence.artifacts["apply-source-state"]
        del evidence.entries["apply-source-state"]
    _resequence_trace(evidence, args, trace)
    _reject(flow, args)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("run_id", "f" * 64), ("execution_id", "other-writer"),
    ("command_id", "reconcile"), ("capture_point", "after_return"),
    ("result_id", "sync_" + "f" * 64), ("corpus_revision", "f" * 64), ("records", []),
])
def test_public_decision_requires_parseable_final_apply_checkpoint_binding(contradictory_run, field, value):
    flow, args = contradictory_run
    state = flow.evidence.obj("apply-source-state", "source_state")
    state[field] = value
    flow.evidence.add("apply-source-state", "source_state", state)
    _reject(flow, args)


@pytest.mark.parametrize("namespace", ["sources/raw/", "sources/extracted/"])
def test_decision_compares_checkpoint_source_bytes_to_terminal_captures(contradictory_run, namespace):
    flow, args = contradictory_run
    state = flow.evidence.obj("apply-source-state", "source_state")
    captured = next(entry for entry in state["files"] if entry["path"].startswith(namespace))
    raw = flow.evidence.read(captured["artifact_id"], "file_capture")
    flow.evidence.add(captured["artifact_id"], "file_capture", raw + b"Changed after checkpoint.\n")
    _reseal(flow.evidence, args)
    with pytest.raises(contract.EventLogContractError, match="source bytes differ from final apply checkpoint"):
        _validate(args, flow.scenario)


@pytest.mark.parametrize("omission", ["all-sources", "raw", "extracted", "one-cited-representation"])
def test_public_decision_requires_complete_final_apply_source_captures(contradictory_run, omission):
    flow, args = contradictory_run
    evidence = flow.evidence
    state = evidence.obj("apply-source-state", "source_state")
    if omission == "one-cited-representation":
        identity = flow.scenario["interpretation_policy"]["claim_identities"][0]
        record_entry = next(entry for entry in state["records"]
                            if entry["path"] == f"sources/ledger/{identity['source_id']}.json")
        record = evidence.obj(record_entry["artifact_id"], "source_record")
        paths = {"sources/raw/" + record["versions"][identity["content_sha256"]]["raw_path"],
                 record["derivations"][identity["derivation_id"]]["output_path"]}
    else:
        prefix = {"all-sources": "sources/", "raw": "sources/raw/", "extracted": "sources/extracted/"}[omission]
        paths = {entry["path"] for entry in state["files"] if entry["path"].startswith(prefix)}
    removed = [entry for entry in state["files"] if entry["path"] in paths]
    assert len(removed) == (4 if omission == "all-sources" else 2)
    changed_ids = {"apply-source-state", *(entry["artifact_id"] for entry in removed)}
    unchanged = {name: artifact for name, artifact in evidence.artifacts.items() if name not in changed_ids}
    state["files"] = [entry for entry in state["files"] if entry["path"] not in paths]
    evidence.add("apply-source-state", "source_state", state)
    for entry in removed:
        _remove_artifact(evidence, args, entry["artifact_id"])
    assert {name: artifact for name, artifact in evidence.artifacts.items() if name != "apply-source-state"} == unchanged
    _reseal(evidence, args)
    with pytest.raises(contract.EventLogContractError, match="final apply checkpoint missing source capture"):
        _validate(args, flow.scenario)


@pytest.mark.parametrize("capture", ["record", "raw", "extracted"])
@pytest.mark.parametrize("attack", [
    "missing-entry", "missing-artifact", "wrong-path", "duplicate-path",
    "duplicate-artifact", "noncanonical-path", "wrong-artifact",
])
def test_public_decision_requires_exact_final_apply_capture_inventory(contradictory_run, capture, attack):
    flow, args = contradictory_run
    evidence = flow.evidence
    state = evidence.obj("apply-source-state", "source_state")
    field, prefix = {"record": ("records", "sources/ledger/"), "raw": ("files", "sources/raw/"),
                     "extracted": ("files", "sources/extracted/")}[capture]
    entry = next(entry for entry in state[field] if entry["path"].startswith(prefix))
    if attack == "missing-entry":
        state[field].remove(entry)
        _remove_artifact(evidence, args, entry["artifact_id"])
    elif attack == "missing-artifact":
        _remove_artifact(evidence, args, entry["artifact_id"])
    elif attack == "wrong-path":
        entry["path"] = prefix + "wrong"
    elif attack == "noncanonical-path":
        entry["path"] = entry["path"].replace(prefix, prefix + "./", 1)
    elif attack in {"duplicate-path", "duplicate-artifact"}:
        extra = dict(entry)
        if attack == "duplicate-path":
            extra["artifact_id"] = "duplicated-checkpoint-capture"
            kind, raw = evidence.artifacts[entry["artifact_id"]]
            evidence.add(extra["artifact_id"], kind, raw)
        else:
            extra["path"] = prefix + "extra"
        state[field].append(extra)
    else:
        other = next(item for item in state[field]
                     if item["path"].startswith(prefix) and item != entry)
        entry["artifact_id"], other["artifact_id"] = other["artifact_id"], entry["artifact_id"]
    state[field].sort(key=lambda item: item["path"])
    evidence.add("apply-source-state", "source_state", state)
    _reject(flow, args)


@pytest.mark.parametrize("namespace", ["sources/raw/", "sources/extracted/"])
@pytest.mark.parametrize("mutation", ["digest", "size"])
def test_matching_checkpoint_and_terminal_captures_require_ledger_digest_and_size(contradictory_run, namespace, mutation):
    flow, args = contradictory_run
    evidence = flow.evidence
    state = evidence.obj("apply-source-state", "source_state")
    entry = next(entry for entry in state["files"] if entry["path"].startswith(namespace))
    raw = evidence.read(entry["artifact_id"], "file_capture")
    modified = bytes([raw[0] ^ 1]) + raw[1:] if mutation == "digest" else raw + b"\n"
    evidence.add(entry["artifact_id"], "file_capture", modified)
    _replace_terminal_capture(flow, entry["path"], modified)
    _reject(flow, args)


def test_decision_selects_final_apply_checkpoint_over_earlier_equal_revision(contradictory_run):
    flow, args = contradictory_run
    evidence = flow.evidence
    earlier = evidence.obj("reconcile-source-state", "source_state")
    final = evidence.obj("apply-source-state", "source_state")
    assert earlier["corpus_revision"] == final["corpus_revision"]
    capture_id = earlier["records"][0]["artifact_id"]
    record = evidence.obj(capture_id, "source_record")
    record["derivations"][record["active_derivation_id"]]["output_mtime_ns"] += 1
    evidence.add(capture_id, "source_record", record)
    _reseal(evidence, args)
    _validate(args, flow.scenario)


def test_decision_uses_the_final_command_observation_checkpoint_id(contradictory_run):
    flow, args = contradictory_run
    evidence = flow.evidence
    evidence.add("opaque-checkpoint", "source_state", evidence.read("apply-source-state", "source_state"))
    del evidence.artifacts["apply-source-state"]
    del evidence.entries["apply-source-state"]
    observation = evidence.obj("apply", "command_observation")
    observation["source_state_id"] = "opaque-checkpoint"
    evidence.add("apply", "command_observation", observation)
    trace = evidence.obj("trace", "execution_trace")
    next(row for row in trace["records"] if row.get("source_state_id") == "apply-source-state")["source_state_id"] = "opaque-checkpoint"
    _replace_trace(evidence, args, trace)
    _validate(args, flow.scenario)


@pytest.mark.parametrize("field", [
    "schema_version", "run_id", "execution_id", "scenario_id", "scenario_sha256", "interpretation_policy_sha256",
    "approval_event_record_id", "approval_marker_id", "approval_id", "approval_event_id", "approval_decision",
    "validate_event_record_id", "validate_marker_id", "terminal_snapshot_id", "wiki_apply_event_record_id",
    "wiki_apply_command_id", "wiki_manifest_id", "question_path", "question_capture_id", "question_sha256", "question_id",
])
def test_public_decision_artifact_requires_every_exact_join(contradictory_run, field):
    flow, args = contradictory_run
    decision = flow.evidence.obj("interpretation-decision", "interpretation_decision")
    decision[field] = True if field == "schema_version" else ("f" * 64 if field.endswith("sha256") else "wrong")
    flow.evidence.add("interpretation-decision", "interpretation_decision", decision)
    _reject(flow, args)


@pytest.mark.parametrize("attack", ["missing", "extra", "decision", "preference", "approval", "one-sided", "rebound", "duplicate", "incomplete", "unsorted", "duplicate-artifact"])
def test_public_decision_artifact_requires_closed_typed_semantics(contradictory_run, attack):
    flow, args = contradictory_run
    decision = flow.evidence.obj("interpretation-decision", "interpretation_decision")
    if attack == "missing":
        del decision["interpretation"]["approval_event_id"]
    elif attack == "extra":
        decision["answer_prose"] = "Prefer 2025"
    elif attack in {"decision", "preference", "approval"}:
        field, value = {"decision": ("decision", "preferred"), "preference": ("preference_citation_id", "alpha-2"),
                        "approval": ("approval_event_id", "interpretation-withheld")}[attack]
        decision["interpretation"][field] = value
    elif attack == "one-sided":
        decision["claim_identities"].pop()
    elif attack == "rebound":
        decision["claim_identities"][0]["derivation_id"] = decision["claim_identities"][1]["derivation_id"]
    elif attack == "duplicate":
        decision["claim_identities"][1] = dict(decision["claim_identities"][0])
    elif attack == "incomplete":
        del decision["claim_identities"][0]["source_id"]
    elif attack == "unsorted":
        decision["claim_identities"].reverse()
    elif attack == "duplicate-artifact":
        flow.evidence.add("reused-decision", "interpretation_decision", decision)
    flow.evidence.add("interpretation-decision", "interpretation_decision", decision)
    _reject(flow, args)


@pytest.mark.parametrize("capture", ["old-question", "claims-stage-0", "cross-writer", "terminal-only"])
def test_decision_rejects_stale_staged_and_same_content_capture_substitutions(contradictory_run, capture):
    flow, args = contradictory_run
    evidence = flow.evidence
    decision = evidence.obj("interpretation-decision", "interpretation_decision")
    if capture in {"cross-writer", "terminal-only"}:
        evidence.add(capture, "file_capture", evidence.read(decision["question_capture_id"], "file_capture"))
    decision["question_capture_id"] = capture
    decision["question_sha256"] = evidence.entries[capture]["sha256"]
    if capture == "terminal-only":
        # Matching terminal bytes still cannot substitute the apply writer's capture ID.
        for name, entry in list(evidence.entries.items()):
            if entry["type"] not in {"workspace_snapshot", "diff"}:
                continue
            value = evidence.obj(name, entry["type"])
            inventories = [value["inventory"]] if name == decision["terminal_snapshot_id"] else ([value["after"]] if name == "final-diff" else [])
            for inventory in inventories:
                for item in inventory:
                    if item["path"] == decision["question_path"]:
                        item["content_id"] = capture
            evidence.add(name, entry["type"], value)
    evidence.add("interpretation-decision", "interpretation_decision", decision)
    _reject(flow, args)


def _replace_terminal_text(flow, transform):
    """Reseal all existing byte joins so the validator must reparse the document."""
    evidence = flow.evidence
    decision = evidence.obj("interpretation-decision", "interpretation_decision")
    original = evidence.read(decision["question_capture_id"], "file_capture")
    modified = transform(original.decode()).encode()
    digest = hashlib.sha256(modified).hexdigest()
    changed_ids = set()
    for name, (kind, raw) in list(evidence.artifacts.items()):
        if kind == "file_capture" and raw == original:
            evidence.add(name, kind, modified)
            changed_ids.add(name)
    for name, (kind, raw) in list(evidence.artifacts.items()):
        if kind not in {"workspace_snapshot", "diff"}:
            continue
        value = json.loads(raw)
        for inventory in ([value["inventory"]] if kind == "workspace_snapshot" else [value["before"], value["after"]]):
            for item in inventory:
                if item["content_id"] in changed_ids:
                    item.update(sha256=digest, bytes=len(modified))
        evidence.add(name, kind, value)
    capture = evidence.obj("apply-manifest", "wiki_manifest")
    manifest = json.loads(evidence.read(capture["content_id"], "file_capture"))
    for change in manifest["changes"]:
        if change["path"] == decision["question_path"]:
            change["sha256"] = digest
    evidence.add(capture["content_id"], "file_capture", manifest)
    capture["sha256"] = evidence.entries[capture["content_id"]]["sha256"]
    evidence.add("apply-manifest", "wiki_manifest", capture)
    decision["question_sha256"] = digest
    evidence.add("interpretation-decision", "interpretation_decision", decision)


@pytest.mark.parametrize("attack", ["v1", "question-id", "missing-state", "untyped", "companion", "preferred-missing", "routine-preferred", "one-sided", "unresolving", "unused", "wrong-link"])
def test_terminal_question_is_reparsed_after_all_byte_joins_are_resealed(contradictory_run, attack):
    flow, args = contradictory_run
    def alter(body):
        if attack == "v1":
            return body.replace("schema_version: 2", "schema_version: 1")
        if attack == "question-id":
            return body.replace("id: question-what-is-alpha", "id: question-other")
        if attack == "missing-state":
            return body.replace("interpretation_decision: unresolved\n", "")
        if attack == "untyped":
            return body.replace("interpretation_decision: unresolved", "interpretation_decision: true")
        if attack == "companion":
            return body.replace("interpretation_decision: unresolved", "interpretation_decision: unresolved\ninterpretation_approval_event_id: approval")
        if attack in {"preferred-missing", "routine-preferred"}:
            body = body.replace("answer_status: conflicted", "answer_status: answered").replace("interpretation_decision: unresolved", "interpretation_decision: preferred")
            if attack == "routine-preferred":
                body = body.replace("interpretation_decision: preferred", "interpretation_decision: preferred\ninterpretation_preference_citation_id: alpha-2\ninterpretation_approval_event_id: interpretation-withheld")
            return body
        if attack == "one-sided":
            return body.replace("2025 Alpha limit is 12.[^alpha-2]", "2025 Alpha limit is 12.")
        if attack == "unused":
            return body.replace("2025 Alpha limit is 12.[^alpha-2]", "2025 Alpha limit is 12. `[^alpha-2]`")
        if attack == "wrong-link":
            return body.replace("../../sources/raw/alpha-2025.txt", "../../sources/raw/alpha-2024.txt")
        return body.replace("anchor: `line:1`", "anchor: `line:999`")
    _replace_terminal_text(flow, alter)
    _reject(flow, args)


def test_preference_prose_cannot_prove_typed_preferred_state(tmp_path):
    flow = build_contradictory_workflow(tmp_path, answer_suffix="Prefer 2025.[^alpha-2]\n")
    args = flow.validate()
    decision = flow.evidence.obj("interpretation-decision", "interpretation_decision")
    assert decision["interpretation"] == {"decision": "unresolved", "preference_citation_id": None, "approval_event_id": None}
    decision["interpretation"] = {"decision": "preferred", "preference_citation_id": "alpha-2", "approval_event_id": "interpretation-withheld"}
    flow.evidence.add("interpretation-decision", "interpretation_decision", decision)
    _reject(flow, args)


def test_generic_interpretation_approval_cannot_prove_a_public_pass(tmp_path):
    flow = build_contradictory_workflow(tmp_path)
    args = flow.validate()
    for artifact_id, entry in list(flow.evidence.entries.items()):
        if entry["type"] == "interpretation_decision":
            del flow.evidence.entries[artifact_id]
            del flow.evidence.artifacts[artifact_id]
    event = next(event for event in flow.events if event["name"] == "ask_interpretation_approval")
    record = flow.evidence.obj(event["event_record_id"], "event_record")
    record.pop("interpretation_decision_id", None)
    flow.evidence.add(event["event_record_id"], "event_record", record)
    _reseal(flow.evidence, args)
    with pytest.raises(contract.EventLogContractError, match="interpretation"):
        _validate(args, flow.scenario)


@pytest.mark.parametrize("attack", ["missing-assertion-join", "wrong-assertion-join", "extra-approval", "denied", "manifest", "trace", "snapshot-execution"])
def test_decision_transaction_and_assertion_are_single_use(contradictory_run, attack):
    flow, args = contradictory_run
    evidence = flow.evidence
    if attack.endswith("assertion-join"):
        proof = evidence.obj("assertion-2", "assertion")
        proof.pop("interpretation_decision_id")
        if attack == "wrong-assertion-join":
            proof["interpretation_decision_id"] = "another-decision"
        evidence.add("assertion-2", "assertion", proof)
    elif attack in {"extra-approval", "denied"}:
        approval = evidence.obj("interpretation-approval", "approval")
        if attack == "extra-approval":
            evidence.add("generic-approval", "approval", approval)
        else:
            approval["decision"] = "denied"
            evidence.add("interpretation-approval", "approval", approval)
    elif attack == "manifest":
        capture = evidence.obj("apply-manifest", "wiki_manifest")
        manifest = json.loads(evidence.read(capture["content_id"], "file_capture"))
        manifest.update(change_intent="resolve_contradiction", approval_event_id="interpretation-withheld")
        evidence.add(capture["content_id"], "file_capture", manifest)
        capture["sha256"] = evidence.entries[capture["content_id"]]["sha256"]
        evidence.add("apply-manifest", "wiki_manifest", capture)
    elif attack == "snapshot-execution":
        decision = evidence.obj("interpretation-decision", "interpretation_decision")
        snapshot = evidence.obj(decision["terminal_snapshot_id"], "workspace_snapshot")
        snapshot["execution_id"] = "other-writer"
        evidence.add(decision["terminal_snapshot_id"], "workspace_snapshot", snapshot)
    else:
        # Put the approval's native row after the question-write row in the host trace.
        approval, write = [next(event for event in flow.events if event["name"] == name)
                           for name in ("ask_interpretation_approval", "write_wiki_question")]
        trace = evidence.obj("trace", "execution_trace")
        first, second = [evidence.obj(evidence.marker_snapshot_ids[event["transcript_marker_id"]], "workspace_snapshot")["trace_sequence"] - 1
                         for event in (approval, write)]
        trace["records"][first], trace["records"][second] = trace["records"][second], trace["records"][first]
        evidence.add("trace", "execution_trace", trace)
        process = evidence.obj("process", "process")
        process["trace_sha256"] = evidence.entries["trace"]["sha256"]
        evidence.add("process", "process", process)
    _reject(flow, args)


def test_interpretation_artifact_is_forbidden_on_another_public_scenario(tmp_path):
    from tests.evals.test_marker_snapshot_contract import _minimal_pass

    evidence, scenario, args = _minimal_pass(tmp_path)
    _validate(args, scenario)
    evidence.add("inapplicable-decision", "interpretation_decision", {"schema_version": 1})
    _reseal(evidence, args)
    with pytest.raises(contract.EventLogContractError, match="interpretation_decision artifact cardinality"):
        _validate(args, scenario)


def test_preferred_assertion_requires_the_parsed_decision_even_with_preference_prose(tmp_path):
    from brainlib.wiki_models import parse_question

    flow = build_contradictory_workflow(tmp_path, answer_suffix="Prefer 2025.[^alpha-2]\n")
    question = flow.workspace / "wiki/questions/what-is-alpha.md"
    parsed = parse_question(question, text=question.read_text())
    with pytest.raises(contract.EventLogContractError, match="policy decision"):
        contract._require_interpretation_policy_state(
            parsed, {"expected_decision": "preferred", "expected_approval_decision": "approved"},
            {"decision": "approved", "event_id": "approved"},
            {"change_intent": "resolve_contradiction", "approval_event_id": "approved"},
        )
