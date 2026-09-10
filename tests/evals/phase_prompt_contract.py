"""Deterministic, runner-owned prompts for generated evaluation scenarios."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.evals.generate_scenarios import REQUIRED_IDS, SCENARIOS
from tests.evals.scenario_contract import ScenarioContractError, validate_against_schema


PROMPT_PROTOCOL = "second-brain-eval-phase-prompt-v1"
_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = json.loads((_ROOT / "tests/evals/scenario.v1.schema.json").read_text(encoding="utf-8"))
_KNOWN_EVENTS = frozenset(
    event for item in SCENARIOS for event in item["required_events"]
)
_GENERATED = {
    str(item["id"]): json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    for item in SCENARIOS
}
_APPROVAL_EVENTS = (
    "initial_sync_receipt_verified",
    "initial_sync_receipt_consumed",
    "initial_sync_receipt_durable",
    "initial_sync_receipt_acknowledged",
    "report_local_evidence_gap",
    "ask_web_approval",
)


def _validated_generated_scenario(scenario: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(scenario, Mapping):
        raise ValueError("scenario must be a mapping")
    value = dict(scenario)
    try:
        validate_against_schema(value, _SCHEMA)
    except ScenarioContractError as error:
        raise ValueError("malformed generated scenario") from error
    identifier = value.get("id")
    if type(identifier) is not str or identifier not in REQUIRED_IDS:
        raise ValueError("unknown scenario")
    events = value["required_events"]
    if any(type(event) is not str or event not in _KNOWN_EVENTS for event in events):
        raise ValueError("unknown scenario event")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if _GENERATED.get(identifier) != raw:
        raise ValueError("scenario is not the canonical generated input")
    return value


def canonical_scenario_bytes(scenario: Mapping[str, Any]) -> bytes:
    """Return the exact canonical bytes of one schema-owned generated scenario."""

    value = _validated_generated_scenario(scenario)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def scenario_sha256(scenario: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_scenario_bytes(scenario)).hexdigest()


def required_events_for_phase(scenario: Mapping[str, Any], phase: str) -> tuple[str, ...]:
    """Return the only ordered event markers the sealed phase may emit."""

    value = _validated_generated_scenario(scenario)
    if type(phase) is not str:
        raise ValueError("phase must be a string")
    events = tuple(value["required_events"])
    if value["network_mode"] != "mock_only":
        if phase != "main":
            raise ValueError("unknown scenario phase")
        return events
    if phase == "approval":
        if events[: len(_APPROVAL_EVENTS)] != _APPROVAL_EVENTS:
            raise ValueError("web approval partition is malformed")
        return _APPROVAL_EVENTS
    if phase == "approved_capture":
        capture = events[len(_APPROVAL_EVENTS) :]
        if not capture:
            raise ValueError("web capture partition is malformed")
        return capture
    raise ValueError("unknown scenario phase")


def _phase_restrictions(scenario: dict[str, Any], phase: str) -> str:
    if scenario["id"] != "web-approval-and-capture":
        return (
            "Complete only this main phase. Do not emit any marker outside the "
            "allowed list, and do not treat a marker as proof of work."
        )
    if phase == "approval":
        return (
            "First complete the listed local initial-sync receipt lifecycle in "
            "order, then report the local evidence gap and ask for approval, then stop. "
            "Do not access the public web; capture or snapshot a fixture; stage, "
            "register, publish, or persist a claim; emit a phase-two marker; or start "
            "phase two. Only the runner may release phase two."
        )
    return (
        "This approved-capture phase may begin only after the runner releases it. "
        "Do not emit approval-phase markers or any marker outside the allowed list."
    )


def canonical_phase_prompt_bytes(scenario: Mapping[str, Any], phase: str) -> bytes:
    """Build sealed UTF-8 launch bytes with a JSON-delimited raw user request."""

    value = _validated_generated_scenario(scenario)
    events = required_events_for_phase(value, phase)
    request = json.dumps(value["prompt"], ensure_ascii=False, separators=(",", ":"))
    lines = [
        "SECOND BRAIN EVALUATION SEALED PHASE PROMPT",
        f"protocol={PROMPT_PROTOCOL}",
        f"scenario_id={value['id']}",
        f"phase={phase}",
        "RAW_USER_REQUEST_JSON_BEGIN",
        request,
        "RAW_USER_REQUEST_JSON_END",
        "Perform the request only through the local evaluation workflow for this phase.",
        "After each named corroborable action is complete, emit its exact marker as one standalone line.",
        "A marker is never evidence. Emit no extra markers and preserve the listed order exactly.",
        "ALLOWED_ORDERED_EVENT_MARKERS_BEGIN",
        *("EVENT:" + name for name in events),
        "ALLOWED_ORDERED_EVENT_MARKERS_END",
        _phase_restrictions(value, phase),
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
