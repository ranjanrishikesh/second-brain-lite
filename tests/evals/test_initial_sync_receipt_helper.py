"""Focused sealed-read regressions for the phase-one initial-sync receipt."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
from pathlib import Path
from types import MappingProxyType

import pytest

from brainlib.cli import main
import tests.evals.event_log_contract as contract
from tests.evals.test_event_evidence import CapturedEvidence, delivery_case
from tests.evals.test_semantic_evidence import capture_source_state


ROOT = Path(__file__).resolve().parents[2]
_STAGES = ("verified", "consumed", "durable", "acknowledged")
_COMMAND_IDS = ("verify", "consume", "durable", "acknowledge")


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class _WriteForbiddenSealedReader:
    """Freeze a test artifact corpus after capture; the helper may only read it."""

    def __init__(self, evidence: CapturedEvidence) -> None:
        self.run_id = evidence.run_id
        self.entries = MappingProxyType(copy.deepcopy(evidence.entries))
        self._artifacts = MappingProxyType({
            artifact_id: (artifact_type, raw)
            for artifact_id, (artifact_type, raw) in evidence.artifacts.items()
        })
        self.write_attempts = 0
        self.fail_read_for: str | None = None
        self.raise_unexpected_read_error = False

    def read(self, artifact_id: str, artifact_type: str) -> bytes:
        if self.raise_unexpected_read_error:
            raise AssertionError("unexpected reader programming failure")
        if artifact_id == self.fail_read_for:
            raise KeyError(artifact_id)
        entry = self.entries.get(artifact_id)
        if entry is None or entry["type"] != artifact_type:
            raise contract.EventLogContractError("untyped or unindexed evidence")
        observed_type, raw = self._artifacts[artifact_id]
        if observed_type != artifact_type:
            raise contract.EventLogContractError("artifact type changed after seal")
        if (len(raw) != entry["bytes"]
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]):
            raise contract.EventLogContractError("sealed artifact digest")
        return raw

    def obj(self, artifact_id: str, artifact_type: str) -> dict[str, object]:
        value = json.loads(self.read(artifact_id, artifact_type))
        if type(value) is not dict:
            raise contract.EventLogContractError("sealed artifact object")
        return value

    def add(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.write_attempts += 1
        raise AssertionError("the sealed artifact reader is write-forbidden")

    def inventory(self) -> tuple[tuple[str, str, bytes, str, int], ...]:
        return tuple(sorted(
            (
                artifact_id,
                artifact_type,
                raw,
                str(self.entries[artifact_id]["sha256"]),
                int(self.entries[artifact_id]["bytes"]),
            )
            for artifact_id, (artifact_type, raw) in self._artifacts.items()
        ))


def _receipt_builder(
    tmp_path: Path,
) -> tuple[CapturedEvidence, tuple[object, ...], object, str]:
    """Capture a genuine product receipt lifecycle before freezing its bytes."""

    workspace = tmp_path / "workspace"
    shutil.copytree(
        ROOT / "tests/evals/fixtures/repository-development-not-archived/repo",
        workspace,
    )
    for name in ("AGENTS.md", "BRAIN.md", "brain", "pyproject.toml"):
        shutil.copyfile(ROOT / name, workspace / name)
    evidence = CapturedEvidence()

    def invoke(command_id: str, args: list[str]) -> dict[str, object]:
        output = io.StringIO()
        error = io.StringIO()
        assert main(["--json", *args], cwd=workspace, stdout=output, stderr=error) == 0, (
            output.getvalue() + error.getvalue()
        )
        result = json.loads(output.getvalue())
        evidence.command(command_id, ["./brain", "--json", *args], result)
        return result

    verify = invoke("verify", ["sync"])
    reference = verify["data"]["result_manifest"]
    assert type(reference) is dict
    result_id = reference["result_id"]
    assert type(result_id) is str
    capture_source_state(evidence, workspace, "verify", result_id=result_id)
    invoke("consume", ["source", "consume-sync-result", "--result-id", result_id])
    capture_source_state(evidence, workspace, "consume", result_id=result_id)
    invoke("durable", ["source", "consume-sync-result", "--result-id", result_id])
    invoke("acknowledge", ["source", "acknowledge-sync-result", "--result-id", result_id])

    evidence.add(
        "verify-stream", "sync_stream", (workspace / reference["path"]).read_bytes(),
    )
    evidence.add(
        "durable-durable", "consumption_receipt",
        (workspace / ".brain/sync-results" / f"consumed_{result_id}.json").read_bytes(),
    )
    # ``CapturedEvidence.command`` has no native-marker rows.  Model the
    # shared trace explicitly: every command ends, its parser-bound marker is
    # recorded, and only then may the next command start.
    for index, command_id in enumerate(_COMMAND_IDS):
        observation = evidence.obj(command_id, "command_observation")
        observation["start_sequence"] = 1 + 3 * index
        observation["end_sequence"] = 2 + 3 * index
        evidence.add(command_id, "command_observation", observation)
    boundaries = tuple(
        contract.InitialSyncReceiptStageBoundary(stage=stage, trace_sequence=3 + 3 * index)
        for index, stage in enumerate(_STAGES)
    )
    selection = contract.InitialSyncReceiptSelection(
        verify_command_id="verify",
        consume_command_id="consume",
        durable_command_id="durable",
        acknowledge_command_id="acknowledge",
        stream_artifact_id="verify-stream",
        durable_artifact_id="durable-durable",
    )
    return evidence, boundaries, selection, result_id


def _sealed_receipt(
    tmp_path: Path,
) -> tuple[_WriteForbiddenSealedReader, tuple[object, ...], object, str]:
    builder, boundaries, selection, result_id = _receipt_builder(tmp_path)
    return _WriteForbiddenSealedReader(builder), boundaries, selection, result_id


def _rename_artifact(evidence: CapturedEvidence, old_id: str, new_id: str) -> None:
    artifact_type, raw = evidence.artifacts.pop(old_id)
    evidence.entries.pop(old_id)
    evidence.add(new_id, artifact_type, raw)


def _replace_command_result(
    evidence: CapturedEvidence,
    command_id: str,
    result: dict[str, object],
) -> None:
    evidence.add(command_id + "-result", "command_result", result)
    observation = evidence.obj(command_id, "command_observation")
    observation["result_sha256"] = hashlib.sha256(_encoded(result)).hexdigest()
    evidence.add(command_id, "command_observation", observation)


def _handoff_receipt_builder(
    tmp_path: Path,
) -> tuple[CapturedEvidence, tuple[object, ...], object]:
    """Build a real product sync stream whose typed effects include a handoff."""

    workspace = tmp_path / "handoff-workspace"
    shutil.copytree(
        ROOT / "tests/evals/fixtures/repository-development-not-archived/repo",
        workspace,
    )
    for name in ("AGENTS.md", "BRAIN.md", "brain", "pyproject.toml"):
        shutil.copyfile(ROOT / name, workspace / name)
    evidence = CapturedEvidence()
    evidence.workspace = workspace
    # ``delivery_case`` captures genuine CLI result/stream/durable/delivery
    # bytes; its event-log shaping is irrelevant to this sealed-read helper.
    evidence, _ = delivery_case((
        evidence,
        {"receipts": [{}], "deliveries": [], "events": []},
        None,
    ))
    command_ids = ("verify", "consume", "durable", "ack")
    for index, command_id in enumerate(command_ids):
        observation = evidence.obj(command_id, "command_observation")
        observation["start_sequence"] = 1 + 3 * index
        observation["end_sequence"] = 2 + 3 * index
        evidence.add(command_id, "command_observation", observation)
    boundaries = tuple(
        contract.InitialSyncReceiptStageBoundary(stage=stage, trace_sequence=3 + 3 * index)
        for index, stage in enumerate(_STAGES)
    )
    selection = contract.InitialSyncReceiptSelection(
        verify_command_id="verify",
        consume_command_id="consume",
        durable_command_id="durable",
        acknowledge_command_id="ack",
        stream_artifact_id="stream",
        durable_artifact_id="receipt",
    )
    return evidence, boundaries, selection


def test_sealed_initial_sync_receipt_returns_immutable_facts_without_writing(tmp_path: Path) -> None:
    """Break caught: a phase gate could repair or trust mutable receipt evidence."""

    reader, boundaries, selection, result_id = _sealed_receipt(tmp_path)
    before = reader.inventory()

    facts = contract.validate_initial_sync_receipt_from_sealed_artifacts(
        reader,
        execution_id="main",
        stage_boundaries=boundaries,
        selection=selection,
    )

    assert facts.result_id == result_id
    assert facts.corpus_revision == reader.obj("verify-result", "command_result")["data"]["corpus_revision"]
    assert facts.effect_digest == reader.obj("consume-result", "command_result")["data"]["effect_digest"]
    assert facts.selection == selection
    assert facts.selection.command_observation_ids == _COMMAND_IDS
    assert facts.selection.stream_artifact_id == "verify-stream"
    assert facts.selection.durable_artifact_id == "durable-durable"
    assert list(facts.event_counts) == sorted(facts.event_counts)
    with pytest.raises(TypeError):
        facts.event_counts["new"] = 1  # type: ignore[index]
    assert reader.write_attempts == 0
    assert reader.inventory() == before


def test_sealed_initial_sync_receipt_uses_explicit_stream_and_durable_selection(
    tmp_path: Path,
) -> None:
    """Break caught: the helper could infer artifact names from command IDs."""

    builder, boundaries, _, result_id = _receipt_builder(tmp_path)
    _rename_artifact(builder, "verify-stream", "sealed-stream")
    _rename_artifact(builder, "durable-durable", "sealed-durable")
    reader = _WriteForbiddenSealedReader(builder)
    selection = contract.InitialSyncReceiptSelection(
        verify_command_id="verify",
        consume_command_id="consume",
        durable_command_id="durable",
        acknowledge_command_id="acknowledge",
        stream_artifact_id="sealed-stream",
        durable_artifact_id="sealed-durable",
    )

    facts = contract.validate_initial_sync_receipt_from_sealed_artifacts(
        reader,
        execution_id="main",
        stage_boundaries=boundaries,
        selection=selection,
    )

    assert facts.result_id == result_id
    assert reader.write_attempts == 0


def test_initial_sync_receipt_selection_rejects_an_ambiguous_id_reuse() -> None:
    """Break caught: one sealed artifact could masquerade as two receipt roles."""

    with pytest.raises(contract.EventLogContractError, match="ambiguous"):
        contract.InitialSyncReceiptSelection(
            verify_command_id="verify",
            consume_command_id="consume",
            durable_command_id="durable",
            acknowledge_command_id="acknowledge",
            stream_artifact_id="verify",
            durable_artifact_id="sealed-durable",
        )


def test_initial_sync_receipt_facts_sort_counts_when_constructed_directly() -> None:
    """Break caught: a caller could bypass the helper's sorted-count input."""

    selection = contract.InitialSyncReceiptSelection(
        verify_command_id="verify",
        consume_command_id="consume",
        durable_command_id="durable",
        acknowledge_command_id="acknowledge",
        stream_artifact_id="stream",
        durable_artifact_id="receipt",
    )

    facts = contract.InitialSyncReceiptFacts(
        result_id="sync_" + "a" * 64,
        corpus_revision="b" * 64,
        event_counts={"zeta": 1, "alpha": 2},
        effect_digest="c" * 64,
        selection=selection,
    )

    assert list(facts.event_counts) == ["alpha", "zeta"]
    with pytest.raises(TypeError):
        facts.event_counts["later"] = 3  # type: ignore[index]


def test_sealed_initial_sync_receipt_normalizes_expected_reader_faults_only(tmp_path: Path) -> None:
    """Break caught: an unreadable sealed artifact could escape the contract type."""

    reader, boundaries, selection, _ = _sealed_receipt(tmp_path)
    reader.fail_read_for = "verify-result"

    with pytest.raises(contract.EventLogContractError, match="sealed evidence"):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    reader.fail_read_for = None
    reader.raise_unexpected_read_error = True
    with pytest.raises(AssertionError, match="unexpected reader programming failure"):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    assert reader.write_attempts == 0


def test_sealed_initial_sync_receipt_rejects_an_extra_phase_command(tmp_path: Path) -> None:
    """Break caught: an unselected command could be hidden beside the receipt."""

    builder, boundaries, selection, _ = _receipt_builder(tmp_path)
    builder.command("unexpected", ["./brain", "--json", "validate"], {
        "ok": True,
        "command": "validate",
        "data": {"reports": [{"checks": ["source"], "issues": [], "corpus_revision": "a" * 64}]},
        "warnings": [],
        "errors": [],
    })
    reader = _WriteForbiddenSealedReader(builder)

    with pytest.raises(contract.EventLogContractError):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    assert reader.write_attempts == 0


def test_sealed_initial_sync_receipt_rejects_real_consume_and_durable_handoff_delivery(
    tmp_path: Path,
) -> None:
    """Break caught: phase one could accept a real handoff delivery lifecycle."""

    builder, boundaries, selection = _handoff_receipt_builder(tmp_path)
    reader = _WriteForbiddenSealedReader(builder)

    with pytest.raises(contract.EventLogContractError, match="delivery is unavailable"):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    assert reader.write_attempts == 0


def test_sealed_initial_sync_receipt_rejects_a_valid_structured_handoff_stream(
    tmp_path: Path,
) -> None:
    """Break caught: an actual typed handoff effect could pass without delivery prose."""

    builder, boundaries, selection = _handoff_receipt_builder(tmp_path)
    for command_id in ("consume", "durable"):
        result = builder.obj(command_id + "-result", "command_result")
        result["data"]["handoff_delivery"] = None
        _replace_command_result(builder, command_id, result)
    durable = builder.obj("receipt", "consumption_receipt")
    durable["handoff_delivery"] = None
    builder.add("receipt", "consumption_receipt", durable)
    builder.artifacts.pop("delivery")
    builder.entries.pop("delivery")
    reader = _WriteForbiddenSealedReader(builder)

    with pytest.raises(contract.EventLogContractError, match="handoff is unavailable"):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    assert reader.write_attempts == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "inverted_boundaries",
        "collapsed_stage_interval",
        "missing_consumption_state",
        "changed_stream_digest",
        "attached_delivery",
        "changed_durable_replay_identity",
        "mis_typed_selected_artifact",
    ],
)
def test_sealed_initial_sync_receipt_rejects_order_state_digest_and_delivery_faults(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Break caught: a malformed lifecycle could unlock phase two as a receipt."""

    builder, boundaries, selection, _ = _receipt_builder(tmp_path)
    if mutation == "inverted_boundaries":
        boundaries = tuple(reversed(boundaries))
    elif mutation == "collapsed_stage_interval":
        boundaries = (
            contract.InitialSyncReceiptStageBoundary(stage="verified", trace_sequence=2),
            *boundaries[1:],
        )
    elif mutation == "missing_consumption_state":
        observation = builder.obj("consume", "command_observation")
        observation["source_state_id"] = None
        builder.add("consume", "command_observation", observation)
    elif mutation == "changed_stream_digest":
        builder.add("verify-stream", "sync_stream", b"{}\n")
    elif mutation == "attached_delivery":
        builder.add("consume-delivery", "delivery", _encoded({"unexpected": True}))
    elif mutation == "changed_durable_replay_identity":
        result = builder.obj("durable-result", "command_result")
        result["data"]["effect_digest"] = "0" * 64
        builder.add("durable-result", "command_result", result)
        observation = builder.obj("durable", "command_observation")
        observation["result_sha256"] = hashlib.sha256(_encoded(result)).hexdigest()
        builder.add("durable", "command_observation", observation)
    elif mutation == "mis_typed_selected_artifact":
        builder.add("wrong-type", "delivery", _encoded({"unexpected": True}))
        selection = contract.InitialSyncReceiptSelection(
            verify_command_id="verify",
            consume_command_id="consume",
            durable_command_id="durable",
            acknowledge_command_id="acknowledge",
            stream_artifact_id="wrong-type",
            durable_artifact_id="durable-durable",
        )
    else:  # pragma: no cover - parametrization is closed above.
        raise AssertionError(mutation)
    reader = _WriteForbiddenSealedReader(builder)

    with pytest.raises(contract.EventLogContractError):
        contract.validate_initial_sync_receipt_from_sealed_artifacts(
            reader,
            execution_id="main",
            stage_boundaries=boundaries,
            selection=selection,
        )
    assert reader.write_attempts == 0
