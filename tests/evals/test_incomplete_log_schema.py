"""Schema coverage for runner-owned incomplete diagnostic logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals import event_log_contract as contract
from tests.evals import run_cross_client as runner
from tests.evals.scenario_contract import validate_against_schema
from tests.evals.test_marker_snapshot_contract import _minimal_pass, _reseal


ROOT = Path(__file__).resolve().parents[2]


def _claude_template() -> list[str]:
    return [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--restricted",
        "--strict-mcp-config",
        "--mcp-config",
        "{mcp_config}",
        "--no-chrome",
        "--no-session-persistence",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Edit,Write,Glob,Grep,Bash",
        "--allowedTools",
        "Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)",
        "--verbose",
        "{prompt}",
    ]


def _schema() -> dict[str, object]:
    return json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text())


def _unregistered_outcome(tmp_path: Path) -> runner.RunOutcome:
    return runner.run_scenario(
        client="claude",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
    )


def _phase_one_diagnostic_log() -> dict[str, object]:
    """The bridge's sealed public shape has no semantic assertion authority."""

    scenario = runner._scenario("web-approval-and-capture")
    return runner._incomplete_log(
        run_id="a" * 64,
        scenario=scenario,
        client="claude",
        version="2.1.251",
        fixture_sha256="b" * 64,
        executions=[{
            "id": "execution-1", "phase": "approval", "kind": "actual_client_process",
            "policy_id": "policy-1", "process_id": "process-1", "transcript_id": "transcript-1",
        }],
        reasons=["public_semantic_projection_unavailable"],
    )


def test_unregistered_runner_diagnostic_is_schema_valid_with_no_execution_or_event(
    tmp_path: Path,
) -> None:
    """The advertised no-registration path is a well-formed incomplete log."""

    outcome = _unregistered_outcome(tmp_path)

    assert outcome.log["result"] == "incomplete"
    assert outcome.log["incomplete_reasons"] == ["client_executable_unregistered"]
    assert outcome.log["executions"] == []
    assert outcome.log["events"] == []
    validate_against_schema(outcome.log, _schema())


def test_rejected_policy_diagnostic_is_schema_valid_with_no_execution_or_event(
    tmp_path: Path,
) -> None:
    """Rejected argv cannot require a fabricated process just to fit the schema."""

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(["claude", "--unsafe-extra-flag"]),
        network_attestation="mock-only",
        scratch_root=tmp_path,
    )

    assert outcome.log["result"] == "incomplete"
    assert outcome.log["incomplete_reasons"] == ["policy_rejected"]
    assert outcome.log["executions"] == []
    assert outcome.log["events"] == []
    validate_against_schema(outcome.log, _schema())


def test_phase_one_diagnostic_empty_assertions_are_schema_valid() -> None:
    """The closed bridge must not fabricate an assertion or a diff reference."""

    log = _phase_one_diagnostic_log()
    assert log["repository_assertions"] == []
    validate_against_schema(log, _schema())


def test_empty_diagnostic_arrays_cannot_turn_into_an_accepted_pass(tmp_path: Path) -> None:
    """Structural support for diagnostics never relaxes the pass validator."""

    outcome = _unregistered_outcome(tmp_path)
    claimed = dict(outcome.log)
    claimed["result"] = "pass"
    claimed["incomplete_reasons"] = []

    validate_against_schema(claimed, _schema())
    with pytest.raises(contract.EventLogContractError):
        contract.validate_event_log(
            claimed,
            _schema(),
            runner._scenario("current-wiki-fast-path"),
            claimed["fixture_sha256"],
            contract.TrustedRunContext(outcome.control_root, outcome.workspace),
        )


def test_empty_bridge_assertions_fail_an_otherwise_valid_sealed_pass(tmp_path: Path) -> None:
    """Empty diagnostic assertions cannot be substituted into valid pass evidence."""

    evidence, scenario, args = _minimal_pass(tmp_path)
    log, schema, fixture_sha256, context = args
    contract.validate_event_log(log, schema, scenario, fixture_sha256, context)

    log["repository_assertions"] = []
    _reseal(evidence, args)
    validate_against_schema(log, schema)

    with pytest.raises(contract.EventLogContractError, match="scenario assertions"):
        contract.validate_event_log(log, schema, scenario, fixture_sha256, context)
