"""Behavioral tests for the test-only controlled ``./brain`` shim.

The shim is deliberately tested against generated fixture workspaces rather
than mocked command results: its job is to route real CLI work through
controlled services while exposing no general network capability.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.evals import brain_shim
from tests.evals.fixture_services import FixtureServiceError, StaticFixtureTransport


ROOT = Path(__file__).resolve().parents[2]


def _fixture_workspace(tmp_path: Path, scenario_id: str) -> Path:
    workspace = tmp_path / "workspace"
    source = ROOT / "tests/evals/fixtures" / scenario_id / "repo"
    shutil.copytree(source, workspace)
    # Generated overlays deliberately contain only archive state.  The runner
    # supplies the normal repository launcher/root markers around that state.
    for name in ("AGENTS.md", "pyproject.toml"):
        shutil.copy2(ROOT / name, workspace / name)
    return workspace


def _write_web_control(tmp_path: Path, workspace: Path, *, phase: str) -> tuple[Path, dict[str, str]]:
    """Create runner-owned fixture bytes outside the evaluated workspace."""

    control = tmp_path / "control"
    control.mkdir()
    assets = ROOT / "tests/evals/fixtures/web-approval-and-capture"
    copied: dict[str, Path] = {}
    for name in ("static-shell.html", "rendered-dom.html"):
        target = control / name
        shutil.copy2(assets / name, target)
        copied[name] = target
    fixture_hash = json.loads((assets / "fixture-manifest.json").read_text())["tree_sha256"]
    descriptor = {
        "fixture_id": "web-approval-and-capture",
        "fixture_sha256": fixture_hash,
        "static_sha256": hashlib.sha256(copied["static-shell.html"].read_bytes()).hexdigest(),
        "rendered_sha256": hashlib.sha256(copied["rendered-dom.html"].read_bytes()).hexdigest(),
        "unused_sha256": hashlib.sha256((assets / "unused-candidate.html").read_bytes()).hexdigest(),
        "final_url": "https://example.test/standard",
        "media_type": "text/html",
        "retrieved_at": "2026-09-04T12:00:00Z",
        "redirect_urls": [],
    }
    (control / "fixture-descriptor.json").write_text(
        json.dumps(descriptor, sort_keys=True), encoding="utf-8"
    )
    approval = {
        "event_id": "approval-standard",
        "scope": "one controlled standard fixture",
        "note": "Approve static and faithful rendered fixture capture",
    }
    config = {
        "schema_version": 1,
        "workspace": str(workspace.resolve()),
        "scenario_id": "web-approval-and-capture",
        "phase": phase,
        "approval": approval,
        "fixture": {
            "descriptor_path": "fixture-descriptor.json",
            "static_shell_path": "static-shell.html",
            "rendered_dom_path": "rendered-dom.html",
        },
    }
    (control / "brain-shim.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8"
    )
    return control, approval


def _invoke(workspace: Path, control: Path, *argv: str) -> tuple[int, dict[str, object], str]:
    code, raw, stderr = _invoke_raw(workspace, control, *argv)
    return code, json.loads(raw), stderr


def _invoke_raw(workspace: Path, control: Path, *argv: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = brain_shim.main(
        list(argv),
        cwd=workspace,
        stdout=stdout,
        stderr=stderr,
        environ={brain_shim.CONTROL_ENV: str(control)},
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _write_plain_control(tmp_path: Path, workspace: Path, scenario_id: str) -> Path:
    control = tmp_path / "control"
    control.mkdir()
    (control / "brain-shim.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace.resolve()),
                "scenario_id": scenario_id,
                "phase": "main",
                "approval": None,
                "fixture": None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return control


def _consume_and_ack(workspace: Path, control: Path, result_id: str) -> dict[str, object]:
    code, consumed, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        result_id,
    )
    assert code == 0, stderr
    code, acknowledged, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )
    assert code == 0, stderr
    assert acknowledged["ok"] is True
    return consumed


def test_static_fixture_runs_real_snapshot_and_emits_rendered_handoff(tmp_path: Path) -> None:
    """A regression to catch replacing the fixture route with a shaped result."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")

    code, payload, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )

    assert code == 0, stderr
    assert payload["command"] == "eval mock-web-capture"
    product = payload["data"]["product_result"]
    assert product["command"] == "source snapshot-url"
    assert product["data"]["snapshot"]["active_representation"] is None

    reference = product["data"]["result_manifest"]
    consume_code, consumed, consume_stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        reference["result_id"],
    )
    assert consume_code == 0, consume_stderr
    delivery = consumed["data"]["handoff_delivery"]
    items = json.loads((workspace / delivery["path"]).read_text())["items"]
    assert [item["kind"] for item in items] == ["rendered_web_capture"]


def test_binary_fixture_uses_real_unavailable_pdf_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-visible converter must not change the fixture's PDF decision."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    hostile_bin = tmp_path / "host-bin"
    hostile_bin.mkdir()
    hostile = hostile_bin / "pdftotext"
    hostile.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    hostile.chmod(0o755)
    monkeypatch.setenv("PATH", str(hostile_bin))

    code, payload, stderr = _invoke(workspace, control, "--json", "sync")

    assert code == 1, stderr
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "source_coverage_gaps"
    reference = payload["data"]["result_manifest"]
    consumed = _consume_and_ack(workspace, control, reference["result_id"])
    delivery = consumed["data"]["handoff_delivery"]
    items = json.loads((workspace / delivery["path"]).read_text())["items"]
    assert len(items) == 1 and items[0]["kind"] == "extraction"
    assert items[0]["prerequisite_digest"] == hashlib.sha256(
        b"converter-v1\0unavailable\0pdf"
    ).hexdigest()
    record = next(
        json.loads(path.read_text())
        for path in (workspace / "sources/ledger").glob("src_*.json")
        if path.read_text().find("quarterly.pdf") >= 0
    )
    assert record["state"] == "needs_agent"
    assert record["active_derivation_id"] is None
    assert not any("quarterly.pdf" in path.as_posix() for path in (workspace / "sources/extracted").rglob("*"))


def test_static_fixture_transport_refuses_symlinked_workspace_staging(tmp_path: Path) -> None:
    """Fixture bytes cannot escape through a pre-created workspace symlink."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".brain").symlink_to(outside, target_is_directory=True)
    transport = StaticFixtureTransport(
        "https://example.test/standard",
        b"<html><script>fixture</script></html>",
        datetime(2026, 9, 4, tzinfo=timezone.utc),
        "text/html",
    )
    with pytest.raises(FixtureServiceError):
        transport.capture(
            "https://example.test/standard",
            paths=SimpleNamespace(root=workspace),
            timeout_seconds=1,
            max_output_bytes=1_000,
            now=datetime(2026, 9, 4, tzinfo=timezone.utc),
        )
    assert not (outside / "web-staging" / "fixture-static.html").exists()


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("status",),
        ("sync",),
        ("--json", "--json", "status"),
        ("--json", "status", "--json"),
    ],
)
def test_malformed_json_invocation_is_recorded_under_trusted_control(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    """A rejected invocation remains visible to the global command audit."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    code, raw, stderr = _invoke_raw(workspace, control, *argv)

    assert code == 2
    assert "requires exactly one leading --json" in stderr
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "eval_json_required"
    command_log = [
        json.loads(line)
        for line in (control / "brain-shim-command-log.jsonl").read_text().splitlines()
    ]
    assert command_log[0]["argv"] == ["./brain", *argv]
    assert command_log[0]["exit_code"] == 2
    assert command_log[0]["result_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    capture = json.loads((control / "brain-shim-capture-index.jsonl").read_text())
    assert capture["argv"] == ["./brain", *argv]
    assert (control / capture["result_path"]).read_text() == raw
    trace = [
        json.loads(line) for line in (control / "brain-shim-trace.jsonl").read_text().splitlines()
    ]
    assert [row["kind"] for row in trace] == ["command_start", "command_end"]


@pytest.mark.parametrize("missing", [False, True])
def test_malformed_json_without_trusted_control_is_not_recorded(
    tmp_path: Path, missing: bool
) -> None:
    """The shim does not invent a recorder destination when config is invalid."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = tmp_path / "invalid-control"
    if not missing:
        control.mkdir()
        (control / "brain-shim.json").write_text("not JSON", encoding="utf-8")
    code, raw, stderr = _invoke_raw(workspace, control, "status")

    assert code == 2
    assert raw == ""
    assert "requires exactly one leading --json" in stderr
    assert not (control / "brain-shim-command-log.jsonl").exists()
    assert not (control / "brain-shim-capture-index.jsonl").exists()
    assert not (control / "brain-shim-trace.jsonl").exists()


def test_control_root_containing_workspace_is_rejected(tmp_path: Path) -> None:
    """A host control directory cannot be an ancestor of the evaluated repo."""

    control = tmp_path / "control"
    control.mkdir()
    workspace = _fixture_workspace(control, "new-binary-before-question")
    (control / "brain-shim.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workspace": str(workspace.resolve()),
                "scenario_id": "new-binary-before-question",
                "phase": "main",
                "approval": None,
                "fixture": None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(brain_shim.ShimError, match="outside the fixture workspace"):
        brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})


def test_recorder_rejects_symlinked_retained_source_root(tmp_path: Path) -> None:
    """A source-state inventory cannot silently skip a symlinked root."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    outside = tmp_path / "outside"
    outside.mkdir()
    extracted = workspace / "sources/extracted"
    shutil.rmtree(extracted)
    extracted.symlink_to(outside, target_is_directory=True)
    config = brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})
    recorder = brain_shim.Recorder(config)
    recorder.start()
    with pytest.raises(brain_shim.ShimError, match="unsafe retained source root"):
        recorder._capture_state({"command": "sync", "data": {}}, ("sync",))


def _raw_writer_identity(info) -> tuple[int, int, int, int, int, int, int, int]:
    """Mirror the private receipt's explicit fd-identity field order."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def test_raw_write_receipt_hook_is_default_off_and_leaves_public_records_unchanged(
    tmp_path: Path,
) -> None:
    """The opt-in host hook must not alter any 7b-visible recording bytes."""

    moment = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def run(
        root: Path,
        *,
        raw_write_receipt_sink=None,
    ) -> tuple[Path, int, str, str, list[object]]:
        root.mkdir()
        workspace = _fixture_workspace(root, "new-binary-before-question")
        control = _write_plain_control(root, workspace, "new-binary-before-question")
        observed: list[object] = []

        def recorder_factory(config: brain_shim.ControlConfig) -> brain_shim.Recorder:
            return brain_shim.Recorder(
                config,
                now=lambda: moment,
                raw_write_receipt_sink=(
                    observed.append if raw_write_receipt_sink is not None else None
                ),
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        code = brain_shim.main(
            ["--json", "sync"],
            cwd=workspace,
            stdout=stdout,
            stderr=stderr,
            environ={brain_shim.CONTROL_ENV: str(control)},
            recorder_factory=recorder_factory,
        )
        return control, code, stdout.getvalue(), stderr.getvalue(), observed

    disabled = run(tmp_path / "disabled")
    enabled = run(tmp_path / "enabled", raw_write_receipt_sink=object())
    disabled_control, disabled_code, disabled_stdout, disabled_stderr, disabled_receipts = disabled
    enabled_control, enabled_code, enabled_stdout, enabled_stderr, enabled_receipts = enabled

    assert disabled_code == enabled_code == 1
    assert disabled_stderr == enabled_stderr == ""
    assert disabled_receipts == []
    assert enabled_receipts

    # The product assigns a fresh source-result timestamp/ID per isolated
    # workspace, so compare its externally visible shape and outcome rather
    # than treating two genuine runs as byte-identical.
    disabled_payload = json.loads(disabled_stdout)
    enabled_payload = json.loads(enabled_stdout)
    assert disabled_payload["command"] == enabled_payload["command"] == "sync"
    assert disabled_payload["ok"] is enabled_payload["ok"] is False
    assert [item["code"] for item in disabled_payload["errors"]] == [
        item["code"] for item in enabled_payload["errors"]
    ]

    # These are the externally consumed command/run event records.  The
    # receipt handoff is private and must neither add a file nor an output
    # field here, whether or not a host receiver is installed.
    public_names = (
        "brain-shim-command-log.jsonl",
        "brain-shim-capture-index.jsonl",
        "brain-shim-trace.jsonl",
    )
    for name in public_names:
        assert b"raw_write" not in (disabled_control / name).read_bytes()
        assert b"raw_write" not in (enabled_control / name).read_bytes()
    assert (disabled_control / "brain-shim-trace.jsonl").read_bytes() == (
        enabled_control / "brain-shim-trace.jsonl"
    ).read_bytes()
    disabled_log = json.loads((disabled_control / "brain-shim-command-log.jsonl").read_text())
    enabled_log = json.loads((enabled_control / "brain-shim-command-log.jsonl").read_text())
    assert set(disabled_log) == set(enabled_log) == {
        "argv",
        "exit_code",
        "result_sha256",
        "timestamp",
    }
    assert {
        key: value for key, value in disabled_log.items() if key != "result_sha256"
    } == {
        key: value for key, value in enabled_log.items() if key != "result_sha256"
    }
    disabled_index = json.loads((disabled_control / "brain-shim-capture-index.jsonl").read_text())
    enabled_index = json.loads((enabled_control / "brain-shim-capture-index.jsonl").read_text())
    assert set(disabled_index) == set(enabled_index) == {
        "command_id",
        "argv",
        "argv_sha256",
        "exit_code",
        "result_path",
        "result_sha256",
        "source_state_path",
        "receipt_artifacts_path",
        "start_sequence",
        "end_sequence",
        "phase",
        "timestamp",
    }
    assert {
        key: value for key, value in disabled_index.items() if key != "result_sha256"
    } == {
        key: value for key, value in enabled_index.items() if key != "result_sha256"
    }
    # Captured binary leaf names are content-addressed from genuine source
    # results and therefore differ across independent runs.  Root-level
    # control artifacts are stable: an enabled receiver must not create a
    # serialized receipt sidecar or alter the normal event-file set.
    assert {
        path.name for path in disabled_control.iterdir() if path.is_file()
    } == {
        path.name for path in enabled_control.iterdir() if path.is_file()
    }


def test_raw_write_receipts_cover_each_recorder_producer_with_live_fd_identity(
    tmp_path: Path,
) -> None:
    """Every raw producer emits a receipt before its fd identity can be lost."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    observed: list[tuple[object, bytes, tuple[int, int, int, int, int, int, int, int]]] = []

    def receive(receipt: object) -> None:
        relative_path = receipt.relative_path
        path = control / relative_path
        observed.append((receipt, path.read_bytes(), _raw_writer_identity(path.stat())))

    def recorder_factory(config: brain_shim.ControlConfig) -> brain_shim.Recorder:
        return brain_shim.Recorder(
            config,
            now=lambda: datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
            raw_write_receipt_sink=receive,
        )

    stdout, stderr = io.StringIO(), io.StringIO()
    code = brain_shim.main(
        ["--json", "sync"],
        cwd=workspace,
        stdout=stdout,
        stderr=stderr,
        environ={brain_shim.CONTROL_ENV: str(control)},
        recorder_factory=recorder_factory,
    )

    assert code == 1
    assert stderr.getvalue() == ""
    assert observed
    append_receipts = [
        item for item in observed if type(item[0]) is brain_shim._RawAppendReceipt
    ]
    write_receipts = [
        item for item in observed if type(item[0]) is brain_shim._RawWriteReceipt
    ]
    assert len(append_receipts) + len(write_receipts) == len(observed)
    assert {receipt.relative_path for receipt, _raw, _identity in append_receipts} == {
        "brain-shim-trace.jsonl",
        "brain-shim-capture-index.jsonl",
        "brain-shim-command-log.jsonl",
    }
    assert any(receipt.relative_path.endswith(".bin") for receipt, _raw, _identity in write_receipts)
    assert any(
        receipt.relative_path.endswith("/source-state.json")
        for receipt, _raw, _identity in write_receipts
    )
    assert any(
        receipt.relative_path.endswith("/receipt-artifacts.json")
        for receipt, _raw, _identity in write_receipts
    )
    assert any(
        receipt.relative_path.endswith("/result.json")
        for receipt, _raw, _identity in write_receipts
    )

    for receipt, raw, identity in write_receipts:
        assert "raw" not in vars(receipt)
        assert receipt.sha256 == hashlib.sha256(raw).hexdigest()
        assert receipt.byte_count == len(raw)
        assert receipt.identity == identity
    for receipt, raw, identity in append_receipts:
        assert "raw" not in vars(receipt)
        assert receipt.completed_sha256 == hashlib.sha256(raw).hexdigest()
        assert receipt.completed_bytes == len(raw)
        assert receipt.completed_identity == identity


def test_raw_write_receipt_rejects_same_byte_path_replacement_before_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor-time receipt cannot bless a same-byte replacement race."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    config = brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})
    received: list[object] = []
    recorder = brain_shim.Recorder(config, raw_write_receipt_sink=received.append)
    target = control / "same-byte-race.bin"
    replacement = control / "same-byte-race-replacement.bin"
    body = b"same bytes must not establish producer identity\n"
    original_fsync = brain_shim.os.fsync
    swapped = False

    def swap_after_fsync(descriptor: int) -> None:
        nonlocal swapped

        original_fsync(descriptor)
        if not swapped:
            replacement.write_bytes(body)
            replacement.replace(target)
            swapped = True

    monkeypatch.setattr(brain_shim.os, "fsync", swap_after_fsync)
    with pytest.raises(brain_shim.ShimError) as raised:
        recorder._write_bytes(target, body, exclusive=True, mode=0o600)

    assert raised.value.code == "eval_capture_invalid"
    assert swapped
    assert received == []
    assert target.read_bytes() == body


def test_armed_raw_one_shot_write_requires_a_fresh_target(
    tmp_path: Path,
) -> None:
    """The private receipt route cannot adopt a planted capture inode.

    The normal shim deliberately retains its historic overwrite behavior.
    Once the private receipt sink is armed, however, every one-shot capture
    is a fresh command-scoped producer and must be created exclusively.
    """

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    config = brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})
    body = b"private one-shot capture\n"

    legacy_target = control / "legacy-overwrite.bin"
    legacy_target.write_bytes(b"legacy bytes\n")
    brain_shim.Recorder(config)._write_bytes(legacy_target, body)
    assert legacy_target.read_bytes() == body

    received: list[object] = []
    # The raw receipt hook remains independently useful and compatible.  The
    # stricter ownership rule starts only when phase two also provides the
    # raw-journal predecessor preflight.
    receipt_only = brain_shim.Recorder(config, raw_write_receipt_sink=received.append)
    receipt_only_target = control / "receipt-only-overwrite.bin"
    receipt_only_target.write_bytes(b"receipt-only old bytes\n")
    receipt_only._write_bytes(receipt_only_target, body)
    assert receipt_only_target.read_bytes() == body
    assert len(received) == 1
    received.clear()

    def phase_two_preflight(_relative_path: str) -> brain_shim._RawAppendPreflight:
        pytest.fail("one-shot write unexpectedly requested an append preflight")

    root_fd = os.open(
        control,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        armed = brain_shim.Recorder(
            config,
            raw_write_receipt_sink=received.append,
            raw_append_preflight=phase_two_preflight,
            raw_write_root_fd=root_fd,
        )
        planted_target = control / "planted-one-shot.bin"
        planted_target.write_bytes(b"foreign bytes\n")
        with pytest.raises(brain_shim.ShimError, match="raw producer write"):
            armed._write_bytes(planted_target, body)
        assert planted_target.read_bytes() == b"foreign bytes\n"
        assert received == []

        capture_root = control / "brain-shim-captures"
        capture_root.mkdir(mode=0o700)
        outside = tmp_path / "outside-captures"
        outside.mkdir(mode=0o700)
        redirected = capture_root / "shim-command-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        redirected.symlink_to(outside, target_is_directory=True)
        with pytest.raises(brain_shim.ShimError, match="capture parent"):
            armed._write_bytes(redirected / "result.json", body)
        assert not (outside / "result.json").exists()
        assert received == []

        fresh_target = capture_root / "shim-command-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" / "result.json"
        armed._write_bytes(fresh_target, body)
        assert fresh_target.read_bytes() == body
        assert len(received) == 1
        receipt = received[0]
        assert type(receipt) is brain_shim._RawWriteReceipt
        assert receipt.relative_path == fresh_target.relative_to(control).as_posix()
        assert receipt.sha256 == hashlib.sha256(body).hexdigest()
        assert receipt.byte_count == len(body)
    finally:
        os.close(root_fd)


def test_raw_append_receipts_form_an_unambiguous_prefix_and_row_chain(tmp_path: Path) -> None:
    """Repeated identical JSON rows remain ordered by byte and row predecessors."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    config = brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})
    receipts: list[object] = []
    recorder = brain_shim.Recorder(config, raw_write_receipt_sink=receipts.append)

    recorder._append("raw-receipt-chain.jsonl", {"event": "same-row"})
    recorder._append("raw-receipt-chain.jsonl", {"event": "same-row"})

    chain = [
        receipt
        for receipt in receipts
        if receipt.relative_path == "raw-receipt-chain.jsonl"
    ]
    assert len(chain) == 2
    first, second = chain
    assert type(first) is brain_shim._RawAppendReceipt
    assert type(second) is brain_shim._RawAppendReceipt
    assert first.prior_sha256 == hashlib.sha256(b"").hexdigest()
    assert first.prior_bytes == 0
    assert first.prior_identity[4] == 1
    assert first.prior_identity[5] == 0
    assert first.prior_rows == 0
    assert first.completed_bytes == first.appended_bytes
    assert first.completed_sha256 == first.appended_sha256
    assert first.completed_rows == 1
    assert second.prior_sha256 == first.completed_sha256
    assert second.prior_bytes == first.completed_bytes
    assert second.prior_identity == first.completed_identity
    assert second.prior_rows == first.completed_rows
    assert second.appended_sha256 == first.appended_sha256
    assert second.completed_bytes == first.completed_bytes + second.appended_bytes
    assert second.completed_rows == first.completed_rows + 1
    completed = (control / "raw-receipt-chain.jsonl").read_bytes()
    assert hashlib.sha256(completed).hexdigest() == second.completed_sha256
    assert len(completed) == second.completed_bytes
    assert hashlib.sha256(completed[: first.completed_bytes]).hexdigest() == first.completed_sha256
    assert hashlib.sha256(completed[first.completed_bytes :]).hexdigest() == second.appended_sha256
    assert len(completed.splitlines()) == second.completed_rows


def test_raw_append_preflight_preserves_legacy_mode_and_rejects_unknown_predecessors(
    tmp_path: Path,
) -> None:
    """An armed phase-two journal cannot adopt an empty or changed predecessor."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    config = brain_shim.load_control(workspace, {brain_shim.CONTROL_ENV: str(control)})

    # The extra preflight is default-off: an ordinary recorder retains its
    # historical append behavior, including appending to an already-created
    # empty journal.
    legacy_target = control / "legacy-preflight-off.jsonl"
    legacy_target.write_bytes(b"")
    brain_shim.Recorder(config)._append("legacy-preflight-off.jsonl", {"event": "legacy"})
    assert legacy_target.read_bytes() == b'{"event":"legacy"}\n'

    target = control / "armed-preflight.jsonl"
    received: list[object] = []
    empty_sha256 = hashlib.sha256(b"").hexdigest()

    def absent_preflight(relative_path: str) -> brain_shim._RawAppendPreflight:
        assert relative_path == "armed-preflight.jsonl"
        return brain_shim._RawAppendPreflight(True, empty_sha256, 0, None, 0)

    # Parent-observed absence is stronger than an empty prefix: an attacker
    # cannot plant an empty pathname between the parent snapshot and the
    # child's first append, because the armed append must use O_EXCL.
    target.write_bytes(b"")
    absent = brain_shim.Recorder(
        config,
        raw_write_receipt_sink=received.append,
        raw_append_preflight=absent_preflight,
    )
    with pytest.raises(brain_shim.ShimError, match="raw append"):
        absent._append("armed-preflight.jsonl", {"event": "blocked"})
    assert target.read_bytes() == b""
    assert received == []

    seed = b'{"seed":true}\n'
    target.write_bytes(seed)
    identity = _raw_writer_identity(target.stat())

    def mismatched_preflight(_relative_path: str) -> brain_shim._RawAppendPreflight:
        return brain_shim._RawAppendPreflight(
            False,
            hashlib.sha256(b"x" * len(seed)).hexdigest(),
            len(seed),
            identity,
            1,
        )

    mismatched = brain_shim.Recorder(
        config,
        raw_write_receipt_sink=received.append,
        raw_append_preflight=mismatched_preflight,
    )
    with pytest.raises(brain_shim.ShimError, match="predecessor changed"):
        mismatched._append("armed-preflight.jsonl", {"event": "blocked"})
    assert target.read_bytes() == seed
    assert received == []

    def matching_preflight(_relative_path: str) -> brain_shim._RawAppendPreflight:
        return brain_shim._RawAppendPreflight(
            False,
            hashlib.sha256(seed).hexdigest(),
            len(seed),
            _raw_writer_identity(target.stat()),
            1,
        )

    matching = brain_shim.Recorder(
        config,
        raw_write_receipt_sink=received.append,
        raw_append_preflight=matching_preflight,
    )
    matching._append("armed-preflight.jsonl", {"event": "accepted"})
    assert len(received) == 1
    receipt = received[0]
    assert type(receipt) is brain_shim._RawAppendReceipt
    assert receipt.prior_sha256 == hashlib.sha256(seed).hexdigest()
    assert receipt.prior_identity == identity
    assert target.read_bytes().startswith(seed)


def test_raw_write_receipt_sink_failure_fails_closed_before_public_output(
    tmp_path: Path,
) -> None:
    """A receiver failure is a denied capture, never a silently unreceipted run."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    received: list[object] = []

    def fail_closed(receipt: object) -> None:
        received.append(receipt)
        raise RuntimeError("host receipt receiver unavailable")

    def recorder_factory(config: brain_shim.ControlConfig) -> brain_shim.Recorder:
        return brain_shim.Recorder(config, raw_write_receipt_sink=fail_closed)

    stdout, stderr = io.StringIO(), io.StringIO()
    with pytest.raises(brain_shim.ShimError) as raised:
        brain_shim.main(
            ["--json", "sync"],
            cwd=workspace,
            stdout=stdout,
            stderr=stderr,
            environ={brain_shim.CONTROL_ENV: str(control)},
            recorder_factory=recorder_factory,
        )

    assert raised.value.code == "eval_capture_invalid"
    assert received
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
    # The first private write can exist, but no completed command/event record
    # may claim a run whose raw receipt did not reach the host receiver.
    assert not (control / "brain-shim-capture-index.jsonl").exists()
    assert not (control / "brain-shim-command-log.jsonl").exists()


def test_direct_url_and_unrendered_snapshot_are_rejected_before_transport(tmp_path: Path) -> None:
    """No ordinary caller argument may reach the shim's forbidden transport."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "snapshot-url",
        "--url",
        "https://example.test/evil",
        "--description",
        "evil",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_raw_url_denied"
    assert not (workspace / ".brain/web-staging").exists()

    # ``argparse`` also accepts the equals spelling.  Pairing it with rendered
    # metadata used to evade the simple token check and reach product mutation
    # code, despite still being a raw URL capability.
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "snapshot-url",
        "--url=https://example.test/evil",
        "--description",
        "evil",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
        "--rendered-staging-path",
        ".brain/web-staging/escaped/rendered.html",
        "--retrieved-at",
        "2026-09-04T12:00:00Z",
        "--final-url",
        "https://example.test/evil",
        "--detected-media-type",
        "text/html",
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_raw_url_denied"

    code, initial, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    source_id = initial["data"]["product_result"]["data"]["snapshot"]["source_id"]
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "snapshot-url",
        "--source-id",
        source_id,
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_raw_url_denied"


def test_wrong_web_phase_rejects_before_opening_fixture_assets(tmp_path: Path) -> None:
    """A rejected phase cannot be used to probe runner-owned fixture bytes."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approval")
    # If the shim eagerly opened this asset, its digest mismatch would become
    # observable before the phase gate.  The phase must win first.
    (control / "static-shell.html").write_bytes(b"tampered")
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_phase_denied"


def test_rendered_wrong_handoff_rejects_before_opening_fixture_assets(tmp_path: Path) -> None:
    """A syntactically valid but unknown handoff cannot probe fixture DOM bytes."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, _approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    (control / "rendered-dom.html").write_bytes(b"tampered")
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.rendered",
        "--handoff-id",
        "hnd_" + "0" * 64,
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"


@pytest.mark.parametrize(
    "extra",
    [
        ("--url", "https://example.test/evil"),
        ("--redirect-url", "https://example.test/redirect"),
        ("--descriptor-path", "fixture.json"),
        ("--output-path", "anywhere"),
    ],
)
def test_fixture_eval_extra_capability_fields_fail_before_capture(tmp_path: Path, extra: tuple[str, str]) -> None:
    """The fixture form is closed rather than a parameterized web API."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
        *extra,
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"
    assert not (workspace / ".brain/web-staging").exists()


def test_rendered_fixture_requires_consumed_handoff_and_uses_normal_direct_activation(tmp_path: Path) -> None:
    """A durable rendered handoff is the only route to fixture DOM bytes."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, initial, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    reference = initial["data"]["product_result"]["data"]["result_manifest"]
    unconsumed_handoff = initial["data"]["product_result"]["data"]["handoffs"][0]["handoff_id"]
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.rendered",
        "--handoff-id",
        unconsumed_handoff,
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"
    consumed = _consume_and_ack(workspace, control, reference["result_id"])
    delivery = consumed["data"]["handoff_delivery"]
    item = json.loads((workspace / delivery["path"]).read_text())["items"][0]

    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.rendered",
        "--handoff-id",
        "hnd_" + "0" * 64,
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"
    assert not (workspace / ".brain/web-staging" / ("hnd_" + "0" * 64)).exists()

    code, staged, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.rendered",
        "--handoff-id",
        item["handoff_id"],
    )
    assert code == 0, stderr
    assert staged["data"] == {
        "fixture_id": "web-approval-and-capture.rendered",
        "handoff_id": item["handoff_id"],
        "path": f".brain/web-staging/{item['handoff_id']}/rendered.html",
        "sha256": hashlib.sha256(
            (ROOT / "tests/evals/fixtures/web-approval-and-capture/rendered-dom.html").read_bytes()
        ).hexdigest(),
        "bytes": len((ROOT / "tests/evals/fixtures/web-approval-and-capture/rendered-dom.html").read_bytes()),
    }
    code, activated, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "snapshot-url",
        "--source-id",
        item["source_id"],
        "--rendered-staging-path",
        staged["data"]["path"],
        "--handoff-id",
        item["handoff_id"],
        "--retrieved-at",
        "2026-09-04T12:00:00Z",
        "--final-url",
        "https://example.test/standard",
        "--detected-media-type",
        "text/html",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    assert activated["data"]["snapshot"]["active_representation"] is not None


def test_rendered_delivery_authorization_reads_pinned_receipt_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery authorization must not reopen mutable workspace receipt paths."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, initial, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    consumed = _consume_and_ack(
        workspace,
        control,
        initial["data"]["product_result"]["data"]["result_manifest"]["result_id"],
    )
    handoff_id = json.loads(
        (workspace / consumed["data"]["handoff_delivery"]["path"]).read_text()
    )["items"][0]["handoff_id"]
    handoff = brain_shim.agent_handoff.load_handoff_item(
        brain_shim.RepoPaths.discover(workspace), handoff_id
    )
    original_read_bytes = Path.read_bytes

    def reject_receipt_reopen(path: Path) -> bytes:
        if path.name.startswith(("consumed_sync_", "handoff-delivery_")):
            raise AssertionError("mutable receipt pathname was reopened")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_receipt_reopen)
    assert brain_shim._is_consumed_rendered_delivery(
        brain_shim.RepoPaths.discover(workspace), handoff
    )


def test_rendered_fixture_rejects_staging_parent_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A race after scope validation cannot redirect the rendered DOM write."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, initial, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    consumed = _consume_and_ack(
        workspace,
        control,
        initial["data"]["product_result"]["data"]["result_manifest"]["result_id"],
    )
    handoff_id = json.loads((workspace / consumed["data"]["handoff_delivery"]["path"]).read_text())["items"][0]["handoff_id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    original = brain_shim._safe_web_staging_path

    def replace_after_scope(root: Path, item_id: str):
        target, logical = original(root, item_id)
        target.parent.rmdir()
        target.parent.symlink_to(outside, target_is_directory=True)
        return target, logical

    monkeypatch.setattr(brain_shim, "_safe_web_staging_path", replace_after_scope)
    code, denied, stderr = _invoke(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.rendered",
        "--handoff-id",
        handoff_id,
    )
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"
    assert not (outside / "rendered.html").exists()


def test_eval_sha256_rejects_symlink_and_check_to_use_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing a validated wiki staging path cannot redirect a hash read."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, _approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    logical = ".brain/wiki-staging/wstg_" + "1" * 32 + "/files/wiki/questions/answer.md"
    target = workspace / logical
    target.parent.mkdir(parents=True)
    target.write_bytes(b"the staged answer\n")
    code, success, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", logical)
    assert code == 0, stderr
    assert success["data"] == {"path": logical, "sha256": hashlib.sha256(b"the staged answer\n").hexdigest()}

    page_path = logical.replace("/questions/answer.md", "/pages/not-allowed.md")
    page_target = workspace / page_path
    page_target.parent.mkdir(parents=True)
    page_target.write_bytes(b"a page is not an eval sha256 capability\n")
    code, denied, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", page_path)
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"

    invalid_slug = logical.replace("answer.md", ".md")
    invalid_target = workspace / invalid_slug
    invalid_target.write_bytes(b"hidden name\n")
    code, denied, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", invalid_slug)
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"

    for invalid_name in ("Answer.md", "two words.md", "café.md"):
        invalid_path = logical.replace("answer.md", invalid_name)
        (workspace / invalid_path).write_bytes(b"out of grammar\n")
        code, denied, stderr = _invoke(
            workspace, control, "--json", "eval", "sha256", "--path", invalid_path
        )
        assert code == 2, stderr
        assert denied["errors"][0]["code"] == "eval_fixture_denied"

    target.unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside\n")
    target.symlink_to(outside)
    code, denied, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", logical)
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"

    target.unlink()
    target.write_bytes(b"before race\n")
    original = brain_shim._safe_sha256_path

    def replace_after_check(root: Path, value: str):
        checked, normalized, observations = original(root, value)
        checked.unlink()
        checked.symlink_to(outside)
        return checked, normalized, observations

    monkeypatch.setattr(brain_shim, "_safe_sha256_path", replace_after_check)
    code, denied, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", logical)
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"


def test_eval_sha256_wraps_a_workspace_root_open_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root swap/open failure must stay a structured fixture denial."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, _approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    logical = ".brain/wiki-staging/wstg_" + "2" * 32 + "/files/wiki/questions/answer.md"
    target = workspace / logical
    target.parent.mkdir(parents=True)
    target.write_bytes(b"staged answer\n")
    original_open = brain_shim.os.open

    def reject_workspace(path, *args, **kwargs):
        if path == workspace:
            raise OSError("workspace root changed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(brain_shim.os, "open", reject_workspace)
    code, denied, stderr = _invoke(workspace, control, "--json", "eval", "sha256", "--path", logical)
    assert code == 2, stderr
    assert denied["errors"][0]["code"] == "eval_fixture_denied"


def test_recorder_binds_raw_result_and_captures_manifest_origin_before_return(tmp_path: Path) -> None:
    """The runner receives contemporaneous raw bytes rather than a later reconstruction."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    code, raw, stderr = _invoke_raw(workspace, control, "--json", "sync")
    assert code == 1, stderr
    assert raw.endswith("\n")
    command_log = [json.loads(line) for line in (control / "brain-shim-command-log.jsonl").read_text().splitlines()]
    assert command_log[0]["argv"] == ["./brain", "--json", "sync"]
    assert command_log[0]["exit_code"] == 1
    assert command_log[0]["result_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert set(command_log[0]) == {"argv", "exit_code", "result_sha256", "timestamp"}
    capture = json.loads((control / "brain-shim-capture-index.jsonl").read_text())
    assert capture["argv_sha256"] == hashlib.sha256(
        json.dumps(
            ["./brain", "--json", "sync"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    state = json.loads((control / capture["source_state_path"]).read_text())
    assert state["command"] == ["sync"]
    assert state["pending"] is not None
    assert [entry["path"] for entry in state["files"]] == sorted(
        entry["path"] for entry in state["files"]
    )
    pending = control / state["pending"]["capture_path"]
    assert json.loads(pending.read_text())["command"] == "sync"
    receipt = json.loads((control / capture["receipt_artifacts_path"]).read_text())
    assert receipt["stream"] is not None
    stream = control / receipt["stream"]["capture_path"]
    assert stream.read_bytes().endswith(b"\n")
    assert receipt["stream"]["sha256"] == hashlib.sha256(stream.read_bytes()).hexdigest()
    assert receipt["durable"] is None
    trace = [json.loads(line) for line in (control / "brain-shim-trace.jsonl").read_text().splitlines()]
    assert [row["kind"] for row in trace] == ["command_start", "source_state", "command_end"]
    assert [row["sequence"] for row in trace] == [1, 2, 3]


def test_host_recorder_factory_clamps_a_backward_clock(tmp_path: Path) -> None:
    """The runner can inject one host recorder/clock without timestamp reversal."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    moments = iter(
        (
            datetime(2026, 9, 4, 12, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc),
        )
    )

    def recorder_factory(config: brain_shim.ControlConfig) -> brain_shim.Recorder:
        return brain_shim.Recorder(config, now=lambda: next(moments))

    def invoke(*argv: str) -> tuple[int, dict[str, object]]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = brain_shim.main(
            list(argv),
            cwd=workspace,
            stdout=stdout,
            stderr=stderr,
            environ={brain_shim.CONTROL_ENV: str(control)},
            recorder_factory=recorder_factory,
        )
        assert not stderr.getvalue()
        return code, json.loads(stdout.getvalue())

    code, sync = invoke("--json", "sync")
    assert code == 1
    code, _consumed = invoke(
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        sync["data"]["result_manifest"]["result_id"],
    )
    assert code == 0
    log = [
        json.loads(line)
        for line in (control / "brain-shim-command-log.jsonl").read_text().splitlines()
    ]
    assert [entry["timestamp"] for entry in log] == [
        "2026-09-04T12:00:01+00:00",
        "2026-09-04T12:00:01+00:00",
    ]


def test_initial_fixture_is_one_outer_observation_with_snapshot_state(tmp_path: Path) -> None:
    """The nested product snapshot is evidence inside, not a second command."""

    workspace = _fixture_workspace(tmp_path, "web-approval-and-capture")
    control, approval = _write_web_control(tmp_path, workspace, phase="approved_capture")
    code, raw, stderr = _invoke_raw(
        workspace,
        control,
        "--json",
        "eval",
        "mock-web-capture",
        "--fixture-id",
        "web-approval-and-capture.initial",
        "--approval-event-id",
        approval["event_id"],
        "--approval-scope",
        approval["scope"],
        "--approval-note",
        approval["note"],
    )
    assert code == 0, stderr
    result = json.loads(raw)
    assert set(result["data"]) == {"fixture_id", "product_result"}
    assert result["data"]["product_result"]["command"] == "source snapshot-url"
    index = [json.loads(line) for line in (control / "brain-shim-capture-index.jsonl").read_text().splitlines()]
    assert len(index) == 1
    assert index[0]["argv"][:5] == ["./brain", "--json", "eval", "mock-web-capture", "--fixture-id"]
    state = json.loads((control / index[0]["source_state_path"]).read_text())
    reference = result["data"]["product_result"]["data"]["result_manifest"]
    assert state["result_id"] == reference["result_id"]
    assert state["corpus_revision"] == reference["corpus_revision"]
    # The workspace has a snapshot continuation file, but it is not a sync
    # pending-result authority and must not be captured as one.
    assert state["pending"] is None


def test_first_consume_and_registration_receive_distinct_source_captures(tmp_path: Path) -> None:
    """Receipt and extraction state are captured at their own command boundaries."""

    workspace = _fixture_workspace(tmp_path, "new-binary-before-question")
    control = _write_plain_control(tmp_path, workspace, "new-binary-before-question")
    code, sync, stderr = _invoke(workspace, control, "--json", "sync")
    assert code == 1, stderr
    reference = sync["data"]["result_manifest"]
    consumed = _consume_and_ack(workspace, control, reference["result_id"])
    item = json.loads((workspace / consumed["data"]["handoff_delivery"]["path"]).read_text())["items"][0]
    staging = workspace / ".brain/agent-staging" / item["handoff_id"] / "quarterly.md"
    staging.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "tests/evals/fixtures/new-binary-before-question/expected-quarterly.md", staging)
    code, registered, stderr = _invoke(
        workspace,
        control,
        "--json",
        "source",
        "register-extraction",
        "--handoff-id",
        item["handoff_id"],
        "--staging-path",
        staging.relative_to(workspace).as_posix(),
        "--anchors-json",
        '[{"kind":"page","value":"1"}]',
        "--quality-state",
        "ok",
        "--note",
        "Faithful quarterly PDF extraction",
    )
    assert code == 0, stderr
    assert registered["data"]["registration"]["active_representation"] is not None
    captures = [json.loads(line) for line in (control / "brain-shim-capture-index.jsonl").read_text().splitlines()]
    states = [json.loads((control / item["source_state_path"]).read_text()) for item in captures if item["source_state_path"] is not None]
    assert [state["command"][:2] for state in states] == [
        ["sync"],
        ["source", "consume-sync-result"],
        ["source", "register-extraction"],
    ]
    assert len({state["command_id"] for state in states}) == 3
    consume_capture = next(
        capture for capture in captures if capture["argv"][2:4] == ["source", "consume-sync-result"]
    )
    consume_result = json.loads((control / consume_capture["result_path"]).read_text())
    consume_state = next(
        state for state in states if state["command"][:2] == ["source", "consume-sync-result"]
    )
    assert consume_state["result_id"] == consume_result["data"]["result_id"]
    assert consume_state["corpus_revision"] == consume_result["data"]["corpus_revision"]
    registration_state = next(
        state for state in states if state["command"][:2] == ["source", "register-extraction"]
    )
    assert registration_state["result_id"] is None
    assert registration_state["corpus_revision"] == registered["data"]["registration"]["corpus_revision"]
    receipt = json.loads((control / consume_capture["receipt_artifacts_path"]).read_text())
    assert receipt["stream"] is not None
    assert receipt["durable"] is not None
    assert receipt["delivery"] is not None
    durable = control / receipt["durable"]["capture_path"]
    delivery = control / receipt["delivery"]["capture_path"]
    assert json.loads(durable.read_text())["result_id"] == reference["result_id"]
    assert json.loads(delivery.read_text())["result_id"] == reference["result_id"]
