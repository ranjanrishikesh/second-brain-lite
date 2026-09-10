"""Production-shaped and adversarial tests for the closed normalizer boundary."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest

import tests.evals.event_log_contract as contract
from brainlib.cli import main
from tests.evals.generate_scenarios import SCENARIOS
from tests.evals.phase_prompt_contract import (
    PROMPT_PROTOCOL,
    canonical_phase_prompt_bytes,
    scenario_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_CLIENT_BYTES = b"#!/bin/sh\n# Task 7 executable-evidence contract fixture\nexit 0\n"
_CONTRACT_BUILD_ID = "claude-contract-fixture-v1"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class CapturedEvidence:
    """Unit-test artifact store; never passed to the public trust-root API."""

    run_id = "a" * 64

    def __init__(self):
        self.artifacts = {}
        self.entries = {}

    def add(self, name, kind, value):
        raw = value if type(value) is bytes else encoded(value)
        self.artifacts[name] = (kind, raw)
        self.entries[name] = {"id": name, "type": kind, "relative_path": name,
                              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        return name

    def read(self, name, kind):
        assert self.artifacts[name][0] == kind
        return self.artifacts[name][1]

    def obj(self, name, kind):
        return json.loads(self.read(name, kind))

    def command(self, name, argv, result, *, exit_code=None, execution_id=None):
        previous = self.obj(name, "command_observation") if name in self.entries else {}
        sequence = 1 + 2 * sum(entry["type"] == "command_observation" for entry in self.entries.values())
        self.add(name + "-result", "command_result", result)
        self.add(name, "command_observation", {
            "run_id": self.run_id, "execution_id": previous.get("execution_id", "main") if execution_id is None else execution_id, "argv": argv,
            "exit_code": previous.get("exit_code", 0) if exit_code is None else exit_code, "result_id": name + "-result",
            "result_sha256": self.entries[name + "-result"]["sha256"],
            "timestamp": "2026-09-04T12:00:00Z",
            "start_sequence": previous.get("start_sequence", sequence),
            "end_sequence": previous.get("end_sequence", sequence + 1),
            "source_state_id": previous.get("source_state_id"),
        })


def envelope(command, data):
    return {"ok": True, "command": command, "data": data, "warnings": [], "errors": []}


@pytest.fixture
def receipt_case(tmp_path, request):
    """Capture real sync/consume/replay/ack CLI bytes and durable artifacts."""
    workspace = tmp_path / "workspace"
    shutil.copytree(ROOT / "tests/evals/fixtures/repository-development-not-archived/repo", workspace)
    for name in ("AGENTS.md", "BRAIN.md", "brain", "pyproject.toml"):
        shutil.copyfile(ROOT / name, workspace / name)
    evidence = CapturedEvidence()
    evidence.workspace = workspace

    def invoke(name, args):
        output = io.StringIO()
        error = io.StringIO()
        assert main(["--json", *args], cwd=workspace, stdout=output, stderr=error) == 0, output.getvalue() + error.getvalue()
        result = json.loads(output.getvalue())
        evidence.command(name, ["./brain", "--json", *args], result)
        if args[0] in {"sync", "init"} or name == "consume":
            from tests.evals.test_semantic_evidence import capture_source_state
            result_id = result["data"]["result_manifest"]["result_id"] if args[0] in {"sync", "init"} else result["data"]["result_id"]
            capture_source_state(evidence, workspace, name, result_id=result_id)
        return result

    command = getattr(request, "param", "sync")
    operation = "init" if command == "init" else "initial_sync"
    verify = invoke("verify", [command])
    ref = verify["data"]["result_manifest"]
    consume = invoke("consume", ["source", "consume-sync-result", "--result-id", ref["result_id"]])
    invoke("durable", ["source", "consume-sync-result", "--result-id", ref["result_id"]])
    evidence.add("stream", "sync_stream", (workspace / ref["path"]).read_bytes())
    evidence.add("receipt", "consumption_receipt", (workspace / ".brain/sync-results" / f"consumed_{ref['result_id']}.json").read_bytes())
    invoke("ack", ["source", "acknowledge-sync-result", "--result-id", ref["result_id"]])
    receipt = {"id": "receipt-sync", "operation": operation, "verify_command_id": "verify",
               "consume_command_id": "consume", "durable_command_id": "durable", "acknowledge_command_id": "ack",
               "stream_artifact_id": "stream", "durable_artifact_id": "receipt", "delivery_id": None}
    events = []
    for index, (suffix, cid) in enumerate(zip(("verified", "consumed", "durable", "acknowledged"), ("verify", "consume", "durable", "ack")), 1):
        name = operation + "_receipt_" + suffix
        event = {"sequence": index, "name": name, "execution_id": "main", "evidence_mode": "product_cli",
                 "transcript_marker_id": f"marker-{index}", "event_record_id": f"event-{index}",
                 "corroboration_ids": [cid], "data": {"receipt_id": "receipt-sync"}}
        result = evidence.obj(cid + "-result", "command_result")
        argv = evidence.obj(cid, "command_observation")["argv"]
        evidence.add(event["event_record_id"], "event_record", {
            "run_id": evidence.run_id, "execution_id": "main", "event_name": name,
            "marker_id": event["transcript_marker_id"], "evidence_mode": "product_cli",
            "command_id": cid, "argv_sha256": hashlib.sha256(encoded(argv)).hexdigest(),
            "result_sha256": hashlib.sha256(encoded(result)).hexdigest(),
        })
        events.append(event)
    return evidence, {"receipts": [receipt], "deliveries": [], "events": events}, consume


def test_every_required_event_has_a_closed_rule():
    rules = getattr(contract, "EVENT_RULES", {})
    required = {name for scenario in SCENARIOS for name in scenario["required_events"]}
    assert required <= rules.keys(), sorted(required - rules.keys())
    assert {rule.mode for rule in rules.values()} == {
        "product_cli", "fixture_eval", "marker", "marker_diff", "marker_approval", "marker_delivery",
    }


def test_real_product_sync_receipt_and_idempotent_replay_are_accepted(receipt_case):
    evidence, log, _ = receipt_case
    for event in log["events"]:
        contract._validate_event_record(evidence, event, evidence.run_id)
    result = contract._validate_receipts(evidence, log, log["events"])
    assert result["receipt-sync"]["result_id"].startswith("sync_")


@pytest.mark.parametrize("receipt_case", ["init"], indirect=True)
def test_real_product_init_receipt_and_idempotent_replay_are_accepted(receipt_case):
    evidence, log, _ = receipt_case
    for event in log["events"]:
        contract._validate_event_record(evidence, event, evidence.run_id)
    assert contract._validate_receipts(evidence, log, log["events"])["receipt-sync"]["counts"]["handoff_source_id"] == 0


@pytest.mark.parametrize("receipt_case", ["init"], indirect=True)
def test_init_receipt_rejects_an_extra_valid_product_flag(receipt_case):
    evidence, log, _ = receipt_case
    evidence.command("verify", ["./brain", "--json", "init", "--max-workers", "1"], evidence.obj("verify-result", "command_result"))
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("argv", [
    ["./brain", "--json", "source", "sync"],
    ["./brain", "--json", "sync", "--max-workers", "1"],
    ["./brain", "--json", "eval", "unrelated"],
])
def test_receipt_rejects_wrong_or_extended_verify_command(receipt_case, argv):
    evidence, log, _ = receipt_case
    result = evidence.obj("verify-result", "command_result")
    evidence.command("verify", argv, result)
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("command"), lambda r: r.update(command="status"),
    lambda r: r.update(errors=[{"code": "failure"}]), lambda r: r.update(ok=1),
    lambda r: r.update(warnings=False), lambda r: r.update(extra=True),
])
def test_result_requires_complete_typed_product_envelope(receipt_case, mutation):
    evidence, _, _ = receipt_case
    result = evidence.obj("verify-result", "command_result")
    mutation(result)
    evidence.command("verify", ["./brain", "--json", "sync"], result)
    with pytest.raises(contract.EventLogContractError):
        contract._result(evidence, "verify", "main")


def test_acknowledgement_sync_command_must_match_verify(receipt_case):
    evidence, log, _ = receipt_case
    result = evidence.obj("ack-result", "command_result")
    result["data"]["sync_command"] = "init"
    argv = evidence.obj("ack", "command_observation")["argv"]
    evidence.command("ack", argv, result)
    with pytest.raises(contract.EventLogContractError, match="acknowledg"):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("field,value", [("result_id", "sync_" + "f" * 64), ("corpus_revision", "f" * 64), ("status", True), ("effect_digest", False)])
def test_consumption_all_identity_and_scalar_fields_are_checked(receipt_case, field, value):
    evidence, log, _ = receipt_case
    result = evidence.obj("consume-result", "command_result")
    result["data"][field] = value
    evidence.command("consume", evidence.obj("consume", "command_observation")["argv"], result)
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("target", ["stream", "receipt"])
def test_receipt_requires_separately_captured_immutable_stream_and_durable_bytes(receipt_case, target):
    evidence, log, _ = receipt_case
    evidence.add(target, evidence.entries[target]["type"], b"{}\n")
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("name", ["classify_repository_development", "write_wiki_question", "ask_web_approval", "branch_extraction_handoff", "source_pass_discovery", "links_check"])
def test_generic_eval_cannot_prove_any_event(name):
    evidence = CapturedEvidence()
    argv = ["./brain", "--json", "eval", "unrelated"]
    result = envelope("eval unrelated", {})
    evidence.command("cmd", argv, result)
    event = {"name": name, "execution_id": "main", "evidence_mode": "product_cli", "event_record_id": "record",
             "transcript_marker_id": "marker", "corroboration_ids": ["cmd"], "data": {}}
    evidence.add("record", "event_record", {"run_id": evidence.run_id, "execution_id": "main", "event_name": name,
                 "marker_id": "marker", "evidence_mode": "product_cli", "command_id": "cmd",
                 "argv_sha256": hashlib.sha256(encoded(argv)).hexdigest(), "result_sha256": hashlib.sha256(encoded(result)).hexdigest()})
    with pytest.raises(contract.EventLogContractError):
        contract._validate_event_record(evidence, event, evidence.run_id)


def test_marker_only_downgrade_cannot_prove_a_diff_event():
    evidence = CapturedEvidence()
    event = {"name": "classify_repository_development", "execution_id": "main", "evidence_mode": "marker",
             "event_record_id": "record", "transcript_marker_id": "marker", "corroboration_ids": [], "data": {}}
    evidence.add("record", "event_record", {"run_id": evidence.run_id, "execution_id": "main",
                 "event_name": event["name"], "marker_id": "marker", "mode": "marker"})
    with pytest.raises(contract.EventLogContractError):
        contract._validate_event_record(evidence, event, evidence.run_id)


def search_page(*, scope="sources", pass_name="discovery", complete=True, index=0, cursor=None, next_cursor=None):
    return {"run_id": "search-run", "corpus_revision": "b" * 64, "scope": scope,
            "mode": "research", "pass_name": pass_name, "terms": ["Alpha"], "page_index": index,
            "request_cursor": cursor, "next_cursor": next_cursor, "complete": complete,
            "candidate_count": 1, "candidate_manifest": ".brain/search-runs/search-run/candidates.json",
            "candidate_manifest_sha256": "c" * 64, "result_sha256": "d" * 64,
            "matches": [], "searched_source_ids": [], "coverage_gaps": []}


@pytest.mark.parametrize("name,argv,data", [
    ("links_check", ["links", "check"], {"report": {"checks": ["graph"], "issues": [], "corpus_revision": "b" * 64}}),
    ("validate", ["validate"], {"reports": [{"checks": ["source"], "issues": [], "corpus_revision": "b" * 64}]}),
    ("source_pass_discovery", ["search", "--scope", "sources", "--pass", "discovery", "--term", "Alpha", "--context", "3"], search_page()),
])
def test_product_rows_accept_exact_commands_and_reject_extra_flags(name, argv, data):
    command = "links check" if name == "links_check" else "validate" if name == "validate" else "search"
    result = envelope(command, data)
    contract._event_command(name, ["./brain", "--json", *argv], result)
    with pytest.raises(contract.EventLogContractError):
        contract._event_command(name, ["./brain", "--json", *argv, "--unexpected"], result)


@pytest.mark.parametrize("field,value", [("complete", 1), ("page_index", False), ("candidate_count", True), ("coverage_gaps", [{}]), ("scope", "wiki"), ("pass_name", "verification"), ("mode", "freshness"), ("terms", ["Beta"])])
def test_search_start_requires_actual_typed_matching_result(field, value):
    data = search_page()
    data[field] = value
    with pytest.raises(contract.EventLogContractError):
        contract._event_command("source_pass_discovery", ["./brain", "--json", "search", "--scope", "sources", "--pass", "discovery", "--term", "Alpha", "--context", "3"], envelope("search", data))


def cursor_case():
    evidence = CapturedEvidence()
    start = ["./brain", "--json", "search", "--scope", "sources", "--pass", "discovery", "--term", "Alpha", "--context", "3"]
    evidence.command("start", start, envelope("search", search_page(complete=False, next_cursor="opaque-1")))
    evidence.command("finish", ["./brain", "--json", "search", "--cursor", "opaque-1"], envelope("search", search_page(index=1, cursor="opaque-1")))
    evidence.add("proof", "cursor_proof", {"run_id": evidence.run_id, "execution_id": "main", "family": "source_pass_discovery", "command_ids": ["start", "finish"]})
    return evidence


def test_cursor_proof_drains_exact_ordered_continuations():
    evidence = cursor_case()
    assert hasattr(contract, "_cursor"), "missing full cursor-chain validator"
    result = contract._cursor(evidence, "proof", "main", "source_pass_discovery", "finish")
    assert result["page_count"] == 2


@pytest.mark.parametrize("mutation", [
    lambda e: e.command("finish", ["./brain", "--json", "search", "--cursor", "wrong"], envelope("search", search_page(index=1, cursor="opaque-1"))),
    lambda e: e.command("finish", ["./brain", "--json", "search", "--cursor", "opaque-1"], envelope("search", search_page(index=2, cursor="opaque-1"))),
    lambda e: e.add("proof", "cursor_proof", {"run_id": e.run_id, "execution_id": "main", "family": "source_pass_discovery", "command_ids": ["finish"]}),
])
def test_cursor_proof_rejects_wrong_cursor_missing_start_and_page_gaps(mutation):
    evidence = cursor_case()
    mutation(evidence)
    assert hasattr(contract, "_cursor"), "missing full cursor-chain validator"
    with pytest.raises(contract.EventLogContractError):
        contract._cursor(evidence, "proof", "main", "source_pass_discovery", "finish")


def test_nonarchival_classification_requires_inventory_proven_diff():
    evidence = CapturedEvidence()
    assert hasattr(contract, "_diff"), "missing typed diff validator"
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [], "changed_paths": [], "staged_paths": []})
    assert contract._diff(evidence, "diff", "main")["changed_paths"] == []
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [], "changed_paths": ["wiki/questions/invented.md"], "staged_paths": []})
    with pytest.raises(contract.EventLogContractError):
        contract._diff(evidence, "diff", "main")


@pytest.mark.parametrize("field,value", [("bytes", True), ("sha256", False), ("path", "../outside"), ("path", "wiki//question.md")])
def test_diff_rejects_bad_scalar_or_path_before_content_read(field, value):
    evidence = CapturedEvidence()
    evidence.add("body", "file_capture", b"body")
    item = {"path": "wiki/questions/alpha.md", "sha256": hashlib.sha256(b"body").hexdigest(), "bytes": 4, "content_id": "body"}
    item[field] = value
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [item], "changed_paths": ["wiki/questions/alpha.md"], "staged_paths": []})
    assert hasattr(contract, "_diff"), "missing typed diff validator"
    with pytest.raises(contract.EventLogContractError):
        contract._diff(evidence, "diff", "main")


@pytest.mark.parametrize("suffix", [[], ["--extra", "1"], ["--url", "https://example.test"], ["--fixture-id", "web-approval-and-capture.rendered", "--handoff-id", "wrong"]])
def test_initial_fixture_form_cannot_be_generic_or_expanded(suffix):
    argv = ["./brain", "--json", "eval", "mock-web-capture", *suffix]
    assert hasattr(contract, "_fixture_argv"), "missing exact fixture form validator"
    with pytest.raises(contract.EventLogContractError):
        contract._fixture_argv(argv, rendered=False)


def test_registration_and_snapshot_activation_cannot_substitute_each_other():
    assert hasattr(contract, "_representation"), "missing typed activation parser"
    with pytest.raises(contract.EventLogContractError):
        contract._event_command("verify_snapshot_active_representation", ["./brain", "--json", "source", "register-extraction"], envelope("source register-extraction", {"registration": {"active_representation": {}}}))
    with pytest.raises(contract.EventLogContractError):
        contract._event_command("verify_registration_active_representation", ["./brain", "--json", "source", "snapshot-url"], envelope("source snapshot-url", {"snapshot": {"active_representation": {}}}))


def test_command_reuse_is_named_and_receipt_aliases_do_not_generalize():
    assert hasattr(contract, "_check_reuse"), "missing explicit command reuse rules"
    contract._check_reuse(["public_web_access", "snapshot_used_source", "initial_snapshot_receipt_verified"])
    contract._check_reuse(["register_extraction_handoff", "verify_registration_active_representation"])
    contract._check_reuse(["source_pass_discovery", "source_pass_discovery_drained"])
    for names in (["links_check", "validate"], ["initial_sync_receipt_verified", "post_registration_sync_receipt_verified"],
                  ["wiki_apply", "persist_claim"], ["source_pass_discovery", "source_pass_expansion"]):
        with pytest.raises(contract.EventLogContractError):
            contract._check_reuse(names)


def test_web_approval_must_bind_host_scope_note_fixture_and_phase():
    assert hasattr(contract, "_approval"), "missing exact approval binder"
    evidence = CapturedEvidence()
    manifest = {"fixture_capability_id": "capability", "approval": {"event_id": "approval-1", "scope": "one fixture", "note": "approved static capture", "decision": "approved"}}
    event = {"name": "ask_web_approval", "execution_id": "approval"}
    approval = {"run_id": evidence.run_id, "execution_id": "approval", "event_id": "approval-1", "scope": "one fixture", "note": "approved static capture", "decision": "approved", "fixture_id": "web-approval-and-capture.initial", "capability_id": "capability", "manifest_id": None}
    log = {"run_id": evidence.run_id, "scenario_id": "web-approval-and-capture", "_manifest": manifest}
    evidence.add("approval", "approval", approval)
    contract._approval(evidence, "approval", event, log)
    for field, value in (("scope", "other"), ("note", "other"), ("fixture_id", "other"), ("decision", "denied"), ("execution_id", "approved_capture")):
        evidence.add("approval", "approval", {**approval, field: value})
        with pytest.raises(contract.EventLogContractError):
            contract._approval(evidence, "approval", event, log)


def test_interpretation_can_be_withheld_without_authorizing_web():
    assert hasattr(contract, "_approval"), "missing typed interpretation decision"
    evidence = CapturedEvidence()
    approval = {"run_id": evidence.run_id, "execution_id": "main", "event_id": "interpretation-1", "scope": "Alpha limits", "note": "Preserve both", "decision": "withheld", "fixture_id": None, "capability_id": None, "manifest_id": "manifest"}
    evidence.add("approval", "approval", approval)
    manifest = {"fixture_capability_id": None, "approval": {key: approval[key] for key in ("event_id", "scope", "note", "decision")}}
    event = {"name": "ask_interpretation_approval", "execution_id": "main"}
    log = {"run_id": evidence.run_id, "scenario_id": "contradictory-evidence", "_manifest": manifest}
    assert contract._approval(evidence, "approval", event, log)["decision"] == "withheld"
    with pytest.raises(contract.EventLogContractError):
        contract._approval(evidence, "approval", {**event, "name": "ask_web_approval"}, log)


def _merge_snapshot_inventory(*inventories):
    """Combine compatible captured paths without making an implicit rewrite."""

    merged = {}
    for inventory in inventories:
        for item in inventory:
            existing = merged.get(item["path"])
            if existing is not None and existing != item:
                raise AssertionError("contract fixture snapshot path conflict: " + item["path"])
            merged[item["path"]] = copy.deepcopy(item)
    return [merged[path] for path in sorted(merged)]


def _packet_snapshot_inventory(evidence, packet_id):
    """Produce the exact local document and ledger bytes a packet relies on."""

    packet = evidence.obj(packet_id, "evidence_packet")
    inventory = []
    for citation in packet["citations"]:
        raw = evidence.read(citation["document_id"], "file_capture")
        inventory.append({"path": citation["document_path"], "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "content_id": citation["document_id"]})
    for index, ledger_id in enumerate(packet["ledger_ids"]):
        raw = evidence.read(ledger_id, "source_record")
        source_id = json.loads(raw)["source_id"]
        capture_id = f"{packet_id}-ledger-capture-{index}"
        evidence.add(capture_id, "file_capture", raw)
        inventory.append({"path": f"sources/ledger/{source_id}.json", "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "content_id": capture_id})
    return _merge_snapshot_inventory(inventory)


def seal_execution(evidence, events, run, scenario, phase, phase_prompt_id, phase_binding, trusted_executable):
    """Sourced native-record/snapshot fixture; never executable client evidence."""

    suffix = "" if phase == "main" else "-" + phase
    transcript_id, trace_id = "transcript" + suffix, "trace" + suffix
    policy_id, process_id = "policy" + suffix, "process" + suffix
    version_id, help_id = "version" + suffix, "help" + suffix
    mcp_identity = contract._file_identity((run / "mcp").stat())
    executable_path = os.fspath(trusted_executable.resolved_path)
    phase_prompt = canonical_phase_prompt_bytes(scenario, phase)
    phase_prompt_sha256 = hashlib.sha256(phase_prompt).hexdigest()
    evidence.add(phase_prompt_id, "phase_prompt", phase_prompt)
    argv = [executable_path, "--print", "--output-format", "stream-json", "--restricted", "--strict-mcp-config", "--mcp-config", str(run / "mcp"), "--no-chrome", "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)", "--verbose", phase_prompt.decode("utf-8", "strict")]
    transcript = b""
    trace = []
    initial_id = f"workspace-snapshot-{phase}-initial"
    evidence.add(initial_id, "workspace_snapshot", {
        "schema_version": 1, "run_id": evidence.run_id, "execution_id": phase,
        "anchor_kind": "phase_initial", "trace_sequence": 0,
        "inventory": [], "staged_paths": [],
    })
    evidence.initial_snapshot_ids = getattr(evidence, "initial_snapshot_ids", {}) | {phase: initial_id}
    evidence.marker_snapshot_ids = getattr(evidence, "marker_snapshot_ids", {})
    evidence.marker_baseline_snapshot_ids = getattr(evidence, "marker_baseline_snapshot_ids", {})

    def native_record(raw, inventory, staged_paths):
        nonlocal transcript
        start, end = len(transcript), len(transcript) + len(raw)
        sequence = len(trace) + 1
        snapshot_id = f"workspace-snapshot-{phase}-{sequence}"
        digest = hashlib.sha256(raw).hexdigest()
        evidence.add(snapshot_id, "workspace_snapshot", {
            "schema_version": 1, "run_id": evidence.run_id, "execution_id": phase,
            "anchor_kind": "native_record", "trace_sequence": sequence,
            "transcript_id": transcript_id, "native_record_start": start,
            "native_record_end": end, "native_record_sha256": digest,
            "inventory": copy.deepcopy(inventory), "staged_paths": list(staged_paths),
        })
        trace.append({"sequence": sequence, "kind": "native_record", "transcript_id": transcript_id,
                      "byte_start": start, "byte_end": end, "sha256": digest,
                      "workspace_snapshot_id": snapshot_id})
        transcript += raw
        return snapshot_id, start, end, digest

    native_record(native_init(getattr(evidence, "workspace", ROOT)), [], [])
    observed, used_diffs, used_packets = set(), set(), set()

    def trace_command(cid):
        if cid in observed:
            return
        observed.add(cid)
        observation = evidence.obj(cid, "command_observation")
        observation["start_sequence"] = len(trace) + 1
        trace.append({"sequence": len(trace) + 1, "kind": "command_start", "command_id": cid})
        if observation["source_state_id"] is not None:
            trace.append({"sequence": len(trace) + 1, "kind": "source_state", "command_id": cid, "source_state_id": observation["source_state_id"]})
        observation["end_sequence"] = len(trace) + 1
        trace.append({"sequence": len(trace) + 1, "kind": "command_end", "command_id": cid})
        evidence.add(cid, "command_observation", observation)

    terminal_marker_id = getattr(evidence, "terminal_marker_id", None)
    assertion_diff_id = getattr(evidence, "assertion_diff_id", "final-diff")
    for index, event in enumerate(events, 1):
        if event["execution_id"] != phase:
            continue
        record = evidence.obj(event["event_record_id"], "event_record")
        if "cursor_proof_id" in record:
            for cid in evidence.obj(record["cursor_proof_id"], "cursor_proof")["command_ids"]:
                trace_command(cid)
        if "command_id" in record:
            trace_command(record["command_id"])
        diff_id = record.get("diff_id")
        if diff_id in used_diffs:
            cloned_id = f"{diff_id}-for-{event['transcript_marker_id']}"
            evidence.add(cloned_id, "diff", copy.deepcopy(evidence.obj(diff_id, "diff")))
            record["diff_id"] = cloned_id
            evidence.add(event["event_record_id"], "event_record", record)
            diff_id = cloned_id
        if diff_id is not None:
            used_diffs.add(diff_id)
        packet_id = record.get("packet_id")
        if packet_id in used_packets:
            cloned_id = f"{packet_id}-for-{event['transcript_marker_id']}"
            evidence.add(cloned_id, "evidence_packet", copy.deepcopy(evidence.obj(packet_id, "evidence_packet")))
            record["packet_id"] = cloned_id
            evidence.add(event["event_record_id"], "event_record", record)
            packet_id = cloned_id
        if packet_id is not None:
            used_packets.add(packet_id)
        baselines = []
        if diff_id is not None:
            baselines.append(("diff", evidence.obj(diff_id, "diff")["before"]))
        if event["transcript_marker_id"] == terminal_marker_id and assertion_diff_id == "final-diff":
            baselines.append(("assertion", evidence.obj("final-diff", "diff")["before"]))
        baseline_ids = {}
        for role, inventory in baselines:
            raw = encoded({"type": "assistant", "message": {"model": "contract-test", "content": []},
                           "parent_tool_use_id": None, "session_id": "contract-test",
                           "uuid": f"baseline-{index}-{role}"}) + b"\n"
            baseline_ids[role] = native_record(raw, inventory, [])[0]
        after_inventories = []
        staged_paths = []
        if diff_id is not None:
            diff = evidence.obj(diff_id, "diff")
            after_inventories.append(diff["after"])
            staged_paths = diff["staged_paths"]
        if packet_id is not None:
            after_inventories.append(_packet_snapshot_inventory(evidence, packet_id))
        if event["transcript_marker_id"] == terminal_marker_id and assertion_diff_id == "final-diff":
            after_inventories.append(evidence.obj("final-diff", "diff")["after"])
        after = _merge_snapshot_inventory(*after_inventories) if after_inventories else []
        marker = ("EVENT:" + event["name"] + "\n").encode()
        prefix = b"The prior standard does not answer this year's change.\n" if event["name"] == "report_local_evidence_gap" else b""
        native = encoded({"type": "assistant", "message": {"model": "contract-test", "content": [{"type": "text", "text": (prefix + marker).decode()}]}, "parent_tool_use_id": None, "session_id": "contract-test", "uuid": f"message-{index}"}) + b"\n"
        snapshot_id, start, end, native_sha = native_record(native, after, staged_paths)
        evidence.add(event["transcript_marker_id"], "marker", {"run_id": evidence.run_id, "execution_id": phase, "transcript_id": transcript_id, "marker": marker.decode(), "byte_start": len(prefix), "byte_end": len(prefix) + len(marker), "excerpt_sha256": hashlib.sha256(marker).hexdigest(), "native_record_start": start, "native_record_end": end, "native_record_sha256": native_sha, "text_pointer": "/message/content/0/text"})
        evidence.marker_snapshot_ids[event["transcript_marker_id"]] = snapshot_id
        if diff_id is not None:
            diff = evidence.obj(diff_id, "diff")
            diff.update(before_snapshot_id=baseline_ids["diff"], after_snapshot_id=snapshot_id)
            evidence.add(diff_id, "diff", diff)
        if packet_id is not None:
            packet = evidence.obj(packet_id, "evidence_packet")
            packet["snapshot_id"] = snapshot_id
            evidence.add(packet_id, "evidence_packet", packet)
        if "assertion" in baseline_ids:
            evidence.marker_baseline_snapshot_ids[event["transcript_marker_id"]] = baseline_ids["assertion"]
    for cid, entry in list(evidence.entries.items()):
        if entry["type"] == "command_observation" and evidence.obj(cid, "command_observation")["execution_id"] == phase:
            trace_command(cid)
    native_record(native_result(), [], [])
    evidence.add(transcript_id, "transcript", transcript)
    evidence.add(trace_id, "execution_trace", {"schema_version": 1, "run_id": evidence.run_id, "execution_id": phase, "records": trace})
    evidence.add(version_id, "version", b"2.1.251 (Claude Code)\n")
    evidence.add(help_id, "help", b'{"help":true}')
    executable_id = "client-executable" + suffix
    executable = {
        "schema_version": 1,
        "run_id": evidence.run_id,
        "execution_id": phase,
        "phase": phase,
        "client": "claude",
        "resolved_path": executable_path,
        "launch_identity": dict(trusted_executable.identity),
        "launch_sha256": trusted_executable.sha256,
        "seal_identity": dict(trusted_executable.identity),
        "seal_sha256": trusted_executable.sha256,
        "reported_version": "2.1.251",
        "version_probe": {
            "argv": [executable_path, "--version"], "exit_code": 0, "output_id": version_id,
            "output_sha256": evidence.entries[version_id]["sha256"],
        },
        "help_probe": {
            "argv": [executable_path, "--help"], "exit_code": 0, "output_id": help_id,
            "output_sha256": evidence.entries[help_id]["sha256"],
        },
        "supported_build_id": _CONTRACT_BUILD_ID,
    }
    evidence.add(executable_id, "client_executable", executable)
    policy = {"run_id": evidence.run_id, "execution_id": phase, "phase": phase, "client": "claude", "profile": "claude-restricted-tool-surface-v1", "network_attestation": "mock-only", "argv": argv, "argv_sha256": hashlib.sha256(encoded(argv)).hexdigest(), "version_id": version_id, "help_id": help_id, "executable_id": executable_id, "phase_prompt_id": phase_prompt_id, "phase_prompt_sha256": phase_prompt_sha256, "phase_prompt_transport": "argv_final_utf8", "mcp_config_id": "mcp", "mcp_config_sha256": hashlib.sha256(contract.EMPTY_MCP_BYTES).hexdigest(), "mcp_path": str(run / "mcp"), "mcp_identity": mcp_identity}
    policy.update(phase_binding)
    evidence.add(policy_id, "policy", policy)
    evidence.add(process_id, "process", {"run_id": evidence.run_id, "execution_id": phase, "phase": phase, "client": "claude", "client_version": "2.1.251", "argv": argv, "argv_sha256": policy["argv_sha256"], "exit_code": 0, "transcript_id": transcript_id, "transcript_sha256": hashlib.sha256(transcript).hexdigest(), "mcp_identity": mcp_identity, "trace_id": trace_id, "trace_sha256": evidence.entries[trace_id]["sha256"], "native_format": "claude-stream-json-2.1.251-v1", "executable_id": executable_id, "phase_prompt_id": phase_prompt_id, "phase_prompt_sha256": phase_prompt_sha256, "phase_prompt_transport": "argv_final_utf8", **phase_binding})
    return {"id": phase, "phase": phase, "kind": "actual_client_process", "policy_id": policy_id, "process_id": process_id, "transcript_id": transcript_id}


def native_init(workspace):
    return encoded({"type": "system", "subtype": "init", "uuid": "init", "session_id": "contract-test", "claude_code_version": "2.1.251", "cwd": str(workspace), "model": "contract-test", "tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"], "mcp_servers": [], "permissionMode": "dontAsk", "apiKeySource": "contract-test", "slash_commands": [], "output_style": "default", "skills": [], "plugins": []}) + b"\n"


def native_result():
    return encoded({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1, "num_turns": 1, "is_error": False, "session_id": "contract-test", "uuid": "result", "result": "", "stop_reason": "end_turn", "total_cost_usd": 0, "usage": {}, "modelUsage": {}, "permission_denials": []}) + b"\n"


def seal_interpretation_decision(evidence, scenario, events):
    """Test-only witness builder from already sealed native terminal captures."""
    from brainlib.citations import parse_citation_definitions
    from brainlib.wiki_models import parse_question
    from tests.evals.generate_scenarios import interpretation_policy_sha256

    approval_event = next(event for event in events if event["name"] == "ask_interpretation_approval")
    terminal = next(event for event in events if event["name"] == "validate")
    apply = next(event for event in events if event["name"] == "wiki_apply")
    approval_record = evidence.obj(approval_event["event_record_id"], "event_record")
    approval = evidence.obj(approval_record["approval_id"], "approval")
    apply_record = evidence.obj(apply["event_record_id"], "event_record")
    snapshot_id = evidence.marker_snapshot_ids[terminal["transcript_marker_id"]]
    snapshot = evidence.obj(snapshot_id, "workspace_snapshot")
    path = scenario["interpretation_policy"]["question_path"]
    capture = next(entry for entry in snapshot["inventory"] if entry["path"] == path)
    body = evidence.read(capture["content_id"], "file_capture").decode()
    question = parse_question(Path(path), text=body)
    identities = sorted({(cite.source_id, cite.content_sha256, cite.derivation_id)
                         for cite in parse_citation_definitions(body, path=Path(path))})
    artifact_id = "interpretation-decision"
    evidence.add(artifact_id, "interpretation_decision", {
        "schema_version": 1, "run_id": evidence.run_id, "execution_id": terminal["execution_id"],
        "scenario_id": scenario["id"], "scenario_sha256": scenario_sha256(scenario),
        "interpretation_policy_sha256": interpretation_policy_sha256(scenario),
        "approval_event_record_id": approval_event["event_record_id"], "approval_marker_id": approval_event["transcript_marker_id"],
        "approval_id": approval_record["approval_id"], "approval_event_id": approval["event_id"], "approval_decision": approval["decision"],
        "validate_event_record_id": terminal["event_record_id"], "validate_marker_id": terminal["transcript_marker_id"],
        "terminal_snapshot_id": snapshot_id, "wiki_apply_event_record_id": apply["event_record_id"],
        "wiki_apply_command_id": apply_record["command_id"], "wiki_manifest_id": apply_record["manifest_id"],
        "question_path": path, "question_capture_id": capture["content_id"], "question_sha256": capture["sha256"],
        "question_id": question.question_id,
        "interpretation": {"decision": question.interpretation.decision,
                           "preference_citation_id": question.interpretation.preference_citation_id,
                           "approval_event_id": question.interpretation.approval_event_id},
        "claim_identities": [dict(zip(("source_id", "content_sha256", "derivation_id"), identity)) for identity in identities],
    })
    approval_record["interpretation_decision_id"] = artifact_id
    evidence.add(approval_event["event_record_id"], "event_record", approval_record)


def seal_run(tmp_path, evidence, scenario, events, receipts=(), deliveries=()):
    """A contract-test control tree, never a real client evaluation or saved run."""
    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o700, exist_ok=True)
    run = control_root / "runs" / evidence.run_id
    run.mkdir(parents=True, mode=0o700, exist_ok=True)
    fixture = json.loads((ROOT / "tests/evals/fixtures" / scenario["fixture_id"] / "fixture-manifest.json").read_text())
    evidence.add("mcp", "mcp_config", contract.EMPTY_MCP_BYTES)
    (run / "mcp").write_bytes(contract.EMPTY_MCP_BYTES)
    phases = ["approval", "approved_capture"] if scenario["network_mode"] == "mock_only" else ["main"]
    client_directory = control_root / "contract-client"
    client_directory.mkdir(mode=0o700, exist_ok=True)
    client_path = client_directory / "claude"
    client_path.write_bytes(_CONTRACT_CLIENT_BYTES)
    client_path.chmod(0o700)
    client_sha256 = hashlib.sha256(_CONTRACT_CLIENT_BYTES).hexdigest()
    client_identity = contract._file_identity(client_path.lstat())
    trusted_executables = {
        phase: contract.TrustedExecutable(
            phase, "claude", client_path.resolve(), client_identity, client_sha256, _CONTRACT_BUILD_ID,
        )
        for phase in phases
    }
    supported_builds = {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", client_sha256): _CONTRACT_BUILD_ID,
    }
    workspace = getattr(evidence, "workspace", ROOT)
    manifest = {"schema_version": 1, "run_id": evidence.run_id, "client": "claude", "scenario_id": scenario["id"], "fixture_id": scenario["fixture_id"], "fixture_sha256": fixture["tree_sha256"], "workspace": str(workspace), "phases": phases, "policy_profiles": ["claude-restricted-tool-surface-v1"] * len(phases), "fixture_capability_id": None, "approval": None, "software_paths": [], "scenario_sha256": scenario_sha256(scenario), "phase_prompt_protocol": PROMPT_PROTOCOL, "phase_prompts": [], **getattr(evidence, "manifest_overrides", {})}
    terminal_name = contract._ASSERTION_TERMINAL_EVENTS[scenario["id"]]
    terminal_event = next(event for event in events if event["name"] == terminal_name)
    evidence.terminal_marker_id = terminal_event["transcript_marker_id"]
    terminal_record = evidence.obj(terminal_event["event_record_id"], "event_record")
    evidence.assertion_diff_id = terminal_record.get("diff_id", "final-diff")
    if evidence.assertion_diff_id == "final-diff" and "final-diff" not in evidence.entries:
        evidence.add("final-diff", "diff", {"run_id": evidence.run_id, "execution_id": terminal_event["execution_id"], "before": [], "after": [], "changed_paths": [], "staged_paths": []})
    executions = []
    for index, phase in enumerate(phases, 1):
        binding = {"fixture_id": scenario["fixture_id"], "fixture_sha256": fixture["tree_sha256"], "fixture_capability_id": manifest["fixture_capability_id"] if phase == "approved_capture" else None, "approval": manifest["approval"] if phase == "approved_capture" else None}
        prompt_id = f"phase-prompt-{index}"
        prompt = canonical_phase_prompt_bytes(scenario, phase)
        executions.append(seal_execution(evidence, events, run, scenario, phase, prompt_id, binding, trusted_executables[phase]))
        manifest["phase_prompts"].append({"execution_id": phase, "phase": phase, "phase_prompt_id": prompt_id, "phase_prompt_sha256": hashlib.sha256(prompt).hexdigest(), "phase_prompt_transport": "argv_final_utf8"})
    evidence.assertion_diff_id = evidence.obj(terminal_event["event_record_id"], "event_record").get("diff_id", evidence.assertion_diff_id)
    if evidence.assertion_diff_id == "final-diff":
        final_diff = evidence.obj("final-diff", "diff")
        final_diff.update(
            before_snapshot_id=evidence.marker_baseline_snapshot_ids[evidence.terminal_marker_id],
            after_snapshot_id=evidence.marker_snapshot_ids[evidence.terminal_marker_id],
        )
        evidence.add("final-diff", "diff", final_diff)
    if scenario["id"] == "contradictory-evidence":
        seal_interpretation_decision(evidence, scenario, events)
    assertions = []
    for index, text in enumerate(scenario["repository_assertions"]):
        name = f"assertion-{index}"
        evidence.add(name, "assertion", {"run_id": evidence.run_id, "text": text, "passed": True, "diff_id": evidence.assertion_diff_id, "marker_id": evidence.terminal_marker_id, "snapshot_id": evidence.marker_snapshot_ids[evidence.terminal_marker_id]})
        if scenario["id"] == "contradictory-evidence" and index == 2:
            proof = evidence.obj(name, "assertion")
            proof["interpretation_decision_id"] = "interpretation-decision"
            evidence.add(name, "assertion", proof)
        assertions.append({"text": text, "assertion_id": name, "diff_id": evidence.assertion_diff_id, "passed": True})
    for name, (_, raw) in evidence.artifacts.items():
        if name != "mcp":
            (run / name).write_bytes(raw)
    index = encoded({"schema_version": 1, "run_id": evidence.run_id, "entries": list(evidence.entries.values())})
    (run / "evidence-index.json").write_bytes(index)
    (run / "run-manifest.json").write_bytes(encoded(manifest))
    log = {"schema_version": 1, "run_id": evidence.run_id, "scenario_id": scenario["id"], "client": "claude", "client_version": "2.1.251", "fixture_sha256": fixture["tree_sha256"], "network_mode": scenario["network_mode"], "evidence_index_sha256": hashlib.sha256(index).hexdigest(), "executions": executions, "receipts": list(receipts), "deliveries": list(deliveries), "events": events, "repository_assertions": assertions, "result": "pass", "incomplete_reasons": []}
    attestation = encoded({"schema_version": 1, "run_id": evidence.run_id, "client": "claude", "scenario_id": scenario["id"], "fixture_sha256": fixture["tree_sha256"], "evidence_index_sha256": log["evidence_index_sha256"], "log_sha256": hashlib.sha256(encoded(log)).hexdigest()})
    (run / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()
    context = contract.TrustedRunContext(
        control_root, workspace, trusted_executables=trusted_executables,
        supported_builds=supported_builds,
    )
    schema = json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text())
    return log, schema, fixture["tree_sha256"], context


def add_event(evidence, events, name, mode, command_id=None, references=None, data=None, *, execution_id="main"):
    index = len(events) + 1
    event = {"sequence": index, "name": name, "execution_id": execution_id, "evidence_mode": mode,
             "transcript_marker_id": f"marker-{index}", "event_record_id": f"event-{index}", "corroboration_ids": [] if command_id is None else [command_id], "data": {} if data is None else data}
    record = {"run_id": evidence.run_id, "execution_id": execution_id, "event_name": name, "marker_id": event["transcript_marker_id"], "evidence_mode": mode, **(references or {})}
    if command_id is not None:
        observation = evidence.obj(command_id, "command_observation")
        record.update(command_id=command_id, argv_sha256=hashlib.sha256(encoded(observation["argv"])).hexdigest(), result_sha256=hashlib.sha256(encoded(evidence.obj(observation["result_id"], "command_result"))).hexdigest())
    evidence.add(event["event_record_id"], "event_record", record)
    events.append(event)


def test_attested_receipt_lifecycle_is_valid_before_a_sealed_generated_launch(receipt_case):
    evidence, receipt_log, _ = receipt_case
    # This is deliberately not a generated scenario and cannot receive a
    # phase prompt or become a public pass. The receipt itself remains useful
    # as an actual-product unit boundary before a sealed launch joins it.
    for event in receipt_log["events"]:
        contract._validate_event_record(evidence, event, evidence.run_id)
    verified = contract._validate_receipts(
        evidence,
        {**receipt_log, "_scenario_required_events": [event["name"] for event in receipt_log["events"]]},
        receipt_log["events"],
    )
    assert verified["receipt-sync"]["result_id"].startswith("sync_")


def test_closed_marker_diff_minimal_pass(tmp_path):
    evidence = CapturedEvidence()
    scenario = next(item for item in SCENARIOS if item["id"] == "repository-development-not-archived")
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [], "changed_paths": [], "staged_paths": []})
    events = []
    for name in scenario["required_events"]:
        add_event(evidence, events, name, "marker_diff", references={"diff_id": "diff"})
    args = seal_run(tmp_path, evidence, scenario, events)
    contract.validate_event_log(args[0], args[1], scenario, args[2], args[3])


def test_complete_current_wiki_scenario_uses_real_cli_receipts_search_and_publish(tmp_path):
    from tests.evals.generate_scenarios import _build_fixture

    _build_fixture(tmp_path / "fixtures", "current-wiki-fast-path")
    workspace = tmp_path / "fixtures/current-wiki-fast-path/repo"
    for name in ("AGENTS.md", "BRAIN.md", "pyproject.toml", "brain"):
        shutil.copy2(ROOT / name, workspace / name)
    (workspace / "CLAUDE.md").symlink_to("AGENTS.md")
    for name in (".agents", ".claude", ".codex", "docs"):
        shutil.copytree(ROOT / name, workspace / name, symlinks=True)
    evidence, events, receipts = CapturedEvidence(), [], []
    evidence.workspace = workspace
    scenario = next(item for item in SCENARIOS if item["id"] == "current-wiki-fast-path")

    def invoke(cid, args):
        output, errors = io.StringIO(), io.StringIO()
        code = main(["--json", *args], cwd=workspace, stdout=output, stderr=errors)
        assert code == 0, output.getvalue() + errors.getvalue()
        result = json.loads(output.getvalue())
        evidence.command(cid, ["./brain", "--json", *args], result)
        if args[0] in {"sync", "init"} or cid == "consume":
            from tests.evals.test_semantic_evidence import capture_source_state
            result_id = result["data"]["result_manifest"]["result_id"] if args[0] in {"sync", "init"} else result["data"]["result_id"]
            capture_source_state(evidence, workspace, cid, result_id=result_id)
        return result

    verify = invoke("sync", ["sync"])
    ref = verify["data"]["result_manifest"]
    invoke("consume", ["source", "consume-sync-result", "--result-id", ref["result_id"]])
    invoke("durable", ["source", "consume-sync-result", "--result-id", ref["result_id"]])
    evidence.add("stream", "sync_stream", (workspace / ref["path"]).read_bytes())
    evidence.add("durable-receipt", "consumption_receipt", (workspace / ".brain/sync-results" / f"consumed_{ref['result_id']}.json").read_bytes())
    invoke("ack", ["source", "acknowledge-sync-result", "--result-id", ref["result_id"]])
    receipts.append({"id": "receipt-sync", "operation": "initial_sync", "verify_command_id": "sync", "consume_command_id": "consume", "durable_command_id": "durable", "acknowledge_command_id": "ack", "stream_artifact_id": "stream", "durable_artifact_id": "durable-receipt", "delivery_id": None})
    for stage, cid in zip(("verified", "consumed", "durable", "acknowledged"), ("sync", "consume", "durable", "ack")):
        add_event(evidence, events, "initial_sync_receipt_" + stage, "product_cli", cid, data={"receipt_id": "receipt-sync"})

    def capture_manifest(name, path, value):
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded(value))
        evidence.add(name + "-body", "file_capture", target.read_bytes())
        evidence.add(name, "wiki_manifest", {"path": path, "content_id": name + "-body", "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})

    manifest = {"schema_version": 1, "expected_corpus_revision": ref["corpus_revision"], "change_intent": "routine", "approval_event_id": None, "citation_rewrites": [], "link_candidate_runs": [], "changes": []}
    capture_manifest("reconcile-manifest", ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json", manifest)
    invoke("reconcile", ["wiki", "apply", "--manifest", ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"])
    add_event(evidence, events, "wiki_reconcile_citations", "product_cli", "reconcile", {"manifest_id": "reconcile-manifest"})
    search = invoke("search", ["search", "--scope", "wiki", "--term", "Alpha"])["data"]
    assert search["complete"] is True
    evidence.add("wiki-proof", "cursor_proof", {"run_id": evidence.run_id, "execution_id": "main", "family": "wiki_search", "command_ids": ["search"]})
    add_event(evidence, events, "wiki_search_drained", "product_cli", "search", {"cursor_proof_id": "wiki-proof"})
    question = "wiki/questions/what-is-alpha.md"
    body = (workspace / question).read_bytes()
    evidence.add("question-body", "file_capture", body)
    ledger_ids = []
    for index, path in enumerate(sorted((workspace / "sources/ledger").glob("src_*.json"))):
        ledger_ids.append(evidence.add(f"source-{index}", "source_record", path.read_bytes()))
    packet = {"run_id": evidence.run_id, "execution_id": "main", "kind": "wiki", "corpus_revision": ref["corpus_revision"], "cursor_proof_ids": ["wiki-proof"], "ledger_ids": ledger_ids, "citations": [{"document_path": question, "citation_id": "alpha-1", "document_id": "question-body"}], "complete": True, "current": True, "reason": None}
    evidence.add("packet", "evidence_packet", packet)
    for name in ("revalidate_underlying_citations", "build_wiki_evidence_packet", "judge_sufficient"):
        add_event(evidence, events, name, "marker", references={"packet_id": "packet"})
    staging = ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/" + question
    staged = body.replace(b"prior_phrasings: [What is Alpha?]", b"prior_phrasings: [What is Alpha?, Explain Alpha for a risk review.]")
    (workspace / staging).parent.mkdir(parents=True, exist_ok=True)
    (workspace / staging).write_bytes(staged)
    evidence.add("staged-body", "file_capture", staged)

    def inventory(path, content, content_id):
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "content_id": content_id}

    evidence.add("stage-diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [inventory(staging, staged, "staged-body")], "changed_paths": [staging], "staged_paths": []})
    add_event(evidence, events, "stage_existing_question_update", "marker_diff", references={"diff_id": "stage-diff"})
    links = invoke("candidates", ["links", "candidates", question, "--term", "Alpha"])["data"]
    assert links["complete"] is True
    evidence.add("links-proof", "cursor_proof", {"run_id": evidence.run_id, "execution_id": "main", "family": "link_candidates", "command_ids": ["candidates"]})
    add_event(evidence, events, "link_candidates_drained", "product_cli", "candidates", {"cursor_proof_id": "links-proof"})
    proof = {key: links[key] for key in ("run_id", "corpus_revision", "page_path", "terms", "candidate_manifest_sha256", "candidate_count")}
    proof["page_count"] = 1
    manifest = {**manifest, "link_candidate_runs": [proof], "changes": [{"operation": "write", "path": question, "staging_path": staging, "sha256": hashlib.sha256(staged).hexdigest()}]}
    capture_manifest("apply-manifest", ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json", manifest)
    invoke("apply", ["wiki", "apply", "--manifest", ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json"])
    assert (workspace / question).read_bytes() == staged
    evidence.add("apply-diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [inventory(question, body, "question-body")], "after": [inventory(question, staged, "staged-body")], "changed_paths": [question], "staged_paths": []})
    add_event(evidence, events, "wiki_apply", "product_cli", "apply", {"manifest_id": "apply-manifest", "diff_id": "apply-diff"})
    invoke("links-check", ["links", "check"])
    add_event(evidence, events, "links_check", "product_cli", "links-check")
    invoke("validate", ["validate"])
    add_event(evidence, events, "validate", "product_cli", "validate")
    assert [event["name"] for event in events] == scenario["required_events"]
    args = seal_run(tmp_path, evidence, scenario, events, receipts)
    contract.validate_event_log(args[0], args[1], scenario, args[2], args[3])


def delivery_case(receipt_case, *, effects_order=(0, 1), delivery_mutation=None):
    from tests.evals.test_semantic_evidence import capture_source_state
    from tests.helpers_extractors import web_test_services

    evidence, log, _ = receipt_case
    workspace = evidence.workspace
    for digit in ("1", "2"):
        (workspace / "sources/raw" / f"quarterly{digit}.pdf").write_bytes((ROOT / "tests/evals/fixtures/new-binary-before-question/repo/sources/raw/quarterly.pdf").read_bytes() + digit.encode())

    def invoke(cid, args):
        output = io.StringIO()
        code = main(["--json", *args], cwd=workspace, stdout=output, stderr=io.StringIO(), services=web_test_services())
        result = json.loads(output.getvalue())
        assert code == (1 if cid == "verify" else 0), result
        evidence.command(cid, ["./brain", "--json", *args], result, exit_code=code)
        return result

    verify = invoke("verify", ["sync"])
    reference = verify["data"]["result_manifest"]
    capture_source_state(evidence, workspace, "verify", result_id=reference["result_id"])
    consume = invoke("consume", ["source", "consume-sync-result", "--result-id", reference["result_id"]])
    capture_source_state(evidence, workspace, "consume", result_id=reference["result_id"])
    invoke("durable", ["source", "consume-sync-result", "--result-id", reference["result_id"]])
    invoke("ack", ["source", "acknowledge-sync-result", "--result-id", reference["result_id"]])
    stream = (workspace / reference["path"]).read_bytes()
    delivery = json.loads((workspace / consume["data"]["handoff_delivery"]["path"]).read_bytes())
    items = copy.deepcopy(delivery["items"])
    if effects_order != (0, 1):
        rows = [json.loads(line) for line in stream.splitlines()]
        effects = [row for row in rows[1:-1] if row["kind"] != "handoff_source_id"]
        handoffs = [row for row in rows[1:-1] if row["kind"] == "handoff_source_id"]
        effects.extend(handoffs[index] for index in effects_order)
        lines = b"".join(encoded({**row, "sequence": index}) + b"\n" for index, row in enumerate(effects, 1))
        counts = {**reference["event_counts"], "handoff_source_id": len(effects_order)}
        stream = encoded(rows[0]) + b"\n" + lines + encoded({**rows[-1], "event_count": len(effects), "event_counts": counts, "events_sha256": hashlib.sha256(lines).hexdigest()}) + b"\n"
        digest = hashlib.sha256(stream).hexdigest()
        reference = {**reference, "result_id": "sync_" + digest, "path": ".brain/sync-results/sync_" + digest + ".jsonl", "sha256": digest, "event_counts": counts}
        delivery.update(result_id=reference["result_id"], reference=copy.deepcopy(reference))
        verify["data"]["result_manifest"] = reference
        verify["data"]["handoff_source_id_count"] = len(effects_order)
        evidence.command("verify", ["./brain", "--json", "sync"], verify)
    counts = reference["event_counts"]
    if delivery_mutation:
        delivery_mutation(delivery)
    raw_delivery = encoded(delivery)
    delivery_reference = {"result_id": reference["result_id"], "path": ".brain/sync-results/handoff-delivery_" + reference["result_id"] + ".json", "sha256": hashlib.sha256(raw_delivery).hexdigest(), "item_count": 2}
    effect_digest = hashlib.sha256(b"".join(encoded({"kind": row["kind"], "data": row["data"]}) + b"\n" for row in [json.loads(line) for line in stream.splitlines()][1:-1])).hexdigest()
    evidence.add("stream", "sync_stream", stream)
    evidence.add("delivery", "delivery", raw_delivery)
    evidence.add("receipt", "consumption_receipt", {"schema_version": 2, "result_id": reference["result_id"], "reference": reference, "event_counts": counts, "effect_digest": effect_digest, "handoff_delivery": delivery_reference})
    for cid in ("consume", "durable"):
        evidence.command(cid, ["./brain", "--json", "source", "consume-sync-result", "--result-id", reference["result_id"]], envelope("source consume-sync-result", {"result_id": reference["result_id"], "status": "consumed" if cid == "consume" else "already_consumed", "manifest_path": reference["path"], "corpus_revision": reference["corpus_revision"], "event_counts": counts, "effect_digest": effect_digest, "handoff_delivery": delivery_reference}))
    evidence.command("ack", ["./brain", "--json", "source", "acknowledge-sync-result", "--result-id", reference["result_id"]], envelope("source acknowledge-sync-result", {"result_id": reference["result_id"], "sync_command": "sync", "status": "acknowledged"}))
    projected = [{"item_id": item["handoff_id"], "kind": item["kind"], "handoff_id": item["handoff_id"], "handoff_source_id": item["source_id"], "payload_sha256": hashlib.sha256(encoded(item)).hexdigest()} for item in items]
    log["receipts"][0]["delivery_id"] = "delivery-sync"
    log["deliveries"] = [{"id": "delivery-sync", "receipt_id": "receipt-sync", "delivery_artifact_id": "delivery", "items": projected}]
    log["events"].insert(3, {"sequence": 4, "name": "initial_sync_handoff_delivery_verified", "execution_id": "main", "evidence_mode": "marker_delivery", "corroboration_ids": [], "data": {"delivery_id": "delivery-sync", "item_mappings": [{"item_id": item["item_id"], "handoff_source_id": item["handoff_source_id"]} for item in projected]}})
    log["events"][-1]["sequence"] = 5
    return evidence, log


def test_delivery_matches_full_typed_items_and_ordered_durable_effects(receipt_case):
    evidence, log = delivery_case(receipt_case)
    receipts = contract._validate_receipts(evidence, log, log["events"])
    assert sorted(item["raw_path"] for item in receipts["receipt-sync"]["items"]) == ["quarterly1.pdf", "quarterly2.pdf"]


@pytest.mark.parametrize("order", [(1, 0), (0, 0), (0,), (0, 1, 1)])
def test_delivery_rejects_reordered_duplicate_missing_extra_durable_effects(receipt_case, order):
    evidence, log = delivery_case(receipt_case, effects_order=order)
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version=True), lambda d: d["items"][0].update(agent_revision=True),
    lambda d: d["items"][0].pop("required_anchor_kinds"), lambda d: d["items"][0].update(extra=True),
    lambda d: d["reference"]["event_counts"].update(hashed_path=False),
])
def test_delivery_rejects_untyped_or_noncanonical_immutable_payload(receipt_case, mutation):
    evidence, log = delivery_case(receipt_case, delivery_mutation=mutation)
    with pytest.raises(contract.EventLogContractError):
        contract._validate_receipts(evidence, log, log["events"])


@pytest.mark.parametrize("attack", ["root_symlink", "runs_symlink", "artifact_symlink", "artifact_fifo", "writable_root", "writable_artifact", "wrong_hash", "boolean_index_version", "boolean_process_exit"])
def test_public_validator_rejects_artifact_path_and_scalar_attacks(tmp_path, attack):
    import os

    evidence = CapturedEvidence()
    scenario = next(item for item in SCENARIOS if item["id"] == "repository-development-not-archived")
    evidence.add("diff", "diff", {"run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [], "changed_paths": [], "staged_paths": []})
    events = []
    for name in scenario["required_events"]:
        add_event(evidence, events, name, "marker_diff", references={"diff_id": "diff"})
    args = seal_run(tmp_path, evidence, scenario, events)
    log, schema, fixture_hash, context = args
    run = context.control_root / "runs" / evidence.run_id
    if attack == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(context.control_root, target_is_directory=True)
        context = contract.TrustedRunContext(alias, ROOT)
    elif attack == "runs_symlink":
        (context.control_root / "runs").rename(context.control_root / "actual-runs")
        (context.control_root / "runs").symlink_to(context.control_root / "actual-runs", target_is_directory=True)
    elif attack in {"artifact_symlink", "artifact_fifo"}:
        (run / "diff").rename(run / "diff-original")
        if attack == "artifact_symlink":
            (run / "diff").symlink_to(run / "diff-original")
        else:
            os.mkfifo(run / "diff")
    elif attack == "writable_root":
        context.control_root.chmod(0o777)
    elif attack == "writable_artifact":
        (run / "diff").chmod(0o666)
    elif attack == "wrong_hash":
        (run / "diff").write_bytes(b"{}")
    elif attack == "boolean_index_version":
        index = json.loads((run / "evidence-index.json").read_bytes())
        index["schema_version"] = True
        raw = encoded(index)
        (run / "evidence-index.json").write_bytes(raw)
        log["evidence_index_sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        process = evidence.obj("process", "process")
        process["exit_code"] = False
        evidence.add("process", "process", process)
        (run / "process").write_bytes(encoded(process))
        # Reseal the tampered artifact without overwriting it via seal_run.
        index = encoded({"schema_version": 1, "run_id": evidence.run_id, "entries": list(evidence.entries.values())})
        (run / "evidence-index.json").write_bytes(index)
        log["evidence_index_sha256"] = hashlib.sha256(index).hexdigest()
        unsigned = {key: value for key, value in log.items() if key != "log_attestation_sha256"}
        attestation = json.loads((run / "log-attestation.json").read_bytes())
        attestation.update(evidence_index_sha256=log["evidence_index_sha256"], log_sha256=hashlib.sha256(encoded(unsigned)).hexdigest())
        raw = encoded(attestation)
        (run / "log-attestation.json").write_bytes(raw)
        log["log_attestation_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(contract.EventLogContractError):
        contract.validate_event_log(log, schema, scenario, fixture_hash, context)


@pytest.mark.parametrize("name", sorted({name for scenario in SCENARIOS for name in scenario["required_events"]}))
def test_each_required_event_rejects_missing_mode_specific_witness(name):
    evidence = CapturedEvidence()
    rule = contract.EVENT_RULES[name]
    event = {"name": name, "execution_id": "main", "evidence_mode": rule.mode, "event_record_id": "record", "transcript_marker_id": "marker", "corroboration_ids": [], "data": {}}
    # Missing command/diff/approval/packet or delivery references must never
    # downgrade to a generic semantic marker.
    evidence.add("record", "event_record", {"run_id": evidence.run_id, "execution_id": "main", "event_name": name, "marker_id": "marker", "evidence_mode": rule.mode})
    if name == "report_local_evidence_gap":
        event["evidence_mode"] = "product_cli"
    with pytest.raises(contract.EventLogContractError):
        contract._validate_event_record(evidence, event, evidence.run_id)


@pytest.mark.parametrize("argv,phase,scenario_id", [
    (["./brain", "--json", "eval", "unrelated"], "approved_capture", "web-approval-and-capture"),
    (["./brain", "--json", "eval", "sha256", "--path", ".brain/wiki-staging/wstg_" + "1" * 32 + "/files/wiki/questions/alpha.md"], "main", "current-wiki-fast-path"),
    (["./brain", "--json", "eval", "sha256", "--path", "../alpha.md"], "approved_capture", "web-approval-and-capture"),
    (["./brain", "--json", "source", "snapshot-url", "--url", "https://example.test"], "approved_capture", "web-approval-and-capture"),
])
def test_even_unreferenced_commands_cannot_open_generic_eval_or_raw_url(argv, phase, scenario_id):
    assert hasattr(contract, "_audit_commands"), "missing complete observation audit"
    evidence = CapturedEvidence()
    command = " ".join(argv[2:4])
    evidence.command("unreferenced", argv, envelope(command, {}))
    log = {"scenario_id": scenario_id, "network_mode": "mock_only" if scenario_id == "web-approval-and-capture" else "disabled"}
    with pytest.raises(contract.EventLogContractError):
        contract._audit_commands(evidence, log, {"main": {"phase": phase}})


@pytest.mark.parametrize("field,value", [("source_version", True), ("retrieval", False), ("extraction_result", True)])
def test_snapshot_nested_contract_fields_reject_booleans(field, value):
    from brainlib.contracts import _content_version_to_dict, _retrieval_to_dict
    from tests.helpers import make_retrieval_metadata, make_source_record

    retrieval = make_retrieval_metadata()
    source = make_source_record(retrieval=retrieval)
    snapshot = {"source_id": source.source_id, "raw_path": source.current_raw_path.as_posix(), "content_sha256": source.active_content_sha256,
                "source_version": _content_version_to_dict(source.versions[source.active_content_sha256]), "retrieval": _retrieval_to_dict(retrieval),
                "extraction_result": None, "active_representation": None, "corpus_revision": "b" * 64}
    snapshot[field] = value
    with pytest.raises(contract.EventLogContractError):
        contract._snapshot(snapshot, {"corpus_revision": "b" * 64}, active=False)
