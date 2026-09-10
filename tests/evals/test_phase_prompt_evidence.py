"""Public-validator regressions for sealed runner-owned phase prompts."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.evals import event_log_contract as contract
from tests.evals.generate_scenarios import SCENARIOS
from tests.evals.test_event_evidence import CapturedEvidence, add_event, encoded, seal_run
from tests.evals.test_workflow_acceptance import build_binary_workflow, build_web_workflow


def _reseal(flow, args) -> None:
    log, _, _, context = args
    run = context.control_root / "runs" / log["run_id"]
    for artifact_id, (_, raw) in flow.evidence.artifacts.items():
        target = run / flow.evidence.entries[artifact_id]["relative_path"]
        if not target.exists() or target.read_bytes() != raw:
            target.write_bytes(raw)
    index = encoded({"schema_version": 1, "run_id": log["run_id"], "entries": list(flow.evidence.entries.values())})
    (run / "evidence-index.json").write_bytes(index)
    log["evidence_index_sha256"] = hashlib.sha256(index).hexdigest()
    unsigned = {key: value for key, value in log.items() if key != "log_attestation_sha256"}
    attestation = encoded({"schema_version": 1, "run_id": log["run_id"], "client": log["client"], "scenario_id": log["scenario_id"], "fixture_sha256": log["fixture_sha256"], "evidence_index_sha256": log["evidence_index_sha256"], "log_sha256": contract._canon(unsigned)})
    (run / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()


def _replace_prompt(flow, args, prompt_id: str, raw: bytes, *, transport: str | None = None) -> None:
    """Keep every host-written join self-consistent except the intended attack."""

    log, _, _, context = args
    digest = hashlib.sha256(raw).hexdigest()
    flow.evidence.add(prompt_id, "phase_prompt", raw)
    for execution in log["executions"]:
        policy_id, process_id = execution["policy_id"], execution["process_id"]
        policy = flow.evidence.obj(policy_id, "policy")
        process = flow.evidence.obj(process_id, "process")
        if policy["phase_prompt_id"] != prompt_id:
            continue
        policy["phase_prompt_sha256"] = digest
        process["phase_prompt_sha256"] = digest
        if transport is not None:
            policy["phase_prompt_transport"] = transport
            process["phase_prompt_transport"] = transport
        flow.evidence.add(policy_id, "policy", policy)
        flow.evidence.add(process_id, "process", process)
    manifest_path = context.control_root / "runs" / log["run_id"] / "run-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["phase_prompts"]:
        if entry["phase_prompt_id"] == prompt_id:
            entry["phase_prompt_sha256"] = digest
            if transport is not None:
                entry["phase_prompt_transport"] = transport
    manifest_path.write_bytes(encoded(manifest))
    _reseal(flow, args)


def _append_native_marker(flow, args, *, phase: str, anchor_event: str, marker: str) -> None:
    """Append one marker line while preserving every unrelated evidence join."""

    log, _, _, _ = args
    execution = next(item for item in log["executions"] if item["id"] == phase)
    anchor = next(item for item in log["events"] if item["execution_id"] == phase and item["name"] == anchor_event)
    marker_row = flow.evidence.obj(anchor["transcript_marker_id"], "marker")
    transcript_id = execution["transcript_id"]
    original = flow.evidence.read(transcript_id, "transcript")
    original_native = contract._native_transcript(
        original, log["client"], log["client_version"],
        flow.evidence.obj(execution["process_id"], "process")["native_format"],
    )
    lines = original.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        if offset == marker_row["native_record_start"]:
            record = json.loads(line)
            pointer = marker_row["text_pointer"].split("/")
            record[pointer[1]][pointer[2]][int(pointer[3])][pointer[4]] += marker
            lines[index] = encoded(record) + b"\n"
            break
        offset += len(line)
    else:  # pragma: no cover - the valid fixture always has the anchor.
        raise AssertionError("native marker anchor missing")
    replacement = b"".join(lines)
    replacement_native = contract._native_transcript(
        replacement, log["client"], log["client_version"],
        flow.evidence.obj(execution["process_id"], "process")["native_format"],
    )
    native_offsets = dict(zip(original_native, replacement_native, strict=True))
    replacement_rows = dict(replacement_native)

    trace_id = flow.evidence.obj(execution["process_id"], "process")["trace_id"]
    trace = flow.evidence.obj(trace_id, "execution_trace")
    for entry in trace["records"]:
        if entry["kind"] != "native_record":
            continue
        new_start = native_offsets[entry["byte_start"]]
        parsed = replacement_rows[new_start]
        entry.update(byte_start=new_start, byte_end=parsed["end"], sha256=parsed["sha256"])
        snapshot = flow.evidence.obj(entry["workspace_snapshot_id"], "workspace_snapshot")
        snapshot.update(native_record_start=new_start, native_record_end=parsed["end"], native_record_sha256=parsed["sha256"])
        flow.evidence.add(entry["workspace_snapshot_id"], "workspace_snapshot", snapshot)
    flow.evidence.add(trace_id, "execution_trace", trace)
    for event in log["events"]:
        row = flow.evidence.obj(event["transcript_marker_id"], "marker")
        if row["execution_id"] != phase:
            continue
        new_start = native_offsets[row["native_record_start"]]
        parsed = replacement_rows[new_start]
        row.update(native_record_start=new_start, native_record_end=parsed["end"], native_record_sha256=parsed["sha256"])
        flow.evidence.add(event["transcript_marker_id"], "marker", row)
    flow.evidence.add(transcript_id, "transcript", replacement)
    process = flow.evidence.obj(execution["process_id"], "process")
    process.update(transcript_sha256=hashlib.sha256(replacement).hexdigest(), trace_sha256=flow.evidence.entries[trace_id]["sha256"])
    flow.evidence.add(execution["process_id"], "process", process)
    _reseal(flow, args)


def _sealed_marker_only_run(tmp_path):
    """Build a public-pass fixture without product workflow setup."""

    scenario = next(item for item in SCENARIOS if item["id"] == "repository-development-not-archived")
    evidence = CapturedEvidence()
    evidence.add("diff", "diff", {
        "run_id": evidence.run_id, "execution_id": "main", "before": [], "after": [],
        "changed_paths": [], "staged_paths": [],
    })
    events = []
    for name in scenario["required_events"]:
        add_event(evidence, events, name, "marker_diff", references={"diff_id": "diff"})
    args = seal_run(tmp_path, evidence, scenario, events)
    contract.validate_event_log(args[0], args[1], scenario, args[2], args[3])
    return SimpleNamespace(evidence=evidence, scenario=scenario), args


def test_public_pass_rejects_old_policy_shape_without_phase_prompt_join(
    tmp_path,
) -> None:
    """An old policy shape cannot be made compatible by a coherent transcript."""

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    policy = flow.evidence.obj("policy", "policy")
    policy.pop("phase_prompt_id")
    flow.evidence.add("policy", "policy", policy)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="policy shape"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


@pytest.mark.parametrize(
    "raw",
    [
        lambda value: value[:-1] + b"X",
        lambda value: value + "\u00e9".encode("utf-8"),
        lambda value: value + "e\u0301".encode("utf-8"),
        lambda value: value + b"\0",
        lambda value: value + b"\n",
    ],
)
def test_public_pass_rejects_one_byte_unicode_nul_or_newline_phase_prompt_changes(tmp_path, raw) -> None:
    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    prompt = flow.evidence.read("phase-prompt-1", "phase_prompt")
    _replace_prompt(flow, args, "phase-prompt-1", raw(prompt))
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="phase prompt bytes"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_raw_scenario_request_instead_of_sealed_envelope(tmp_path) -> None:
    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    raw_request = json.dumps(flow.scenario["prompt"], ensure_ascii=False).encode("utf-8")
    _replace_prompt(flow, args, "phase-prompt-1", raw_request)
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="phase prompt bytes"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_incorrect_phase_prompt_transport(tmp_path) -> None:
    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    _replace_prompt(flow, args, "phase-prompt-1", flow.evidence.read("phase-prompt-1", "phase_prompt"), transport="stdin_utf8")
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="phase prompt binding"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


@pytest.mark.parametrize("field,value", [
    ("scenario_sha256", "0" * 64),
    ("phase_prompt_protocol", "wrong-protocol"),
])
def test_public_pass_rejects_wrong_manifest_prompt_protocol_or_scenario_hash(tmp_path, field, value) -> None:
    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    manifest_path = context.control_root / "runs" / log["run_id"] / "run-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest[field] = value
    manifest_path.write_bytes(encoded(manifest))
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="run manifest phase prompt"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_swapped_phase_prompt_artifacts(tmp_path) -> None:
    flow = build_web_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    manifest_path = context.control_root / "runs" / log["run_id"] / "run-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    first, second = manifest["phase_prompts"]
    first["phase_prompt_id"], second["phase_prompt_id"] = second["phase_prompt_id"], first["phase_prompt_id"]
    replacements = {entry["execution_id"]: entry for entry in manifest["phase_prompts"]}
    for execution in log["executions"]:
        policy = flow.evidence.obj(execution["policy_id"], "policy")
        process = flow.evidence.obj(execution["process_id"], "process")
        replacement = replacements[execution["id"]]
        for value in (policy, process):
            value["phase_prompt_id"] = replacement["phase_prompt_id"]
            value["phase_prompt_sha256"] = replacement["phase_prompt_sha256"]
            flow.evidence.add(execution["policy_id"] if value is policy else execution["process_id"], "policy" if value is policy else "process", value)
    manifest_path.write_bytes(encoded(manifest))
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="phase prompt bytes"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_unreferenced_phase_prompt_artifact(tmp_path) -> None:
    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    flow.evidence.add("phase-prompt-2", "phase_prompt", b"unreferenced")
    _reseal(flow, args)
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="unreferenced phase prompt"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_duplicate_reused_phase_prompt_id(tmp_path) -> None:
    flow = build_web_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    manifest_path = context.control_root / "runs" / log["run_id"] / "run-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["phase_prompts"][1]["phase_prompt_id"] = manifest["phase_prompts"][0]["phase_prompt_id"]
    manifest_path.write_bytes(encoded(manifest))
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="run manifest phase prompt"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_an_extra_standalone_native_event_marker(tmp_path) -> None:
    """A new native EVENT line must have exactly one indexed event-row join."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow, args, phase="main", anchor_event="use_software_workflow", marker="EVENT:unindexed_marker\n",
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_marker_after_a_longer_fence_closer(tmp_path) -> None:
    """A longer matching closer ends a fenced block under CommonMark."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow,
        args,
        phase="main",
        anchor_event="use_software_workflow",
        marker=(
            "```text\n"
            "EVENT:unindexed_marker\n"
            "````\n"
            "EVENT:unindexed_marker\n"
        ),
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_marker_after_invalid_backtick_fence_info(tmp_path) -> None:
    """A backtick in a backtick-fence info string cannot open a fence."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow,
        args,
        phase="main",
        anchor_event="use_software_workflow",
        marker="```text`invalid\nEVENT:unindexed_marker\n",
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_ignores_marker_in_multiline_inline_code_span(tmp_path) -> None:
    """A marker physical line inside an inline code span is not a marker."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow,
        args,
        phase="main",
        anchor_event="use_software_workflow",
        marker="Before ``\nEVENT:unindexed_marker\n`` after\n",
    )
    log, schema, fixture_sha256, context = args

    contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_marker_after_backslash_terminated_code_span(tmp_path) -> None:
    """Backslashes do not escape a closer once parsing an inline code span."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow,
        args,
        phase="main",
        anchor_event="use_software_workflow",
        marker="`code\\`\nEVENT:unindexed_marker\n`\n",
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


@pytest.mark.parametrize("suffix", [
    '[x](/foo`)\nEVENT:unindexed_marker\n`\n',
    'Before <a title="`">\nEVENT:unindexed_marker\n`\n',
    '[a][`\nEVENT:unindexed_marker\n`]\n\n[` EVENT:unindexed_marker `]: /url\n',
], ids=["link-destination", "html-attribute", "reference-label"])
def test_public_pass_rejects_marker_near_non_code_backticks(tmp_path, suffix) -> None:
    """Backticks consumed by link/HTML tokens cannot hide an unindexed marker."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow, args, phase="main", anchor_event="use_software_workflow", marker=suffix,
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_rejects_marker_after_bare_cr_and_closed_fence(tmp_path) -> None:
    """CommonMark normalizes bare CR without shifting the original marker bytes."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow, args, phase="main", anchor_event="use_software_workflow",
        marker="prose\r```text\ncode\n```\nEVENT:unindexed_marker\n\n",
    )
    log, schema, fixture_sha256, context = args

    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


@pytest.mark.parametrize("suffix", [
    "Before ` unmatched ``\nEVENT:unindexed_marker\n`` after\n",
    "Before \\``\nEVENT:unindexed_marker\n` after\n",
], ids=["delayed-width", "escaped-first-backtick"])
def test_public_pass_ignores_parser_recognized_multiline_code(tmp_path, suffix) -> None:
    """Unmatched/escaped backticks do not block a later valid code opener."""

    flow, args = _sealed_marker_only_run(tmp_path)
    _append_native_marker(
        flow, args, phase="main", anchor_event="use_software_workflow", marker=suffix,
    )
    log, schema, fixture_sha256, context = args

    contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_pass_audits_only_standalone_native_event_marker_lines(tmp_path) -> None:
    """Prose and code mentions are inert, but an extra marker line is not."""

    ignored_root = tmp_path / "ignored"
    ignored_root.mkdir()
    ignored_flow, ignored_args = _sealed_marker_only_run(ignored_root)
    _append_native_marker(
        ignored_flow,
        ignored_args,
        phase="main",
        anchor_event="use_software_workflow",
        marker=(
            "The literal EVENT:unindexed_marker appears in ordinary prose.\n"
            "Inline code `EVENT:unindexed_marker` is also ordinary text.\n"
            "```text\n"
            "EVENT:unindexed_marker\n"
            "```\n"
        ),
    )
    log, schema, fixture_sha256, context = ignored_args
    contract.validate_event_log(log, schema, ignored_flow.scenario, fixture_sha256, context)

    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    extra_flow, extra_args = _sealed_marker_only_run(extra_root)
    _append_native_marker(
        extra_flow,
        extra_args,
        phase="main",
        anchor_event="use_software_workflow",
        marker="EVENT:unindexed_marker\n",
    )
    log, schema, fixture_sha256, context = extra_args
    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        contract.validate_event_log(log, schema, extra_flow.scenario, fixture_sha256, context)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_initial_sync_acknowledgement",
        "report_gap_before_acknowledgement",
        "approved_capture_event_in_approval_phase",
    ],
)
def test_web_phase_one_rejects_incomplete_or_cross_phase_lifecycle(tmp_path, mutation) -> None:
    flow = build_web_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    if mutation == "missing_initial_sync_acknowledgement":
        log["events"] = [event for event in log["events"] if event["name"] != "initial_sync_receipt_acknowledged"]
    elif mutation == "report_gap_before_acknowledgement":
        gap = next(index for index, event in enumerate(log["events"]) if event["name"] == "report_local_evidence_gap")
        acknowledgement = next(index for index, event in enumerate(log["events"]) if event["name"] == "initial_sync_receipt_acknowledged")
        log["events"][gap], log["events"][acknowledgement] = log["events"][acknowledgement], log["events"][gap]
    else:
        event = next(event for event in log["events"] if event["name"] == "public_web_access")
        event["execution_id"] = "approval"
        record = flow.evidence.obj(event["event_record_id"], "event_record")
        record["execution_id"] = "approval"
        flow.evidence.add(event["event_record_id"], "event_record", record)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_web_phase_one_rejects_a_mismatched_initial_sync_receipt_result_id(tmp_path) -> None:
    flow = build_web_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    result_id = "initial_sync-consume-result"
    result = flow.evidence.obj(result_id, "command_result")
    result["data"]["result_id"] = "sync_" + "f" * 64
    flow.evidence.add(result_id, "command_result", result)
    observation = flow.evidence.obj("initial_sync-consume", "command_observation")
    observation["result_sha256"] = hashlib.sha256(encoded(result)).hexdigest()
    flow.evidence.add("initial_sync-consume", "command_observation", observation)
    for event in log["events"]:
        record = flow.evidence.obj(event["event_record_id"], "event_record")
        if record.get("command_id") == "initial_sync-consume":
            record["result_sha256"] = observation["result_sha256"]
            flow.evidence.add(event["event_record_id"], "event_record", record)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)
