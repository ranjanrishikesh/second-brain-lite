"""Public-validator regressions for executable provenance evidence.

These construct ordinary contract fixtures only.  They do not execute a
client binary; the point is that a coherent log must not turn a bare selector
such as ``claude`` into evidence of the executable selected through PATH.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.evals import event_log_contract as contract
from tests.evals.test_event_evidence import encoded
from tests.evals.test_workflow_acceptance import build_binary_workflow, build_web_workflow


def _reseal(flow, args) -> None:
    """Apply controlled mutations to the otherwise real public fixture log."""

    log, _, _, context = args
    run = context.control_root / "runs" / log["run_id"]
    for artifact_id, (_, raw) in flow.evidence.artifacts.items():
        target = run / flow.evidence.entries[artifact_id]["relative_path"]
        if not target.exists() or target.read_bytes() != raw:
            target.write_bytes(raw)
    index = encoded({
        "schema_version": 1,
        "run_id": log["run_id"],
        "entries": list(flow.evidence.entries.values()),
    })
    (run / "evidence-index.json").write_bytes(index)
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
    (run / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()


def _rebind_main_executable(flow, args, path):
    """Retarget every public executable join to a controlled alternate path."""

    log, _, _, context = args
    raw = path.read_bytes()
    identity = contract._file_identity(path.lstat())
    digest = hashlib.sha256(raw).hexdigest()
    executable = flow.evidence.obj("client-executable", "client_executable")
    executable.update(
        resolved_path=str(path), launch_identity=identity, seal_identity=identity,
        launch_sha256=digest, seal_sha256=digest,
    )
    executable["version_probe"]["argv"][0] = str(path)
    executable["help_probe"]["argv"][0] = str(path)
    flow.evidence.add("client-executable", "client_executable", executable)
    policy = flow.evidence.obj("policy", "policy")
    process = flow.evidence.obj("process", "process")
    policy["argv"][0] = str(path)
    policy["argv_sha256"] = hashlib.sha256(encoded(policy["argv"])).hexdigest()
    process["argv"] = list(policy["argv"])
    process["argv_sha256"] = policy["argv_sha256"]
    flow.evidence.add("policy", "policy", policy)
    flow.evidence.add("process", "process", process)
    _reseal(flow, args)
    trusted = contract.TrustedExecutable(
        "main", "claude", path, identity, digest, executable["supported_build_id"],
    )
    return contract.TrustedRunContext(
        context.control_root,
        context.workspace_root,
        trusted_executables={"main": trusted},
        supported_builds=context.supported_builds,
    )


def test_public_validator_rejects_bare_client_selector_that_path_can_shadow(tmp_path):
    """A bare ``claude`` command has no path/identity provenance.

    Removing the executable-provenance check would make this test fail: the
    existing full binary workflow is otherwise internally coherent and the
    old public validator accepted it.  A runner could have resolved this
    spelling to a caller-controlled PATH shadow.
    """

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    policy = flow.evidence.obj("policy", "policy")
    process = flow.evidence.obj("process", "process")
    assert policy["argv"][0].startswith("/")
    policy["argv"][0] = "claude"
    policy["argv_sha256"] = hashlib.sha256(encoded(policy["argv"])).hexdigest()
    process["argv"] = list(policy["argv"])
    process["argv_sha256"] = policy["argv_sha256"]
    flow.evidence.add("policy", "policy", policy)
    flow.evidence.add("process", "process", process)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="executable"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_rejects_missing_executable_reference(tmp_path):
    """A self-consistent transcript and process cannot omit its trust anchor."""

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    policy = flow.evidence.obj("policy", "policy")
    policy.pop("executable_id")
    flow.evidence.add("policy", "policy", policy)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="policy shape"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_rejects_mismatched_executable_seal(tmp_path):
    """A post-launch digest swap cannot be hidden by coherent event records."""

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    executable = flow.evidence.obj("client-executable", "client_executable")
    executable["seal_sha256"] = "0" * 64
    flow.evidence.add("client-executable", "client_executable", executable)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="launch/seal"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_requires_prelaunch_context_per_execution(tmp_path):
    """Evidence bytes cannot manufacture a trusted expectation after launch."""

    flow = build_binary_workflow(tmp_path)
    log, schema, fixture_sha256, context = flow.validate()
    missing = contract.TrustedRunContext(
        context.control_root, context.workspace_root, supported_builds=context.supported_builds,
    )

    with pytest.raises(contract.EventLogContractError, match="trusted executable expectation"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, missing)


def test_public_validator_rejects_executable_replaced_after_prelaunch(tmp_path):
    """The final descriptor/hash check detects a post-launch path replacement."""

    flow = build_binary_workflow(tmp_path)
    log, schema, fixture_sha256, context = flow.validate()
    executable = context.trusted_executables["main"].resolved_path
    executable.unlink()
    executable.write_bytes(b"#!/bin/sh\nexit 99\n")
    executable.chmod(0o700)

    with pytest.raises(contract.EventLogContractError, match="trusted executable"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_rejects_probe_from_a_different_argv(tmp_path):
    """Version/help output must be from the exact sealed executable path."""

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    executable = flow.evidence.obj("client-executable", "client_executable")
    executable["version_probe"]["argv"] = [executable["resolved_path"], "--help"]
    flow.evidence.add("client-executable", "client_executable", executable)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="version_probe"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_requires_a_closed_approved_build_tuple(tmp_path):
    """A trusted file plus a claimed version cannot invent an adapter/build ID."""

    flow = build_binary_workflow(tmp_path)
    log, schema, fixture_sha256, context = flow.validate()
    unsupported = contract.TrustedRunContext(
        context.control_root,
        context.workspace_root,
        trusted_executables=context.trusted_executables,
        supported_builds={},
    )

    with pytest.raises(contract.EventLogContractError, match="unsupported client executable build"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, unsupported)


@pytest.mark.parametrize("ancestor", ["symlink", "world-writable"])
def test_public_validator_rejects_unsafe_executable_ancestor(tmp_path, ancestor):
    """Every parent of the selected executable is part of executable identity."""

    flow = build_binary_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, _ = args
    source = args[3].trusted_executables["main"].resolved_path
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir(mode=0o700)
    target = target_parent / "claude"
    target.write_bytes(source.read_bytes())
    target.chmod(0o700)
    if ancestor == "symlink":
        parent = tmp_path / "symlink-parent"
        parent.symlink_to(target_parent, target_is_directory=True)
        executable = parent / "claude"
    else:
        parent = tmp_path / "world-writable-parent"
        parent.mkdir(mode=0o700)
        parent.chmod(0o777)
        executable = parent / "claude"
        executable.write_bytes(target.read_bytes())
        executable.chmod(0o700)

    with pytest.raises(contract.EventLogContractError, match="unsafe|ancestor|executable"):
        context = _rebind_main_executable(flow, args, executable)
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)


def test_public_validator_rejects_a_probe_artifact_reused_by_two_phases(tmp_path):
    """Each actual execution owns fresh version and help probe capture bytes."""

    flow = build_web_workflow(tmp_path)
    args = flow.validate()
    log, schema, fixture_sha256, context = args
    first_policy = flow.evidence.obj("policy-approval", "policy")
    second_policy = flow.evidence.obj("policy-approved_capture", "policy")
    second_policy["version_id"] = first_policy["version_id"]
    second_policy["help_id"] = first_policy["help_id"]
    flow.evidence.add("policy-approved_capture", "policy", second_policy)
    second_executable = flow.evidence.obj("client-executable-approved_capture", "client_executable")
    for field, artifact_id in (("version_probe", first_policy["version_id"]), ("help_probe", first_policy["help_id"])):
        second_executable[field]["output_id"] = artifact_id
        second_executable[field]["output_sha256"] = flow.evidence.entries[artifact_id]["sha256"]
    flow.evidence.add("client-executable-approved_capture", "client_executable", second_executable)
    for artifact_id in ("version-approved_capture", "help-approved_capture"):
        flow.evidence.entries.pop(artifact_id)
        flow.evidence.artifacts.pop(artifact_id)
    _reseal(flow, args)

    with pytest.raises(contract.EventLogContractError, match="probe artifact"):
        contract.validate_event_log(log, schema, flow.scenario, fixture_sha256, context)
