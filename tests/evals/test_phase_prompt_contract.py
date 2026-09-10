from __future__ import annotations

import copy
import hashlib
import json

import pytest

from tests.evals.generate_scenarios import SCENARIOS
from tests.evals.phase_prompt_contract import (
    PROMPT_PROTOCOL,
    canonical_phase_prompt_bytes,
    canonical_scenario_bytes,
    required_events_for_phase,
    scenario_sha256,
)


def _scenario(identifier: str) -> dict[str, object]:
    return copy.deepcopy(next(item for item in SCENARIOS if item["id"] == identifier))


def test_canonical_scenario_bytes_and_hash_are_stable_and_not_pretty_json() -> None:
    scenario = _scenario("empty-wiki-first-question")

    raw = canonical_scenario_bytes(scenario)

    assert raw == json.dumps(
        scenario, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert b"\n" not in raw
    assert scenario_sha256(scenario) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: str(item["id"]))
def test_non_web_main_phase_owns_every_required_event_in_declared_order(
    scenario: dict[str, object],
) -> None:
    if scenario["network_mode"] == "mock_only":
        pytest.skip("web phase partition is specified separately")

    assert required_events_for_phase(scenario, "main") == tuple(
        scenario["required_events"]
    )
    with pytest.raises(ValueError):
        required_events_for_phase(scenario, "approval")


def test_web_phase_partition_seals_the_full_receipt_lifecycle_before_approval() -> None:
    scenario = _scenario("web-approval-and-capture")

    assert scenario["receipt_operations"] == [
        "initial_sync",
        "initial_snapshot",
        "rendered_snapshot",
    ]
    assert required_events_for_phase(scenario, "approval") == (
        "initial_sync_receipt_verified",
        "initial_sync_receipt_consumed",
        "initial_sync_receipt_durable",
        "initial_sync_receipt_acknowledged",
        "report_local_evidence_gap",
        "ask_web_approval",
    )
    assert required_events_for_phase(scenario, "approved_capture") == tuple(
        scenario["required_events"][6:]
    )


def test_phase_prompt_is_deterministic_enveloped_and_preserves_request_as_json_string() -> None:
    scenario = _scenario("current-wiki-fast-path")

    raw = canonical_phase_prompt_bytes(scenario, "main")
    text = raw.decode("utf-8")

    assert PROMPT_PROTOCOL in text
    assert "phase=main" in text
    assert json.dumps(scenario["prompt"], ensure_ascii=False) in text
    assert [line for line in text.splitlines() if line.startswith("EVENT:")] == [
        "EVENT:" + name for name in scenario["required_events"]
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(id="unknown-scenario"),
        lambda value: value.update(required_events=["unknown_event"]),
        lambda value: value.update(prompt=object()),
    ],
)
def test_phase_prompt_contract_fails_closed_for_noncanonical_scenarios(mutation) -> None:
    scenario = _scenario("empty-wiki-first-question")
    mutation(scenario)

    with pytest.raises(ValueError):
        canonical_scenario_bytes(scenario)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(prompt="Caller-replaced request"),
        lambda value: value.update(required_events=value["required_events"][:-1]),
    ],
)
def test_phase_prompt_contract_rejects_known_id_scenarios_changed_from_generator(mutation) -> None:
    scenario = _scenario("empty-wiki-first-question")
    mutation(scenario)

    with pytest.raises(ValueError, match="canonical generated"):
        canonical_scenario_bytes(scenario)
