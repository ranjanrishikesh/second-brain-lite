"""Focused safety tests for the isolated cross-client evaluation runner.

These tests deliberately exercise only host-owned fixture setup and failure
paths.  They do not and cannot manufacture an ``actual_client_process`` pass.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import io
import inspect
import json
import hashlib
import os
import pickle
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace

import pytest

from tests.evals import run_cross_client as runner
from tests.evals.scenario_contract import validate_against_schema


ROOT = Path(__file__).resolve().parents[2]
# Some phase-one fixture helpers monkeypatch the module object shared by
# ``runner.subprocess`` and ``subprocess``.  Keep the genuine constructor at
# import time for the one nested local-shim process in the receipt fixture.
_TASK7D_NATIVE_POPEN = subprocess.Popen
# The producer waits for the runner's FD-bound acceptance of its first native
# row before starting the nested shim.  Descriptor-heavy host validation can
# exceed the five-second reap window under a combined selector, but this
# remains safely below the runner's live-stream idle deadline.
_TASK7D_NATIVE_RECEIPT_BARRIER_TIMEOUT_SECONDS = (
    runner._LIVE_STREAM_IDLE_TIMEOUT_SECONDS / 2
)


def _codex_template() -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-C",
        "{workspace}",
        "-",
    ]


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


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _captured_source_state_files(
    writer: runner.EvidenceWriter, workspace: Path, *, prefix: str,
) -> list[dict[str, str]]:
    """Build the complete Task 7b retained-source file portion of a state."""

    paths = [
        *sorted((workspace / "sources/raw").rglob("*")),
        *sorted((workspace / "sources/extracted").rglob("*")),
        workspace / "config/extractors.toml",
    ]
    captures: list[dict[str, str]] = []
    for index, path in enumerate(sorted((path for path in paths if path.is_file()), key=lambda item: item.as_posix()), 1):
        logical = path.relative_to(workspace).as_posix()
        artifact_id = f"{prefix}-file-{index}"
        writer.add_bytes(artifact_id, "file_capture", path.read_bytes())
        captures.append({"path": logical, "artifact_id": artifact_id})
    return captures


def _captured_inventory(
    writer: runner.EvidenceWriter,
    files: dict[str, bytes],
    *,
    prefix: str,
) -> dict[str, dict[str, object]]:
    """Retain a tiny immutable workspace view for assertion-predicate tests."""

    inventory: dict[str, dict[str, object]] = {}
    for index, (path, raw) in enumerate(sorted(files.items()), 1):
        artifact_id = f"{prefix}-{index}"
        writer.add_bytes(artifact_id, "file_capture", raw)
        inventory[path] = {
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "content_id": artifact_id,
        }
    return inventory


def _state_bound_apply_command(
    writer: runner.EvidenceWriter,
    *,
    run_id: str,
    command_id: str,
    state_id: str,
    revision: str,
    start_sequence: int = 1,
    execution_id: str = "main",
) -> runner.SemanticCommandEvidence:
    """Add one fully indexed publication-shaped checkpoint owner for a test."""

    argv = (
        "./brain", "--json", "wiki", "apply", "--manifest",
        ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json",
    )
    result = {
        "ok": True,
        "command": "wiki apply",
        "data": {
            "corpus_revision": revision,
            "changed_paths": ["wiki/index.md", "wiki/questions/what-is-alpha.md"],
            "index_path": "wiki/index.md",
            "recovered": False,
        },
        "warnings": [],
        "errors": [],
    }
    raw = _encoded(result)
    command = runner.SemanticCommandEvidence(
        command_id=command_id, execution_id=execution_id, argv=argv,
        result=result, result_raw=raw, result_sha256=hashlib.sha256(raw).hexdigest(),
        start_sequence=start_sequence, end_sequence=start_sequence + 1,
        artifact_ids={"source_state": state_id},
    )
    writer.add_bytes(command_id + "-result", "command_result", raw)
    writer.add_json(command_id, "command_observation", {
        "run_id": run_id, "execution_id": execution_id, "argv": list(argv),
        "exit_code": 0, "result_id": command_id + "-result",
        "result_sha256": command.result_sha256,
        "timestamp": "2026-09-04T12:00:00Z",
        "start_sequence": command.start_sequence, "end_sequence": command.end_sequence,
        "source_state_id": state_id,
    })
    return command


def _claude_transcript(
    content: list[dict[str, str]], *, cwd: str = "/runner-test-workspace",
) -> bytes:
    """A locally shaped 2.1.251 parser fixture, never an actual client stream."""

    rows = [
        {
            "type": "system", "subtype": "init", "uuid": "init", "session_id": "runner-test",
            "claude_code_version": "2.1.251", "cwd": cwd, "model": "runner-test",
            "tools": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"], "mcp_servers": [],
            "permissionMode": "dontAsk", "apiKeySource": "runner-test", "slash_commands": [],
            "output_style": "default", "skills": [], "plugins": [],
        },
        {
            "type": "assistant", "message": {"model": "runner-test", "content": content},
            "parent_tool_use_id": None, "session_id": "runner-test", "uuid": "assistant",
        },
        {
            "type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
            "num_turns": 1, "is_error": False, "session_id": "runner-test", "uuid": "result",
            "result": "", "stop_reason": "end_turn", "total_cost_usd": 0, "usage": {},
            "modelUsage": {}, "permission_denials": [],
        },
    ]
    return b"".join(_encoded(row) + b"\n" for row in rows)


def _initial_sync_receipt_lifecycle_stream(
    cwd: Path,
    environ: dict[str, str],
    *,
    separate_approval_blocks: bool = False,
    approval_before_report: bool = False,
    merge_acknowledgement_and_approval: bool = False,
    invert_merged_acknowledgement_and_approval: bool = False,
    approval_code_context: str | None = None,
    after_sync: Callable[[Path], None] | None = None,
) -> object:
    """Yield one phase-one stream with commands before their exact markers.

    This is a test-only Claude-shaped stream around the real fixture shim.
    It deliberately exercises the receipt timeline: each native marker
    arrives only after its matching command has closed in the shared trace.
    """

    template = [json.loads(line) for line in _claude_transcript([]).splitlines()]
    template[0]["cwd"] = str(cwd.resolve())

    def assistant(index: int, content: list[dict[str, str]]) -> bytes:
        return _encoded({
            "type": "assistant",
            "message": {"model": "runner-test", "content": content},
            "parent_tool_use_id": None,
            "session_id": "runner-test",
            "uuid": f"approval-lifecycle-{index}",
        }) + b"\n"

    def invoke(args: list[str]) -> dict[str, object]:
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", *args],
            cwd=cwd, env=environ, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert completed.returncode in {0, 1}, completed.stderr.decode()
        result = json.loads(completed.stdout)
        assert result["ok"] is True, result
        return result

    def stream() -> object:
        yield _encoded(template[0]) + b"\n"
        sync = invoke(["sync"])
        manifest = _shim_manifest(sync)
        result_id = manifest["result_id"]
        assert isinstance(result_id, str)
        if after_sync is not None:
            after_sync(Path(environ[runner.CONTROL_ENV]))
        yield assistant(1, [{
            "type": "text", "text": "EVENT:initial_sync_receipt_verified\n",
        }])
        invoke(["source", "consume-sync-result", "--result-id", result_id])
        yield assistant(2, [{
            "type": "text", "text": "EVENT:initial_sync_receipt_consumed\n",
        }])
        invoke(["source", "consume-sync-result", "--result-id", result_id])
        yield assistant(3, [{
            "type": "text", "text": "EVENT:initial_sync_receipt_durable\n",
        }])
        invoke(["source", "acknowledge-sync-result", "--result-id", result_id])
        approval_content = (
            [
                {
                    "type": "text",
                    "text": (
                        "The prior standard has no current local evidence.\n"
                        "EVENT:report_local_evidence_gap\n"
                    ),
                },
                {"type": "text", "text": "EVENT:ask_web_approval\n"},
            ]
            if separate_approval_blocks
            else [{
                "type": "text",
                "text": (
                    "The prior standard has no current local evidence.\n"
                    "EVENT:report_local_evidence_gap\n"
                    "EVENT:ask_web_approval\n"
                ),
            }]
        )
        if approval_before_report:
            assert separate_approval_blocks
            approval_content = list(reversed(approval_content))
        if approval_code_context == "fenced":
            approval_content = [{
                "type": "text",
                "text": "```text\n" + "".join(
                    block["text"] for block in approval_content
                ) + "```\n",
            }]
        elif approval_code_context == "multiline_inline":
            approval_content = [{
                "type": "text",
                "text": "Before ``\n" + "".join(
                    block["text"] for block in approval_content
                ) + "`` after\n",
            }]
        elif approval_code_context is not None:
            raise AssertionError(approval_code_context)
        acknowledgement = {
            "type": "text", "text": "EVENT:initial_sync_receipt_acknowledged\n",
        }
        if invert_merged_acknowledgement_and_approval:
            assert merge_acknowledgement_and_approval
            assert len(approval_content) == 2
            yield assistant(4, [approval_content[0], acknowledgement, approval_content[1]])
        elif merge_acknowledgement_and_approval:
            yield assistant(4, [acknowledgement, *approval_content])
        else:
            yield assistant(4, [acknowledgement])
            yield assistant(5, approval_content)
        yield _encoded(template[-1]) + b"\n"

    return stream()


def _canonical_web_approval_capture(
    cwd: Path, environ: dict[str, str],
) -> runner.ProcessCapture:
    """Return a phase-one capture that closes the initial-sync receipt."""

    return runner.ProcessCapture(
        exit_code=0,
        stdout=b"",
        stdout_chunks=_initial_sync_receipt_lifecycle_stream(cwd, environ),
        stderr=b"",
        version=b"2.1.251\n",
        help=b"stream-json\n",
    )


def _native_trace(transcript: bytes, transcript_id: str = "transcript-1") -> list[dict[str, object]]:
    offset = 0
    records: list[dict[str, object]] = []
    for sequence, row in enumerate(transcript.splitlines(keepends=True), 1):
        records.append({
            "sequence": sequence, "kind": "native_record", "transcript_id": transcript_id,
            "byte_start": offset, "byte_end": offset + len(row), "sha256": hashlib.sha256(row).hexdigest(),
            "workspace_snapshot_id": f"workspace-snapshot-test-{sequence}",
        })
        offset += len(row)
    return records


def _semantic_marker(
    name: str, *, execution_id: str, trace_sequence: int,
) -> runner.SemanticMarkerEvidence:
    marker = f"EVENT:{name}\n"
    # Synthetic evidence must still describe a physically possible transcript:
    # independent trace records cannot all claim the same native byte span.
    native_start = (trace_sequence - 1) * 100
    return runner.SemanticMarkerEvidence(
        marker_id="marker-" + name.replace("_", "-"),
        execution_id=execution_id,
        candidate=runner.MarkerCandidate(
            event_name=name,
            marker=marker,
            byte_start=0,
            byte_end=len(marker),
            excerpt_sha256=hashlib.sha256(marker.encode()).hexdigest(),
            native_record_start=native_start,
            native_record_end=native_start + 100,
            native_record_sha256="a" * 64,
            text_pointer="/message/content/0/text",
            trace_sequence=trace_sequence,
        ),
    )


def _marker_snapshot_native(
    trace_sequence: int, *, transcript_id: str = "transcript",
) -> dict[str, object]:
    """Synthetic marker provenance matching ``_semantic_marker`` exactly."""

    return {
        "transcript_id": transcript_id,
        "byte_start": (trace_sequence - 1) * 100,
        "byte_end": trace_sequence * 100,
        "sha256": "a" * 64,
    }


def _persist_marker_snapshot(
    writer: runner.EvidenceWriter,
    source: runner.WorkspaceSnapshot,
    *,
    artifact_id: str,
    trace_sequence: int,
    inventory: tuple[object, ...] | None = None,
    staged_paths: tuple[str, ...] | None = None,
) -> runner.WorkspaceSnapshot:
    """Create a separately indexed synthetic marker snapshot for one test."""

    return runner._persist_workspace_snapshot(
        writer,
        execution_id=source.execution_id,
        trace_sequence=trace_sequence,
        inventory=source.inventory if inventory is None else inventory,
        staged_paths=source.staged_paths if staged_paths is None else staged_paths,
        role="marker",
        native_record=_marker_snapshot_native(trace_sequence),
        artifact_id=artifact_id,
    )


def test_indexed_marker_trace_sequence_must_match_its_sealed_native_record() -> None:
    """An in-memory marker cannot redirect snapshot selection to another trace row."""

    marker = _semantic_marker("validate", execution_id="main", trace_sequence=7)
    native_records = {
        marker.candidate.native_record_start: {
            "end": marker.candidate.native_record_end,
            "sha256": marker.candidate.native_record_sha256,
            "sequence": 7,
        },
    }

    assert runner._marker_trace_sequence_matches(marker, native_records) is True
    redirected = replace(
        marker,
        candidate=replace(marker.candidate, trace_sequence=8),
    )
    assert runner._marker_trace_sequence_matches(redirected, native_records) is False


def test_early_marker_trace_gate_rejects_a_native_row_inside_a_command_interval(
    tmp_path: Path,
) -> None:
    """A marker cannot mint preprojection support from an unowned trace span."""

    control = tmp_path / "control"
    control.mkdir()
    run_id = "a" * 64
    writer = runner.EvidenceWriter(control, run_id)
    marker = _semantic_marker("validate", execution_id="execution-1", trace_sequence=2)
    native_raw = b"x" * (marker.candidate.native_record_end - marker.candidate.native_record_start)
    trace = {
        "schema_version": 1,
        "run_id": run_id,
        "execution_id": "execution-1",
        "records": [
            {"sequence": 1, "kind": "command_start", "command_id": "forged-command"},
            {
                "sequence": 2, "kind": "native_record", "transcript_id": "transcript",
                "byte_start": marker.candidate.native_record_start,
                "byte_end": marker.candidate.native_record_end,
                "sha256": hashlib.sha256(native_raw).hexdigest(),
                "workspace_snapshot_id": "workspace-snapshot-test-2",
            },
            {"sequence": 3, "kind": "command_end", "command_id": "forged-command"},
        ],
    }
    # Keep every byte-level marker join valid so the missing interval check is
    # the sole reason this malformed trace is currently admitted.
    marker = replace(
        marker,
        candidate=replace(marker.candidate, native_record_sha256=hashlib.sha256(native_raw).hexdigest()),
    )
    writer.add_json("trace-1", "execution_trace", trace)
    writer.add_json("process-1", "process", {
        "execution_id": "execution-1", "trace_id": "trace-1",
        "trace_sha256": hashlib.sha256(_encoded(trace)).hexdigest(),
    })

    with pytest.raises(runner.RunnerError, match="native row inside command interval"):
        runner._verified_marker_trace_evidence(writer, (marker,))


def test_early_marker_trace_gate_requires_the_native_snapshot_artifact(
    tmp_path: Path,
) -> None:
    """A trace-owned snapshot ID must resolve before marker projection starts."""

    control = tmp_path / "control"
    control.mkdir()
    run_id = "b" * 64
    writer = runner.EvidenceWriter(control, run_id)
    marker = _semantic_marker("validate", execution_id="execution-1", trace_sequence=1)
    native_raw = b"x" * (marker.candidate.native_record_end - marker.candidate.native_record_start)
    marker = replace(
        marker,
        candidate=replace(marker.candidate, native_record_sha256=hashlib.sha256(native_raw).hexdigest()),
    )
    trace = {
        "schema_version": 1,
        "run_id": run_id,
        "execution_id": "execution-1",
        "records": [{
            "sequence": 1,
            "kind": "native_record",
            "transcript_id": "transcript",
            "byte_start": marker.candidate.native_record_start,
            "byte_end": marker.candidate.native_record_end,
            "sha256": marker.candidate.native_record_sha256,
            "workspace_snapshot_id": "forged-workspace-snapshot",
        }],
    }
    writer.add_json("trace-1", "execution_trace", trace)
    writer.add_json("process-1", "process", {
        "execution_id": "execution-1", "trace_id": "trace-1",
        "trace_sha256": hashlib.sha256(_encoded(trace)).hexdigest(),
    })

    with pytest.raises(runner.RunnerError, match="workspace snapshot"):
        runner._verified_marker_trace_evidence(writer, (marker,))


def test_closed_json_argv_templates_reject_policy_expansion_and_arbitrary_slots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mcp = tmp_path / "brain-shim-mcp.json"
    mcp.write_bytes(b'{"mcpServers":{}}\n')

    assert runner.parse_client_command_json(json.dumps(_codex_template())) == _codex_template()
    for raw in ("{}", "null", '["codex", 1]', '["codex", ""]', "not-json"):
        with pytest.raises(runner.RunnerError):
            runner.parse_client_command_json(raw)

    actual = runner.build_effective_argv(
        "codex", _codex_template(), workspace, mcp, "ignored"
    )
    assert actual[-3:] == ["-C", str(workspace.resolve()), "-"]
    assert actual[0] == "codex"

    expanded = _codex_template()[:-1] + ["--search", "-"]
    with pytest.raises(runner.RunnerError, match="closed Codex"):
        runner.build_effective_argv("codex", expanded, workspace, mcp, "ignored")

    bad_slot = _claude_template()
    bad_slot[7] = str(mcp)
    with pytest.raises(runner.RunnerError, match="closed Claude"):
        runner.build_effective_argv("claude", bad_slot, workspace, mcp, "prompt")

    with pytest.raises(SystemExit):
        runner._cli_parser().parse_args([
            "--client", "codex", "--scenario", "current-wiki-fast-path",
            "--client-command-json", json.dumps(_codex_template()),
            "--network-attestation", "workspace-egress-denied",
            "--scratch-root", str(tmp_path),
        ])


def test_cli_reports_an_unregistered_client_without_a_test_only_scratch_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = runner.main([
        "--client", "codex", "--scenario", "current-wiki-fast-path",
        "--client-command-json", json.dumps(_codex_template()),
        "--network-attestation", "workspace-egress-denied",
    ])
    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["result"] == "incomplete"
    assert Path(report["log_path"]).is_file()


def test_scenario_and_fixture_dispatch_are_closed_before_any_path_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller text cannot traverse the generator-owned scenario/fixture roots."""

    scenario_root = tmp_path / "scenarios"
    fixture_root = tmp_path / "fixtures"
    scenario_root.mkdir()
    fixture_root.mkdir()
    # The old lexical join reached this file for ``../escaped`` and would
    # accept it when its self-reported ID matched.  A closed runner registry
    # must reject the identifier before it asks the filesystem anything.
    (tmp_path / "escaped.json").write_text('{"id":"../escaped"}')
    (tmp_path / "escaped").mkdir()
    (tmp_path / "escaped" / "fixture-manifest.json").write_text(
        '{"scenario_id":"current-wiki-fast-path","tree_sha256":"'
        + "a" * 64 + '"}'
    )
    monkeypatch.setattr(runner, "SCENARIO_ROOT", scenario_root)
    monkeypatch.setattr(runner, "FIXTURE_ROOT", fixture_root)

    for scenario_id in ("../escaped", "/tmp/escaped", "unknown-scenario", "all"):
        with pytest.raises(runner.RunnerError, match="unknown scenario"):
            runner._scenario(scenario_id)

    with pytest.raises(runner.RunnerError, match="invalid scenario fixture"):
        runner._fixture({"id": "current-wiki-fast-path", "fixture_id": "../escaped"})

    # ``all`` is a runner-owned fixed ordering, not a glob of a writable
    # scenario directory.  A dropped JSON file must never become a work item.
    assert runner._scenario_ids_for_cli("all") == runner.KNOWN_SCENARIO_IDS


@pytest.mark.parametrize(
    ("client", "template", "attestation"),
    [
        ("codex", ["/tmp/codex", *_codex_template()[1:]], "workspace-egress-denied"),
        ("codex", [*_codex_template(), "https://example.invalid/raw-url"], "workspace-egress-denied"),
        ("codex", [
            *["danger-full-access" if item == "workspace-write" else item for item in _codex_template()]
        ], "workspace-egress-denied"),
        ("claude", [
            "/tmp/alternate-mcp" if item == "{mcp_config}" else item for item in _claude_template()
        ], "mock-only"),
        ("claude", [
            "Read,Edit,Write,Glob,Grep,Bash,WebSearch" if item == "Read,Edit,Write,Glob,Grep,Bash" else item
            for item in _claude_template()
        ], "mock-only"),
    ],
)
def test_rejected_policy_or_raw_url_never_reaches_a_process_seam(
    tmp_path: Path, client: str, template: list[str], attestation: str,
) -> None:
    invoked = False

    def unexpected_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        nonlocal invoked
        invoked = True
        raise AssertionError("a rejected profile must not reach a process seam")

    outcome = runner.run_scenario(
        client=client,
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(template),
        network_attestation=attestation,
        scratch_root=tmp_path,
        process_runner=unexpected_process,
    )

    assert invoked is False
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["incomplete_reasons"] == ["policy_rejected"]


def test_fake_process_receives_only_runner_owned_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "PYTHONPATH", "PYTHONHOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "SECOND_BRAIN_EVAL_CONTROL", "GIT_CONFIG_GLOBAL",
    ):
        monkeypatch.setenv(name, "/hostile/inherited/value")
    observed: dict[str, str] = {}

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        observed.update(environ)
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert set(observed) == {"PATH", "HOME", "LANG", runner.CONTROL_ENV, runner.RUNNER_STATE_ENV}
    assert observed["PATH"] == "/usr/bin:/bin"
    assert observed["HOME"] == str(outcome.control_root / "home")
    assert observed[runner.CONTROL_ENV] == str(outcome.control_root)
    assert observed[runner.RUNNER_STATE_ENV] == str(outcome.control_root / "runner-state.json")
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


def test_fixture_copy_restores_ledger_mtimes_installs_package_wrapper_and_detects_tamper(
    tmp_path: Path,
) -> None:
    source_ledger = ROOT / "tests/evals/fixtures/current-wiki-fast-path/repo/sources/ledger"
    before = {path.name: path.read_bytes() for path in source_ledger.glob("src_*.json")}

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path
    )
    assert prepared.workspace.is_relative_to(tmp_path)
    assert prepared.control_root.is_relative_to(tmp_path)
    assert (prepared.workspace / ".git").is_dir()
    assert not (prepared.workspace / ".brain").exists()
    assert not (prepared.workspace / ".context").exists()
    assert not (prepared.workspace / "brain").is_symlink()
    assert "tests.evals.run_cross_client" in (prepared.workspace / "brain").read_text()
    original_launcher = json.loads((prepared.control_root / "original-brain.json").read_text())
    original_bytes = (prepared.control_root / "original-brain.bin").read_bytes()
    assert original_launcher["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert set(original_launcher["identity"]) == {
        "device", "inode", "mode", "uid", "nlink", "bytes", "mtime_ns", "ctime_ns",
    }

    record = next((prepared.workspace / "sources/ledger").glob("src_*.json"))
    payload = json.loads(record.read_text())
    version = next(iter(payload["versions"].values()))
    raw = prepared.workspace / "sources/raw" / version["raw_path"]
    assert raw.stat().st_mtime_ns == version["fingerprint"]["mtime_ns"]
    assert before == {path.name: path.read_bytes() for path in source_ledger.glob("src_*.json")}

    runner.verify_wrapper(prepared.wrapper_seal)
    prepared.wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="wrapper"):
        runner.verify_wrapper(prepared.wrapper_seal)


def test_fixture_copy_preserves_canonical_agent_instruction_symlinks(
    tmp_path: Path,
) -> None:
    """A copied evaluator root remains a valid brain checkout for ``validate``."""

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )

    claude = prepared.workspace / "CLAUDE.md"
    assert claude.is_symlink()
    assert os.readlink(claude) == "AGENTS.md"
    for skill in (
        "brain-answer",
        "brain-initialize",
        "brain-validate",
        "brain-web-research",
        "brain-wiki-maintenance",
    ):
        target = prepared.workspace / ".claude" / "skills" / skill
        assert target.is_symlink()
        assert os.readlink(target) == f"../../.agents/skills/{skill}"


def test_fixture_copy_excludes_stale_evaluator_runs_and_python_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copied import root must not retain executable test-cache state."""

    source_root = tmp_path / "source-root"
    shutil.copytree(ROOT, source_root, ignore=shutil.ignore_patterns(".git"))
    (source_root / ".pytest_cache").mkdir(exist_ok=True)
    (source_root / ".pytest_cache" / "sentinel").write_text("stale pytest", encoding="utf-8")
    (source_root / ".ruff_cache").mkdir(exist_ok=True)
    (source_root / ".ruff_cache" / "sentinel").write_text("stale ruff", encoding="utf-8")
    stale_bytecode = source_root / "tests/evals/__pycache__"
    stale_bytecode.mkdir(exist_ok=True)
    (stale_bytecode / "run_cross_client.cpython-314.pyc").write_bytes(b"untrusted bytecode")
    stale_runs = source_root / "tests/evals/runs"
    stale_runs.mkdir(exist_ok=True)
    (stale_runs / "prior.event-log.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(runner, "ROOT", source_root)
    monkeypatch.setattr(runner, "EVAL_ROOT", source_root / "tests/evals")
    monkeypatch.setattr(runner, "SCENARIO_ROOT", source_root / "tests/evals/scenarios")
    monkeypatch.setattr(runner, "FIXTURE_ROOT", source_root / "tests/evals/fixtures")

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path / "runs"
    )

    assert not (prepared.workspace / ".pytest_cache").exists()
    assert not (prepared.workspace / ".ruff_cache").exists()
    assert not (prepared.workspace / "tests/evals/__pycache__").exists()
    assert not (prepared.workspace / "tests/evals/runs").exists()


def test_pinned_control_root_uses_a_retained_descriptor_and_rejects_replacement(
    tmp_path: Path,
) -> None:
    """Capture conversion must not reopen an attacker-replaced root pathname."""

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    (control / "capture.json").write_bytes(b'{"trusted":true}\n')
    pin = runner.PinnedDirectory.pin(control, private=True)
    assert pin.read_relative("capture.json") == b'{"trusted":true}\n'

    moved = tmp_path / "original-control"
    control.rename(moved)
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    (control / "capture.json").write_bytes(b'{"trusted":false}\n')

    with pytest.raises(runner.RunnerError, match="trusted root"):
        pin.read_relative("capture.json")
    pin.close()


@pytest.mark.parametrize("operation", ("retain", "retain-relative"))
def test_pinned_directory_retention_rejects_malformed_direct_instance(
    operation: str,
) -> None:
    """Descriptor retention must normalize forged direct instances to RunnerError."""

    malformed = object.__new__(runner.PinnedDirectory)

    with pytest.raises(runner.RunnerError):
        if operation == "retain":
            runner.PinnedDirectory.retain(malformed)
        else:
            assert operation == "retain-relative"
            runner.PinnedDirectory.retain_relative_directory(
                malformed,
                "run-child",
                private=True,
            )


def test_pinned_directory_retain_closes_duplicate_once_after_final_verify_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final retain recheck releases its transferred duplicate once."""

    native_close = os.close
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source = object.__new__(runner.PinnedDirectory)
    leaf_identity = (1, 2, stat.S_IFDIR | 0o700, 1000)
    object.__setattr__(source, "path", Path("/trusted"))
    object.__setattr__(
        source,
        "edges",
        (("/", (1, 1, stat.S_IFDIR | 0o755, 1000)), ("/trusted", leaf_identity)),
    )
    object.__setattr__(source, "private", True)
    object.__setattr__(source, "root_fd", source_fd)

    duplicate_fd = 101
    verified: list[runner.PinnedDirectory] = []
    closed: list[int] = []

    def fake_verify(candidate: runner.PinnedDirectory) -> None:
        verified.append(candidate)
        if candidate is not source:
            assert candidate.root_fd == duplicate_fd
            raise runner.RunnerError("synthetic final retain verification failure")

    def fake_dup(fd: int) -> int:
        assert fd == source_fd
        return duplicate_fd

    def fake_set_inheritable(fd: int, inheritable: bool) -> None:
        assert (fd, inheritable) == (duplicate_fd, False)

    def fake_fstat(fd: int) -> SimpleNamespace:
        assert fd == duplicate_fd
        return SimpleNamespace(
            st_dev=leaf_identity[0],
            st_ino=leaf_identity[1],
            st_mode=leaf_identity[2],
            st_uid=leaf_identity[3],
        )

    def fake_close(fd: int) -> None:
        closed.append(fd)

    monkeypatch.setattr(runner.PinnedDirectory, "verify", fake_verify)
    monkeypatch.setattr(runner.os, "dup", fake_dup)
    monkeypatch.setattr(runner.os, "set_inheritable", fake_set_inheritable)
    monkeypatch.setattr(runner.os, "fstat", fake_fstat)
    monkeypatch.setattr(runner.os, "close", fake_close)

    try:
        with pytest.raises(runner.RunnerError, match="synthetic final retain"):
            runner.PinnedDirectory.retain(source)

        assert verified[0] is source
        assert len(verified) == 2
        assert verified[1] is not source
        assert closed == [duplicate_fd]
    finally:
        # ``source`` is synthetic, so prevent its destructor from closing a
        # now-unrelated real descriptor after monkeypatch restores ``os.close``.
        object.__setattr__(source, "root_fd", -1)
        native_close(source_fd)


def test_pinned_directory_walk_closes_every_fd_on_child_identity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child-validation failure closes its child and each retained parent once."""

    identities = {
        "root": SimpleNamespace(
            st_dev=1, st_ino=10, st_mode=stat.S_IFDIR | 0o755, st_uid=1000,
        ),
        "trusted": SimpleNamespace(
            st_dev=1, st_ino=11, st_mode=stat.S_IFDIR | 0o700, st_uid=1000,
        ),
        "child": SimpleNamespace(
            st_dev=1, st_ino=12, st_mode=stat.S_IFDIR | 0o700, st_uid=1000,
        ),
        "changed-child": SimpleNamespace(
            st_dev=1, st_ino=13, st_mode=stat.S_IFDIR | 0o700, st_uid=1000,
        ),
    }
    opened: list[int] = []
    closed: list[int] = []

    def fake_open(
        path: str,
        _flags: int,
        _mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        expected = {
            ("/", None): 100,
            ("trusted", 100): 101,
            ("child", 101): 102,
        }
        fd = expected.get((path, dir_fd))
        if fd is None:
            raise AssertionError(f"unexpected synthetic open: {(path, dir_fd)!r}")
        opened.append(fd)
        return fd

    def fake_stat(
        path: str,
        *,
        dir_fd: int,
        follow_symlinks: bool,
    ) -> SimpleNamespace:
        assert follow_symlinks is False
        expected = {
            ("trusted", 100): identities["trusted"],
            ("child", 101): identities["child"],
        }
        calls = [entry for entry in opened if entry in {101, 102}]
        if (path, dir_fd) == ("child", 101) and calls.count(102) == 1:
            # The named child changes after its descriptor was opened.
            return identities["changed-child"]
        result = expected.get((path, dir_fd))
        if result is None:
            raise AssertionError(f"unexpected synthetic stat: {(path, dir_fd)!r}")
        return result

    def fake_fstat(fd: int) -> SimpleNamespace:
        expected = {
            100: identities["root"],
            101: identities["trusted"],
            102: identities["child"],
        }
        return expected[fd]

    def fake_close(fd: int) -> None:
        closed.append(fd)

    monkeypatch.setattr(runner.os, "open", fake_open)
    monkeypatch.setattr(runner.os, "stat", fake_stat)
    monkeypatch.setattr(runner.os, "fstat", fake_fstat)
    monkeypatch.setattr(runner.os, "close", fake_close)

    with pytest.raises(runner.RunnerError, match="trusted root changed while opened"):
        runner.PinnedDirectory._walk(Path("/trusted/child"))

    assert opened == [100, 101, 102]
    assert set(closed) == {100, 101, 102}
    assert len(closed) == 3
    assert all(closed.count(fd) == 1 for fd in opened)


def test_workspace_snapshotter_rejects_an_outside_hydrated_content_artifact(
    tmp_path: Path,
) -> None:
    """A forged indexed digest must not grant a snapshotter external read authority."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control, "a" * 64)
    outside = tmp_path / "outside-content.bin"
    raw = b"forged pre-existing snapshot content\n"
    outside.write_bytes(raw)
    assert not outside.resolve().is_relative_to(writer.run_root.resolve())
    digest = hashlib.sha256(raw).hexdigest()
    artifact_id = "workspace-content-" + digest
    writer.entries.append({
        "id": artifact_id,
        "type": "file_capture",
        "relative_path": str(outside.resolve()),
        "sha256": digest,
        "bytes": len(raw),
    })
    workspace_pin = runner.PinnedDirectory.pin(workspace, private=False)

    try:
        with pytest.raises(runner.RunnerError):
            runner.WorkspaceSnapshotter(
                workspace=workspace, workspace_pin=workspace_pin, writer=writer,
            )
    finally:
        workspace_pin.close()


def test_workspace_snapshotter_reuses_content_only_from_an_indexed_snapshot(
    tmp_path: Path,
) -> None:
    """A reserved content row needs snapshot provenance before phase-two reuse."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    raw = b"unchanged workspace content\n"
    (workspace / "observed.txt").write_bytes(raw)
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control, "b" * 64)
    digest = hashlib.sha256(raw).hexdigest()
    artifact_id = "workspace-content-" + digest
    writer.add_bytes(artifact_id, "file_capture", raw)
    workspace_pin = runner.PinnedDirectory.pin(workspace, private=False)
    inventory = ({
        "path": "observed.txt",
        "sha256": digest,
        "bytes": len(raw),
        "content_id": artifact_id,
    },)

    try:
        unproven = runner.WorkspaceSnapshotter(
            workspace=workspace, workspace_pin=workspace_pin, writer=writer,
        )
        with pytest.raises(runner.RunnerError, match="duplicate artifact identity"):
            unproven.capture(execution_id="execution-1", trace_sequence=0)

        prior = runner._persist_workspace_snapshot(
            writer,
            execution_id="execution-1",
            trace_sequence=0,
            inventory=inventory,
            staged_paths=(),
            role="initial",
            native_record=None,
        )
        reused = runner.WorkspaceSnapshotter(
            workspace=workspace,
            workspace_pin=workspace_pin,
            writer=writer,
            reusable_snapshots=(prior,),
        ).capture(execution_id="execution-2", trace_sequence=0)

        assert reused.inventory == inventory
        assert [entry["id"] for entry in writer.entries].count(artifact_id) == 1
    finally:
        workspace_pin.close()


def test_marker_bound_workspace_snapshots_make_a_typed_diff_from_captured_bytes(
    tmp_path: Path,
) -> None:
    """Diff support is built from marker-time host snapshots, never a late read."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    (workspace / "feature.txt").write_text("before\n", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    writer = runner.EvidenceWriter(control, "c" * 64)
    snapshots = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=runner.PinnedDirectory.pin(workspace, private=False),
        writer=writer,
    )
    before = snapshots.capture(execution_id="main", trace_sequence=0)
    (workspace / "feature.txt").write_text("after\n", encoding="utf-8")
    after = snapshots.capture(
        execution_id="main", trace_sequence=4,
        native_record=_marker_snapshot_native(4),
    )
    marker = _semantic_marker(
        "classify_repository_development", execution_id="main", trace_sequence=4,
    )

    references = runner._build_marker_diff_references(
        writer, run_id="c" * 64,
        required_events=("classify_repository_development",),
        markers=(marker,), initial_snapshots={"main": before}, snapshots=(after,),
        execution_order={"main": 0},
    )

    assert set(references) == {"classify_repository_development"}
    diff_id = references["classify_repository_development"]["diff_id"]
    entry = next(entry for entry in writer.entries if entry["id"] == diff_id)
    diff = json.loads((writer.run_root / entry["relative_path"]).read_text())
    assert diff["changed_paths"] == ["feature.txt"]
    assert diff["staged_paths"] == []
    assert diff["before_snapshot_id"] == before.artifact_id
    assert diff["after_snapshot_id"] == after.artifact_id
    before_id = diff["before"][0]["content_id"]
    after_id = diff["after"][0]["content_id"]
    entries = {entry["id"]: entry for entry in writer.entries}
    assert (writer.run_root / entries[before_id]["relative_path"]).read_bytes() == b"before\n"
    assert (writer.run_root / entries[after_id]["relative_path"]).read_bytes() == b"after\n"


def test_marker_diff_reloads_the_indexed_snapshot_instead_of_python_snapshot_fields(
    tmp_path: Path,
) -> None:
    """A relabelled Python snapshot cannot replace its immutable capture."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    (workspace / "feature.txt").write_text("before\n", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    writer = runner.EvidenceWriter(control, "e" * 64)
    snapshots = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=runner.PinnedDirectory.pin(workspace, private=False),
        writer=writer,
    )
    before = snapshots.capture(execution_id="main", trace_sequence=0)
    (workspace / "feature.txt").write_text("after\n", encoding="utf-8")
    after = snapshots.capture(
        execution_id="main", trace_sequence=4,
        native_record=_marker_snapshot_native(4),
    )
    marker = _semantic_marker(
        "classify_repository_development", execution_id="main", trace_sequence=4,
    )
    relabelled = replace(after, inventory=before.inventory, staged_paths=before.staged_paths)

    references = runner._build_marker_diff_references(
        writer, run_id="e" * 64,
        required_events=("classify_repository_development",), markers=(marker,),
        initial_snapshots={"main": before}, snapshots=(relabelled,),
        execution_order={"main": 0},
    )

    entry = next(entry for entry in writer.entries if entry["id"] == references[
        "classify_repository_development"]["diff_id"])
    diff = json.loads((writer.run_root / entry["relative_path"]).read_text())
    assert diff["changed_paths"] == ["feature.txt"]


def test_marker_diff_rejects_a_same_provenance_snapshot_not_owned_by_the_trace(
    tmp_path: Path,
) -> None:
    """A second snapshot for one native row cannot replace its trace-owned ID."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    (workspace / "feature.txt").write_text("before\n", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    writer = runner.EvidenceWriter(control, "1" * 64)
    snapshots = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=runner.PinnedDirectory.pin(workspace, private=False),
        writer=writer,
    )
    before = snapshots.capture(execution_id="main", trace_sequence=0)
    (workspace / "feature.txt").write_text("after\n", encoding="utf-8")
    owned = snapshots.capture(
        execution_id="main", trace_sequence=4,
        native_record=_marker_snapshot_native(4),
    )
    alternate = _persist_marker_snapshot(
        writer, owned, artifact_id="alternate-marker-snapshot", trace_sequence=4,
    )
    marker = _semantic_marker(
        "classify_repository_development", execution_id="main", trace_sequence=4,
    )

    with pytest.raises(runner.RunnerError, match="not trace-owned"):
        runner._build_marker_diff_references(
            writer, run_id="1" * 64,
            required_events=("classify_repository_development",),
            markers=(marker,), initial_snapshots={"main": before}, snapshots=(alternate,),
            execution_order={"main": 0},
            trace_snapshot_ids={("main", 4): owned.artifact_id},
        )


def test_workspace_snapshot_artifacts_have_closed_initial_and_native_provenance(
    tmp_path: Path,
) -> None:
    """Each marker-time workspace view is a typed, indexed native-row record."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    (workspace / "feature.txt").write_text("before\n", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    writer = runner.EvidenceWriter(control, "f" * 64)
    snapshots = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=runner.PinnedDirectory.pin(workspace, private=False),
        writer=writer,
    )
    initial = snapshots.capture(execution_id="main", trace_sequence=0)
    (workspace / "feature.txt").write_text("after\n", encoding="utf-8")
    marker_snapshot = snapshots.capture(
        execution_id="main", trace_sequence=4,
        native_record=_marker_snapshot_native(4),
    )
    entries = {entry["id"]: entry for entry in writer.entries}
    initial_record = json.loads((writer.run_root / entries[initial.artifact_id]["relative_path"]).read_text())
    marker_record = json.loads((writer.run_root / entries[marker_snapshot.artifact_id]["relative_path"]).read_text())

    assert entries[initial.artifact_id]["type"] == "workspace_snapshot"
    assert set(initial_record) == {
        "schema_version", "run_id", "execution_id", "anchor_kind", "trace_sequence",
        "inventory", "staged_paths",
    }
    assert initial_record["anchor_kind"] == "phase_initial"
    assert initial_record["trace_sequence"] == 0
    assert set(marker_record) == {
        "schema_version", "run_id", "execution_id", "anchor_kind", "trace_sequence",
        "transcript_id", "native_record_start", "native_record_end", "native_record_sha256",
        "inventory", "staged_paths",
    }
    assert marker_record["anchor_kind"] == "native_record"
    assert marker_record["trace_sequence"] == 4
    assert marker_record["transcript_id"] == "transcript"
    assert marker_record["native_record_start"] == 300
    assert marker_record["native_record_end"] == 400
    assert marker_record["native_record_sha256"] == "a" * 64


def test_same_native_record_diff_markers_share_the_pre_record_snapshot(
    tmp_path: Path,
) -> None:
    """Adjacent markers describe one post-command state, not an empty delta."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    (workspace / "feature.txt").write_text("before\n", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    control.chmod(0o700)
    writer = runner.EvidenceWriter(control, "d" * 64)
    snapshots = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=runner.PinnedDirectory.pin(workspace, private=False),
        writer=writer,
    )
    before = snapshots.capture(execution_id="main", trace_sequence=0)
    (workspace / "feature.txt").write_text("after\n", encoding="utf-8")
    after = snapshots.capture(
        execution_id="main", trace_sequence=4,
        native_record=_marker_snapshot_native(4),
    )
    preserve = _semantic_marker("preserve_both_claims", execution_id="main", trace_sequence=4)
    cite = _semantic_marker("cite_both_sides", execution_id="main", trace_sequence=4)
    cite = replace(
        cite,
        candidate=replace(
            cite.candidate,
            byte_start=len(preserve.candidate.marker.encode()),
            byte_end=len(preserve.candidate.marker.encode()) + len(cite.candidate.marker.encode()),
        ),
    )

    references = runner._build_marker_diff_references(
        writer, run_id="d" * 64,
        required_events=("preserve_both_claims", "cite_both_sides"),
        markers=(preserve, cite), initial_snapshots={"main": before}, snapshots=(after,),
        execution_order={"main": 0},
    )

    assert set(references) == {"preserve_both_claims", "cite_both_sides"}
    for event in references:
        entry = next(
            entry for entry in writer.entries
            if entry["id"] == references[event]["diff_id"]
        )
        diff = json.loads((writer.run_root / entry["relative_path"]).read_text())
        assert diff["changed_paths"] == ["feature.txt"]


def test_runner_marks_shim_configuration_tampering_as_an_integrity_failure(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        (Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").write_text("{}", encoding="utf-8")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_runner_marks_runner_state_tampering_as_an_integrity_failure(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        Path(environ[runner.RUNNER_STATE_ENV]).write_text("{}", encoding="utf-8")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_runner_seals_claude_mcp_bytes_and_identity_across_the_process(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        mcp_path = Path(argv[7])
        assert mcp_path.read_bytes() == runner.EMPTY_MCP_BYTES
        mcp_path.write_bytes(b'{"mcpServers":{"evil":{}}}\n')
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    mcp_entry = next(entry for entry in index["entries"] if entry["id"] == "mcp-1")
    # The client receives the exact indexed file.  A same-user fake can alter
    # it after launch, but the sealed entry keeps the original digest and the
    # post-launch identity failure prevents that altered file from supporting
    # any replay or pass.
    assert mcp_entry["sha256"] == hashlib.sha256(runner.EMPTY_MCP_BYTES).hexdigest()
    assert (outcome.log_path.parent / mcp_entry["relative_path"]).read_bytes() != runner.EMPTY_MCP_BYTES


def test_web_integrity_failure_does_not_restore_control_and_continue_to_phase_two(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        control = Path(environ[runner.CONTROL_ENV])
        phases.append(json.loads((control / "brain-shim.json").read_text())["phase"])
        (control / "brain-shim.json").write_text("{}", encoding="utf-8")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_web_nonzero_approval_process_never_receives_phase_two_fixture_capability(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        phases.append(json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"])
        return runner.ProcessCapture(
            exit_code=1, stdout=b"", stderr=b"approval did not stop safely\n",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "client_exit_nonzero" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "fixture_capability" not in {entry["type"] for entry in index["entries"]}


def test_web_phase_two_requires_parser_backed_gap_and_approval_markers(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        phases.append(json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"])
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=b"",
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


def test_web_fixture_assets_are_sealed_across_both_phases(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        control = Path(environ[runner.CONTROL_ENV])
        phase = json.loads((control / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        if phase == "approved_capture":
            (control / "static-shell.html").write_bytes(b"attacker static shell")
            descriptor = json.loads((control / "fixture-descriptor.json").read_text())
            descriptor["static_sha256"] = hashlib.sha256(b"attacker static shell").hexdigest()
            (control / "fixture-descriptor.json").write_text(
                json.dumps(descriptor, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
        transcript = _claude_transcript([{
            "type": "text",
            "text": "No semantic pass is claimed.\n",
        }], cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=transcript, stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert phases == ["approval", "approved_capture"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    artifact_ids = {entry["id"] for entry in index["entries"]}
    # The mutation occurs after the approved-capture process starts.  It
    # invalidates that phase's control seal, so no descriptor/capability may
    # be projected from attacker-controlled bytes at all.
    assert "fixture-descriptor" not in artifact_ids
    assert "fixture-capability" not in artifact_ids


def test_web_fixture_assets_reject_mutation_between_phase_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase-two baseline cannot quietly accept phase-one asset replacement."""

    phases: list[str] = []
    original_phase_control = runner._phase_control

    def mutate_before_phase_two(
        prepared: runner.PreparedWorkspace, *, phase: str, approval: dict[str, str] | None,
    ) -> runner.PhaseControlSeals:
        if phase == "approved_capture":
            static = prepared.control_root / "static-shell.html"
            static.write_bytes(b"replaced between phases")
            descriptor_path = prepared.control_root / "fixture-descriptor.json"
            descriptor = json.loads(descriptor_path.read_text())
            descriptor["static_sha256"] = hashlib.sha256(static.read_bytes()).hexdigest()
            descriptor_path.write_text(json.dumps(descriptor, sort_keys=True, separators=(",", ":")))
        return original_phase_control(prepared, phase=phase, approval=approval)

    monkeypatch.setattr(runner, "_phase_control", mutate_before_phase_two)

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        text = "No semantic pass is claimed.\n"
        return runner.ProcessCapture(
            exit_code=0,
            stdout=b"",
            stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=(_claude_transcript(
                [{"type": "text", "text": text}], cwd=str(cwd.resolve()),
            ),),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_web_fixture_assets_reject_same_byte_inode_replacement_between_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An os.replace lookalike cannot evade the phase-one asset seal."""

    phases: list[str] = []
    original_phase_control = runner._phase_control

    def replace_before_phase_two(
        prepared: runner.PreparedWorkspace, *, phase: str, approval: dict[str, str] | None,
    ) -> runner.PhaseControlSeals:
        if phase == "approved_capture":
            static = prepared.control_root / "static-shell.html"
            replacement = prepared.control_root / "static-shell-replacement.html"
            replacement.write_bytes(static.read_bytes())
            replacement.chmod(0o600)
            os.replace(replacement, static)
        return original_phase_control(prepared, phase=phase, approval=approval)

    monkeypatch.setattr(runner, "_phase_control", replace_before_phase_two)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        text = "No semantic pass is claimed.\n"
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=(_claude_transcript(
                [{"type": "text", "text": text}], cwd=str(cwd.resolve()),
            ),),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_web_phase_two_uses_assets_captured_at_workspace_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating the global fixture source after phase one cannot change phase two."""

    fixture_root = tmp_path / "fixtures"
    shutil.copytree(ROOT / "tests/evals/fixtures", fixture_root)
    monkeypatch.setattr(runner, "FIXTURE_ROOT", fixture_root)
    phases: list[str] = []
    original_phase_control = runner._phase_control

    def mutate_global_source_before_phase_two(
        prepared: runner.PreparedWorkspace, *, phase: str, approval: dict[str, str] | None,
    ) -> runner.PhaseControlSeals:
        if phase == "approved_capture":
            asset_root = fixture_root / "web-approval-and-capture"
            changed = b"attacker replacement fixture shell\n"
            (asset_root / "static-shell.html").write_bytes(changed)
            (prepared.control_root / "static-shell.html").write_bytes(changed)
            descriptor_path = prepared.control_root / "fixture-descriptor.json"
            descriptor = json.loads(descriptor_path.read_text())
            descriptor["static_sha256"] = hashlib.sha256(changed).hexdigest()
            descriptor_path.write_text(json.dumps(descriptor, sort_keys=True, separators=(",", ":")))
        return original_phase_control(prepared, phase=phase, approval=approval)

    monkeypatch.setattr(runner, "_phase_control", mutate_global_source_before_phase_two)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        text = "No semantic pass is claimed.\n"
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=(_claude_transcript(
                [{"type": "text", "text": text}], cwd=str(cwd.resolve()),
            ),),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]


def test_web_manifest_never_projects_capture_capability_after_phase_two_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-one approval is not evidence that capture was ever launched."""

    phases: list[str] = []
    original_phase_control = runner._phase_control

    def fail_before_capture_setup(
        prepared: runner.PreparedWorkspace, *, phase: str, approval: dict[str, str] | None,
    ) -> runner.PhaseControlSeals:
        if phase == "approved_capture":
            raise runner.RunnerError("injected phase-two setup failure")
        return original_phase_control(prepared, phase=phase, approval=approval)

    monkeypatch.setattr(runner, "_phase_control", fail_before_capture_setup)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        raise AssertionError("phase-two setup failure must occur before process launch")

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] is None
    assert manifest["approval"] is None
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "fixture_capability" not in {entry["type"] for entry in index["entries"]}


def test_web_phase_one_foreign_native_workspace_cannot_release_capture_phase(
    tmp_path: Path,
) -> None:
    """A parser-valid Claude init must still name the isolated workspace."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        text = (
            "The prior standard has no current local evidence.\n"
            "EVENT:report_local_evidence_gap\n"
            "EVENT:ask_web_approval\n"
        ) if phase == "approval" else "No semantic pass is claimed.\n"
        # The fixture helper's parser-valid init names /runner-test-workspace,
        # not the private workspace supplied to this fake process.
        transcript = _claude_transcript([{"type": "text", "text": text}])
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=completed.stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] is None
    assert manifest["approval"] is None


def test_web_phase_one_requires_the_entire_initial_sync_receipt_lifecycle(
    tmp_path: Path,
) -> None:
    """A lone sync plus approval prose cannot release the capture fixture."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            completed = subprocess.run(
                [str(cwd / "brain"), "--json", "sync"],
                cwd=cwd, env=environ, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            assert completed.returncode in {0, 1}, completed.stderr.decode()
            text = (
                "The prior standard has no current local evidence.\n"
                "EVENT:report_local_evidence_gap\n"
                "EVENT:ask_web_approval\n"
            )
            stderr = completed.stderr
        else:
            text = "No semantic pass is claimed.\n"
            stderr = b""
        transcript = _claude_transcript(
            [{"type": "text", "text": text}], cwd=str(cwd.resolve()),
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"approval", "fixture_capability", "fixture_descriptor"} & {
        entry["type"] for entry in index["entries"]
    }


def test_web_phase_one_allows_canonical_markers_in_separate_content_blocks(
    tmp_path: Path,
) -> None:
    """Canonical text-block order, not per-block offsets, controls approval."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd, environ, separate_approval_blocks=True,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        else:
            content = [{"type": "text", "text": "No semantic pass is claimed.\n"}]
            stderr = b""
        transcript = _claude_transcript(content, cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval", "approved_capture"]
    assert "web_approval_phase_unverified" not in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] == "fixture-capability"


def test_web_phase_one_allows_ordered_acknowledgement_and_approval_in_one_native_record(
    tmp_path: Path,
) -> None:
    """A later content block may continue the receipt marker's native row."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd,
                    environ,
                    separate_approval_blocks=True,
                    merge_acknowledgement_and_approval=True,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        transcript = _claude_transcript(
            [{"type": "text", "text": "No semantic pass is claimed.\n"}],
            cwd=str(cwd.resolve()),
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval", "approved_capture"]
    assert "web_approval_phase_unverified" not in outcome.log["incomplete_reasons"]


def test_web_phase_one_rejects_inverted_acknowledgement_and_approval_in_one_native_record(
    tmp_path: Path,
) -> None:
    """Same-record markers must still follow the sealed receipt order."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd,
                    environ,
                    separate_approval_blocks=True,
                    merge_acknowledgement_and_approval=True,
                    invert_merged_acknowledgement_and_approval=True,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        raise AssertionError("inverted approval markers must not reach phase two")

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


def test_web_phase_one_rejects_approval_before_report_in_one_native_record(
    tmp_path: Path,
) -> None:
    """Approval cannot precede the evidence-gap report in canonical block order."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd,
                    environ,
                    separate_approval_blocks=True,
                    approval_before_report=True,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        raise AssertionError("approval before report must not reach phase two")

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


@pytest.mark.parametrize("code_context", ["fenced", "multiline_inline"])
def test_web_phase_one_rejects_approval_markers_quoted_as_commonmark_code(
    tmp_path: Path,
    code_context: str,
) -> None:
    """Quoted report/approval examples cannot authorize fixture capability."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd, environ, approval_code_context=code_context,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        raise AssertionError("quoted approval markers must not reach phase two")

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] is None
    assert manifest["approval"] is None


@pytest.mark.parametrize(
    ("event_name", "code_context"),
    [
        (event_name, code_context)
        for event_name in (
            "initial_sync_receipt_verified",
            "initial_sync_receipt_consumed",
            "initial_sync_receipt_durable",
            "initial_sync_receipt_acknowledged",
            "report_local_evidence_gap",
            "ask_web_approval",
        )
        for code_context in ("fenced", "multiline_inline")
    ],
)
def test_event_extraction_excludes_every_phase_one_marker_quoted_as_commonmark_code(
    event_name: str,
    code_context: str,
) -> None:
    """Runner candidates use Task 7a's fenced and inline-code classification."""

    marker = f"EVENT:{event_name}\n"
    text = (
        f"```text\n{marker}```\n"
        if code_context == "fenced"
        else f"Before ``\n{marker}`` after\n"
    )
    transcript = _claude_transcript([{"type": "text", "text": text}])
    normalized = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=transcript,
    )
    assert normalized.complete is True

    candidates = runner.extract_event_markers(
        transcript=transcript,
        native_records=normalized.records,
        trace_records=_native_trace(transcript),
        transcript_id="transcript-1",
        allowed_event_names={event_name},
    )

    assert candidates == ()


def test_web_phase_one_rejects_a_semantically_forged_sync_source_state(
    tmp_path: Path,
) -> None:
    """A shape-valid checkpoint cannot release fixture authority by itself."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        control = Path(environ[runner.CONTROL_ENV])
        phase = json.loads((control / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            def tamper_source_state(after_sync_control: Path) -> None:
                capture = json.loads(
                    (after_sync_control / "brain-shim-capture-index.jsonl").read_text().splitlines()[-1]
                )
                state_path = capture["source_state_path"]
                assert isinstance(state_path, str)
                state_file = after_sync_control / state_path
                state = json.loads(state_file.read_text())
                # Preserve the capture's shallow JSON shape while severing its
                # semantic link to the exact sync result bytes.
                state["result_id"] = "sync_" + "0" * 64
                state_file.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))

            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd, environ, after_sync=tamper_source_state,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        else:
            content = [{"type": "text", "text": "No semantic pass is claimed.\n"}]
            stderr = b""
        transcript = _claude_transcript(content, cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] is None
    assert manifest["approval"] is None
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "fixture_capability" not in {entry["type"] for entry in index["entries"]}


def test_web_phase_one_rejects_a_forged_extractor_registry_capture(
    tmp_path: Path,
) -> None:
    """An internally coherent source state still needs fixed-registry bytes."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        control = Path(environ[runner.CONTROL_ENV])
        phase = json.loads((control / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            def tamper_registry(after_sync_control: Path) -> None:
                capture = json.loads(
                    (after_sync_control / "brain-shim-capture-index.jsonl").read_text().splitlines()[-1]
                )
                state_path = capture["source_state_path"]
                assert isinstance(state_path, str)
                state_file = after_sync_control / state_path
                state = json.loads(state_file.read_text())
                registry = next(
                    entry for entry in state["files"] if entry["path"] == "config/extractors.toml"
                )
                forged = b"[extractors]\ndefault = 'forged'\n"
                (after_sync_control / registry["capture_path"]).write_bytes(forged)
                registry["sha256"] = hashlib.sha256(forged).hexdigest()
                registry["bytes"] = len(forged)
                state_file.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))

            return runner.ProcessCapture(
                exit_code=0,
                stdout=b"",
                stdout_chunks=_initial_sync_receipt_lifecycle_stream(
                    cwd, environ, after_sync=tamper_registry,
                ),
                stderr=b"",
                version=b"2.1.251\n",
                help=b"stream-json\n",
            )
        else:
            content = [{"type": "text", "text": "No semantic pass is claimed.\n"}]
            stderr = b""
        transcript = _claude_transcript(content, cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["fixture_capability_id"] is None
    assert manifest["approval"] is None
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "fixture_capability" not in {entry["type"] for entry in index["entries"]}


def test_web_phase_one_raw_url_attempt_cannot_authorize_phase_two(
    tmp_path: Path,
) -> None:
    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        control = Path(environ[runner.CONTROL_ENV])
        phase = json.loads((control / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        rejected = subprocess.run(
            [
                str(cwd / "brain"), "--json", "source", "snapshot-url",
                "--url", "https://example.invalid/not-a-fixture",
            ],
            cwd=cwd, env=environ, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert rejected.returncode == 2
        transcript = _claude_transcript([{
            "type": "text",
            "text": (
                "The prior standard has no current local evidence.\n"
                "EVENT:report_local_evidence_gap\n"
                "EVENT:ask_web_approval\n"
            ),
        }])
        return runner.ProcessCapture(
            exit_code=0, stdout=transcript, stderr=rejected.stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


def test_web_phase_one_rejects_duplicate_local_sync_before_fixture_release(
    tmp_path: Path,
) -> None:
    """The approval prompt means one local evidence query, then stop."""

    phases: list[str] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            for _ in range(2):
                completed = subprocess.run(
                    [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False,
                )
                assert completed.returncode in {0, 1}, completed.stderr.decode()
            text = (
                "The prior standard has no current local evidence.\n"
                "EVENT:report_local_evidence_gap\n"
                "EVENT:ask_web_approval\n"
            )
        else:
            text = "No semantic pass is claimed.\n"
        transcript = _claude_transcript([{"type": "text", "text": text}])
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=(transcript,),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


def test_web_phase_one_rejects_markers_emitted_before_the_local_sync(
    tmp_path: Path,
) -> None:
    """Approval prose cannot be backfilled by a later valid local query."""

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase != "approval":
            return runner.ProcessCapture(
                exit_code=0, stdout=b"", stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
                stdout_chunks=(_claude_transcript([{"type": "text", "text": "No claim.\n"}]),),
            )
        transcript = _claude_transcript([{
            "type": "text",
            "text": (
                "The prior standard has no current local evidence.\n"
                "EVENT:report_local_evidence_gap\n"
                "EVENT:ask_web_approval\n"
            ),
        }])
        lines = transcript.splitlines(keepends=True)

        def chunks() -> object:
            # Emit the initialization and both approval markers before the
            # shim command begins; a later sync must not retroactively prove
            # that the approval was based on local evidence.
            yield lines[0]
            yield lines[1]
            completed = subprocess.run(
                [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
            assert completed.returncode in {0, 1}, completed.stderr.decode()
            yield lines[2]

        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=chunks(),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "web_approval_phase_unverified" in outcome.log["incomplete_reasons"]


def test_web_phase_one_halts_when_private_transcript_retention_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substitute in-memory transcript cannot authorize fixture release."""

    phases: list[str] = []

    def fail_retention(control: Path, transcript_id: str, raw: bytes) -> bytes:
        del control, transcript_id, raw
        raise runner.RunnerError("simulated transcript retention failure")

    monkeypatch.setattr(runner, "_preserve_transcript", fail_retention)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        text = (
            "The prior standard has no current local evidence.\n"
            "EVENT:report_local_evidence_gap\n"
            "EVENT:ask_web_approval\n"
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=completed.stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
            stdout_chunks=(_claude_transcript([{"type": "text", "text": text}]),),
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval"]
    assert "transcript_preservation_failed" in outcome.log["incomplete_reasons"]


def test_trace_and_native_normalization_fail_closed_without_a_sourced_adapter() -> None:
    trace = runner.SharedTrace(run_id="a" * 64, execution_id="main")
    command_id = trace.command_start()
    trace.command_end(command_id)
    trace.native_record(b'{"type":"assistant"}\n')
    records = trace.records()
    assert [item["sequence"] for item in records] == [1, 2, 3]
    assert runner.validate_trace_records(records) is None

    with pytest.raises(runner.RunnerError, match="trace"):
        runner.validate_trace_records([records[0], records[2]])

    codex = runner.normalize_native_transcript(
        client="codex", version="0.153.0", transcript=b'{"event":"done"}\n'
    )
    assert codex.complete is False
    assert codex.reason == "unsupported_native_format"

    malformed_claude = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=b"not-json\n"
    )
    assert malformed_claude.complete is False
    assert malformed_claude.reason == "unsupported_native_format"


def test_marker_extraction_uses_one_root_assistant_text_block_and_shared_trace() -> None:
    transcript = _claude_transcript([{"type": "text", "text": "Résumé\nEVENT:validate\n"}])
    normalized = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=transcript,
    )
    assert normalized.complete is True
    trace = _native_trace(transcript)

    markers = runner.extract_event_markers(
        transcript=transcript, native_records=normalized.records, trace_records=trace,
        transcript_id="transcript-1", allowed_event_names={"validate"},
    )

    assert len(markers) == 1
    marker = markers[0]
    assert marker.event_name == "validate"
    assert marker.marker == "EVENT:validate\n"
    assert marker.byte_start == len("Résumé\n".encode("utf-8"))
    assert marker.byte_end == len("Résumé\nEVENT:validate\n".encode("utf-8"))
    assert marker.trace_sequence == 2
    assert transcript[marker.native_record_start:marker.native_record_end] == transcript.splitlines(keepends=True)[1]

    ordered_content = [{"type": "text", "text": "ordinary\n"} for _ in range(11)]
    ordered_content[2] = {"type": "text", "text": "EVENT:alpha\n"}
    ordered_content[10] = {"type": "text", "text": "EVENT:beta\n"}
    ordered_transcript = _claude_transcript(ordered_content)
    ordered_normalized = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=ordered_transcript,
    )
    ordered_markers = runner.extract_event_markers(
        transcript=ordered_transcript, native_records=ordered_normalized.records,
        trace_records=_native_trace(ordered_transcript), transcript_id="transcript-1",
        allowed_event_names={"alpha", "beta"},
    )
    assert [candidate.event_name for candidate in ordered_markers] == ["alpha", "beta"]

    split = _claude_transcript([
        {"type": "text", "text": "EVENT:val"},
        {"type": "text", "text": "idate\n"},
    ])
    split_normalized = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=split,
    )
    with pytest.raises(runner.RunnerError, match="malformed transcript marker"):
        runner.extract_event_markers(
            transcript=split, native_records=split_normalized.records, trace_records=_native_trace(split),
            transcript_id="transcript-1", allowed_event_names={"validate"},
        )

    duplicate = _claude_transcript([{"type": "text", "text": "EVENT:validate\nEVENT:validate\n"}])
    duplicate_normalized = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=duplicate,
    )
    with pytest.raises(runner.RunnerError, match="duplicate transcript marker"):
        runner.extract_event_markers(
            transcript=duplicate, native_records=duplicate_normalized.records,
            trace_records=_native_trace(duplicate), transcript_id="transcript-1",
            allowed_event_names={"validate"},
        )


def test_semantic_normalizer_maps_all_noncommand_rule_families_and_fails_closed() -> None:
    """Task 7c selects typed host support; it never invents a CLI command."""

    scenario = {
        "id": "semantic-rule-test",
        "required_events": [
            "report_local_evidence_gap", "ask_web_approval",
            "classify_repository_development", "wiki_no_supported_evidence",
            "branch_extraction_handoff",
        ],
        "receipt_operations": [],
    }
    delivery = runner.SemanticDeliveryEvidence(
        delivery_id="delivery-initial-sync", operation="initial_sync",
        artifact_id="delivery-artifact", items=(
            {
                "item_id": "handoff-extraction", "kind": "extraction",
                "handoff_id": "hnd_" + "a" * 64,
                "handoff_source_id": "src_" + "b" * 64,
                "payload_sha256": "c" * 64,
            },
        ),
    )
    wiki_search_result = {
        "ok": True, "command": "search",
        "data": {
            "run_id": "wiki-search-run", "corpus_revision": "b" * 64,
            "scope": "wiki", "mode": "research", "pass_name": None,
            "terms": ["Alpha"], "page_index": 0, "request_cursor": None,
            "next_cursor": None, "complete": True, "candidate_count": 0,
            "candidate_manifest": ".brain/search-runs/wiki-search-run/candidates.json",
            "candidate_manifest_sha256": "c" * 64, "result_sha256": "d" * 64,
            "matches": [], "searched_source_ids": [], "coverage_gaps": [],
        },
        "warnings": [], "errors": [],
    }
    wiki_search_raw = _encoded(wiki_search_result)
    wiki_search = runner.SemanticCommandEvidence(
        command_id="wiki-search", execution_id="main",
        argv=("./brain", "--json", "search", "--scope", "wiki", "--term", "Alpha"),
        result=wiki_search_result, result_raw=wiki_search_raw,
        result_sha256=hashlib.sha256(wiki_search_raw).hexdigest(),
        start_sequence=2, end_sequence=3,
    )
    evidence = runner.SemanticNormalizationInput(
        run_id="d" * 64,
        scenario=scenario,
        execution_order={"approval": 0, "main": 1},
        markers=(
            _semantic_marker("report_local_evidence_gap", execution_id="approval", trace_sequence=1),
            _semantic_marker("ask_web_approval", execution_id="approval", trace_sequence=2),
            _semantic_marker("classify_repository_development", execution_id="main", trace_sequence=1),
            _semantic_marker("wiki_no_supported_evidence", execution_id="main", trace_sequence=4),
            _semantic_marker("branch_extraction_handoff", execution_id="main", trace_sequence=5),
        ),
        commands=(wiki_search,),
        artifact_types={
            "approval-web": "approval", "development-diff": "diff",
            "wiki-cursor": "cursor_proof", "delivery-artifact": "delivery",
        },
        event_references={
            "ask_web_approval": {"approval_id": "approval-web"},
            "classify_repository_development": {"diff_id": "development-diff"},
            "wiki_no_supported_evidence": {"cursor_proof_id": "wiki-cursor"},
        },
        deliveries=(delivery,),
        cursor_proofs=(runner.SemanticCursorProofEvidence(
            proof_id="wiki-cursor", execution_id="main", family="wiki_search",
            command_ids=("wiki-search",),
        ),),
    )

    plan = runner.plan_semantic_events(evidence)

    assert plan.complete is True
    assert [event["name"] for event in plan.events] == scenario["required_events"]
    assert plan.event_records["event-2"]["approval_id"] == "approval-web"
    assert plan.event_records["event-3"]["diff_id"] == "development-diff"
    assert plan.event_records["event-4"]["cursor_proof_id"] == "wiki-cursor"
    assert plan.events[-1]["data"] == {
        "delivery_id": "delivery-initial-sync",
        "item_id": "handoff-extraction",
        "handoff_id": "hnd_" + "a" * 64,
        "handoff_source_id": "src_" + "b" * 64,
    }

    denied = runner.plan_semantic_events(
        runner.SemanticNormalizationInput(
            run_id=evidence.run_id, scenario=scenario,
            execution_order=evidence.execution_order, markers=evidence.markers,
            commands=(wiki_search,), artifact_types={"approval-web": "approval"},
            event_references={"ask_web_approval": {"approval_id": "approval-web"}},
            deliveries=(),
        )
    )
    assert denied.complete is False
    assert denied.events == ()
    assert "semantic_missing_reference:classify_repository_development:diff_id" in denied.incomplete_reasons
    assert "semantic_missing_delivery:branch_extraction_handoff" in denied.incomplete_reasons


def test_semantic_normalizer_uses_closed_product_command_and_receipt_lifecycle() -> None:
    """A real fixture command can be selected, but arbitrary result data cannot."""

    validate_result = {
        "ok": True,
        "command": "validate",
        "data": {
            "reports": [{"checks": [], "issues": [], "corpus_revision": None}],
        },
        "warnings": [],
        "errors": [],
    }
    validate_command = runner.SemanticCommandEvidence(
        command_id="validate-command", execution_id="main",
        argv=("./brain", "--json", "validate"), result=validate_result,
        result_raw=_encoded(validate_result),
        result_sha256=hashlib.sha256(_encoded(validate_result)).hexdigest(),
        start_sequence=1, end_sequence=2,
    )
    simple = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id="e" * 64,
        scenario={"id": "command-test", "required_events": ["validate"], "receipt_operations": []},
        execution_order={"main": 0},
        markers=(_semantic_marker("validate", execution_id="main", trace_sequence=3),),
        commands=(validate_command,), artifact_types={}, event_references={}, deliveries=(),
    ))
    assert simple.complete is True
    assert simple.event_records["event-1"]["command_id"] == "validate-command"
    assert simple.events[0]["corroboration_ids"] == ["validate-command"]

    # A result that happens to satisfy the event-specific `validate` parser
    # still cannot be selected when its observed outer shim exit would fail
    # Task 7a's command-observation validator.
    failed_exit = runner.SemanticCommandEvidence(
        command_id=validate_command.command_id,
        execution_id=validate_command.execution_id,
        argv=validate_command.argv,
        result=validate_command.result,
        result_raw=validate_command.result_raw,
        result_sha256=validate_command.result_sha256,
        start_sequence=validate_command.start_sequence,
        end_sequence=validate_command.end_sequence,
        exit_code=1,
    )
    failed = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id="e" * 64,
        scenario={"id": "command-test", "required_events": ["validate"], "receipt_operations": []},
        execution_order={"main": 0},
        markers=(_semantic_marker("validate", execution_id="main", trace_sequence=3),),
        commands=(failed_exit,), artifact_types={}, event_references={}, deliveries=(),
    ))
    assert failed.complete is False
    assert "semantic_missing_command:validate" in failed.incomplete_reasons

    out_of_order = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id="e" * 64,
        scenario={"id": "command-test", "required_events": ["validate"], "receipt_operations": []},
        execution_order={"main": 0},
        markers=(_semantic_marker("validate", execution_id="main", trace_sequence=2),),
        commands=(validate_command,), artifact_types={}, event_references={}, deliveries=(),
    ))
    assert out_of_order.complete is False
    assert out_of_order.events == ()
    assert "semantic_missing_command:validate" in out_of_order.incomplete_reasons


def test_semantic_event_record_uses_canonical_result_digest_not_raw_capture_digest() -> None:
    """Task 7a event records join parsed result JSON, not capture framing bytes."""

    result = {
        "ok": True,
        "command": "validate",
        "data": {"reports": [{"checks": [], "issues": [], "corpus_revision": None}]},
        "warnings": [],
        "errors": [],
    }
    raw_with_newline = _encoded(result) + b"\n"
    command = runner.SemanticCommandEvidence(
        command_id="newline-validate", execution_id="main",
        argv=("./brain", "--json", "validate"), result=result,
        result_raw=raw_with_newline,
        result_sha256=hashlib.sha256(raw_with_newline).hexdigest(),
        start_sequence=1, end_sequence=2,
    )

    plan = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id="f" * 64,
        scenario={"id": "newline-command-test", "required_events": ["validate"], "receipt_operations": []},
        execution_order={"main": 0},
        markers=(_semantic_marker("validate", execution_id="main", trace_sequence=3),),
        commands=(command,), artifact_types={}, event_references={}, deliveries=(),
    ))

    assert plan.complete is True
    assert plan.event_records["event-1"]["result_sha256"] == hashlib.sha256(
        runner._canonical_json(result)
    ).hexdigest()
    assert plan.event_records["event-1"]["result_sha256"] != command.result_sha256


def test_semantic_normalizer_binds_cursor_chains_and_explicit_command_aliases(
    tmp_path: Path,
) -> None:
    """A drained event owns a complete chain, not an arbitrary search row."""

    def envelope(data: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "command": "search", "data": data, "warnings": [], "errors": []}

    def page(*, index: int, cursor: str | None, next_cursor: str | None, complete: bool) -> dict[str, object]:
        return {
            "run_id": "search-run", "corpus_revision": "b" * 64,
            "scope": "sources", "mode": "research", "pass_name": "discovery",
            "terms": ["Alpha"], "page_index": index, "request_cursor": cursor,
            "next_cursor": next_cursor, "complete": complete, "candidate_count": 1,
            "candidate_manifest": ".brain/search-runs/search-run/candidates.json",
            "candidate_manifest_sha256": "c" * 64, "result_sha256": "d" * 64,
            "matches": [], "searched_source_ids": [], "coverage_gaps": [],
        }

    def command(
        command_id: str, argv: list[str], result: dict[str, object], start: int, end: int,
    ) -> runner.SemanticCommandEvidence:
        raw = _encoded(result)
        return runner.SemanticCommandEvidence(
            command_id=command_id, execution_id="main", argv=tuple(argv), result=result,
            result_raw=raw, result_sha256=hashlib.sha256(raw).hexdigest(),
            start_sequence=start, end_sequence=end,
        )

    start = command(
        "search-start",
        ["./brain", "--json", "search", "--scope", "sources", "--pass", "discovery", "--term", "Alpha", "--context", "3"],
        envelope(page(index=0, cursor=None, next_cursor="opaque-1", complete=False)), 1, 2,
    )
    finish = command(
        "search-finish", ["./brain", "--json", "search", "--cursor", "opaque-1"],
        envelope(page(index=1, cursor="opaque-1", next_cursor=None, complete=True)), 4, 5,
    )
    control = tmp_path / "cursor-control"
    control.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control, "a" * 64)
    proofs = runner._build_semantic_cursor_proofs(
        writer, run_id="f" * 64, commands=(start, finish),
    )
    assert len(proofs) == 1
    proof = proofs[0]
    assert proof.family == "source_pass_discovery"
    assert proof.command_ids == ("search-start", "search-finish")
    proof_entry = next(entry for entry in writer.entries if entry["id"] == proof.proof_id)
    assert json.loads((writer.run_root / proof_entry["relative_path"]).read_text()) == {
        "run_id": "f" * 64, "execution_id": "main",
        "family": "source_pass_discovery", "command_ids": ["search-start", "search-finish"],
    }
    evidence = runner.SemanticNormalizationInput(
        run_id="f" * 64,
        scenario={
            "id": "cursor-test",
            "required_events": ["source_pass_discovery", "source_pass_discovery_drained"],
            "receipt_operations": [],
        },
        execution_order={"main": 0},
        markers=(
            _semantic_marker("source_pass_discovery", execution_id="main", trace_sequence=3),
            _semantic_marker("source_pass_discovery_drained", execution_id="main", trace_sequence=6),
        ),
        commands=(start, finish), artifact_types={proof.proof_id: "cursor_proof"},
        event_references={"source_pass_discovery_drained": {"cursor_proof_id": proof.proof_id}},
        deliveries=(),
        cursor_proofs=proofs,
    )

    plan = runner.plan_semantic_events(evidence)

    assert plan.complete is True
    assert plan.event_records["event-1"]["command_id"] == "search-start"
    assert plan.event_records["event-2"]["command_id"] == "search-finish"

    # A one-page chain intentionally reuses the one command for its explicit
    # start/drain alias pair.  It must not be rejected merely because its
    # start precedes the first marker.
    one_page_command = command(
        "search-only",
        ["./brain", "--json", "search", "--scope", "sources", "--pass", "discovery", "--term", "Alpha", "--context", "3"],
        envelope(page(index=0, cursor=None, next_cursor=None, complete=True)), 1, 2,
    )
    one_page_writer = runner.EvidenceWriter(control, "b" * 64)
    one_page_proofs = runner._build_semantic_cursor_proofs(
        one_page_writer, run_id="f" * 64, commands=(one_page_command,),
    )
    assert len(one_page_proofs) == 1
    one_page_proof = one_page_proofs[0]
    one_page = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id="f" * 64, scenario=evidence.scenario, execution_order={"main": 0},
        markers=(
            _semantic_marker("source_pass_discovery", execution_id="main", trace_sequence=3),
            _semantic_marker("source_pass_discovery_drained", execution_id="main", trace_sequence=4),
        ),
        commands=(one_page_command,),
        artifact_types={one_page_proof.proof_id: "cursor_proof"},
        event_references={"source_pass_discovery_drained": {"cursor_proof_id": one_page_proof.proof_id}},
        deliveries=(),
        cursor_proofs=one_page_proofs,
    ))
    assert one_page.complete is True
    assert [record["command_id"] for record in one_page.event_records.values()] == [
        "search-only", "search-only",
    ]


def test_semantic_packet_projection_uses_captured_state_and_marker_snapshot(
    tmp_path: Path,
) -> None:
    """An insufficiency packet comes from typed captures, not a late ledger read."""

    from brainlib.contracts import compute_corpus_revision

    prepared = runner.prepare_isolated_workspace(
        "empty-wiki-first-question", scratch_root=tmp_path,
    )
    writer = runner.EvidenceWriter(prepared.control_root, "a" * 64)
    records = runner.LedgerStore(runner.RepoPaths.discover(prepared.workspace)).load_all()
    revision = compute_corpus_revision(records.values())
    state_records: list[dict[str, str]] = []
    for index, path in enumerate(sorted((prepared.workspace / "sources/ledger").glob("src_*.json")), 1):
        artifact_id = f"packet-source-{index}"
        writer.add_bytes(artifact_id, "source_record", path.read_bytes())
        state_records.append({"path": f"sources/ledger/{path.name}", "artifact_id": artifact_id})
    state_files = _captured_source_state_files(
        writer, prepared.workspace, prefix="packet-state",
    )
    writer.add_json("packet-state", "source_state", {
        "schema_version": 1, "run_id": "a" * 64, "execution_id": "main",
        "command_id": "state-command", "capture_point": "after_command_before_return",
        "result_id": None, "corpus_revision": revision,
        "records": state_records, "files": state_files, "pending_result_id": None,
    })
    snapshot = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace, workspace_pin=prepared.workspace_pin, writer=writer,
    ).capture(
        execution_id="main", trace_sequence=5,
        native_record=_marker_snapshot_native(5),
    )
    search_result = {
        "ok": True, "command": "search",
        "data": {
            "run_id": "wiki-search-run", "corpus_revision": revision,
            "scope": "wiki", "mode": "research", "pass_name": None,
            "terms": ["Alpha"], "page_index": 0, "request_cursor": None,
            "next_cursor": None, "complete": True, "candidate_count": 0,
            "candidate_manifest": ".brain/search-runs/wiki-search-run/candidates.json",
            "candidate_manifest_sha256": "c" * 64, "result_sha256": "d" * 64,
            "matches": [], "searched_source_ids": [], "coverage_gaps": [],
        },
        "warnings": [], "errors": [],
    }
    search_raw = _encoded(search_result)
    commands = (
        _state_bound_apply_command(
            writer, run_id="a" * 64, command_id="state-command",
            state_id="packet-state", revision=revision,
        ),
        runner.SemanticCommandEvidence(
            command_id="wiki-search", execution_id="main",
            argv=("./brain", "--json", "search", "--scope", "wiki", "--term", "Alpha"),
            result=search_result, result_raw=search_raw,
            result_sha256=hashlib.sha256(search_raw).hexdigest(),
            start_sequence=3, end_sequence=4,
        ),
    )
    writer.add_json("wiki-proof", "cursor_proof", {
        "run_id": "a" * 64, "execution_id": "main", "family": "wiki_search",
        "command_ids": ["wiki-search"],
    })
    proof = runner.SemanticCursorProofEvidence(
        proof_id="wiki-proof", execution_id="main", family="wiki_search",
        command_ids=("wiki-search",),
    )
    marker = _semantic_marker("judge_insufficient", execution_id="main", trace_sequence=5)

    # Even an otherwise valid checkpoint cannot claim `current` if a later
    # marker snapshot has gained another ledger entry.
    late_raw = b'{"late":"ledger entry"}\n'
    writer.add_bytes("late-ledger-entry", "file_capture", late_raw)
    stale_snapshot = _persist_marker_snapshot(
        writer, snapshot, artifact_id="packet-stale-ledger-snapshot", trace_sequence=5,
        inventory=tuple(sorted(snapshot.inventory + ({
            "path": "sources/ledger/src_late.json", "sha256": hashlib.sha256(late_raw).hexdigest(),
            "bytes": len(late_raw), "content_id": "late-ledger-entry",
        },), key=lambda item: str(item["path"]))),
    )
    assert runner._build_semantic_packet_references(
        writer, run_id="a" * 64,
        required_events=("wiki_search_drained", "wiki_no_supported_evidence", "judge_insufficient"),
        markers=(marker,), commands=commands, cursor_proofs=(proof,),
        marker_snapshots=(stale_snapshot,), execution_order={"main": 0},
    ) == {}

    # A current packet cannot reuse the selected source-state's old raw or
    # extraction bytes merely because its ledger files still match.  The
    # complete captured state must match the exact marker-time snapshot.
    raw_path = next(item["path"] for item in state_files if item["path"].startswith("sources/raw/"))
    raw_entry = next(item for item in snapshot.inventory if item["path"] == raw_path)
    mutated_raw = b"tampered-after-source-checkpoint\n"
    writer.add_bytes("packet-stale-raw", "file_capture", mutated_raw)
    stale_raw_snapshot = _persist_marker_snapshot(
        writer, snapshot, artifact_id="packet-stale-raw-snapshot", trace_sequence=5,
        inventory=tuple(sorted(
            ({
                **raw_entry,
                "sha256": hashlib.sha256(mutated_raw).hexdigest(),
                "bytes": len(mutated_raw),
                "content_id": "packet-stale-raw",
            } if item["path"] == raw_path else item for item in snapshot.inventory),
            key=lambda item: str(item["path"]),
        )),
    )
    assert runner._build_semantic_packet_references(
        writer, run_id="a" * 64,
        required_events=("wiki_search_drained", "wiki_no_supported_evidence", "judge_insufficient"),
        markers=(marker,), commands=commands, cursor_proofs=(proof,),
        marker_snapshots=(stale_raw_snapshot,), execution_order={"main": 0},
    ) == {}

    references = runner._build_semantic_packet_references(
        writer, run_id="a" * 64,
        required_events=("wiki_search_drained", "wiki_no_supported_evidence", "judge_insufficient"),
        markers=(marker,), commands=commands, cursor_proofs=(proof,),
        marker_snapshots=(snapshot,), execution_order={"main": 0},
    )

    packet_id = references["judge_insufficient"]["packet_id"]
    packet_entry = next(entry for entry in writer.entries if entry["id"] == packet_id)
    packet = json.loads((writer.run_root / packet_entry["relative_path"]).read_text())
    assert packet["kind"] == "insufficiency"
    assert packet["reason"] == "no_supported_evidence"
    assert packet["corpus_revision"] == revision
    assert packet["ledger_ids"] == [item["artifact_id"] for item in state_records]
    assert packet["cursor_proof_ids"] == ["wiki-proof"]
    assert packet["snapshot_id"] == snapshot.artifact_id


def test_semantic_packet_projection_revalidates_current_wiki_citations_from_captures(
    tmp_path: Path,
) -> None:
    """The nonempty wiki branch never trusts a live question or stale citation."""

    from brainlib.contracts import compute_corpus_revision

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )
    run_id = "c" * 64
    writer = runner.EvidenceWriter(prepared.control_root, run_id)
    records = runner.LedgerStore(runner.RepoPaths.discover(prepared.workspace)).load_all()
    revision = compute_corpus_revision(records.values())
    state_records: list[dict[str, str]] = []
    for index, path in enumerate(sorted((prepared.workspace / "sources/ledger").glob("src_*.json")), 1):
        artifact_id = f"current-wiki-source-{index}"
        writer.add_bytes(artifact_id, "source_record", path.read_bytes())
        state_records.append({"path": f"sources/ledger/{path.name}", "artifact_id": artifact_id})
    state_files = _captured_source_state_files(
        writer, prepared.workspace, prefix="current-wiki-state",
    )
    writer.add_json("current-wiki-state", "source_state", {
        "schema_version": 1, "run_id": run_id, "execution_id": "main",
        "command_id": "current-wiki-state-command",
        "capture_point": "after_command_before_return", "result_id": None,
        "corpus_revision": revision, "records": state_records,
        "files": state_files, "pending_result_id": None,
    })
    snapshot = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace, workspace_pin=prepared.workspace_pin, writer=writer,
    ).capture(
        execution_id="main", trace_sequence=5,
        native_record=_marker_snapshot_native(5),
    )
    question_path = "wiki/questions/what-is-alpha.md"
    question_entry = next(item for item in snapshot.inventory if item["path"] == question_path)
    search_result = {
        "ok": True, "command": "search",
        "data": {
            "run_id": "current-wiki-search", "corpus_revision": revision,
            "scope": "wiki", "mode": "research", "pass_name": None,
            "terms": ["Alpha"], "page_index": 0, "request_cursor": None,
            "next_cursor": None, "complete": True, "candidate_count": 1,
            "candidate_manifest": ".brain/search-runs/current-wiki-search/candidates.json",
            "candidate_manifest_sha256": "c" * 64, "result_sha256": "d" * 64,
            "matches": [{
                "path": question_path, "line_number": 12,
                "text": "Alpha has a documented local answer.", "kind": "question",
            }],
            "searched_source_ids": [], "coverage_gaps": [],
        },
        "warnings": [], "errors": [],
    }
    search_raw = _encoded(search_result)
    commands = (
        _state_bound_apply_command(
            writer, run_id=run_id, command_id="current-wiki-state-command",
            state_id="current-wiki-state", revision=revision,
        ),
        runner.SemanticCommandEvidence(
            command_id="current-wiki-search", execution_id="main",
            argv=("./brain", "--json", "search", "--scope", "wiki", "--term", "Alpha"),
            result=search_result, result_raw=search_raw,
            result_sha256=hashlib.sha256(search_raw).hexdigest(),
            start_sequence=3, end_sequence=4,
        ),
    )
    writer.add_json("current-wiki-proof", "cursor_proof", {
        "run_id": run_id, "execution_id": "main", "family": "wiki_search",
        "command_ids": ["current-wiki-search"],
    })
    proof = runner.SemanticCursorProofEvidence(
        proof_id="current-wiki-proof", execution_id="main", family="wiki_search",
        command_ids=("current-wiki-search",),
    )
    names = (
        "revalidate_underlying_citations", "build_wiki_evidence_packet", "judge_sufficient",
    )
    markers = tuple(
        _semantic_marker(name, execution_id="main", trace_sequence=index)
        for index, name in enumerate(names, 5)
    )
    marker_snapshots = tuple(
        _persist_marker_snapshot(
            writer, snapshot,
            artifact_id=f"current-wiki-packet-snapshot-{marker.candidate.trace_sequence}",
            trace_sequence=marker.candidate.trace_sequence,
        )
        for marker in markers
    )

    references = runner._build_semantic_packet_references(
        writer, run_id=run_id,
        required_events=("wiki_search_drained", *names),
        markers=markers, commands=commands, cursor_proofs=(proof,),
        marker_snapshots=marker_snapshots, execution_order={"main": 0},
    )

    assert set(references) == set(names)
    for name in names:
        packet_id = references[name]["packet_id"]
        entry = next(item for item in writer.entries if item["id"] == packet_id)
        packet = json.loads((writer.run_root / entry["relative_path"]).read_text())
        assert packet["kind"] == "wiki"
        assert packet["complete"] is True and packet["current"] is True
        snapshot = next(
            item for item in marker_snapshots
            if item.trace_sequence == markers[names.index(name)].candidate.trace_sequence
        )
        assert packet["snapshot_id"] == snapshot.artifact_id
        assert packet["citations"] == [{
            "document_path": question_path,
            "citation_id": "alpha-1",
            "document_id": question_entry["content_id"],
        }]

    source = next(iter(records.values()))
    original = source.active_content_sha256.encode("ascii")
    stale_question = (prepared.workspace / question_path).read_bytes().replace(
        original, b"f" * 64, 1,
    )
    assert stale_question != (prepared.workspace / question_path).read_bytes()
    writer.add_bytes("current-wiki-stale-question", "file_capture", stale_question)
    stale_entry = {
        **question_entry,
        "sha256": hashlib.sha256(stale_question).hexdigest(),
        "bytes": len(stale_question),
        "content_id": "current-wiki-stale-question",
    }
    stale_snapshot = _persist_marker_snapshot(
        writer, snapshot, artifact_id="current-wiki-stale-snapshot", trace_sequence=5,
        inventory=tuple(sorted(
            (stale_entry if item["path"] == question_path else item for item in snapshot.inventory),
            key=lambda item: str(item["path"]),
        )),
    )
    with pytest.raises(runner.RunnerError, match="citation is not current"):
        runner._build_semantic_packet_references(
            writer, run_id=run_id,
            required_events=("wiki_search_drained", "revalidate_underlying_citations"),
            markers=(markers[0],), commands=commands, cursor_proofs=(proof,),
            marker_snapshots=(stale_snapshot,), execution_order={"main": 0},
        )


def test_repository_assertion_projection_rejects_current_wiki_plan_without_indexed_event_support(
    tmp_path: Path,
) -> None:
    """A plan-shaped Python object cannot substitute for indexed event evidence."""

    from brainlib.contracts import compute_corpus_revision

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )
    run_id = "d" * 64
    writer = runner.EvidenceWriter(prepared.control_root, run_id)
    records = runner.LedgerStore(runner.RepoPaths.discover(prepared.workspace)).load_all()
    revision = compute_corpus_revision(records.values())
    state_records: list[dict[str, str]] = []
    for index, path in enumerate(sorted((prepared.workspace / "sources/ledger").glob("src_*.json")), 1):
        artifact_id = f"current-wiki-assertion-source-{index}"
        writer.add_bytes(artifact_id, "source_record", path.read_bytes())
        state_records.append({"path": f"sources/ledger/{path.name}", "artifact_id": artifact_id})
    state_files = _captured_source_state_files(
        writer, prepared.workspace, prefix="current-wiki-assertion-state",
    )
    writer.add_json("current-wiki-assertion-state", "source_state", {
        "schema_version": 1, "run_id": run_id, "execution_id": "main",
        "command_id": "current-wiki-assertion-state-command",
        "capture_point": "after_command_before_return", "result_id": None,
        "corpus_revision": revision, "records": state_records,
        "files": state_files, "pending_result_id": None,
    })
    initial = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace, workspace_pin=prepared.workspace_pin, writer=writer,
    ).capture(execution_id="main", trace_sequence=0)
    question_path = "wiki/questions/what-is-alpha.md"
    question_entry = next(item for item in initial.inventory if item["path"] == question_path)
    updated_question = (prepared.workspace / question_path).read_bytes().replace(
        b"prior_phrasings: [What is Alpha?]",
        b"prior_phrasings: [What is Alpha?, Risk-review phrasing]",
    )
    assert b"Risk-review phrasing" in updated_question
    writer.add_bytes("current-wiki-assertion-question", "file_capture", updated_question)
    updated_entry = {
        **question_entry,
        "sha256": hashlib.sha256(updated_question).hexdigest(),
        "bytes": len(updated_question),
        "content_id": "current-wiki-assertion-question",
    }
    terminal_snapshot = _persist_marker_snapshot(
        writer, initial, artifact_id="current-wiki-assertion-terminal", trace_sequence=5,
        inventory=tuple(sorted(
            (updated_entry if item["path"] == question_path else item for item in initial.inventory),
            key=lambda item: str(item["path"]),
        )),
    )
    scenario = runner._scenario("current-wiki-fast-path")
    terminal_marker = _semantic_marker("validate", execution_id="main", trace_sequence=5)
    state_command = runner.SemanticCommandEvidence(
        command_id="current-wiki-assertion-state-command", execution_id="main",
        argv=("./brain", "--json", "sync"), result={}, result_raw=b"{}",
        result_sha256=hashlib.sha256(b"{}").hexdigest(),
        start_sequence=1, end_sequence=2,
        artifact_ids={"source_state": "current-wiki-assertion-state"},
    )
    plan = runner.SemanticEventPlan(
        events=tuple({"name": name} for name in scenario["required_events"]),
        event_records={}, marker_records={}, receipts=(), deliveries=(), incomplete_reasons=(),
    )

    projection = runner._project_repository_assertions(
        writer,
        run_id=run_id,
        scenario=scenario,
        marker_diff_references={},
        fallback_before=initial,
        fallback_after=terminal_snapshot,
        actual_client_verified=False,
        semantic_plan=plan,
        markers=(terminal_marker,),
        marker_snapshots=(terminal_snapshot,),
        initial_snapshots={"main": initial},
        commands=(state_command,),
    )

    # The copied source state and marker-time snapshot are real captures, but
    # this hand-built plan never emitted the Task 7a event records, packet,
    # manifest, or staged/apply diffs that bind the three scenario assertions.
    # It must therefore remain a visibly incomplete assertion projection.
    assert projection.marker_time_available is False
    assert projection.rules_satisfied is False
    assert all(item["passed"] is False for item in projection.assertions)


def test_repository_assertion_projection_rejects_generic_commands_in_an_indexed_current_plan(
    tmp_path: Path,
) -> None:
    """Indexed rows cannot relabel generic successful commands as event proof."""

    from brainlib.contracts import compute_corpus_revision
    from tests.evals import event_log_contract as contract

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )
    run_id = "e" * 64
    writer = runner.EvidenceWriter(prepared.control_root, run_id)
    scenario = runner._scenario("current-wiki-fast-path")
    records = runner.LedgerStore(runner.RepoPaths.discover(prepared.workspace)).load_all()
    revision = compute_corpus_revision(records.values())
    snapshotter = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace, workspace_pin=prepared.workspace_pin, writer=writer,
    )
    initial = snapshotter.capture(execution_id="main", trace_sequence=0)
    question_path = "wiki/questions/what-is-alpha.md"
    question_entry = next(item for item in initial.inventory if item["path"] == question_path)
    updated_question = (prepared.workspace / question_path).read_bytes().replace(
        b"prior_phrasings: [What is Alpha?]",
        b"prior_phrasings: [What is Alpha?, Explain Alpha for a risk review.]",
    )
    writer.add_bytes("current-indexed-question", "file_capture", updated_question)
    updated_entry = {
        **question_entry,
        "sha256": hashlib.sha256(updated_question).hexdigest(),
        "bytes": len(updated_question),
        "content_id": "current-indexed-question",
    }
    terminal_sequence = 500
    terminal_snapshot = _persist_marker_snapshot(
        writer, initial, artifact_id="current-indexed-terminal", trace_sequence=terminal_sequence,
        inventory=tuple(sorted(
            (updated_entry if item["path"] == question_path else item for item in initial.inventory),
            key=lambda item: str(item["path"]),
        )),
    )

    ledger_ids: list[str] = []
    state_records: list[dict[str, str]] = []
    for index, path in enumerate(sorted((prepared.workspace / "sources/ledger").glob("src_*.json")), 1):
        artifact_id = f"current-indexed-source-{index}"
        writer.add_bytes(artifact_id, "source_record", path.read_bytes())
        ledger_ids.append(artifact_id)
        state_records.append({"path": f"sources/ledger/{path.name}", "artifact_id": artifact_id})
    state_files = _captured_source_state_files(
        writer, prepared.workspace, prefix="current-indexed-state",
    )

    staging_path = (
        ".brain/wiki-staging/wstg_11111111111111111111111111111111/files/"
        + question_path
    )
    writer.add_json("current-stage-diff", "diff", {
        "run_id": run_id, "execution_id": "main", "before": [],
        "after": [{**updated_entry, "path": staging_path}],
        "changed_paths": [staging_path], "staged_paths": [],
    })
    writer.add_json("current-apply-diff", "diff", {
        "run_id": run_id, "execution_id": "main",
        "before": [dict(question_entry)], "after": [dict(updated_entry)],
        "changed_paths": [question_path], "staged_paths": [],
    })
    manifest_body = {
        "schema_version": 1, "expected_corpus_revision": revision,
        "change_intent": "routine", "approval_event_id": None,
        "citation_rewrites": [], "link_candidate_runs": [],
        "changes": [{
            "operation": "write", "path": question_path, "staging_path": staging_path,
            "sha256": updated_entry["sha256"],
        }],
    }
    writer.add_json("current-apply-manifest-body", "file_capture", manifest_body)
    writer.add_json("current-apply-manifest", "wiki_manifest", {
        "path": ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json",
        "content_id": "current-apply-manifest-body",
        "sha256": hashlib.sha256(runner._canonical_json(manifest_body)).hexdigest(),
    })
    writer.add_json("current-reconcile-manifest-body", "file_capture", {
        **manifest_body, "changes": [],
    })
    writer.add_json("current-reconcile-manifest", "wiki_manifest", {
        "path": ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json",
        "content_id": "current-reconcile-manifest-body",
        "sha256": hashlib.sha256(runner._canonical_json({**manifest_body, "changes": []})).hexdigest(),
    })
    for name in (
        "revalidate_underlying_citations", "build_wiki_evidence_packet", "judge_sufficient",
    ):
        writer.add_json("current-" + name + "-packet", "evidence_packet", {
            "run_id": run_id, "execution_id": "main", "kind": "wiki",
            "corpus_revision": revision, "cursor_proof_ids": ["current-wiki-proof"],
            "ledger_ids": ledger_ids,
            "citations": [{
                "document_path": question_path, "citation_id": "alpha-1",
                "document_id": "current-indexed-question",
            }],
            "complete": True, "current": True, "reason": None,
        })
    for artifact_id in ("current-wiki-proof", "current-links-proof"):
        writer.add_json(artifact_id, "cursor_proof", {
            "run_id": run_id, "execution_id": "main", "family": "test", "command_ids": [],
        })

    apply_result = {
        "ok": True, "command": "wiki apply", "data": {
            "corpus_revision": revision, "changed_paths": [question_path],
            "index_path": "wiki/index.md", "recovered": False,
        }, "warnings": [], "errors": [],
    }
    validate_result = {
        "ok": True, "command": "validate", "data": {
            "reports": [{"checks": ["wiki"], "issues": [], "corpus_revision": revision}],
        }, "warnings": [], "errors": [],
    }
    command_by_event: dict[str, runner.SemanticCommandEvidence] = {}
    commands: list[runner.SemanticCommandEvidence] = []
    for index, name in enumerate(scenario["required_events"], 1):
        rule = contract.EVENT_RULES[name]
        if rule.mode not in {"product_cli", "fixture_eval"}:
            continue
        command_id = "current-command-" + str(index)
        if name == "wiki_apply":
            argv, result, artifacts = (
                ("./brain", "--json", "wiki", "apply", "--manifest", ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"),
                apply_result,
                {"source_state": "current-assertion-state", "wiki_manifest": "current-apply-manifest"},
            )
        elif name == "validate":
            argv, result, artifacts = (("./brain", "--json", "validate"), validate_result, {})
        else:
            argv, result, artifacts = (("./brain", "--json", "eval", "placeholder"), {}, {})
        raw = _encoded(result)
        command = runner.SemanticCommandEvidence(
            command_id=command_id, execution_id="main", argv=argv, result=result,
            result_raw=raw, result_sha256=hashlib.sha256(raw).hexdigest(),
            start_sequence=index * 10, end_sequence=index * 10 + 1,
            artifact_ids=artifacts,
        )
        writer.add_bytes(command_id + "-result", "command_result", raw)
        writer.add_json(command_id, "command_observation", {
            "run_id": run_id, "execution_id": "main", "argv": list(argv), "exit_code": 0,
            "result_id": command_id + "-result", "result_sha256": command.result_sha256,
            "timestamp": "2026-09-04T12:00:00Z", "start_sequence": command.start_sequence,
            "end_sequence": command.end_sequence,
            "source_state_id": artifacts.get("source_state"),
        })
        command_by_event[name] = command
        commands.append(command)

    apply_command = command_by_event["wiki_apply"]
    writer.add_json("current-assertion-state", "source_state", {
        "schema_version": 1, "run_id": run_id, "execution_id": "main",
        "command_id": apply_command.command_id,
        "capture_point": "after_command_before_return", "result_id": None,
        "corpus_revision": revision, "records": state_records,
        "files": state_files, "pending_result_id": None,
    })

    markers: list[runner.SemanticMarkerEvidence] = []
    events: list[dict[str, object]] = []
    records_by_id: dict[str, dict[str, object]] = {}
    marker_records: dict[str, dict[str, object]] = {}
    for index, name in enumerate(scenario["required_events"], 1):
        marker = _semantic_marker(
            name, execution_id="main",
            trace_sequence=terminal_sequence if name == "validate" else index * 10 + 5,
        )
        markers.append(marker)
        marker_value = runner._semantic_marker_record(run_id, marker)
        marker_records[marker.marker_id] = marker_value
        writer.add_json(marker.marker_id, "marker", marker_value)
        rule = contract.EVENT_RULES[name]
        references: dict[str, str] = {}
        for key, artifact_type in rule.references:
            if key == "diff_id":
                references[key] = "current-stage-diff" if name == "stage_existing_question_update" else "current-apply-diff"
            elif key == "manifest_id":
                references[key] = "current-apply-manifest" if name == "wiki_apply" else "current-reconcile-manifest"
            elif key == "packet_id":
                references[key] = "current-" + name + "-packet"
            elif key == "cursor_proof_id":
                references[key] = "current-links-proof" if name == "link_candidates_drained" else "current-wiki-proof"
            else:
                artifact_id = "current-" + name + "-" + key
                writer.add_json(artifact_id, artifact_type, {"placeholder": name})
                references[key] = artifact_id
        record_id = "current-event-" + str(index)
        record: dict[str, object] = {
            "run_id": run_id, "execution_id": "main", "event_name": name,
            "marker_id": marker.marker_id, "evidence_mode": rule.mode, **references,
        }
        command = command_by_event.get(name)
        if command is not None:
            record |= {
                "command_id": command.command_id,
                "argv_sha256": runner._sha256(runner._canonical_json(list(command.argv))),
                "result_sha256": command.result_sha256,
            }
        records_by_id[record_id] = record
        writer.add_json(record_id, "event_record", record)
        events.append({
            "sequence": index, "name": name, "execution_id": "main",
            "transcript_marker_id": marker.marker_id, "event_record_id": record_id,
            "corroboration_ids": [] if command is None else [command.command_id],
            "data": {}, "evidence_mode": rule.mode,
        })
    plan = runner.SemanticEventPlan(
        events=tuple(events), event_records=records_by_id, marker_records=marker_records,
        receipts=(), deliveries=(), incomplete_reasons=(),
    )

    projection = runner._project_repository_assertions(
        writer, run_id=run_id, scenario=scenario, marker_diff_references={},
        fallback_before=initial, fallback_after=terminal_snapshot,
        actual_client_verified=False, semantic_plan=plan, markers=tuple(markers),
        marker_snapshots=(terminal_snapshot,), initial_snapshots={"main": initial},
        commands=tuple(commands),
    )

    # The event records, markers, state, and packet artifacts are all indexed,
    # but most command-mode records deliberately carry ``eval placeholder``.
    # The assertion path must rerun Task 7a's event-specific command parser,
    # rather than accepting a hand-built ``SemanticEventPlan`` on shape alone.
    assert projection.marker_time_available is False
    assert projection.rules_satisfied is False
    assert all(item["passed"] is False for item in projection.assertions)
    assert {item["diff_id"] for item in projection.assertions} == {
        "repository-assertion-fallback-diff",
    }


def test_interpretation_approval_is_runner_scripted_and_bound_to_final_manifest(
    tmp_path: Path,
) -> None:
    """A contradictory-fixture decision cannot float free of its final apply."""

    writer = runner.EvidenceWriter(tmp_path, "a" * 64)
    writer.add_json("apply-manifest", "wiki_manifest", {
        "path": ".brain/wiki-staging/wstg_11111111111111111111111111/manifest.json",
        "content_id": "manifest-bytes", "sha256": "a" * 64,
    })
    apply_result = {
        "ok": True, "command": "wiki apply",
        "data": {
            "corpus_revision": "b" * 64,
            "changed_paths": ["wiki/questions/alpha.md"],
            "index_path": "wiki/index.md", "recovered": False,
        },
        "warnings": [], "errors": [],
    }
    raw = _encoded(apply_result)
    apply = runner.SemanticCommandEvidence(
        command_id="final-apply", execution_id="main",
        argv=("./brain", "--json", "wiki", "apply", "--manifest", ".brain/wiki-staging/wstg_11111111111111111111111111/manifest.json"),
        result=apply_result, result_raw=raw, result_sha256=hashlib.sha256(raw).hexdigest(),
        start_sequence=4, end_sequence=5, artifact_ids={"wiki_manifest": "apply-manifest"},
    )
    marker = _semantic_marker("ask_interpretation_approval", execution_id="main", trace_sequence=3)
    scenario = runner._scenario("contradictory-evidence")

    references, approval = runner._build_interpretation_approval_reference(
        writer, run_id="a" * 64, scenario=scenario, markers=(marker,),
        commands=(apply,), execution_order={"main": 0},
    )

    assert references == {
        "ask_interpretation_approval": {
            "approval_id": "interpretation-approval",
            "interpretation_decision_id": runner._PRIVATE_INTERPRETATION_DECISION_ID,
        },
    }
    assert approval == {
        "event_id": "interpretation-withheld",
        "scope": "preserve both sourced limits",
        "note": "Do not approve a preferred interpretation; preserve both sourced limits.",
        "decision": "withheld",
    }
    artifact = next(entry for entry in writer.entries if entry["id"] == "interpretation-approval")
    assert json.loads((writer.run_root / artifact["relative_path"]).read_text())["manifest_id"] == "apply-manifest"
    # The opaque decision identity is predeclared only for planning.  A test
    # process must never persist a public decision artifact merely by
    # exercising the private normalizer.
    assert not [
        entry for entry in writer.entries
        if entry["type"] == "interpretation_decision"
    ]
    assert runner._semantic_command_requires_source_state("wiki_apply", scenario)


def test_interpretation_approval_refuses_a_noncanonical_typed_policy() -> None:
    """A prose-matching scenario cannot mint the private decision reference."""

    scenario = runner._scenario("contradictory-evidence")
    assert runner._scripted_interpretation_approval(scenario) is not None

    scenario["interpretation_policy"] = {
        **scenario["interpretation_policy"],
        "expected_decision": "preferred",
    }

    assert runner._scripted_interpretation_approval(scenario) is None


def test_private_interpretation_decision_identity_is_single_use_and_unindexed(
    tmp_path: Path,
) -> None:
    """The opaque decision slot cannot be reused, replaced, or persisted."""

    scenario = runner._scenario("contradictory-evidence")
    run_id = "a" * 64

    def plan_with(*, decision_id: str = runner._PRIVATE_INTERPRETATION_DECISION_ID,
                  extra_use: bool = False) -> runner.SemanticEventPlan:
        records: dict[str, dict[str, object]] = {}
        events: list[dict[str, object]] = []
        for sequence, name in enumerate(scenario["required_events"], 1):
            record_id = f"event-{sequence}"
            record: dict[str, object] = {"event_name": name}
            if name == "ask_interpretation_approval":
                record.update(
                    approval_id="interpretation-approval",
                    interpretation_decision_id=decision_id,
                )
            if extra_use and name == "validate":
                record["interpretation_decision_id"] = decision_id
            records[record_id] = record
            events.append({
                "sequence": sequence,
                "name": name,
                "event_record_id": record_id,
            })
        return runner.SemanticEventPlan(
            events=tuple(events), event_records=records, marker_records={},
            receipts=(), deliveries=(), incomplete_reasons=(),
        )

    writer = runner.EvidenceWriter(tmp_path / "clean", run_id)
    assert runner._private_interpretation_decision_id(
        writer, scenario=scenario, plan=plan_with(),
    ) == runner._PRIVATE_INTERPRETATION_DECISION_ID

    with pytest.raises(runner.RunnerError, match="interpretation decision"):
        runner._private_interpretation_decision_id(
            writer, scenario=scenario, plan=plan_with(decision_id="other-decision"),
        )
    with pytest.raises(runner.RunnerError, match="interpretation decision"):
        runner._private_interpretation_decision_id(
            writer, scenario=scenario, plan=plan_with(extra_use=True),
        )

    collided = runner.EvidenceWriter(tmp_path / "collided", run_id)
    collided.add_json(runner._PRIVATE_INTERPRETATION_DECISION_ID, "source_record", {})
    with pytest.raises(runner.RunnerError, match="interpretation decision"):
        runner._private_interpretation_decision_id(
            collided, scenario=scenario, plan=plan_with(),
        )


def test_private_overlay_keeps_the_interpretation_decision_ephemeral(
    tmp_path: Path,
) -> None:
    """A private decision has canonical in-memory bytes but no index row."""

    run_id = "b" * 64
    writer = runner.EvidenceWriter(tmp_path, run_id)
    scenario = runner._scenario("contradictory-evidence")
    markers = tuple(
        _semantic_marker(name, execution_id="main", trace_sequence=index)
        for index, name in enumerate(scenario["required_events"], 1)
    )
    records: dict[str, dict[str, object]] = {}
    events: list[dict[str, object]] = []
    marker_records = {
        marker.marker_id: runner._semantic_marker_record(run_id, marker)
        for marker in markers
    }
    for sequence, marker in enumerate(markers, 1):
        record_id = f"event-{sequence}"
        record: dict[str, object] = {
            "run_id": run_id,
            "execution_id": "main",
            "event_name": marker.candidate.event_name,
            "marker_id": marker.marker_id,
            "evidence_mode": "marker",
        }
        if marker.candidate.event_name == "ask_interpretation_approval":
            record.update(
                evidence_mode="marker_approval",
                approval_id="interpretation-approval",
                interpretation_decision_id=runner._PRIVATE_INTERPRETATION_DECISION_ID,
            )
        records[record_id] = record
        events.append({
            "sequence": sequence,
            "name": marker.candidate.event_name,
            "execution_id": "main",
            "transcript_marker_id": marker.marker_id,
            "event_record_id": record_id,
            "corroboration_ids": [],
            "data": {},
            "evidence_mode": record["evidence_mode"],
        })
    plan = runner.SemanticEventPlan(
        events=tuple(events), event_records=records, marker_records=marker_records,
        receipts=(), deliveries=(), incomplete_reasons=(),
    )
    decision = {"schema_version": 1, "private": True}

    assert runner._private_semantic_overlay(
        writer, run_id=run_id, plan=plan, markers=markers,
    ) is None
    overlay = runner._private_semantic_overlay(
        writer, run_id=run_id, plan=plan, markers=markers,
        interpretation_decision=decision,
    )
    assert overlay is not None
    assert overlay[runner._PRIVATE_INTERPRETATION_DECISION_ID].artifact_type == "interpretation_decision"
    control = runner._IndexedSemanticControl(
        writer, run_id=run_id, workspace=tmp_path, overlay=overlay,
    )
    assert control.obj(
        runner._PRIVATE_INTERPRETATION_DECISION_ID, "interpretation_decision",
    ) == decision
    assert not [entry for entry in writer.entries if entry["type"] == "interpretation_decision"]

    writer.add_json("persistent-decision", "interpretation_decision", {"schema_version": 1})
    assert runner._private_semantic_overlay(
        writer, run_id=run_id, plan=plan, markers=markers,
        interpretation_decision=decision,
    ) is None


@pytest.mark.parametrize(
    "persisted_artifact_type",
    ("marker", "event_record", "interpretation_decision"),
)
def test_private_replay_rejects_preexisting_semantic_rows(
    tmp_path: Path,
    persisted_artifact_type: str,
) -> None:
    """A writer-owned semantic row can never mix with a private overlay."""

    fixture = _repository_assertion_indexed_fixture(
        tmp_path,
        run_id="f" * 64,
        paths=tuple(runner._software_paths_for_scenario(
            runner._scenario("repository-development-not-archived"),
        )),
        preexisting_semantic_type=persisted_artifact_type,
        expect_private_replay=False,
    )

    assert fixture.private_replay is None


def test_private_replay_supplies_contract_execution_process_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Private replay gives contract validators the sealed process identity."""

    from tests.evals import event_log_contract as contract

    seen: list[object] = []
    original = contract._validate_interpretation_decision

    def observe(control: object, log: dict[str, object], scenario: dict[str, object], artifact_id: object) -> None:
        seen.append(log.get("executions"))
        original(control, log, scenario, artifact_id)

    monkeypatch.setattr(contract, "_validate_interpretation_decision", observe)

    _repository_assertion_indexed_fixture(
        tmp_path,
        run_id="e" * 64,
        paths=tuple(runner._software_paths_for_scenario(
            runner._scenario("repository-development-not-archived"),
        )),
    )

    assert seen == [[{
        "id": "execution-1",
        "phase": "main",
        "transcript_id": "transcript-1",
        "process_id": "process-1",
    }]]


def test_forced_actual_client_seam_cannot_emit_a_private_interpretation_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An obsolete actual predicate cannot turn ProcessRunner into a pass path.

    The legacy predicate remains monkeypatchable only as a compatibility
    surface for older focused tests. It has no authority: a supplied
    ProcessRunner stays test-process/incomplete even when it offers a
    complete-looking private contradictory plan.
    """

    scenario = runner._scenario("contradictory-evidence")

    def private_decision_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        records: dict[str, dict[str, object]] = {}
        events: list[dict[str, object]] = []
        for sequence, name in enumerate(evidence.scenario["required_events"], 1):
            record_id = f"forced-event-{sequence}"
            record: dict[str, object] = {"event_name": name}
            if name == "ask_interpretation_approval":
                record.update(
                    approval_id="interpretation-approval",
                    interpretation_decision_id=runner._PRIVATE_INTERPRETATION_DECISION_ID,
                )
            records[record_id] = record
            events.append({"sequence": sequence, "name": name, "event_record_id": record_id})
        return runner.SemanticEventPlan(
            events=tuple(events), event_records=records, marker_records={},
            receipts=(), deliveries=(), incomplete_reasons=(),
        )

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, environ, stdin
        transcript = _claude_transcript(
            [{"type": "text", "text": "".join(
                f"EVENT:{name}\n" for name in scenario["required_events"]
            )}],
            cwd=str(cwd.resolve()),
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    # This obsolete predicate must not select the registered-live branch.
    monkeypatch.setattr(
        runner, "_registered_client_process_verified", lambda: True,
        raising=False,
    )
    monkeypatch.setattr(runner, "plan_semantic_events", private_decision_plan)
    monkeypatch.setattr(
        runner, "_indexed_execution_binding", lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        runner, "_emit_semantic_plan",
        lambda *args, **kwargs: pytest.fail("private interpretation reference reached public emission"),
    )
    monkeypatch.setattr(
        runner,
        "_atomic_validate_and_publish_candidate",
        lambda *args, **kwargs: pytest.fail("ProcessRunner reached atomic pass publication"),
    )

    outcome = runner.run_scenario(
        client="claude", scenario_id="contradictory-evidence",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record", "interpretation_decision"} & {
        entry["type"] for entry in index["entries"]
    }


def test_assertion_predicates_require_immutable_citations_and_graph(
    tmp_path: Path,
) -> None:
    """The future-live facts fail closed on each captured semantic mutation.

    This does not manufacture a client pass.  It exercises only the runner's
    deterministic interpretation of immutable `file_capture` artifacts that
    a later registered-client route would already have to seal.
    """

    fixture = ROOT / "tests/evals/fixtures/contradictory-evidence/repo/wiki"
    question_raw = (fixture / "questions/what-is-alpha.md").read_text(encoding="utf-8")
    alpha_raw = (fixture / "pages/alpha.md").read_text(encoding="utf-8")
    index_raw = (fixture / "index.md").read_text(encoding="utf-8")
    cited_question = question_raw.replace(
        "None.\n\n## Related pages",
        "No contrary claim is supported.[^alpha-1]\n\n## Related pages",
    )
    assert cited_question != question_raw

    citation_writer = runner.EvidenceWriter(tmp_path / "citation-control", "a" * 64)
    citation_inventory = _captured_inventory(
        citation_writer,
        {"wiki/questions/what-is-alpha.md": cited_question.encode("utf-8")},
        prefix="citation-capture",
    )
    citation_document = runner._snapshot_wiki_documents(
        citation_writer, citation_inventory,
    )["wiki/questions/what-is-alpha.md"]
    current_identities = frozenset({
        (citation_document.path, "alpha-1", "src_" + "1" * 64,
         "2" * 64, "drv_" + "3" * 64, "line", "1"),
    })
    assert runner._captured_answer_passages_are_exactly_cited(
        citation_document, current_identities,
    ) is True

    uncited_writer = runner.EvidenceWriter(tmp_path / "uncited-control", "b" * 64)
    uncited_inventory = _captured_inventory(
        uncited_writer,
        {
            "wiki/questions/what-is-alpha.md": cited_question.replace(
                "The cited record is retained locally.[^alpha-1]",
                "The cited record is retained locally.",
            ).encode("utf-8"),
        },
        prefix="uncited-capture",
    )
    uncited_document = runner._snapshot_wiki_documents(
        uncited_writer, uncited_inventory,
    )["wiki/questions/what-is-alpha.md"]
    assert runner._captured_answer_passages_are_exactly_cited(
        uncited_document, current_identities,
    ) is False

    graph_writer = runner.EvidenceWriter(tmp_path / "graph-control", "c" * 64)
    graph_inventory = _captured_inventory(
        graph_writer,
        {
            "wiki/index.md": index_raw.encode("utf-8"),
            "wiki/pages/alpha.md": alpha_raw.encode("utf-8"),
            "wiki/questions/what-is-alpha.md": question_raw.encode("utf-8"),
        },
        prefix="graph-capture",
    )
    graph_documents = runner._snapshot_wiki_documents(graph_writer, graph_inventory)
    revision = graph_documents["wiki/questions/what-is-alpha.md"].model.corpus_revision
    assert runner._captured_wiki_graph_is_valid(
        graph_writer, graph_inventory, graph_documents, revision,
    ) is True

    nonreciprocal_writer = runner.EvidenceWriter(tmp_path / "nonreciprocal-control", "d" * 64)
    nonreciprocal_inventory = _captured_inventory(
        nonreciprocal_writer,
        {
            "wiki/index.md": index_raw.encode("utf-8"),
            "wiki/pages/alpha.md": alpha_raw.replace(
                "- [What is Alpha?](../questions/what-is-alpha.md): The durable answer record.\n",
                "",
            ).encode("utf-8"),
            "wiki/questions/what-is-alpha.md": question_raw.encode("utf-8"),
        },
        prefix="nonreciprocal-capture",
    )
    nonreciprocal_documents = runner._snapshot_wiki_documents(
        nonreciprocal_writer, nonreciprocal_inventory,
    )
    assert runner._captured_wiki_graph_is_valid(
        nonreciprocal_writer, nonreciprocal_inventory, nonreciprocal_documents, revision,
    ) is False
    # A parsed document from a separate writer cannot substitute for the
    # terminal inventory's nonreciprocal capture.  The helper must rederive
    # graph input from exactly `nonreciprocal_inventory`, not trust its
    # convenient caller-provided document mapping.
    assert runner._captured_wiki_graph_is_valid(
        nonreciprocal_writer, nonreciprocal_inventory, graph_documents, revision,
    ) is False


def test_assertion_citation_predicate_keeps_adjacent_commonmark_blocks_separate(
    tmp_path: Path,
) -> None:
    """A citation in a list item cannot cover preceding paragraph prose.

    CommonMark permits a paragraph immediately followed by a list without a
    blank line.  They are separate parser blocks even though their physical
    source lines are adjacent, so accepting the list citation for the
    paragraph would turn uncited answer prose into a passing fact.
    """

    fixture = ROOT / "tests/evals/fixtures/contradictory-evidence/repo/wiki"
    question_raw = (fixture / "questions/what-is-alpha.md").read_text(encoding="utf-8")
    adjacent_blocks = question_raw.replace(
        "None.\n\n## Related pages",
        "Unsupported factual sentence.\n- Cited list item.[^alpha-1]\n\n## Related pages",
    )
    assert adjacent_blocks != question_raw

    writer = runner.EvidenceWriter(tmp_path / "adjacent-block-control", "e" * 64)
    inventory = _captured_inventory(
        writer,
        {"wiki/questions/what-is-alpha.md": adjacent_blocks.encode("utf-8")},
        prefix="adjacent-block-capture",
    )
    document = runner._snapshot_wiki_documents(
        writer, inventory,
    )["wiki/questions/what-is-alpha.md"]
    current_identities = frozenset({
        (document.path, "alpha-1", "src_" + "1" * 64,
         "2" * 64, "drv_" + "3" * 64, "line", "1"),
    })

    assert runner._captured_answer_passages_are_exactly_cited(
        document, current_identities,
    ) is False


def test_private_semantic_control_freezes_nested_native_trace_rows(
    tmp_path: Path,
) -> None:
    """A caller cannot mutate text to make a rejected marker validate."""

    writer = runner.EvidenceWriter(tmp_path / "control", "f" * 64)
    expected = "EVENT:validate\n"
    pointer = "/message/content/0/text"
    native_rows = {
        "execution-1": {
            17: {
                "sequence": 1,
                "end": 42,
                "sha256": "a" * 64,
                "texts": {pointer: "not an event\n"},
            },
        },
    }
    control = runner._IndexedSemanticControl(
        writer, run_id="f" * 64, workspace=tmp_path,
        overlay={
            "marker-1": runner._VirtualSemanticArtifact(
                "marker", _encoded({
                    "run_id": "f" * 64,
                    "execution_id": "execution-1",
                    "transcript_id": "transcript-1",
                    "marker": expected,
                    "byte_start": 0,
                    "byte_end": len(expected.encode("utf-8")),
                    "excerpt_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                    "native_record_start": 17,
                    "native_record_end": 42,
                    "native_record_sha256": "a" * 64,
                    "text_pointer": pointer,
                }),
            ),
        },
        native_records=native_rows,
    )
    from tests.evals import event_log_contract as contract

    event = {
        "name": "validate",
        "execution_id": "execution-1",
        "transcript_marker_id": "marker-1",
    }
    execution = {"transcript_id": "transcript-1"}
    with pytest.raises(contract.EventLogContractError):
        contract._validate_marker(control, event, execution, "f" * 64)

    native_rows["execution-1"][17]["texts"][pointer] = expected

    assert control.native_records["execution-1"][17]["texts"][pointer] == "not an event\n"
    with pytest.raises(contract.EventLogContractError):
        contract._validate_marker(control, event, execution, "f" * 64)



def test_contradictory_preference_prose_stays_unproven_without_a_closed_terminal_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural approval facts cannot launder a preferred prose claim.

    The test deliberately supplies every non-decision contradiction fact,
    including a v2-shaped withheld terminal state and both exact citations,
    while the captured body says to prefer the newer limit.  The third public
    assertion must remain false: prose cannot replace the sealed typed
    decision, final checkpoint, and causal joins.
    """

    source_2024 = "src_" + "1" * 64
    source_2025 = "src_" + "2" * 64
    sha_2024, sha_2025 = "3" * 64, "4" * 64
    derivation_2024, derivation_2025 = "drv_" + "5" * 64, "drv_" + "6" * 64
    terminal_question = SimpleNamespace(
        path="wiki/questions/what-is-alpha.md",
        model=SimpleNamespace(
            question_id="question-what-is-alpha", answer_status="conflicted",
            body="2024 limit is 10. 2025 limit is 12. Prefer the 2025 limit 12.",
        ),
    )
    initial_question = SimpleNamespace(
        path=terminal_question.path,
        model=SimpleNamespace(
            question_id="question-what-is-alpha", answer_status="answered", body="2024 limit is 10.",
        ),
    )
    terminal_documents = {terminal_question.path: terminal_question}
    initial_documents = {initial_question.path: initial_question}
    context = SimpleNamespace(
        state=SimpleNamespace(sources={
            source_2024: SimpleNamespace(
                source_id=source_2024,
                current_raw_path=PurePosixPath("alpha-2024.txt"),
                active_content_sha256=sha_2024,
                active_derivation_id=derivation_2024,
            ),
            source_2025: SimpleNamespace(
                source_id=source_2025,
                current_raw_path=PurePosixPath("alpha-2025.txt"),
                active_content_sha256=sha_2025,
                active_derivation_id=derivation_2025,
            ),
        }),
        terminal_inventory={
            "sources/raw/alpha-2024.txt": {},
            "sources/raw/alpha-2025.txt": {},
        },
        initial_inventory={},
    )
    terminal_identities = frozenset({
        (terminal_question.path, "alpha-2024", source_2024, sha_2024, derivation_2024, "line", "1"),
        (terminal_question.path, "alpha-2025", source_2025, sha_2025, derivation_2025, "line", "1"),
    })
    initial_identities = frozenset({
        (initial_question.path, "alpha-2024", source_2024, sha_2024, derivation_2024, "line", "1"),
    })
    staged_path = ".brain/wiki-staging/wstg_11111111111111111111111111/files/wiki/questions/what-is-alpha.md"
    diffs = {
        "preserve_both_claims": SimpleNamespace(changed_paths=(staged_path,)),
        "cite_both_sides": SimpleNamespace(changed_paths=(staged_path,)),
        "wiki_apply": SimpleNamespace(changed_paths=("wiki/index.md", terminal_question.path)),
    }
    monkeypatch.setattr(runner, "_assertion_wiki_documents", lambda _writer, _context: terminal_documents)
    monkeypatch.setattr(runner, "_snapshot_wiki_documents", lambda _writer, _inventory: initial_documents)
    identities = iter((terminal_identities, initial_identities))
    monkeypatch.setattr(
        runner, "_current_citation_identities",
        lambda _writer, *, context, documents: next(identities),
    )
    monkeypatch.setattr(runner, "_assertion_event_diff", lambda _writer, *, context, run_id, name: diffs[name])
    monkeypatch.setattr(runner, "_assertion_positions_in_order", lambda _context, *names: True)
    monkeypatch.setattr(
        runner, "_inventory_capture_bytes",
        lambda _writer, _inventory, path: {
            "sources/raw/alpha-2024.txt": b"2024 Alpha limit is 10.",
            "sources/raw/alpha-2025.txt": b"2025 Alpha limit is 12.",
        }[path],
    )

    assert runner._contradictory_assertion_facts(
        object(), run_id="a" * 64, context=context,
    ) == [True, True, False]


def test_semantic_normalizer_projects_a_real_shim_receipt_lifecycle(
    tmp_path: Path,
) -> None:
    """Receipt roles use four ordered Task 7b observations, never prose."""

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )
    control = runner._phase_control(prepared, phase="main", approval=None)
    state = runner._write_runner_state(
        prepared, run_id="f" * 64, execution_id="main", phase="main",
    )
    writer = runner.EvidenceWriter(prepared.control_root, "f" * 64)
    snapshotter = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace,
        workspace_pin=prepared.workspace_pin,
        writer=writer,
    )
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(prepared.control_root / "home"),
        "LANG": "C.UTF-8", runner.CONTROL_ENV: str(prepared.control_root),
        runner.RUNNER_STATE_ENV: str(state.path),
    }
    trace = runner.ControlTraceWriter(prepared.control_root, "main")

    def invoke(args: list[str], command_id: str) -> tuple[runner.SemanticCommandEvidence, int]:
        completed = subprocess.run(
            [str(prepared.workspace / "brain"), "--json", *args],
            cwd=prepared.workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert completed.returncode in {0, 1}, completed.stderr.decode()
        row = json.loads((prepared.control_root / "brain-shim-capture-index.jsonl").read_text().splitlines()[-1])
        assert row["command_id"]
        native_raw = b"{}\n"
        marker_sequence = trace.append_native_record(
            transcript_id="transcript-main", byte_start=0,
            byte_end=len(native_raw), sha256=hashlib.sha256(native_raw).hexdigest(),
            snapshot_factory=lambda sequence: snapshotter.capture(
                execution_id="main",
                trace_sequence=sequence,
                role="marker",
                native_record={
                    "transcript_id": "transcript-main",
                    "byte_start": 0,
                    "byte_end": len(native_raw),
                    "sha256": hashlib.sha256(native_raw).hexdigest(),
                },
            ).artifact_id,
        )
        return (
            runner.SemanticCommandEvidence(
                command_id=command_id, execution_id="main",
                argv=tuple(row["argv"]), result=json.loads(completed.stdout),
                result_raw=completed.stdout,
                result_sha256=hashlib.sha256(completed.stdout).hexdigest(),
                start_sequence=row["start_sequence"], end_sequence=row["end_sequence"],
                exit_code=completed.returncode,
                    artifact_ids={
                        "sync_stream": "stream-initial" if command_id == "sync" else "",
                        "consumption_receipt": "durable-initial" if command_id == "durable" else "",
                        **({"source_state": "source-state-" + command_id}
                           if command_id in {"sync", "consume"} else {}),
                    },
            ),
            marker_sequence,
        )

    sync, verified_marker = invoke(["sync"], "sync")
    result_id = sync.result["data"]["result_manifest"]["result_id"]
    consume, consumed_marker = invoke(
        ["source", "consume-sync-result", "--result-id", result_id], "consume",
    )
    durable, durable_marker = invoke(
        ["source", "consume-sync-result", "--result-id", result_id], "durable",
    )
    acknowledge, acknowledged_marker = invoke(
        ["source", "acknowledge-sync-result", "--result-id", result_id], "acknowledge",
    )
    runner.verify_phase_control(control)
    native_rows = [row for row in trace.records() if row["kind"] == "native_record"]
    assert len(native_rows) == 4
    assert all(
        runner._indexed_trace_workspace_snapshot(
            writer, execution_id="main", native_row=row,
        ).artifact_id == row["workspace_snapshot_id"]
        for row in native_rows
    )

    normalization = runner.SemanticNormalizationInput(
        run_id="f" * 64,
        scenario={
            "id": "receipt-test",
            "required_events": [
                "initial_sync_receipt_verified", "initial_sync_receipt_consumed",
                "initial_sync_receipt_durable", "initial_sync_receipt_acknowledged",
            ],
            "receipt_operations": ["initial_sync"],
        },
        execution_order={"main": 0},
        markers=(
            _semantic_marker("initial_sync_receipt_verified", execution_id="main", trace_sequence=verified_marker),
            _semantic_marker("initial_sync_receipt_consumed", execution_id="main", trace_sequence=consumed_marker),
            _semantic_marker("initial_sync_receipt_durable", execution_id="main", trace_sequence=durable_marker),
            _semantic_marker("initial_sync_receipt_acknowledged", execution_id="main", trace_sequence=acknowledged_marker),
        ),
        commands=(sync, consume, durable, acknowledge),
        artifact_types={
            "stream-initial": "sync_stream", "durable-initial": "consumption_receipt",
            "source-state-sync": "source_state",
            "source-state-consume": "source_state",
        },
        event_references={}, deliveries=(),
    )
    plan = runner.plan_semantic_events(normalization)

    assert plan.complete is True
    assert plan.receipts == (
        {
            "id": "receipt-initial-sync", "operation": "initial_sync",
            "verify_command_id": "sync", "consume_command_id": "consume",
            "durable_command_id": "durable", "acknowledge_command_id": "acknowledge",
            "delivery_id": None, "stream_artifact_id": "stream-initial",
            "durable_artifact_id": "durable-initial",
        },
    )
    assert [event["corroboration_ids"] for event in plan.events] == [
        ["sync"], ["consume"], ["durable"], ["acknowledge"],
    ]

    missing_states = runner.plan_semantic_events(replace(
        normalization,
        commands=tuple(replace(
            command,
            artifact_ids={key: value for key, value in command.artifact_ids.items()
                          if key != "source_state"},
        ) for command in normalization.commands),
    ))
    assert missing_states.complete is False
    assert "semantic_missing_receipt_source_state:initial_sync:verified" in missing_states.incomplete_reasons

    missing_consume = runner.plan_semantic_events(replace(
        normalization,
        commands=tuple(
            replace(command, artifact_ids={key: value for key, value in command.artifact_ids.items()
                                           if not (command.command_id == "consume" and key == "source_state")})
            for command in normalization.commands
        ),
    ))
    assert missing_consume.complete is False
    assert "semantic_missing_receipt_source_state:initial_sync:consumed" in missing_consume.incomplete_reasons


def test_semantic_normalizer_requires_command_bound_state_for_registration_and_web_publication() -> None:
    """Task 7a's state-dependent commands cannot be corroborated by prose alone."""

    handoff_id = "hnd_" + "a" * 64
    source_id = "src_" + "b" * 64
    content_sha256 = "c" * 64
    derivation_id = "drv_" + "d" * 64
    extracted_path = f"sources/extracted/{content_sha256}/{derivation_id}.md"
    registration_result = {
        "ok": True,
        "command": "source register-extraction",
        "data": {
            "registration": {
                "source_id": source_id,
                "content_sha256": content_sha256,
                "derivation_id": derivation_id,
                "output_path": extracted_path,
                "active_representation": {
                    "source_id": source_id,
                    "content_sha256": content_sha256,
                    "derivation_id": derivation_id,
                    "raw_path": "quarterly.pdf",
                    "extracted_path": extracted_path,
                    "output_sha256": "e" * 64,
                    "quality_state": "ok",
                    "anchors": [{"kind": "page", "value": "1"}],
                },
                "corpus_revision": "f" * 64,
            },
        },
        "warnings": [],
        "errors": [],
    }
    registration_raw = _encoded(registration_result)
    registration = runner.SemanticCommandEvidence(
        command_id="register", execution_id="main",
        argv=(
            "./brain", "--json", "source", "register-extraction", "--handoff-id", handoff_id,
            "--staging-path", f".brain/agent-staging/{handoff_id}/quarterly.md",
            "--anchors-json", '[{"kind":"page","value":"1"}]', "--quality-state", "ok",
            "--note", "Faithful quarterly PDF extraction",
        ),
        result=registration_result, result_raw=registration_raw,
        result_sha256=hashlib.sha256(registration_raw).hexdigest(),
        start_sequence=1, end_sequence=2,
    )
    registration_input = runner.SemanticNormalizationInput(
        run_id="a" * 64,
        scenario={
            "id": "registration-state-test",
            "required_events": ["register_extraction_handoff"],
            "receipt_operations": [],
        },
        execution_order={"main": 0},
        markers=(_semantic_marker("register_extraction_handoff", execution_id="main", trace_sequence=3),),
        commands=(registration,), artifact_types={}, event_references={}, deliveries=(),
    )

    missing_registration_state = runner.plan_semantic_events(registration_input)

    assert missing_registration_state.complete is False
    assert "semantic_missing_command_source_state:register_extraction_handoff" in (
        missing_registration_state.incomplete_reasons
    )
    asserted_registration_state = runner.plan_semantic_events(replace(
        registration_input,
        commands=(replace(registration, artifact_ids={"source_state": "registration-state"}),),
        artifact_types={"registration-state": "source_state"},
    ))
    assert asserted_registration_state.complete is True

    apply_result = {
        "ok": True,
        "command": "wiki apply",
        "data": {
            "corpus_revision": "f" * 64,
            "changed_paths": ["wiki/index.md", "wiki/questions/standard.md"],
            "index_path": "wiki/index.md",
            "recovered": False,
        },
        "warnings": [],
        "errors": [],
    }
    apply_raw = _encoded(apply_result)
    publication = runner.SemanticCommandEvidence(
        command_id="persist", execution_id="approved_capture",
        argv=(
            "./brain", "--json", "wiki", "apply", "--manifest",
            ".brain/wiki-staging/wstg_web/manifest.json",
        ),
        result=apply_result, result_raw=apply_raw,
        result_sha256=hashlib.sha256(apply_raw).hexdigest(),
        start_sequence=1, end_sequence=2,
        artifact_ids={"wiki_manifest": "web-manifest"},
    )
    publication_input = runner.SemanticNormalizationInput(
        run_id="a" * 64,
        scenario={
            "id": "web-approval-and-capture", "network_mode": "mock_only",
            "required_events": ["persist_claim"], "receipt_operations": [],
        },
        execution_order={"approved_capture": 0},
        markers=(_semantic_marker("persist_claim", execution_id="approved_capture", trace_sequence=3),),
        commands=(publication,),
        artifact_types={"web-manifest": "wiki_manifest", "web-diff": "diff"},
        event_references={"persist_claim": {"manifest_id": "web-manifest", "diff_id": "web-diff"}},
        deliveries=(),
    )

    missing_publication_state = runner.plan_semantic_events(publication_input)

    assert missing_publication_state.complete is False
    assert "semantic_missing_command_source_state:persist_claim" in (
        missing_publication_state.incomplete_reasons
    )
    asserted_publication_state = runner.plan_semantic_events(replace(
        publication_input,
        commands=(replace(publication, artifact_ids={
            "wiki_manifest": "web-manifest", "source_state": "publication-state",
        }),),
        artifact_types={
            "web-manifest": "wiki_manifest", "web-diff": "diff",
            "publication-state": "source_state",
        },
    ))
    assert asserted_publication_state.complete is True


def test_semantic_normalizer_projects_delivery_from_the_first_real_consumption(
    tmp_path: Path,
) -> None:
    """A receipt delivery is decoded from captured immutable bytes, not prose.

    The Task 7b shim captures the delivery for both the first consume and the
    durable replay.  Only the first, ``status=consumed`` observation may own
    the projected delivery used by the Task 7a receipt lifecycle.
    """

    prepared = runner.prepare_isolated_workspace(
        "new-binary-before-question", scratch_root=tmp_path,
    )
    runner._phase_control(prepared, phase="main", approval=None)
    state = runner._write_runner_state(
        prepared, run_id="a" * 64, execution_id="main", phase="main",
    )
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(prepared.control_root / "home"),
        "LANG": "C.UTF-8", runner.CONTROL_ENV: str(prepared.control_root),
        runner.RUNNER_STATE_ENV: str(state.path),
    }

    def invoke(args: list[str]) -> tuple[dict[str, object], dict[str, object]]:
        completed = subprocess.run(
            [str(prepared.workspace / "brain"), "--json", *args],
            cwd=prepared.workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        assert completed.returncode in {0, 1}, completed.stderr.decode()
        return json.loads(completed.stdout), json.loads(
            (prepared.control_root / "brain-shim-capture-index.jsonl").read_text().splitlines()[-1]
        )

    sync, sync_row = invoke(["sync"])
    result_id = sync["data"]["result_manifest"]["result_id"]
    consumed, consumed_row = invoke(
        ["source", "consume-sync-result", "--result-id", result_id],
    )
    durable, durable_row = invoke(
        ["source", "consume-sync-result", "--result-id", result_id],
    )
    assert consumed["data"]["status"] == "consumed"
    assert durable["data"]["status"] == "already_consumed"

    receipt = json.loads(
        (prepared.control_root / consumed_row["receipt_artifacts_path"]).read_text()
    )
    assert receipt["delivery"] is not None
    delivery_raw = (
        prepared.control_root / receipt["delivery"]["capture_path"]
    ).read_bytes()
    writer = runner.EvidenceWriter(prepared.control_root, "b" * 64)
    writer.add_bytes("consumed-delivery", "delivery", delivery_raw)

    def evidence(
        command_id: str,
        result: dict[str, object],
        row: dict[str, object],
        artifact_id: str | None,
    ) -> runner.SemanticCommandEvidence:
        raw = _encoded(result) + b"\n"
        # The real shim stdout has a terminal newline; re-read it to retain
        # its exact framing rather than constructing a JSON result ourselves.
        capture_path = prepared.control_root / row["result_path"]
        raw = capture_path.read_bytes()
        return runner.SemanticCommandEvidence(
            command_id=command_id, execution_id="main", argv=tuple(row["argv"]),
            result=result, result_raw=raw, result_sha256=hashlib.sha256(raw).hexdigest(),
            start_sequence=row["start_sequence"], end_sequence=row["end_sequence"],
            exit_code=row["exit_code"],
            artifact_ids={} if artifact_id is None else {"delivery": artifact_id},
        )

    projected = runner._semantic_deliveries_from_commands(
        writer,
        (
            evidence("verify", sync, sync_row, None),
            evidence("consumed", consumed, consumed_row, "consumed-delivery"),
            evidence("durable", durable, durable_row, "consumed-delivery"),
        ),
        ("initial_sync",),
    )

    assert len(projected) == 1
    delivery = projected[0]
    assert delivery.operation == "initial_sync"
    assert delivery.artifact_id == "consumed-delivery"
    assert len(delivery.items) == 1
    assert delivery.items[0]["kind"] == "extraction"
    assert delivery.items[0]["handoff_id"].startswith("hnd_")

    # A durable replay cannot be laundered into the first consumption's
    # delivery evidence if the genuine first-consume capture is unavailable.
    with pytest.raises(runner.RunnerError, match="delivery"):
        runner._semantic_deliveries_from_commands(
            writer,
            (
                evidence("verify", sync, sync_row, None),
                evidence("durable", durable, durable_row, "consumed-delivery"),
            ),
            ("initial_sync",),
        )


def test_fake_process_run_emits_a_versioned_incomplete_log_and_never_a_pass(
    tmp_path: Path,
) -> None:
    seen: list[tuple[list[str], Path, bytes]] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        seen.append((argv, cwd, stdin))
        return runner.ProcessCapture(
            exit_code=0,
            stdout=b'{"event":"pretend-pass"}\n',
            stderr=b"",
            version=b"0.153.0\n",
            help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert len(seen) == 1
    assert seen[0][0][-3:] == ["-C", str(outcome.workspace), "-"]
    assert outcome.log["schema_version"] == 1
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log_path.is_file()
    assert json.loads(outcome.log_path.read_text()) == outcome.log
    assert (
        outcome.control_root / ".brain/eval-transcripts/transcript-1.jsonl"
    ).read_bytes() == b'{"event":"pretend-pass"}\n'


@pytest.mark.parametrize(
    "mutation",
    (
        "profile",
        "allowed_tools",
        "browser",
        "persistence",
        "client_attestation",
        "alternate_mcp",
        "phase_prompt_binding",
    ),
)
def test_malicious_indexed_policy_cannot_reach_semantic_event_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    """Policy/process evidence is a pre-emission gate, not a later audit.

    A minimal sourced Claude transcript is enough to exercise the indexed
    execution seam.  The fake plan is deliberately not a pass fixture: its
    only purpose is to detect whether a forged policy can reach the emitter
    before the runner's normal fail-closed result path takes over.
    """

    original_add_json = runner.EvidenceWriter.add_json
    alternate_mcp: dict[str, object] = {}

    def tampered_add_json(
        writer: runner.EvidenceWriter, artifact_id: str, artifact_type: str, value: object,
    ) -> str:
        if artifact_id == "policy-1" and artifact_type == "policy":
            assert isinstance(value, dict)
            policy = dict(value)
            if mutation == "alternate_mcp":
                alternate_id = "mcp-alternate"
                writer.add_bytes(
                    alternate_id, "mcp_config", runner.EMPTY_MCP_BYTES, suffix=".json",
                )
                entry = runner._writer_entry_map(writer)[alternate_id]
                path = (writer.run_root / entry["relative_path"]).resolve()
                argv = list(policy["argv"])
                argv[argv.index("--mcp-config") + 1] = str(path)
                alternate_mcp.update({
                    "id": alternate_id,
                    "path": str(path),
                    "identity": runner._identity(path),
                    "argv": argv,
                    "argv_sha256": hashlib.sha256(runner._canonical_json(argv)).hexdigest(),
                })
                policy.update({
                    "mcp_config_id": alternate_id,
                    "mcp_config_sha256": hashlib.sha256(runner.EMPTY_MCP_BYTES).hexdigest(),
                    "mcp_path": str(path),
                    "mcp_identity": alternate_mcp["identity"],
                    "argv": argv,
                    "argv_sha256": alternate_mcp["argv_sha256"],
                })
            elif mutation == "profile":
                policy["profile"] = "claude-expanded-tool-surface-v1"
            elif mutation == "client_attestation":
                policy["network_attestation"] = "workspace-egress-denied"
            elif mutation == "phase_prompt_binding":
                policy["phase_prompt_sha256"] = "0" * 64
            else:
                argv = list(policy["argv"])
                if mutation == "allowed_tools":
                    argv[argv.index("--allowedTools") + 1] = "Read,Edit,Write,Glob,Grep,Bash(*)"
                elif mutation == "browser":
                    argv[argv.index("--no-chrome")] = "--chrome"
                elif mutation == "persistence":
                    argv[argv.index("--no-session-persistence")] = "--session-persistence"
                else:  # pragma: no cover - parametrization is closed above
                    raise AssertionError(mutation)
                policy["argv"] = argv
                policy["argv_sha256"] = hashlib.sha256(
                    runner._canonical_json(argv)
                ).hexdigest()
            value = policy
        elif (mutation == "alternate_mcp" and artifact_id == "process-1"
                and artifact_type == "process"):
            assert isinstance(value, dict)
            process = dict(value)
            process.update({
                "argv": alternate_mcp["argv"],
                "argv_sha256": alternate_mcp["argv_sha256"],
                "mcp_identity": alternate_mcp["identity"],
            })
            value = process
        return original_add_json(writer, artifact_id, artifact_type, value)

    emitted: list[runner.SemanticEventPlan] = []

    def fake_plan(*args: object, **kwargs: object) -> runner.SemanticEventPlan:
        del args, kwargs
        return runner.SemanticEventPlan((), {}, {}, (), (), ())

    def record_emission(
        writer: runner.EvidenceWriter, plan: runner.SemanticEventPlan,
    ) -> None:
        del writer
        emitted.append(plan)

    monkeypatch.setattr(runner.EvidenceWriter, "add_json", tampered_add_json)
    monkeypatch.setattr(runner, "plan_semantic_events", fake_plan)
    monkeypatch.setattr(runner, "_emit_semantic_plan", record_emission)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode in {0, 1}
        transcript = _claude_transcript([], cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=completed.stderr,
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert emitted == []
    assert outcome.log["events"] == []
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


@pytest.mark.parametrize("mutation", ("approval", "capability", "descriptor"))
def test_malformed_web_phase_artifact_cannot_reach_semantic_event_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    """Web authority is validated before a complete-looking plan can emit.

    The fake planner is intentionally empty but complete: this isolates the
    ordering boundary.  A malformed typed phase artifact must be rejected by
    the strict indexed binding before ``_emit_semantic_plan`` is reachable.
    """

    original_add_json = runner.EvidenceWriter.add_json
    original_add_bytes = runner.EvidenceWriter.add_bytes
    original_plan = runner.plan_semantic_events

    def tampered_add_json(
        writer: runner.EvidenceWriter, artifact_id: str, artifact_type: str, value: object,
    ) -> str:
        if ((mutation == "approval" and artifact_id == "web-approval" and artifact_type == "approval")
                or (mutation == "capability" and artifact_id == "fixture-capability"
                    and artifact_type == "fixture_capability")):
            assert isinstance(value, dict)
            value = {**value, "scope": "forged-scope"}
        return original_add_json(writer, artifact_id, artifact_type, value)

    def tampered_add_bytes(
        writer: runner.EvidenceWriter, artifact_id: str, artifact_type: str, raw: bytes,
        *, suffix: str = ".bin", fixed_name: str | None = None,
    ) -> str:
        if mutation == "descriptor" and artifact_id == "fixture-descriptor":
            descriptor = json.loads(raw)
            descriptor["static_sha256"] = "0" * 64
            raw = runner._canonical_json(descriptor)
        return original_add_bytes(
            writer, artifact_id, artifact_type, raw, suffix=suffix, fixed_name=fixed_name,
        )

    emitted: list[runner.SemanticEventPlan] = []

    def fake_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        if evidence.scenario.get("id") == "phase-one-initial-sync":
            return original_plan(evidence)
        return runner.SemanticEventPlan((), {}, {}, (), (), ())

    def record_emission(
        writer: runner.EvidenceWriter, plan: runner.SemanticEventPlan,
    ) -> None:
        del writer
        emitted.append(plan)

    monkeypatch.setattr(runner.EvidenceWriter, "add_json", tampered_add_json)
    monkeypatch.setattr(runner.EvidenceWriter, "add_bytes", tampered_add_bytes)
    monkeypatch.setattr(runner, "plan_semantic_events", fake_plan)
    monkeypatch.setattr(runner, "_emit_semantic_plan", record_emission)

    phases: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        phases.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        text = "No semantic pass is claimed.\n"
        transcript = _claude_transcript(
            [{"type": "text", "text": text}], cwd=str(cwd.resolve()),
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert phases == ["approval", "approved_capture"]
    assert emitted == []
    assert outcome.log["events"] == []
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


def test_injected_claude_uses_the_exact_indexed_mcp_config_file(
    tmp_path: Path,
) -> None:
    """The process path, policy path, index artifact, and sealed file agree."""

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, cwd, environ, stdin
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"incomplete"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    policy = json.loads((outcome.log_path.parent / entries["policy-1"]["relative_path"]).read_text())
    process = json.loads((outcome.log_path.parent / entries["process-1"]["relative_path"]).read_text())
    mcp_path = (outcome.log_path.parent / entries[policy["mcp_config_id"]]["relative_path"]).resolve()

    assert Path(policy["mcp_path"]) == mcp_path
    assert policy["argv"][7] == str(mcp_path)
    assert mcp_path.read_bytes() == runner.EMPTY_MCP_BYTES
    assert policy["mcp_config_sha256"] == hashlib.sha256(runner.EMPTY_MCP_BYTES).hexdigest()
    assert process["mcp_identity"] == policy["mcp_identity"] == runner._identity(mcp_path)
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


@pytest.mark.parametrize(
    ("client", "template", "attestation", "transport"),
    (
        ("codex", _codex_template, "workspace-egress-denied", "stdin_utf8"),
        ("claude", _claude_template, "mock-only", "argv_final_utf8"),
    ),
)
def test_injected_launch_uses_and_binds_only_the_sealed_phase_prompt(
    tmp_path: Path,
    client: str,
    template: Callable[[], list[str]],
    attestation: str,
    transport: str,
) -> None:
    """The generated phase envelope, never raw scenario prompt, owns launch input."""

    from tests.evals.phase_prompt_contract import (
        PROMPT_PROTOCOL,
        canonical_phase_prompt_bytes,
        scenario_sha256,
    )

    scenario = runner._scenario("current-wiki-fast-path")
    raw = canonical_phase_prompt_bytes(scenario, "main")
    observed: list[tuple[list[str], bytes]] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del cwd
        prompt_paths = list(
            (Path(environ[runner.CONTROL_ENV]) / "runs").glob(
                "*/artifacts/phase-prompt-1.txt"
            )
        )
        assert len(prompt_paths) == 1
        assert prompt_paths[0].read_bytes() == raw
        observed.append((list(argv), stdin))
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"incomplete"}\n', stderr=b"",
            version=b"2.1.251\n" if client == "claude" else b"0.153.0\n",
            help=b"stream-json\n" if client == "claude" else b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client=client,
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(template()),
        network_attestation=attestation,
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert raw != scenario["prompt"].encode("utf-8")
    assert len(observed) == 1
    argv, stdin = observed[0]
    if client == "codex":
        assert argv[-1] == "-"
        assert stdin == raw
    else:
        assert stdin == b""
        assert argv[-1] == raw.decode("utf-8", "strict")

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    prompt_entry = entries["phase-prompt-1"]
    assert prompt_entry["type"] == "phase_prompt"
    assert (outcome.log_path.parent / prompt_entry["relative_path"]).read_bytes() == raw
    prompt_sha256 = hashlib.sha256(raw).hexdigest()
    policy = json.loads((outcome.log_path.parent / entries["policy-1"]["relative_path"]).read_text())
    process = json.loads((outcome.log_path.parent / entries["process-1"]["relative_path"]).read_text())
    expected_binding = {
        "phase_prompt_id": "phase-prompt-1",
        "phase_prompt_sha256": prompt_sha256,
        "phase_prompt_transport": transport,
    }
    assert {key: policy[key] for key in expected_binding} == expected_binding
    assert {key: process[key] for key in expected_binding} == expected_binding
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["scenario_sha256"] == scenario_sha256(scenario)
    assert manifest["phase_prompt_protocol"] == PROMPT_PROTOCOL
    assert manifest["phase_prompts"] == [{
        "execution_id": "execution-1",
        "phase": "main",
        **expected_binding,
    }]
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["events"] == []


def test_injected_launch_rechecks_the_sealed_phase_prompt_after_spawn(
    tmp_path: Path,
) -> None:
    """A fake process cannot alter its indexed prompt after the launch check."""

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, cwd, stdin
        control = Path(environ[runner.CONTROL_ENV])
        prompts = list((control / "runs").glob("*/artifacts/phase-prompt-1.txt"))
        assert len(prompts) == 1
        prompts[0].write_bytes(b"tampered after spawn\n")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"incomplete"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []


def test_web_launch_binds_distinct_sealed_prompts_for_both_phases(
    tmp_path: Path,
) -> None:
    """Each web phase receives only its own generated envelope and binding."""

    from tests.evals.phase_prompt_contract import (
        PROMPT_PROTOCOL,
        canonical_phase_prompt_bytes,
        scenario_sha256,
    )

    scenario = runner._scenario("web-approval-and-capture")
    expected = {
        phase: canonical_phase_prompt_bytes(scenario, phase)
        for phase in ("approval", "approved_capture")
    }
    observed: list[str] = []

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads(
            (Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text()
        )["phase"]
        prompt_paths = list(
            (Path(environ[runner.CONTROL_ENV]) / "runs").glob(
                f"*/artifacts/phase-prompt-{len(observed) + 1}.txt"
            )
        )
        assert len(prompt_paths) == 1
        assert prompt_paths[0].read_bytes() == expected[phase]
        assert stdin == b""
        assert argv[-1] == expected[phase].decode("utf-8", "strict")
        observed.append(phase)
        if phase == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        transcript = _claude_transcript([], cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert observed == ["approval", "approved_capture"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    assert {
        entry_id for entry_id, entry in entries.items() if entry["type"] == "phase_prompt"
    } == {"phase-prompt-1", "phase-prompt-2"}
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["scenario_sha256"] == scenario_sha256(scenario)
    assert manifest["phase_prompt_protocol"] == PROMPT_PROTOCOL
    assert manifest["phase_prompts"] == [
        {
            "execution_id": f"execution-{index}",
            "phase": phase,
            "phase_prompt_id": f"phase-prompt-{index}",
            "phase_prompt_sha256": hashlib.sha256(expected[phase]).hexdigest(),
            "phase_prompt_transport": "argv_final_utf8",
        }
        for index, phase in enumerate(("approval", "approved_capture"), 1)
    ]
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []


def test_noncanonical_scenario_prompt_cannot_reach_the_process_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sealed prompt source rejects caller-controlled scenario prompt text."""

    original_scenario = runner._scenario

    def noncanonical_scenario(scenario_id: str) -> dict[str, object]:
        value = original_scenario(scenario_id)
        if scenario_id == "current-wiki-fast-path":
            return {**value, "prompt": "attacker-controlled prompt"}
        return value

    monkeypatch.setattr(runner, "_scenario", noncanonical_scenario)
    invoked = False

    def unexpected_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, cwd, environ, stdin
        nonlocal invoked
        invoked = True
        raise AssertionError("noncanonical prompt reached process seam")

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=unexpected_process,
    )

    assert invoked is False
    assert outcome.log["incomplete_reasons"] == ["policy_rejected"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert all(entry["type"] != "phase_prompt" for entry in index["entries"])


def test_fake_marker_diff_uses_the_marker_time_workspace_snapshot_but_stays_incomplete(
    tmp_path: Path,
) -> None:
    """The runner can project host diff evidence without trusting a fake pass."""

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        target = cwd / "brainlib" / "cli.py"
        target.write_bytes(target.read_bytes() + b"\n# fixture development change\n")
        transcript = _claude_transcript([{
            "type": "text",
            "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
        }])
        return runner.ProcessCapture(
            exit_code=0,
            stdout=b"", stdout_chunks=(transcript,),
            stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="repository-development-not-archived",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    diffs = [entry for entry in index["entries"] if entry["type"] == "diff"]
    # The fake transcript's initialization CWD is deliberately not the
    # isolated workspace.  It may exercise marker/diff capture mechanics, but
    # the strict lifecycle replay must leave assertions on their incomplete
    # fallback rather than treating that lookalike transcript as an anchor.
    assert len(diffs) == 3
    first = json.loads((outcome.log_path.parent / diffs[0]["relative_path"]).read_text())
    assert "brainlib/cli.py" in first["changed_paths"]
    # Marker-time diff capture remains useful diagnostics, but the fake
    # transcript's wrong native workspace identity now fails the strict
    # pre-emission execution binding.  It cannot leave marker/event artifacts
    # that a later path might accidentally treat as a semantic claim.
    artifact_types = {entry["type"] for entry in index["entries"]}
    assert "diff" in artifact_types
    assert "event_record" not in artifact_types
    assert "marker" not in artifact_types
    assert outcome.log["events"] == []
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]
    # The runner's scope is policy-owned, but malformed fake native identity
    # cannot make an assertion (or the whole log) pass.
    manifest = json.loads((outcome.log_path.parent / "run-manifest.json").read_text())
    assert manifest["software_paths"] == list(runner._software_paths_for_scenario(
        runner._scenario("repository-development-not-archived")
    ))
    assert [item["text"] for item in outcome.log["repository_assertions"]] == [
        "No wiki question file is added or changed.",
        "No wiki page file is added or changed.",
        "Only requested software-development paths may differ.",
    ]
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    assert {item["diff_id"] for item in outcome.log["repository_assertions"]} == {
        "repository-assertion-fallback-diff"
    }


def test_redirected_marker_trace_sequence_cannot_emit_semantic_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate cannot use a later snapshot than its sealed native row.

    The fake stream has a valid later native record and therefore a real
    marker-time workspace snapshot at that sequence.  Repointing the parser's
    in-memory candidates at it must stop *all* semantic projection before
    marker, event-record, or marker-diff artifacts are written.
    """

    original_extract = runner.extract_event_markers

    def redirected_extract(*args: object, **kwargs: object) -> tuple[runner.MarkerCandidate, ...]:
        candidates = original_extract(*args, **kwargs)
        return tuple(replace(candidate, trace_sequence=candidate.trace_sequence + 1)
                     for candidate in candidates)

    monkeypatch.setattr(runner, "extract_event_markers", redirected_extract)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, environ, stdin
        target = cwd / "brainlib" / "cli.py"
        target.write_bytes(target.read_bytes() + b"\n# redirected marker attempt\n")
        transcript = _claude_transcript([{
            "type": "text",
            "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
        }])
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="repository-development-not-archived",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    semantic_ids = {
        entry["id"] for entry in index["entries"]
        if entry["type"] in {"marker", "event_record"} or entry["id"].startswith("diff-marker-")
    }
    assert semantic_ids == set()
    assert outcome.log["events"] == []
    assert "semantic_marker_trace_unverified" in outcome.log["incomplete_reasons"]


def test_streamed_current_wiki_run_replays_indexed_lifecycle_and_assertion_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake Claude can exercise the full normalizer, but never mint a pass.

    This intentionally does not use the contract/workflow evidence builders.
    The only command records are emitted by the Task 7b root-wrapper shim
    while each native marker is streamed after its corresponding side effect.
    It is therefore a useful end-to-end test of the Task 7c bridge, timeline,
    packet/manifest projection, and assertion evaluator rather than a
    hand-authored candidate log.
    """

    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    forged_bindings: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay
    original_binding = runner._indexed_execution_binding

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        observed_plans.append(plan)
        return plan

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)

    def forge_bound_native_text(*args: object, **kwargs: object) -> object:
        binding = original_binding(*args, **kwargs)
        if binding is None:
            return None
        changed = False
        for rows in binding.control.native_records.values():
            for row in rows.values():
                texts = row.get("texts")
                if not isinstance(texts, dict):
                    continue
                for pointer, value in list(texts.items()):
                    if isinstance(value, str) and "EVENT:" in value:
                        texts[pointer] = value.replace("EVENT:", "FORGED:")
                        changed = True
        assert changed, "the forged-control regression needs a parsed native marker"
        forged_bindings.append(binding)
        return binding

    monkeypatch.setattr(runner, "_indexed_execution_binding", forge_bound_native_text)

    def observe_private_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_private_replay)

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        rg_binary = shutil.which("rg")
        assert rg_binary is not None, "focused semantic integration needs the local rg fixture tool"
        shim_environment = {
            **environ,
            # The runner's actual fake-client environment remains checked by
            # the dedicated hostile-env test.  This private fake invokes the
            # real product shim only to obtain command captures, and search
            # requires its already-installed local fixture tool.
            "PATH": str(Path(rg_binary).parent) + ":" + environ["PATH"],
        }

        template_rows = [
            json.loads(line) for line in _claude_transcript([]).splitlines()
        ]
        # The sourced Task 7a parser binds stream initialization to the
        # runner-created fixture root, not this test helper's default path.
        template_rows[0]["cwd"] = str(cwd)

        def native_marker(name: str, index: int) -> bytes:
            return _encoded({
                "type": "assistant",
                "message": {
                    "model": "runner-test",
                    "content": [{"type": "text", "text": f"EVENT:{name}\n"}],
                },
                "parent_tool_use_id": None,
                "session_id": "runner-test",
                "uuid": f"assistant-{index}",
            }) + b"\n"

        def invoke(args: list[str]) -> dict[str, object]:
            completed = subprocess.run(
                [str(cwd / "brain"), "--json", *args],
                cwd=cwd, env=shim_environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            assert completed.returncode in {0, 1}, completed.stderr.decode()
            result = json.loads(completed.stdout)
            assert result["ok"] is True, result
            return result

        def drain_search(args: list[str], *, links: bool = False) -> dict[str, object]:
            result = invoke(args)
            pages = 1
            while result["data"]["next_cursor"] is not None:
                cursor = result["data"]["next_cursor"]
                result = invoke(
                    ["links", "candidates", "--cursor", cursor]
                    if links else ["search", "--cursor", cursor]
                )
                pages += 1
            result["_page_count"] = pages
            return result

        def chunks() -> object:
            yield _encoded(template_rows[0]) + b"\n"
            marker_index = 1

            sync = invoke(["sync"])
            result_id = sync["data"]["result_manifest"]["result_id"]
            yield native_marker("initial_sync_receipt_verified", marker_index)
            marker_index += 1
            invoke(["source", "consume-sync-result", "--result-id", result_id])
            yield native_marker("initial_sync_receipt_consumed", marker_index)
            marker_index += 1
            invoke(["source", "consume-sync-result", "--result-id", result_id])
            yield native_marker("initial_sync_receipt_durable", marker_index)
            marker_index += 1
            invoke(["source", "acknowledge-sync-result", "--result-id", result_id])
            yield native_marker("initial_sync_receipt_acknowledged", marker_index)
            marker_index += 1

            revision = sync["data"]["result_manifest"]["corpus_revision"]
            reconcile_path = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
            reconcile = cwd / reconcile_path
            reconcile.parent.mkdir(mode=0o700, parents=True)
            reconcile.write_bytes(_encoded({
                "schema_version": 1,
                "expected_corpus_revision": revision,
                "change_intent": "routine",
                "approval_event_id": None,
                "citation_rewrites": [],
                "link_candidate_runs": [],
                "changes": [],
            }) + b"\n")
            invoke(["wiki", "apply", "--manifest", reconcile_path])
            yield native_marker("wiki_reconcile_citations", marker_index)
            marker_index += 1

            drain_search(["search", "--scope", "wiki", "--term", "Alpha"])
            yield native_marker("wiki_search_drained", marker_index)
            marker_index += 1
            yield native_marker("revalidate_underlying_citations", marker_index)
            marker_index += 1
            yield native_marker("build_wiki_evidence_packet", marker_index)
            marker_index += 1
            yield native_marker("judge_sufficient", marker_index)
            marker_index += 1

            question_path = "wiki/questions/what-is-alpha.md"
            original = (cwd / question_path).read_bytes()
            updated = original.replace(
                b"prior_phrasings: [What is Alpha?]",
                b'prior_phrasings: ["What is Alpha?", "Explain Alpha for a risk review."]',
            )
            assert updated != original
            stage_path = (
                ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/"
                + question_path
            )
            staged = cwd / stage_path
            staged.parent.mkdir(mode=0o700, parents=True)
            staged.write_bytes(updated)
            yield native_marker("stage_existing_question_update", marker_index)
            marker_index += 1

            links = drain_search(
                ["links", "candidates", question_path, "--term", "Alpha"], links=True,
            )
            yield native_marker("link_candidates_drained", marker_index)
            marker_index += 1
            link_data = links["data"]
            link_proof = {
                key: link_data[key]
                for key in (
                    "run_id", "corpus_revision", "page_path", "terms",
                    "candidate_manifest_sha256", "candidate_count",
                )
            } | {"page_count": links["_page_count"]}
            apply_path = ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json"
            apply = cwd / apply_path
            apply.write_bytes(_encoded({
                "schema_version": 1,
                "expected_corpus_revision": revision,
                "change_intent": "routine",
                "approval_event_id": None,
                "citation_rewrites": [],
                "link_candidate_runs": [link_proof],
                "changes": [{
                    "operation": "write", "path": question_path,
                    "staging_path": stage_path,
                    "sha256": hashlib.sha256(updated).hexdigest(),
                }],
            }) + b"\n")
            invoke(["wiki", "apply", "--manifest", apply_path])
            yield native_marker("wiki_apply", marker_index)
            marker_index += 1
            invoke(["links", "check"])
            yield native_marker("links_check", marker_index)
            marker_index += 1
            invoke(["validate"])
            yield native_marker("validate", marker_index)
            marker_index += 1

            result_row = dict(template_rows[-1])
            result_row["uuid"] = "result-final"
            yield _encoded(result_row) + b"\n"

        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=chunks(), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert "semantic_assertion_index_unavailable" in outcome.log["incomplete_reasons"]
    # The fake path may retain nonsemantic capture diagnostics, but an
    # unregistered executable cannot publish semantic artifacts or claims.
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record"} & {entry["type"] for entry in index["entries"]}
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    # The private replay must independently rebuild the plan from only the
    # indexed command/artifact evidence before it overlays ephemeral marker
    # rows.  Both the initial candidate and this rebuilt plan must be exact.
    assert len(observed_plans) == 2
    assert all(plan.complete for plan in observed_plans)
    assert observed_plans[0] == observed_plans[1]
    assert [event["name"] for event in observed_plans[0].events] == runner._scenario(
        "current-wiki-fast-path"
    )["required_events"]
    assert not [
        reason for reason in observed_plans[0].incomplete_reasons
        if reason.startswith("semantic_missing_")
    ]
    assert len(private_replays) == 1
    assert private_replays[0] is not None
    # The first pre-emission binding and the replay binding were each forged
    # in memory.  Private replay must ignore both mutable views and rebuild
    # native rows from the sealed transcript/trace instead.
    assert len(forged_bindings) == 2


@pytest.mark.parametrize(
    "scenario_id",
    tuple(
        scenario_id for scenario_id in runner.KNOWN_SCENARIO_IDS
        if scenario_id != "repository-development-not-archived"
    ),
)
def test_marker_only_stream_cannot_produce_a_private_candidate_for_knowledge_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_id: str,
) -> None:
    """A marker-only fake cannot stand in for the fixture-local shim workflow.

    This is the permanent negative control for the all-scenario candidate
    matrix.  It drives each generated knowledge scenario through the real
    stream/parser path, but deliberately omits all ``./brain --json`` calls.
    Markers alone must never create a complete candidate or private replay;
    separate faithful real-shim scripts below establish the positive cases.
    """

    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        observed_plans.append(plan)
        return plan

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)

    def observe_replay(*args: object, **kwargs: object) -> object:
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)

    def marker_only_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        phase = json.loads(
            (Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text(encoding="utf-8")
        )["phase"]
        names = [
            name for name in runner._scenario(scenario_id)["required_events"]
            if name in runner._semantic_events_for_phase(runner._scenario(scenario_id), phase)
        ]
        transcript = _claude_transcript(
            [{"type": "text", "text": "".join(f"EVENT:{name}\n" for name in names)}],
            cwd=str(cwd.resolve()),
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id=scenario_id,
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=marker_only_process,
    )

    assert not any(plan.complete for plan in observed_plans)
    assert private_replays == []
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record"} & {entry["type"] for entry in index["entries"]}


def _faithful_claude_fixture_process(
    scenario_id: str,
    workflow: object,
    *,
    allow_expected_incomplete: Callable[[list[str], dict[str, object]], bool] | None = None,
) -> object:
    """Make a local Claude-shaped stream around real Task 7b shim commands.

    The workflow callback is test-local and yields native marker rows only
    after it has invoked the private fixture's ``./brain --json`` wrapper.
    It cannot make an actual-client pass: callers still inject this process
    through the runner's explicitly incomplete test seam.
    """

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        del argv, stdin
        rg_binary = shutil.which("rg")
        assert rg_binary is not None, "faithful fixture shim needs local rg"
        shim_environment = {
            **environ,
            "PATH": str(Path(rg_binary).parent) + ":" + environ["PATH"],
        }
        phase = json.loads(
            (Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text(encoding="utf-8")
        )["phase"]
        scenario = runner._scenario(scenario_id)
        expected = [
            name for name in scenario["required_events"]
            if name in runner._semantic_events_for_phase(scenario, phase)
        ]
        emitted: list[str] = []
        template_rows = [
            json.loads(line) for line in _claude_transcript([]).splitlines()
        ]
        template_rows[0]["cwd"] = str(cwd.resolve())

        def invoke(args: list[str]) -> dict[str, object]:
            completed = subprocess.run(
                [str(cwd / "brain"), "--json", *args],
                cwd=cwd, env=shim_environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            assert completed.returncode in {0, 1}, completed.stderr.decode()
            result = json.loads(completed.stdout)
            if result["ok"] is not True:
                assert (
                    allow_expected_incomplete is not None
                    and allow_expected_incomplete(args, result)
                ), result
            return result

        def drain(args: list[str], *, links: bool = False) -> dict[str, object]:
            result = invoke(args)
            pages = 1
            while result["data"]["next_cursor"] is not None:
                cursor = result["data"]["next_cursor"]
                result = invoke(
                    ["links", "candidates", "--cursor", cursor]
                    if links else ["search", "--cursor", cursor]
                )
                pages += 1
            result["_page_count"] = pages
            return result

        def marker(name: str, index: int, *, context: str = "") -> bytes:
            assert len(emitted) < len(expected), (scenario_id, phase, name, expected)
            assert name == expected[len(emitted)], (scenario_id, phase, name, expected)
            emitted.append(name)
            text = (context.rstrip("\n") + "\n" if context else "") + f"EVENT:{name}\n"
            return _encoded({
                "type": "assistant",
                "message": {
                    "model": "runner-test",
                    "content": [{"type": "text", "text": text}],
                },
                "parent_tool_use_id": None,
                "session_id": "runner-test",
                "uuid": f"assistant-{phase}-{index}",
            }) + b"\n"

        def marker_group(
            items: tuple[tuple[str, str], ...], index: int,
        ) -> bytes:
            """Emit ordered marker content blocks in one native assistant row.

            The evaluator's marker order is byte/content-block order within a
            native row.  Faithful workflow tests use this only where one
            workspace transition must witness several immediately following
            event rules.
            """

            assert items, (scenario_id, phase, items)
            content: list[dict[str, str]] = []
            for name, context in items:
                assert len(emitted) < len(expected), (scenario_id, phase, name, expected)
                assert name == expected[len(emitted)], (scenario_id, phase, name, expected)
                emitted.append(name)
                text = (context.rstrip("\n") + "\n" if context else "") + f"EVENT:{name}\n"
                content.append({"type": "text", "text": text})
            return _encoded({
                "type": "assistant",
                "message": {"model": "runner-test", "content": content},
                "parent_tool_use_id": None,
                "session_id": "runner-test",
                "uuid": f"assistant-{phase}-{index}-group",
            }) + b"\n"

        setattr(marker, "group", marker_group)

        def chunks() -> object:
            yield _encoded(template_rows[0]) + b"\n"
            yield from workflow(phase, cwd, invoke, drain, marker)
            assert emitted == expected, (scenario_id, phase, emitted, expected)
            result_row = dict(template_rows[-1])
            result_row["uuid"] = "result-" + phase
            yield _encoded(result_row) + b"\n"

        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=chunks(), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    return fake_process


def _shim_manifest(result: dict[str, object]) -> dict[str, object]:
    """Extract a sync-result manifest from a real or fixture-eval envelope."""

    data = result.get("data")
    if not isinstance(data, dict):
        raise AssertionError(result)
    if isinstance(data.get("result_manifest"), dict):
        return data["result_manifest"]
    product = data.get("product_result")
    if isinstance(product, dict) and isinstance(product.get("data"), dict):
        manifest = product["data"].get("result_manifest")
        if isinstance(manifest, dict):
            return manifest
    raise AssertionError(result)


def _write_fixture_manifest(
    cwd: Path,
    relative: str,
    *,
    revision: str,
    changes: list[dict[str, object]] | None = None,
    link_candidate_runs: list[dict[str, object]] | None = None,
    approval_event_id: str | None = None,
) -> None:
    target = cwd / relative
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(_encoded({
        "schema_version": 1,
        "expected_corpus_revision": revision,
        "change_intent": "routine",
        "approval_event_id": approval_event_id,
        "citation_rewrites": [],
        "link_candidate_runs": [] if link_candidate_runs is None else link_candidate_runs,
        "changes": [] if changes is None else changes,
    }) + b"\n")


def _drained_link_proof(result: dict[str, object]) -> dict[str, object]:
    data = result["data"]
    assert isinstance(data, dict)
    return {
        key: data[key]
        for key in (
            "run_id", "corpus_revision", "page_path", "terms",
            "candidate_manifest_sha256", "candidate_count",
        )
    } | {"page_count": result["_page_count"]}


def _faithful_web_workflow(
    phase: str, cwd: Path, invoke: object, drain: object, marker: object,
) -> object:
    """Exercise the complete generated web workflow through local shim APIs.

    Unlike the legacy fixture seam, callers may stream this workflow only
    through a closed fake Popen boundary.  It never supplies an alternate
    executable, probe, approval, or network transport.
    """

    call = invoke
    drain_call = drain
    emit = marker
    assert callable(call) and callable(drain_call) and callable(emit)
    approval = runner._phase_approval(runner._scenario("web-approval-and-capture"))
    assert approval is not None
    marker_index = 1

    def receipt(
        operation: str, verified: dict[str, object], *, expect_handoff: bool = False,
        verified_marker_already_emitted: bool = False,
    ) -> object:
        nonlocal marker_index
        result_id = _shim_manifest(verified)["result_id"]
        assert isinstance(result_id, str)
        if not verified_marker_already_emitted:
            yield emit(operation + "_receipt_verified", marker_index)
            marker_index += 1
        consumed = call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit(operation + "_receipt_consumed", marker_index)
        marker_index += 1
        call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit(operation + "_receipt_durable", marker_index)
        marker_index += 1
        item: dict[str, object] | None = None
        if expect_handoff:
            data = consumed["data"]
            assert isinstance(data, dict)
            delivery = data["handoff_delivery"]
            assert isinstance(delivery, dict)
            delivery_path = delivery["path"]
            assert isinstance(delivery_path, str)
            payload = json.loads((cwd / delivery_path).read_text(encoding="utf-8"))
            items = payload["items"]
            assert isinstance(items, list) and len(items) == 1
            item = items[0]
            assert isinstance(item, dict)
            yield emit(operation + "_handoff_delivery_verified", marker_index)
            marker_index += 1
        call(["source", "acknowledge-sync-result", "--result-id", result_id])
        yield emit(operation + "_receipt_acknowledged", marker_index)
        marker_index += 1
        return item

    if phase == "approval":
        initial = call(["sync"])
        _ = yield from receipt("initial_sync", initial)
        yield emit("report_local_evidence_gap", marker_index,
                   context="The prior standard has no current local evidence.")
        marker_index += 1
        yield emit("ask_web_approval", marker_index)
        return

    assert phase == "approved_capture"
    static = call([
        "eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.initial",
        "--approval-event-id", approval["event_id"], "--approval-scope", approval["scope"],
        "--approval-note", approval["note"],
    ])
    emit_group = getattr(emit, "group", None)
    assert callable(emit_group)
    yield emit_group((("public_web_access", ""), ("select_used_source", ""),
                      ("snapshot_used_source", ""), ("initial_snapshot_receipt_verified", "")),
                     marker_index)
    marker_index += 4
    item = yield from receipt("initial_snapshot", static, expect_handoff=True,
                              verified_marker_already_emitted=True)
    assert isinstance(item, dict) and item["kind"] == "rendered_web_capture"
    handoff_id, source_id = item["handoff_id"], item["source_id"]
    assert isinstance(handoff_id, str) and isinstance(source_id, str)
    yield emit("branch_rendered_web_capture_handoff", marker_index)
    marker_index += 1
    staged = call(["eval", "mock-web-capture", "--fixture-id",
                   "web-approval-and-capture.rendered", "--handoff-id", handoff_id])
    staged_data = staged["data"]
    assert isinstance(staged_data, dict) and isinstance(staged_data["path"], str)
    yield emit("stage_faithful_browser_capture", marker_index)
    marker_index += 1
    rendered = call([
        "source", "snapshot-url", "--source-id", source_id,
        "--rendered-staging-path", staged_data["path"], "--handoff-id", handoff_id,
        "--retrieved-at", "2026-09-04T12:00:00Z", "--final-url", "https://example.test/standard",
        "--detected-media-type", "text/html", "--approval-event-id", approval["event_id"],
        "--approval-scope", approval["scope"], "--approval-note", approval["note"],
    ])
    rendered_data = rendered["data"]
    assert isinstance(rendered_data, dict)
    snapshot = rendered_data["snapshot"]
    assert isinstance(snapshot, dict)
    active = snapshot["active_representation"]
    assert isinstance(active, dict) and active["source_id"] == source_id
    active_raw_path = active["raw_path"]
    assert isinstance(active_raw_path, str)
    yield emit("rendered_snapshot_url", marker_index)
    marker_index += 1
    assert (yield from receipt("rendered_snapshot", rendered)) is None
    yield emit("verify_snapshot_active_representation", marker_index)
    marker_index += 1
    for part, term in (("discovery", "standard"), ("expansion", "limit"),
                       ("verification", "12")):
        drain_call(["search", "--scope", "sources", "--pass", part,
                    "--term", term, "--context", "3"])
        yield emit("source_pass_" + part, marker_index)
        marker_index += 1
        yield emit("source_pass_" + part + "_drained", marker_index)
        marker_index += 1
    question_path = "wiki/questions/external-standard.md"
    original = (cwd / question_path).read_text(encoding="utf-8")
    old_revision = next(line for line in original.splitlines()
                        if line.startswith("corpus_revision: "))
    revision = _shim_manifest(rendered)["corpus_revision"]
    assert isinstance(revision, str)
    body = original.replace(old_revision, "corpus_revision: " + revision)
    body = body.replace("answer_status: partial", "answer_status: answered")
    body = body.replace("verification_terms: [prior]", 'verification_terms: ["12"]')
    body = body.replace("## Current answer\n", "## Current answer\nThe captured current standard now requires limit 12.[^rendered-standard]\n\n")
    record = next(json.loads(path.read_text(encoding="utf-8"))
                  for path in sorted((cwd / "sources/ledger").glob("src_*.json"))
                  if json.loads(path.read_text(encoding="utf-8"))["source_id"] == source_id)
    derivation_id = record["active_derivation_id"]
    derivation = record["derivations"][derivation_id]
    anchor = derivation["anchors"][0]
    body += ("\n[^rendered-standard]: source_id: `" + source_id
             + "`; content_sha256: `" + record["active_content_sha256"]
             + "`; derivation_id: `" + derivation_id + "`; anchor: `"
             + anchor["kind"] + ":" + anchor["value"] + "`; [original](../../sources/raw/"
             + active_raw_path + "); [extracted](../../" + derivation["output_path"]
             + "#" + anchor["kind"] + ":" + anchor["value"] + ")\n")
    staging = ".brain/wiki-staging/wstg_33333333333333333333333333333333"
    staged_question = cwd / staging / "files" / question_path
    staged_question.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged_question.write_text(body, encoding="utf-8")
    links = drain_call(["links", "candidates", question_path, "--term", "standard"], links=True)
    yield emit("link_candidates_drained", marker_index)
    marker_index += 1
    manifest_path = staging + "/manifest.json"
    _write_fixture_manifest(cwd, manifest_path, revision=revision,
                            link_candidate_runs=[_drained_link_proof(links)], changes=[{
                                "operation": "write", "path": question_path,
                                "staging_path": staging + "/files/" + question_path,
                                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            }])
    call(["wiki", "apply", "--manifest", manifest_path])
    yield emit("persist_claim", marker_index)


def test_faithful_contradictory_shim_replay_validates_one_private_v2_decision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The full v2 decision validator accepts sealed private evidence only."""

    from tests.evals import event_log_contract as contract

    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    decision_ids: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay
    original_validator = contract._validate_interpretation_decision

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        observed_plans.append(plan)
        return plan

    def observe_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    def observe_decision(
        control: object,
        log: dict[str, object],
        scenario: dict[str, object],
        artifact_id: object,
    ) -> None:
        if scenario["id"] == "contradictory-evidence":
            decision_ids.append(artifact_id)
        original_validator(control, log, scenario, artifact_id)

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)
    monkeypatch.setattr(contract, "_validate_interpretation_decision", observe_decision)

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        assert phase == "main"
        call = invoke
        drain_call = drain
        emit = marker
        assert callable(call) and callable(drain_call) and callable(emit)
        marker_index = 1

        sync = call(["sync"])
        manifest = _shim_manifest(sync)
        result_id = manifest["result_id"]
        revision = manifest["corpus_revision"]
        assert isinstance(result_id, str) and isinstance(revision, str)
        yield emit("initial_sync_receipt_verified", marker_index)
        marker_index += 1
        call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_consumed", marker_index)
        marker_index += 1
        call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_durable", marker_index)
        marker_index += 1
        call(["source", "acknowledge-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_acknowledged", marker_index)
        marker_index += 1

        reconcile_path = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
        _write_fixture_manifest(cwd, reconcile_path, revision=revision)
        call(["wiki", "apply", "--manifest", reconcile_path])
        yield emit("wiki_reconcile_citations", marker_index)
        marker_index += 1

        drain_call(["search", "--scope", "wiki", "--term", "Alpha"])
        yield emit("wiki_search_drained", marker_index)
        marker_index += 1
        yield emit("revalidate_underlying_citations", marker_index)
        marker_index += 1
        yield emit("judge_insufficient", marker_index)
        marker_index += 1

        for part, term in (
            ("discovery", "Alpha"), ("expansion", "limit"), ("verification", "2025"),
        ):
            drain_call([
                "search", "--scope", "sources", "--pass", part,
                "--term", term, "--context", "3",
            ])
            yield emit("source_pass_" + part, marker_index)
            marker_index += 1
            yield emit("source_pass_" + part + "_drained", marker_index)
            marker_index += 1

        question_path = "wiki/questions/what-is-alpha.md"
        original = (cwd / question_path).read_text(encoding="utf-8")
        old_revision_line = next(
            line for line in original.splitlines()
            if line.startswith("corpus_revision: ")
        )
        body = original.replace("answer_status: answered", "answer_status: conflicted")
        body = body.replace(
            "interpretation_decision: not_applicable",
            "interpretation_decision: unresolved",
        )
        body = body.replace(old_revision_line, "corpus_revision: " + revision)
        body = body.replace("expansion_terms: [Beta]", "expansion_terms: [limit]")
        body = body.replace("verification_terms: [Alpha]", "verification_terms: [2025]")
        body = body.replace(
            "## Current answer\n",
            "## Current answer\n"
            "2024 [Alpha](../pages/alpha.md) limit is 10.[^alpha-1] "
            "2025 Alpha limit is 12.[^alpha-2]\n",
        )
        body = body.replace(
            "## Contradictory evidence\nNone.",
            "## Contradictory evidence\n"
            "2024 [Alpha](../pages/alpha.md) limit is 10.[^alpha-1] "
            "2025 Alpha limit is 12.[^alpha-2]",
        )
        newer = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((cwd / "sources/ledger").glob("src_*.json"))
            if json.loads(path.read_text(encoding="utf-8"))["current_raw_path"] == "alpha-2025.txt"
        )
        derivation = newer["derivations"][newer["active_derivation_id"]]
        anchor = derivation["anchors"][0]
        body += (
            "\n[^alpha-2]: source_id: `" + newer["source_id"]
            + "`; content_sha256: `" + newer["active_content_sha256"]
            + "`; derivation_id: `" + newer["active_derivation_id"]
            + "`; anchor: `" + anchor["kind"] + ":" + anchor["value"]
            + "`; [original](../../sources/raw/" + newer["current_raw_path"]
            + "); [extracted](../../" + derivation["output_path"]
            + "#" + anchor["kind"] + ":" + anchor["value"] + ")\n"
        )
        staged_path = (
            ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/"
            + question_path
        )
        staged = cwd / staged_path
        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Each marker-diff event needs a fresh staged-file mutation.  The
        # temporary comments preserve the fully cited body while exercising
        # distinct marker-time snapshots; the final write restores the exact
        # bytes promoted by the manifest.
        staged.write_text(body + "\n<!-- preserve -->\n", encoding="utf-8")
        yield emit("preserve_both_claims", marker_index)
        marker_index += 1
        staged.write_text(
            body + "\n<!-- preserve -->\n<!-- cite -->\n", encoding="utf-8",
        )
        yield emit("cite_both_sides", marker_index)
        marker_index += 1
        yield emit("ask_interpretation_approval", marker_index)
        marker_index += 1
        staged.write_text(body, encoding="utf-8")
        yield emit("write_wiki_question", marker_index)
        marker_index += 1

        links = drain_call(
            ["links", "candidates", question_path, "--term", "Alpha"], links=True,
        )
        yield emit("link_candidates_drained", marker_index)
        marker_index += 1
        link_data = links["data"]
        assert isinstance(link_data, dict)
        link_proof = {
            key: link_data[key]
            for key in (
                "run_id", "corpus_revision", "page_path", "terms",
                "candidate_manifest_sha256", "candidate_count",
            )
        } | {"page_count": links["_page_count"]}
        apply_path = ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json"
        _write_fixture_manifest(
            cwd, apply_path, revision=revision,
            link_candidate_runs=[link_proof],
            changes=[{
                "operation": "write", "path": question_path,
                "staging_path": staged_path,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }],
        )
        call(["wiki", "apply", "--manifest", apply_path])
        yield emit("wiki_apply", marker_index)
        marker_index += 1
        call(["links", "check"])
        yield emit("links_check", marker_index)
        marker_index += 1
        call(["validate"])
        yield emit("validate", marker_index)

    outcome = runner.run_scenario(
        client="claude", scenario_id="contradictory-evidence",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=_faithful_claude_fixture_process(
            "contradictory-evidence", workflow,
        ),
    )

    if not observed_plans or not all(plan.complete for plan in observed_plans):
        pytest.fail("outcome: " + repr(outcome.log["incomplete_reasons"]) + "\n" + "\n".join(
            "plan %d:\n%s" % (index, "\n".join(plan.incomplete_reasons))
            for index, plan in enumerate(observed_plans, 1)
        ))
    assert private_replays and private_replays[-1] is not None
    assert decision_ids == [runner._PRIVATE_INTERPRETATION_DECISION_ID]
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record", "interpretation_decision"} & {
        entry["type"] for entry in index["entries"]
    }


def test_faithful_empty_wiki_fixture_replays_privately_without_semantic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty-wiki fixture needs real source research, not markers alone."""

    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        observed_plans.append(plan)
        return plan

    def observe_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        assert phase == "main"
        call = invoke
        drain_call = drain
        emit = marker
        assert callable(call) and callable(drain_call) and callable(emit)
        marker_index = 1

        sync = call(["sync"])
        manifest = _shim_manifest(sync)
        result_id = manifest["result_id"]
        revision = manifest["corpus_revision"]
        assert isinstance(result_id, str) and isinstance(revision, str)
        yield emit("initial_sync_receipt_verified", marker_index)
        marker_index += 1
        call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_consumed", marker_index)
        marker_index += 1
        call(["source", "consume-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_durable", marker_index)
        marker_index += 1
        call(["source", "acknowledge-sync-result", "--result-id", result_id])
        yield emit("initial_sync_receipt_acknowledged", marker_index)
        marker_index += 1

        reconcile_path = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
        _write_fixture_manifest(cwd, reconcile_path, revision=revision)
        call(["wiki", "apply", "--manifest", reconcile_path])
        yield emit("wiki_reconcile_citations", marker_index)
        marker_index += 1

        wiki_search = drain_call(["search", "--scope", "wiki", "--term", "Alpha"])
        assert wiki_search["data"]["matches"] == []
        yield emit("wiki_search_drained", marker_index)
        marker_index += 1
        yield emit("wiki_no_supported_evidence", marker_index)
        marker_index += 1
        yield emit("judge_insufficient", marker_index)
        marker_index += 1

        for part, term in (("discovery", "Alpha"), ("expansion", "Beta"), ("verification", "relationship")):
            drain_call([
                "search", "--scope", "sources", "--pass", part,
                "--term", term, "--context", "3",
            ])
            yield emit("source_pass_" + part, marker_index)
            marker_index += 1
            yield emit("source_pass_" + part + "_drained", marker_index)
            marker_index += 1

        ledger_path = next((cwd / "sources/ledger").glob("src_*.json"))
        source = json.loads(ledger_path.read_text(encoding="utf-8"))
        source_id = source["source_id"]
        content = source["active_content_sha256"]
        derivation_id = source["active_derivation_id"]
        derivation = source["derivations"][derivation_id]
        anchor = derivation["anchors"][0]
        question = "wiki/questions/alpha-beta.md"
        staged_path = ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/" + question
        body = f"""---
schema_version: 2
id: question-alpha-beta
title: What does Alpha say about Beta?
description: Alpha documents support for the Beta relationship.
canonical_question: What does Alpha say about Beta?
prior_phrasings: [What does Alpha say about Beta?]
answer_status: answered
interpretation_decision: not_applicable
corpus_revision: {revision}
last_researched: 2026-09-04
discovery_terms: [Alpha]
expansion_terms: [Beta]
verification_terms: [relationship]
---
# What does Alpha say about Beta?

## Current answer
Alpha supports the Beta relationship.[^alpha-beta]

## Supporting evidence
The retained Alpha source explicitly supports that relationship.[^alpha-beta]

## Contradictory evidence
None in the retained source.[^alpha-beta]

## Related pages

## Sources

[^alpha-beta]: source_id: `{source_id}`; content_sha256: `{content}`; derivation_id: `{derivation_id}`; anchor: `{anchor["kind"]}:{anchor["value"]}`; [original](../../sources/raw/{source["current_raw_path"]}); [extracted](../../{derivation["output_path"]}#{anchor["kind"]}:{anchor["value"]})
""".encode("utf-8")
        staged = cwd / staged_path
        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged.write_bytes(body)
        yield emit("write_wiki_question", marker_index)
        marker_index += 1

        links = drain_call(
            ["links", "candidates", question, "--term", "Alpha"], links=True,
        )
        yield emit("link_candidates_drained", marker_index)
        marker_index += 1
        apply_path = ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json"
        _write_fixture_manifest(
            cwd, apply_path, revision=revision,
            link_candidate_runs=[_drained_link_proof(links)],
            changes=[{
                "operation": "write", "path": question,
                "staging_path": staged_path,
                "sha256": hashlib.sha256(body).hexdigest(),
            }],
        )
        call(["wiki", "apply", "--manifest", apply_path])
        yield emit("wiki_apply", marker_index)
        marker_index += 1
        call(["links", "check"])
        yield emit("links_check", marker_index)
        marker_index += 1
        call(["validate"])
        yield emit("validate", marker_index)

    outcome = runner.run_scenario(
        client="claude", scenario_id="empty-wiki-first-question",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=_faithful_claude_fixture_process(
            "empty-wiki-first-question", workflow,
        ),
    )

    assert len(observed_plans) == 2
    assert observed_plans[0].complete is True
    assert observed_plans[0] == observed_plans[1]
    assert [event["name"] for event in observed_plans[0].events] == runner._scenario(
        "empty-wiki-first-question"
    )["required_events"]
    assert private_replays and private_replays == [private_replays[0]]
    assert private_replays[0] is not None
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record"} & {entry["type"] for entry in index["entries"]}


def test_faithful_new_binary_shim_replays_privately_without_semantic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real local extraction handoff can normalize, but cannot publish."""

    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        observed_plans.append(plan)
        return plan

    def observe_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        assert phase == "main"
        call = invoke
        drain_call = drain
        emit = marker
        assert callable(call) and callable(drain_call) and callable(emit)
        marker_index = 1

        def receipt(
            operation: str,
            sync_result: dict[str, object],
            *,
            expect_handoff: bool = False,
        ) -> object:
            nonlocal marker_index
            manifest = _shim_manifest(sync_result)
            result_id = manifest["result_id"]
            assert isinstance(result_id, str)
            yield emit(operation + "_receipt_verified", marker_index)
            marker_index += 1
            consumed = call([
                "source", "consume-sync-result", "--result-id", result_id,
            ])
            yield emit(operation + "_receipt_consumed", marker_index)
            marker_index += 1
            call(["source", "consume-sync-result", "--result-id", result_id])
            yield emit(operation + "_receipt_durable", marker_index)
            marker_index += 1

            item: dict[str, object] | None = None
            if expect_handoff:
                data = consumed["data"]
                assert isinstance(data, dict)
                delivery = data["handoff_delivery"]
                assert isinstance(delivery, dict)
                delivery_path = delivery["path"]
                assert isinstance(delivery_path, str)
                payload = json.loads((cwd / delivery_path).read_text(encoding="utf-8"))
                assert isinstance(payload, dict)
                items = payload["items"]
                assert isinstance(items, list) and len(items) == 1
                item = items[0]
                assert isinstance(item, dict) and item["kind"] == "extraction"
                yield emit(operation + "_handoff_delivery_verified", marker_index)
                marker_index += 1

            call(["source", "acknowledge-sync-result", "--result-id", result_id])
            yield emit(operation + "_receipt_acknowledged", marker_index)
            marker_index += 1
            return item

        initial = call(["sync"])
        data = initial["data"]
        assert isinstance(data, dict) and data["coverage_gap_count"] > 0
        item = yield from receipt("initial_sync", initial, expect_handoff=True)
        assert isinstance(item, dict)
        handoff_id = item["handoff_id"]
        source_id = item["source_id"]
        assert isinstance(handoff_id, str) and isinstance(source_id, str)
        yield emit("branch_extraction_handoff", marker_index)
        marker_index += 1

        extraction_path = ".brain/agent-staging/" + handoff_id + "/quarterly.md"
        extracted = (ROOT / "tests/evals/fixtures/new-binary-before-question/expected-quarterly.md").read_bytes()
        target = cwd / extraction_path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(extracted)
        yield emit("stage_handoff_scoped_extraction", marker_index)
        marker_index += 1

        registration = call([
            "source", "register-extraction", "--handoff-id", handoff_id,
            "--staging-path", extraction_path,
            "--anchors-json", '[{"kind":"page","value":"1"}]',
            "--quality-state", "ok",
            "--note", "Faithful quarterly PDF extraction",
        ])
        registration_data = registration["data"]
        assert isinstance(registration_data, dict)
        registered = registration_data["registration"]
        assert isinstance(registered, dict)
        active = registered["active_representation"]
        assert isinstance(active, dict) and active["source_id"] == source_id
        yield emit("register_extraction_handoff", marker_index)
        marker_index += 1
        yield emit("verify_registration_active_representation", marker_index)
        marker_index += 1

        post_registration = call(["sync"])
        _ = yield from receipt("post_registration_sync", post_registration)
        revision = _shim_manifest(post_registration)["corpus_revision"]
        assert isinstance(revision, str)

        reconcile_path = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
        _write_fixture_manifest(cwd, reconcile_path, revision=revision)
        call(["wiki", "apply", "--manifest", reconcile_path])
        yield emit("wiki_reconcile_citations", marker_index)
        marker_index += 1

        drain_call([
            "search", "--scope", "sources", "--freshness",
            "--source-id", source_id, "--term", "Alpha",
        ])
        yield emit("freshness_search_batched", marker_index)
        marker_index += 1
        yield emit("freshness_search_drained", marker_index)
        marker_index += 1

        drain_call(["search", "--scope", "wiki", "--term", "Alpha"])
        yield emit("wiki_search_drained", marker_index)
        marker_index += 1
        yield emit("revalidate_underlying_citations", marker_index)
        marker_index += 1
        yield emit("judge_insufficient", marker_index)
        marker_index += 1

        for part, term in (
            ("discovery", "Alpha"), ("expansion", "quarterly"),
            ("verification", "limit"),
        ):
            drain_call([
                "search", "--scope", "sources", "--pass", part,
                "--term", term, "--context", "3",
            ])
            yield emit("source_pass_" + part, marker_index)
            marker_index += 1
            yield emit("source_pass_" + part + "_drained", marker_index)
            marker_index += 1

        question_path = "wiki/questions/what-is-alpha.md"
        original = (cwd / question_path).read_text(encoding="utf-8")
        old_revision_line = next(
            line for line in original.splitlines()
            if line.startswith("corpus_revision: ")
        )
        body = original.replace(old_revision_line, "corpus_revision: " + revision)
        body = body.replace("expansion_terms: [Beta]", "expansion_terms: [quarterly]")
        body = body.replace("verification_terms: [Alpha]", "verification_terms: [limit]")
        body = body.replace(
            "## Current answer\n",
            "## Current answer\n"
            "The new quarterly PDF sets the [Alpha](../pages/alpha.md) limit to 12.[^quarterly]\n\n",
        )
        quarterly = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((cwd / "sources/ledger").glob("src_*.json"))
            if json.loads(path.read_text(encoding="utf-8"))["current_raw_path"] == "quarterly.pdf"
        )
        derivation_id = quarterly["active_derivation_id"]
        derivation = quarterly["derivations"][derivation_id]
        anchor = derivation["anchors"][0]
        body += (
            "\n[^quarterly]: source_id: `" + quarterly["source_id"]
            + "`; content_sha256: `" + quarterly["active_content_sha256"]
            + "`; derivation_id: `" + derivation_id
            + "`; anchor: `" + anchor["kind"] + ":" + anchor["value"]
            + "`; [original](../../sources/raw/" + quarterly["current_raw_path"]
            + "); [extracted](../../" + derivation["output_path"]
            + "#" + anchor["kind"] + ":" + anchor["value"] + ")\n"
        )
        staging_path = (
            ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/"
            + question_path
        )
        staged = cwd / staging_path
        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged.write_text(body, encoding="utf-8")
        yield emit("write_wiki_question", marker_index)
        marker_index += 1

        links = drain_call(
            ["links", "candidates", question_path, "--term", "Alpha"], links=True,
        )
        yield emit("link_candidates_drained", marker_index)
        marker_index += 1
        link_data = links["data"]
        assert isinstance(link_data, dict)
        link_proof = {
            key: link_data[key]
            for key in (
                "run_id", "corpus_revision", "page_path", "terms",
                "candidate_manifest_sha256", "candidate_count",
            )
        } | {"page_count": links["_page_count"]}
        apply_path = ".brain/wiki-staging/wstg_22222222222222222222222222222222/manifest.json"
        _write_fixture_manifest(
            cwd, apply_path, revision=revision,
            link_candidate_runs=[link_proof],
            changes=[{
                "operation": "write", "path": question_path,
                "staging_path": staging_path,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }],
        )
        call(["wiki", "apply", "--manifest", apply_path])
        yield emit("wiki_apply", marker_index)
        marker_index += 1
        call(["links", "check"])
        yield emit("links_check", marker_index)
        marker_index += 1
        call(["validate"])
        yield emit("validate", marker_index)

    def allow_initial_coverage_gap(
        args: list[str], result: dict[str, object],
    ) -> bool:
        """The documented first sync reports its extraction gap as incomplete."""

        data = result.get("data")
        return (
            args == ["sync"]
            and result.get("ok") is False
            and isinstance(data, dict)
            and data.get("coverage_gap_count") == 1
            and isinstance(data.get("result_manifest"), dict)
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="new-binary-before-question",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=_faithful_claude_fixture_process(
            "new-binary-before-question", workflow,
            allow_expected_incomplete=allow_initial_coverage_gap,
        ),
    )

    assert len(observed_plans) == 2
    assert all(plan.complete for plan in observed_plans)
    assert observed_plans[0] == observed_plans[1]
    assert [event["name"] for event in observed_plans[0].events] == runner._scenario(
        "new-binary-before-question"
    )["required_events"]
    # Two sync lifecycles intentionally share the same command grammar.  The
    # sealed manifest result ID, rather than generic argv matching, must bind
    # the extraction delivery only to the first (initial) lifecycle.
    assert [receipt["operation"] for receipt in observed_plans[0].receipts] == [
        "initial_sync", "post_registration_sync",
    ]
    assert [receipt["delivery_id"] is not None for receipt in observed_plans[0].receipts] == [
        True, False,
    ]
    assert len(observed_plans[0].deliveries) == 1
    assert observed_plans[0].deliveries[0]["receipt_id"] == "receipt-initial-sync"
    assert len(private_replays) == 1
    assert private_replays[0] is not None
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record", "interpretation_decision"} & {
        entry["type"] for entry in index["entries"]
    }


def test_faithful_web_shim_replays_privately_without_semantic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two-phase fixture workflow normalizes privately after its real gate."""

    scenario = runner._scenario("web-approval-and-capture")
    approval = runner._phase_approval(scenario)
    assert approval is not None
    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        # The phase-one authority gate deliberately plans its narrow receipt
        # internally.  This test observes only the final all-scenario plan and
        # its exact private rebuild.
        if evidence.scenario.get("id") == scenario["id"]:
            observed_plans.append(plan)
        return plan

    def observe_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        call = invoke
        drain_call = drain
        emit = marker
        assert callable(call) and callable(drain_call) and callable(emit)
        marker_index = 1

        def receipt(
            operation: str,
            verified: dict[str, object],
            *,
            expect_handoff: bool = False,
            verified_marker_already_emitted: bool = False,
        ) -> object:
            nonlocal marker_index
            result_id = _shim_manifest(verified)["result_id"]
            assert isinstance(result_id, str)
            if not verified_marker_already_emitted:
                yield emit(operation + "_receipt_verified", marker_index)
                marker_index += 1
            consumed = call([
                "source", "consume-sync-result", "--result-id", result_id,
            ])
            yield emit(operation + "_receipt_consumed", marker_index)
            marker_index += 1
            call(["source", "consume-sync-result", "--result-id", result_id])
            yield emit(operation + "_receipt_durable", marker_index)
            marker_index += 1

            item: dict[str, object] | None = None
            if expect_handoff:
                data = consumed["data"]
                assert isinstance(data, dict)
                delivery = data["handoff_delivery"]
                assert isinstance(delivery, dict)
                delivery_path = delivery["path"]
                assert isinstance(delivery_path, str)
                payload = json.loads((cwd / delivery_path).read_text(encoding="utf-8"))
                assert isinstance(payload, dict)
                items = payload["items"]
                assert isinstance(items, list) and len(items) == 1
                item = items[0]
                assert isinstance(item, dict)
                yield emit(operation + "_handoff_delivery_verified", marker_index)
                marker_index += 1

            call(["source", "acknowledge-sync-result", "--result-id", result_id])
            yield emit(operation + "_receipt_acknowledged", marker_index)
            marker_index += 1
            return item

        if phase == "approval":
            initial = call(["sync"])
            _ = yield from receipt("initial_sync", initial)
            yield emit(
                "report_local_evidence_gap", marker_index,
                context="The prior standard has no current local evidence.",
            )
            marker_index += 1
            yield emit("ask_web_approval", marker_index)
            return

        assert phase == "approved_capture"
        static = call([
            "eval", "mock-web-capture", "--fixture-id",
            "web-approval-and-capture.initial",
            "--approval-event-id", approval["event_id"],
            "--approval-scope", approval["scope"],
            "--approval-note", approval["note"],
        ])
        emit_group = getattr(emit, "group", None)
        assert callable(emit_group)
        yield emit_group((
            ("public_web_access", ""),
            ("select_used_source", ""),
            ("snapshot_used_source", ""),
            ("initial_snapshot_receipt_verified", ""),
        ), marker_index)
        marker_index += 4
        item = yield from receipt(
            "initial_snapshot",
            static,
            expect_handoff=True,
            verified_marker_already_emitted=True,
        )
        assert isinstance(item, dict)
        assert item["kind"] == "rendered_web_capture"
        handoff_id = item["handoff_id"]
        source_id = item["source_id"]
        assert isinstance(handoff_id, str) and isinstance(source_id, str)
        yield emit("branch_rendered_web_capture_handoff", marker_index)
        marker_index += 1

        staged = call([
            "eval", "mock-web-capture", "--fixture-id",
            "web-approval-and-capture.rendered", "--handoff-id", handoff_id,
        ])
        staged_data = staged["data"]
        assert isinstance(staged_data, dict)
        staging_path = staged_data["path"]
        assert isinstance(staging_path, str)
        yield emit("stage_faithful_browser_capture", marker_index)
        marker_index += 1

        rendered = call([
            "source", "snapshot-url", "--source-id", source_id,
            "--rendered-staging-path", staging_path, "--handoff-id", handoff_id,
            "--retrieved-at", "2026-09-04T12:00:00Z",
            "--final-url", "https://example.test/standard",
            "--detected-media-type", "text/html",
            "--approval-event-id", approval["event_id"],
            "--approval-scope", approval["scope"],
            "--approval-note", approval["note"],
        ])
        rendered_data = rendered["data"]
        assert isinstance(rendered_data, dict)
        snapshot = rendered_data["snapshot"]
        assert isinstance(snapshot, dict)
        active = snapshot["active_representation"]
        assert isinstance(active, dict) and active["source_id"] == source_id
        active_raw_path = active["raw_path"]
        assert isinstance(active_raw_path, str)
        yield emit("rendered_snapshot_url", marker_index)
        marker_index += 1
        no_delivery = yield from receipt("rendered_snapshot", rendered)
        assert no_delivery is None
        yield emit("verify_snapshot_active_representation", marker_index)
        marker_index += 1

        for part, term in (
            ("discovery", "standard"), ("expansion", "limit"),
            ("verification", "12"),
        ):
            drain_call([
                "search", "--scope", "sources", "--pass", part,
                "--term", term, "--context", "3",
            ])
            yield emit("source_pass_" + part, marker_index)
            marker_index += 1
            yield emit("source_pass_" + part + "_drained", marker_index)
            marker_index += 1

        question_path = "wiki/questions/external-standard.md"
        original = (cwd / question_path).read_text(encoding="utf-8")
        old_revision_line = next(
            line for line in original.splitlines()
            if line.startswith("corpus_revision: ")
        )
        revision = _shim_manifest(rendered)["corpus_revision"]
        assert isinstance(revision, str)
        body = original.replace(old_revision_line, "corpus_revision: " + revision)
        body = body.replace("answer_status: partial", "answer_status: answered")
        body = body.replace("verification_terms: [prior]", 'verification_terms: ["12"]')
        body = body.replace(
            "## Current answer\n",
            "## Current answer\n"
            "The captured current standard now requires limit 12.[^rendered-standard]\n\n",
        )
        record = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((cwd / "sources/ledger").glob("src_*.json"))
            if json.loads(path.read_text(encoding="utf-8"))["source_id"] == source_id
        )
        derivation_id = record["active_derivation_id"]
        derivation = record["derivations"][derivation_id]
        anchor = derivation["anchors"][0]
        body += (
            "\n[^rendered-standard]: source_id: `" + source_id
            + "`; content_sha256: `" + record["active_content_sha256"]
            + "`; derivation_id: `" + derivation_id
            + "`; anchor: `" + anchor["kind"] + ":" + anchor["value"]
            + "`; [original](../../sources/raw/" + active_raw_path
            + "); [extracted](../../" + derivation["output_path"]
            + "#" + anchor["kind"] + ":" + anchor["value"] + ")\n"
        )
        wiki_staging_path = (
            ".brain/wiki-staging/wstg_33333333333333333333333333333333/files/"
            + question_path
        )
        staged_question = cwd / wiki_staging_path
        staged_question.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged_question.write_text(body, encoding="utf-8")
        links = drain_call(
            ["links", "candidates", question_path, "--term", "standard"], links=True,
        )
        yield emit("link_candidates_drained", marker_index)
        marker_index += 1
        link_proof = _drained_link_proof(links)
        manifest_path = ".brain/wiki-staging/wstg_33333333333333333333333333333333/manifest.json"
        _write_fixture_manifest(
            cwd, manifest_path, revision=revision,
            link_candidate_runs=[link_proof],
            changes=[{
                "operation": "write", "path": question_path,
                "staging_path": wiki_staging_path,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }],
        )
        call(["wiki", "apply", "--manifest", manifest_path])
        yield emit("persist_claim", marker_index)

    outcome = runner.run_scenario(
        client="claude", scenario_id=scenario["id"],
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=_faithful_claude_fixture_process(scenario["id"], workflow),
    )

    assert len(observed_plans) == 2, (
        outcome.log["incomplete_reasons"],
        observed_plans[0].incomplete_reasons if observed_plans else (),
    )
    assert all(plan.complete for plan in observed_plans)
    assert observed_plans[0] == observed_plans[1]
    assert [event["name"] for event in observed_plans[0].events] == scenario["required_events"]
    assert len(private_replays) == 1
    assert private_replays[0] is not None
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record", "interpretation_decision"} & {
        entry["type"] for entry in index["entries"]
    }


def test_faithful_repository_development_shim_replays_privately_without_semantic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository-only fixture needs no brain command or archive mutation.

    The real isolated fixture shim still supplies the sealed control/workspace
    boundary and the Claude-shaped stream.  Unlike knowledge scenarios, this
    workflow must prove that its two marker-time software diffs alone form the
    private candidate: calling ``./brain --json`` or touching an archive path
    would violate the scenario rather than corroborate it.
    """

    scenario = runner._scenario("repository-development-not-archived")
    observed_plans: list[runner.SemanticEventPlan] = []
    private_replays: list[object] = []
    brain_json_invocations: list[tuple[str, ...]] = []
    source_integrity: list[bool] = []
    wiki_integrity: list[bool] = []
    original_plan = runner.plan_semantic_events
    original_replay = runner._indexed_semantic_replay
    original_run = subprocess.run

    def observe_plan(
        evidence: runner.SemanticNormalizationInput,
    ) -> runner.SemanticEventPlan:
        plan = original_plan(evidence)
        if evidence.scenario.get("id") == scenario["id"]:
            observed_plans.append(plan)
        return plan

    def observe_replay(*args: object, **kwargs: object) -> object:
        assert kwargs.get("private_candidate") is True
        result = original_replay(*args, **kwargs)
        private_replays.append(result)
        return result

    def observe_subprocess(
        args: object, *args_tail: object, **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if (
            isinstance(args, (list, tuple))
            and len(args) >= 2
            and isinstance(args[0], str)
            and isinstance(args[1], str)
            and Path(args[0]).name == "brain"
            and args[1] == "--json"
        ):
            brain_json_invocations.append(tuple(args))
        return original_run(args, *args_tail, **kwargs)

    monkeypatch.setattr(runner, "plan_semantic_events", observe_plan)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", observe_replay)
    monkeypatch.setattr(subprocess, "run", observe_subprocess)

    def tree_digests(cwd: Path, relative_root: str) -> dict[str, str]:
        root = cwd / relative_root
        return {
            path.relative_to(cwd).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        assert phase == "main"
        # These objects are deliberately available through the fixture harness,
        # but repository development must not use either product CLI path.
        assert callable(invoke) and callable(drain) and callable(marker)
        emit = marker
        sources_before = tree_digests(cwd, "sources")
        wiki_before = tree_digests(cwd, "wiki")

        yield emit("classify_repository_development", 1)

        cli_path = cwd / "brainlib/cli.py"
        cli_path.write_bytes(cli_path.read_bytes() + b"\n# repository replay fixture: dry-run parser\n")
        test_path = cwd / "tests/unit/test_cli.py"
        test_path.write_bytes(test_path.read_bytes() + b"\n# repository replay fixture: dry-run coverage\n")
        yield emit("use_software_workflow", 2)

        source_integrity.append(tree_digests(cwd, "sources") == sources_before)
        wiki_integrity.append(tree_digests(cwd, "wiki") == wiki_before)

    outcome = runner.run_scenario(
        client="claude", scenario_id=scenario["id"],
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=_faithful_claude_fixture_process(scenario["id"], workflow),
    )

    assert brain_json_invocations == []
    assert source_integrity == [True]
    assert wiki_integrity == [True]
    assert len(observed_plans) == 2, (
        outcome.log["incomplete_reasons"],
        [plan.incomplete_reasons for plan in observed_plans],
    )
    assert all(plan.complete for plan in observed_plans)
    assert observed_plans[0] == observed_plans[1]
    assert [event["name"] for event in observed_plans[0].events] == scenario["required_events"]
    assert len(private_replays) == 1
    assert private_replays[0] is not None
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(item["passed"] is False for item in outcome.log["repository_assertions"])
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert not {"marker", "event_record", "interpretation_decision"} & {
        entry["type"] for entry in index["entries"]
    }
    entries = {entry["id"]: entry for entry in index["entries"]}
    first_diff = json.loads(
        (outcome.log_path.parent / entries["diff-marker-1"]["relative_path"]).read_text()
    )
    second_diff = json.loads(
        (outcome.log_path.parent / entries["diff-marker-2"]["relative_path"]).read_text()
    )
    assert first_diff["changed_paths"] == []
    assert second_diff["changed_paths"] == ["brainlib/cli.py", "tests/unit/test_cli.py"]


def test_only_repository_development_permits_an_empty_shim_command_bridge() -> None:
    """A future lookalike must not inherit the repository no-command exception."""

    repository = runner._scenario("repository-development-not-archived")
    lookalike = dict(repository)
    lookalike["id"] = "unreviewed-marker-only-lookalike"

    assert runner._scenario_permits_no_shim_commands(repository) is True
    assert runner._scenario_permits_no_shim_commands(lookalike) is False


@pytest.mark.parametrize("preapply_bytes", (b"", b"{}\n", b"not-json\n"))
def test_repository_no_command_bridge_rejects_present_preapply_capture(
    tmp_path: Path,
    preapply_bytes: bytes,
) -> None:
    """A present preapply input, including an empty file, blocks the bridge."""

    scenario = runner._scenario("repository-development-not-archived")

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        del cwd, invoke, drain
        assert phase == "main"
        assert callable(marker)
        yield marker("classify_repository_development", 1)
        yield marker("use_software_workflow", 2)

    faithful = _faithful_claude_fixture_process(scenario["id"], workflow)

    def contaminated_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        control = Path(environ[runner.CONTROL_ENV])
        (control / "runner-preapply-manifests.jsonl").write_bytes(preapply_bytes)
        return faithful(argv, cwd, environ, stdin)

    outcome = runner.run_scenario(
        client="claude", scenario_id=scenario["id"],
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=contaminated_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "shim_capture_conversion_failed" in outcome.log["incomplete_reasons"]
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    process = json.loads((outcome.log_path.parent / entries["process-1"]["relative_path"]).read_text())
    assert process["trace_id"] == "trace-unconverted-1"
    assert "trace-execution-1" not in entries


def test_repository_no_command_bridge_cannot_recover_after_index_read_then_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A converter failure cannot be relabelled after its raw index disappears."""

    scenario = runner._scenario("repository-development-not-archived")
    malformed_index = b'{"unexpected":true}\n'
    index_reads: list[bytes] = []
    original_read = runner._read_control_capture

    def workflow(
        phase: str,
        cwd: Path,
        invoke: object,
        drain: object,
        marker: object,
    ) -> object:
        del cwd, invoke, drain
        assert phase == "main"
        assert callable(marker)
        yield marker("classify_repository_development", 1)
        yield marker("use_software_workflow", 2)

    faithful = _faithful_claude_fixture_process(scenario["id"], workflow)

    def contaminated_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        control = Path(environ[runner.CONTROL_ENV])
        (control / "brain-shim-capture-index.jsonl").write_bytes(malformed_index)
        return faithful(argv, cwd, environ, stdin)

    def read_then_remove(
        control_root: Path,
        relative: object,
        *,
        control_pin: runner.PinnedDirectory | None = None,
    ) -> bytes:
        raw = original_read(control_root, relative, control_pin=control_pin)
        if relative == "brain-shim-capture-index.jsonl":
            (control_root / str(relative)).unlink()
            index_reads.append(raw)
        return raw

    monkeypatch.setattr(runner, "_read_control_capture", read_then_remove)
    outcome = runner.run_scenario(
        client="claude", scenario_id=scenario["id"],
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=contaminated_process,
    )

    assert index_reads == [malformed_index]
    assert outcome.log["result"] == "incomplete"
    assert "shim_capture_conversion_failed" in outcome.log["incomplete_reasons"]
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    process = json.loads((outcome.log_path.parent / entries["process-1"]["relative_path"]).read_text())
    assert process["trace_id"] == "trace-unconverted-1"
    assert "trace-execution-1" not in entries


def _repository_assertion_diff(
    writer: runner.EvidenceWriter, *, run_id: str, paths: tuple[str, ...],
) -> str:
    """Build one immutable host diff for repository-assertion projection tests."""

    after = []
    for index, path in enumerate(sorted(paths), 1):
        raw = ("changed " + path + "\n").encode("utf-8")
        content_id = f"repository-assertion-content-{index}"
        writer.add_bytes(content_id, "file_capture", raw)
        after.append({
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "content_id": content_id,
        })
    diff_id = "repository-marker-diff"
    writer.add_json(diff_id, "diff", {
        "run_id": run_id,
        "execution_id": "execution-1",
        "before": [],
        "after": after,
        "changed_paths": sorted(paths),
        "staged_paths": [],
    })
    return diff_id


def _repository_assertion_indexed_fixture(
    tmp_path: Path,
    *,
    run_id: str,
    paths: tuple[str, ...],
    separate_marker_records: bool = False,
    preexisting_semantic_type: str | None = None,
    expect_private_replay: bool = True,
) -> SimpleNamespace:
    """Build a complete, sealed-shape repository replay before testing scope.

    The repository assertion projector must not be exercised with only a
    hand-written diff: its marker-time input is meaningful only after the
    normalizer has rebuilt the same plan from trace-bound native snapshots,
    launch policy, process bytes, and the fixed run manifest.  ``paths`` is
    deliberately the sole semantic variable in this fixture, so a negative
    path-policy test cannot accidentally stop at an unrelated lifecycle gap.
    """

    scenario = runner._scenario("repository-development-not-archived")
    from tests.evals.phase_prompt_contract import (
        PROMPT_PROTOCOL,
        canonical_phase_prompt_bytes,
        scenario_sha256,
    )

    prepared = runner.prepare_isolated_workspace(
        "repository-development-not-archived", scratch_root=tmp_path,
    )
    writer = runner.EvidenceWriter(prepared.control_root, run_id)
    snapshotter = runner.WorkspaceSnapshotter(
        workspace=prepared.workspace,
        workspace_pin=prepared.workspace_pin,
        writer=writer,
    )
    initial = snapshotter.capture(
        execution_id="execution-1", trace_sequence=0, role="initial",
    )
    for path in paths:
        target = prepared.workspace / path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(("fixture change " + path + "\n").encode("utf-8"))

    transcript_id = "transcript-1"
    if separate_marker_records:
        template = _claude_transcript([], cwd=str(prepared.workspace.resolve())).splitlines(
            keepends=True,
        )

        def assistant_record(index: int, name: str) -> bytes:
            return _encoded({
                "type": "assistant",
                "message": {
                    "model": "runner-test",
                    "content": [{"type": "text", "text": f"EVENT:{name}\n"}],
                },
                "parent_tool_use_id": None,
                "session_id": "runner-test",
                "uuid": f"assistant-{index}",
            }) + b"\n"

        transcript = b"".join((
            template[0],
            assistant_record(1, "classify_repository_development"),
            assistant_record(2, "use_software_workflow"),
            template[-1],
        ))
    else:
        transcript = _claude_transcript(
            [{
                "type": "text",
                "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
            }],
            cwd=str(prepared.workspace.resolve()),
        )
    trace = runner.ControlTraceWriter(prepared.control_root, "execution-1")
    marker_snapshots: list[runner.WorkspaceSnapshot] = []
    offset = 0
    for native_raw in transcript.splitlines(keepends=True):
        native_start = offset
        native_end = native_start + len(native_raw)
        native_sha256 = hashlib.sha256(native_raw).hexdigest()

        def capture_native_snapshot(
            sequence: int,
            *,
            start: int = native_start,
            end: int = native_end,
            digest: str = native_sha256,
        ) -> str:
            snapshot = snapshotter.capture(
                execution_id="execution-1",
                trace_sequence=sequence,
                role="marker",
                native_record={
                    "transcript_id": transcript_id,
                    "byte_start": start,
                    "byte_end": end,
                    "sha256": digest,
                },
            )
            marker_snapshots.append(snapshot)
            return snapshot.artifact_id

        trace.append_native_record(
            transcript_id=transcript_id,
            byte_start=native_start,
            byte_end=native_end,
            sha256=native_sha256,
            snapshot_factory=capture_native_snapshot,
        )
        offset = native_end
    trace_rows = trace.records()
    normalization = runner.normalize_native_transcript(
        client="claude", version="2.1.251", transcript=transcript,
    )
    assert normalization.complete is True
    candidates = runner.extract_event_markers(
        transcript=transcript,
        native_records=normalization.records,
        trace_records=trace_rows,
        transcript_id=transcript_id,
        allowed_event_names=scenario["required_events"],
    )
    markers = tuple(
        runner.SemanticMarkerEvidence(
            marker_id=f"marker-{index}",
            execution_id="execution-1",
            candidate=candidate,
            transcript_id=transcript_id,
        )
        for index, candidate in enumerate(candidates, 1)
    )
    assert [marker.candidate.event_name for marker in markers] == scenario["required_events"]
    trace_snapshot_ids = {
        ("execution-1", row["sequence"]): row["workspace_snapshot_id"]
        for row in trace_rows
        if row["kind"] == "native_record"
    }
    references = runner._build_marker_diff_references(
        writer,
        run_id=run_id,
        required_events=scenario["required_events"],
        markers=markers,
        initial_snapshots={"execution-1": initial},
        snapshots=tuple(marker_snapshots),
        execution_order={"execution-1": 0},
        trace_snapshot_ids=trace_snapshot_ids,
    )

    fixture_sha256 = prepared.fixture["tree_sha256"]
    writer.add_bytes("mcp-1", "mcp_config", runner.EMPTY_MCP_BYTES, suffix=".json")
    mcp_path = (writer.run_root / "artifacts" / "mcp-1.json").resolve()
    phase_prompt = canonical_phase_prompt_bytes(scenario, "main")
    phase_prompt_sha256 = hashlib.sha256(phase_prompt).hexdigest()
    writer.add_bytes("phase-prompt-1", "phase_prompt", phase_prompt, suffix=".txt")
    argv = runner.build_effective_argv(
        "claude", runner._CLAUDE_TEMPLATE, prepared.workspace, mcp_path,
        phase_prompt.decode("utf-8", "strict"),
    )
    argv_sha256 = runner._sha256(runner._canonical_json(argv))
    mcp_identity = runner._identity(mcp_path)
    writer.add_bytes("version-1", "version", b"2.1.251\n")
    writer.add_bytes("help-1", "help", b"stream-json\n")
    writer.add_bytes("transcript-1", "transcript", transcript)
    writer.add_json("trace-execution-1", "execution_trace", {
        "schema_version": 1,
        "run_id": run_id,
        "execution_id": "execution-1",
        "records": trace_rows,
    })
    trace_entry = next(entry for entry in writer.entries if entry["id"] == "trace-execution-1")
    writer.add_json("policy-1", "policy", {
        "run_id": run_id,
        "execution_id": "execution-1",
        "phase": "main",
        "client": "claude",
        "profile": "claude-restricted-tool-surface-v1",
        "network_attestation": "mock-only",
        "argv": argv,
        "argv_sha256": argv_sha256,
        "version_id": "version-1",
        "help_id": "help-1",
        "fixture_id": scenario["fixture_id"],
        "fixture_sha256": fixture_sha256,
        "fixture_capability_id": None,
        "approval": None,
        "phase_prompt_id": "phase-prompt-1",
        "phase_prompt_sha256": phase_prompt_sha256,
        "phase_prompt_transport": "argv_final_utf8",
        "mcp_config_id": "mcp-1",
        "mcp_config_sha256": runner._sha256(runner.EMPTY_MCP_BYTES),
        "mcp_path": str(mcp_path),
        "mcp_identity": mcp_identity,
    })
    writer.add_json("process-1", "process", {
        "run_id": run_id,
        "execution_id": "execution-1",
        "phase": "main",
        "client": "claude",
        "client_version": "2.1.251",
        "argv": argv,
        "argv_sha256": argv_sha256,
        "exit_code": 0,
        "transcript_id": transcript_id,
        "transcript_sha256": runner._sha256(transcript),
        "trace_id": "trace-execution-1",
        "trace_sha256": trace_entry["sha256"],
        "native_format": "claude-stream-json-2.1.251-v1",
        "fixture_id": scenario["fixture_id"],
        "fixture_sha256": fixture_sha256,
        "fixture_capability_id": None,
        "approval": None,
        "phase_prompt_id": "phase-prompt-1",
        "phase_prompt_sha256": phase_prompt_sha256,
        "phase_prompt_transport": "argv_final_utf8",
        "mcp_identity": mcp_identity,
    })
    writer.add_fixed_json("run-manifest", "run_manifest", "run-manifest.json", {
        "schema_version": 1,
        "run_id": run_id,
        "client": "claude",
        "scenario_id": scenario["id"],
        "fixture_id": scenario["fixture_id"],
        "fixture_sha256": fixture_sha256,
        "workspace": str(prepared.workspace.resolve()),
        "phases": ["main"],
        "policy_profiles": ["claude-restricted-tool-surface-v1"],
        "fixture_capability_id": None,
        "approval": None,
        "software_paths": list(runner._software_paths_for_scenario(scenario)),
        "scenario_sha256": scenario_sha256(scenario),
        "phase_prompt_protocol": PROMPT_PROTOCOL,
        "phase_prompts": [{
            "execution_id": "execution-1",
            "phase": "main",
            "phase_prompt_id": "phase-prompt-1",
            "phase_prompt_sha256": phase_prompt_sha256,
            "phase_prompt_transport": "argv_final_utf8",
        }],
    })
    if preexisting_semantic_type is not None:
        assert preexisting_semantic_type in {
            "marker", "event_record", "interpretation_decision",
        }
        writer.add_json(
            "preexisting-semantic-row", preexisting_semantic_type,
            {"schema_version": 1},
        )
    artifact_types = {entry["id"]: entry["type"] for entry in writer.entries}
    plan = runner.plan_semantic_events(runner.SemanticNormalizationInput(
        run_id=run_id,
        scenario=scenario,
        execution_order={"execution-1": 0},
        markers=markers,
        commands=(),
        artifact_types=artifact_types,
        event_references=references,
        deliveries=(),
    ))
    assert plan.complete is True
    # The candidate first succeeds against a virtual view, then the focused
    # fixture persists the exact same marker/event rows for assertion replay.
    private_replay = runner._indexed_semantic_replay(
        writer,
        run_id=run_id,
        scenario=scenario,
        plan=plan,
        markers=markers,
        commands=(),
        execution_order={"execution-1": 0},
        workspace=prepared.workspace,
        client="claude",
        fixture_sha256=fixture_sha256,
        fixture_capability_id=None,
        approval=None,
        private_candidate=True,
    )
    if expect_private_replay:
        assert private_replay == {}
    else:
        assert private_replay is None
    runner._emit_semantic_plan(writer, plan)
    return SimpleNamespace(
        prepared=prepared,
        writer=writer,
        scenario=scenario,
        fixture_sha256=fixture_sha256,
        plan=plan,
        markers=markers,
        references=references,
        trace_snapshot_ids=trace_snapshot_ids,
        private_replay=private_replay,
    )


def _project_indexed_repository_assertions(
    fixture: SimpleNamespace,
    *,
    actual_client_verified: bool = False,
) -> runner.RepositoryAssertionProjection:
    """Project repository facts only after the canonical fixture is bound."""

    return runner._project_repository_assertions(
        fixture.writer,
        run_id=fixture.writer.run_id,
        scenario=fixture.scenario,
        marker_diff_references=fixture.references,
        fallback_before=None,
        fallback_after=None,
        actual_client_verified=actual_client_verified,
        semantic_plan=fixture.plan,
        markers=fixture.markers,
        commands=(),
        execution_order={"execution-1": 0},
        workspace=fixture.prepared.workspace,
        trace_snapshot_ids=fixture.trace_snapshot_ids,
        client="claude",
        fixture_sha256=fixture.fixture_sha256,
        fixture_capability_id=None,
        approval=None,
    )


def test_public_repository_assertion_facts_are_pure_then_emit_true_rows(
    tmp_path: Path,
) -> None:
    """The pass-capable assertion path cannot write a fallback or false row."""

    fixture = _repository_assertion_indexed_fixture(
        tmp_path,
        run_id="e" * 64,
        paths=tuple(runner._software_paths_for_scenario(
            runner._scenario("repository-development-not-archived"),
        )),
    )
    before_entries = list(fixture.writer.entries)
    facts = runner._derive_repository_assertion_facts(
        fixture.writer,
        run_id=fixture.writer.run_id,
        scenario=fixture.scenario,
        marker_diff_references=fixture.references,
        semantic_plan=fixture.plan,
        markers=fixture.markers,
        marker_snapshots=(),
        initial_snapshots={},
        commands=(),
        execution_order={"execution-1": 0},
        workspace=fixture.prepared.workspace,
        trace_snapshot_ids=fixture.trace_snapshot_ids,
        client="claude",
        fixture_sha256=fixture.fixture_sha256,
        fixture_capability_id=None,
        approval=None,
    )

    assert facts.complete is True
    assert fixture.writer.entries == before_entries
    emitted = runner._emit_public_repository_assertions(
        fixture.writer, run_id=fixture.writer.run_id, facts=facts,
    )
    assert [row["passed"] for row in emitted] == [True, True, True]
    assert all(entry["type"] != "assertion" for entry in before_entries)


def test_public_repository_assertion_emitter_refuses_incomplete_facts(
    tmp_path: Path,
) -> None:
    """An unavailable marker-time assertion leaves the evidence writer unchanged."""

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control, "f" * 64)
    facts = runner.RepositoryAssertionFacts(
        texts=("closed assertion",),
        facts=(False,),
        diff_id=None,
        marker_id=None,
        snapshot_id=None,
        marker_time_available=False,
        incomplete_reasons=("semantic_assertion_index_unavailable",),
    )

    with pytest.raises(runner.RunnerError, match="public assertion facts are incomplete"):
        runner._emit_public_repository_assertions(writer, run_id=writer.run_id, facts=facts)
    assert writer.entries == []


def test_repository_assertion_projection_uses_closed_marker_time_scope(
    tmp_path: Path,
) -> None:
    """Repository assertions use runner policy and an indexed marker diff only."""

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    run_id = "a" * 64
    writer = runner.EvidenceWriter(control, run_id)
    scenario = runner._scenario("repository-development-not-archived")
    allowed = tuple(runner._software_paths_for_scenario(scenario))
    assert allowed == (
        "brainlib/cli.py",
        "brainlib/commands.py",
        "tests/unit/test_cli.py",
        "tests/unit/test_sync.py",
    )
    diff_id = _repository_assertion_diff(writer, run_id=run_id, paths=allowed)

    projection = runner._project_repository_assertions(
        writer,
        run_id=run_id,
        scenario=scenario,
        marker_diff_references={"use_software_workflow": {"diff_id": diff_id}},
        fallback_before=None,
        fallback_after=None,
        actual_client_verified=False,
    )

    assert projection.marker_time_available is False
    assert projection.rules_satisfied is False
    assert [item["text"] for item in projection.assertions] == scenario["repository_assertions"]
    assert {item["diff_id"] for item in projection.assertions} == {
        "repository-assertion-fallback-diff"
    }
    # A fake harness may prove projection mechanics but cannot claim a real
    # repository assertion or turn an incomplete log into a pass.
    assert all(item["passed"] is False for item in projection.assertions)
    entry_by_id = {entry["id"]: entry for entry in writer.entries}
    for item in projection.assertions:
        proof = json.loads(
            (writer.run_root / entry_by_id[item["assertion_id"]]["relative_path"]).read_text()
        )
        assert proof == {
            "run_id": run_id,
            "text": item["text"],
            "passed": False,
            "diff_id": "repository-assertion-fallback-diff",
        }


def test_repository_assertion_projection_cannot_pass_from_a_forged_diff_without_indexed_lifecycle(
    tmp_path: Path,
) -> None:
    """A future executable flag cannot bypass the shared semantic replay."""

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    run_id = "9" * 64
    writer = runner.EvidenceWriter(control, run_id)
    scenario = runner._scenario("repository-development-not-archived")
    diff_id = _repository_assertion_diff(
        writer, run_id=run_id, paths=tuple(runner._software_paths_for_scenario(scenario)),
    )

    projection = runner._project_repository_assertions(
        writer,
        run_id=run_id,
        scenario=scenario,
        marker_diff_references={"use_software_workflow": {"diff_id": diff_id}},
        fallback_before=None,
        fallback_after=None,
        actual_client_verified=True,
    )

    assert projection.marker_time_available is False
    assert projection.rules_satisfied is False
    assert all(item["passed"] is False for item in projection.assertions)


def test_repository_assertion_projection_rejects_archive_and_unrequested_paths(
    tmp_path: Path,
) -> None:
    """A bound replay and pure immutable scope facts have separate roles."""

    allowed = _repository_assertion_indexed_fixture(
        tmp_path,
        run_id="b" * 64,
        paths=tuple(runner._software_paths_for_scenario(
            runner._scenario("repository-development-not-archived"),
        )),
    )
    allowed_projection = _project_indexed_repository_assertions(allowed)
    assert allowed_projection.marker_time_available is True
    assert allowed_projection.rules_satisfied is True
    assert all(item["passed"] is False for item in allowed_projection.assertions)

    # The archive mutation is deliberately exercised below the semantic
    # replay boundary.  A public-contract failure is never relaxed merely so
    # this deterministic path policy can be tested.
    control = tmp_path / "scope-control"
    control.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control, "c" * 64)
    unsafe_diff_id = _repository_assertion_diff(
        writer,
        run_id="c" * 64,
        paths=(
            "wiki/questions/unsafe.md",
            "wiki/pages/unsafe.md",
            "sources/raw/unsafe.txt",
            ".brain/unsafe-state.json",
            "brainlib/validation.py",
        ),
    )
    unsafe_diff = runner._read_repository_diff(
        writer, run_id="c" * 64, diff_id=unsafe_diff_id,
    )
    assert runner._repository_assertion_scope_facts(
        allowed.scenario, unsafe_diff.changed_paths,
    ) == [False, False, False]


def test_repository_scope_violation_in_an_earlier_marker_cannot_be_laundered(
    tmp_path: Path,
) -> None:
    """A clean terminal delta cannot erase an earlier archive mutation."""

    fixture = _repository_assertion_indexed_fixture(
        tmp_path,
        run_id="d" * 64,
        paths=("wiki/questions/unsafe.md",),
        separate_marker_records=True,
        expect_private_replay=False,
    )

    projection = _project_indexed_repository_assertions(
        fixture, actual_client_verified=True,
    )

    # A rejected first marker cannot become a candidate merely because the
    # second marker has an empty diff.  The fallback also keeps a future
    # registered-process flag from changing this failure into a pass.
    assert projection.marker_time_available is False
    assert projection.rules_satisfied is False
    assert all(item["passed"] is False for item in projection.assertions)


def test_nonstreaming_marker_stdout_cannot_mint_marker_time_semantic_support(
    tmp_path: Path,
) -> None:
    """A completed stdout buffer has no trustworthy marker-time workspace view."""

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        target = cwd / "brainlib" / "cli.py"
        target.write_bytes(target.read_bytes() + b"\n# mutation after buffered marker emission\n")
        return runner.ProcessCapture(
            exit_code=0,
            stdout=_claude_transcript([{
                "type": "text",
                "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
            }]),
            stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="repository-development-not-archived",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert "semantic_marker_capture_not_streamed" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "event_record" not in {entry["type"] for entry in index["entries"]}


def test_nonstreaming_escaped_json_marker_cannot_bypass_marker_time_refusal(
    tmp_path: Path,
) -> None:
    """The sourced parser, not raw JSON spelling, decides whether text is a marker."""

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        transcript = _claude_transcript([{
            "type": "text",
            "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
        }]).replace(b"EVENT:", b"\\u0045VENT:")
        assert b"EVENT:" not in transcript
        return runner.ProcessCapture(
            exit_code=0, stdout=transcript, stderr=b"", version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="repository-development-not-archived",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    assert "semantic_marker_capture_not_streamed" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert "event_record" not in {entry["type"] for entry in index["entries"]}


def test_streamed_escaped_json_marker_gets_its_marker_time_snapshot(
    tmp_path: Path,
) -> None:
    """Escaped marker spelling cannot cause a streamed workspace snapshot gap."""

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        target = cwd / "brainlib" / "cli.py"
        target.write_bytes(target.read_bytes() + b"\n# escaped marker mutation\n")
        transcript = _claude_transcript([{
            "type": "text",
            "text": "EVENT:classify_repository_development\nEVENT:use_software_workflow\n",
        }]).replace(b"EVENT:", b"\\u0045VENT:")
        assert b"EVENT:" not in transcript
        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stdout_chunks=(transcript,), stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="repository-development-not-archived",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=fake_process,
    )

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    # Both decoded marker lines retain their own marker-time diagnostic diff;
    # the injected client remains provenance-incomplete, so the independent
    # repository-assertion fallback diff is retained as well.  It must not be
    # mistaken for a third marker binding or permit event emission.
    diffs = [entry["id"] for entry in index["entries"] if entry["type"] == "diff"]
    assert set(diffs) == {
        "diff-marker-1", "diff-marker-2", "repository-assertion-fallback-diff",
    }
    assert "event_record" not in {entry["type"] for entry in index["entries"]}
    assert "semantic_execution_binding_unavailable" in outcome.log["incomplete_reasons"]


def test_injected_host_shim_uses_the_shared_trace_and_cannot_be_relabelled_pass(
    tmp_path: Path,
) -> None:
    """The actual 7b shim bridge is exercised; only the client is fake."""

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        # This goes through the root wrapper and its injected HostRecorder,
        # not a hand-authored command/capture dictionary.
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        assert completed.returncode in {0, 1}
        return runner.ProcessCapture(
            exit_code=0,
            stdout=b'{"event":"pretend-pass"}\n',
            stderr=completed.stderr,
            version=b"0.153.0\n",
            help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    trace_path = outcome.control_root / "runner-trace-execution-1.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [item["sequence"] for item in trace] == list(range(1, len(trace) + 1))
    assert [item["kind"] for item in trace] == [
        "command_start", "source_state", "command_end", "native_record",
    ]
    assert not (outcome.control_root / "brain-shim-trace.jsonl").exists()
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]

    # A fake cannot become a pass by changing only the public result string.
    from tests.evals import event_log_contract as contract

    claimed = dict(outcome.log)
    claimed["result"] = "pass"
    claimed["incomplete_reasons"] = []
    schema = json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text())
    scenario = json.loads((ROOT / "tests/evals/scenarios/current-wiki-fast-path.json").read_text())
    with pytest.raises(contract.EventLogContractError):
        contract.validate_event_log(
            claimed, schema, scenario, claimed["fixture_sha256"],
            contract.TrustedRunContext(outcome.control_root, outcome.workspace),
        )


def test_streaming_fake_process_allocates_native_trace_rows_at_chunk_arrival(
    tmp_path: Path,
) -> None:
    """A shim command between stdout chunks must occupy that trace interval."""

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        def chunks() -> object:
            yield b'{"event":"before-command"}\n'
            completed = subprocess.run(
                [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
            assert completed.returncode in {0, 1}
            yield b'{"event":"after-command"}\n'

        return runner.ProcessCapture(
            exit_code=0, stdout=b"", stderr=b"", version=b"0.153.0\n", help=b"JSONL\n",
            stdout_chunks=chunks(),
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    trace_path = outcome.control_root / "runner-trace-execution-1.jsonl"
    trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert [item["kind"] for item in trace] == [
        "native_record", "command_start", "source_state", "command_end", "native_record",
    ]
    assert (outcome.control_root / ".brain/eval-transcripts/transcript-1.jsonl").read_bytes() == (
        b'{"event":"before-command"}\n{"event":"after-command"}\n'
    )
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


def _task7d_registered_phase_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    runner.EvidenceWriter, Path, Path, Path, runner.ControlTraceWriter,
    runner.PinnedDirectory, runner.PinnedDirectory,
]:
    """Install a test-only closed build without consulting PATH or HOME."""

    from tests.evals import event_log_contract as contract

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    registered_root = tmp_path / "runner-owned-registration"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    registered_root.mkdir(mode=0o700)
    executable = registered_root / "claude"
    executable.write_bytes(b"#!/bin/sh\nprintf 'test client\\n'\n")
    executable.chmod(0o700)
    raw, _identity = contract._read_executable(str(executable))
    digest = hashlib.sha256(raw).hexdigest()
    registration = runner.RunnerOwnedExecutableRegistration(
        client="claude",
        resolved_path=executable,
        expected_version="2.1.251",
        native_format="claude-stream-json-2.1.251-v1",
    )
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({"claude": registration}),
    )
    monkeypatch.setattr(contract, "_DEFAULT_SUPPORTED_BUILDS", {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", digest): "claude-test-build-v1",
    })
    writer = runner.EvidenceWriter(control_root, "a" * 64)
    control_pin = runner.PinnedDirectory.pin(control_root, private=True)
    workspace_pin = runner.PinnedDirectory.pin(workspace, private=False)
    return writer, control_root, workspace, executable, runner.ControlTraceWriter(
        control_root, "execution-1", control_pin=control_pin,
    ), control_pin, workspace_pin


class _Task7dRecordingInput(io.BytesIO):
    """Retain written bytes after the live launcher closes stdin."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.writes.append(bytes(value))
        return super().write(value)


def _task7d_closed_pipe(raw: bytes):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, raw)
    os.close(write_fd)
    return os.fdopen(read_fd, "rb", buffering=0)


def test_runner_owned_registration_probes_a_closed_absolute_candidate_and_binds_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symbolic profile cannot select PATH's client or skip fresh probes.

    This would fail if the launcher trusted ``argv[0]``, skipped either probe,
    or wrote executable evidence that was not bound to its final absolute
    launch path.
    """

    writer, control_root, workspace, executable, trace, control_pin, workspace_pin = _task7d_registered_phase_inputs(
        tmp_path, monkeypatch,
    )
    mcp = control_root / "mcp.json"
    mcp.write_bytes(runner.EMPTY_MCP_BYTES)
    argv = runner.build_effective_argv(
        "claude", _claude_template(), workspace, mcp, "sealed phase prompt",
    )
    monkeypatch.setenv("PATH", str(tmp_path / "path-shadow"))
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        assert cwd == workspace
        assert environ["PATH"] == "/usr/bin:/bin"
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"2.1.251 (test Claude)\n", b"")
        return subprocess.CompletedProcess(argv, 0, b"stream-json help\n", b"")

    class CompleteFakeProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"type":"result"}\n')
            self.stderr = _task7d_closed_pipe(b"diagnostic\n")
            self.wait_called = False
            self.communicated = False

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            self.wait_called = True
            assert self.stdout.closed and self.stderr.closed
            return 0

        def communicate(self, *args: object, **kwargs: object) -> None:
            self.communicated = True
            raise AssertionError("live evidence must not use communicate")

    process = CompleteFakeProcess()
    launches: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv: list[str], **kwargs: object) -> CompleteFakeProcess:
        launches.append((list(argv), dict(kwargs)))
        return process

    _registration, trusted = runner._prepare_registered_live_executable(
        client="claude", execution_id="execution-1", control_pin=control_pin,
        workspace_pin=workspace_pin,
    )
    trusted_context = runner._registered_live_trusted_context(
        control_pin=control_pin, workspace_pin=workspace_pin,
        trusted_executables={"execution-1": trusted},
    )

    phase = runner._launch_registered_live_phase(
        writer=writer,
        client="claude",
        execution_id="execution-1",
        phase="main",
        phase_index=1,
        workspace=workspace,
        control_pin=control_pin,
        workspace_pin=workspace_pin,
        argv=argv,
        environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
        stdin=b"",
        trace=trace,
        transcript_id="transcript-1",
        on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
        probe_runner=probe,
        popen_factory=popen,
        trusted_context=trusted_context,
    )

    absolute = str(executable)
    assert probes == [[absolute, "--version"], [absolute, "--help"]]
    assert launches[0][0][0] == absolute
    assert launches[0][1]["shell"] is False
    assert process.wait_called and not process.communicated
    assert phase.argv[0] == absolute
    assert phase.trusted_executable.resolved_path == executable
    assert phase.trusted_context is trusted_context
    assert phase.trusted_context.trusted_executables["execution-1"] is phase.trusted_executable
    assert phase.trusted_context.control_root == control_root
    assert phase.trusted_context.workspace_root == workspace
    phase.control_pin.verify()
    phase.workspace_pin.verify()
    assert phase.capture.stdout == b'{"type":"result"}\n'
    assert phase.capture.stderr == b"diagnostic\n"
    assert phase.launch_identity == phase.seal_identity
    assert phase.launch_sha256 == phase.seal_sha256

    entries = {entry["id"]: entry for entry in writer.entries}
    executable_entry = entries[phase.executable_id]
    evidence = json.loads((writer.run_root / executable_entry["relative_path"]).read_text())
    assert executable_entry["type"] == "client_executable"
    assert evidence["resolved_path"] == absolute
    assert evidence["version_probe"] == {
        "argv": [absolute, "--version"], "exit_code": 0,
        "output_id": phase.version_id,
        "output_sha256": entries[phase.version_id]["sha256"],
    }
    assert evidence["help_probe"] == {
        "argv": [absolute, "--help"], "exit_code": 0,
        "output_id": phase.help_id,
        "output_sha256": entries[phase.help_id]["sha256"],
    }
    # This is preregistration staging evidence only.  It cannot make an
    # event log or a pass before a separately reviewed atomic validator.
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()


@pytest.mark.parametrize(
    ("persisted_process_argv_sha256", "expected_bound"),
    (
        (None, True),
        ("0" * 64, False),
    ),
    ids=("matching-process-argv", "mutated-process-argv-sha"),
)
def test_registered_phase_evidence_gate_requires_exact_policy_process_and_prelaunch_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted_process_argv_sha256: str | None,
    expected_bound: bool,
) -> None:
    """A phase-release gate rejects an argv/context mismatch before phase two.

    This would fail if a registered phase merely had a client-executable
    artifact but its indexed policy/process did not bind the final absolute
    argv and the exact prelaunch TrustedRunContext.
    """

    writer, control_root, workspace, _executable, trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    mcp = control_root / "mcp.json"
    mcp.write_bytes(runner.EMPTY_MCP_BYTES)
    argv = runner.build_effective_argv(
        "claude", _claude_template(), workspace, mcp, "sealed phase prompt",
    )

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        del cwd, environ
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"type":"result"}\n')
            self.stderr = _task7d_closed_pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            return 0

    _registration, trusted = runner._prepare_registered_live_executable(
        client="claude", execution_id="execution-1", control_pin=control_pin,
        workspace_pin=workspace_pin,
    )
    context = runner._registered_live_trusted_context(
        control_pin=control_pin, workspace_pin=workspace_pin,
        trusted_executables={"execution-1": trusted},
    )
    phase = runner._launch_registered_live_phase(
        writer=writer, client="claude", execution_id="execution-1", phase="approval",
        phase_index=1, workspace=workspace, control_pin=control_pin,
        workspace_pin=workspace_pin, argv=argv,
        environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
        stdin=b"", trace=trace, transcript_id="transcript-1",
        on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
        probe_runner=probe, popen_factory=lambda *_args, **_kwargs: CompleteProcess(),
        trusted_context=context,
    )
    policy = {
        "run_id": writer.run_id, "execution_id": "execution-1", "phase": "approval",
        "client": "claude", "argv": list(phase.argv),
        "argv_sha256": (
            persisted_process_argv_sha256
            if persisted_process_argv_sha256 is not None
            else hashlib.sha256(_encoded(list(phase.argv))).hexdigest()
        ),
        "version_id": phase.version_id, "help_id": phase.help_id,
        "executable_id": phase.executable_id,
    }
    trace_value = {
        "schema_version": 1,
        "run_id": writer.run_id,
        "execution_id": "execution-1",
        "records": [],
    }
    writer.add_bytes("transcript-1", "transcript", phase.capture.stdout)
    writer.add_json("trace-execution-1", "execution_trace", trace_value)
    trace_raw = _encoded(trace_value)
    process = {
        "run_id": writer.run_id, "execution_id": "execution-1", "phase": "approval",
        "client": "claude", "argv": list(phase.argv),
        "argv_sha256": hashlib.sha256(_encoded(list(phase.argv))).hexdigest(),
        "client_version": phase.reported_version,
        "native_format": phase.native_format,
        "executable_id": phase.executable_id,
        "exit_code": 0,
        "transcript_id": "transcript-1",
        "transcript_sha256": hashlib.sha256(phase.capture.stdout).hexdigest(),
        "trace_id": "trace-execution-1",
        "trace_sha256": hashlib.sha256(trace_raw).hexdigest(),
    }
    writer.add_json("policy-1", "policy", policy)
    writer.add_json("process-1", "process", process)

    assert runner._registered_phase_evidence_is_bound(
        writer=writer, workspace=workspace, execution_id="execution-1",
        phase="approval", live_phase=phase,
    ) is expected_bound
    if expected_bound:
        assert not runner._registered_phase_evidence_is_bound(
            writer=writer, workspace=workspace, execution_id="execution-1",
            phase="approval", live_phase=replace(phase, argv=("/wrong", *phase.argv[1:])),
        )


def test_registered_context_rejects_extra_phase_keys_and_stale_root_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase trust is bound to both exact keys and pinned descriptor roots."""

    _writer, _control_root, _workspace, _executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    _registration, first = runner._prepare_registered_live_executable(
        client="claude", execution_id="execution-1", control_pin=control_pin,
        workspace_pin=workspace_pin,
    )
    _registration, second = runner._prepare_registered_live_executable(
        client="claude", execution_id="execution-2", control_pin=control_pin,
        workspace_pin=workspace_pin,
    )
    aggregate = runner._registered_live_trusted_context(
        control_pin=control_pin, workspace_pin=workspace_pin,
        trusted_executables={"execution-1": first, "execution-2": second},
    )

    with pytest.raises(runner.RunnerError, match="execution set"):
        runner._registered_live_context_phase(
            client="claude", execution_id="execution-1", control_pin=control_pin,
            workspace_pin=workspace_pin, trusted_context=aggregate,
            expected_execution_ids=frozenset({"execution-1"}),
        )

    phase_one = runner._registered_live_trusted_context(
        control_pin=control_pin, workspace_pin=workspace_pin,
        trusted_executables={"execution-1": first},
    )
    object.__setattr__(phase_one, "control_identity", (0, 0))
    with pytest.raises(runner.RunnerError, match="root mismatch"):
        runner._registered_live_context_phase(
            client="claude", execution_id="execution-1", control_pin=control_pin,
            workspace_pin=workspace_pin, trusted_context=phase_one,
            expected_execution_ids=frozenset({"execution-1"}),
        )


def test_registered_probe_times_out_a_hung_test_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe has a total wall-clock bound before any live client launch."""

    monkeypatch.setattr(runner, "_REGISTERED_PROBE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(runner.RunnerError, match="probe timed out"):
        runner._registered_probe(
            argv=[sys.executable, "-c", "import time; time.sleep(0.2)"],
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
            probe_runner=None,
        )


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_registered_probe_rejects_oversized_captured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
) -> None:
    """Version/help probes cannot accumulate unbounded stdout or stderr."""

    monkeypatch.setattr(runner, "_MAX_REGISTERED_PROBE_BYTES", 8)
    script = f"import sys; sys.{stream_name}.buffer.write(b'x' * 9)"
    with pytest.raises(runner.RunnerError, match="probe output exceeds size limit"):
        runner._registered_probe(
            argv=[sys.executable, "-c", script],
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
            probe_runner=None,
        )


def test_registered_probe_retains_exact_bounded_output_bytes(tmp_path: Path) -> None:
    """The bounded drain preserves the exact bytes later sealed as probe evidence."""

    expected_stdout = b"2.1.251 (test)\n"
    expected_stderr = b"diagnostic\x00\n"
    completed = runner._registered_probe(
        argv=[
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'2.1.251 (test)\\n'); "
            "sys.stderr.buffer.write(b'diagnostic\\x00\\n')",
        ],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        probe_runner=None,
    )

    assert completed.returncode == 0
    assert completed.stdout == expected_stdout
    assert completed.stderr == expected_stderr


def test_registered_live_phase_rejects_a_caller_selected_executable_before_probe_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary absolute argv token is not a registration authority."""

    writer, control_root, workspace, _executable, trace, control_pin, workspace_pin = _task7d_registered_phase_inputs(
        tmp_path, monkeypatch,
    )
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"2.1.251\n", b"")

    with pytest.raises(runner.RunnerError, match="symbolic client selector"):
        runner._launch_registered_live_phase(
            writer=writer,
            client="claude",
            execution_id="execution-1",
            phase="main",
            phase_index=1,
            workspace=workspace,
            control_pin=control_pin,
            workspace_pin=workspace_pin,
            argv=[str(control_root / "caller-selected-claude"), "--print"],
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            probe_runner=probe,
            popen_factory=lambda *args, **kwargs: pytest.fail("caller argv reached spawn"),
        )
    assert probes == []


def test_registered_live_phase_rejects_overlapping_pinned_roots_before_probe_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace nested under control evidence cannot share launch authority."""

    writer, control_root, _workspace, _executable, trace, control_pin, _workspace_pin = _task7d_registered_phase_inputs(
        tmp_path, monkeypatch,
    )
    nested_workspace = control_root / "nested-workspace"
    nested_workspace.mkdir(mode=0o700)
    nested_pin = runner.PinnedDirectory.pin(nested_workspace, private=False)
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"2.1.251\n", b"")

    try:
        with pytest.raises(runner.RunnerError, match="roots overlap"):
            runner._launch_registered_live_phase(
                writer=writer,
                client="claude",
                execution_id="execution-1",
                phase="main",
                phase_index=1,
                workspace=nested_workspace,
                control_pin=control_pin,
                workspace_pin=nested_pin,
                argv=["claude", "--print"],
                environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
                stdin=b"",
                trace=trace,
                transcript_id="transcript-1",
                on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
                probe_runner=probe,
                popen_factory=lambda *args, **kwargs: pytest.fail("overlapping roots reached spawn"),
            )
    finally:
        nested_pin.close()
    assert probes == []


def test_registered_live_phase_rejects_a_symlink_workspace_before_probe_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathname alias cannot select a launch CWD outside its pinned identity."""

    writer, control_root, workspace, _executable, trace, control_pin, workspace_pin = _task7d_registered_phase_inputs(
        tmp_path, monkeypatch,
    )
    workspace_alias = tmp_path / "workspace-alias"
    workspace_alias.symlink_to(workspace, target_is_directory=True)
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"2.1.251\n", b"")

    with pytest.raises(runner.RunnerError, match="root pin mismatch|roots unavailable"):
        runner._launch_registered_live_phase(
            writer=writer,
            client="claude",
            execution_id="execution-1",
            phase="main",
            phase_index=1,
            workspace=workspace_alias,
            control_pin=control_pin,
            workspace_pin=workspace_pin,
            argv=["claude", "--print"],
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            probe_runner=probe,
            popen_factory=lambda *args, **kwargs: pytest.fail("symlink workspace reached spawn"),
        )
    assert probes == []


def test_registered_live_phase_rejects_a_closed_root_descriptor_before_probe_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale descriptor is not repaired by reopening its pathname."""

    writer, control_root, workspace, _executable, trace, control_pin, workspace_pin = _task7d_registered_phase_inputs(
        tmp_path, monkeypatch,
    )
    control_pin.close()
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"2.1.251\n", b"")

    with pytest.raises(runner.RunnerError, match="descriptor|trusted root"):
        runner._launch_registered_live_phase(
            writer=writer,
            client="claude",
            execution_id="execution-1",
            phase="main",
            phase_index=1,
            workspace=workspace,
            control_pin=control_pin,
            workspace_pin=workspace_pin,
            argv=["claude", "--print"],
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            probe_runner=probe,
            popen_factory=lambda *args, **kwargs: pytest.fail("closed root reached spawn"),
        )
    assert probes == []


@pytest.mark.parametrize("failure_type", [AttributeError, OSError])
def test_live_phase_reaps_and_closes_partial_streams_when_stream_acquisition_fails(
    tmp_path: Path,
    failure_type: type[Exception],
) -> None:
    """A missing later pipe closes earlier descriptors and reaps the child."""

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class PartialStreamsProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        @property
        def stderr(self):
            raise failure_type("missing stderr")

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = PartialStreamsProcess()
    with pytest.raises(runner.RunnerError, match="launch streams unavailable"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]
    assert process.stdin.closed and process.stdout.closed


class _Task7dAcquisitionAbort(BaseException):
    """A non-Exception getter failure must still trigger child cleanup."""


@pytest.mark.parametrize("failure_type", [TypeError, _Task7dAcquisitionAbort])
def test_live_phase_preserves_and_cleans_up_unexpected_stream_acquisition_failure(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    """Post-spawn cleanup covers all exceptions without laundering interrupts."""

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class UnexpectedStreamsProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        @property
        def stderr(self):
            raise failure_type("unexpected stream getter failure")

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = UnexpectedStreamsProcess()
    with pytest.raises(failure_type, match="unexpected stream getter failure"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]
    assert process.stdin.closed and process.stdout.closed


def test_live_phase_reaps_and_closes_pipes_when_selector_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selector setup is post-spawn work and cannot bypass process cleanup."""

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class SelectorFailureProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b"")
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = SelectorFailureProcess()

    def allocation_failure() -> object:
        raise OSError("selector allocation failed")

    monkeypatch.setattr(runner.selectors, "DefaultSelector", allocation_failure)
    with pytest.raises(OSError, match="selector allocation failed"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed


def test_registered_probe_reaps_and_closes_pipes_when_selector_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe selector allocation has the same post-spawn cleanup guarantee."""

    class ProbeSelectorFailureProcess:
        def __init__(self) -> None:
            self.stdout = _task7d_closed_pipe(b"2.1.251\n")
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = ProbeSelectorFailureProcess()

    def allocation_failure() -> object:
        raise OSError("selector allocation failed")

    monkeypatch.setattr(runner.selectors, "DefaultSelector", allocation_failure)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(runner.RunnerError, match="probe unavailable"):
        runner._registered_probe(
            argv=[str((tmp_path / "registered-client").resolve()), "--version"],
            cwd=tmp_path,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
            probe_runner=None,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]
    assert process.stdout.closed and process.stderr.closed


def test_live_phase_streams_native_rows_and_snapshots_before_waiting_for_exit(
    tmp_path: Path,
) -> None:
    """A later shim interval must follow the arrived native snapshot, not replay.

    This would fail if the live launcher used ``communicate()``, deferred
    native trace allocation until after ``wait()``, or failed to drain stderr
    while stdout remained open.
    """

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    (workspace / "observed.md").write_text("initial\n")
    writer = runner.EvidenceWriter(control_root, "b" * 64)
    workspace_pin = runner.PinnedDirectory.pin(workspace, private=False)
    snapshotter = runner.WorkspaceSnapshotter(
        workspace=workspace, workspace_pin=workspace_pin, writer=writer,
    )
    trace = runner.ControlTraceWriter(control_root, "execution-1")
    first_snapshot = threading.Event()
    snapshots: list[runner.WorkspaceSnapshot] = []

    def snapshot(raw: bytes, start: int, end: int, sequence: int) -> str:
        captured = snapshotter.capture(
            execution_id="execution-1",
            trace_sequence=sequence,
            role="marker",
            native_record={
                "transcript_id": "transcript-1", "byte_start": start,
                "byte_end": end, "sha256": runner._sha256(raw),
            },
        )
        snapshots.append(captured)
        if sequence == 1:
            first_snapshot.set()
        return captured.artifact_id

    class StreamingFakeProcess:
        def __init__(self) -> None:
            stdout_read, self._stdout_write = os.pipe()
            stderr_read, self._stderr_write = os.pipe()
            self.stdin = _Task7dRecordingInput()
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
            self.wait_called = False
            self.communicated = False
            self.streams_closed = threading.Event()
            self.producer_errors: list[BaseException] = []
            self.producer = threading.Thread(target=self._produce, daemon=True)
            self.producer.start()

        def _produce(self) -> None:
            try:
                os.write(self._stdout_write, b'{"record":1}\n')
                if not first_snapshot.wait(timeout=2):
                    raise AssertionError("first native snapshot was not captured at arrival")
                # The trace append cannot acquire the shared lock until the
                # native row's snapshot and row are both durable.
                trace.append("command_start", command_id="between")
                trace.append("command_end", command_id="between")
                os.write(self._stderr_write, b"concurrent diagnostic\n")
                os.write(self._stdout_write, b'{"record":2}\n')
            except BaseException as error:  # surfaced by wait below
                self.producer_errors.append(error)
            finally:
                os.close(self._stdout_write)
                os.close(self._stderr_write)
                self.streams_closed.set()

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            self.producer.join(timeout=2)
            assert not self.producer.is_alive()
            assert not self.producer_errors
            assert self.streams_closed.is_set()
            assert self.stdout.closed and self.stderr.closed
            self.wait_called = True
            return 0

        def communicate(self, *args: object, **kwargs: object) -> None:
            self.communicated = True
            raise AssertionError("live evidence must not use communicate")

    process = StreamingFakeProcess()
    try:
        capture = runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"sealed prompt",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=snapshot,
            popen_factory=lambda *args, **kwargs: process,
        )
    finally:
        workspace_pin.close()

    assert capture.exit_code == 0
    assert capture.stdout == b'{"record":1}\n{"record":2}\n'
    assert capture.stderr == b"concurrent diagnostic\n"
    assert process.stdin.writes == [b"sealed prompt"]
    assert process.wait_called and not process.communicated
    rows = trace.records()
    assert [row["kind"] for row in rows] == [
        "native_record", "command_start", "command_end", "native_record",
    ]
    assert [row["workspace_snapshot_id"] for row in rows if row["kind"] == "native_record"] == [
        snapshot.artifact_id for snapshot in snapshots
    ]
    assert [snapshot.trace_sequence for snapshot in snapshots] == [1, 4]


def test_live_phase_reaps_its_child_when_snapshot_capture_raises_non_runner_error(
    tmp_path: Path,
) -> None:
    """A callback failure must not leak a launched client process.

    This would fail if the live reader reaped only ``RunnerError`` paths and
    allowed an ordinary snapshot/IO exception to bypass process cleanup.
    """

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class SnapshotFailureProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"record":1}\n')
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_calls = 0

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 5.0
            self.wait_calls += 1
            return 143

        def communicate(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("live evidence must not use communicate")

    process = SnapshotFailureProcess()
    with pytest.raises(ValueError, match="snapshot failed"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda *_args: (_ for _ in ()).throw(ValueError("snapshot failed")),
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_calls == 1


def test_live_phase_closes_pipes_and_reaps_on_stdin_delivery_failure(
    tmp_path: Path,
) -> None:
    """A failed prompt write must not leak pipe descriptors or a child.

    This would fail if the pre-selector stdin path terminated a child but
    bypassed the stream cleanup/final bounded reap path.
    """

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class FailingInput:
        def write(self, value: bytes) -> int:
            raise OSError("stdin closed")

        def flush(self) -> None:
            raise AssertionError("flush must not follow failed write")

        def close(self) -> None:
            return None

    class StdinFailureProcess:
        def __init__(self) -> None:
            self.stdin = FailingInput()
            self.stdout = _task7d_closed_pipe(b"")
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = StdinFailureProcess()
    with pytest.raises(runner.RunnerError, match="stdin delivery failed"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"sealed prompt",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]
    assert process.stdout.closed and process.stderr.closed


def test_live_phase_uses_a_bounded_final_wait_and_reaps_a_timeout(
    tmp_path: Path,
) -> None:
    """EOF does not authorize an unbounded wait for a stalled child.

    This would fail if the pipe reader used ``wait()`` without a timeout after
    draining stdout/stderr, leaving an evaluator process indefinitely hung.
    """

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")

    class TimedOutProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"record":1}\n')
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            if self.terminated:
                return 143
            if timeout is None:
                raise AssertionError("final process wait was unbounded")
            raise subprocess.TimeoutExpired("registered-client", timeout)

        def communicate(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("live evidence must not use communicate")

    process = TimedOutProcess()
    with pytest.raises(runner.RunnerError, match="process timed out"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert len(process.wait_timeouts) == 2
    assert all(timeout is not None and timeout > 0 for timeout in process.wait_timeouts)
    assert process.stdout.closed and process.stderr.closed


def test_live_phase_enforces_a_total_deadline_before_a_stalled_stream_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous process ownership has a total deadline, not only idle polls."""

    control_root = tmp_path / "control"
    workspace = tmp_path / "workspace"
    control_root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    trace = runner.ControlTraceWriter(control_root, "execution-1")
    monkeypatch.setattr(runner, "_LIVE_PHASE_TOTAL_TIMEOUT_SECONDS", 0.0, raising=False)

    class DeadlineProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b"")
            self.stderr = _task7d_closed_pipe(b"")
            self.terminated = False
            self.wait_timeouts: list[float | None] = []

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            return 143

    process = DeadlineProcess()
    with pytest.raises(runner.RunnerError, match="process timed out"):
        runner._stream_live_phase(
            argv=[str((tmp_path / "registered-client").resolve()), "--print"],
            cwd=workspace,
            environment={"PATH": "/usr/bin:/bin", "HOME": str(control_root / "home")},
            stdin=b"",
            trace=trace,
            transcript_id="transcript-1",
            on_native_record=lambda _raw, _start, _end, sequence: f"snapshot-{sequence}",
            popen_factory=lambda *args, **kwargs: process,
        )

    assert process.terminated
    assert process.wait_timeouts == [5.0]


def _task7d_candidate_log(
    *,
    run_id: str,
    scenario: dict[str, object],
    fixture_sha256: str,
) -> dict[str, object]:
    """Make a deliberately skeletal pass candidate for finalizer-order tests.

    The test validator is the only component permitted to accept this shape;
    real public validation would (correctly) reject it as incomplete evidence.
    """

    candidate = runner._incomplete_log(
        run_id=run_id,
        scenario=scenario,
        client="claude",
        version="2.1.251",
        fixture_sha256=fixture_sha256,
        executions=[],
        reasons=["placeholder"],
    )
    candidate["result"] = "pass"
    candidate["incomplete_reasons"] = []
    return candidate


def test_prepublication_candidate_is_validated_after_seal_and_before_pass_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass file is published only after the sealed candidate validates."""

    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o700)
    scenario = runner._scenario("current-wiki-fast-path")
    writer = runner.EvidenceWriter(control_root, "c" * 64)
    writer.add_json("candidate-note", "assertion", {"reason": "test candidate"})
    candidate = _task7d_candidate_log(
        run_id=writer.run_id, scenario=scenario, fixture_sha256="d" * 64,
    )
    observed: list[dict[str, object]] = []

    def validate_before_publish(
        log: dict[str, object], schema: dict[str, object], validated_scenario: dict[str, object],
        fixture_sha256: str, context: object,
    ) -> None:
        assert (writer.run_root / "evidence-index.json").is_file()
        assert (writer.run_root / "log-attestation.json").is_file()
        assert not (writer.run_root / "event-log.json").exists()
        assert log["result"] == "pass"
        assert schema["type"] == "object"
        assert validated_scenario is scenario
        assert fixture_sha256 == "d" * 64
        assert context == "test-context"
        observed.append(log)

    monkeypatch.setattr(runner, "_validate_prepublication_event_log", validate_before_publish, raising=False)
    result = runner._atomic_validate_and_publish_candidate(
        writer=writer,
        candidate_log=candidate,
        scenario=scenario,
        fixture_sha256="d" * 64,
        trusted_context="test-context",
    )

    assert result.published is True
    assert observed == [result.log]
    assert result.log["result"] == "pass"
    assert result.log_path == writer.run_root / "event-log.json"
    assert json.loads(result.log_path.read_text()) == result.log


def test_prepublication_validation_failure_leaves_only_an_incomplete_diagnostic_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected staged evidence cannot leave a pass file behind to be mistaken for one."""

    from tests.evals import event_log_contract as contract

    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o700)
    scenario = runner._scenario("current-wiki-fast-path")
    writer = runner.EvidenceWriter(control_root, "e" * 64)
    writer.add_json("candidate-note", "assertion", {"reason": "test candidate"})
    candidate = _task7d_candidate_log(
        run_id=writer.run_id, scenario=scenario, fixture_sha256="f" * 64,
    )

    def reject_before_publish(*_args: object, **_kwargs: object) -> None:
        assert (writer.run_root / "evidence-index.json").is_file()
        assert (writer.run_root / "log-attestation.json").is_file()
        assert not (writer.run_root / "event-log.json").exists()
        raise contract.EventLogContractError("deliberate candidate rejection")

    monkeypatch.setattr(runner, "_validate_prepublication_event_log", reject_before_publish, raising=False)
    result = runner._atomic_validate_and_publish_candidate(
        writer=writer,
        candidate_log=candidate,
        scenario=scenario,
        fixture_sha256="f" * 64,
        trusted_context="test-context",
    )

    assert result.published is False
    assert result.stage_run_id == writer.run_id
    assert not (writer.run_root / "event-log.json").exists()
    assert result.log_path != writer.run_root / "event-log.json"
    assert result.log["result"] == "incomplete"
    assert result.log["events"] == []
    assert result.log["receipts"] == []
    assert result.log["deliveries"] == []
    assert "prepublication_validation_failed" in result.log["incomplete_reasons"]
    assert json.loads(result.log_path.read_text()) == result.log
    diagnostic_entries = json.loads((result.log_path.parent / "evidence-index.json").read_text())["entries"]
    assert [entry["type"] for entry in diagnostic_entries] == ["assertion"]


def test_ordinary_finalizers_reject_a_pass_outside_atomic_validation(
    tmp_path: Path,
) -> None:
    """No convenience finalizer may publish a syntactically pass-shaped log.

    This would fail if a caller could bypass the atomic validator simply by
    calling either ordinary finalization helper with ``result='pass'``. In
    particular, no module-level post-validation capability factory may remain
    available for a future caller to invoke without having validated.
    """

    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o700)
    scenario = runner._scenario("current-wiki-fast-path")
    writer = runner.EvidenceWriter(control_root, "a" * 64)
    writer.add_json("candidate-note", "assertion", {"reason": "test candidate"})
    candidate = _task7d_candidate_log(
        run_id=writer.run_id, scenario=scenario, fixture_sha256="b" * 64,
    )

    with pytest.raises(runner.RunnerError, match="incomplete"):
        runner._finalize_log(writer, candidate)
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()

    runner._seal_and_attest_log(writer, candidate)
    assert not hasattr(runner, "_post_validation_pass_publication")
    assert not hasattr(runner, "_PostValidationPassPublication")
    with pytest.raises(runner.RunnerError, match="incomplete"):
        runner._publish_sealed_log(writer, candidate)
    assert not (writer.run_root / "event-log.json").exists()


def test_pass_publication_has_no_reusable_cross_writer_helper(
    tmp_path: Path,
) -> None:
    """A sealed pass has no helper path outside atomic validation.

    This is deliberately stronger than testing a capability's identity: the
    capability factory itself must not exist for a caller to mint. Both
    independently sealed candidates therefore remain unpublished through the
    ordinary helper.
    """

    scenario = runner._scenario("current-wiki-fast-path")
    control_one = tmp_path / "control-one"
    control_two = tmp_path / "control-two"
    control_one.mkdir(mode=0o700)
    control_two.mkdir(mode=0o700)
    first = runner.EvidenceWriter(control_one, "c" * 64)
    second = runner.EvidenceWriter(control_two, "d" * 64)
    first.add_json("candidate-note", "assertion", {"reason": "first"})
    second.add_json("candidate-note", "assertion", {"reason": "second"})
    first_log = _task7d_candidate_log(
        run_id=first.run_id, scenario=scenario, fixture_sha256="e" * 64,
    )
    second_log = _task7d_candidate_log(
        run_id=second.run_id, scenario=scenario, fixture_sha256="e" * 64,
    )
    runner._seal_and_attest_log(first, first_log)
    runner._seal_and_attest_log(second, second_log)
    assert not hasattr(runner, "_post_validation_pass_publication")
    with pytest.raises(runner.RunnerError, match="incomplete"):
        runner._publish_sealed_log(second, second_log)
    assert not (second.run_root / "event-log.json").exists()

    with pytest.raises(runner.RunnerError, match="incomplete"):
        runner._publish_sealed_log(first, first_log)
    assert not (first.run_root / "event-log.json").exists()


def test_prepublication_validator_cannot_mutate_the_candidate_it_just_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation receives a detached copy, never the to-be-published mapping.

    This would fail if a validator hook could alter candidate bytes after
    attestation and have that unvalidated mutation become the public pass.
    """

    control_root = tmp_path / "control"
    control_root.mkdir(mode=0o700)
    scenario = runner._scenario("current-wiki-fast-path")
    writer = runner.EvidenceWriter(control_root, "f" * 64)
    writer.add_json("candidate-note", "assertion", {"reason": "test candidate"})
    candidate = _task7d_candidate_log(
        run_id=writer.run_id, scenario=scenario, fixture_sha256="0" * 64,
    )

    def mutate_validation_view(
        log: dict[str, object], *_args: object, **_kwargs: object,
    ) -> None:
        log["client_version"] = "mutated-by-validator"

    monkeypatch.setattr(
        runner, "_validate_prepublication_event_log", mutate_validation_view,
    )
    result = runner._atomic_validate_and_publish_candidate(
        writer=writer, candidate_log=candidate, scenario=scenario,
        fixture_sha256="0" * 64, trusted_context="test-context",
    )

    assert result.published is True
    assert result.log["client_version"] == "2.1.251"
    assert json.loads((writer.run_root / "event-log.json").read_text())["client_version"] == "2.1.251"


def test_injected_process_never_reaches_the_atomic_prepublication_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old ProcessRunner seam remains incapable of a candidate pass."""

    validated: list[object] = []
    monkeypatch.setattr(
        runner, "_validate_prepublication_event_log",
        lambda *_args, **_kwargs: validated.append("called"), raising=False,
    )

    def injected(
        _argv: list[str], _cwd: Path, _environment: dict[str, str], _stdin: bytes,
    ) -> runner.ProcessCapture:
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"type":"result"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=injected,
    )

    assert validated == []
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]


def test_registered_actual_preparation_uses_only_runner_owned_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered bridge entry prepares a closed plan without caller authority.

    Its signature intentionally excludes caller argv, executable, environment,
    process, and adapter inputs. The symbolic profile and network attestation
    come solely from the selected client, while the executable comes only from
    the code-owned registration map.
    """

    registration = runner.RunnerOwnedExecutableRegistration(
        client="claude",
        resolved_path=Path("/runner-owned/closed/claude"),
        expected_version="2.1.251",
        native_format="claude-stream-json-2.1.251-v1",
    )
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({"claude": registration}),
    )
    monkeypatch.setenv("PATH", str(tmp_path / "path-shadow"))
    launches: list[object] = []
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    signature = inspect.signature(runner.prepare_registered_actual_scenario)
    assert tuple(signature.parameters) == ("client", "scenario_id", "scratch_root")
    assert not {
        "client_command_json", "network_attestation", "process_runner",
        "probe_runner", "popen_factory", "environment", "executable",
    } & set(signature.parameters)

    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
    )

    assert plan.client == "claude"
    assert plan.scenario_id == "current-wiki-fast-path"
    assert plan.scratch_root == tmp_path
    assert plan.symbolic_template == tuple(_claude_template())
    assert plan.network_attestation == "mock-only"
    assert plan.registration is registration
    assert launches == []

    with pytest.raises(runner.RunnerError, match="phase-one diagnostic bridge"):
        runner.run_registered_actual_scenario(
            client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
        )
    assert launches == []


def test_registered_actual_preparation_rejects_the_test_live_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual preparation boundary cannot inherit test-only adapters."""

    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", object())

    with pytest.raises(runner.RunnerError, match="test-only"):
        runner.prepare_registered_actual_scenario(
            client="claude", scenario_id="current-wiki-fast-path", scratch_root=None,
        )


def test_registered_actual_preparation_and_rejected_bridge_run_leave_no_live_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation and a rejected bridge plan cannot touch live state or scratch.

    This is intentionally stronger than the signature check: even if live
    helpers are available in the module, both preparation and a rejected
    non-web named run entry must stop before any of them can receive a call.
    """

    def scratch_inventory(root: Path) -> tuple[tuple[object, ...], ...]:
        """Capture root/tree lstat state without following symlinks."""

        entries: list[tuple[object, ...]] = []

        def visit(path: Path, relative: str) -> None:
            info = path.lstat()
            kind = stat.S_IFMT(info.st_mode)
            target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
            payload = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            entries.append((relative, kind, stat.S_IMODE(info.st_mode), target, payload))
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    visit(child, child.name if relative == "." else relative + "/" + child.name)

        visit(root, ".")
        return tuple(entries)

    scratch_root = tmp_path / "scratch-sentinel"
    scratch_root.mkdir(mode=0o700)
    sentinel = scratch_root / "must-remain-untouched"
    sentinel.write_bytes(b"preparation has no workspace side effect\n")
    (scratch_root / "empty-directory").mkdir(mode=0o700)
    link = scratch_root / "sentinel-link"
    link.symlink_to(sentinel.name)
    before = scratch_inventory(scratch_root)
    assert (".", stat.S_IFDIR, stat.S_IMODE(scratch_root.lstat().st_mode), None, None) in before
    assert ("empty-directory", stat.S_IFDIR, stat.S_IMODE((scratch_root / "empty-directory").lstat().st_mode), None, None) in before
    assert ("sentinel-link", stat.S_IFLNK, stat.S_IMODE(link.lstat().st_mode), sentinel.name, None) in before
    registration = runner.RunnerOwnedExecutableRegistration(
        client="claude",
        resolved_path=Path("/runner-owned/closed/claude"),
        expected_version="2.1.251",
        native_format="claude-stream-json-2.1.251-v1",
    )
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({"claude": registration}),
    )

    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail(f"preparation unexpectedly called {name}")
        return reject

    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("launch"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("publish"),
    )

    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=scratch_root,
    )
    assert plan.registration is registration
    with pytest.raises(runner.RunnerError, match="phase-one diagnostic bridge"):
        runner.run_registered_actual_scenario(
            client="claude", scenario_id="current-wiki-fast-path", scratch_root=scratch_root,
        )

    after = scratch_inventory(scratch_root)
    assert calls == []
    assert after == before


def test_legacy_cli_and_run_scenario_cannot_select_registered_actual_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Existing fake and CLI surfaces remain outside the registered boundary."""

    def unexpected_registered_prepare(*_args: object, **_kwargs: object) -> object:
        pytest.fail("legacy entrypoint selected registered actual preparation")

    monkeypatch.setattr(
        runner, "prepare_registered_actual_scenario", unexpected_registered_prepare,
    )

    def injected(
        _argv: list[str], _cwd: Path, _environment: dict[str, str], _stdin: bytes,
    ) -> runner.ProcessCapture:
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"type":"result"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=injected,
    )
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]

    with pytest.raises(SystemExit):
        runner._cli_parser().parse_args([
            "--client", "claude", "--scenario", "current-wiki-fast-path",
            "--client-command-json", json.dumps(_claude_template()),
            "--network-attestation", "mock-only", "--registered-actual",
        ])

    status = runner.main([
        "--client", "claude", "--scenario", "current-wiki-fast-path",
        "--client-command-json", json.dumps(_claude_template()),
        "--network-attestation", "mock-only",
    ])
    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["result"] == "incomplete"


def _task7d_closed_registered_claude() -> runner.RunnerOwnedExecutableRegistration:
    """Return a code-owned registration without touching a client binary."""

    return runner.RunnerOwnedExecutableRegistration(
        client="claude",
        resolved_path=Path("/runner-owned/closed/claude"),
        expected_version="2.1.251",
        native_format="claude-stream-json-2.1.251-v1",
    )


def test_registered_actual_entry_dispatches_only_private_closed_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the public entry could stop outside its actual-only dispatcher."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    dispatched: list[object] = []

    def dispatcher(authority: object) -> object:
        dispatched.append(authority)
        raise runner.RunnerError("private dispatcher reached")

    monkeypatch.setattr(runner, "_run_registered_actual_plan", dispatcher, raising=False)

    with pytest.raises(runner.RunnerError, match="private dispatcher reached"):
        runner.run_registered_actual_scenario(
            client="claude", scenario_id="current-wiki-fast-path", scratch_root=None,
        )

    assert len(dispatched) == 1
    assert isinstance(dispatched[0], runner._RegisteredActualAuthority)
    assert dispatched[0].plan.registration is registration


def test_registered_actual_public_and_private_route_inputs_are_closed() -> None:
    """Break caught: an adapter or caller-selected launch input could enter either route."""

    public_signature = inspect.signature(runner.run_registered_actual_scenario)
    assert tuple(public_signature.parameters) == ("client", "scenario_id", "scratch_root")
    forbidden = {
        "client_command_json", "network_attestation", "argv", "template", "executable",
        "environment", "path", "home", "prompt", "process_runner", "probe_runner",
        "popen_factory", "supported_builds", "trusted_context", "mode",
    }
    assert not forbidden & set(public_signature.parameters)

    private_signature = inspect.signature(runner._run_registered_actual_plan)
    assert tuple(private_signature.parameters) == ("authority",)
    assert not forbidden & set(private_signature.parameters)


def test_registered_actual_test_hook_rejects_before_dispatch_or_live_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a test adapter could reach actual workspace/probe/spawn work."""

    dispatched: list[str] = []
    side_effects: list[str] = []

    def dispatcher(*_args: object, **_kwargs: object) -> object:
        dispatched.append("dispatcher")
        pytest.fail("test hook reached private actual dispatcher")

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            side_effects.append(name)
            pytest.fail("test hook reached " + name)
        return reject

    monkeypatch.setattr(runner, "_run_registered_actual_plan", dispatcher, raising=False)
    monkeypatch.setattr(runner, "prepare_isolated_workspace", forbidden("workspace"))
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("launch"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("publish"),
    )
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden("popen"))
    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", object())

    with pytest.raises(runner.RunnerError, match="test-only"):
        runner.run_registered_actual_scenario(
            client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
        )

    assert dispatched == []
    assert side_effects == []


def test_registered_actual_unregistered_codex_rejects_before_dispatch_or_live_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an unsupported Codex key could reach actual setup before rejection."""

    dispatched: list[str] = []
    side_effects: list[str] = []

    def dispatcher(*_args: object, **_kwargs: object) -> object:
        dispatched.append("dispatcher")
        pytest.fail("unregistered Codex reached private actual dispatcher")

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            side_effects.append(name)
            pytest.fail("unregistered Codex reached " + name)
        return reject

    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({"claude": _task7d_closed_registered_claude()}),
    )
    monkeypatch.setattr(runner, "_run_registered_actual_plan", dispatcher, raising=False)
    monkeypatch.setattr(runner, "prepare_isolated_workspace", forbidden("workspace"))
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("launch"))
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden("popen"))
    scratch_root = tmp_path / "must-not-be-created"

    with pytest.raises(runner.RunnerError, match="not runner-registered"):
        runner.run_registered_actual_scenario(
            client="codex", scenario_id="current-wiki-fast-path", scratch_root=scratch_root,
        )

    assert not scratch_root.exists()
    assert dispatched == []
    assert side_effects == []


def test_private_registered_actual_dispatcher_never_uses_legacy_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the actual route could launder authority through run_scenario."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=None,
    )

    def unexpected_legacy_runner(*_args: object, **_kwargs: object) -> object:
        pytest.fail("private actual dispatcher called legacy run_scenario")

    monkeypatch.setattr(runner, "run_scenario", unexpected_legacy_runner)
    monkeypatch.setattr(
        runner,
        "_launch_registered_live_phase",
        lambda *_args, **_kwargs: pytest.fail("rejected bridge plan reached live launcher"),
    )
    monkeypatch.setattr(
        runner,
        "_atomic_validate_and_publish_candidate",
        lambda *_args, **_kwargs: pytest.fail("rejected bridge plan reached publisher"),
    )

    with pytest.raises(runner.RunnerError, match="phase-one diagnostic bridge"):
        runner._run_registered_actual_plan(runner._RegisteredActualAuthority(plan))


def test_legacy_routes_never_dispatch_registered_actual_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Break caught: legacy API or CLI could gain a path to actual authority."""

    dispatched: list[object] = []
    side_effects: list[str] = []
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({"claude": _task7d_closed_registered_claude()}),
    )

    def dispatcher(authority: object) -> object:
        dispatched.append(authority)
        pytest.fail("legacy route reached private actual dispatcher")

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            side_effects.append(name)
            pytest.fail("legacy route reached " + name)
        return reject

    monkeypatch.setattr(runner, "_run_registered_actual_plan", dispatcher, raising=False)
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("launch"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("publish"),
    )

    def injected(
        _argv: list[str], _cwd: Path, _environment: dict[str, str], _stdin: bytes,
    ) -> runner.ProcessCapture:
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"type":"result"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=injected,
    )
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]

    status = runner.main([
        "--client", "claude", "--scenario", "current-wiki-fast-path",
        "--client-command-json", json.dumps(_claude_template()),
        "--network-attestation", "mock-only",
    ])
    report = json.loads(capsys.readouterr().out)
    assert status == 1
    assert report["result"] == "incomplete"
    assert dispatched == []
    assert side_effects == []


def test_actual_prepared_run_factory_binds_closed_plan_to_fresh_isolated_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: preparation could launch, emit semantics, or detach its roots.

    This deliberately exercises only the bridge's disconnected workspace and
    private evidence allocation.  It creates no phase control, prompt,
    process, phase-two, semantic, pass, or public-log artifact.
    """

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    scratch_root = tmp_path / "actual-prepared-scratch"
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=scratch_root,
    )
    authority = runner._RegisteredActualAuthority(plan)
    calls: list[str] = []
    workspace_calls: list[tuple[str, Path | None]] = []
    git_processes: list[list[str]] = []
    original_prepare_workspace = runner.prepare_isolated_workspace
    original_popen = runner.subprocess.Popen

    def observe_workspace(
        scenario_id: str,
        *,
        scratch_root: Path | None = None,
    ) -> runner.PreparedWorkspace:
        workspace_calls.append((scenario_id, scratch_root))
        return original_prepare_workspace(scenario_id, scratch_root=scratch_root)

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("prepared-run construction reached " + name)
        return reject

    def observe_git_popen(argv: object, *args: object, **kwargs: object) -> object:
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            pytest.fail("prepared-run construction used a noncanonical process argv")
        git_processes.append(list(argv))
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(runner, "prepare_isolated_workspace", observe_workspace)
    # The copied fixture initializes a private Git repository.  That is the
    # only allowed subprocess activity in this construction-only slice.
    monkeypatch.setattr(runner.subprocess, "Popen", observe_git_popen)
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(
        runner, "_prepare_registered_live_executable", forbidden("executable-preparation"),
    )
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("live-launch"))
    monkeypatch.setattr(
        runner, "_launch_actual_registered_live_phase", forbidden("actual-launch"),
    )
    monkeypatch.setattr(runner, "_phase_control", forbidden("phase-control"))
    monkeypatch.setattr(runner, "_write_runner_state", forbidden("runner-state"))
    monkeypatch.setattr(runner, "_index_phase_prompt", forbidden("phase-prompt"))
    monkeypatch.setattr(
        runner, "_RegisteredActualPreparedPhase", forbidden("prepared-phase"),
    )
    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("private-replay"))
    monkeypatch.setattr(runner, "_finalize_log", forbidden("finalize"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("publish"),
    )
    monkeypatch.setattr(runner, "run_scenario", forbidden("legacy-run"))

    signature = inspect.signature(runner._prepare_registered_actual_run)
    assert tuple(signature.parameters) == ("authority",)
    forbidden_inputs = {
        "client_command_json", "network_attestation", "process_runner", "probe_runner",
        "popen_factory", "argv", "environment", "template", "prompt", "executable",
    }
    assert not forbidden_inputs & set(signature.parameters)

    prepared_runs: list[object] = []
    try:
        prepared_runs = [
            runner._prepare_registered_actual_run(authority),
            runner._prepare_registered_actual_run(authority),
        ]
        first, second = prepared_runs
        assert type(first) is runner._RegisteredActualPreparedRun
        assert type(second) is runner._RegisteredActualPreparedRun
        assert first.authority is authority
        assert second.authority is authority
        assert workspace_calls == [
            (plan.scenario_id, plan.scratch_root),
            (plan.scenario_id, plan.scratch_root),
        ]
        assert git_processes
        assert all(process[0] == str(runner._runner_git_binary()) for process in git_processes)
        assert first.prepared.scenario["id"] == plan.scenario_id
        assert first.prepared.run_root.parent == scratch_root.resolve()
        assert second.prepared.run_root.parent == scratch_root.resolve()
        assert first.prepared.run_root != second.prepared.run_root
        assert first.run_id != second.run_id
        assert first.writer.run_root != second.writer.run_root
        for prepared_run in (first, second):
            prepared = prepared_run.prepared
            assert prepared.control_root != prepared.workspace
            assert not prepared.control_root.is_relative_to(prepared.workspace)
            assert not prepared.workspace.is_relative_to(prepared.control_root)
            assert prepared.control_pin.path == prepared.control_root
            assert prepared.workspace_pin.path == prepared.workspace
            assert prepared.control_pin.private is True
            assert prepared.workspace_pin.private is False
            prepared.control_pin.verify()
            prepared.workspace_pin.verify()
            assert prepared_run.writer.control_root == prepared.control_root
            assert prepared_run.writer.run_id == prepared_run.run_id
            assert prepared_run.writer.run_root == prepared.control_root / "runs" / prepared_run.run_id
            assert prepared_run.writer.entries == []
            assert not (prepared_run.writer.run_root / "evidence-index.json").exists()
            assert not list(prepared_run.writer.run_root.glob("artifacts/*"))
        assert calls == []
    finally:
        for prepared_run in prepared_runs:
            prepared_run.prepared.control_pin.close()
            prepared_run.prepared.workspace_pin.close()


def test_actual_prepared_run_factory_rejects_test_hook_and_detached_plan_before_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an adapter or altered plan could create an actual workspace."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
    )
    authority = runner._RegisteredActualAuthority(plan)
    workspace_calls: list[object] = []

    def no_workspace(*_args: object, **_kwargs: object) -> object:
        workspace_calls.append("workspace")
        pytest.fail("rejected prepared-run input reached workspace construction")

    monkeypatch.setattr(runner, "prepare_isolated_workspace", no_workspace)
    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", object())
    with pytest.raises(runner.RunnerError, match="test-only"):
        runner._prepare_registered_actual_run(authority)
    assert workspace_calls == []

    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", None)
    with pytest.raises(runner.RunnerError, match="authority"):
        runner._prepare_registered_actual_run(object())
    assert workspace_calls == []

    detached_plan = replace(plan, symbolic_template=("not", "runner-owned"))
    detached_authority = runner._RegisteredActualAuthority(detached_plan)
    with pytest.raises(runner.RunnerError, match="runner-owned"):
        runner._prepare_registered_actual_run(detached_authority)
    assert workspace_calls == []


def test_phase_one_bridge_rejects_non_web_plan_without_prepared_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a non-web plan could allocate the bridge's run state."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
    )
    calls: list[str] = []

    def no_prepared_run(*_args: object, **_kwargs: object) -> object:
        calls.append("prepared-run")
        pytest.fail("non-web bridge plan constructed a prepared run")

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_run", no_prepared_run, raising=False,
    )
    with pytest.raises(runner.RunnerError, match="phase-one diagnostic bridge"):
        runner._run_registered_actual_plan(runner._RegisteredActualAuthority(plan))
    assert calls == []


def test_registered_actual_phase_one_bridge_rejects_non_web_plan_before_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: any plan outside Claude/web could allocate a workspace."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path", scratch_root=tmp_path,
    )
    workspace_calls: list[str] = []

    def no_workspace(*_args: object, **_kwargs: object) -> object:
        workspace_calls.append("workspace")
        pytest.fail("non-web bridge plan reached workspace setup")

    monkeypatch.setattr(runner, "prepare_isolated_workspace", no_workspace)
    with pytest.raises(runner.RunnerError, match="phase-one diagnostic bridge"):
        runner._run_registered_actual_plan(runner._RegisteredActualAuthority(plan))
    assert workspace_calls == []


@pytest.mark.parametrize(
    ("gate_result", "expected_reason"),
    [
            ("success", "public_semantic_projection_unavailable"),
            ("real", "public_semantic_projection_unavailable"),
            ("eager", "web_approval_phase_unverified"),
            ("none", "web_approval_phase_unverified"),
        ("error", None),
        ("post_gate_unbound", None),
        ("malformed", None),
    ],
)
def test_registered_actual_phase_one_bridge_finalizes_only_indexed_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_result: str,
    expected_reason: str | None,
) -> None:
    """Break caught: actual bridge could loop, project semantics, or pass-publish."""

    _writer, _control, _workspace, executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    control_pin.close()
    workspace_pin.close()
    original_popen = runner.subprocess.Popen
    calls: list[str] = []
    probes: list[list[str]] = []
    client_spawns: list[tuple[list[str], dict[str, object]]] = []
    non_client_spawns: list[list[str]] = []
    local_brain_commands: list[list[str]] = []
    native_recorded = threading.Event()
    expected_git_init = [
        str(runner._runner_git_binary()),
        "-c", "init.templateDir=", "-c", "core.hooksPath=", "init", "-q",
    ]

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("phase-one bridge reached " + name)
        return reject

    # The private phase-two launcher may exist for a later disconnected
    # activation slice, but the phase-one diagnostic dispatcher must never
    # enter it.  Keep the sentinel local to the actual-only bridge rather
    # than relying on a public/legacy route to prove that boundary.
    monkeypatch.setattr(
        runner,
        "_launch_registered_actual_phase_two",
        forbidden("phase-two-private-launcher"),
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_run_registered_actual_public_web_route",
        forbidden("public-web-route"),
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_verify_registered_actual_phase_two_post_launch_provenance",
        forbidden("phase-two-post-launch-provenance"),
        raising=False,
    )

    def probe(
        *, argv: list[str], cwd: Path, environment: dict[str, str], probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, environment, probe_runner
        probes.append(list(argv))
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class CompleteProcess:
        def __init__(self, stdout_chunks: object) -> None:
            stdout_read, self._stdout_write = os.pipe()
            stderr_read, self._stderr_write = os.pipe()
            self.stdin = _Task7dRecordingInput()
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
            self._stdout_chunks = stdout_chunks
            self.producer_errors: list[BaseException] = []
            self.producer = threading.Thread(target=self._produce, daemon=True)
            self.producer.start()

        def _produce(self) -> None:
            try:
                for chunk in self._stdout_chunks:  # type: ignore[union-attr]
                    native_recorded.clear()
                    os.write(self._stdout_write, chunk)
                    if (gate_result != "eager"
                            and not native_recorded.wait(timeout=30)):
                        raise AssertionError("native row was not trace-bound before next shim command")
                os.write(self._stderr_write, b"diagnostic\n")
            except BaseException as error:
                self.producer_errors.append(error)
            finally:
                os.close(self._stdout_write)
                os.close(self._stderr_write)

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            self.producer.join(timeout=2)
            assert not self.producer.is_alive()
            assert not self.producer_errors
            assert self.stdout.closed and self.stderr.closed
            return 0

    def popen(argv: list[str], **kwargs: object) -> object:
        if argv[0] == str(executable):
            assert "mock-web-capture" not in argv
            assert "--fixture-id" not in argv
            if client_spawns:
                pytest.fail("bridge attempted a second registered-client launch")
            client_spawns.append((list(argv), dict(kwargs)))
            transcript = _initial_sync_receipt_lifecycle_stream(
                kwargs["cwd"], kwargs["env"],  # type: ignore[arg-type]
            )
            if gate_result == "eager":
                # This deliberately invalid transport reproduces the old
                # eager join: every command precedes every native marker, so
                # the real sealed gate must withhold its release token.
                transcript = [b"".join(transcript)]
            return CompleteProcess(transcript)
        if argv == expected_git_init:
            non_client_spawns.append(list(argv))
            return original_popen(argv, **kwargs)
        if "mock-web-capture" in argv:
            return forbidden("mock-web-capture")()  # type: ignore[return-value]
        if argv[0] == str(kwargs.get("cwd", "") / "brain"):
            local_brain_commands.append(list(argv))
            suffix = argv[1:]
            if suffix == ["--json", "sync"]:
                return original_popen(argv, **kwargs)
            if (len(suffix) == 5
                    and suffix[:4] in (
                        ["--json", "source", "consume-sync-result", "--result-id"],
                        ["--json", "source", "acknowledge-sync-result", "--result-id"],
                    )
                    and suffix[4].startswith("sync_")):
                return original_popen(argv, **kwargs)
            return forbidden("unexpected-local-brain-command")()  # type: ignore[return-value]
        pytest.fail("bridge attempted unallowlisted subprocess: " + repr(argv))

    monkeypatch.setattr(runner, "_registered_probe", probe)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    original_append_native = runner.ControlTraceWriter.append_native_record

    def signal_native_record(
        trace: runner.ControlTraceWriter,
        *,
        transcript_id: str,
        byte_start: int,
        byte_end: int,
        sha256: str,
        snapshot_factory: Callable[[int], str],
    ) -> int:
        sequence = original_append_native(
            trace,
            transcript_id=transcript_id,
            byte_start=byte_start,
            byte_end=byte_end,
            sha256=sha256,
            snapshot_factory=snapshot_factory,
        )
        if transcript_id == "transcript-1":
            native_recorded.set()
        return sequence

    monkeypatch.setattr(runner.ControlTraceWriter, "append_native_record", signal_native_record)
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner, "run_scenario", forbidden("legacy"))
    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("semantic-replay"))
    monkeypatch.setattr(runner, "plan_semantic_events", forbidden("semantic-plan"))
    if gate_result not in {"real", "eager"}:
        monkeypatch.setattr(runner, "extract_event_markers", forbidden("markers"))
    bound_calls: list[bool] = []
    original_bound = runner._registered_phase_evidence_is_bound

    def record_bound(**kwargs: object) -> bool:
        if gate_result == "post_gate_unbound" and len(bound_calls) == 2:
            bound_calls.append(False)
            return False
        result = original_bound(**kwargs)  # type: ignore[arg-type]
        bound_calls.append(result)
        return result

    monkeypatch.setattr(runner, "_registered_phase_evidence_is_bound", record_bound)
    gate_calls: list[dict[str, object]] = []
    gate_tokens: list[runner.PhaseOneReceiptGate] = []
    # Build this at runtime: the workspace snapshot deliberately includes this
    # test source, so a literal would be a false-positive artifact match.
    gate_sentinel = "phase-one-gate-only-" + "sentinel-9f2b8e"
    gate_result_id = "sync_" + ("13579bdf" * 8)
    gate_corpus_revision = "0123456789abcdef" * 4
    gate_effect_digest = "fedcba9876543210" * 4
    gate_selection_ids = tuple(
        "phase-one-gate-selection-" + suffix
        for suffix in ("verify", "consume", "durable", "acknowledge", "stream", "receipt")
    )
    gate_sentinels = (
        gate_sentinel, gate_result_id, gate_corpus_revision, gate_effect_digest, *gate_selection_ids,
    )
    original_gate = runner._reconstruct_actual_phase_one_receipt_gate
    def gate(**kwargs: object) -> object | None:
        gate_calls.append(dict(kwargs))
        assert len(bound_calls) >= 2
        assert kwargs["writer"] is not None
        assert kwargs["phase_one"].executable_id == "client-executable-1"  # type: ignore[union-attr]
        assert kwargs["conversion"].trace_id == "trace-execution-1"  # type: ignore[union-attr]
        assert kwargs["scenario"]["id"] == "web-approval-and-capture"  # type: ignore[index]
        assert kwargs["workspace"] == kwargs["phase_one"].workspace_pin.path  # type: ignore[union-attr]
        assert kwargs["execution_id"] == "execution-1"
        writer = kwargs["writer"]
        assert not (writer.run_root / "evidence-index.json").exists()  # type: ignore[union-attr]
        assert not (writer.run_root / "event-log.json").exists()  # type: ignore[union-attr]
        if gate_result == "error":
            raise runner.RunnerError("test receipt gate failure")
        if gate_result == "none":
            return None
        if gate_result == "malformed":
            return object()
        if gate_result in {"real", "eager"}:
            # This must traverse the real sealed reader, not just prove that
            # the dispatcher can receive a shaped token.
            token = original_gate(**kwargs)  # type: ignore[arg-type]
            if gate_result == "eager":
                assert token is None
                return None
            assert type(token) is runner.PhaseOneReceiptGate
            gate_tokens.append(token)
            return token
        from tests.evals import event_log_contract as contract

        selection = contract.InitialSyncReceiptSelection(
            verify_command_id=gate_selection_ids[0],
            consume_command_id=gate_selection_ids[1],
            durable_command_id=gate_selection_ids[2],
            acknowledge_command_id=gate_selection_ids[3],
            stream_artifact_id=gate_selection_ids[4],
            durable_artifact_id=gate_selection_ids[5],
        )
        facts = contract.InitialSyncReceiptFacts(
            result_id=gate_result_id,
            corpus_revision=gate_corpus_revision,
            event_counts={gate_sentinel: 1},
            effect_digest=gate_effect_digest,
            selection=selection,
        )
        token = runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=facts,
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )
        gate_tokens.append(token)
        return token

    monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", gate)
    original_trusted_context = runner._registered_live_trusted_context
    trusted_context_calls: list[dict[str, object]] = []

    def no_aggregate_trusted_context(**kwargs: object) -> object:
        trusted_context_calls.append(dict(kwargs))
        trusted_executables = kwargs["trusted_executables"]
        if (not isinstance(trusted_executables, dict)
                or set(trusted_executables) not in (set(), {"execution-1"})
                or "execution-2" in trusted_executables):
            return forbidden("aggregate-trusted-context")()  # type: ignore[return-value]
        return original_trusted_context(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_registered_live_trusted_context", no_aggregate_trusted_context)
    original_actual_launch = runner._launch_actual_registered_live_phase

    def no_phase_two_actual_launch(*args: object, **kwargs: object) -> object:
        prepared_phase = args[0] if args else kwargs.get("prepared_phase")
        if getattr(prepared_phase, "execution_id", None) != "execution-1":
            return forbidden("phase-two-launch")(*args, **kwargs)
        return original_actual_launch(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_launch_actual_registered_live_phase", no_phase_two_actual_launch)
    original_live_launch = runner._launch_registered_live_phase

    def no_extra_live_launch(*args: object, **kwargs: object) -> object:
        if (kwargs.get("execution_id") != "execution-1"
                or kwargs.get("phase") != "approval"
                or kwargs.get("phase_index") != 1):
            return forbidden("phase-two-low-level-launch")(*args, **kwargs)
        return original_live_launch(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_launch_registered_live_phase", no_extra_live_launch)
    original_prepare_executable = runner._prepare_registered_live_executable

    def no_phase_two_executable(*args: object, **kwargs: object) -> object:
        execution_id = kwargs.get("execution_id")
        if execution_id != "execution-1":
            return forbidden("phase-two")(*args, **kwargs)
        return original_prepare_executable(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_prepare_registered_live_executable", no_phase_two_executable)
    original_phase_control = runner._phase_control

    def no_phase_two_control(*args: object, **kwargs: object) -> object:
        if kwargs.get("phase") != "approval":
            return forbidden("phase-two-control")(*args, **kwargs)
        return original_phase_control(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_phase_control", no_phase_two_control)
    original_write_state = runner._write_runner_state

    def no_phase_two_state(*args: object, **kwargs: object) -> object:
        if (kwargs.get("execution_id") != "execution-1"
                or kwargs.get("phase") != "approval"):
            return forbidden("phase-two-state")(*args, **kwargs)
        return original_write_state(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_write_runner_state", no_phase_two_state)
    original_prompt = runner._index_phase_prompt

    def no_phase_two_prompt(*args: object, **kwargs: object) -> object:
        if kwargs.get("phase_index") != 1 or kwargs.get("phase") != "approval":
            return forbidden("phase-two-prompt")(*args, **kwargs)
        return original_prompt(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_index_phase_prompt", no_phase_two_prompt)
    original_trace_init = runner.ControlTraceWriter.__init__

    def no_phase_two_trace(
        self: runner.ControlTraceWriter,
        control_root: Path,
        execution_id: str,
        *,
        control_pin: runner.PinnedDirectory | None = None,
    ) -> None:
        if execution_id != "execution-1":
            forbidden("phase-two-trace")()
        original_trace_init(self, control_root, execution_id, control_pin=control_pin)

    monkeypatch.setattr(runner.ControlTraceWriter, "__init__", no_phase_two_trace)
    original_add_bytes = runner.EvidenceWriter.add_bytes

    def no_phase_two_mcp(
        self: runner.EvidenceWriter,
        artifact_id: str,
        artifact_type: str,
        raw: bytes,
        **kwargs: object,
    ) -> str:
        if ((artifact_type == "mcp_config" and artifact_id != "mcp-1")
                or artifact_type in {"fixture_capability", "approval"}):
            return forbidden("phase-two-mcp-or-capability")()  # type: ignore[return-value]
        return original_add_bytes(self, artifact_id, artifact_type, raw, **kwargs)

    monkeypatch.setattr(runner.EvidenceWriter, "add_bytes", no_phase_two_mcp)
    original_add_json = runner.EvidenceWriter.add_json

    def no_capability_or_approval(
        self: runner.EvidenceWriter, artifact_id: str, artifact_type: str, value: object,
    ) -> str:
        if artifact_type in {"fixture_capability", "approval"}:
            return forbidden("capability-or-approval")()  # type: ignore[return-value]
        return original_add_json(self, artifact_id, artifact_type, value)

    monkeypatch.setattr(runner.EvidenceWriter, "add_json", no_capability_or_approval)
    monkeypatch.setattr(runner, "_indexed_run_manifest", forbidden("manifest"))
    monkeypatch.setattr(runner, "_indexed_execution_binding", forbidden("binding"))
    monkeypatch.setattr(runner, "_project_repository_assertions", forbidden("assertions"))
    monkeypatch.setattr(runner, "_atomic_validate_and_publish_candidate", forbidden("publisher"))
    original_context = runner._registered_live_context_phase

    def no_phase_two_context(*args: object, **kwargs: object) -> object:
        if kwargs.get("execution_id") != "execution-1":
            return forbidden("phase-two-context")(*args, **kwargs)
        return original_context(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_registered_live_context_phase", no_phase_two_context)

    if gate_result in {"error", "post_gate_unbound", "malformed"}:
        expected_error = (
            "test receipt gate failure" if gate_result == "error"
            else ("phase-one evidence is no longer bound" if gate_result == "post_gate_unbound"
                  else "receipt gate is invalid")
        )
        with pytest.raises(runner.RunnerError, match=expected_error):
            runner.run_registered_actual_scenario(
                client="claude", scenario_id="web-approval-and-capture",
                scratch_root=tmp_path / "bridge",
            )
        assert len(gate_calls) == 1
        assert calls == []
        assert not list((tmp_path / "bridge").rglob("event-log.json"))
        return

    outcome = runner.run_registered_actual_scenario(
        client="claude", scenario_id="web-approval-and-capture", scratch_root=tmp_path / "bridge",
    )

    assert type(outcome) is runner.RunOutcome
    assert outcome.trusted_context is None
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["incomplete_reasons"] == [expected_reason]
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert outcome.log["repository_assertions"] == []
    validate_against_schema(
        outcome.log,
        json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text()),
    )
    assert outcome.log["executions"] == [{
        "id": "execution-1", "phase": "approval", "kind": "actual_client_process",
        "policy_id": "policy-1", "process_id": "process-1", "transcript_id": "transcript-1",
    }]
    assert outcome.log_path.name == "event-log.json"
    assert outcome.log_path.read_bytes() == runner._canonical_json(outcome.log)
    assert probes == [
        [str(executable), "--version"],
        [str(executable), "--help"],
    ]
    assert len(client_spawns) == 1
    client_argv, client_kwargs = client_spawns[0]
    assert client_argv[0] == str(executable)
    assert client_kwargs["cwd"] == outcome.workspace
    assert client_kwargs["env"]["PATH"] == "/usr/bin:/bin"  # type: ignore[index]
    assert non_client_spawns == [expected_git_init]
    assert [command[1:4] for command in local_brain_commands] == [
        ["--json", "sync"],
        ["--json", "source", "consume-sync-result"],
        ["--json", "source", "consume-sync-result"],
        ["--json", "source", "acknowledge-sync-result"],
    ]
    # Pre-launch has no executable authority; the only nonempty trusted map is
    # the indexed phase-one executable.  Neither may aggregate a future phase.
    assert {
        frozenset(call["trusted_executables"])
        for call in trusted_context_calls
    } == {frozenset(), frozenset({"execution-1"})}

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = index["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    policy = json.loads((outcome.log_path.parent / by_id["policy-1"]["relative_path"]).read_text())
    process = json.loads((outcome.log_path.parent / by_id["process-1"]["relative_path"]).read_text())
    assert client_argv == policy["argv"] == process["argv"]
    assert policy["fixture_capability_id"] is None
    assert policy["approval"] is None
    assert process["fixture_capability_id"] is None
    assert process["approval"] is None
    allowed_types = (
        "phase_prompt", "mcp_config", "workspace_snapshot", "file_capture",
        "version", "help", "client_executable", "policy", "transcript",
        "process", "execution_trace", "command_observation", "command_result",
        "source_state", "source_record", "sync_stream", "pending_sync_result",
        "consumption_receipt",
    )
    assert len(allowed_types) == len(set(allowed_types))
    assert {entry["type"] for entry in entries} <= set(allowed_types)
    forbidden_types = {
        "marker", "event_record", "interpretation_decision", "approval",
        "fixture_capability", "run_manifest", "assertion",
    }
    assert not ({entry["type"] for entry in entries} & forbidden_types)
    assert all(
        not entry["id"].startswith((
            "marker-", "event-", "decision-", "approval-", "fixture-capability",
            "run-manifest", "assertion-",
        ))
        for entry in entries
    )
    assert not (outcome.control_root / "runner-trace-execution-2.jsonl").exists()
    for name in (
        "policy-2.json", "process-2.json", "version-2.bin", "help-2.bin",
        "transcript-2.bin", "workspace-snapshot-execution-2-initial-0.json",
    ):
        assert not list(outcome.log_path.parent.rglob(name))
        assert not list((tmp_path / "bridge").rglob(name))
    assert len(gate_calls) == 1
    assert len(gate_tokens) == (1 if gate_result in {"success", "real"} else 0)
    assert all(token is not outcome.log for token in gate_tokens)
    assert not any(
        entry["id"].startswith("phase-one-receipt-gate")
        or entry["type"] == "phase_one_receipt_gate"
        for entry in entries
    )
    assert all(
        all(sentinel.encode("utf-8") not in path.read_bytes() for sentinel in gate_sentinels)
        for path in outcome.log_path.parent.rglob("*") if path.is_file()
    )
    assert calls == []


def test_registered_actual_phase_one_bridge_index_failure_never_finalizes_or_enters_later_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a failed launch/index could emit a public-looking log."""

    _writer, _control, _workspace, executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    control_pin.close()
    workspace_pin.close()
    original_popen = runner.subprocess.Popen
    calls: list[str] = []
    setup_spawns: list[list[str]] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("failed bridge reached " + name)
        return reject

    def probe(
        *, argv: list[str], cwd: Path, environment: dict[str, str], probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, environment, probe_runner
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"type":"result"}\n')
            self.stderr = _task7d_closed_pipe(b"diagnostic\n")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            assert self.stdout.closed and self.stderr.closed
            return 0

    def popen(argv: list[str], **kwargs: object) -> object:
        if argv[0] == str(executable):
            return CompleteProcess()
        if argv[0] == str(runner._runner_git_binary()):
            setup_spawns.append(list(argv))
            return original_popen(argv, **kwargs)
        pytest.fail("failed bridge attempted unallowlisted subprocess: " + repr(argv))

    monkeypatch.setattr(runner, "_registered_probe", probe)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    index_calls: list[str] = []

    def reject_index(*_args: object, **_kwargs: object) -> object:
        index_calls.append("index")
        raise runner.RunnerError("test diagnostic indexing failure")

    monkeypatch.setattr(runner, "_index_registered_actual_phase_one_diagnostic", reject_index)
    monkeypatch.setattr(runner, "_finalize_log", forbidden("finalize"))
    monkeypatch.setattr(runner, "run_scenario", forbidden("legacy"))
    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("semantic-replay"))
    monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", forbidden("receipt-gate"))
    monkeypatch.setattr(runner, "_atomic_validate_and_publish_candidate", forbidden("publisher"))

    with pytest.raises(runner.RunnerError, match="test diagnostic indexing failure"):
        runner.run_registered_actual_scenario(
            client="claude", scenario_id="web-approval-and-capture",
            scratch_root=tmp_path / "failed-bridge",
        )
    assert index_calls == ["index"]
    assert calls == []
    assert setup_spawns
    assert all(argv[0] == str(runner._runner_git_binary()) for argv in setup_spawns)
    assert not list((tmp_path / "failed-bridge").rglob("event-log.json"))


def test_registered_actual_phase_one_bridge_finalizer_rejects_semantic_fields_before_generic_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: generic incomplete finalization could launder semantic rows."""

    writer = runner.EvidenceWriter(tmp_path, "b" * 64)
    scenario = runner._scenario("web-approval-and-capture")
    log = runner._incomplete_log(
        run_id=writer.run_id,
        scenario=scenario,
        client="claude",
        version="2.1.251",
        fixture_sha256="c" * 64,
        executions=[{
            "id": "execution-1", "phase": "approval", "kind": "actual_client_process",
            "policy_id": "policy-1", "process_id": "process-1", "transcript_id": "transcript-1",
        }],
        reasons=["public_semantic_projection_unavailable"],
    )
    finalized: list[object] = []
    monkeypatch.setattr(runner, "_finalize_log", lambda *_args: finalized.append("finalized"))

    unverified = dict(log)
    unverified["incomplete_reasons"] = ["web_approval_phase_unverified"]
    runner._finalize_registered_actual_phase_one_diagnostic(writer, unverified)
    assert finalized == ["finalized"]
    finalized.clear()

    for field, forbidden_value in (
        ("events", [{"name": "forbidden"}]),
        ("receipts", [{"id": "forbidden"}]),
        ("deliveries", [{"id": "forbidden"}]),
        ("repository_assertions", [{"assertion_id": "forbidden"}]),
    ):
        rejected = dict(log)
        rejected[field] = forbidden_value
        with pytest.raises(runner.RunnerError, match="diagnostic finalization boundary"):
            runner._finalize_registered_actual_phase_one_diagnostic(writer, rejected)
    for reasons in (
        ["public_semantic_projection_unavailable", "web_approval_phase_unverified"],
        ["arbitrary_incomplete_reason"],
        [],
    ):
        rejected = dict(log)
        rejected["incomplete_reasons"] = reasons
        with pytest.raises(runner.RunnerError, match="diagnostic finalization boundary"):
            runner._finalize_registered_actual_phase_one_diagnostic(writer, rejected)
    rejected = dict(log)
    rejected["executions"] = [*log["executions"], dict(log["executions"][0])]
    with pytest.raises(runner.RunnerError, match="diagnostic finalization boundary"):
        runner._finalize_registered_actual_phase_one_diagnostic(writer, rejected)
    assert finalized == []


def _task7d_actual_prepared_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario_id: str = "current-wiki-fast-path",
) -> object:
    """Allocate the separately tested disconnected run before phase sentinels."""

    registration = _task7d_closed_registered_claude()
    monkeypatch.setattr(
        runner, "_RUNNER_OWNED_EXECUTABLES", MappingProxyType({"claude": registration}),
    )
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id=scenario_id, scratch_root=tmp_path / "scratch",
    )
    return runner._prepare_registered_actual_run(runner._RegisteredActualAuthority(plan))


def test_actual_phase_one_setup_seals_only_first_phase_without_live_or_semantic_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: phase-one setup could select a client or create public output."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("phase-one sealed setup reached " + name)
        return reject

    def no_popen(*_args: object, **_kwargs: object) -> object:
        calls.append("popen")
        pytest.fail("phase-one sealed setup reached Popen")

    monkeypatch.setattr(runner, "prepare_isolated_workspace", forbidden("workspace"))
    monkeypatch.setattr(
        runner, "_prepare_registered_live_executable", forbidden("executable-preparation"),
    )
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("live-launch"))
    monkeypatch.setattr(
        runner, "_launch_actual_registered_live_phase", forbidden("actual-launch"),
    )
    monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("private-replay"))
    monkeypatch.setattr(runner, "_finalize_log", forbidden("finalize"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("publish"),
    )
    monkeypatch.setattr(runner, "run_scenario", forbidden("legacy-run"))

    signature = inspect.signature(runner._prepare_registered_actual_phase_one)
    assert tuple(signature.parameters) == ("prepared_run",)
    assert not {
        "phase", "phase_index", "execution_id", "argv", "environment", "prompt",
        "process_runner", "probe_runner", "popen_factory",
    } & set(signature.parameters)

    try:
        phase = runner._prepare_registered_actual_phase_one(prepared_run)
        assert type(phase) is runner._RegisteredActualPreparedPhase
        assert phase.prepared_run is prepared_run
        assert phase.authority is prepared_run.authority
        assert phase.writer is prepared_run.writer
        assert phase.execution_id == "execution-1"
        assert phase.phase_index == 1
        assert phase.phase == "main"
        assert phase.transcript_id == "transcript-1"
        assert phase.workspace == prepared_run.prepared.workspace
        assert phase.control_pin is prepared_run.prepared.control_pin
        assert phase.workspace_pin is prepared_run.prepared.workspace_pin
        assert phase.control_pin.private is True
        assert phase.workspace_pin.private is False
        runner.verify_wrapper(prepared_run.prepared.wrapper_seal)
        runner.verify_phase_control(phase.phase_control)
        assert phase.phase_control.config.path == prepared_run.writer.control_root / "brain-shim.json"
        phase_control_raw = runner._safe_read(phase.phase_control.config.path)
        assert json.loads(phase_control_raw) == {
            "schema_version": 1,
            "workspace": str(phase.workspace),
            "scenario_id": "current-wiki-fast-path",
            "phase": "main",
            "approval": None,
            "fixture": None,
        }
        assert phase_control_raw == runner._canonical_json(json.loads(phase_control_raw))

        assert phase.runner_state.path == prepared_run.writer.control_root / "runner-state.json"
        runner.verify_control_file(phase.runner_state)
        runner_state_raw = runner._safe_read(phase.runner_state.path)
        assert json.loads(runner_state_raw) == {
            "schema_version": 1,
            "control_root": str(prepared_run.writer.control_root),
            "workspace": str(phase.workspace),
            "run_id": prepared_run.run_id,
            "execution_id": "execution-1",
            "phase": "main",
        }
        assert runner_state_raw == runner._canonical_json(json.loads(runner_state_raw))

        entries = {entry["id"]: entry for entry in prepared_run.writer.entries}
        assert entries["mcp-1"]["type"] == "mcp_config"
        assert entries["mcp-1"]["sha256"] == hashlib.sha256(runner.EMPTY_MCP_BYTES).hexdigest()
        assert phase.mcp_config is not None
        assert phase.mcp_config.path == (
            prepared_run.writer.run_root / str(entries["mcp-1"]["relative_path"])
        ).resolve()
        assert runner._safe_read(phase.mcp_config.path) == runner.EMPTY_MCP_BYTES
        runner.verify_control_file(phase.mcp_config)

        assert phase.phase_prompt.artifact_id == "phase-prompt-1"
        assert phase.phase_prompt.raw == runner._canonical_phase_prompt(
            prepared_run.prepared.scenario, "main",
        )
        assert phase.phase_prompt.transport == "argv_final_utf8"
        runner._verify_phase_prompt(phase.phase_prompt)
        assert phase.trace.control_root == prepared_run.writer.control_root
        assert phase.trace.path == prepared_run.writer.control_root / "runner-trace-execution-1.jsonl"
        assert phase.trace.lock_path == prepared_run.writer.control_root / ".runner-trace.lock"
        assert phase.trace.records() == []
        assert phase.marker_workspace_snapshots == []

        initial = runner._indexed_workspace_snapshot(
            prepared_run.writer, phase.initial_workspace_snapshot,
        )
        assert initial.execution_id == "execution-1"
        assert initial.role == "initial"
        assert initial.trace_sequence == 0
        assert initial.artifact_id == "workspace-snapshot-execution-1-initial-0"
        assert initial.staged_paths == ()
        assert "workspace-snapshot-execution-1-initial-0" in entries
        assert entries["workspace-snapshot-execution-1-initial-0"]["type"] == "workspace_snapshot"
        assert calls == []
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_setup_callback_binds_marker_snapshot_to_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a later native row could lack a trace-owned marker snapshot."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    # The callback's future marker capture normally reads the private Git
    # index.  Keep this focused pure binding test process-free.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    try:
        phase = runner._prepare_registered_actual_phase_one(prepared_run)
        raw = b'{"type":"assistant","text":"EVENT: ask_web_approval"}\n'
        sequence = phase.trace.append_native_record(
            transcript_id=phase.transcript_id,
            byte_start=0,
            byte_end=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            snapshot_factory=lambda trace_sequence: phase.on_native_record(
                raw, 0, len(raw), trace_sequence,
            ),
        )
        assert sequence == 1
        assert len(phase.marker_workspace_snapshots) == 1
        marker = phase.marker_workspace_snapshots[0]
        loaded = runner._indexed_workspace_snapshot(prepared_run.writer, marker)
        assert loaded.execution_id == "execution-1"
        assert loaded.role == "marker"
        assert loaded.trace_sequence == 1
        assert loaded.native_record == {
            "transcript_id": "transcript-1",
            "byte_start": 0,
            "byte_end": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        assert phase.trace.records() == [{
            "sequence": 1,
            "kind": "native_record",
            "transcript_id": "transcript-1",
            "byte_start": 0,
            "byte_end": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "workspace_snapshot_id": marker.artifact_id,
        }]
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_setup_rejects_test_hook_and_detached_run_before_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a mutable/detached run could create first-phase control files."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    writer = prepared_run.writer
    before_entries = list(writer.entries)
    expected_absent = [
        prepared_run.prepared.control_root / "brain-shim.json",
        prepared_run.prepared.control_root / "runner-state.json",
    ]
    assert all(not path.exists() for path in expected_absent)

    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", object())
    with pytest.raises(runner.RunnerError, match="test-only"):
        runner._prepare_registered_actual_phase_one(prepared_run)
    assert writer.entries == before_entries
    assert all(not path.exists() for path in expected_absent)

    monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", None)
    with pytest.raises(runner.RunnerError, match="prepared run"):
        runner._prepare_registered_actual_phase_one(object())
    assert writer.entries == before_entries
    assert all(not path.exists() for path in expected_absent)

    detached_plan = replace(
        prepared_run.authority.plan, symbolic_template=("not", "runner-owned"),
    )
    detached_authority = runner._RegisteredActualAuthority(detached_plan)
    detached_run = replace(prepared_run, authority=detached_authority)
    with pytest.raises(runner.RunnerError, match="runner-owned"):
        runner._prepare_registered_actual_phase_one(detached_run)
    assert writer.entries == before_entries
    assert all(not path.exists() for path in expected_absent)
    prepared_run.prepared.control_pin.close()
    prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_setup_rejects_git_index_before_phase_artifacts_or_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an index could trigger setup writes or an implicit Git spawn."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    writer = prepared_run.writer
    workspace = prepared_run.prepared.workspace
    index = workspace / ".git" / "index"
    assert not index.exists()
    index_bytes = b"untrusted staged index\\n"
    index.write_bytes(index_bytes)
    calls: list[str] = []

    def no_popen(*_args: object, **_kwargs: object) -> object:
        calls.append("popen")
        pytest.fail("indexed phase-one setup spawned Git or a client")

    monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
    expected_absent = [
        prepared_run.prepared.control_root / "brain-shim.json",
        prepared_run.prepared.control_root / "runner-state.json",
        prepared_run.prepared.control_root / "runner-trace-execution-1.jsonl",
    ]
    assert writer.entries == []
    assert all(not path.exists() for path in expected_absent)
    try:
        with pytest.raises(runner.RunnerError, match="staged Git index"):
            runner._prepare_registered_actual_phase_one(prepared_run)
        assert calls == []
        assert writer.entries == []
        assert all(not path.exists() for path in expected_absent)
        assert index.read_bytes() == index_bytes
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_setup_keeps_web_phase_two_impossible_and_wrapper_rechecks_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: setup could expose phase two or launch after control mutation."""

    prepared_run = _task7d_actual_prepared_run(
        tmp_path, monkeypatch, scenario_id="web-approval-and-capture",
    )
    low_level_calls: list[object] = []

    def no_low_level(*_args: object, **_kwargs: object) -> object:
        low_level_calls.append("low-level")
        pytest.fail("unlaunched phase-one setup reached low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", no_low_level)
    # Keep this setup-only test from invoking the normal Git snapshot command.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    try:
        phase = runner._prepare_registered_actual_phase_one(prepared_run)
        assert phase.phase == "approval"
        assert phase.execution_id == "execution-1"
        assert not any(entry["id"].endswith("-2") for entry in prepared_run.writer.entries)
        assert not (prepared_run.writer.control_root / "runner-trace-execution-2.jsonl").exists()

        phase.phase_control.config.path.write_bytes(b"{}\n")
        with pytest.raises(runner.RunnerError, match="control"):
            runner._launch_actual_registered_live_phase(phase)
        assert low_level_calls == []

        # A phase-two facade cannot even be constructed from phase-one setup:
        # it lacks the predecessor receipt and workspace-baseline proofs that
        # only the approved phase-one diagnostic may produce.
        with pytest.raises(runner.RunnerError, match="phase-two raw predecessor snapshot"):
            replace(
                phase, execution_id="execution-2", phase="approved_capture", phase_index=2,
                transcript_id="transcript-2",
            )
        assert low_level_calls == []
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_wrapper_rechecks_control_after_low_level_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a control replacement during launch could evade the final seal check."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    reached: list[str] = []

    def mutate_control_then_fail(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        prepared_run.prepared.control_root.joinpath("brain-shim.json").write_bytes(b"{}\n")
        raise runner.RunnerError("test low-level boundary")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", mutate_control_then_fail)
    try:
        phase = runner._prepare_registered_actual_phase_one(prepared_run)
        with pytest.raises(runner.RunnerError, match="control"):
            runner._launch_actual_registered_live_phase(phase)
        assert reached == ["low-level"]
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def test_actual_phase_one_wrapper_rejects_supplied_trusted_context_before_low_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a retained context could bypass fresh phase-one trust setup."""

    prepared_run = _task7d_actual_prepared_run(tmp_path, monkeypatch)
    reached: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            reached.append(name)
            pytest.fail("supplied trusted context reached " + name)
        return reject

    monkeypatch.setattr(
        runner, "_prepare_registered_live_executable", forbidden("executable-preparation"),
    )
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("low-level"))
    try:
        phase = runner._prepare_registered_actual_phase_one(prepared_run)
        stale_context = object()
        with pytest.raises(runner.RunnerError, match="trusted context"):
            runner._launch_actual_registered_live_phase(
                replace(phase, trusted_context=stale_context),
            )
        assert reached == []
    finally:
        prepared_run.prepared.control_pin.close()
        prepared_run.prepared.workspace_pin.close()


def _task7d_actual_production_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_name: str = "runner-state.json",
    state_overrides: dict[str, object] | None = None,
) -> tuple[object, Path, runner.EvidenceWriter, Path, Path]:
    """Build first-phase sealed facts for the disconnected actual wrapper."""

    _writer, _control_root, _workspace, executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    control_pin.close()
    workspace_pin.close()
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        scratch_root=tmp_path / "actual-wrapper-scratch",
    )
    prepared_run = runner._prepare_registered_actual_run(
        runner._RegisteredActualAuthority(plan),
    )
    prepared_phase = runner._prepare_registered_actual_phase_one(prepared_run)
    writer = prepared_phase.writer
    assert prepared_phase.mcp_config is not None
    mcp_path = prepared_phase.mcp_config.path
    state_path = prepared_phase.runner_state.path
    if state_name != "runner-state.json" or state_overrides is not None:
        state_path = writer.control_root / state_name
        state_value: dict[str, object] = {
            "schema_version": 1,
            "control_root": str(writer.control_root),
            "workspace": str(prepared_phase.workspace),
            "run_id": writer.run_id,
            "execution_id": "execution-1",
            "phase": "main",
        }
        if state_overrides is not None:
            state_value.update(state_overrides)
        if state_path.exists():
            state_path.write_bytes(runner._canonical_json(state_value))
        else:
            runner._write_private(state_path, runner._canonical_json(state_value))
        prepared_phase = replace(
            prepared_phase, runner_state=runner._seal_control_file(state_path),
        )
    return prepared_phase, executable, writer, mcp_path, state_path


def _task7d_actual_indexable_phase_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, runner.RegisteredLivePhase]:
    """Build one genuinely captured, private actual-phase fixture before sentinels.

    The local ``brain`` fixture commands create the real shim capture files;
    the client process itself is only the existing low-level pipe double.  The
    diagnostic indexer under test receives the returned sealed facts after all
    test-only process seams have been replaced by fail-fast sentinels.
    """

    # Keep the descriptor-pinned temporary executable registration installed
    # by the existing live-wrapper fixture; the simpler prepared-run helper
    # intentionally uses a nonexistent closed placeholder because it never
    # launches.
    _writer, _control, _workspace, _executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    control_pin.close()
    workspace_pin.close()
    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        scratch_root=tmp_path / "diagnostic-indexer-scratch",
    )
    prepared_run = runner._prepare_registered_actual_run(
        runner._RegisteredActualAuthority(plan),
    )
    prepared_phase = runner._prepare_registered_actual_phase_one(prepared_run)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(prepared_phase.writer.control_root / "home"),
        "LANG": "C.UTF-8",
        runner.CONTROL_ENV: str(prepared_phase.writer.control_root),
        runner.RUNNER_STATE_ENV: str(prepared_phase.runner_state.path),
    }
    # Run the local fixture bridge before installing the client-Popen double.
    # The converter must consume these host-recorded raw files rather than an
    # injected conversion result.
    transcript = b"".join(_initial_sync_receipt_lifecycle_stream(
        prepared_phase.workspace, environment,
    ))

    def probe(
        *,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, environment, probe_runner
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(transcript)
            self.stderr = _task7d_closed_pipe(b"diagnostic\n")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            assert self.stdout.closed and self.stderr.closed
            return 0

    def popen(_argv: list[str], **_kwargs: object) -> CompleteProcess:
        return CompleteProcess()

    monkeypatch.setattr(runner, "_registered_probe", probe)
    # Native record callbacks normally read a Git staged-path inventory.  That
    # is unrelated to this already-captured phase fixture and must not turn the
    # client-Popen double into a Git process double.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    live_phase = runner._launch_actual_registered_live_phase(prepared_phase)
    return prepared_phase, live_phase


def _task7d_actual_replayable_phase_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[runner._RegisteredActualPreparedPhase, runner.RegisteredLivePhase]:
    """Launch phase one with native rows and shim commands truly interleaved.

    The ordinary indexable fixture eagerly exhausts its test generator before
    the client pipe is read, which is sufficient for phase-one indexing but
    not for a receipt replay whose native-marker positions must follow the
    matching command intervals.  This narrow sibling releases each native
    row only after the parent appended it to the shared trace; the next
    generator advance can then execute its next local shim command.
    """

    _writer, _control, _workspace, _executable, _trace, control_pin, workspace_pin = (
        _task7d_registered_phase_inputs(tmp_path, monkeypatch)
    )
    control_pin.close()
    workspace_pin.close()
    plan = runner.prepare_registered_actual_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        scratch_root=tmp_path / "replayable-phase-one-scratch",
    )
    prepared_run = runner._prepare_registered_actual_run(
        runner._RegisteredActualAuthority(plan),
    )
    prepared_phase = runner._prepare_registered_actual_phase_one(prepared_run)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(prepared_phase.writer.control_root / "home"),
        "LANG": "C.UTF-8",
        runner.CONTROL_ENV: str(prepared_phase.writer.control_root),
        runner.RUNNER_STATE_ENV: str(prepared_phase.runner_state.path),
    }
    native_row_appended = threading.Event()
    original_append_native_record = runner.ControlTraceWriter.append_native_record

    def note_phase_one_native_append(
        trace: runner.ControlTraceWriter,
        *args: object,
        **kwargs: object,
    ) -> object:
        sequence = original_append_native_record(trace, *args, **kwargs)
        if trace is prepared_phase.trace:
            native_row_appended.set()
        return sequence

    class InterleavedPhaseOneProcess:
        def __init__(self) -> None:
            stdout_read, self._stdout_write = os.pipe()
            stderr_read, self._stderr_write = os.pipe()
            self.stdin = _Task7dRecordingInput()
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
            self._stopped = threading.Event()
            self.errors: list[BaseException] = []
            self._producer = threading.Thread(target=self._produce, daemon=True)
            self._producer.start()

        def _write_all(self, raw: bytes) -> None:
            offset = 0
            while offset < len(raw):
                offset += os.write(self._stdout_write, raw[offset:])

        def _produce(self) -> None:
            try:
                stream = _initial_sync_receipt_lifecycle_stream(
                    prepared_phase.workspace,
                    environment,
                )
                for raw in stream:
                    if type(raw) is not bytes or not raw.endswith(b"\n"):
                        raise AssertionError("phase-one receipt fixture yielded invalid native row")
                    native_row_appended.clear()
                    self._write_all(raw)
                    if not native_row_appended.wait(timeout=5):
                        raise AssertionError("phase-one native row was not appended to trace")
                    if self._stopped.is_set():
                        return
            except BaseException as error:
                self.errors.append(error)
            finally:
                os.close(self._stdout_write)
                os.close(self._stderr_write)

        def poll(self) -> int | None:
            return None if self._producer.is_alive() else 0

        def terminate(self) -> None:
            self._stopped.set()
            native_row_appended.set()
            self._producer.join(timeout=5)

        def kill(self) -> None:
            self.terminate()

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            native_row_appended.set()
            self._producer.join(timeout=5)
            assert not self._producer.is_alive()
            if self.errors:
                raise self.errors[0]
            return 0

    def probe(
        *,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd == prepared_phase.workspace
        assert probe_runner is None
        assert environment[runner.CONTROL_ENV] == str(prepared_phase.writer.control_root)
        output = (
            b"2.1.251 (replayable phase-one fixture)\n"
            if argv[-1] == "--version"
            else b"stream-json help\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, b"")

    registered_client = str(plan.registration.resolved_path)

    def popen(argv: list[str], **kwargs: object) -> object:
        if argv[0] != registered_client:
            return _TASK7D_NATIVE_POPEN(argv, **kwargs)
        assert kwargs["cwd"] == prepared_phase.workspace
        launch_environment = kwargs["env"]
        assert isinstance(launch_environment, dict)
        assert launch_environment == environment
        return InterleavedPhaseOneProcess()

    try:
        # This sibling's client/trace barriers are needed only to construct
        # phase one.  Do not leak its append wrapper into a later phase-two
        # fixture, whose own receipt tracker has a distinct append boundary.
        with monkeypatch.context() as phase_one_patches:
            phase_one_patches.setattr(runner, "_registered_probe", probe)
            phase_one_patches.setattr(
                runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: (),
            )
            phase_one_patches.setattr(
                runner.ControlTraceWriter,
                "append_native_record",
                note_phase_one_native_append,
            )
            phase_one_patches.setattr(runner.subprocess, "Popen", popen)
            live_phase = runner._launch_actual_registered_live_phase(prepared_phase)
    except BaseException:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()
        raise
    return prepared_phase, live_phase


def test_public_phase_one_index_is_direct_and_authorizes_before_phase_two_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public continuation starts from a direct host graph, never a diagnostic stage."""

    prepared_phase, live_phase = _task7d_actual_replayable_phase_one(tmp_path, monkeypatch)
    try:
        phase_one = runner._index_registered_actual_public_phase_one(
            prepared_phase, live_phase,
        )
        writer = prepared_phase.writer
        entries = {entry["id"]: entry for entry in writer.entries}
        assert entries["mcp-1"]["relative_path"] == "artifacts/mcp-1.json"
        assert entries["policy-1"]["relative_path"] == "artifacts/policy-1.json"
        assert not any("phase-one-diagnostic" in entry["relative_path"]
                       for entry in writer.entries)
        assert {"web-approval", "fixture-capability"}.isdisjoint(entries)

        authorization = runner._authorize_registered_actual_public_web_phase_two(
            prepared_phase.prepared_run, phase_one,
        )
        phase_two = runner._prepare_registered_actual_public_phase_two(authorization)
        entries = {entry["id"]: entry for entry in writer.entries}
        assert {"web-approval", "fixture-capability", "phase-prompt-2", "mcp-2"} <= set(entries)
        assert "policy-2" not in entries and "process-2" not in entries
        assert phase_two.trusted_context.trusted_executables.keys() == {"execution-1", "execution-2"}
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_public_phase_two_launcher_uses_only_registered_local_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detached public launcher has no caller-provided process seam."""

    prepared_phase, first_live = _task7d_actual_replayable_phase_one(tmp_path, monkeypatch)
    try:
        phase_one = runner._index_registered_actual_public_phase_one(prepared_phase, first_live)
        authorization = runner._authorize_registered_actual_public_web_phase_two(
            prepared_phase.prepared_run, phase_one,
        )
        second_setup = runner._prepare_registered_actual_public_phase_two(authorization)
        transcript = _claude_transcript(
            [], cwd=str(prepared_phase.workspace.resolve()),
        )
        probes: list[list[str]] = []
        launches: list[list[str]] = []
        nested_results: list[subprocess.CompletedProcess[bytes]] = []

        class CompleteProcess:
            def __init__(self, environment: dict[str, str]) -> None:
                self.stdin = _Task7dRecordingInput()
                self.stdout = _task7d_closed_pipe(transcript)
                self.stderr = _task7d_closed_pipe(b"public phase-two fixture\n")
                self.environment = dict(environment)

            def wait(self, timeout: float | None = None) -> int:
                assert timeout is not None and timeout > 0
                assert self.stdout.closed and self.stderr.closed
                child_argv = [
                    str(prepared_phase.workspace / "brain"), "--json", "eval",
                    "mock-web-capture", "--fixture-id", "web-approval-and-capture.initial",
                    "--approval-event-id", str(authorization.approval["event_id"]),
                    "--approval-scope", str(authorization.approval["scope"]),
                    "--approval-note", str(authorization.approval["note"]),
                ]
                child = _TASK7D_NATIVE_POPEN(
                    child_argv, cwd=prepared_phase.workspace, env=self.environment,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                stdout, stderr = child.communicate()
                nested_results.append(subprocess.CompletedProcess(
                    child_argv, child.returncode, stdout, stderr,
                ))
                assert child.returncode == 0, stderr.decode("utf-8", "replace")
                return 0

        def probe(
            *, argv: list[str], cwd: Path, environment: dict[str, str], probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            assert cwd == prepared_phase.workspace
            assert probe_runner is None
            assert environment[runner.RUNNER_STATE_ENV] == str(second_setup.runner_state.path)
            probes.append(list(argv))
            output = b"2.1.251 (public fixture)\n" if argv[-1] == "--version" else b"stream-json help\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        def popen(argv: list[str], **kwargs: object) -> CompleteProcess:
            assert argv[0] == str(prepared_phase.authority.plan.registration.resolved_path)
            assert kwargs["cwd"] == prepared_phase.workspace
            launches.append(list(argv))
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            return CompleteProcess(environment)

        monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
        monkeypatch.setattr(runner, "_registered_probe", probe)
        monkeypatch.setattr(runner.subprocess, "Popen", popen)
        second_live = runner._launch_registered_actual_public_phase_two(second_setup)

        assert second_live.capture.exit_code == 0
        assert [argv[-1] for argv in probes] == ["--version", "--help"]
        assert len(launches) == 1
        assert launches[0][-1] == second_setup.phase_prompt.raw.decode("utf-8")
        assert nested_results and nested_results[0].returncode == 0
        indexed = runner._index_registered_actual_public_phase_two(second_setup, second_live)
        assert indexed.policy_id == "policy-2"
        assert indexed.process_id == "process-2"
        host = runner._registered_host_execution_eligibility(authorization, indexed)
        assert host.verify().client_version == second_live.reported_version

        # A parser-valid but semantically empty second phase cannot reach the
        # pass writer.  Projection must reject it before an assertion or
        # event-log artifact can exist.
        with pytest.raises(runner.RunnerError, match="semantic plan is incomplete"):
            runner._project_registered_actual_public_web_candidate(host)
        assert not (prepared_phase.writer.run_root / "event-log.json").exists()
        assert not any(entry["type"] == "assertion"
                       for entry in prepared_phase.writer.entries)

        # The host receipt is fixed before semantic candidate rows exist.  A
        # later change to a retained process row must therefore be detected,
        # rather than silently becoming the baseline for public projection.
        process_entry = next(entry for entry in prepared_phase.writer.entries
                             if entry["id"] == "process-2")
        original_process_sha = process_entry["sha256"]
        process_entry["sha256"] = "0" * 64
        with pytest.raises(runner.RunnerError, match="artifact changed"):
            host.verify()
        process_entry["sha256"] = original_process_sha

        # A later third-phase host artifact is residue, not candidate semantic
        # evidence.  It must fail before the public projector can ignore it.
        prepared_phase.writer.add_json("policy-3", "policy", {
            "execution_id": "execution-3", "phase": "unexpected",
        })
        with pytest.raises(runner.RunnerError, match="extra execution"):
            host.verify()
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


@pytest.mark.parametrize("validator_rejects", (False, True), ids=("accept", "reject"))
def test_public_web_route_projects_one_complete_fake_candidate_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator_rejects: bool,
) -> None:
    """The closed route can publish only after one complete local replay."""

    plan = runner.prepare_registered_actual_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        scratch_root=tmp_path / "complete-public-route",
    )
    authority = runner._RegisteredActualAuthority(plan)
    native_row_appended = threading.Event()
    original_append = runner.ControlTraceWriter.append_native_record

    def note_native_append(
        trace: runner.ControlTraceWriter, *args: object, **kwargs: object,
    ) -> object:
        result = original_append(trace, *args, **kwargs)
        native_row_appended.set()
        return result

    class StreamingFixtureProcess:
        def __init__(self, capture: runner.ProcessCapture) -> None:
            assert capture.stdout_chunks is not None
            stdout_read, self._stdout_write = os.pipe()
            stderr_read, self._stderr_write = os.pipe()
            self.stdin = _Task7dRecordingInput()
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
            self._capture = capture
            self._stopped = threading.Event()
            self.errors: list[BaseException] = []
            self._producer = threading.Thread(target=self._produce, daemon=True)
            self._producer.start()

        def _produce(self) -> None:
            try:
                for raw in self._capture.stdout_chunks or ():
                    assert type(raw) is bytes and raw.endswith(b"\n")
                    native_row_appended.clear()
                    os.write(self._stdout_write, raw)
                    if not native_row_appended.wait(timeout=10):
                        raise AssertionError("public fixture native row was not indexed")
                    if self._stopped.is_set():
                        return
            except BaseException as error:
                self.errors.append(error)
            finally:
                os.close(self._stdout_write)
                os.close(self._stderr_write)

        def poll(self) -> int | None:
            return None if self._producer.is_alive() else 0

        def terminate(self) -> None:
            self._stopped.set()
            native_row_appended.set()
            self._producer.join(timeout=10)

        def kill(self) -> None:
            self.terminate()

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            native_row_appended.set()
            self._producer.join(timeout=10)
            assert not self._producer.is_alive()
            if self.errors:
                raise self.errors[0]
            return 0

    def probe(
        *, argv: list[str], cwd: Path, environment: dict[str, str], probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd.is_dir() and probe_runner is None
        output = b"2.1.251 (complete public fixture)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    process_factory = _faithful_claude_fixture_process(
        "web-approval-and-capture", _faithful_web_workflow,
    )

    def popen(argv: list[str], **kwargs: object) -> object:
        environment = kwargs.get("env")
        if argv[0] != str(plan.registration.resolved_path):
            return _TASK7D_NATIVE_POPEN(argv, **kwargs)
        assert isinstance(environment, dict)
        cwd = kwargs.get("cwd")
        assert isinstance(cwd, Path)
        capture = process_factory(argv, cwd, environment, b"")
        assert type(capture) is runner.ProcessCapture
        return StreamingFixtureProcess(capture)

    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner.ControlTraceWriter, "append_native_record", note_native_append)
    monkeypatch.setattr(runner, "_registered_probe", probe)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(
        runner, "_private_semantic_overlay",
        lambda *_args, **_kwargs: pytest.fail("public route reached private semantic overlay"),
    )
    if validator_rejects:
        def reject_validator(*_args: object, **_kwargs: object) -> None:
            raise ValueError("controlled public validator rejection")

        monkeypatch.setattr(runner, "_validate_prepublication_event_log", reject_validator)
    outcome = runner._run_registered_actual_public_web_route(authority)

    if validator_rejects:
        assert outcome.log["result"] == "incomplete"
        assert outcome.log["incomplete_reasons"] == ["prepublication_validation_failed"]
        logs = list((outcome.control_root / "runs").glob("*/event-log.json"))
        assert logs
        assert all(json.loads(path.read_text())["result"] != "pass" for path in logs)
    else:
        assert outcome.log["result"] == "pass"
        assert outcome.log["incomplete_reasons"] == []
        assert outcome.log_path.name == "event-log.json"
        assert outcome.log_path.is_file()
        index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
        types = {entry["type"] for entry in index["entries"]}
        assert {"marker", "event_record", "assertion", "run_manifest"} <= types


def _task7d_actual_phase_two_replayable_receipt_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    runner._RegisteredActualPreparedPhase,
    runner._RegisteredActualPhaseOneDiagnostic,
    runner.PhaseOneReceiptGate,
]:
    """Build phase-one facts whose receipt gate can be replayed from the trace."""

    phase_one, live_phase = _task7d_actual_replayable_phase_one(tmp_path, monkeypatch)
    diagnostic = runner._index_registered_actual_phase_one_diagnostic(phase_one, live_phase)
    receipt_gate = runner._reconstruct_actual_phase_one_receipt_gate(
        writer=phase_one.prepared_run.writer,
        phase_one=live_phase,
        scenario=phase_one.prepared_run.prepared.scenario,
        conversion=diagnostic.conversion,
        workspace=phase_one.workspace,
        execution_id="execution-1",
        control_pin=phase_one.control_pin,
    )
    assert type(receipt_gate) is runner.PhaseOneReceiptGate
    assert receipt_gate.selection.command_observation_ids == diagnostic.conversion.observation_ids
    return phase_one, diagnostic, receipt_gate


def test_actual_phase_one_diagnostic_indexer_retains_only_host_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: post-launch indexing could activate semantics or phase two."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("phase-one diagnostic indexer reached " + name)
        return reject

    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("semantic-replay"))
    monkeypatch.setattr(runner, "plan_semantic_events", forbidden("semantic-plan"))
    monkeypatch.setattr(runner, "extract_event_markers", forbidden("event-extraction"))
    monkeypatch.setattr(
        runner, "_semantic_commands_from_conversion", forbidden("semantic-command-projection"),
    )
    monkeypatch.setattr(runner, "_emit_semantic_plan", forbidden("semantic-emission"))
    monkeypatch.setattr(runner, "_project_repository_assertions", forbidden("assertion-projection"))
    monkeypatch.setattr(
        runner, "_private_interpretation_decision_projection", forbidden("decision-projection"),
    )
    monkeypatch.setattr(runner, "_phase_one_web_capture_authorized", forbidden("web-authority"))
    monkeypatch.setattr(
        runner, "_reconstruct_actual_phase_one_receipt_gate", forbidden("receipt-gate"),
    )
    monkeypatch.setattr(runner, "_finalize_log", forbidden("finalizer"))
    monkeypatch.setattr(runner, "_publish_sealed_log", forbidden("publisher"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("pass-publisher"),
    )
    monkeypatch.setattr(runner, "run_scenario", forbidden("legacy-route"))
    monkeypatch.setattr(runner, "_run_registered_actual_plan", forbidden("dispatcher"))
    monkeypatch.setattr(runner, "_indexed_run_manifest", forbidden("manifest-index"))
    monkeypatch.setattr(runner, "_indexed_execution_binding", forbidden("execution-binding"))
    monkeypatch.setattr(
        runner, "_prepare_registered_live_executable", forbidden("phase-two-preparation"),
    )
    monkeypatch.setattr(
        runner, "_registered_live_trusted_context", forbidden("aggregate-context"),
    )
    monkeypatch.setattr(runner, "_registered_probe", forbidden("probe"))
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden("live-launch"))
    monkeypatch.setattr(
        runner, "_launch_actual_registered_live_phase", forbidden("actual-launch"),
    )
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden("popen"))
    monkeypatch.setattr(
        runner.WorkspaceSnapshotter, "_staged_paths", forbidden("final-workspace-snapshot"),
    )

    try:
        signature = inspect.signature(runner._index_registered_actual_phase_one_diagnostic)
        assert tuple(signature.parameters) == ("prepared_phase", "live_phase")
        indexed = runner._index_registered_actual_phase_one_diagnostic(
            prepared_phase, live_phase,
        )

        assert type(indexed) is runner._RegisteredActualPhaseOneDiagnostic
        assert indexed.prepared_phase is prepared_phase
        assert indexed.live_phase is live_phase
        assert indexed.policy_id == "policy-1"
        assert indexed.process_id == "process-1"
        assert indexed.transcript_id == "transcript-1"
        assert indexed.stderr_id == "stderr-1"
        assert indexed.trace_id == indexed.conversion.trace_id == "trace-execution-1"
        assert indexed.trace_sha256 == indexed.conversion.trace_sha256
        assert indexed.normalized.complete is True
        assert len(indexed.conversion.observation_ids) == 4
        with pytest.raises(TypeError):
            indexed.normalized.records[0] = {}  # type: ignore[index]

        entries = {entry["id"]: entry for entry in writer.entries}
        adopted_ids = {
            "policy-1", "process-1", "transcript-1", "stderr-1",
            "trace-execution-1", "raw-capture-index-execution-1",
            "raw-command-log-execution-1",
        }
        assert adopted_ids <= set(entries)
        assert all(
            entries[artifact_id]["relative_path"].startswith(".phase-one-diagnostic-")
            for artifact_id in adopted_ids
        )
        policy = json.loads((writer.run_root / entries["policy-1"]["relative_path"]).read_text())
        process = json.loads((writer.run_root / entries["process-1"]["relative_path"]).read_text())
        assert policy["argv"] == list(live_phase.argv)
        assert policy["argv_sha256"] == hashlib.sha256(_encoded(list(live_phase.argv))).hexdigest()
        assert policy["phase_prompt_id"] == prepared_phase.phase_prompt.artifact_id
        assert policy["phase_prompt_sha256"] == prepared_phase.phase_prompt.sha256
        assert policy["phase_prompt_transport"] == prepared_phase.phase_prompt.transport
        assert policy["executable_id"] == live_phase.executable_id
        assert policy["fixture_capability_id"] is None
        assert policy["approval"] is None
        assert process["argv"] == list(live_phase.argv)
        assert process["trace_id"] == indexed.trace_id
        assert process["trace_sha256"] == indexed.trace_sha256
        assert process["transcript_id"] == "transcript-1"
        assert process["transcript_sha256"] == hashlib.sha256(live_phase.capture.stdout).hexdigest()
        assert process["fixture_capability_id"] is None
        assert process["approval"] is None
        assert (writer.run_root / entries["transcript-1"]["relative_path"]).read_bytes() == (
            live_phase.capture.stdout
        )
        assert (writer.run_root / entries["stderr-1"]["relative_path"]).read_bytes() == (
            live_phase.capture.stderr
        )
        assert runner._registered_phase_evidence_is_bound(
            writer=writer,
            workspace=prepared_phase.workspace,
            execution_id="execution-1",
            phase="approval",
            live_phase=live_phase,
        )
        assert {entry["type"] for entry in writer.entries}.isdisjoint({
            "marker", "event_record", "interpretation_decision", "approval",
            "fixture_capability", "run_manifest", "assertion",
        })
        assert not (writer.run_root / "run-manifest.json").exists()
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_two_setup_rejects_an_unbound_retained_phase_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a mutable phase-one snapshot handle cannot seed phase-two reuse."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    prepared_run = prepared_phase.prepared_run
    writer = prepared_run.writer
    try:
        diagnostic = runner._index_registered_actual_phase_one_diagnostic(
            prepared_phase, live_phase,
        )
        from tests.evals import event_log_contract as contract

        verify_id, consume_id, durable_id, acknowledge_id = diagnostic.conversion.observation_ids
        receipt_gate = runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=contract.InitialSyncReceiptFacts(
                result_id="sync_" + "a" * 64,
                corpus_revision="b" * 64,
                event_counts={},
                effect_digest="c" * 64,
                selection=contract.InitialSyncReceiptSelection(
                    verify_command_id=verify_id,
                    consume_command_id=consume_id,
                    durable_command_id=durable_id,
                    acknowledge_command_id=acknowledge_id,
                    stream_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        verify_id
                    ]["stream"],
                    durable_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        durable_id
                    ]["durable"],
                ),
            ),
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )
        unbound = runner._persist_workspace_snapshot(
            writer,
            execution_id="execution-1",
            trace_sequence=99,
            inventory=prepared_phase.initial_workspace_snapshot.inventory,
            staged_paths=prepared_phase.initial_workspace_snapshot.staged_paths,
            role="marker",
            native_record={
                "transcript_id": "transcript-1",
                "byte_start": 0,
                "byte_end": 1,
                "sha256": "d" * 64,
            },
            artifact_id="unbound-phase-one-marker-snapshot",
        )
        prepared_phase.marker_workspace_snapshots.append(unbound)
        monkeypatch.setattr(
            runner,
            "_reconstruct_actual_phase_one_receipt_gate",
            lambda **_kwargs: receipt_gate,
        )
        before_entries = list(writer.entries)
        control_paths = (
            prepared_phase.phase_control.config.path,
            prepared_phase.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}

        with pytest.raises(runner.RunnerError, match="snapshot"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert writer.entries == before_entries
        assert {path: path.read_bytes() for path in control_paths} == before_control
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_two_setup_rejects_corrupt_reserved_content_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an unused corrupt content row cannot defer failure past setup writes."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    prepared_run = prepared_phase.prepared_run
    writer = prepared_run.writer
    try:
        diagnostic = runner._index_registered_actual_phase_one_diagnostic(
            prepared_phase, live_phase,
        )
        from tests.evals import event_log_contract as contract

        verify_id, consume_id, durable_id, acknowledge_id = diagnostic.conversion.observation_ids
        receipt_gate = runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=contract.InitialSyncReceiptFacts(
                result_id="sync_" + "a" * 64,
                corpus_revision="b" * 64,
                event_counts={},
                effect_digest="c" * 64,
                selection=contract.InitialSyncReceiptSelection(
                    verify_command_id=verify_id,
                    consume_command_id=consume_id,
                    durable_command_id=durable_id,
                    acknowledge_command_id=acknowledge_id,
                    stream_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        verify_id
                    ]["stream"],
                    durable_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        durable_id
                    ]["durable"],
                ),
            ),
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )
        declared = b"declared-phase-one-content\n"
        corrupted = b"corruptd-phase-one-content\n"
        assert len(corrupted) == len(declared)
        digest = hashlib.sha256(declared).hexdigest()
        artifact_id = "workspace-content-" + digest
        artifact_path = writer.run_root / "artifacts" / (artifact_id + ".bin")
        artifact_path.write_bytes(corrupted)
        writer.entries.append({
            "id": artifact_id,
            "type": "file_capture",
            "relative_path": f"artifacts/{artifact_id}.bin",
            "sha256": digest,
            "bytes": len(declared),
        })
        monkeypatch.setattr(
            runner,
            "_reconstruct_actual_phase_one_receipt_gate",
            lambda **_kwargs: receipt_gate,
        )
        before_entries = list(writer.entries)
        control_paths = (
            prepared_phase.phase_control.config.path,
            prepared_phase.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}

        with pytest.raises(runner.RunnerError, match="artifact changed|content artifact"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert writer.entries == before_entries
        assert {path: path.read_bytes() for path in control_paths} == before_control
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


@pytest.mark.parametrize("residue", ("sealed", "orphan-phase-two"))
def test_actual_phase_two_setup_rejects_terminal_or_orphan_residue_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue: str,
) -> None:
    """Break caught: phase-two setup cannot append to final or unindexed phase-one output."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    prepared_run = prepared_phase.prepared_run
    writer = prepared_run.writer
    try:
        diagnostic = runner._index_registered_actual_phase_one_diagnostic(
            prepared_phase, live_phase,
        )
        from tests.evals import event_log_contract as contract

        verify_id, consume_id, durable_id, acknowledge_id = diagnostic.conversion.observation_ids
        receipt_gate = runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=contract.InitialSyncReceiptFacts(
                result_id="sync_" + "a" * 64,
                corpus_revision="b" * 64,
                event_counts={},
                effect_digest="c" * 64,
                selection=contract.InitialSyncReceiptSelection(
                    verify_command_id=verify_id,
                    consume_command_id=consume_id,
                    durable_command_id=durable_id,
                    acknowledge_command_id=acknowledge_id,
                    stream_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        verify_id
                    ]["stream"],
                    durable_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        durable_id
                    ]["durable"],
                ),
            ),
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )
        if residue == "sealed":
            writer.seal()
        else:
            (writer.run_root / "artifacts" / "policy-2.json").write_bytes(b"{}\n")
        sealed_reader_calls: list[object] = []
        monkeypatch.setattr(
            runner,
            "_reconstruct_actual_phase_one_receipt_gate",
            lambda **_kwargs: sealed_reader_calls.append("called") or receipt_gate,
        )
        before_entries = list(writer.entries)
        control_paths = (
            prepared_phase.phase_control.config.path,
            prepared_phase.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}

        with pytest.raises(runner.RunnerError, match="sealed|output inventory"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert sealed_reader_calls == []
        assert writer.entries == before_entries
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert not any(entry["id"] in {"mcp-2", "phase-prompt-2"} for entry in writer.entries)
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_hook_or_tampered_live_phase_before_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: detached authority could add host records after phase one."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    try:
        monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", object())
        with pytest.raises(runner.RunnerError, match="test-only"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before

        monkeypatch.setattr(runner, "_TEST_ONLY_REGISTERED_LIVE_LAUNCH", None)
        tampered = replace(live_phase, argv=("relative-client", *live_phase.argv[1:]))
        with pytest.raises(runner.RunnerError, match="argv|executable"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, tampered)
        assert writer.entries == before
        assert not any(entry["id"] in {"policy-1", "process-1"} for entry in writer.entries)

        detached = replace(prepared_phase, phase="approved_capture")
        with pytest.raises(runner.RunnerError, match="phase-one diagnostic"):
            runner._index_registered_actual_phase_one_diagnostic(detached, live_phase)
        assert writer.entries == before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_output_collision_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: retrying a partial index could retain/conversion-write around a duplicate."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    writer.add_json("policy-1", "policy", {"stale": True})
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("output collision reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="no longer disconnected"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_unindexed_output_collision_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an orphaned output file could retain a transcript before O_EXCL fails."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    stale = writer.run_root / "artifacts" / "policy-1.json"
    stale.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale.write_bytes(b'{"stale":true}\n')
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("unindexed output collision reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="output collision"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rechecks_output_inventory_at_write_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a late orphaned output could bypass an earlier-only inventory check."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    stale = writer.run_root / "artifacts" / "policy-1.json"
    before = list(writer.entries)
    calls: list[str] = []
    original_verify_prompt = runner._verify_phase_prompt
    verification_count = 0

    def inject_stale_output(prompt: runner.PhasePromptEvidence) -> None:
        nonlocal verification_count
        original_verify_prompt(prompt)
        verification_count += 1
        if verification_count == 2:
            stale.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stale.write_bytes(b'{"late":true}\n')

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("late output collision reached " + name)
        return reject

    monkeypatch.setattr(runner, "_verify_phase_prompt", inject_stale_output)
    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="output collision"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert verification_count == 2
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_raw_phase_two_capture_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a phase-two raw bridge row could reach diagnostic writes."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    capture_index = writer.control_root / "brain-shim-capture-index.jsonl"
    rows = [json.loads(line) for line in capture_index.read_bytes().splitlines()]
    assert rows and rows[0]["phase"] == "approval"
    rows[0]["phase"] = "approved_capture"
    capture_index.write_bytes(b"".join(_encoded(row) + b"\n" for row in rows))
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("raw phase-two capture reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="phase-two"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_unreferenced_setup_file_capture_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a generic host artifact could contaminate the sealed setup inventory."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    writer.add_bytes("foreign-file-capture", "file_capture", b"unreferenced setup bytes\n")
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("foreign setup artifact reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="evidence inventory"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_phase_two_state_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: phase two could not be prepared before phase-one authority exists."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    writer.add_json("policy-2", "policy", {"stale": True})
    (writer.control_root / "runner-trace-execution-2.jsonl").write_bytes(b"{}\n")
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("phase-two state reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="phase-two"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_phase_two_trace_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an unindexed future trace could not bypass writer-entry checks."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    (writer.control_root / "runner-trace-execution-2.jsonl").write_bytes(b"{}\n")
    before = list(writer.entries)
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("phase-two trace reached " + name)
        return reject

    monkeypatch.setattr(runner, "_preserve_transcript", forbidden("transcript-retention"))
    monkeypatch.setattr(runner, "convert_shim_captures", forbidden("conversion"))
    try:
        with pytest.raises(runner.RunnerError, match="phase-two"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_keeps_failed_conversion_private_and_unbound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: malformed raw capture could not become a semantic/public retry path."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    capture_index = writer.control_root / "brain-shim-capture-index.jsonl"
    capture_index.write_bytes(capture_index.read_bytes() + b"{malformed-capture}\n")
    calls: list[str] = []

    def forbidden(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            pytest.fail("failed conversion reached " + name)
        return reject

    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden("private-overlay"))
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden("semantic-replay"))
    monkeypatch.setattr(
        runner, "_reconstruct_actual_phase_one_receipt_gate", forbidden("receipt-gate"),
    )
    monkeypatch.setattr(runner, "_finalize_log", forbidden("finalizer"))
    monkeypatch.setattr(runner, "_publish_sealed_log", forbidden("publisher"))
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate", forbidden("pass-publisher"),
    )
    try:
        with pytest.raises(runner.RunnerError, match="shim capture index|JSON"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)

        # Conversion is contained in a private stage.  A malformed capture
        # must not adopt even the diagnostic prefix into the base writer.
        assert writer.entries == before
        stages = list(writer.run_root.glob(".phase-one-diagnostic-*"))
        assert len(stages) == 1
        assert not (stages[0] / "evidence-index.json").exists()
        assert {entry["type"] for entry in writer.entries}.isdisjoint({
            "marker", "event_record", "interpretation_decision", "approval",
            "fixture_capability", "run_manifest", "assertion",
        })
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not runner._registered_phase_evidence_is_bound(
            writer=writer,
            workspace=prepared_phase.workspace,
            execution_id="execution-1",
            phase="approval",
            live_phase=live_phase,
        )
        with pytest.raises(runner.RunnerError, match="output collision|stage|disconnected"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert calls == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


@pytest.mark.parametrize(
    ("phase", "missing"),
    [("main", False), (None, False), (7, False), ("approval", True)],
)
def test_actual_phase_one_diagnostic_indexer_rejects_non_approval_raw_phase_before_stage_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: object,
    missing: bool,
) -> None:
    """Break caught: a parsable raw row could evade the phase-one boundary."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    capture_index = writer.control_root / "brain-shim-capture-index.jsonl"
    rows = [json.loads(line) for line in capture_index.read_bytes().splitlines()]
    assert rows
    if missing:
        del rows[0]["phase"]
    else:
        rows[0]["phase"] = phase
    capture_index.write_bytes(b"".join(_encoded(row) + b"\n" for row in rows))
    before = list(writer.entries)
    try:
        with pytest.raises(runner.RunnerError, match="raw capture.*phase"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert not list(writer.run_root.glob(".phase-one-diagnostic-*"))
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_late_raw_snapshot_change_without_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a raw bridge replacement after freeze could be adopted."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    capture_index = writer.control_root / "brain-shim-capture-index.jsonl"
    before = list(writer.entries)
    original = runner.convert_shim_captures

    def mutate_then_convert(*args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        capture_index.write_bytes(capture_index.read_bytes() + b"\n")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "convert_shim_captures", mutate_then_convert)
    try:
        with pytest.raises(runner.RunnerError, match="raw capture|frozen"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
        assert len(list(writer.run_root.glob(".phase-one-diagnostic-*"))) == 1
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


@pytest.mark.parametrize(
    "trace_name",
    ["runner-trace-execution-2.jsonl", "runner-trace-foreign.jsonl"],
)
def test_actual_phase_one_diagnostic_indexer_rejects_late_foreign_trace_without_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace_name: str,
) -> None:
    """Break caught: a future/foreign runner trace could arrive after preflight."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    original = runner.convert_shim_captures

    def inject_foreign_trace(*args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        (writer.control_root / trace_name).write_bytes(b"{}\n")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "convert_shim_captures", inject_foreign_trace)
    try:
        with pytest.raises(runner.RunnerError, match="trace"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_post_final_guard_flat_collision_without_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a flat policy collision after final guards could win adoption."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    original = runner.convert_shim_captures

    def inject_flat_collision(*args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        (writer.run_root / "artifacts" / "policy-1.json").write_bytes(b"{\"late\":true}\n")
        return result

    monkeypatch.setattr(runner, "convert_shim_captures", inject_flat_collision)
    try:
        with pytest.raises(runner.RunnerError, match="output collision"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert writer.entries == before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_pinned_private_child_rejects_replacement_before_descriptor_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: mkdir followed by pathname pinning could select a replacement stage."""

    control_root = tmp_path / "private-control"
    control_root.mkdir(mode=0o700)
    writer = runner.EvidenceWriter(control_root, "a" * 64)
    base_before = list(writer.entries)
    run_pin = runner.PinnedDirectory.pin(writer.run_root, private=True)
    original_mkdir = os.mkdir
    original_rename = os.rename
    original_fsync = os.fsync
    created: dict[str, str] = {}
    replaced = False

    def capture_mkdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd == run_pin.root_fd:
            created["name"] = os.fspath(path)

    def replace_after_mkdir(fd: int) -> None:
        nonlocal replaced
        if fd == run_pin.root_fd and not replaced and "name" in created:
            name = created["name"]
            moved = name + ".moved"
            original_rename(name, moved, src_dir_fd=fd, dst_dir_fd=fd)
            original_mkdir(name, 0o700, dir_fd=fd)
            replaced = True
        original_fsync(fd)

    monkeypatch.setattr(runner.os, "mkdir", capture_mkdir)
    monkeypatch.setattr(runner.os, "fsync", replace_after_mkdir)
    try:
        with pytest.raises(runner.RunnerError, match="private diagnostic stage|changed"):
            run_pin.create_private_child(".phase-one-diagnostic-")
        assert replaced is True
        assert writer.entries == base_before
    finally:
        run_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_same_byte_staged_swap_before_adoption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a staged same-byte replacement could evade tree-only commit checks."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    original = runner.convert_shim_captures
    replaced = False

    def replace_after_conversion(*args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        nonlocal replaced
        conversion = original(*args, **kwargs)  # type: ignore[arg-type]
        stage_writer = args[0]
        assert type(stage_writer) is runner._PrivatePhaseOneDiagnosticWriter
        policy = stage_writer.stage_pin.path / "policy-1.json"
        replacement = stage_writer.stage_pin.path / "same-byte-policy-replacement.json"
        replacement.write_bytes(policy.read_bytes())
        os.replace(replacement, policy)
        if policy.exists():
            replaced = True
        return conversion

    monkeypatch.setattr(runner, "convert_shim_captures", replace_after_conversion)
    try:
        with pytest.raises(runner.RunnerError, match="staged.*artifact|staged.*tree|stage is invalid|adoption"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert replaced is True
        assert writer.entries == before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_does_not_read_trace_before_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: stale inventory rows could bind a newer frozen valid trace."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    original_records = runner.ControlTraceWriter.records
    replaced = False

    def replace_after_first_read(trace: runner.ControlTraceWriter) -> list[dict[str, object]]:
        nonlocal replaced
        rows = original_records(trace)
        if trace is prepared_phase.trace and not replaced:
            replacement = [dict(row) for row in rows]
            native = next(row for row in replacement if row["kind"] == "native_record")
            native["workspace_snapshot_id"] = "forged-snapshot"
            trace.path.write_bytes(b"".join(runner._line(row) for row in replacement))
            replaced = True
        return rows

    monkeypatch.setattr(runner.ControlTraceWriter, "records", replace_after_first_read)
    try:
        indexed = runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert type(indexed) is runner._RegisteredActualPhaseOneDiagnostic
        # The repaired route never consults the mutable trace reader before
        # freezing it, so the legacy read-and-replace hook cannot run.
        assert replaced is False
        assert writer.entries != before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_one_diagnostic_indexer_rejects_valid_trace_replacement_during_freeze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a frozen trace replacement cannot reach inventory or adoption."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    writer = prepared_phase.writer
    before = list(writer.entries)
    original_snapshot = runner.PinnedDirectory.snapshot_relative
    replaced = False

    def replace_after_trace_snapshot(
        pin: runner.PinnedDirectory, relative: str,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int, int, int]]:
        nonlocal replaced
        snapshot = original_snapshot(pin, relative)
        if pin is prepared_phase.control_pin and relative == prepared_phase.trace.path.name:
            replacement = writer.control_root / "valid-trace-replacement.jsonl"
            replacement.write_bytes(snapshot[0])
            os.replace(replacement, prepared_phase.trace.path)
            replaced = True
        return snapshot

    monkeypatch.setattr(runner.PinnedDirectory, "snapshot_relative", replace_after_trace_snapshot)
    try:
        with pytest.raises(runner.RunnerError, match="frozen control artifact changed|trace"):
            runner._index_registered_actual_phase_one_diagnostic(prepared_phase, live_phase)
        assert replaced is True
        assert writer.entries == before
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_pinned_snapshot_does_not_pair_old_bytes_with_replacement_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: split snapshot read/stat could pair old bytes with a new inode."""

    root = tmp_path / "snapshot-root"
    root.mkdir(mode=0o700)
    target = root / "raw.jsonl"
    target.write_bytes(b'{"phase":"approval"}\n')
    pin = runner.PinnedDirectory.pin(root, private=True)
    original_identity = runner.PinnedDirectory._regular_identity(target.lstat())
    original_read = runner.PinnedDirectory.read_relative
    replaced = False

    def replace_after_read(
        selected: runner.PinnedDirectory, relative: str, *, maximum: int = runner._MAX_CAPTURE_BYTES,
    ) -> bytes:
        nonlocal replaced
        raw = original_read(selected, relative, maximum=maximum)
        if selected is pin and relative == "raw.jsonl":
            replacement = root / "replacement.jsonl"
            replacement.write_bytes(raw)
            os.replace(replacement, target)
            replaced = True
        return raw

    monkeypatch.setattr(runner.PinnedDirectory, "read_relative", replace_after_read)
    try:
        _raw, identity = pin.snapshot_relative("raw.jsonl")
        # The vulnerable read-then-stat implementation returns the old bytes
        # with this replacement identity. The repaired one does not delegate
        # to read_relative, retaining the original descriptor identity.
        assert identity == original_identity
        if replaced:
            with pytest.raises(runner.RunnerError, match="frozen control artifact changed"):
                pin.reverify_snapshot("raw.jsonl", (_raw, identity))
    finally:
        pin.close()


def test_actual_default_launch_wrapper_uses_production_defaults_and_sealed_phase_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: actual launch could accept a seam or lose sealed argv/prompt facts."""

    prepared_phase, executable, writer, mcp_path, state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch,
    )
    expected_prompt = prepared_phase.phase_prompt.raw
    expected_argv = _claude_template()
    expected_argv[0] = str(executable)
    expected_argv[expected_argv.index("{mcp_config}")] = str(mcp_path)
    expected_argv[-1] = expected_prompt.decode("utf-8", "strict")
    expected_argv_sha256 = hashlib.sha256(_encoded(expected_argv)).hexdigest()
    probes: list[tuple[list[str], Path, dict[str, str], object]] = []
    launches: list[tuple[list[str], dict[str, object]]] = []
    low_level_calls: list[dict[str, object]] = []

    def probe(
        *,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        probes.append((list(argv), cwd, dict(environment), probe_runner))
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(b'{"type":"result"}\n')
            self.stderr = _task7d_closed_pipe(b"diagnostic\n")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            assert self.stdout.closed and self.stderr.closed
            return 0

    process = CompleteProcess()

    def production_popen(argv: list[str], **kwargs: object) -> CompleteProcess:
        launches.append((list(argv), dict(kwargs)))
        return process

    original_launch = runner._launch_registered_live_phase

    def observe_low_level(**kwargs: object) -> runner.RegisteredLivePhase:
        low_level_calls.append(dict(kwargs))
        return original_launch(**kwargs)

    monkeypatch.setattr(runner, "_registered_probe", probe)
    # This wrapper test owns the production client Popen boundary.  Native
    # marker arrival also performs a Git staged-path query; keep that separate
    # so the global Popen double observes only the intended client launch.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner.subprocess, "Popen", production_popen)
    monkeypatch.setattr(runner, "_launch_registered_live_phase", observe_low_level)

    phase = runner._launch_actual_registered_live_phase(prepared_phase)

    assert len(low_level_calls) == 1
    assert low_level_calls[0]["probe_runner"] is None
    assert low_level_calls[0]["popen_factory"] is runner.subprocess.Popen
    assert probes == [
        ([str(executable), "--version"], prepared_phase.workspace, {
            "PATH": "/usr/bin:/bin",
            "HOME": str(writer.control_root / "home"),
            "LANG": "C.UTF-8",
            runner.CONTROL_ENV: str(writer.control_root),
            runner.RUNNER_STATE_ENV: str(state_path),
        }, None),
        ([str(executable), "--help"], prepared_phase.workspace, {
            "PATH": "/usr/bin:/bin",
            "HOME": str(writer.control_root / "home"),
            "LANG": "C.UTF-8",
            runner.CONTROL_ENV: str(writer.control_root),
            runner.RUNNER_STATE_ENV: str(state_path),
        }, None),
    ]
    assert launches == [(expected_argv, {
        "cwd": prepared_phase.workspace,
        "env": probes[0][2],
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
        "close_fds": True,
        "shell": False,
    })]
    assert phase.argv == tuple(expected_argv)
    assert phase.argv[0] == str(executable)
    assert phase.capture.stdout == b'{"type":"result"}\n'
    assert phase.capture.stderr == b"diagnostic\n"
    assert process.stdin.writes == []
    assert prepared_phase.phase_prompt.transport == "argv_final_utf8"
    assert phase.argv[-1] == expected_prompt.decode("utf-8", "strict")
    assert hashlib.sha256(_encoded(list(phase.argv))).hexdigest() == expected_argv_sha256

    entries = {entry["id"]: entry for entry in writer.entries}
    assert {phase.version_id, phase.help_id, phase.executable_id} == {
        "version-1", "help-1", "client-executable-1",
    }
    assert len({phase.version_id, phase.help_id, phase.executable_id}) == 3
    assert entries[phase.version_id]["type"] == "version"
    assert entries[phase.help_id]["type"] == "help"
    executable_evidence = json.loads(
        (writer.run_root / str(entries[phase.executable_id]["relative_path"])).read_text(),
    )
    assert executable_evidence["version_probe"]["argv"] == [str(executable), "--version"]
    assert executable_evidence["help_probe"]["argv"] == [str(executable), "--help"]
    # Policy/process indexing belongs to the closed phase-one diagnostic bridge,
    # not this launch wrapper.  The wrapper returns the exact final tuple that
    # those records must hash, without creating a semantic or pass artifact.
    assert {entry["type"] for entry in writer.entries}.isdisjoint({"policy", "process"})


def test_actual_default_launch_wrapper_has_closed_input_and_rejects_test_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a test adapter could become an actual-wrapper input or route."""

    prepared_phase, _executable, _writer, _mcp_path, _state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch,
    )
    signature = inspect.signature(runner._launch_actual_registered_live_phase)
    assert tuple(signature.parameters) == ("prepared_phase",)
    forbidden = {
        "process_runner", "probe_runner", "popen_factory", "argv", "environment",
        "template", "prompt", "client_command_json", "network_attestation",
    }
    assert not forbidden & set(signature.parameters)
    reached: list[object] = []

    def low_level(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        pytest.fail("test adapter reached actual low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", low_level)
    monkeypatch.setattr(
        runner,
        "_TEST_ONLY_REGISTERED_LIVE_LAUNCH",
        runner._TestOnlyRegisteredLiveLaunch(
            probe_runner=lambda *_args: subprocess.CompletedProcess([], 0, b"test\n", b""),
            popen_factory=lambda *_args, **_kwargs: object(),
        ),
    )

    with pytest.raises(runner.RunnerError, match="test-only"):
        runner._launch_actual_registered_live_phase(prepared_phase)
    assert reached == []


def test_actual_default_launch_wrapper_rejects_alternate_control_runner_state_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: an in-control alternate state locator could steer the shim."""

    prepared_phase, _executable, _writer, _mcp_path, _state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch, state_name="alternate-runner-state.json",
    )
    reached: list[object] = []

    def low_level(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        pytest.fail("alternate runner state reached actual low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", low_level)

    with pytest.raises(runner.RunnerError, match="runner state locator"):
        runner._launch_actual_registered_live_phase(prepared_phase)
    assert reached == []


@pytest.mark.parametrize(
    "state_overrides",
    (
        {"execution_id": "execution-2"},
        {"workspace": "wrong-workspace"},
    ),
    ids=("wrong-execution", "wrong-workspace"),
)
def test_actual_default_launch_wrapper_rejects_wrong_canonical_runner_state_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_overrides: dict[str, object],
) -> None:
    """Break caught: shape-valid state bytes could bind a different phase/root."""

    prepared_phase, _executable, _writer, _mcp_path, _state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch, state_overrides=state_overrides,
    )
    reached: list[object] = []

    def low_level(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        pytest.fail("wrong runner state reached actual low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", low_level)

    with pytest.raises(runner.RunnerError, match="runner state binding"):
        runner._launch_actual_registered_live_phase(prepared_phase)
    assert reached == []


@pytest.mark.parametrize(
    "mutation",
    ("execution-two-trace", "cross-phase-transcript", "alternate-trace-lock"),
    ids=("execution-two-trace", "cross-phase-transcript", "alternate-trace-lock"),
)
def test_actual_default_launch_wrapper_rejects_cross_phase_trace_identity_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Break caught: a phase could inherit a different trace/transcript lock."""

    prepared_phase, _executable, _writer, _mcp_path, _state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch,
    )
    if mutation == "execution-two-trace":
        tampered = replace(
            prepared_phase,
            trace=runner.ControlTraceWriter(
                prepared_phase.writer.control_root,
                "execution-2",
                control_pin=prepared_phase.control_pin,
            ),
        )
    elif mutation == "cross-phase-transcript":
        tampered = replace(prepared_phase, transcript_id="transcript-2")
    else:
        trace = runner.ControlTraceWriter(
            prepared_phase.writer.control_root,
            prepared_phase.execution_id,
            control_pin=prepared_phase.control_pin,
        )
        trace.lock_path = prepared_phase.writer.control_root / ".alternate-trace.lock"
        tampered = replace(prepared_phase, trace=trace)
    reached: list[object] = []

    def low_level(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        pytest.fail("cross-phase trace identity reached actual low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", low_level)

    with pytest.raises(runner.RunnerError, match="trace|transcript"):
        runner._launch_actual_registered_live_phase(tampered)
    assert reached == []


@pytest.mark.parametrize(
    "pin_kind",
    ("nonprivate-control", "private-workspace"),
    ids=("nonprivate-control", "private-workspace"),
)
def test_actual_default_launch_wrapper_requires_private_control_and_public_workspace_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pin_kind: str,
) -> None:
    """Break caught: a phase could swap the privacy role of either pinned root."""

    prepared_phase, _executable, _writer, _mcp_path, _state_path = _task7d_actual_production_phase(
        tmp_path, monkeypatch,
    )
    if pin_kind == "nonprivate-control":
        replacement_pin = runner.PinnedDirectory.pin(
            prepared_phase.writer.control_root, private=False,
        )
        tampered = replace(
            prepared_phase,
            control_pin=replacement_pin,
            trace=runner.ControlTraceWriter(
                prepared_phase.writer.control_root,
                prepared_phase.execution_id,
                control_pin=replacement_pin,
            ),
        )
    else:
        # The real copied workspace is deliberately public-mode.  Change only
        # this disposable adversarial fixture so a wrongly private pin can be
        # constructed and rejected by the wrapper's role check.
        prepared_phase.workspace.chmod(0o700)
        replacement_pin = runner.PinnedDirectory.pin(prepared_phase.workspace, private=True)
        tampered = replace(prepared_phase, workspace_pin=replacement_pin)
    reached: list[object] = []

    def low_level(*_args: object, **_kwargs: object) -> object:
        reached.append("low-level")
        pytest.fail("wrong root pin privacy reached actual low-level launch")

    monkeypatch.setattr(runner, "_launch_registered_live_phase", low_level)
    try:
        with pytest.raises(runner.RunnerError, match="root pin"):
            runner._launch_actual_registered_live_phase(tampered)
    finally:
        replacement_pin.close()
    assert reached == []


def test_legacy_process_runner_cannot_reach_actual_default_launch_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: legacy ProcessRunner could be laundered into the actual wrapper."""

    reached: list[object] = []

    def actual_wrapper(*_args: object, **_kwargs: object) -> object:
        reached.append("actual-wrapper")
        pytest.fail("legacy ProcessRunner reached actual default wrapper")

    monkeypatch.setattr(
        runner, "_launch_actual_registered_live_phase", actual_wrapper, raising=False,
    )

    def injected(
        _argv: list[str], _cwd: Path, _environment: dict[str, str], _stdin: bytes,
    ) -> runner.ProcessCapture:
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"type":"result"}\n', stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path, process_runner=injected,
    )

    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" in outcome.log["incomplete_reasons"]
    assert reached == []


def test_test_only_registered_live_path_uses_the_closed_phase_launcher_without_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal-run live route uses registered evidence, not ProcessRunner bytes.

    The injected Popen/probe adapters make this a host-only test.  Its missing
    shim workflow intentionally prevents a candidate, so it cannot write a
    pass while proving the phase-local registered evidence reaches the normal
    runner assembly path.
    """

    from tests.evals import event_log_contract as contract

    registration_root = tmp_path / "runner-owned-registration"
    registration_root.mkdir(mode=0o700)
    executable = registration_root / "claude"
    executable.write_bytes(b"#!/bin/sh\nprintf 'test client\\n'\n")
    executable.chmod(0o700)
    raw, _identity = contract._read_executable(str(executable))
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({
            "claude": runner.RunnerOwnedExecutableRegistration(
                client="claude", resolved_path=executable, expected_version="2.1.251",
                native_format="claude-stream-json-2.1.251-v1",
            ),
        }),
    )
    monkeypatch.setattr(contract, "_DEFAULT_SUPPORTED_BUILDS", {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", digest): "claude-test-build-v1",
    })
    probes: list[list[str]] = []
    launches: list[tuple[list[str], dict[str, object]]] = []
    validations: list[object] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        probes.append(list(argv))
        assert cwd.name == "workspace"
        assert environ["PATH"] == "/usr/bin:/bin"
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class LiveTestProcess:
        def __init__(self, cwd: Path) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(_claude_transcript([], cwd=str(cwd.resolve())))
            self.stderr = _task7d_closed_pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            assert self.stdout.closed and self.stderr.closed
            return 0

    def popen(argv: list[str], **kwargs: object) -> LiveTestProcess:
        launches.append((list(argv), dict(kwargs)))
        return LiveTestProcess(Path(kwargs["cwd"]))

    monkeypatch.setattr(
        runner,
        "_TEST_ONLY_REGISTERED_LIVE_LAUNCH",
        runner._TestOnlyRegisteredLiveLaunch(probe_runner=probe, popen_factory=popen),
        raising=False,
    )
    monkeypatch.setattr(
        runner, "_validate_prepublication_event_log",
        lambda *_args, **_kwargs: validations.append("called"),
    )
    candidate_attempts: list[object] = []
    monkeypatch.setattr(
        runner,
        "_atomic_validate_and_publish_candidate",
        lambda *_args, **_kwargs: candidate_attempts.append("called"),
    )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
    )

    absolute = str(executable)
    assert probes == [[absolute, "--version"], [absolute, "--help"]]
    assert launches and launches[0][0][0] == absolute
    assert launches[0][1]["shell"] is False
    assert validations == []
    assert candidate_attempts == []
    assert outcome.trusted_context is not None
    assert outcome.log["result"] == "incomplete"
    assert "test_process_not_actual" not in outcome.log["incomplete_reasons"]
    assert "test_registered_live_launch_not_pass_capable" in outcome.log["incomplete_reasons"]
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    policy = json.loads((outcome.log_path.parent / entries["policy-1"]["relative_path"]).read_text())
    process = json.loads((outcome.log_path.parent / entries["process-1"]["relative_path"]).read_text())
    assert policy["executable_id"] == process["executable_id"] == "client-executable-1"
    assert policy["argv"][0] == process["argv"][0] == absolute


def test_test_only_registered_live_hook_cannot_publish_a_forced_complete_private_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a forced complete private replay cannot make the test hook public.

    The test deliberately supplies an otherwise complete-looking planner and
    replay result.  It would fail if the private test launcher relied only on
    today’s missing shim evidence rather than its irrevocable incomplete
    classification.
    """

    from tests.evals import event_log_contract as contract

    registration_root = tmp_path / "runner-owned-registration"
    registration_root.mkdir(mode=0o700)
    executable = registration_root / "claude"
    executable.write_bytes(b"#!/bin/sh\nprintf 'test client\\n'\n")
    executable.chmod(0o700)
    raw, _identity = contract._read_executable(str(executable))
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({
            "claude": runner.RunnerOwnedExecutableRegistration(
                client="claude", resolved_path=executable, expected_version="2.1.251",
                native_format="claude-stream-json-2.1.251-v1",
            ),
        }),
    )
    monkeypatch.setattr(contract, "_DEFAULT_SUPPORTED_BUILDS", {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", digest): "claude-test-build-v1",
    })

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        del cwd, environ
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class Process:
        def __init__(self, cwd: Path) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(_claude_transcript([], cwd=str(cwd.resolve())))
            self.stderr = _task7d_closed_pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            return 0

    monkeypatch.setattr(
        runner,
        "_TEST_ONLY_REGISTERED_LIVE_LAUNCH",
        runner._TestOnlyRegisteredLiveLaunch(
            probe_runner=probe,
            popen_factory=lambda _argv, **kwargs: Process(Path(kwargs["cwd"])),
        ),
    )

    def forced_conversion(*_args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        execution_id = kwargs["execution_id"]
        assert isinstance(execution_id, str)
        return runner.ShimCaptureConversion(
            (), {}, "trace-" + execution_id, "0" * 64, b"", b"", b"",
        )

    complete_plan = runner.SemanticEventPlan((), {}, {}, (), (), ())
    monkeypatch.setattr(runner, "convert_shim_captures", forced_conversion)
    monkeypatch.setattr(runner, "_semantic_commands_from_conversion", lambda *_args: ())
    monkeypatch.setattr(runner, "_verified_marker_trace_evidence", lambda *_args: ())
    monkeypatch.setattr(runner, "_indexed_native_snapshot_ids", lambda *_args: {})
    monkeypatch.setattr(runner, "_semantic_deliveries_from_commands", lambda *_args: ())
    monkeypatch.setattr(runner, "_build_semantic_cursor_proofs", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(runner, "_build_marker_diff_references", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "_build_semantic_packet_references", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "_build_interpretation_approval_reference", lambda *_args, **_kwargs: ({}, None))
    monkeypatch.setattr(runner, "plan_semantic_events", lambda *_args: complete_plan)
    monkeypatch.setattr(runner, "_indexed_execution_binding", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner, "_indexed_semantic_replay", lambda *_args, **_kwargs: {})
    atomic_calls: list[object] = []
    monkeypatch.setattr(
        runner, "_atomic_validate_and_publish_candidate",
        lambda *_args, **_kwargs: atomic_calls.append("called"),
    )

    outcome = runner.run_scenario(
        client="claude", scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
    )

    assert atomic_calls == []
    assert outcome.log["result"] == "incomplete"
    assert outcome.log["events"] == []
    assert outcome.log["receipts"] == []
    assert outcome.log["deliveries"] == []
    assert all(assertion["passed"] is False for assertion in outcome.log["repository_assertions"])
    assert "test_registered_live_launch_not_pass_capable" in outcome.log["incomplete_reasons"]
    types = {
        entry["type"]
        for entry in json.loads((outcome.log_path.parent / "evidence-index.json").read_text())["entries"]
    }
    assert not {"marker", "event_record", "interpretation_decision"} & types


def test_registered_web_context_is_prelaunch_and_phase_gate_sees_bound_phase_one_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase two gets a fresh aggregate context only after a bound phase one.

    This would fail if phase two trust were prepared before the approval gate,
    if the phase-one context were widened in place, or if the gate ran before
    its registered executable/policy/process evidence had been indexed.
    """

    from tests.evals import event_log_contract as contract

    registration_root = tmp_path / "runner-owned-registration"
    registration_root.mkdir(mode=0o700)
    executable = registration_root / "claude"
    executable.write_bytes(b"#!/bin/sh\nprintf 'test client\\n'\n")
    executable.chmod(0o700)
    raw, _identity = contract._read_executable(str(executable))
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({
            "claude": runner.RunnerOwnedExecutableRegistration(
                client="claude", resolved_path=executable, expected_version="2.1.251",
                native_format="claude-stream-json-2.1.251-v1",
            ),
        }),
    )
    monkeypatch.setattr(contract, "_DEFAULT_SUPPORTED_BUILDS", {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", digest): "claude-test-build-v1",
    })
    prepared_ids: list[str] = []
    launched_contexts: list[object] = []
    original_prepare = runner._prepare_registered_live_executable
    original_launch = runner._launch_registered_live_phase

    def prepare(**kwargs: object) -> tuple[runner.RunnerOwnedExecutableRegistration, object]:
        execution_id = kwargs["execution_id"]
        assert isinstance(execution_id, str)
        prepared_ids.append(execution_id)
        return original_prepare(**kwargs)

    def launch(**kwargs: object) -> runner.RegisteredLivePhase:
        launched_contexts.append(kwargs["trusted_context"])
        return original_launch(**kwargs)

    def require_bound_phase_one(**kwargs: object) -> bool:
        assert prepared_ids == ["execution-1"]
        writer = kwargs["writer"]
        assert isinstance(writer, runner.EvidenceWriter)
        entries = {entry["id"] for entry in writer.entries}
        assert {"client-executable-1", "policy-1", "process-1", "version-1", "help-1"} <= entries
        return True

    monkeypatch.setattr(runner, "_prepare_registered_live_executable", prepare)
    monkeypatch.setattr(runner, "_launch_registered_live_phase", launch)
    monkeypatch.setattr(runner, "_phase_one_web_capture_authorized", require_bound_phase_one)

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        del cwd, environ
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class PhaseProcess:
        def __init__(self, cwd: Path) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(_claude_transcript([], cwd=str(cwd.resolve())))
            self.stderr = _task7d_closed_pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            return 0

    monkeypatch.setattr(
        runner,
        "_TEST_ONLY_REGISTERED_LIVE_LAUNCH",
        runner._TestOnlyRegisteredLiveLaunch(
            probe_runner=probe,
            popen_factory=lambda _argv, **kwargs: PhaseProcess(Path(kwargs["cwd"])),
        ),
    )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
    )

    assert prepared_ids == ["execution-1", "execution-2"]
    assert len(launched_contexts) == 2
    first, second = launched_contexts
    assert set(first.trusted_executables) == {"execution-1"}
    assert set(second.trusted_executables) == {"execution-1", "execution-2"}
    assert second.trusted_executables["execution-1"] is first.trusted_executables["execution-1"]
    assert outcome.trusted_context is second
    assert outcome.log["result"] == "incomplete"
    assert "test_registered_live_launch_not_pass_capable" in outcome.log["incomplete_reasons"]


@pytest.mark.parametrize(
    "replacement_mode",
    ("different-bytes", "same-bytes-os-replace"),
)
def test_registered_web_rechecks_phase_one_executable_before_phase_two_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_mode: str,
) -> None:
    """Replacing the approved phase-one binary blocks phase two pre-spawn.

    The byte-identical replacement case protects executable identity, not
    merely the digest: a trusted path can be atomically rebound to a new inode
    without changing its content hash.
    """

    from tests.evals import event_log_contract as contract

    registration_root = tmp_path / "runner-owned-registration"
    registration_root.mkdir(mode=0o700)
    executable = registration_root / "claude"
    original_bytes = b"#!/bin/sh\nprintf 'test client\\n'\n"
    executable.write_bytes(original_bytes)
    executable.chmod(0o700)
    original_inode = executable.stat().st_ino
    raw, _identity = contract._read_executable(str(executable))
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        runner,
        "_RUNNER_OWNED_EXECUTABLES",
        MappingProxyType({
            "claude": runner.RunnerOwnedExecutableRegistration(
                client="claude", resolved_path=executable, expected_version="2.1.251",
                native_format="claude-stream-json-2.1.251-v1",
            ),
        }),
    )
    monkeypatch.setattr(contract, "_DEFAULT_SUPPORTED_BUILDS", {
        ("claude", "2.1.251", "claude-stream-json-2.1.251-v1", digest): "claude-test-build-v1",
    })
    launches: list[str] = []
    probes: list[list[str]] = []

    def probe(argv: list[str], cwd: Path, environ: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        del cwd, environ
        probes.append(list(argv))
        output = b"2.1.251 (test Claude)\n" if argv[-1] == "--version" else b"stream-json help\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    class Process:
        def __init__(self, cwd: Path) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(_claude_transcript([], cwd=str(cwd.resolve())))
            self.stderr = _task7d_closed_pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            return 0

    def popen(_argv: list[str], **kwargs: object) -> Process:
        env = kwargs["env"]
        assert isinstance(env, dict)
        control = Path(env[runner.CONTROL_ENV])
        launches.append(json.loads((control / "brain-shim.json").read_text())["phase"])
        return Process(Path(kwargs["cwd"]))

    def mutate_after_phase_one(**_kwargs: object) -> bool:
        if replacement_mode == "same-bytes-os-replace":
            replacement = registration_root / "claude-replacement"
            replacement.write_bytes(original_bytes)
            replacement.chmod(0o700)
            os.replace(replacement, executable)
        else:
            executable.write_bytes(b"#!/bin/sh\nprintf 'replaced client\\n'\n")
        return True

    monkeypatch.setattr(runner, "_phase_one_web_capture_authorized", mutate_after_phase_one)
    monkeypatch.setattr(
        runner,
        "_TEST_ONLY_REGISTERED_LIVE_LAUNCH",
        runner._TestOnlyRegisteredLiveLaunch(probe_runner=probe, popen_factory=popen),
    )

    outcome = runner.run_scenario(
        client="claude", scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()), network_attestation="mock-only",
        scratch_root=tmp_path,
    )

    assert launches == ["approval"]
    assert len(probes) == 2
    if replacement_mode == "same-bytes-os-replace":
        assert executable.read_bytes() == original_bytes
        assert executable.stat().st_ino != original_inode
    assert "execution_integrity_failure" in outcome.log["incomplete_reasons"]
    assert outcome.trusted_context is not None
    assert set(outcome.trusted_context.trusted_executables) == {"execution-1"}
    assert outcome.log["result"] == "incomplete"


def test_each_native_trace_row_binds_its_indexed_workspace_snapshot(
    tmp_path: Path,
) -> None:
    """A sealed native row owns the immutable snapshot captured at arrival."""

    def fake_process(
        argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes,
    ) -> runner.ProcessCapture:
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"first"}\n{"event":"second"}\n',
            stderr=b"", version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    entries = {entry["id"]: entry for entry in index["entries"]}
    trace = [
        json.loads(line)
        for line in (outcome.control_root / "runner-trace-execution-1.jsonl").read_text().splitlines()
    ]
    native_rows = [row for row in trace if row["kind"] == "native_record"]

    assert len(native_rows) == 2
    assert len({row["workspace_snapshot_id"] for row in native_rows}) == 2
    for row in native_rows:
        snapshot_id = row["workspace_snapshot_id"]
        entry = entries[snapshot_id]
        assert entry["type"] == "workspace_snapshot"
        snapshot = json.loads((outcome.log_path.parent / entry["relative_path"]).read_text())
        assert snapshot["anchor_kind"] == "native_record"
        assert snapshot["trace_sequence"] == row["sequence"]
        assert snapshot["transcript_id"] == row["transcript_id"]
        assert snapshot["native_record_start"] == row["byte_start"]
        assert snapshot["native_record_end"] == row["byte_end"]
        assert snapshot["native_record_sha256"] == row["sha256"]


def test_shim_capture_conversion_indexes_command_and_receipt_boundary_bytes(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=completed.stderr,
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    types = {entry["type"] for entry in index["entries"]}
    assert {
        "command_observation", "command_result", "source_state", "source_record",
        "sync_stream", "execution_trace",
    } <= types
    observations = [entry for entry in index["entries"] if entry["type"] == "command_observation"]
    assert len(observations) == 1


def test_preapply_wiki_manifest_is_captured_before_dispatch_and_projected(
    tmp_path: Path,
) -> None:
    """A later workspace rewrite cannot change the manifest normalizer sees."""

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path,
    )
    phase_control = runner._phase_control(prepared, phase="main", approval=None)
    state = runner._write_runner_state(
        prepared, run_id="a" * 64, execution_id="main", phase="main",
    )
    relative = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
    manifest = prepared.workspace / relative
    manifest.parent.mkdir(mode=0o700, parents=True)
    from brainlib.contracts import compute_corpus_revision

    records = runner.LedgerStore(runner.RepoPaths.discover(prepared.workspace)).load_all()
    original = _encoded({
        "schema_version": 1,
        "expected_corpus_revision": compute_corpus_revision(records.values()),
        "change_intent": "routine",
        "approval_event_id": None,
        "citation_rewrites": [],
        "link_candidate_runs": [],
        "changes": [],
    }) + b"\n"
    manifest.write_bytes(original)
    environment = {
        "PATH": "/usr/bin:/bin", "HOME": str(prepared.control_root / "home"),
        "LANG": "C.UTF-8", runner.CONTROL_ENV: str(prepared.control_root),
        runner.RUNNER_STATE_ENV: str(state.path),
    }

    completed = subprocess.run(
        [str(prepared.workspace / "brain"), "--json", "wiki", "apply", "--manifest", relative],
        cwd=prepared.workspace, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert completed.returncode in {0, 1, 2}
    runner.verify_phase_control(phase_control)
    raw_rows = [
        json.loads(line)
        for line in (prepared.control_root / "runner-preapply-manifests.jsonl").read_text().splitlines()
    ]
    assert len(raw_rows) == 1
    raw = raw_rows[0]
    assert raw["argv"] == ["./brain", "--json", "wiki", "apply", "--manifest", relative]
    assert (prepared.control_root / raw["capture_path"]).read_bytes() == original

    # This is deliberately after the shim returned: conversion must consume
    # the raw runner capture, never re-open this mutable workspace manifest.
    manifest.write_bytes(b'{"replacement":true}\n')
    writer = runner.EvidenceWriter(prepared.control_root, "b" * 64)
    conversion = runner.convert_shim_captures(
        writer, control_root=prepared.control_root, run_id="b" * 64,
        execution_id="main", phase="main", transcript_id="transcript-main",
        trace=runner.ControlTraceWriter(prepared.control_root, "main"),
        control_pin=prepared.control_pin,
    )
    observation_id = conversion.observation_ids[0]
    manifest_id = conversion.manifest_ids[observation_id]
    manifest_entry = next(entry for entry in writer.entries if entry["id"] == manifest_id)
    assert manifest_entry["type"] == "wiki_manifest"
    capture = json.loads((writer.run_root / manifest_entry["relative_path"]).read_text())
    body_entry = next(entry for entry in writer.entries if entry["id"] == capture["content_id"])
    assert (writer.run_root / body_entry["relative_path"]).read_bytes() == original


def test_capture_conversion_rejects_an_unowned_or_wrong_phase_raw_index_row(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        index_path = Path(environ[runner.CONTROL_ENV]) / "brain-shim-capture-index.jsonl"
        row = json.loads(index_path.read_text().splitlines()[0])
        row["command_id"] = "foreign-command"
        row["phase"] = "foreign-phase"
        with index_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=completed.stderr,
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "shim_capture_conversion_failed" in outcome.log["incomplete_reasons"]


def test_capture_conversion_rejects_a_command_log_that_disagrees_with_its_index(
    tmp_path: Path,
) -> None:
    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        completed = subprocess.run(
            [str(cwd / "brain"), "--json", "sync"], cwd=cwd, env=environ,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        command_log = Path(environ[runner.CONTROL_ENV]) / "brain-shim-command-log.jsonl"
        row = json.loads(command_log.read_text().splitlines()[0])
        row["exit_code"] = 99
        command_log.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        return runner.ProcessCapture(
            exit_code=0, stdout=b'{"event":"pretend-pass"}\n', stderr=completed.stderr,
            version=b"0.153.0\n", help=b"JSONL\n",
        )

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert outcome.log["result"] == "incomplete"
    assert "shim_capture_conversion_failed" in outcome.log["incomplete_reasons"]


def test_web_runner_binds_fixture_capability_only_to_approved_capture_phase(
    tmp_path: Path,
) -> None:
    seen_configs: list[dict[str, object]] = []

    def fake_process(argv: list[str], cwd: Path, environ: dict[str, str], stdin: bytes) -> runner.ProcessCapture:
        config = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())
        seen_configs.append(config)
        if config["phase"] == "approval":
            return _canonical_web_approval_capture(cwd, environ)
        transcript = _claude_transcript([
            {
                "type": "text",
                "text": "Approved capture remains fixture-bound.\n",
            },
        ], cwd=str(cwd.resolve()))
        return runner.ProcessCapture(
            exit_code=0, stdout=transcript,
            stderr=b"",
            version=b"2.1.251\n", help=b"stream-json\n",
        )

    outcome = runner.run_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )

    assert [config["phase"] for config in seen_configs] == ["approval", "approved_capture"]
    # The accepted 7b shim requires a typed approval/fixture configuration
    # even during phase one; its phase gate, not malformed control, denies
    # every fixture operation before capture.
    assert seen_configs[0]["approval"] == runner._phase_approval(
        json.loads((ROOT / "tests/evals/scenarios/web-approval-and-capture.json").read_text())
    )
    assert seen_configs[0]["fixture"] is not None
    assert seen_configs[1]["fixture"] is not None
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    policies = {
        entry["id"]: json.loads((outcome.log_path.parent / entry["relative_path"]).read_text())
        for entry in index["entries"] if entry["type"] == "policy"
    }
    assert policies["policy-1"]["fixture_capability_id"] is None
    assert policies["policy-1"]["approval"] is None
    assert policies["policy-2"]["fixture_capability_id"] == "fixture-capability"
    assert policies["policy-2"]["approval"] == runner._phase_approval(
        json.loads((ROOT / "tests/evals/scenarios/web-approval-and-capture.json").read_text())
    ) | {"decision": "approved"}
    manifest_entry = next(entry for entry in index["entries"] if entry["type"] == "run_manifest")
    manifest = json.loads((outcome.log_path.parent / manifest_entry["relative_path"]).read_text())
    fixed_manifest = outcome.log_path.parent / "run-manifest.json"
    assert manifest_entry["relative_path"] == "run-manifest.json"
    assert fixed_manifest.read_bytes() == (
        outcome.log_path.parent / manifest_entry["relative_path"]
    ).read_bytes()
    assert manifest["phases"] == ["approval", "approved_capture"]
    assert manifest["fixture_capability_id"] == "fixture-capability"
    # The phase-one markers reached the planner, but omitted later scenario
    # markers leave an all-or-nothing incomplete plan rather than old generic
    # "normalization unavailable" wording or partial event claims.
    assert "semantic_event_normalization_unavailable" not in outcome.log["incomplete_reasons"]
    assert any(reason.startswith("semantic_missing_marker:") for reason in outcome.log["incomplete_reasons"])
    assert "event_record" not in {entry["type"] for entry in index["entries"]}


def test_web_plan_uses_two_separately_bound_phases() -> None:
    assert runner.phase_plan("web-approval-and-capture") == (
        "approval",
        "approved_capture",
    )
    assert runner.phase_plan("current-wiki-fast-path") == ("main",)


def test_web_approval_phase_has_no_fixture_capability_until_approved_capture(
    tmp_path: Path,
) -> None:
    prepared = runner.prepare_isolated_workspace(
        "web-approval-and-capture", scratch_root=tmp_path
    )
    approval = runner._phase_approval(prepared.scenario)
    assert approval is not None

    runner._phase_control(prepared, phase="approval", approval=approval)
    before = json.loads((prepared.control_root / "brain-shim.json").read_text())
    assert before["approval"] == approval
    assert before["fixture"] == {
        "descriptor_path": "fixture-descriptor.json",
        "static_shell_path": "static-shell.html",
        "rendered_dom_path": "rendered-dom.html",
    }
    assert (prepared.control_root / "static-shell.html").is_file()

    state = runner._write_runner_state(
        prepared, run_id="a" * 64, execution_id="execution-1", phase="approval"
    )
    rejected = subprocess.run(
        [
            str(prepared.workspace / "brain"), "--json", "eval", "mock-web-capture",
            "--fixture-id", "web-approval-and-capture.initial",
            "--approval-event-id", approval["event_id"],
            "--approval-scope", approval["scope"],
            "--approval-note", approval["note"],
        ],
        cwd=prepared.workspace,
        env={
            "PATH": "/usr/bin:/bin", "HOME": str(prepared.control_root / "home"),
            runner.CONTROL_ENV: str(prepared.control_root),
            runner.RUNNER_STATE_ENV: str(state.path),
        },
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    assert rejected.returncode == 2
    assert (prepared.control_root / "brain-shim-capture-index.jsonl").is_file()

    runner._phase_control(prepared, phase="approved_capture", approval=approval)
    after = json.loads((prepared.control_root / "brain-shim.json").read_text())
    assert after["approval"] == approval
    assert after["fixture"] == {
        "descriptor_path": "fixture-descriptor.json",
        "static_shell_path": "static-shell.html",
        "rendered_dom_path": "rendered-dom.html",
    }


def test_fixture_overlay_validator_rejects_unlisted_or_changed_archive_files(
    tmp_path: Path,
) -> None:
    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path
    )
    runner.validate_fixture_overlay(prepared.workspace, prepared.fixture)

    rogue = prepared.workspace / "sources/raw/rogue.txt"
    rogue.write_text("unlisted", encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="fixture overlay"):
        runner.validate_fixture_overlay(prepared.workspace, prepared.fixture)


def test_fixture_source_tree_rejects_an_unlisted_root_file_before_overlay(
    tmp_path: Path,
) -> None:
    fixture_root, fixture = runner._fixture(runner._scenario("current-wiki-fast-path"))
    copied = tmp_path / "fixture-repo"
    shutil.copytree(fixture_root / "repo", copied)
    (copied / "unexpected-root-file.txt").write_text("not in fixture manifest", encoding="utf-8")

    with pytest.raises(runner.RunnerError, match="fixture overlay"):
        runner.validate_fixture_source_tree(copied, fixture)


def test_private_git_initialization_ignores_inherited_global_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "host-template"
    (template / "hooks").mkdir(parents=True)
    (template / "hooks" / "evil-hook").write_text("host configuration", encoding="utf-8")
    global_config = tmp_path / "host-gitconfig"
    global_config.write_text(
        "[init]\n\ttemplateDir = " + str(template) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    prepared = runner.prepare_isolated_workspace(
        "current-wiki-fast-path", scratch_root=tmp_path
    )
    assert not (prepared.workspace / ".git/hooks/evil-hook").exists()


def test_private_git_binary_ignores_a_path_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "git").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake_bin))

    assert runner._runner_git_binary() == Path("/usr/bin/git")


def test_runner_never_discovers_or_launches_a_path_shadowed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH shadow cannot become a runner-owned executable candidate."""

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "touch fake-path-was-launched\n"
        "if [ \"$1\" = \"--version\" ]; then echo 0.153.0; exit 0; fi\n"
        "if [ \"$1\" = \"--help\" ]; then echo JSONL; exit 0; fi\n"
        "echo '{\"event\":\"fake\"}'\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    outcome = runner.run_scenario(
        client="codex",
        scenario_id="current-wiki-fast-path",
        client_command_json=json.dumps(_codex_template()),
        network_attestation="workspace-egress-denied",
        scratch_root=tmp_path,
    )

    assert outcome.log["result"] == "incomplete"
    assert "client_executable_unregistered" in outcome.log["incomplete_reasons"]
    assert outcome.trusted_context is None
    assert not (outcome.workspace / "fake-path-was-launched").exists()
    index = json.loads((outcome.log_path.parent / "evidence-index.json").read_text())
    assert {entry["type"] for entry in index["entries"]} == {"assertion", "diff"}


class _Task7dReadOnlyEvidenceWriter:
    """A sealed evidence snapshot that makes any gate-side write observable."""

    def __init__(self, outcome: runner.RunOutcome) -> None:
        self.run_id = outcome.log["run_id"]
        assert isinstance(self.run_id, str)
        self.control_root = outcome.control_root
        self.run_root = outcome.log_path.parent
        index = json.loads((self.run_root / "evidence-index.json").read_text())
        self.entries = tuple(MappingProxyType(dict(entry)) for entry in index["entries"])
        self.write_attempts = 0

    def add_bytes(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.write_attempts += 1
        raise AssertionError("phase-one receipt gate attempted a sealed writer addition")

    def add_json(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.write_attempts += 1
        raise AssertionError("phase-one receipt gate attempted a sealed writer addition")

    def inventory(self) -> tuple[tuple[str, str, str, int], ...]:
        return tuple(sorted(
            (
                str(entry["id"]), str(entry["type"]), str(entry["sha256"]),
                int(entry["bytes"]),
            )
            for entry in self.entries
        ))

    def clone_artifact_id_for_test(self, source_id: str, selected_id: str) -> None:
        """Expose a second sealed snapshot identity without invoking a writer.

        This is deliberately test-only snapshot shaping: both identities point
        at the same already-retained bytes, so a gate must obey conversion's
        explicit selection instead of a conventional command-derived name.
        """

        if any(entry["id"] == selected_id for entry in self.entries):
            raise AssertionError("duplicate test snapshot artifact id")
        source = next((entry for entry in self.entries if entry["id"] == source_id), None)
        if source is None:
            raise AssertionError("missing test snapshot artifact")
        copied = dict(source)
        copied["id"] = selected_id
        self.entries = (*self.entries, MappingProxyType(copied))

    def reseal_json_for_test(self, artifact_id: str, value: object) -> None:
        """Create a distinct test snapshot before the gate is allowed to read it."""

        raw = runner._canonical_json(value)
        rewritten = []
        found = False
        for entry in self.entries:
            updated = dict(entry)
            if entry["id"] == artifact_id:
                assert entry["type"] in {"process", "source_state", "consumption_receipt"}
                (self.run_root / str(entry["relative_path"])).write_bytes(raw)
                updated["sha256"] = hashlib.sha256(raw).hexdigest()
                updated["bytes"] = len(raw)
                found = True
            rewritten.append(MappingProxyType(updated))
        assert found
        self.entries = tuple(rewritten)


def _task7d_sealed_phase_one_receipt_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_kwargs: dict[str, object] | None = None,
    add_extra_command_after_sync: bool = False,
) -> tuple[
    _Task7dReadOnlyEvidenceWriter,
    runner.RegisteredLivePhase,
    runner.ShimCaptureConversion,
    dict[str, object],
    Path,
]:
    """Capture a genuine shim receipt, then expose only frozen indexed bytes."""

    conversions: list[runner.ShimCaptureConversion] = []
    original_conversion = runner.convert_shim_captures

    def record_conversion(*args: object, **kwargs: object) -> runner.ShimCaptureConversion:
        conversion = original_conversion(*args, **kwargs)
        if kwargs.get("execution_id") == "execution-1":
            conversions.append(conversion)
        return conversion

    def fake_process(
        _argv: list[str], cwd: Path, environ: dict[str, str], _stdin: bytes,
    ) -> runner.ProcessCapture:
        phase = json.loads((Path(environ[runner.CONTROL_ENV]) / "brain-shim.json").read_text())["phase"]
        assert phase == "approval"
        kwargs = {} if stream_kwargs is None else dict(stream_kwargs)
        if add_extra_command_after_sync:
            assert "after_sync" not in kwargs

            def add_extra_command(_control: Path) -> None:
                completed = subprocess.run(
                    [str(cwd / "brain"), "--json", "validate"],
                    cwd=cwd, env=environ, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                assert completed.returncode in {0, 1}, completed.stderr.decode()
                assert json.loads(completed.stdout)["ok"] is True

            kwargs["after_sync"] = add_extra_command
        return runner.ProcessCapture(
            exit_code=0,
            stdout=b"",
            stdout_chunks=_initial_sync_receipt_lifecycle_stream(cwd, environ, **kwargs),
            stderr=b"",
            version=b"2.1.251\n",
            help=b"stream-json\n",
        )

    monkeypatch.setattr(runner, "convert_shim_captures", record_conversion)
    # The legacy test-only overlay gate is intentionally outside this fixture:
    # it must not be the authority exercised by the new sealed reader.
    monkeypatch.setattr(runner, "_phase_one_web_capture_authorized", lambda **_kwargs: False)
    outcome = runner.run_scenario(
        client="claude",
        scenario_id="web-approval-and-capture",
        client_command_json=json.dumps(_claude_template()),
        network_attestation="mock-only",
        scratch_root=tmp_path,
        process_runner=fake_process,
    )
    assert outcome.log["result"] == "incomplete"
    assert conversions and len(conversions) == 1

    writer = _Task7dReadOnlyEvidenceWriter(outcome)
    entries = {str(entry["id"]): entry for entry in writer.entries}
    process = json.loads(
        (writer.run_root / str(entries["process-1"]["relative_path"])).read_text()
    )
    # The direct fixture uses the injected shim only to obtain real command
    # captures.  Shape its sealed process row like the actual registered
    # Claude phase so the gate exercises the closed live-process schema.
    process["executable_id"] = "client-executable-1"
    process["mcp_identity"] = {"fixture": "sealed-phase-one"}
    writer.reseal_json_for_test("process-1", process)
    entries = {str(entry["id"]): entry for entry in writer.entries}
    process = json.loads(
        (writer.run_root / str(entries["process-1"]["relative_path"])).read_text()
    )
    transcript_id = process["transcript_id"]
    transcript = (writer.run_root / str(entries[transcript_id]["relative_path"])).read_bytes()
    stderr = (writer.run_root / str(entries["stderr-1"]["relative_path"])).read_bytes()
    version = (writer.run_root / str(entries["version-1"]["relative_path"])).read_bytes()
    help_output = (writer.run_root / str(entries["help-1"]["relative_path"])).read_bytes()
    phase_one = runner.RegisteredLivePhase(
        capture=runner.ProcessCapture(
            exit_code=process["exit_code"], stdout=transcript, stderr=stderr,
            version=version, help=help_output,
        ),
        trusted_executable=object(),
        trusted_context=object(),
        control_pin=runner.PinnedDirectory.pin(outcome.control_root, private=True),
        workspace_pin=runner.PinnedDirectory.pin(outcome.workspace, private=False),
        executable_id="client-executable-1",
        version_id="version-1",
        help_id="help-1",
        argv=tuple(process["argv"]),
        reported_version=process["client_version"],
        native_format=process["native_format"],
        supported_build_id="test-build",
        launch_identity=MappingProxyType({}),
        launch_sha256="0" * 64,
        seal_identity=MappingProxyType({}),
        seal_sha256="0" * 64,
    )
    return writer, phase_one, conversions[0], runner._scenario("web-approval-and-capture"), outcome.workspace


def test_actual_phase_one_receipt_gate_reconstructs_only_from_sealed_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: phase-two authority could depend on a private overlay or write."""

    from tests.evals import event_log_contract as contract

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    before = writer.inventory()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sealed receipt gate reached a forbidden private semantic path")

    monkeypatch.setattr(runner, "_private_semantic_overlay", forbidden)
    monkeypatch.setattr(runner, "plan_semantic_events", forbidden)
    monkeypatch.setattr(runner, "_indexed_semantic_replay", forbidden)
    monkeypatch.setattr(contract, "_validate_marker", forbidden)
    monkeypatch.setattr(contract, "_validate_event_record", forbidden)
    monkeypatch.setattr(contract, "_validate_receipts", forbidden)
    monkeypatch.setattr(contract, "_audit_commands", forbidden)
    # The narrow receipt reader has no phase-two authority.  These sentinels
    # make an accidental pre-gate preparation/probe/spawn visible even though
    # this direct fixture supplies no phase-two input at all.
    monkeypatch.setattr(runner, "_prepare_registered_live_executable", forbidden)
    monkeypatch.setattr(runner, "_registered_live_trusted_context", forbidden)
    monkeypatch.setattr(runner, "_registered_probe", forbidden)
    monkeypatch.setattr(runner, "_launch_registered_live_phase", forbidden)
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden)

    gate = runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    )

    assert isinstance(gate, runner.PhaseOneReceiptGate)
    assert gate.execution_id == "execution-1"
    assert gate.receipt_facts.selection.command_observation_ids == conversion.observation_ids
    assert gate.acknowledgement_position < gate.report_gap_position < gate.approval_position
    assert writer.write_attempts == 0
    assert writer.inventory() == before


def test_actual_phase_one_receipt_gate_uses_conversion_selected_renamed_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: the gate could recreate conventional command-ID artifact names."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    verify_command, _consume_command, durable_command, _acknowledge_command = conversion.observation_ids
    receipt_artifacts = {
        command_id: dict(artifacts)
        for command_id, artifacts in conversion.receipt_artifact_ids.items()
    }
    conventional_stream = receipt_artifacts[verify_command]["stream"]
    conventional_durable = receipt_artifacts[durable_command]["durable"]
    renamed_stream = "selected-phase-one-stream"
    renamed_durable = "selected-phase-one-durable"
    assert conventional_stream == verify_command + "-stream"
    assert conventional_durable == durable_command + "-durable"
    # Keep the conventional IDs present as plausible-but-unselected decoys.
    # The phase gate must use the immutable conversion mapping rather than
    # deriving ``{command_id}-stream``/``{command_id}-durable`` itself.
    writer.clone_artifact_id_for_test(conventional_stream, renamed_stream)
    writer.clone_artifact_id_for_test(conventional_durable, renamed_durable)
    receipt_artifacts[verify_command]["stream"] = renamed_stream
    receipt_artifacts[durable_command]["durable"] = renamed_durable
    conversion = replace(conversion, receipt_artifact_ids=receipt_artifacts)

    gate = runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    )

    assert isinstance(gate, runner.PhaseOneReceiptGate)
    assert gate.receipt_facts.selection.stream_artifact_id == renamed_stream
    assert gate.receipt_facts.selection.durable_artifact_id == renamed_durable
    assert any(entry["id"] == conventional_stream for entry in writer.entries)
    assert any(entry["id"] == conventional_durable for entry in writer.entries)
    assert writer.write_attempts == 0


def test_shim_capture_conversion_rejects_mutable_or_ambiguous_observation_ids() -> None:
    """Break caught: a mutable conversion could detach gate selection from capture order."""

    kwargs = {
        "manifest_ids": {},
        "trace_id": "trace-execution-1",
        "trace_sha256": "a" * 64,
        "raw_index": b"index\n",
        "raw_command_log": b"commands\n",
        "raw_preapply_manifests": b"",
    }
    with pytest.raises(runner.RunnerError, match="receipt observation"):
        runner.ShimCaptureConversion(observation_ids=["verify"], **kwargs)  # type: ignore[arg-type]
    with pytest.raises(runner.RunnerError, match="receipt observation"):
        runner.ShimCaptureConversion(observation_ids=("verify", "verify"), **kwargs)
    with pytest.raises(runner.RunnerError, match="receipt artifact mapping"):
        runner.ShimCaptureConversion(
            observation_ids=("verify",), receipt_artifact_ids=object(), **kwargs,  # type: ignore[arg-type]
        )
    selected = {"verify": {"stream": "selected-stream"}}
    conversion = runner.ShimCaptureConversion(
        observation_ids=("verify",), receipt_artifact_ids=selected, **kwargs,
    )
    selected["verify"]["stream"] = "mutated-after-capture"
    assert conversion.receipt_artifact_ids["verify"]["stream"] == "selected-stream"
    with pytest.raises(TypeError):
        conversion.receipt_artifact_ids["verify"]["stream"] = "forged"  # type: ignore[index]


def test_shim_capture_conversion_freezes_manifest_mapping() -> None:
    """Break caught: a frozen diagnostic fact could expose a mutable conversion map."""

    manifests = {"verify": "verify-wiki-manifest"}
    conversion = runner.ShimCaptureConversion(
        observation_ids=("verify",),
        manifest_ids=manifests,
        trace_id="trace-execution-1",
        trace_sha256="a" * 64,
        raw_index=b"index\n",
        raw_command_log=b"commands\n",
        raw_preapply_manifests=b"",
    )
    manifests["verify"] = "forged-manifest"

    assert conversion.manifest_ids["verify"] == "verify-wiki-manifest"
    with pytest.raises(TypeError):
        conversion.manifest_ids["verify"] = "forged-again"  # type: ignore[index]


def test_phase_one_receipt_gate_rejects_forged_facts_and_retains_typed_selection() -> None:
    """Break caught: an arbitrary object could masquerade as sealed receipt authority."""

    from tests.evals import event_log_contract as contract

    selection = contract.InitialSyncReceiptSelection(
        verify_command_id="verify",
        consume_command_id="consume",
        durable_command_id="durable",
        acknowledge_command_id="acknowledge",
        stream_artifact_id="stream",
        durable_artifact_id="durable-receipt",
    )
    facts = contract.InitialSyncReceiptFacts(
        result_id="sync_" + "a" * 64,
        corpus_revision="b" * 64,
        event_counts={},
        effect_digest="c" * 64,
        selection=selection,
    )
    gate = runner.PhaseOneReceiptGate(
        execution_id="execution-1",
        receipt_facts=facts,
        acknowledgement_position=(0, 1, 0, 0),
        report_gap_position=(0, 2, 0, 0),
        approval_position=(0, 3, 0, 0),
    )
    assert gate.selection is selection
    with pytest.raises(runner.RunnerError, match="receipt gate facts"):
        runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=SimpleNamespace(selection=selection),
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )


def test_actual_phase_one_receipt_gate_rejects_an_extra_resealed_process_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a permissive process decoder could admit an unknown authority field."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    process_entry = next(entry for entry in writer.entries if entry["id"] == "process-1")
    process = json.loads((writer.run_root / str(process_entry["relative_path"])).read_text())
    process["unexpected_process_authority"] = "not accepted"
    writer.reseal_json_for_test("process-1", process)

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_one_receipt_gate_rejects_a_tampered_selected_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a shape-valid selected durable receipt could drift from sync facts."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    durable_command = conversion.observation_ids[2]
    durable_id = conversion.receipt_artifact_ids[durable_command]["durable"]
    entry = next(entry for entry in writer.entries if entry["id"] == durable_id)
    durable = json.loads((writer.run_root / str(entry["relative_path"])).read_text())
    durable["effect_digest"] = "0" * 64
    writer.reseal_json_for_test(durable_id, durable)

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_one_receipt_gate_rejects_a_tampered_receipt_source_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a source-state row must remain tied to the sealed sync result."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    verify_command = conversion.observation_ids[0]
    state_id = verify_command + "-source-state"
    entry = next(entry for entry in writer.entries if entry["id"] == state_id)
    source_state = json.loads((writer.run_root / str(entry["relative_path"])).read_text())
    source_state["result_id"] = "sync_" + "0" * 64
    writer.reseal_json_for_test(state_id, source_state)

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


@pytest.mark.parametrize(
    "stream_kwargs",
    [
        {"approval_code_context": "fenced"},
        {"approval_code_context": "multiline_inline"},
        {"separate_approval_blocks": True, "approval_before_report": True},
    ],
    ids=("quoted-fenced", "quoted-inline", "approval-before-report"),
)
def test_actual_phase_one_receipt_gate_rejects_quoted_or_out_of_order_approval_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_kwargs: dict[str, object],
) -> None:
    """Break caught: exact parser candidates and canonical order gate phase two."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch, stream_kwargs=stream_kwargs,
    )

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_one_receipt_gate_rejects_an_extra_phase_one_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: a valid lifecycle cannot hide another phase-one command."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch, add_extra_command_after_sync=True,
    )
    assert len(conversion.observation_ids) == 5

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_one_receipt_gate_rejects_extra_sealed_command_after_narrowing_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: conversion selection cannot hide a retained extra command row."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch, add_extra_command_after_sync=True,
    )
    assert len(conversion.observation_ids) == 5
    extra_id = next(
        command_id
        for command_id in conversion.observation_ids
        if json.loads((writer.run_root / str(next(
            entry["relative_path"] for entry in writer.entries if entry["id"] == command_id
        ))).read_text())["argv"][2:] == ["validate"]
    )
    selected_ids = tuple(command_id for command_id in conversion.observation_ids if command_id != extra_id)
    narrowed = replace(
        conversion,
        observation_ids=selected_ids,
        receipt_artifact_ids={
            command_id: dict(artifacts)
            for command_id, artifacts in conversion.receipt_artifact_ids.items()
            if command_id in selected_ids
        },
    )
    assert len(narrowed.observation_ids) == 4

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=phase_one,
        scenario=scenario,
        conversion=narrowed,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_one_receipt_gate_rejects_a_registered_phase_from_another_control_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: receipt facts cannot be paired with an unrelated private pin."""

    writer, phase_one, conversion, scenario, workspace = _task7d_sealed_phase_one_receipt_fixture(
        tmp_path, monkeypatch,
    )
    detached_phase = replace(
        phase_one,
        control_pin=runner.PinnedDirectory.pin(writer.run_root, private=True),
    )

    assert runner._reconstruct_actual_phase_one_receipt_gate(
        writer=writer,
        phase_one=detached_phase,
        scenario=scenario,
        conversion=conversion,
        workspace=workspace,
        execution_id="execution-1",
    ) is None
    assert writer.write_attempts == 0


def test_actual_phase_two_setup_requires_a_sealed_phase_one_receipt_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: phase-two setup could begin without sealed phase-one approval."""

    prepared_phase, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    prepared_run = prepared_phase.prepared_run
    writer = prepared_run.writer
    try:
        diagnostic = runner._index_registered_actual_phase_one_diagnostic(
            prepared_phase, live_phase,
        )
        from tests.evals import event_log_contract as contract

        verify_id, consume_id, durable_id, acknowledge_id = diagnostic.conversion.observation_ids
        receipt_gate = runner.PhaseOneReceiptGate(
            execution_id="execution-1",
            receipt_facts=contract.InitialSyncReceiptFacts(
                result_id="sync_" + "a" * 64,
                corpus_revision="b" * 64,
                event_counts={},
                effect_digest="c" * 64,
                selection=contract.InitialSyncReceiptSelection(
                    verify_command_id=verify_id,
                    consume_command_id=consume_id,
                    durable_command_id=durable_id,
                    acknowledge_command_id=acknowledge_id,
                    stream_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        verify_id
                    ]["stream"],
                    durable_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                        durable_id
                    ]["durable"],
                ),
            ),
            acknowledgement_position=(0, 1, 0, 0),
            report_gap_position=(0, 2, 0, 0),
            approval_position=(0, 3, 0, 0),
        )
        sealed_reader_calls: list[dict[str, object]] = []

        def sealed_reader(**kwargs: object) -> runner.PhaseOneReceiptGate:
            sealed_reader_calls.append(dict(kwargs))
            return receipt_gate

        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)

        launches: list[object] = []

        def no_launch(*_args: object, **_kwargs: object) -> object:
            launches.append("client")
            pytest.fail("phase-two setup launched a client")

        monkeypatch.setattr(runner, "_launch_registered_live_phase", no_launch)
        monkeypatch.setattr(runner, "_launch_actual_registered_live_phase", no_launch)
        monkeypatch.setattr(runner.subprocess, "Popen", no_launch)

        signature = inspect.signature(runner._prepare_registered_actual_phase_two)
        assert tuple(signature.parameters) == (
            "prepared_run", "phase_one_diagnostic", "receipt_gate",
        )
        before_entries = list(writer.entries)
        control_paths = (
            prepared_phase.phase_control.config.path,
            prepared_phase.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}
        with pytest.raises(runner.RunnerError):
            runner._prepare_registered_actual_phase_two(prepared_run, diagnostic, None)
        assert writer.entries == before_entries
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert sealed_reader_calls == []

        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        expected_reader_call = {
            "writer": writer,
            "phase_one": diagnostic.live_phase,
            "scenario": prepared_run.prepared.scenario,
            "conversion": diagnostic.conversion,
            "workspace": prepared_run.prepared.workspace,
            "execution_id": "execution-1",
        }
        assert type(phase_two) is runner._RegisteredActualPreparedPhase
        # Setup replays the gate before and after snapshot hydration.  Its
        # final no-write launch gate then reconstructs twice more before a
        # prepared token can escape.  Those final rechecks must use the
        # private launch descriptor rather than the pre-transition facade.
        assert sealed_reader_calls[:2] == [expected_reader_call] * 2
        assert len(sealed_reader_calls) == 4
        for call in sealed_reader_calls[2:]:
            assert {
                key: value
                for key, value in call.items()
                if key not in {"control_pin", "phase_one", "conversion"}
            } == {
                key: value
                for key, value in expected_reader_call.items()
                if key not in {"phase_one", "conversion"}
            }
            private_control_pin = call["control_pin"]
            private_phase_one = call["phase_one"]
            private_conversion = call["conversion"]
            assert type(private_control_pin) is runner.PinnedDirectory
            assert private_control_pin is not phase_two.control_pin
            assert private_control_pin.private is True
            assert private_control_pin.path == phase_two.control_pin.path
            assert type(private_phase_one) is runner.RegisteredLivePhase
            assert private_phase_one is not diagnostic.live_phase
            assert private_phase_one.control_pin is private_control_pin
            assert private_phase_one.workspace_pin is not phase_two.workspace_pin
            assert private_phase_one.workspace_pin.path == phase_two.workspace_pin.path
            assert private_phase_one.executable_id == diagnostic.live_phase.executable_id
            assert type(private_conversion) is runner.ShimCaptureConversion
            assert private_conversion is not diagnostic.conversion
            assert private_conversion.raw_index == diagnostic.conversion.raw_index
        assert phase_two.prepared_run is prepared_run
        assert phase_two.authority is prepared_run.authority
        assert phase_two.writer is writer
        assert (phase_two.execution_id, phase_two.phase_index, phase_two.phase) == (
            "execution-2", 2, "approved_capture",
        )
        assert phase_two.trusted_context is not None
        assert phase_two.trusted_context is not diagnostic.live_phase.trusted_context
        assert set(phase_two.trusted_context.trusted_executables) == {
            "execution-1", "execution-2",
        }
        assert phase_two.trusted_context.trusted_executables["execution-1"] is (
            diagnostic.live_phase.trusted_executable
        )
        assert phase_two.trusted_context.trusted_executables["execution-2"] is not (
            diagnostic.live_phase.trusted_executable
        )
        assert phase_two.trusted_context.trusted_executables["execution-2"].execution_id == (
            "execution-2"
        )
        assert launches == []
    finally:
        prepared_phase.control_pin.close()
        prepared_phase.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_receipt_gate_after_snapshot_hydration_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid-shaped gate mutation after hydration cannot rewrite phase-two control."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_snapshots = runner._sealed_phase_one_reusable_snapshots
    original_facts = receipt_gate.receipt_facts
    replacement_effect_digest = (
        "0" * 64 if original_facts.effect_digest != "0" * 64 else "f" * 64
    )
    replacement_facts = replace(
        original_facts, effect_digest=replacement_effect_digest,
    )

    try:
        assert replacement_facts.selection is original_facts.selection
        assert replacement_facts.effect_digest != original_facts.effect_digest
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        control_paths = (
            phase_one.phase_control.config.path,
            phase_one.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}
        hydration_calls: list[tuple[object, object, object, object]] = []

        def hydrate_then_mutate_gate(
            observed_writer: runner.EvidenceWriter,
            observed_phase_one: runner._RegisteredActualPreparedPhase,
            *,
            control_pin: runner.PinnedDirectory,
            trace_id: str,
        ) -> tuple[runner.WorkspaceSnapshot, ...]:
            assert observed_writer is writer
            assert observed_phase_one is phase_one
            assert control_pin is phase_one.control_pin
            assert trace_id == diagnostic.trace_id
            assert writer.entries == before_entries
            assert writer.write_identities == before_identities
            assert _task7d_run_tree_bytes(writer) == before_tree
            reusable = original_snapshots(
                observed_writer,
                observed_phase_one,
                control_pin=control_pin,
                trace_id=trace_id,
            )
            assert writer.entries == before_entries
            assert writer.write_identities == before_identities
            assert _task7d_run_tree_bytes(writer) == before_tree
            object.__setattr__(receipt_gate, "receipt_facts", replacement_facts)
            assert receipt_gate.__post_init__() is None
            hydration_calls.append((
                observed_writer, observed_phase_one, control_pin, trace_id,
            ))
            return reusable

        monkeypatch.setattr(
            runner, "_sealed_phase_one_reusable_snapshots", hydrate_then_mutate_gate,
        )
        with pytest.raises(runner.RunnerError, match="receipt gate is no longer valid"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert hydration_calls == [(
            writer, phase_one, phase_one.control_pin, diagnostic.trace_id,
        )]
        assert receipt_gate.receipt_facts is replacement_facts
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(receipt_gate, "receipt_facts", original_facts)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    ("control_name", "control_path"),
    (
        ("runner-state", lambda phase_one: phase_one.runner_state.path),
        ("brain-shim", lambda phase_one: phase_one.phase_control.config.path),
    ),
)
def test_actual_phase_two_setup_rechecks_phase_one_control_identity_after_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_name: str,
    control_path: Callable[[runner._RegisteredActualPreparedPhase], Path],
) -> None:
    """A same-byte phase-one control substitution cannot reach phase-two writes."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_snapshots = runner._sealed_phase_one_reusable_snapshots
    target = control_path(phase_one)
    target_raw = target.read_bytes()
    target_inode = target.stat().st_ino

    try:
        assert target.name in {"brain-shim.json", "runner-state.json"}
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        hydration_calls: list[str] = []

        def hydrate_then_replace_control(
            observed_writer: runner.EvidenceWriter,
            observed_phase_one: runner._RegisteredActualPreparedPhase,
            *,
            control_pin: runner.PinnedDirectory,
            trace_id: str,
        ) -> tuple[runner.WorkspaceSnapshot, ...]:
            reusable = original_snapshots(
                observed_writer,
                observed_phase_one,
                control_pin=control_pin,
                trace_id=trace_id,
            )
            replacement = target.with_name(target.name + ".same-byte-replacement")
            replacement.write_bytes(target_raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            assert target.read_bytes() == target_raw
            assert target.stat().st_ino != target_inode
            hydration_calls.append(control_name)
            return reusable

        monkeypatch.setattr(
            runner, "_sealed_phase_one_reusable_snapshots", hydrate_then_replace_control,
        )
        with pytest.raises(runner.RunnerError, match="control file changed|phase-two binding"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert hydration_calls == [control_name]
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_prepared_control_root_after_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached prepared control root cannot receive phase-two setup output."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer
    original_snapshots = runner._sealed_phase_one_reusable_snapshots
    original_control_root = prepared.control_root
    foreign_control_root = tmp_path / "foreign-phase-two-control-root"
    foreign_control_root.mkdir()

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        hydration_calls: list[str] = []

        def hydrate_then_detach_control_root(
            observed_writer: runner.EvidenceWriter,
            observed_phase_one: runner._RegisteredActualPreparedPhase,
            *,
            control_pin: runner.PinnedDirectory,
            trace_id: str,
        ) -> tuple[runner.WorkspaceSnapshot, ...]:
            reusable = original_snapshots(
                observed_writer,
                observed_phase_one,
                control_pin=control_pin,
                trace_id=trace_id,
            )
            object.__setattr__(prepared, "control_root", foreign_control_root)
            hydration_calls.append("detached")
            return reusable

        monkeypatch.setattr(
            runner, "_sealed_phase_one_reusable_snapshots", hydrate_then_detach_control_root,
        )
        with pytest.raises(runner.RunnerError, match="phase-two binding"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert hydration_calls == ["detached"]
        assert prepared.control_root == foreign_control_root
        assert list(foreign_control_root.iterdir()) == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(prepared, "control_root", original_control_root)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_plan_client_after_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plausible post-hydration client swap cannot seed phase-two controls."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    plan = prepared_run.authority.plan
    original_snapshots = runner._sealed_phase_one_reusable_snapshots
    original_client = plan.client
    original_template = plan.symbolic_template
    original_network_attestation = plan.network_attestation
    original_registration = plan.registration
    replacement_client = "codex" if original_client != "codex" else "claude"
    replacement_template, replacement_network_attestation = (
        runner._runner_owned_symbolic_profile(replacement_client)
    )
    replacement_registration = runner.RunnerOwnedExecutableRegistration(
        client=replacement_client,
        resolved_path=original_registration.resolved_path,
        expected_version=original_registration.expected_version,
        native_format="test-only-" + replacement_client + "-native-json",
    )
    replacement_registrations = dict(runner._RUNNER_OWNED_EXECUTABLES)
    replacement_registrations[replacement_client] = replacement_registration

    try:
        assert replacement_client != original_client
        monkeypatch.setattr(
            runner,
            "_RUNNER_OWNED_EXECUTABLES",
            MappingProxyType(replacement_registrations),
        )
        assert runner._runner_owned_registration(replacement_client) is replacement_registration
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        hydration_calls: list[str] = []

        def hydrate_then_swap_client(
            observed_writer: runner.EvidenceWriter,
            observed_phase_one: runner._RegisteredActualPreparedPhase,
            *,
            control_pin: runner.PinnedDirectory,
            trace_id: str,
        ) -> tuple[runner.WorkspaceSnapshot, ...]:
            reusable = original_snapshots(
                observed_writer,
                observed_phase_one,
                control_pin=control_pin,
                trace_id=trace_id,
            )
            object.__setattr__(plan, "client", replacement_client)
            object.__setattr__(plan, "symbolic_template", replacement_template)
            object.__setattr__(plan, "network_attestation", replacement_network_attestation)
            object.__setattr__(plan, "registration", replacement_registration)
            hydration_calls.append(replacement_client)
            return reusable

        monkeypatch.setattr(
            runner, "_sealed_phase_one_reusable_snapshots", hydrate_then_swap_client,
        )
        with pytest.raises(runner.RunnerError, match="phase-two binding"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert hydration_calls == [replacement_client]
        assert plan.client == replacement_client
        assert plan.symbolic_template == replacement_template
        assert plan.network_attestation == replacement_network_attestation
        assert plan.registration is replacement_registration
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(plan, "client", original_client)
        object.__setattr__(plan, "symbolic_template", original_template)
        object.__setattr__(plan, "network_attestation", original_network_attestation)
        object.__setattr__(plan, "registration", original_registration)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_phase_control_after_helper_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced returned phase-two config cannot reach state or evidence writes."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_phase_control = runner._phase_control
    phase_one_state = phase_one.runner_state.path
    phase_one_state_raw = phase_one_state.read_bytes()

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        replacement_records: list[tuple[Path, int, int, bytes]] = []

        def phase_control_then_replace(
            *args: object,
            **kwargs: object,
        ) -> runner.PhaseControlSeals:
            seals = original_phase_control(*args, **kwargs)  # type: ignore[arg-type]
            assert kwargs["phase"] == "approved_capture"
            target = seals.config.path
            raw = target.read_bytes()
            before_inode = target.stat().st_ino
            replacement = target.with_name(target.name + ".phase-control-same-byte-replacement")
            replacement.write_bytes(raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            after_inode = target.stat().st_ino
            assert target.read_bytes() == raw
            assert after_inode != before_inode
            replacement_records.append((target, before_inode, after_inode, raw))
            return seals

        monkeypatch.setattr(runner, "_phase_control", phase_control_then_replace)
        with pytest.raises(runner.RunnerError, match="control file changed|setup continuation"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert len(replacement_records) == 1
        target, before_inode, after_inode, raw = replacement_records[0]
        assert target.name == "brain-shim.json"
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        # This prior control survives phase-control replacement and therefore
        # proves the new state writer never ran after the returned config was
        # substituted.
        assert phase_one_state.read_bytes() == phase_one_state_raw
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_runner_state_after_helper_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced returned phase-two state cannot reach MCP, prompt, or snapshot writes."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_write_runner_state = runner._write_runner_state

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        replacement_records: list[tuple[Path, int, int, bytes]] = []

        def write_state_then_replace(
            *args: object,
            **kwargs: object,
        ) -> runner.ControlFileSeal:
            seal = original_write_runner_state(*args, **kwargs)  # type: ignore[arg-type]
            assert kwargs["execution_id"] == "execution-2"
            assert kwargs["phase"] == "approved_capture"
            target = seal.path
            raw = target.read_bytes()
            before_inode = target.stat().st_ino
            replacement = target.with_name(target.name + ".runner-state-same-byte-replacement")
            replacement.write_bytes(raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            after_inode = target.stat().st_ino
            assert target.read_bytes() == raw
            assert after_inode != before_inode
            replacement_records.append((target, before_inode, after_inode, raw))
            return seal

        monkeypatch.setattr(runner, "_write_runner_state", write_state_then_replace)
        with pytest.raises(runner.RunnerError, match="control file changed|setup continuation"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert len(replacement_records) == 1
        target, before_inode, after_inode, raw = replacement_records[0]
        assert target.name == "runner-state.json"
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_initial_snapshot_after_capture_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced initial snapshot cannot become a returned phase-two token."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_capture = runner.WorkspaceSnapshotter.capture

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        replacement_records: list[tuple[str, Path, int, int, bytes]] = []

        def capture_then_replace_snapshot(
            snapshotter: runner.WorkspaceSnapshotter,
            *args: object,
            **kwargs: object,
        ) -> runner.WorkspaceSnapshot:
            snapshot = original_capture(snapshotter, *args, **kwargs)  # type: ignore[arg-type]
            if (kwargs.get("execution_id") == "execution-2"
                    and kwargs.get("trace_sequence") == 0):
                assert snapshot.artifact_id == "workspace-snapshot-execution-2-initial-0"
                entry = next(
                    item for item in writer.entries if item["id"] == snapshot.artifact_id
                )
                target = writer.run_root / str(entry["relative_path"])
                raw = target.read_bytes()
                before_inode = target.stat().st_ino
                replacement = target.with_name(target.name + ".snapshot-same-byte-replacement")
                replacement.write_bytes(raw)
                replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                os.replace(replacement, target)
                after_inode = target.stat().st_ino
                assert target.read_bytes() == raw
                assert after_inode != before_inode
                replacement_records.append((
                    snapshot.artifact_id, target, before_inode, after_inode, raw,
                ))
            return snapshot

        monkeypatch.setattr(
            runner.WorkspaceSnapshotter,
            "capture",
            capture_then_replace_snapshot,
        )
        returned_phase_two: list[object] = []
        with pytest.raises(runner.RunnerError):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(replacement_records) == 1
        artifact_id, target, before_inode, after_inode, raw = replacement_records[0]
        assert artifact_id == "workspace-snapshot-execution-2-initial-0"
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        entry = next(item for item in writer.entries if item["id"] == artifact_id)
        retained_identity = writer.write_identities[str(entry["relative_path"])]
        assert runner.PinnedDirectory._regular_identity(target.lstat()) != retained_identity
        assert [dict(entry) for entry in writer.entries][:len(before_entries)] == before_entries
        assert artifact_id in {str(entry["id"]) for entry in writer.entries}
        assert "client-executable-2" not in {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_raw_predecessor_after_gate_reader_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase-one raw substitution after the reader cannot reach phase-two control."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_reader = runner._reconstruct_actual_phase_one_receipt_gate
    raw_target = writer.control_root / "brain-shim-capture-index.jsonl"
    raw_before = raw_target.read_bytes()
    raw_inode_before = raw_target.stat().st_ino

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        control_paths = (
            phase_one.phase_control.config.path,
            phase_one.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}
        reader_calls: list[dict[str, object]] = []
        replacements: list[tuple[int, int]] = []

        def reader_then_replace_raw(
            *args: object,
            **kwargs: object,
        ) -> runner.PhaseOneReceiptGate | None:
            gate = original_reader(*args, **kwargs)  # type: ignore[arg-type]
            reader_calls.append(dict(kwargs))
            if len(reader_calls) == 2:
                replacement = raw_target.with_name(
                    raw_target.name + ".receipt-reader-same-byte-replacement",
                )
                replacement.write_bytes(raw_before)
                replacement.chmod(stat.S_IMODE(raw_target.stat().st_mode))
                os.replace(replacement, raw_target)
                raw_inode_after = raw_target.stat().st_ino
                assert raw_target.read_bytes() == raw_before
                assert raw_inode_after != raw_inode_before
                replacements.append((raw_inode_before, raw_inode_after))
            return gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", reader_then_replace_raw,
        )
        with pytest.raises(runner.RunnerError):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert len(reader_calls) == 2
        assert replacements and replacements[0][0] != replacements[0][1]
        assert raw_target.read_bytes() == raw_before
        assert raw_target.stat().st_ino != raw_inode_before
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rejects_replaced_raw_predecessor_baseline_after_hydration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged raw-baseline facade cannot match the pre-hydration primitive tuple."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    predecessors = diagnostic.raw_predecessors
    original_values = predecessors.values
    original_snapshots = runner._sealed_phase_one_reusable_snapshots
    raw_relative = "brain-shim-capture-index.jsonl"
    raw_target = writer.control_root / raw_relative
    raw_before = raw_target.read_bytes()
    raw_inode_before = raw_target.stat().st_ino

    try:
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        control_paths = (
            phase_one.phase_control.config.path,
            phase_one.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}
        forged_baselines: list[runner._PrivateRawFileBaseline] = []

        def hydrate_then_replace_raw_and_baseline(
            observed_writer: runner.EvidenceWriter,
            observed_phase_one: runner._RegisteredActualPreparedPhase,
            *,
            control_pin: runner.PinnedDirectory,
            trace_id: str,
        ) -> tuple[runner.WorkspaceSnapshot, ...]:
            reusable = original_snapshots(
                observed_writer,
                observed_phase_one,
                control_pin=control_pin,
                trace_id=trace_id,
            )
            replacement = raw_target.with_name(
                raw_target.name + ".forged-baseline-same-byte-replacement",
            )
            replacement.write_bytes(raw_before)
            replacement.chmod(stat.S_IMODE(raw_target.stat().st_mode))
            os.replace(replacement, raw_target)
            raw_inode_after = raw_target.stat().st_ino
            assert raw_target.read_bytes() == raw_before
            assert raw_inode_after != raw_inode_before
            forged = runner._PrivateRawFileBaseline(
                raw_relative,
                hashlib.sha256(raw_before).hexdigest(),
                len(raw_before),
                runner.PinnedDirectory._regular_identity(raw_target.lstat()),
                raw_before.count(b"\n"),
            )
            replacement_values = dict(original_values)
            replacement_values[raw_relative] = forged
            object.__setattr__(
                predecessors,
                "values",
                MappingProxyType(replacement_values),
            )
            assert predecessors.__post_init__() is None
            forged_baselines.append(forged)
            return reusable

        monkeypatch.setattr(
            runner,
            "_sealed_phase_one_reusable_snapshots",
            hydrate_then_replace_raw_and_baseline,
        )
        with pytest.raises(runner.RunnerError, match="raw predecessor baseline changed"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert len(forged_baselines) == 1
        assert predecessors.values[raw_relative] is forged_baselines[0]
        assert raw_target.read_bytes() == raw_before
        assert raw_target.stat().st_ino != raw_inode_before
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert not {
            "mcp-2", "phase-prompt-2", "workspace-snapshot-execution-2-initial-0",
        } & {str(entry["id"]) for entry in writer.entries}
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(predecessors, "values", original_values)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_root_control_after_launch_tree_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-tree root-control replacement cannot escape with a setup token."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_tree_is_open = runner._registered_actual_phase_two_launch_tree_is_open

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        replacements: list[tuple[Path, int, int, bytes]] = []
        entries_after_tree: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def tree_then_replace_root_control(
            observed_phase: runner._RegisteredActualPreparedPhase,
            *,
            pre_popen: bool,
            control_pin: runner.PinnedDirectory | None = None,
        ) -> None:
            original_tree_is_open(
                observed_phase, pre_popen=pre_popen, control_pin=control_pin,
            )
            assert pre_popen is False
            target = observed_phase.phase_control.config.path
            raw = target.read_bytes()
            before_inode = target.stat().st_ino
            replacement = target.with_name(
                target.name + ".post-launch-tree-same-byte-replacement",
            )
            replacement.write_bytes(raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            after_inode = target.stat().st_ino
            assert target.read_bytes() == raw
            assert after_inode != before_inode
            replacements.append((target, before_inode, after_inode, raw))
            entries_after_tree.append([dict(entry) for entry in writer.entries])

        monkeypatch.setattr(
            runner,
            "_registered_actual_phase_two_launch_tree_is_open",
            tree_then_replace_root_control,
        )
        with pytest.raises(runner.RunnerError) as error:
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(replacements) == len(entries_after_tree) == 1, str(error.value)
        target, before_inode, after_inode, raw = replacements[0]
        assert target.name == "brain-shim.json"
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        assert writer.entries == entries_after_tree[0]
        assert "client-executable-2" not in {
            str(entry["id"]) for entry in writer.entries
        }
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_rechecks_initial_snapshot_after_launch_tree_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-tree snapshot replacement cannot be rebaselined into setup state."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_tree_is_open = runner._registered_actual_phase_two_launch_tree_is_open

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        replacements: list[tuple[str, Path, int, int, bytes]] = []
        entries_after_tree: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def tree_then_replace_initial_snapshot(
            observed_phase: runner._RegisteredActualPreparedPhase,
            *,
            pre_popen: bool,
            control_pin: runner.PinnedDirectory | None = None,
        ) -> None:
            original_tree_is_open(
                observed_phase, pre_popen=pre_popen, control_pin=control_pin,
            )
            assert pre_popen is False
            artifact_id = observed_phase.initial_workspace_snapshot.artifact_id
            assert artifact_id == "workspace-snapshot-execution-2-initial-0"
            entry = next(item for item in writer.entries if item["id"] == artifact_id)
            target = writer.run_root / str(entry["relative_path"])
            raw = target.read_bytes()
            before_inode = target.stat().st_ino
            replacement = target.with_name(
                target.name + ".post-launch-tree-same-byte-replacement",
            )
            replacement.write_bytes(raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            after_inode = target.stat().st_ino
            assert target.read_bytes() == raw
            assert after_inode != before_inode
            replacements.append((artifact_id, target, before_inode, after_inode, raw))
            entries_after_tree.append([dict(entry) for entry in writer.entries])

        monkeypatch.setattr(
            runner,
            "_registered_actual_phase_two_launch_tree_is_open",
            tree_then_replace_initial_snapshot,
        )
        with pytest.raises(runner.RunnerError) as error:
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(replacements) == len(entries_after_tree) == 1, str(error.value)
        artifact_id, target, before_inode, after_inode, raw = replacements[0]
        assert artifact_id == "workspace-snapshot-execution-2-initial-0"
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        assert writer.entries == entries_after_tree[0]
        assert "client-executable-2" not in {
            str(entry["id"]) for entry in writer.entries
        }
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("mutation", ("snapshot", "trace"))
def test_actual_phase_two_setup_return_closure_rechecks_final_snapshot_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """The terminal setup closure cannot adopt residue after its final snapshot read."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_indexed_snapshot = runner._indexed_workspace_snapshot
    expected_snapshot_id = "workspace-snapshot-execution-2-initial-0"

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        mutations: list[tuple[str, Path, int | None, int | None, bytes]] = []
        entries_after_mutation: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def indexed_then_mutate_terminal_closure(
            observed_writer: runner.EvidenceWriter,
            snapshot: runner.WorkspaceSnapshot,
            *,
            control_pin: runner.PinnedDirectory | None = None,
        ) -> runner.WorkspaceSnapshot:
            loaded = original_indexed_snapshot(
                observed_writer, snapshot, control_pin=control_pin,
            )
            in_terminal_closure = any(
                frame.function
                == "_revalidate_registered_actual_phase_two_setup_return_closure"
                for frame in inspect.stack(context=0)
            )
            if (not mutations
                    and in_terminal_closure
                    and snapshot.artifact_id == expected_snapshot_id):
                if mutation == "snapshot":
                    entry = next(
                        item for item in writer.entries if item["id"] == expected_snapshot_id
                    )
                    target = writer.run_root / str(entry["relative_path"])
                    raw = target.read_bytes()
                    before_inode = target.stat().st_ino
                    replacement = target.with_name(
                        target.name + ".terminal-closure-same-byte-replacement",
                    )
                    replacement.write_bytes(raw)
                    replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                    os.replace(replacement, target)
                    after_inode = target.stat().st_ino
                    assert target.read_bytes() == raw
                    assert after_inode != before_inode
                    mutations.append((mutation, target, before_inode, after_inode, raw))
                else:
                    assert mutation == "trace"
                    target = writer.control_root / "runner-trace-execution-2.jsonl"
                    assert not target.exists()
                    raw = b'{"foreign":"terminal-closure-trace"}\n'
                    target.write_bytes(raw)
                    mutations.append((mutation, target, None, None, raw))
                entries_after_mutation.append([dict(entry) for entry in writer.entries])
            return loaded

        monkeypatch.setattr(
            runner, "_indexed_workspace_snapshot", indexed_then_mutate_terminal_closure,
        )
        with pytest.raises(runner.RunnerError):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(mutations) == len(entries_after_mutation) == 1
        observed_mutation, target, before_inode, after_inode, raw = mutations[0]
        assert observed_mutation == mutation
        assert target.read_bytes() == raw
        if mutation == "snapshot":
            assert before_inode is not None and after_inode is not None
            assert before_inode != after_inode
        else:
            assert mutation == "trace"
            assert before_inode is after_inode is None
        assert writer.entries == entries_after_mutation[0]
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_return_closure_rejects_late_marker_snapshot_list_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal reader cannot append a plausible marker before token escape."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_indexed_snapshot = runner._indexed_workspace_snapshot
    expected_snapshot_id = "workspace-snapshot-execution-2-initial-0"
    marker_lists: list[list[runner.WorkspaceSnapshot]] = []

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        appended: list[runner.WorkspaceSnapshot] = []
        entries_after_append: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def indexed_then_append_marker(
            observed_writer: runner.EvidenceWriter,
            snapshot: runner.WorkspaceSnapshot,
            *,
            control_pin: runner.PinnedDirectory | None = None,
        ) -> runner.WorkspaceSnapshot:
            loaded = original_indexed_snapshot(
                observed_writer, snapshot, control_pin=control_pin,
            )
            closure_frame = next(
                (
                    frame.frame for frame in inspect.stack(context=0)
                    if frame.function
                    == "_revalidate_registered_actual_phase_two_setup_return_closure"
                ),
                None,
            )
            if (not appended
                    and closure_frame is not None
                    and snapshot.artifact_id == expected_snapshot_id):
                markers = closure_frame.f_locals["marker_workspace_snapshots"]
                assert type(markers) is list and not markers
                marker = replace(
                    loaded,
                    trace_sequence=1,
                    role="marker",
                    artifact_id="workspace-snapshot-execution-2-marker-1",
                    native_record={
                        "transcript_id": "transcript-2",
                        "byte_start": 0,
                        "byte_end": 1,
                        "sha256": "0" * 64,
                    },
                )
                markers.append(marker)
                marker_lists.append(markers)
                appended.append(marker)
                entries_after_append.append([dict(entry) for entry in writer.entries])
            return loaded

        monkeypatch.setattr(
            runner, "_indexed_workspace_snapshot", indexed_then_append_marker,
        )
        with pytest.raises(runner.RunnerError):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(marker_lists) == len(appended) == len(entries_after_append) == 1
        marker = appended[0]
        assert marker.execution_id == "execution-2"
        assert marker.role == "marker"
        assert marker.trace_sequence == 1
        assert marker_lists[0] == [marker]
        assert writer.entries == entries_after_append[0]
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        for markers in marker_lists:
            markers.clear()
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("residue_kind", ("orphan", "raw-ledger", "trace-ledger"))
def test_actual_phase_two_setup_return_closure_rejects_post_provenance_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue_kind: str,
) -> None:
    """The terminal tree rejects residue added after an earlier closure reader."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_verify_provenance = (
        runner._verify_registered_actual_phase_two_expected_evidence_provenance
    )

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        mutations: list[tuple[str, Path, bytes]] = []
        entries_after_mutation: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def verify_provenance_then_create_residue(
            *args: object,
            **kwargs: object,
        ) -> None:
            original_verify_provenance(*args, **kwargs)
            in_terminal_closure = any(
                frame.function
                == "_revalidate_registered_actual_phase_two_setup_return_closure"
                for frame in inspect.stack(context=0)
            )
            if in_terminal_closure and not mutations:
                if residue_kind == "orphan":
                    target = writer.run_root / "terminal-closure-orphan.txt"
                else:
                    assert residue_kind in {"raw-ledger", "trace-ledger"}
                    ledger_kind = residue_kind.removesuffix("-ledger")
                    target = _task7d_phase_two_receipt_paths(writer.control_root)[ledger_kind]
                assert not target.exists()
                raw = ("terminal-closure-" + residue_kind + "\n").encode("utf-8")
                target.write_bytes(raw)
                mutations.append((residue_kind, target, raw))
                entries_after_mutation.append([dict(entry) for entry in writer.entries])

        monkeypatch.setattr(
            runner,
            "_verify_registered_actual_phase_two_expected_evidence_provenance",
            verify_provenance_then_create_residue,
        )
        with pytest.raises(runner.RunnerError):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(mutations) == len(entries_after_mutation) == 1
        observed_kind, target, raw = mutations[0]
        assert observed_kind == residue_kind
        assert target.read_bytes() == raw
        assert writer.entries == entries_after_mutation[0]
        receipt_paths = _task7d_phase_two_receipt_paths(writer.control_root)
        if residue_kind == "orphan":
            assert all(not path.exists() for path in receipt_paths.values())
        else:
            ledger_kind = residue_kind.removesuffix("-ledger")
            assert target == receipt_paths[ledger_kind]
            assert all(
                path == target or not path.exists() for path in receipt_paths.values()
            )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("target_kind", ("phase-two-control", "phase-one-raw"))
def test_actual_phase_two_setup_return_closure_rechecks_full_control_tree_after_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    """The final authority return cannot replace a root control or raw journal."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_authority_revalidation = (
        runner._revalidate_registered_actual_phase_two_setup_authority
    )

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        replacements: list[tuple[str, Path, int, int, bytes]] = []
        entries_after_replacement: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []

        def authority_then_replace_root_input(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            scenario = original_authority_revalidation(*args, **kwargs)
            caller = inspect.currentframe()
            caller_name = (
                None if caller is None or caller.f_back is None
                else caller.f_back.f_code.co_name
            )
            if (not replacements
                    and caller_name
                    == "_revalidate_registered_actual_phase_two_setup_return_closure"):
                if target_kind == "phase-two-control":
                    target = writer.control_root / "brain-shim.json"
                else:
                    assert target_kind == "phase-one-raw"
                    target = writer.control_root / "brain-shim-capture-index.jsonl"
                raw = target.read_bytes()
                before_inode = target.stat().st_ino
                replacement = target.with_name(
                    target.name + ".post-final-authority-same-byte-replacement",
                )
                replacement.write_bytes(raw)
                replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                os.replace(replacement, target)
                after_inode = target.stat().st_ino
                assert target.read_bytes() == raw
                assert after_inode != before_inode
                replacements.append((target_kind, target, before_inode, after_inode, raw))
                entries_after_replacement.append([dict(entry) for entry in writer.entries])
            return scenario

        monkeypatch.setattr(
            runner,
            "_revalidate_registered_actual_phase_two_setup_authority",
            authority_then_replace_root_input,
        )
        with pytest.raises(runner.RunnerError):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(replacements) == len(entries_after_replacement) == 1
        observed_kind, target, before_inode, after_inode, raw = replacements[0]
        assert observed_kind == target_kind
        assert target.read_bytes() == raw
        assert before_inode != after_inode
        if target_kind == "phase-two-control":
            assert target.name == "brain-shim.json"
        else:
            assert target_kind == "phase-one-raw"
            assert target.name == "brain-shim-capture-index.jsonl"
        assert writer.entries == entries_after_replacement[0]
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("mutation_kind", ("existing-file", "unexpected-file"))
def test_actual_phase_two_setup_return_closure_rechecks_sealed_workspace_after_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
) -> None:
    """A final-authority workspace mutation cannot become a prepared-token baseline."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer
    original_authority_revalidation = (
        runner._revalidate_registered_actual_phase_two_setup_authority
    )
    original_add_bytes = runner.EvidenceWriter.add_bytes
    original_popen = runner.subprocess.Popen

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        mutations: list[tuple[str, Path, bytes, bytes]] = []
        entries_after_mutation: list[list[dict[str, object]]] = []
        returned_phase_two: list[object] = []
        post_mutation_evidence_writes: list[object] = []
        post_mutation_processes: list[object] = []

        def evidence_write_guard(
            observed_writer: runner.EvidenceWriter,
            *args: object,
            **kwargs: object,
        ) -> object:
            if observed_writer is writer and mutations:
                post_mutation_evidence_writes.append((args, kwargs))
                pytest.fail("terminal workspace verification wrote evidence")
            return original_add_bytes(observed_writer, *args, **kwargs)

        def process_guard(*args: object, **kwargs: object) -> object:
            if mutations:
                post_mutation_processes.append((args, kwargs))
                pytest.fail("terminal workspace verification launched a process")
            return original_popen(*args, **kwargs)

        def authority_then_mutate_workspace(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            scenario = original_authority_revalidation(*args, **kwargs)
            caller = inspect.currentframe()
            caller_name = (
                None if caller is None or caller.f_back is None
                else caller.f_back.f_code.co_name
            )
            if (not mutations
                    and caller_name
                    == "_revalidate_registered_actual_phase_two_setup_return_closure"):
                if mutation_kind == "existing-file":
                    target = prepared.workspace / "config/extractors.toml"
                    before = target.read_bytes()
                    after = before + b"\n# terminal-closure-workspace-mutation\n"
                else:
                    assert mutation_kind == "unexpected-file"
                    target = prepared.workspace / "terminal-closure-unexpected.txt"
                    before = b""
                    after = b"terminal-closure-unexpected-workspace-file\n"
                    assert not target.exists()
                target.write_bytes(after)
                assert target.read_bytes() == after
                mutations.append((mutation_kind, target, before, after))
                entries_after_mutation.append([dict(entry) for entry in writer.entries])
            return scenario

        monkeypatch.setattr(runner.EvidenceWriter, "add_bytes", evidence_write_guard)
        monkeypatch.setattr(runner.subprocess, "Popen", process_guard)
        monkeypatch.setattr(
            runner,
            "_revalidate_registered_actual_phase_two_setup_authority",
            authority_then_mutate_workspace,
        )
        with pytest.raises(runner.RunnerError, match="workspace changed before launch"):
            returned_phase_two.append(runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            ))

        assert returned_phase_two == []
        assert len(mutations) == len(entries_after_mutation) == 1
        observed_kind, target, before, after = mutations[0]
        assert observed_kind == mutation_kind
        assert target.read_bytes() == after
        if mutation_kind == "existing-file":
            assert before != after
            assert target.relative_to(prepared.workspace).as_posix() == "config/extractors.toml"
        else:
            assert mutation_kind == "unexpected-file"
            assert before == b""
            assert target.relative_to(prepared.workspace).as_posix() == (
                "terminal-closure-unexpected.txt"
            )
        assert writer.entries == entries_after_mutation[0]
        assert post_mutation_evidence_writes == []
        assert post_mutation_processes == []
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_setup_validates_receipt_after_phase_approval_before_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid approval result cannot carry a changed receipt to phase-control writes."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_approval = runner._phase_approval
    original_facts = receipt_gate.receipt_facts
    replacement_facts = replace(
        original_facts,
        effect_digest=(
            "0" * 64 if original_facts.effect_digest != "0" * 64 else "f" * 64
        ),
    )

    try:
        baseline_gate = replace(receipt_gate)
        direct_approval_calls: list[dict[str, str] | None] = []
        mutations: list[object] = []
        phase_control_calls: list[object] = []
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        control_paths = (
            phase_one.phase_control.config.path,
            phase_one.runner_state.path,
        )
        before_control = {path: path.read_bytes() for path in control_paths}

        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return baseline_gate

        def approval_then_mutate_receipt(
            scenario: Mapping[str, object],
        ) -> dict[str, str] | None:
            approval = original_approval(scenario)
            caller = inspect.currentframe()
            caller_name = (
                None if caller is None or caller.f_back is None
                else caller.f_back.f_code.co_name
            )
            if caller_name == "_prepare_registered_actual_phase_two":
                direct_approval_calls.append(approval)
                object.__setattr__(receipt_gate, "receipt_facts", replacement_facts)
                assert receipt_gate.__post_init__() is None
                mutations.append(receipt_gate.receipt_facts)
            return approval

        def no_phase_control(*_args: object, **_kwargs: object) -> object:
            phase_control_calls.append("phase-control")
            pytest.fail("changed receipt reached phase-two phase-control write")

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        monkeypatch.setattr(runner, "_phase_approval", approval_then_mutate_receipt)
        monkeypatch.setattr(runner, "_phase_control", no_phase_control)
        with pytest.raises(runner.RunnerError, match="receipt gate is no longer valid"):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert direct_approval_calls == [original_approval(prepared_run.prepared.scenario)]
        assert mutations == [replacement_facts]
        assert phase_control_calls == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert {path: path.read_bytes() for path in control_paths} == before_control
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(receipt_gate, "receipt_facts", original_facts)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_receipt_allocation_rechecks_retained_raw_predecessor_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared phase retains raw predecessor primitives beyond its facade."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    predecessors = diagnostic.raw_predecessors
    original_values = predecessors.values
    raw_relative = "brain-shim-capture-index.jsonl"
    raw_target = writer.control_root / raw_relative
    raw_before = raw_target.read_bytes()
    raw_inode_before = raw_target.stat().st_ino

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        expected_raw_snapshot = phase_two.phase_one_raw_predecessor_snapshot
        assert expected_raw_snapshot is not None
        expected_workspace_inventory = phase_two.sealed_initial_workspace_inventory
        assert runner._registered_actual_phase_two_workspace_inventory_is_valid(
            expected_workspace_inventory,
        )
        launch_binding = runner._registered_actual_phase_two_launch_binding_for(
            phase_two,
        )
        receipt_paths = _task7d_phase_two_receipt_paths(writer.control_root)
        assert all(not path.exists() for path in receipt_paths.values())

        replacement = raw_target.with_name(
            raw_target.name + ".post-prepare-forged-baseline-replacement",
        )
        replacement.write_bytes(raw_before)
        replacement.chmod(stat.S_IMODE(raw_target.stat().st_mode))
        os.replace(replacement, raw_target)
        raw_inode_after = raw_target.stat().st_ino
        assert raw_target.read_bytes() == raw_before
        assert raw_inode_after != raw_inode_before
        forged = runner._PrivateRawFileBaseline(
            raw_relative,
            hashlib.sha256(raw_before).hexdigest(),
            len(raw_before),
            runner.PinnedDirectory._regular_identity(raw_target.lstat()),
            raw_before.count(b"\n"),
        )
        replacement_values = dict(original_values)
        replacement_values[raw_relative] = forged
        object.__setattr__(predecessors, "values", MappingProxyType(replacement_values))
        assert predecessors.__post_init__() is None
        # This direct allocator unit test intentionally supplies the current
        # tree to isolate its retained preflight inputs.  Its detached launch
        # binding must nevertheless reject the same-byte replacement before
        # allocation can use that refreshed facade as a new baseline.
        expected_pre_allocation_control_tree = MappingProxyType(
            dict(phase_two.control_pin.snapshot_tree()),
        )

        with pytest.raises(runner.RunnerError, match="static control tree changed"):
            runner._allocate_registered_actual_phase_two_receipt_ledgers(
                prepared_run,
                diagnostic,
                phase_two,
                expected_raw_snapshot=expected_raw_snapshot,
                expected_pre_allocation_control_tree=expected_pre_allocation_control_tree,
                expected_workspace_inventory=expected_workspace_inventory,
                launch_binding=launch_binding,
            )

        assert predecessors.values[raw_relative] is forged
        assert raw_target.read_bytes() == raw_before
        assert raw_target.stat().st_ino == raw_inode_after
        assert all(not path.exists() for path in receipt_paths.values())
    finally:
        object.__setattr__(predecessors, "values", original_values)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def _task7d_actual_phase_two_launcher_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    runner._RegisteredActualPreparedPhase,
    runner._RegisteredActualPhaseOneDiagnostic,
    runner.PhaseOneReceiptGate,
]:
    """Build phase-one diagnostic facts plus a shaped private receipt token.

    The fixture's native stream is deliberately pre-captured to make its
    local pipe double deterministic, so the real sealed receipt reader is
    covered elsewhere.  Launcher tests replace that reader with this exact
    typed token and focus on timing, pinned binding, and Popen exclusion.
    """

    phase_one, live_phase = _task7d_actual_indexable_phase_one(tmp_path, monkeypatch)
    assert type(phase_one) is runner._RegisteredActualPreparedPhase
    diagnostic = runner._index_registered_actual_phase_one_diagnostic(
        phase_one, live_phase,
    )
    from tests.evals import event_log_contract as contract

    verify_id, consume_id, durable_id, acknowledge_id = diagnostic.conversion.observation_ids
    receipt_gate = runner.PhaseOneReceiptGate(
        execution_id="execution-1",
        receipt_facts=contract.InitialSyncReceiptFacts(
            result_id="sync_" + "b" * 64,
            corpus_revision="c" * 64,
            event_counts={},
            effect_digest="d" * 64,
            selection=contract.InitialSyncReceiptSelection(
                verify_command_id=verify_id,
                consume_command_id=consume_id,
                durable_command_id=durable_id,
                acknowledge_command_id=acknowledge_id,
                stream_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                    verify_id
                ]["stream"],
                durable_artifact_id=diagnostic.conversion.receipt_artifact_ids[
                    durable_id
                ]["durable"],
            ),
        ),
        acknowledgement_position=(0, 1, 0, 0),
        report_gap_position=(0, 2, 0, 0),
        approval_position=(0, 3, 0, 0),
    )
    return phase_one, diagnostic, receipt_gate


@pytest.mark.parametrize(
    "replacement_mode",
    ("different-bytes", "same-bytes-os-replace"),
)
def test_actual_phase_two_private_launcher_rechecks_phase_one_binding_at_pre_popen_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_mode: str,
) -> None:
    """A phase-one mutation during the final phase-two probe blocks Popen.

    The mutation happens only after phase two has successfully completed its
    version probe.  It therefore cannot be caught by setup-time receipt or
    binding validation: the private launcher must reinstall those exact
    checks at its pre-Popen boundary.
    """

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    try:
        reconstructed_gates: list[dict[str, object]] = []

        def sealed_reader(**kwargs: object) -> runner.PhaseOneReceiptGate:
            reconstructed_gates.append(dict(kwargs))
            return receipt_gate

        # The fixture deliberately builds a pre-captured stream so it can
        # shape test-only client pipes; its ordering is not a real receipt
        # release.  Keep the launcher test focused on the late binding guard;
        # the sealed-reader suite separately proves reconstruction itself.
        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )

        signature = inspect.signature(runner._launch_registered_actual_phase_two)
        assert tuple(signature.parameters) == (
            "prepared_run", "phase_one_diagnostic", "receipt_gate", "prepared_phase",
        )
        forbidden_inputs = {
            "process_runner", "probe_runner", "popen_factory", "argv", "environment",
            "template", "prompt", "client_command_json", "network_attestation",
        }
        assert not forbidden_inputs & set(signature.parameters)

        entries = {entry["id"]: entry for entry in writer.entries}
        policy_path = writer.run_root / str(entries["policy-1"]["relative_path"])
        policy_before_mutation = policy_path.read_bytes()
        policy_inode_before_mutation = policy_path.stat().st_ino
        probe_argvs: list[list[str]] = []
        mutation_complete = False

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal mutation_complete
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                # This replaces a sealed phase-one policy only after phase
                # two's probes have begun.  The same-byte replacement case
                # proves the gate retains artifact identity, not just its
                # digest and JSON payload.
                if replacement_mode == "same-bytes-os-replace":
                    replacement = policy_path.with_name(policy_path.name + ".replacement")
                    replacement.write_bytes(policy_before_mutation)
                    os.replace(replacement, policy_path)
                else:
                    assert replacement_mode == "different-bytes"
                    policy_path.write_bytes(b'{"replaced_after_phase_two_probe":true}\n')
                mutation_complete = True
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        popen_calls: list[object] = []

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("late phase-one mutation reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)

        with pytest.raises(
            runner.RunnerError,
            match=(
                "phase-one|receipt|bound|evidence|"
                "runner artifact changed before semantic planning|"
                "frozen control artifact changed before diagnostic adoption"
            ),
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert probe_argvs and [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert mutation_complete is True
        if replacement_mode == "same-bytes-os-replace":
            assert policy_path.read_bytes() == policy_before_mutation
            assert policy_path.stat().st_ino != policy_inode_before_mutation
        else:
            assert policy_path.read_bytes() != policy_before_mutation
        assert popen_calls == []
        assert len(reconstructed_gates) >= 1
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_pre_popen_verifier_does_not_read_dynamic_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile dynamic approval helper cannot rewrite phase-two control.

    The helper becomes hostile only after the normal ``--help`` probe, so its
    only possible consumer is the final pre-Popen verification.  A controlled
    Popen double proves the sealed fixed approval, rather than the helper's
    forged value, remains what the child would receive.
    """

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        control_path = phase_two.phase_control.config.path
        control_before = control_path.read_bytes()
        expected_approval = runner._registered_actual_phase_two_approval()
        assert json.loads(control_before)["approval"] == expected_approval
        forged_approval = {
            "event_id": "forged-approval",
            "scope": "forged pre-popen scope",
            "note": "forged dynamic approval",
        }
        approval_calls: list[object] = []
        forged_writes: list[bytes] = []
        probe_argvs: list[list[str]] = []
        popen_calls: list[object] = []

        def malicious_phase_approval(scenario: Mapping[str, object]) -> dict[str, str]:
            approval_calls.append(dict(scenario))
            forged_control = json.loads(control_path.read_bytes())
            assert type(forged_control) is dict
            forged_control["approval"] = dict(forged_approval)
            raw = _encoded(forged_control)
            control_path.write_bytes(raw)
            forged_writes.append(raw)
            return dict(forged_approval)

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                # The initial launch check has already run; make the helper
                # hostile only for the adjacent pre-Popen verifier.
                monkeypatch.setattr(
                    runner, "_phase_approval", malicious_phase_approval,
                )
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        def stop_at_popen(argv: list[str], **kwargs: object) -> object:
            popen_calls.append((list(argv), kwargs))
            assert json.loads(control_path.read_bytes())["approval"] == expected_approval
            assert control_path.read_bytes() == control_before
            assert not phase_two.trace.path.exists()
            raise runner.RunnerError("controlled fixed-approval Popen boundary")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", stop_at_popen)
        with pytest.raises(
            runner.RunnerError,
            match="controlled fixed-approval Popen boundary",
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert approval_calls == []
        assert forged_writes == []
        assert len(popen_calls) == 1
        assert control_path.read_bytes() == control_before
        assert json.loads(control_path.read_bytes())["approval"] == expected_approval
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_post_allocation_boundary_rejects_control_replaced_after_trace_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sink arming cannot replace a sealed control before the sole Popen call."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_arm = runner.ControlTraceWriter._arm_private_trace_receipts
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        # The launcher deliberately owns a distinct private trace facade; a
        # public token-side trace hook cannot exercise the receipt-arm
        # boundary that reaches the live process path.
        launch_binding = runner._registered_actual_phase_two_launch_binding_for(
            phase_two,
        )
        arm_calls: list[object] = []
        replacements: list[tuple[Path, int, int, bytes]] = []
        popen_calls: list[object] = []
        token_captures: list[object] = []

        def arm_then_replace_control(
            trace: runner.ControlTraceWriter,
            sink: object,
        ) -> None:
            original_arm(trace, sink)
            if trace is not launch_binding.trace:
                return
            arm_calls.append(sink)
            receipt_paths = _task7d_phase_two_receipt_paths(writer.control_root)
            assert all(path.exists() for path in receipt_paths.values())
            target = phase_two.phase_control.config.path
            raw = target.read_bytes()
            before = target.stat().st_ino
            replacement = target.with_name(
                target.name + ".post-arm-same-byte-replacement",
            )
            replacement.write_bytes(raw)
            replacement.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(replacement, target)
            after = target.stat().st_ino
            assert after != before
            replacements.append((target, before, after, raw))

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            output = (
                b"2.1.251 (test Claude)\n"
                if argv[-1] == "--version" else b"stream-json help\n"
            )
            assert argv[-1] in {"--version", "--help"}
            return subprocess.CompletedProcess(argv, 0, output, b"")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("post-arm control replacement reached Popen")

        def capture_token(*args: object, **kwargs: object) -> object:
            token_captures.append((args, kwargs))
            return original_capture(*args, **kwargs)

        monkeypatch.setattr(
            runner.ControlTraceWriter,
            "_arm_private_trace_receipts",
            arm_then_replace_control,
        )
        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        monkeypatch.setattr(
            runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
        )

        with pytest.raises(runner.RunnerError):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert len(arm_calls) == 1
        assert len(replacements) == 1
        target, before, after, raw = replacements[0]
        assert after != before
        assert target.read_bytes() == raw
        assert target.stat().st_ino == after
        assert popen_calls == []
        assert token_captures == []
        assert not phase_two.trace.path.exists()
        for kind, path in _task7d_phase_two_receipt_paths(writer.control_root).items():
            _header, rows = _task7d_assert_receipt_ledger_chain(
                path, ledger_kind=kind, run_id=writer.run_id,
            )
            assert rows == []
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("mutation_kind", ("tracked-file", "unexpected-file"))
def test_actual_phase_two_private_launcher_rechecks_sealed_workspace_before_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_kind: str,
) -> None:
    """A post-prepare workspace change fails before either phase-two probe."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        tracked_paths = {
            str(item["path"]) for item in phase_two.initial_workspace_snapshot.inventory
        }
        if mutation_kind == "tracked-file":
            target = prepared.workspace / "config/extractors.toml"
            assert target.relative_to(prepared.workspace).as_posix() in tracked_paths
            before = target.read_bytes()
            after = before + b"\n# post-prepare-workspace-change\n"
        else:
            assert mutation_kind == "unexpected-file"
            target = prepared.workspace / "post-prepare-unexpected.txt"
            assert target.relative_to(prepared.workspace).as_posix() not in tracked_paths
            assert not target.exists()
            before = b""
            after = b"post-prepare-unexpected-workspace-file\n"
        target.write_bytes(after)
        assert target.read_bytes() == after
        before_entries = [dict(entry) for entry in writer.entries]
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("post-prepare workspace change reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("post-prepare workspace change reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError, match="workspace changed before launch"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert target.read_bytes() == after
        assert before != after
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rejects_workspace_pin_method_shadow_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token-side pin method shadows cannot forge the sealed workspace read."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    workspace_pin: runner.PinnedDirectory | None = None
    read_shadowed = False
    verify_shadowed = False

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        workspace_pin = phase_two.workspace_pin
        target = prepared.workspace / "config/extractors.toml"
        relative = target.relative_to(prepared.workspace).as_posix()
        assert relative in {
            str(item["path"]) for item in phase_two.initial_workspace_snapshot.inventory
        }
        before = target.read_bytes()
        after = before + b"\n# token-side-pinned-read-shadow\n"
        target.write_bytes(after)
        assert target.read_bytes() == after
        assert "read_relative" not in workspace_pin.__dict__
        assert "verify" not in workspace_pin.__dict__
        forged_read_calls: list[str] = []

        def forged_read_relative(
            requested_relative: str,
            *,
            maximum: int = runner._MAX_CAPTURE_BYTES,
        ) -> bytes:
            forged_read_calls.append(requested_relative)
            if requested_relative == relative:
                return before
            return runner.PinnedDirectory.read_relative(
                workspace_pin,
                requested_relative,
                maximum=maximum,
            )

        def forged_verify() -> None:
            return None

        object.__setattr__(workspace_pin, "read_relative", forged_read_relative)
        read_shadowed = True
        object.__setattr__(workspace_pin, "verify", forged_verify)
        verify_shadowed = True
        assert workspace_pin.read_relative is forged_read_relative
        assert workspace_pin.verify is forged_verify
        before_entries = [dict(entry) for entry in writer.entries]
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("workspace pin shadow reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("workspace pin shadow reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError, match="workspace changed before launch"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        # The class-qualified scanner and PinnedDirectory internals must not
        # consult the attacker-provided read method; the no-op verifier also
        # cannot conceal the changed content at the launch boundary.
        assert forged_read_calls == []
        assert target.read_bytes() == after
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if workspace_pin is not None and read_shadowed:
            object.__delattr__(workspace_pin, "read_relative")
        if workspace_pin is not None and verify_shadowed:
            object.__delattr__(workspace_pin, "verify")
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    ("token_pin_field", "retained_pin_field"),
    (
        ("control_pin", "control_pin"),
        ("workspace_pin", "workspace_pin"),
    ),
    ids=("control-root-fd", "workspace-root-fd"),
)
def test_actual_phase_two_private_launcher_rejects_tampered_token_pin_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token_pin_field: str,
    retained_pin_field: str,
) -> None:
    """A returned pin cannot mutate the detached private launch boundary."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    token_pin: runner.PinnedDirectory | None = None
    original_root_fd: int | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        binding = runner._registered_actual_phase_two_launch_binding_for(phase_two)

        # The token deliberately keeps diagnostic facades, while the binding
        # owns duplicate descriptor roots for every private launch operation.
        assert binding.token_control_pin is phase_two.control_pin
        assert binding.token_workspace_pin is phase_two.workspace_pin
        assert binding.control_pin is not phase_two.control_pin
        assert binding.workspace_pin is not phase_two.workspace_pin
        assert binding.control_pin.root_fd != phase_two.control_pin.root_fd
        assert binding.workspace_pin.root_fd != phase_two.workspace_pin.root_fd
        runner.PinnedDirectory.verify(binding.control_pin)
        runner.PinnedDirectory.verify(binding.workspace_pin)

        token_pin = getattr(phase_two, token_pin_field)
        retained_pin = getattr(binding, retained_pin_field)
        assert type(token_pin) is runner.PinnedDirectory
        assert type(retained_pin) is runner.PinnedDirectory
        assert token_pin is not retained_pin
        original_root_fd = token_pin.root_fd
        assert original_root_fd >= 0
        object.__setattr__(token_pin, "root_fd", -1)
        assert token_pin.root_fd == -1
        # The detached binding must stay usable even as the returned facade is
        # made invalid.  The launcher nevertheless rejects the corrupted
        # facade before it can reach any live-launch boundary.
        assert retained_pin.root_fd >= 0
        runner.PinnedDirectory.verify(retained_pin)

        before_entries = [dict(entry) for entry in writer.entries]
        generic_calls: list[object] = []
        probe_calls: list[object] = []
        popen_calls: list[object] = []
        allocation_calls: list[object] = []

        def no_generic(*args: object, **kwargs: object) -> object:
            generic_calls.append((args, kwargs))
            pytest.fail("tampered token pin reached generic phase-two launch")

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("tampered token pin reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("tampered token pin reached phase-two Popen")

        def no_allocation(*args: object, **kwargs: object) -> object:
            allocation_calls.append((args, kwargs))
            pytest.fail("tampered token pin reached receipt allocation")

        monkeypatch.setattr(runner, "_launch_registered_live_phase", no_generic)
        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        monkeypatch.setattr(
            runner, "_allocate_registered_actual_phase_two_receipt_ledgers", no_allocation,
        )
        with pytest.raises(runner.RunnerError):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert generic_calls == []
        assert probe_calls == []
        assert popen_calls == []
        assert allocation_calls == []
        assert writer.entries == before_entries
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        # The mutation changes only the frozen Python field, not the OS
        # descriptor.  Restore it so normal fixture teardown closes that fd.
        if token_pin is not None and original_root_fd is not None:
            object.__setattr__(token_pin, "root_fd", original_root_fd)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rechecks_sealed_workspace_after_help_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace change during help cannot pass the final pre-Popen gate."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        target = prepared.workspace / "config/extractors.toml"
        assert target.relative_to(prepared.workspace).as_posix() in {
            str(item["path"]) for item in phase_two.initial_workspace_snapshot.inventory
        }
        before = target.read_bytes()
        after = before + b"\n# probe-time-workspace-change\n"
        probe_argvs: list[list[str]] = []
        mutations: list[Path] = []
        popen_calls: list[object] = []

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            assert cwd == prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                target.write_bytes(after)
                assert target.read_bytes() == after
                mutations.append(target)
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("probe-time workspace change reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError, match="workspace changed before launch"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert mutations == [target]
        assert target.read_bytes() == after
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rechecks_workspace_token_after_final_scanner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final scanner cannot replace the retained workspace launch input."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    original_inventory: tuple[tuple[str, str, int], ...] | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        original_inventory = phase_two.sealed_initial_workspace_inventory
        assert runner._registered_actual_phase_two_workspace_inventory_is_valid(
            original_inventory,
        )
        assert original_inventory
        first_path, first_digest, first_bytes = original_inventory[0]
        replacement_digest = "0" * 64 if first_digest != "0" * 64 else "f" * 64
        forged_inventory = (
            (first_path, replacement_digest, first_bytes), *original_inventory[1:],
        )
        # It remains a well-formed immutable inventory; the desired failure
        # is the local replay after the scanner, not shape validation.
        assert runner._registered_actual_phase_two_workspace_inventory_is_valid(
            forged_inventory,
        )
        scanner_calls: list[object] = []
        original_scanner = (
            runner._verify_registered_actual_phase_two_workspace_inventory_is_current
        )

        def scan_then_mutate(*args: object, **kwargs: object) -> None:
            original_scanner(*args, **kwargs)
            scanner_calls.append((args, kwargs))
            object.__setattr__(
                phase_two, "sealed_initial_workspace_inventory", forged_inventory,
            )
            assert phase_two.__post_init__() is None

        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("scanner-side token mutation reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("scanner-side token mutation reached phase-two Popen")

        monkeypatch.setattr(
            runner,
            "_verify_registered_actual_phase_two_workspace_inventory_is_current",
            scan_then_mutate,
        )
        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        before_entries = [dict(entry) for entry in writer.entries]
        with pytest.raises(
            runner.RunnerError,
            match="registered actual phase-two retained setup state changed",
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert len(scanner_calls) == 1
        assert phase_two.sealed_initial_workspace_inventory == forged_inventory
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if phase_two is not None and original_inventory is not None:
            object.__setattr__(
                phase_two, "sealed_initial_workspace_inventory", original_inventory,
            )
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_reloads_indexed_workspace_before_forged_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged retained inventory cannot redefine the post-prepare workspace baseline."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    prepared = prepared_run.prepared
    writer = prepared_run.writer
    original_inventory: tuple[tuple[str, str, int], ...] | None = None
    phase_two: runner._RegisteredActualPreparedPhase | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        original_inventory = phase_two.sealed_initial_workspace_inventory
        assert runner._registered_actual_phase_two_workspace_inventory_is_valid(
            original_inventory,
        )
        target = prepared.workspace / "config/extractors.toml"
        relative = target.relative_to(prepared.workspace).as_posix()
        assert relative in {path for path, _digest, _byte_count in original_inventory}
        before = target.read_bytes()
        after = before + b"\n# forged-workspace-token-target\n"
        target.write_bytes(after)
        forged_rows: list[tuple[str, str, int]] = []
        for path, digest, byte_count in original_inventory:
            if path == relative:
                forged_rows.append(
                    (relative, hashlib.sha256(after).hexdigest(), len(after)),
                )
            else:
                forged_rows.append((path, digest, byte_count))
        forged_inventory = tuple(forged_rows)
        assert forged_inventory != original_inventory
        assert runner._registered_actual_phase_two_workspace_inventory_is_valid(
            forged_inventory,
        )
        object.__setattr__(
            phase_two, "sealed_initial_workspace_inventory", forged_inventory,
        )
        assert phase_two.__post_init__() is None
        before_entries = [dict(entry) for entry in writer.entries]
        scanner_calls: list[object] = []
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_scanner(*args: object, **kwargs: object) -> None:
            scanner_calls.append((args, kwargs))
            pytest.fail("forged workspace token reached the workspace scanner")

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("forged workspace token reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("forged workspace token reached phase-two Popen")

        monkeypatch.setattr(
            runner,
            "_verify_registered_actual_phase_two_workspace_inventory_is_current",
            no_scanner,
        )
        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(
            runner.RunnerError,
            match="registered actual phase-two retained (?:launch token|setup state) changed",
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert target.read_bytes() == after
        assert scanner_calls == []
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if phase_two is not None and original_inventory is not None:
            object.__setattr__(
                phase_two, "sealed_initial_workspace_inventory", original_inventory,
            )
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rejects_resealed_fixture_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase-control facade cannot re-seal a replaced fixture after return."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    original_phase_control: runner.PhaseControlSeals | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        original_phase_control = phase_two.phase_control
        fixture_assets = dict(original_phase_control.fixture_assets)
        asset_seal, asset_raw = fixture_assets["static-shell.html"]
        target = asset_seal.path
        original_inode = target.stat().st_ino
        replacement = target.with_name(
            target.name + ".post-prepare-resealed-fixture-replacement",
        )
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(stat.S_IMODE(target.stat().st_mode))
        os.replace(replacement, target)
        replacement_inode = target.stat().st_ino
        assert replacement_inode != original_inode
        assert target.read_bytes() == asset_raw
        fixture_assets["static-shell.html"] = (
            runner._seal_control_file(target), asset_raw,
        )
        forged_phase_control = runner.PhaseControlSeals(
            config=original_phase_control.config,
            fixture_assets=MappingProxyType(fixture_assets),
        )
        object.__setattr__(phase_two, "phase_control", forged_phase_control)
        assert phase_two.__post_init__() is None
        before_entries = [dict(entry) for entry in writer.entries]
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("re-sealed fixture reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("re-sealed fixture reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert phase_two.phase_control is forged_phase_control
        assert target.stat().st_ino == replacement_inode
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if phase_two is not None and original_phase_control is not None:
            object.__setattr__(phase_two, "phase_control", original_phase_control)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("tamper", ("marker-list", "callback"))
def test_actual_phase_two_private_launcher_rejects_marker_callback_rebinding_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """Returned marker list and callback aliases cannot redefine live capture."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    original_markers: list[runner.WorkspaceSnapshot] | None = None
    original_callback: Callable[[bytes, int, int, int], str] | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        original_markers = phase_two.marker_workspace_snapshots
        original_callback = phase_two.on_native_record
        callback_calls: list[tuple[bytes, int, int, int]] = []
        if tamper == "marker-list":
            replacement_markers: list[runner.WorkspaceSnapshot] = []
            object.__setattr__(phase_two, "marker_workspace_snapshots", replacement_markers)
            assert phase_two.marker_workspace_snapshots is replacement_markers
        else:
            assert tamper == "callback"

            def forged_callback(
                raw: bytes,
                byte_start: int,
                byte_end: int,
                sequence: int,
            ) -> str:
                callback_calls.append((raw, byte_start, byte_end, sequence))
                return phase_two.initial_workspace_snapshot.artifact_id

            object.__setattr__(phase_two, "on_native_record", forged_callback)
            assert phase_two.on_native_record is forged_callback
        assert phase_two.__post_init__() is None
        before_entries = [dict(entry) for entry in writer.entries]
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("rebound marker capture reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("rebound marker capture reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert writer.entries == before_entries
        assert callback_calls == []
        assert probe_calls == []
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if phase_two is not None and original_markers is not None:
            object.__setattr__(phase_two, "marker_workspace_snapshots", original_markers)
        if phase_two is not None and original_callback is not None:
            object.__setattr__(phase_two, "on_native_record", original_callback)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rejects_trace_facade_rebinding_after_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh but shape-valid trace facade cannot replace the retained writer."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    original_trace: runner.ControlTraceWriter | None = None

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        original_trace = phase_two.trace
        forged_trace = runner.ControlTraceWriter(
            writer.control_root,
            "execution-2",
            control_pin=phase_two.control_pin,
        )
        assert forged_trace is not original_trace
        assert forged_trace.path == original_trace.path
        assert forged_trace.lock_path == original_trace.lock_path
        object.__setattr__(phase_two, "trace", forged_trace)
        assert phase_two.__post_init__() is None
        before_entries = [dict(entry) for entry in writer.entries]
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("rebound trace facade reached a phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("rebound trace facade reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert phase_two.trace is forged_trace
        assert writer.entries == before_entries
        assert probe_calls == []
        assert popen_calls == []
        assert not forged_trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if phase_two is not None and original_trace is not None:
            object.__setattr__(phase_two, "trace", original_trace)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("tamper", ("callback-code", "trace-method"))
def test_actual_phase_two_private_launcher_uses_detached_callback_trace_authority_after_token_behavior_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    """Live phase two ignores behavior poisoned only on its exposed token facades."""

    prepared_phases: list[runner._RegisteredActualPreparedPhase] = []
    bindings: list[runner._RegisteredActualPhaseTwoLaunchBinding] = []
    callback_code: object | None = None
    callback: Callable[[bytes, int, int, int], str] | None = None
    token_trace: runner.ControlTraceWriter | None = None
    trace_had_shadow = False
    trace_shadow: object | None = None
    token_callback_calls: list[object] = []
    token_trace_calls: list[object] = []
    original_prepare = runner._prepare_registered_actual_phase_two

    def prepare_then_poison_exposed_token(
        *args: object,
        **kwargs: object,
    ) -> runner._RegisteredActualPreparedPhase:
        nonlocal callback_code, callback, token_trace, trace_had_shadow, trace_shadow
        phase = original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        binding = runner._registered_actual_phase_two_launch_binding_for(phase)
        prepared_phases.append(phase)
        bindings.append(binding)
        callback = phase.on_native_record
        token_trace = phase.trace
        assert binding.trace is not token_trace
        assert binding.private_on_native_record is not callback

        if tamper == "callback-code":
            callback_code = callback.__code__

            def reject_exposed_callback(
                _raw: bytes,
                _byte_start: int,
                _byte_end: int,
                _sequence: int,
            ) -> str:
                raise AssertionError("exposed phase-two token callback was consulted")

            # The public token retains the same callable identity, but its
            # behavior is hostile.  Restore this global function code in the
            # enclosing finally block even if the launch fixture fails.
            assert callback.__code__.co_freevars == ()
            assert reject_exposed_callback.__code__.co_freevars == ()
            callback.__code__ = reject_exposed_callback.__code__
            return phase

        assert tamper == "trace-method"
        trace_had_shadow = "_append_expected_native_trace" in token_trace.__dict__
        trace_shadow = token_trace.__dict__.get("_append_expected_native_trace")
        assert not trace_had_shadow

        def reject_exposed_trace_append(*args: object, **kwargs: object) -> object:
            token_trace_calls.append((args, kwargs))
            raise AssertionError("exposed phase-two token trace was consulted")

        # This shadows only the returned trace instance; the actual private
        # launch binding owns a distinct trace object for native rows.
        setattr(token_trace, "_append_expected_native_trace", reject_exposed_trace_append)
        return phase

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_phase_two", prepare_then_poison_exposed_token,
    )
    phase_one: runner._RegisteredActualPreparedPhase | None = None
    try:
        (
            phase_one,
            _diagnostic,
            _receipt_gate,
            phase_two,
            launched,
            probe_argvs,
            launches,
        ) = _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)

        assert prepared_phases == [phase_two]
        assert len(bindings) == 1
        assert phase_two.marker_workspace_snapshots
        assert phase_two.trace.path.exists()
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(launches) == 1
        assert token_callback_calls == []
        assert token_trace_calls == []
    finally:
        if callback is not None and callback_code is not None:
            callback.__code__ = callback_code  # type: ignore[assignment]
        if token_trace is not None:
            if trace_had_shadow:
                setattr(token_trace, "_append_expected_native_trace", trace_shadow)
            elif "_append_expected_native_trace" in token_trace.__dict__:
                delattr(token_trace, "_append_expected_native_trace")
        if phase_one is not None:
            phase_one.control_pin.close()
            phase_one.workspace_pin.close()


def test_actual_phase_two_private_post_launch_capture_ignores_exposed_trace_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token-trace method poisoned after Popen cannot redirect private capture."""

    prepared_phases: list[runner._RegisteredActualPreparedPhase] = []
    bindings: list[runner._RegisteredActualPhaseTwoLaunchBinding] = []
    original_prepare = runner._prepare_registered_actual_phase_two
    token_trace: runner.ControlTraceWriter | None = None
    trace_calls: list[object] = []
    mutation_calls: list[object] = []

    def prepare_then_capture_phase(
        *args: object,
        **kwargs: object,
    ) -> runner._RegisteredActualPreparedPhase:
        phase = original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        prepared_phases.append(phase)
        bindings.append(runner._registered_actual_phase_two_launch_binding_for(phase))
        return phase

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_phase_two", prepare_then_capture_phase,
    )
    phase_one: runner._RegisteredActualPreparedPhase | None = None
    try:
        def poison_token_trace_after_popen(
            _argv: list[str],
            _kwargs: dict[str, object],
        ) -> None:
            nonlocal token_trace
            assert len(prepared_phases) == 1
            assert len(bindings) == 1
            phase = prepared_phases[0]
            token_trace = phase.trace
            assert bindings[0].trace is not token_trace
            assert "_append_expected_native_trace" not in token_trace.__dict__

            def reject_exposed_trace_append(*args: object, **kwargs: object) -> object:
                trace_calls.append((args, kwargs))
                raise AssertionError("post-Popen exposed trace facade was consulted")

            setattr(token_trace, "_append_expected_native_trace", reject_exposed_trace_append)
            mutation_calls.append(token_trace)

        (
            phase_one,
            _diagnostic,
            _receipt_gate,
            phase_two,
            launched,
            probe_argvs,
            launches,
        ) = _task7d_actual_phase_two_successful_fake_launch(
            tmp_path,
            monkeypatch,
            on_popen=poison_token_trace_after_popen,
        )

        assert mutation_calls == [phase_two.trace]
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert type(launched.post_launch_token) is runner._RegisteredActualPhaseTwoPostLaunchToken
        assert phase_two.marker_workspace_snapshots
        assert phase_two.trace.path.exists()
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(launches) == 1
        assert trace_calls == []
    finally:
        if token_trace is not None and "_append_expected_native_trace" in token_trace.__dict__:
            delattr(token_trace, "_append_expected_native_trace")
        if phase_one is not None:
            phase_one.control_pin.close()
            phase_one.workspace_pin.close()


def test_actual_phase_two_post_launch_verifier_uses_canonical_trace_not_exposed_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later provenance verification rebuilds trace authority from the pinned root."""

    phase_one, diagnostic, receipt_gate, phase_two, launched, _probes, _launches = (
        _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    token_trace = phase_two.trace
    original_path = token_trace.path
    original_lock_path = token_trace.lock_path
    canonical_traces: list[runner.ControlTraceWriter] = []
    original_canonical_trace = runner._registered_actual_phase_two_canonical_retained_trace

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        object.__setattr__(
            token_trace,
            "path",
            writer.control_root / "attacker-selected-runner-trace.jsonl",
        )
        object.__setattr__(
            token_trace,
            "lock_path",
            writer.control_root / ".attacker-selected-runner-trace.lock",
        )
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)

        def record_canonical_trace(
            phase: runner._RegisteredActualPreparedPhase,
            *,
            live_phase: runner.RegisteredLivePhase | None = None,
        ) -> runner.ControlTraceWriter:
            trace = original_canonical_trace(phase, live_phase=live_phase)
            canonical_traces.append(trace)
            assert phase is phase_two
            assert live_phase is launched.live_phase
            assert trace.control_pin is live_phase.control_pin
            assert trace is not token_trace
            assert trace.path == original_path
            assert trace.lock_path == original_lock_path
            return trace

        monkeypatch.setattr(
            runner,
            "_registered_actual_phase_two_canonical_retained_trace",
            record_canonical_trace,
        )
        runner._verify_registered_actual_phase_two_post_launch_provenance(
            prepared_run,
            diagnostic,
            receipt_gate,
            phase_two,
            launched.live_phase,
            launched.post_launch_token,
        )

        assert canonical_traces
        assert token_trace.path != original_path
        assert token_trace.lock_path != original_lock_path
        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
    finally:
        object.__setattr__(token_trace, "path", original_path)
        object.__setattr__(token_trace, "lock_path", original_lock_path)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rejects_executable_swap_despite_exposed_context_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged public executable context cannot rebaseline the private launch."""

    from tests.evals import event_log_contract as contract

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    phase_two: runner._RegisteredActualPreparedPhase | None = None
    executable = prepared_run.authority.plan.registration.resolved_path
    original_executable_raw = executable.read_bytes()
    original_executable_mode = stat.S_IMODE(executable.stat().st_mode)
    original_context_executables: Mapping[str, object] | None = None
    original_context_builds: Mapping[tuple[str, str, str, str], str] | None = None
    original_phase_two_identity: Mapping[str, int] | None = None
    original_phase_two_digest: str | None = None
    original_phase_two_build: str | None = None
    public_context: object | None = None
    public_phase_two: object | None = None
    replacement_installed = False
    context_mutated = False

    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            return receipt_gate

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader,
        )
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        binding = runner._registered_actual_phase_two_launch_binding_for(phase_two)
        public_context = phase_two.trusted_context
        assert type(public_context) is contract.TrustedRunContext
        assert binding.token_trusted_context is public_context
        assert binding.trusted_context is not public_context
        public_phase_two = public_context.trusted_executables["execution-2"]
        assert type(public_phase_two) is contract.TrustedExecutable
        original_context_executables = public_context.trusted_executables
        original_context_builds = public_context.supported_builds
        original_phase_two_identity = public_phase_two.identity
        original_phase_two_digest = public_phase_two.sha256
        original_phase_two_build = public_phase_two.supported_build_id
        original_inode = executable.stat().st_ino

        replacement = executable.with_name(executable.name + ".post-prepare-forged")
        replacement.write_bytes(b"#!/bin/sh\nprintf 'forged client\\n'\n")
        replacement.chmod(original_executable_mode)
        os.replace(replacement, executable)
        replacement_installed = True
        assert executable.stat().st_ino != original_inode
        forged_raw, forged_identity = contract._read_executable(str(executable))
        forged_digest = hashlib.sha256(forged_raw).hexdigest()
        assert forged_digest != original_phase_two_digest
        forged_build = "claude-test-build-forged-v2"

        # Rebuild only the exposed token facade so every public executable
        # field and supported-build lookup describes the swapped path.  The
        # detached launch binding deliberately keeps its own original clones.
        public_phase_one = public_context.trusted_executables["execution-1"]
        assert type(public_phase_one) is contract.TrustedExecutable
        forged_phase_one = contract.TrustedExecutable(
            "execution-1",
            "claude",
            executable,
            forged_identity,
            forged_digest,
            forged_build,
        )
        object.__setattr__(public_phase_two, "identity", MappingProxyType(dict(forged_identity)))
        object.__setattr__(public_phase_two, "sha256", forged_digest)
        object.__setattr__(public_phase_two, "supported_build_id", forged_build)
        assert public_phase_two.__post_init__() is None
        forged_builds = dict(public_context.supported_builds)
        forged_builds[(
            "claude",
            prepared_run.authority.plan.registration.expected_version,
            prepared_run.authority.plan.registration.native_format,
            forged_digest,
        )] = forged_build
        object.__setattr__(
            public_context,
            "trusted_executables",
            MappingProxyType({
                "execution-1": forged_phase_one,
                "execution-2": public_phase_two,
            }),
        )
        object.__setattr__(public_context, "supported_builds", MappingProxyType(forged_builds))
        assert public_context.__post_init__() is None
        context_mutated = True
        assert public_context.trusted_executables["execution-2"] is public_phase_two
        assert public_context.trusted_executables["execution-2"].identity == forged_identity
        assert public_context.trusted_executables["execution-2"].sha256 == forged_digest
        assert public_context.trusted_executables["execution-2"].supported_build_id == forged_build
        assert public_context.supported_builds[(
            "claude",
            prepared_run.authority.plan.registration.expected_version,
            prepared_run.authority.plan.registration.native_format,
            forged_digest,
        )] == forged_build
        assert binding.trusted_context.trusted_executables["execution-2"] is not public_phase_two
        before_entries = [dict(entry) for entry in writer.entries]
        generic_calls: list[object] = []
        probe_calls: list[object] = []
        popen_calls: list[object] = []

        def no_generic(*args: object, **kwargs: object) -> object:
            generic_calls.append((args, kwargs))
            pytest.fail("forged public executable context reached generic launch")

        def no_probe(*args: object, **kwargs: object) -> object:
            probe_calls.append((args, kwargs))
            pytest.fail("forged public executable context reached phase-two probe")

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("forged public executable context reached phase-two Popen")

        monkeypatch.setattr(runner, "_launch_registered_live_phase", no_generic)
        monkeypatch.setattr(runner, "_registered_probe", no_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError, match="registered executable changed during evaluation"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert generic_calls == []
        assert probe_calls == []
        assert popen_calls == []
        assert writer.entries == before_entries
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        if replacement_installed:
            restoration = executable.with_name(executable.name + ".test-restore")
            restoration.write_bytes(original_executable_raw)
            restoration.chmod(original_executable_mode)
            os.replace(restoration, executable)
        if (context_mutated
                and type(public_context) is contract.TrustedRunContext
                and type(public_phase_two) is contract.TrustedExecutable
                and original_context_executables is not None
                and original_context_builds is not None
                and original_phase_two_identity is not None
                and original_phase_two_digest is not None
                and original_phase_two_build is not None):
            object.__setattr__(public_phase_two, "identity", original_phase_two_identity)
            object.__setattr__(public_phase_two, "sha256", original_phase_two_digest)
            object.__setattr__(public_phase_two, "supported_build_id", original_phase_two_build)
            object.__setattr__(public_context, "trusted_executables", original_context_executables)
            object.__setattr__(public_context, "supported_builds", original_context_builds)
            # The restoration itself is another atomic replacement, so it
            # necessarily has a new inode and cannot satisfy the pre-attack
            # executable identity.  The disposable token is not reused.
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_passes_detached_context_to_generic_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic launcher receives private context, not the token facade."""

    from tests.evals import event_log_contract as contract

    original_prepare = runner._prepare_registered_actual_phase_two
    original_generic_launch = runner._launch_registered_live_phase
    bindings: list[runner._RegisteredActualPhaseTwoLaunchBinding] = []
    public_context: object | None = None
    original_context_builds: Mapping[tuple[str, str, str, str], str] | None = None
    context_mutated = False
    generic_contexts: list[object] = []
    facade_only_key = (
        "codex",
        "test-facade-version",
        "test-facade-format",
        "f" * 64,
    )
    facade_only_build = "codex-test-facade-build"

    def prepare_then_mutate_public_context(
        *args: object,
        **kwargs: object,
    ) -> runner._RegisteredActualPreparedPhase:
        nonlocal public_context, original_context_builds, context_mutated
        phase = original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        binding = runner._registered_actual_phase_two_launch_binding_for(phase)
        context = phase.trusted_context
        assert type(context) is contract.TrustedRunContext
        phase_two_executable = context.trusted_executables["execution-2"]
        assert type(phase_two_executable) is contract.TrustedExecutable
        public_context = context
        original_context_builds = context.supported_builds
        facade_builds = dict(context.supported_builds)
        facade_builds[facade_only_key] = facade_only_build
        object.__setattr__(context, "supported_builds", MappingProxyType(facade_builds))
        assert context.__post_init__() is None
        context_mutated = True
        assert binding.token_trusted_context is context
        assert binding.trusted_context is not context
        assert binding.trusted_context.trusted_executables["execution-2"] is not phase_two_executable
        assert context.supported_builds[facade_only_key] == facade_only_build
        assert facade_only_key not in binding.trusted_context.supported_builds

        def observe_generic_launch(*inner_args: object, **inner_kwargs: object) -> object:
            generic_context = inner_kwargs.get("trusted_context")
            generic_contexts.append(generic_context)
            assert generic_context is binding.trusted_context
            assert generic_context is not context
            assert generic_context.trusted_executables["execution-2"] is not phase_two_executable
            assert facade_only_key not in generic_context.supported_builds
            return original_generic_launch(*inner_args, **inner_kwargs)  # type: ignore[arg-type]

        # This runs only after phase one and phase-two preparation have both
        # completed, so the observation is limited to the private phase-two
        # generic-launch boundary under test.
        monkeypatch.setattr(runner, "_launch_registered_live_phase", observe_generic_launch)
        bindings.append(binding)
        return phase

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_phase_two", prepare_then_mutate_public_context,
    )
    phase_one: runner._RegisteredActualPreparedPhase | None = None
    try:
        (
            phase_one,
            _diagnostic,
            _receipt_gate,
            phase_two,
            launched,
            probe_argvs,
            launches,
        ) = _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)

        assert len(bindings) == 1
        assert len(generic_contexts) == 1
        assert generic_contexts[0] is bindings[0].trusted_context
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(launches) == 1
        assert phase_two.trusted_context is public_context
        assert phase_two.marker_workspace_snapshots
    finally:
        if (context_mutated
                and type(public_context) is contract.TrustedRunContext
                and original_context_builds is not None):
            object.__setattr__(public_context, "supported_builds", original_context_builds)
            assert public_context.__post_init__() is None
        if phase_one is not None:
            phase_one.control_pin.close()
            phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_passes_detached_pins_to_generic_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private generic boundary must not receive token-owned root pins."""

    original_prepare = runner._prepare_registered_actual_phase_two
    original_generic_launch = runner._launch_registered_live_phase
    bindings: list[tuple[
        runner._RegisteredActualPreparedPhase,
        runner._RegisteredActualPhaseTwoLaunchBinding,
    ]] = []
    generic_pin_pairs: list[tuple[runner.PinnedDirectory, runner.PinnedDirectory]] = []

    def prepare_then_capture_binding(
        *args: object,
        **kwargs: object,
    ) -> runner._RegisteredActualPreparedPhase:
        phase = original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        binding = runner._registered_actual_phase_two_launch_binding_for(phase)

        def observe_generic_launch(*inner_args: object, **inner_kwargs: object) -> object:
            control_pin = inner_kwargs.get("control_pin")
            workspace_pin = inner_kwargs.get("workspace_pin")
            assert type(control_pin) is runner.PinnedDirectory
            assert type(workspace_pin) is runner.PinnedDirectory
            generic_pin_pairs.append((control_pin, workspace_pin))
            assert control_pin is binding.control_pin
            assert workspace_pin is binding.workspace_pin
            assert control_pin is not phase.control_pin
            assert workspace_pin is not phase.workspace_pin
            assert control_pin.path == phase.control_pin.path
            assert workspace_pin.path == phase.workspace_pin.path
            return original_generic_launch(*inner_args, **inner_kwargs)  # type: ignore[arg-type]

        # Preparation finishes before this replacement, so the observer is
        # limited to the actual phase-two private generic-launch boundary.
        monkeypatch.setattr(runner, "_launch_registered_live_phase", observe_generic_launch)
        bindings.append((phase, binding))
        return phase

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_phase_two", prepare_then_capture_binding,
    )
    phase_one: runner._RegisteredActualPreparedPhase | None = None
    try:
        (
            phase_one,
            _diagnostic,
            _receipt_gate,
            phase_two,
            launched,
            probe_argvs,
            launches,
        ) = _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)

        assert len(bindings) == 1
        assert generic_pin_pairs == [
            (bindings[0][1].control_pin, bindings[0][1].workspace_pin),
        ]
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(launches) == 1
        assert phase_two is bindings[0][0]
    finally:
        if phase_one is not None:
            phase_one.control_pin.close()
            phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rereads_phase_one_without_token_pin_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-one rereads use retained roots, never token pin methods.

    Preparation returns the phase-two aggregate before the actual launcher
    performs several phase-one evidence and receipt rereads.  Those reads
    must remain on the binding-owned descriptor pair even when every normal
    reader method on the returned aggregate's pins has been made hostile.
    """

    original_prepare = runner._prepare_registered_actual_phase_two
    original_generic_launch = runner._launch_registered_live_phase
    original_evidence_bound = runner._registered_phase_evidence_is_bound
    prepared_bindings: list[runner._RegisteredActualPhaseTwoLaunchBinding] = []
    shadowed_methods: list[tuple[runner.PinnedDirectory, str]] = []
    shadow_calls: list[str] = []
    phase_one_evidence_rereads: list[object] = []
    phase_one_receipt_rereads: list[object] = []
    generic_calls: list[object] = []

    def poison_method(root_name: str, method_name: str) -> Callable[..., None]:
        def poisoned(*_args: object, **_kwargs: object) -> None:
            shadow_calls.append(root_name + "." + method_name)
            pytest.fail(
                "phase-two launch consulted a returned token pin method: "
                + shadow_calls[-1],
            )

        return poisoned

    def prepare_then_shadow_returned_pins(
        *args: object,
        **kwargs: object,
    ) -> runner._RegisteredActualPreparedPhase:
        phase = original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        binding = runner._registered_actual_phase_two_launch_binding_for(phase)
        assert binding.token_control_pin is phase.control_pin
        assert binding.token_workspace_pin is phase.workspace_pin
        assert binding.control_pin is not phase.control_pin
        assert binding.workspace_pin is not phase.workspace_pin

        # The successful-launch fixture installs its sealed receipt reader
        # just before it calls preparation.  Wrap that exact reader only
        # after the returned token exists, so these observations prove the
        # actual launch's phase-one rereads rather than setup work.
        sealed_reader = runner._reconstruct_actual_phase_one_receipt_gate

        def observe_phase_one_receipt(*inner_args: object, **inner_kwargs: object) -> object:
            if inner_kwargs.get("execution_id") == "execution-1":
                phase_one_receipt_rereads.append((inner_args, inner_kwargs))
            return sealed_reader(*inner_args, **inner_kwargs)

        def observe_phase_one_evidence(*inner_args: object, **inner_kwargs: object) -> bool:
            if inner_kwargs.get("execution_id") == "execution-1":
                phase_one_evidence_rereads.append((inner_args, inner_kwargs))
            return original_evidence_bound(*inner_args, **inner_kwargs)

        def observe_generic_launch(*inner_args: object, **inner_kwargs: object) -> object:
            generic_calls.append((inner_args, inner_kwargs))
            return original_generic_launch(*inner_args, **inner_kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", observe_phase_one_receipt,
        )
        monkeypatch.setattr(
            runner, "_registered_phase_evidence_is_bound", observe_phase_one_evidence,
        )
        monkeypatch.setattr(runner, "_launch_registered_live_phase", observe_generic_launch)
        for root_name, pin in (
                ("control", phase.control_pin),
                ("workspace", phase.workspace_pin)):
            for method_name in (
                    "verify", "read_relative", "snapshot_relative",
                    "reverify_snapshot", "snapshot_tree", "reverify_tree"):
                assert method_name not in pin.__dict__
                object.__setattr__(
                    pin, method_name, poison_method(root_name, method_name),
                )
                shadowed_methods.append((pin, method_name))
        prepared_bindings.append(binding)
        return phase

    monkeypatch.setattr(
        runner, "_prepare_registered_actual_phase_two", prepare_then_shadow_returned_pins,
    )
    phase_one: runner._RegisteredActualPreparedPhase | None = None
    try:
        (
            phase_one,
            _diagnostic,
            _receipt_gate,
            phase_two,
            launched,
            probe_argvs,
            launches,
        ) = _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)

        assert len(prepared_bindings) == 1
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(generic_calls) == 1
        assert len(launches) == 1
        assert phase_one_evidence_rereads
        assert phase_one_receipt_rereads
        assert shadow_calls == []
        assert phase_two.control_pin is prepared_bindings[0].token_control_pin
        assert phase_two.workspace_pin is prepared_bindings[0].token_workspace_pin
    finally:
        for pin, method_name in reversed(shadowed_methods):
            if method_name in pin.__dict__:
                object.__delattr__(pin, method_name)
        if phase_one is not None:
            phase_one.control_pin.close()
            phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_reconstructs_receipt_after_probes_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt reader must run at the actual Popen boundary, not setup only."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    probe_argvs: list[list[str]] = []
    help_complete = False
    receipt_reader_states: list[tuple[tuple[str, ...], bool]] = []
    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            receipt_reader_states.append((
                tuple(argv[-1] for argv in probe_argvs), help_complete,
            ))
            return receipt_gate

        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal help_complete
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                help_complete = True
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        popen_calls: list[object] = []

        def stop_at_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            # The initial phase-two check and this post-probe check must both
            # happen.  Observing the latter inside the Popen double proves
            # it is a launch-bound re-read rather than a setup-only call.
            assert len(receipt_reader_states) >= 3
            assert receipt_reader_states[0] == ((), False)
            assert receipt_reader_states[-1] == (("--version", "--help"), True)
            raise runner.RunnerError("test reached controlled Popen boundary")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", stop_at_popen)

        with pytest.raises(runner.RunnerError, match="test reached controlled Popen boundary"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert help_complete is True
        assert len(popen_calls) == 1
        assert receipt_reader_states[-1] == (("--version", "--help"), True)
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_rechecks_receipt_after_pre_popen_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid-shaped gate mutation after the final tree read blocks ledger allocation."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    original_tree_is_open = runner._registered_actual_phase_two_launch_tree_is_open
    original_facts = receipt_gate.receipt_facts
    replacement_effect_digest = (
        "0" * 64 if original_facts.effect_digest != "0" * 64 else "f" * 64
    )
    replacement_facts = replace(
        original_facts, effect_digest=replacement_effect_digest,
    )
    baseline_gate = replace(receipt_gate)

    try:
        assert baseline_gate is not receipt_gate
        assert baseline_gate.receipt_facts is original_facts
        assert replacement_facts.selection is original_facts.selection
        assert replacement_facts.effect_digest != original_facts.effect_digest
        receipt_reader_calls: list[dict[str, object]] = []

        def sealed_reader(**kwargs: object) -> runner.PhaseOneReceiptGate:
            receipt_reader_calls.append(dict(kwargs))
            return baseline_gate

        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        probe_argvs: list[list[str]] = []
        tree_calls: list[bool] = []
        reader_calls_before_mutation: int | None = None
        mutations: list[object] = []

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--version":
                output = b"2.1.251 (test Claude)\n"
            else:
                assert argv[-1] == "--help"
                output = b"stream-json help\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        def tree_then_mutate_gate(
            observed_phase: runner._RegisteredActualPreparedPhase,
            *,
            pre_popen: bool,
            control_pin: runner.PinnedDirectory | None = None,
        ) -> None:
            nonlocal reader_calls_before_mutation
            original_tree_is_open(
                observed_phase, pre_popen=pre_popen, control_pin=control_pin,
            )
            tree_calls.append(pre_popen)
            if pre_popen:
                assert observed_phase is phase_two
                assert not phase_two.trace.path.exists()
                assert all(
                    not path.exists()
                    for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
                )
                reader_calls_before_mutation = len(receipt_reader_calls)
                object.__setattr__(receipt_gate, "receipt_facts", replacement_facts)
                assert receipt_gate.__post_init__() is None
                mutations.append(receipt_gate.receipt_facts)

        popen_calls: list[object] = []

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("post-tree receipt mutation reached phase-two Popen")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(
            runner,
            "_registered_actual_phase_two_launch_tree_is_open",
            tree_then_mutate_gate,
        )
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)
        with pytest.raises(runner.RunnerError, match="receipt gate is no longer valid"):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert tree_calls == [False, True]
        assert reader_calls_before_mutation is not None
        assert len(receipt_reader_calls) == reader_calls_before_mutation + 1
        assert mutations == [replacement_facts]
        assert receipt_gate.receipt_facts is replacement_facts
        assert popen_calls == []
        assert not phase_two.trace.path.exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
    finally:
        object.__setattr__(receipt_gate, "receipt_facts", original_facts)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    "residue_kind",
    ("terminal-log", "evidence-index", "future-trace", "orphan-artifact"),
)
def test_actual_phase_two_private_launcher_rejects_probe_time_residue_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    residue_kind: str,
) -> None:
    """A phase-two probe cannot leave terminal or future-phase residue behind."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    probe_argvs: list[list[str]] = []
    help_complete = False
    receipt_reader_states: list[tuple[tuple[str, ...], bool]] = []
    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            receipt_reader_states.append((
                tuple(argv[-1] for argv in probe_argvs), help_complete,
            ))
            return receipt_gate

        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        if residue_kind == "terminal-log":
            residue_path = writer.run_root / "event-log.json"
        elif residue_kind == "evidence-index":
            residue_path = writer.run_root / "evidence-index.json"
        elif residue_kind == "future-trace":
            residue_path = writer.control_root / "runner-trace-execution-3.jsonl"
        else:
            assert residue_kind == "orphan-artifact"
            # This has no EvidenceWriter entry.  It must fail the exact
            # pinned run-tree inventory rather than being ignored merely
            # because it is not a known terminal/future-phase filename.
            residue_path = writer.run_root / "orphan-artifact.bin"
        residue_raw = b'{"probe_time_residue":true}\n'

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal help_complete
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                residue_path.write_bytes(residue_raw)
                help_complete = True
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        popen_calls: list[object] = []

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("probe-time phase-two residue reached Popen")

        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)

        with pytest.raises(
            runner.RunnerError,
            match="phase-two launch|sealed|static control tree|trace namespace|output inventory",
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert help_complete is True
        assert residue_path.read_bytes() == residue_raw
        assert popen_calls == []
        if residue_kind == "future-trace":
            # The private binding's exact static-tree replay rejects a future
            # root trace before receipt reconstruction runs again.
            assert receipt_reader_states[-1] == ((), False)
        else:
            assert receipt_reader_states[-1] == (("--version", "--help"), True)
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_private_launcher_final_tail_rechecks_after_late_context_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation after the late phase-two context read still blocks Popen."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    probe_argvs: list[list[str]] = []
    help_complete = False
    receipt_reader_states: list[tuple[tuple[str, ...], bool, bool]] = []
    mutation_complete = False
    reader_calls_before_mutation: int | None = None
    try:
        def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
            receipt_reader_states.append((
                tuple(argv[-1] for argv in probe_argvs), help_complete, mutation_complete,
            ))
            return receipt_gate

        monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
        phase_two = runner._prepare_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate,
        )
        entries = {entry["id"]: entry for entry in writer.entries}
        policy_path = writer.run_root / str(entries["policy-1"]["relative_path"])
        original_context = runner._registered_live_context_phase

        def mutate_after_late_phase_two_context(**kwargs: object) -> object:
            nonlocal mutation_complete, reader_calls_before_mutation
            result = original_context(**kwargs)  # type: ignore[arg-type]
            if (help_complete
                    and kwargs.get("execution_id") == "execution-2"
                    and not mutation_complete):
                # This hook runs inside the pre-Popen revalidator after its
                # first phase-one receipt/binding pass.  Only the final tail
                # can observe this mutation before process creation.
                policy_path.write_bytes(b'{"late_context_read_mutation":true}\n')
                reader_calls_before_mutation = len(receipt_reader_states)
                mutation_complete = True
            return result

        def phase_two_probe(
            *,
            argv: list[str],
            cwd: Path,
            environment: dict[str, str],
            probe_runner: object,
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal help_complete
            assert cwd == prepared_run.prepared.workspace
            assert probe_runner is None
            assert environment[runner.CONTROL_ENV] == str(writer.control_root)
            probe_argvs.append(list(argv))
            if argv[-1] == "--help":
                help_complete = True
                output = b"stream-json help\n"
            else:
                assert argv[-1] == "--version"
                output = b"2.1.251 (test Claude)\n"
            return subprocess.CompletedProcess(argv, 0, output, b"")

        popen_calls: list[object] = []

        def no_popen(*args: object, **kwargs: object) -> object:
            popen_calls.append((args, kwargs))
            pytest.fail("late revalidation mutation reached phase-two Popen")

        monkeypatch.setattr(
            runner, "_registered_live_context_phase", mutate_after_late_phase_two_context,
        )
        monkeypatch.setattr(runner, "_registered_probe", phase_two_probe)
        monkeypatch.setattr(runner.subprocess, "Popen", no_popen)

        with pytest.raises(
            runner.RunnerError,
            match="phase-one|evidence|bound|frozen control artifact changed before diagnostic adoption",
        ):
            runner._launch_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate, phase_two,
            )

        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert help_complete is True
        assert mutation_complete is True
        assert reader_calls_before_mutation is not None
        assert reader_calls_before_mutation >= 4
        # The frozen phase-one control bridge rejects the late policy rewrite
        # before a further receipt reconstruction can observe probe state.
        assert receipt_reader_states[-1] == ((), False, False)
        assert popen_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def _task7d_actual_phase_two_successful_fake_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_wait: Callable[[runner.EvidenceWriter], None] | None = None,
    on_popen: Callable[[list[str], dict[str, object]], None] | None = None,
    close_pins_after_launch: bool = False,
) -> tuple[
    runner._RegisteredActualPreparedPhase,
    runner._RegisteredActualPhaseOneDiagnostic,
    runner.PhaseOneReceiptGate,
    runner._RegisteredActualPreparedPhase,
    object,
    list[list[str]],
    list[tuple[list[str], dict[str, object]]],
]:
    """Launch phase two only through local pipes, never a real client or web.

    This deliberately stops at the private retained launch wrapper.  Its
    Claude-shaped stdout is a parser fixture with no phase-two markers,
    commands, capability, semantic, log, or pass work, so it supplies the
    smallest successful post-launch provenance boundary for these tests.
    """

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer

    def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
        # Phase-two setup supersedes the phase-one state/config files before
        # its historical pre-Popen reader runs.  The actual gate was already
        # replayed from the interleaved phase-one trace above; post-launch
        # provenance deliberately uses the new frozen receipt facade instead.
        return receipt_gate

    monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
    phase_two = runner._prepare_registered_actual_phase_two(
        prepared_run, diagnostic, receipt_gate,
    )
    transcript = _claude_transcript(
        [], cwd=str(prepared_run.prepared.workspace.resolve()),
    )
    probe_argvs: list[list[str]] = []
    launches: list[tuple[list[str], dict[str, object]]] = []

    class CompleteProcess:
        def __init__(self) -> None:
            self.stdin = _Task7dRecordingInput()
            self.stdout = _task7d_closed_pipe(transcript)
            self.stderr = _task7d_closed_pipe(b"local phase-two fixture\n")

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            assert self.stdout.closed and self.stderr.closed
            if on_wait is not None:
                on_wait(writer)
            return 0

    def probe(
        *,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd == prepared_run.prepared.workspace
        assert probe_runner is None
        assert environment[runner.CONTROL_ENV] == str(writer.control_root)
        assert environment[runner.RUNNER_STATE_ENV] == str(phase_two.runner_state.path)
        probe_argvs.append(list(argv))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"2.1.251 (local fixture)\n", b"")
        assert argv[-1] == "--help"
        return subprocess.CompletedProcess(argv, 0, b"stream-json help\n", b"")

    def popen(argv: list[str], **kwargs: object) -> CompleteProcess:
        assert argv[0] == str(prepared_run.authority.plan.registration.resolved_path)
        assert kwargs["cwd"] == prepared_run.prepared.workspace
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment[runner.CONTROL_ENV] == str(writer.control_root)
        launch_argv = list(argv)
        launch_kwargs = dict(kwargs)
        launches.append((launch_argv, launch_kwargs))
        if on_popen is not None:
            on_popen(launch_argv, launch_kwargs)
        return CompleteProcess()

    # This local Popen double must not also receive the snapshotter's Git
    # inventory subprocesses.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner, "_registered_probe", probe)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    try:
        launched = runner._launch_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate, phase_two,
        )
    except BaseException:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()
        raise
    if close_pins_after_launch:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()
    return (
        phase_one, diagnostic, receipt_gate, phase_two, launched, probe_argvs, launches,
    )


_TASK7D_PHASE_TWO_RECEIPT_LEDGER_NAMES = {
    "raw": "runner-raw-write-receipts-execution-2.jsonl",
    "trace": "runner-private-trace-receipts-execution-2.jsonl",
}
_TASK7D_PHASE_TWO_RAW_APPEND_TARGETS = {
    "brain-shim-trace.jsonl",
    "brain-shim-capture-index.jsonl",
    "brain-shim-command-log.jsonl",
    "runner-preapply-manifests.jsonl",
}


def _task7d_phase_two_receipt_paths(control_root: Path) -> dict[str, Path]:
    """Return only the two fixed private phase-two receipt ledgers."""

    return {
        kind: control_root / name
        for kind, name in _TASK7D_PHASE_TWO_RECEIPT_LEDGER_NAMES.items()
    }


def _task7d_assert_receipt_ledger_chain(
    path: Path,
    *,
    ledger_kind: str,
    run_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Check the immutable header and canonical sidecar-prefix chain."""

    expected_kind = {
        "raw": "registered_actual_phase_two_raw_write_receipts",
        "trace": "registered_actual_phase_two_private_trace_receipts",
    }[ledger_kind]
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    assert lines and all(line.endswith(b"\n") for line in lines)
    header = json.loads(lines[0])
    assert type(header) is dict
    expected_header_keys = {"schema_version", "kind", "run_id", "execution_id", "nonce"}
    if ledger_kind == "raw":
        expected_header_keys.add("raw_prelaunch")
    assert set(header) == expected_header_keys
    assert header["schema_version"] == 1
    assert header["kind"] == expected_kind
    assert header["run_id"] == run_id
    assert header["execution_id"] == "execution-2"
    assert type(header["nonce"]) is str and len(header["nonce"]) == 32
    if ledger_kind == "raw":
        raw_prelaunch = header["raw_prelaunch"]
        assert type(raw_prelaunch) is dict
        assert set(raw_prelaunch) == _TASK7D_PHASE_TWO_RAW_APPEND_TARGETS
        for predecessor in raw_prelaunch.values():
            if predecessor is None:
                continue
            assert type(predecessor) is dict
            assert set(predecessor) == {"sha256", "byte_count", "identity", "rows"}
            assert type(predecessor["sha256"]) is str
            assert len(predecessor["sha256"]) == 64
            assert type(predecessor["byte_count"]) is int
            assert predecessor["byte_count"] >= 0
            assert type(predecessor["identity"]) is list
            assert len(predecessor["identity"]) == 8
            assert predecessor["identity"][5] == predecessor["byte_count"]
            assert type(predecessor["rows"]) is int
            assert predecessor["rows"] >= 0
    assert lines[0] == _encoded(header) + b"\n"

    prefix = lines[0]
    rows: list[dict[str, object]] = []
    expected_keys = {
        "schema_version", "execution_id", "sequence", "sidecar_prior_sha256",
        "sidecar_prior_bytes", "sidecar_prior_rows", "receipt",
    }
    if ledger_kind == "raw":
        expected_keys.add("command_id")
    for sequence, line in enumerate(lines[1:], start=1):
        row = json.loads(line)
        assert type(row) is dict
        assert line == _encoded(row) + b"\n"
        assert set(row) == expected_keys
        assert row["schema_version"] == 1
        assert row["execution_id"] == "execution-2"
        assert row["sequence"] == sequence
        assert row["sidecar_prior_sha256"] == hashlib.sha256(prefix).hexdigest()
        assert row["sidecar_prior_bytes"] == len(prefix)
        assert row["sidecar_prior_rows"] == sequence - 1
        assert type(row["receipt"]) is dict
        if ledger_kind == "raw":
            assert type(row["command_id"]) is str
            assert row["command_id"].startswith("shim-command-")
        rows.append(row)
        prefix += line
    return header, rows


def _task7d_replace_same_bytes(path: Path) -> tuple[int, int]:
    """Atomically substitute one explicit file with identical bytes."""

    before = path.stat().st_ino
    replacement = path.with_name(path.name + ".same-byte-replacement")
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    after = path.stat().st_ino
    assert after != before
    return before, after


def _task7d_actual_phase_two_receipt_fake_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    nested_shim: bool,
    before_phase_two_prepare: Callable[[runner._RegisteredActualPreparedPhase], None] | None = None,
    nested_argv: tuple[str, ...] | None = None,
    before_launch: Callable[[runner.EvidenceWriter, runner._RegisteredActualPreparedPhase], None] | None = None,
    on_popen: Callable[[runner.EvidenceWriter, runner._RegisteredActualPreparedPhase, dict[str, str]], None] | None = None,
    on_wait: Callable[[runner.EvidenceWriter, runner._RegisteredActualPreparedPhase], None] | None = None,
) -> tuple[
    runner._RegisteredActualPreparedPhase,
    runner._RegisteredActualPhaseOneDiagnostic,
    runner.PhaseOneReceiptGate,
    runner._RegisteredActualPreparedPhase,
    object,
    list[subprocess.CompletedProcess[bytes]],
]:
    """Drive phase two through local pipes, optionally invoking its real shim.

    The producer writes one native row, waits until the parent has durably
    assigned it a trace slot, invokes the copied fixture's ``./brain`` wrapper
    with the exact launch environment, then writes the remaining native rows.
    This gives the receipt tests a deterministic native/shim/native interval
    without selecting an actual client or a public route.
    """

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_replayable_receipt_seed(
        tmp_path, monkeypatch,
    )
    if before_phase_two_prepare is not None:
        before_phase_two_prepare(phase_one)
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    nested_command = ("--json", "sync") if nested_argv is None else nested_argv

    def sealed_reader(**_kwargs: object) -> runner.PhaseOneReceiptGate:
        # See the sibling local-launch fixture: this covers only the legacy
        # pre-Popen reader after phase-two control replacement.  The frozen
        # post-launch receipt boundary remains real and unpatched.
        return receipt_gate

    monkeypatch.setattr(runner, "_reconstruct_actual_phase_one_receipt_gate", sealed_reader)
    phase_two = runner._prepare_registered_actual_phase_two(
        prepared_run, diagnostic, receipt_gate,
    )
    if before_launch is not None:
        before_launch(writer, phase_two)

    native_rows = _claude_transcript(
        [], cwd=str(prepared_run.prepared.workspace.resolve()),
    ).splitlines(keepends=True)
    assert len(native_rows) >= 2
    first_native_appended = threading.Event()
    nested_results: list[subprocess.CompletedProcess[bytes]] = []
    original_accept_native_trace = (
        runner._RegisteredActualPhaseTwoLiveIdentityTracker.accept_native_trace_append
    )

    def note_first_native(
        tracker: runner._RegisteredActualPhaseTwoLiveIdentityTracker,
        receipt: object,
    ) -> None:
        # Wait for the tracker to accept the FD-bound native trace receipt,
        # rather than merely observing an attempted trace append.  This also
        # leaves hostile `_append_expected_native_trace` test seams intact.
        original_accept_native_trace(tracker, receipt)
        if tracker.prepared_phase is phase_two:
            first_native_appended.set()

    monkeypatch.setattr(
        runner._RegisteredActualPhaseTwoLiveIdentityTracker,
        "accept_native_trace_append",
        note_first_native,
    )

    class ReceiptProcess:
        def __init__(self, environment: dict[str, str]) -> None:
            stdout_read, self._stdout_write = os.pipe()
            stderr_read, self._stderr_write = os.pipe()
            self.stdin = _Task7dRecordingInput()
            self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
            self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
            self._environment = dict(environment)
            self.errors: list[BaseException] = []
            self.wait_calls = 0
            self._producer = threading.Thread(target=self._produce, daemon=True)
            self._producer.start()

        def _produce(self) -> None:
            try:
                os.write(self._stdout_write, native_rows[0])
                if nested_shim:
                    if not first_native_appended.wait(
                        timeout=_TASK7D_NATIVE_RECEIPT_BARRIER_TIMEOUT_SECONDS,
                    ):
                        raise AssertionError("first native receipt was not appended")
                    child_argv = [str(prepared_run.prepared.workspace / "brain"), *nested_command]
                    nested_process = _TASK7D_NATIVE_POPEN(
                        child_argv,
                        cwd=prepared_run.prepared.workspace,
                        env=self._environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    nested_stdout, nested_stderr = nested_process.communicate()
                    nested_results.append(subprocess.CompletedProcess(
                        child_argv, nested_process.returncode, nested_stdout, nested_stderr,
                    ))
                for row in native_rows[1:]:
                    os.write(self._stdout_write, row)
            except BaseException as error:
                self.errors.append(error)
            finally:
                os.close(self._stdout_write)
                os.close(self._stderr_write)

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self._producer.join(timeout=5)

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            self.wait_calls += 1
            self._producer.join(timeout=5)
            assert not self._producer.is_alive()
            if self.errors:
                raise self.errors[0]
            assert self.stdout.closed and self.stderr.closed
            if on_wait is not None:
                on_wait(writer, phase_two)
            return 0

        def communicate(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("phase-two receipt fixture must stream live")

    def probe(
        *,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        probe_runner: object,
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd == prepared_run.prepared.workspace
        assert probe_runner is None
        assert environment[runner.CONTROL_ENV] == str(writer.control_root)
        assert environment[runner.RUNNER_STATE_ENV] == str(phase_two.runner_state.path)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"2.1.251 (receipt fixture)\n", b"")
        assert argv[-1] == "--help"
        return subprocess.CompletedProcess(argv, 0, b"stream-json help\n", b"")

    def popen(argv: list[str], **kwargs: object) -> ReceiptProcess:
        assert argv[0] == str(prepared_run.authority.plan.registration.resolved_path)
        assert kwargs["cwd"] == prepared_run.prepared.workspace
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment[runner.CONTROL_ENV] == str(writer.control_root)
        if on_popen is not None:
            on_popen(writer, phase_two, environment)
        return ReceiptProcess(environment)

    # The replayable phase-one sibling scopes its own snapshotter seam; this
    # phase-two fake Popen needs an explicit local equivalent.
    monkeypatch.setattr(runner.WorkspaceSnapshotter, "_staged_paths", lambda _self: ())
    monkeypatch.setattr(runner, "_registered_probe", probe)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    try:
        launched = runner._launch_registered_actual_phase_two(
            prepared_run, diagnostic, receipt_gate, phase_two,
        )
    except BaseException:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()
        raise
    return phase_one, diagnostic, receipt_gate, phase_two, launched, nested_results


def _task7d_run_tree_bytes(writer: runner.EvidenceWriter) -> dict[str, bytes]:
    """Snapshot only test-owned retained evidence to prove verifier no-writes."""

    return {
        path.relative_to(writer.run_root).as_posix(): path.read_bytes()
        for path in sorted(writer.run_root.rglob("*"))
        if path.is_file()
    }


def _task7d_assert_frozen_handoff_snapshot(
    control_root: Path,
    relative: str,
    snapshot: tuple[bytes, tuple[int, int, int, int, int, int, int, int]],
) -> None:
    """Check one test-visible handoff record against its live pinned target."""

    raw, identity = snapshot
    path = control_root / relative
    observed = path.stat()
    assert path.read_bytes() == raw
    assert (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_uid,
        observed.st_nlink,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    ) == identity


def test_actual_phase_two_live_launch_returns_a_frozen_nonserialized_provenance_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful local phase two creates a private post-launch read token only."""

    receipt_signature = inspect.signature(
        runner._reconstruct_actual_phase_one_receipt_gate,
    )
    bound_signature = inspect.signature(runner._registered_phase_evidence_is_bound)
    assert "control_pin" in receipt_signature.parameters
    assert "control_pin" in bound_signature.parameters
    phase_one, diagnostic, receipt_gate, phase_two, launched, probe_argvs, launches = (
        _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    try:
        signature = inspect.signature(runner._launch_registered_actual_phase_two)
        assert tuple(signature.parameters) == (
            "prepared_run", "phase_one_diagnostic", "receipt_gate", "prepared_phase",
        )
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        live_phase = launched.live_phase
        token = launched.post_launch_token
        assert type(live_phase) is runner.RegisteredLivePhase
        assert type(token) is runner._RegisteredActualPhaseTwoPostLaunchToken
        assert type(token.receipt_control) is runner._FrozenActualPhaseOneReceiptControl
        assert type(token.receipt_control.artifacts) is runner._FrozenActualPhaseOneReceiptArtifacts
        assert live_phase.control_pin is not phase_two.control_pin
        assert live_phase.executable_id == "client-executable-2"
        assert token.execution_id == "execution-2"
        assert token.artifact_provenance
        assert token.trace_sha256 == hashlib.sha256(phase_two.trace.path.read_bytes()).hexdigest()
        assert token.snapshot_artifact_ids
        assert set(token.snapshot_artifact_ids) == {
            phase_two.initial_workspace_snapshot.artifact_id,
            *(snapshot.artifact_id for snapshot in phase_two.marker_workspace_snapshots),
        }
        with pytest.raises((FrozenInstanceError, AttributeError)):
            token.execution_id = "execution-3"  # type: ignore[misc]
        with pytest.raises(TypeError):
            json.dumps(token)
        for private_value in (
            token.receipt_control,
            token.receipt_control.artifacts,
        ):
            with pytest.raises(TypeError, match="not serializable"):
                pickle.dumps(private_value)

        entries = {entry["id"]: entry for entry in writer.entries}
        assert {"version-2", "help-2", "client-executable-2"} <= set(entries)
        assert phase_two.initial_workspace_snapshot.artifact_id in entries
        assert phase_two.marker_workspace_snapshots
        assert phase_two.trace.path.exists()
        # Phase one already owns its diagnostic policy/process rows.  This
        # disconnected success must not turn the newly live phase two into
        # indexed policy/process/semantic evidence of its own.
        assert {
            "policy-2", "process-2", "transcript-2", "stderr-2",
            "trace-execution-2", "event-record-2", "approval-2",
            "fixture-capability-2", "run-manifest-2",
        }.isdisjoint(entries)
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert [argv[-1] for argv in probe_argvs] == ["--version", "--help"]
        assert len(launches) == 1

        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)
        frozen_receipt_controls: list[object] = []
        legacy_receipt_calls: list[object] = []
        bound_pins: list[object] = []
        original_frozen_receipt_reader = (
            runner._reconstruct_frozen_actual_phase_one_receipt_gate
        )
        original_bound = runner._registered_phase_evidence_is_bound

        def pinned_frozen_receipt_reader(**kwargs: object) -> object:
            frozen_receipt_controls.append(kwargs.get("control"))
            assert kwargs.get("control") is token.receipt_control
            return original_frozen_receipt_reader(**kwargs)

        def reject_legacy_receipt_reader(*_args: object, **_kwargs: object) -> object:
            legacy_receipt_calls.append("called")
            pytest.fail("post-launch verifier re-read the mutable phase-one receipt path")

        def pinned_phase_one_bound(**kwargs: object) -> bool:
            bound_pins.append(kwargs.get("control_pin"))
            assert kwargs.get("control_pin") is live_phase.control_pin
            return original_bound(**kwargs)

        monkeypatch.setattr(
            runner,
            "_reconstruct_frozen_actual_phase_one_receipt_gate",
            pinned_frozen_receipt_reader,
        )
        monkeypatch.setattr(
            runner, "_reconstruct_actual_phase_one_receipt_gate", reject_legacy_receipt_reader,
        )
        monkeypatch.setattr(runner, "_registered_phase_evidence_is_bound", pinned_phase_one_bound)
        verifier = runner._verify_registered_actual_phase_two_post_launch_provenance
        verifier(prepared_run, diagnostic, receipt_gate, phase_two, live_phase, token)
        assert frozen_receipt_controls
        assert legacy_receipt_calls == []
        assert bound_pins
        assert all(control is token.receipt_control for control in frozen_receipt_controls)
        assert all(pin is live_phase.control_pin for pin in bound_pins)
        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_launcher_rejects_same_byte_live_evidence_replaced_during_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait-time replacement cannot become the post-launch token baseline.

    ``version-2`` exists before the fake process reaches ``wait()``; the
    process has already drained and the runner has not yet returned a live
    wrapper or constructed its post-launch token.  Replacing it with identical
    bytes at this point therefore exercises the launch-to-token TOCTOU gap,
    rather than the later verifier's ordinary retained-token comparison.
    """

    wait_mutations: list[tuple[int, int, bytes]] = []
    returned_wrappers: list[object] = []
    token_captures: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def replace_live_version_same_bytes(writer: runner.EvidenceWriter) -> None:
        entries = {entry["id"]: entry for entry in writer.entries}
        assert {"version-2", "help-2"} <= set(entries)
        assert "client-executable-2" not in entries
        target = writer.run_root / str(entries["version-2"]["relative_path"])
        original = target.read_bytes()
        original_inode = target.stat().st_ino
        replacement = target.with_name(target.name + ".wait-same-bytes-replacement")
        replacement.write_bytes(original)
        os.replace(replacement, target)
        replacement_inode = target.stat().st_ino
        assert replacement_inode != original_inode
        wait_mutations.append((original_inode, replacement_inode, original))

    original_token_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def record_token_capture(*args: object, **kwargs: object) -> object:
        token_captures.append((args, dict(kwargs)))
        return original_token_capture(*args, **kwargs)

    monkeypatch.setattr(
        runner,
        "_capture_registered_actual_phase_two_post_launch_provenance",
        record_token_capture,
    )
    with pytest.raises(runner.RunnerError) as raised:
        result = _task7d_actual_phase_two_successful_fake_launch(
            tmp_path,
            monkeypatch,
            on_wait=replace_live_version_same_bytes,
            close_pins_after_launch=True,
        )
        returned_wrappers.append(result[4])

    assert len(wait_mutations) == 1, str(raised.value)
    original_inode, replacement_inode, original = wait_mutations[0]
    assert original
    assert replacement_inode != original_inode
    assert token_captures == []
    assert returned_wrappers == []


@pytest.mark.parametrize(
    "attack",
    ("first-create-collision", "post-append-pre-adoption"),
)
def test_actual_phase_two_launcher_rejects_same_byte_native_trace_replacement_before_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """A hostile phase-two trace cannot become a retained launch baseline.

    The first variant pre-creates the exact first native row just before the
    exclusive runner creation.  The second lets that append succeed, then
    replaces its path with the exact same bytes before the tracker is allowed
    to adopt the append receipt.  Both attacks occur only after the private
    fake Popen has begun live streaming; neither can yield a token, wrapper,
    public route, or a second launch.
    """

    trace_calls: list[bytes] = []
    hostile_trace_bytes: list[bytes] = []
    replacement_inodes: list[tuple[int, int]] = []
    token_captures: list[tuple[tuple[object, ...], dict[str, object]]] = []
    returned_wrappers: list[object] = []
    fake_popen_calls: list[tuple[list[str], dict[str, object]]] = []
    public_route_calls: list[str] = []
    original_append = runner.ControlTraceWriter._append_expected_native_trace
    original_token_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("private phase-two trace race reached " + name)
        return reject

    def hostile_native_append(
        trace: runner.ControlTraceWriter,
        expected: object,
        line: bytes,
    ) -> object:
        # Phase one is fixture setup; target only the first phase-two native
        # row that the private launcher owns.
        if trace.path.name != "runner-trace-execution-2.jsonl":
            return original_append(trace, expected, line)
        assert expected is None
        assert not trace.path.exists()
        trace_calls.append(line)

        if attack == "first-create-collision":
            replacement = trace.path.with_name(trace.path.name + ".hostile-first")
            replacement.write_bytes(line)
            staged_inode = replacement.stat().st_ino
            os.replace(replacement, trace.path)
            assert trace.path.stat().st_ino == staged_inode
            hostile_trace_bytes.append(trace.path.read_bytes())
            # The exclusive producer open must reject the attacker-created
            # same-byte trace rather than treating it as its own first row.
            return original_append(trace, expected, line)

        assert attack == "post-append-pre-adoption"
        receipt = original_append(trace, expected, line)
        receipt_bytes = receipt[0]
        assert receipt_bytes == line
        original_inode = trace.path.stat().st_ino
        replacement = trace.path.with_name(trace.path.name + ".hostile-adoption")
        replacement.write_bytes(receipt_bytes)
        os.replace(replacement, trace.path)
        replacement_inode = trace.path.stat().st_ino
        assert replacement_inode != original_inode
        hostile_trace_bytes.append(trace.path.read_bytes())
        replacement_inodes.append((original_inode, replacement_inode))
        # Returning the original fd-backed receipt forces the tracker to
        # reverify it before it can retain the path-selected replacement.
        return receipt

    def record_token_capture(*args: object, **kwargs: object) -> object:
        token_captures.append((args, dict(kwargs)))
        return original_token_capture(*args, **kwargs)

    def record_fake_popen(argv: list[str], kwargs: dict[str, object]) -> None:
        fake_popen_calls.append((list(argv), dict(kwargs)))
        assert kwargs["shell"] is False
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE

    monkeypatch.setattr(
        runner.ControlTraceWriter,
        "_append_expected_native_trace",
        hostile_native_append,
    )
    monkeypatch.setattr(
        runner,
        "_capture_registered_actual_phase_two_post_launch_provenance",
        record_token_capture,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError) as raised:
        result = _task7d_actual_phase_two_successful_fake_launch(
            tmp_path,
            monkeypatch,
            on_popen=record_fake_popen,
            close_pins_after_launch=True,
        )
        returned_wrappers.append(result[4])

    assert len(trace_calls) == 1, str(raised.value)
    assert hostile_trace_bytes == trace_calls
    if attack == "post-append-pre-adoption":
        assert len(replacement_inodes) == 1
        assert replacement_inodes[0][0] != replacement_inodes[0][1]
    else:
        assert replacement_inodes == []
    assert len(fake_popen_calls) == 1
    assert public_route_calls == []
    assert token_captures == []
    assert returned_wrappers == []


@pytest.mark.parametrize(
    "attack",
    ("live-artifact", "trace", "marker-snapshot", "workspace-root"),
)
def test_actual_phase_two_post_launch_provenance_rejects_drift_before_future_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """A post-launch verifier rejects retained-evidence drift without writing.

    Every mutation is applied only after a complete local fake launch.  The
    local continuation below represents a future indexer: it must remain
    unreachable when the provenance verifier rejects the changed host facts.
    """

    phase_one, diagnostic, receipt_gate, phase_two, launched, _probes, _launches = (
        _task7d_actual_phase_two_successful_fake_launch(tmp_path, monkeypatch)
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        live_phase = launched.live_phase
        token = launched.post_launch_token
        entries = {entry["id"]: entry for entry in writer.entries}

        if attack == "live-artifact":
            target = writer.run_root / str(entries[live_phase.executable_id]["relative_path"])
            original_inode = target.stat().st_ino
            replacement = target.with_name(target.name + ".same-bytes-replacement")
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            assert target.stat().st_ino != original_inode
        elif attack == "trace":
            target = phase_two.trace.path
            original_inode = target.stat().st_ino
            replacement = target.with_name(target.name + ".same-bytes-replacement")
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            assert target.stat().st_ino != original_inode
        elif attack == "marker-snapshot":
            marker = phase_two.marker_workspace_snapshots[0]
            target = writer.run_root / str(entries[marker.artifact_id]["relative_path"])
            original_inode = target.stat().st_ino
            replacement = target.with_name(target.name + ".same-bytes-replacement")
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            assert target.stat().st_ino != original_inode
        else:
            assert attack == "workspace-root"
            target = prepared_run.prepared.workspace
            replaced_root = target.with_name(target.name + "-replaced")
            target.rename(replaced_root)
            target.mkdir(mode=0o700)

        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)
        future_index_calls: list[str] = []

        def future_indexer() -> None:
            runner._verify_registered_actual_phase_two_post_launch_provenance(
                prepared_run, diagnostic, receipt_gate, phase_two, live_phase, token,
            )
            future_index_calls.append("future-indexer-reached")

        with pytest.raises(runner.RunnerError):
            future_indexer()

        assert future_index_calls == []
        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_receipt_ledgers_are_private_exclusive_pre_popen_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase one stays receipt-free; phase two creates both headers before Popen.

    The Popen double is deliberately the first observer after the final
    launch guard.  It proves the parent allocated exclusive, regular,
    single-link private baselines before it can spawn the nested client.
    """

    before_launch: list[dict[str, Path]] = []
    observed_headers: dict[str, tuple[dict[str, object], tuple[int, int, int, int, int, int, int, int]]] = {}

    def assert_absent(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        paths = _task7d_phase_two_receipt_paths(writer.control_root)
        assert not phase_two.trace.path.exists()
        assert all(not path.exists() for path in paths.values())
        before_launch.append(paths)

    def observe_pre_popen(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        paths = _task7d_phase_two_receipt_paths(writer.control_root)
        # No native record has started yet: only private ledger headers may
        # exist at this exact Popen boundary.
        assert not phase_two.trace.path.exists()
        for kind, path in paths.items():
            header, rows = _task7d_assert_receipt_ledger_chain(
                path, ledger_kind=kind, run_id=writer.run_id,
            )
            assert rows == []
            stat_result = path.stat()
            assert stat.S_ISREG(stat_result.st_mode)
            assert stat_result.st_nlink == 1
            observed_headers[kind] = (
                header,
                (
                    stat_result.st_dev,
                    stat_result.st_ino,
                    stat_result.st_mode,
                    stat_result.st_uid,
                    stat_result.st_nlink,
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    stat_result.st_ctime_ns,
                ),
            )

    phase_one, _diagnostic, _gate, phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=False,
            before_launch=assert_absent,
            on_popen=observe_pre_popen,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert nested_results == []
        assert len(before_launch) == 1
        assert set(observed_headers) == {"raw", "trace"}
        # Sidecars are parent-private control material, never evidence-index
        # entries or public run/event records.
        assert {
            _TASK7D_PHASE_TWO_RECEIPT_LEDGER_NAMES["raw"],
            _TASK7D_PHASE_TWO_RECEIPT_LEDGER_NAMES["trace"],
        }.isdisjoint({entry["relative_path"] for entry in writer.entries})
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        # The initial header identity is not itself the final identity (the
        # trace ledger receives native rows), but each sidecar still names the
        # same regular file rather than a newly adopted control path.
        for kind, (_header, before) in observed_headers.items():
            after = _task7d_phase_two_receipt_paths(writer.control_root)[kind].stat()
            assert (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink) == before[:5]
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_receipt_ledgers_bind_nested_shim_raw_and_trace_interleave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real child shim is retained between native trace rows, not replayed."""

    phase_one, _diagnostic, _gate, phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        nested = nested_results[0]
        assert nested.returncode in {0, 1}, nested.stderr.decode("utf-8", "replace")

        trace_rows = [json.loads(line) for line in phase_two.trace.path.read_bytes().splitlines()]
        assert [row["sequence"] for row in trace_rows] == list(range(1, len(trace_rows) + 1))
        assert [row["kind"] for row in trace_rows] == [
            "native_record", "command_start", "source_state", "command_end",
            "native_record", "native_record",
        ]

        ledgers = _task7d_phase_two_receipt_paths(writer.control_root)
        raw_ledger = ledgers["raw"].read_bytes()
        trace_ledger = ledgers["trace"].read_bytes()
        _raw_header, raw_receipt_rows = _task7d_assert_receipt_ledger_chain(
            ledgers["raw"], ledger_kind="raw", run_id=writer.run_id,
        )
        _trace_header, trace_receipt_rows = _task7d_assert_receipt_ledger_chain(
            ledgers["trace"], ledger_kind="trace", run_id=writer.run_id,
        )
        assert raw_receipt_rows
        assert len(trace_receipt_rows) == len(trace_rows)
        # Sidecars carry bounded metadata/digests, never source/result/trace
        # payloads.  The actual raw and trace files remain the only byte
        # carriers.
        assert b'"raw":' not in raw_ledger
        assert b'"raw":' not in trace_ledger
        assert b"brain-shim-capture-index.jsonl" in raw_ledger
        assert b"brain-shim-command-log.jsonl" in raw_ledger
        assert b"command_start" in trace_ledger
        assert b"source_state" in trace_ledger
        assert b"command_end" in trace_ledger
        assert b"native_record" in trace_ledger

        raw_payloads = [row["receipt"] for row in raw_receipt_rows]
        assert all(type(payload) is dict for payload in raw_payloads)
        raw_paths = {payload["relative_path"] for payload in raw_payloads}
        assert {
            "brain-shim-capture-index.jsonl",
            "brain-shim-command-log.jsonl",
        } <= raw_paths
        # RunnerHostRecorder routes command trace rows through the shared
        # ControlTraceWriter, so the generic shim's base trace journal is not
        # duplicated in this phase-two raw sidecar.
        assert "brain-shim-trace.jsonl" not in raw_paths
        assert any(path.endswith("/result.json") for path in raw_paths)
        assert any(path.endswith("/source-state.json") for path in raw_paths)
        assert any(path.endswith("/receipt-artifacts.json") for path in raw_paths)
        for payload in raw_payloads:
            assert "raw" not in payload
            if payload["kind"] == "write":
                assert set(payload) == {
                    "kind", "relative_path", "sha256", "byte_count", "identity",
                }
                assert isinstance(payload["identity"], list) and len(payload["identity"]) == 8
                assert payload["identity"][4] == 1
                assert payload["identity"][5] == payload["byte_count"]
            else:
                assert payload["kind"] == "append"
                assert set(payload) == {
                    "kind", "relative_path", "prior_sha256", "prior_bytes",
                    "prior_identity", "appended_sha256", "appended_bytes",
                    "completed_sha256", "completed_bytes", "completed_identity",
                    "prior_rows", "completed_rows",
                }
                assert payload["completed_rows"] == payload["prior_rows"] + 1
                assert payload["completed_bytes"] == (
                    payload["prior_bytes"] + payload["appended_bytes"]
                )

        trace_raw = phase_two.trace.path.read_bytes()
        prior_receipt: dict[str, object] | None = None
        for index, (ledger_row, trace_row) in enumerate(
            zip(trace_receipt_rows, trace_rows), start=1,
        ):
            receipt = ledger_row["receipt"]
            assert type(receipt) is dict
            assert set(receipt) == {
                "kind", "execution_id", "relative_path", "sequence", "trace_kind",
                "row_sha256", "row_bytes", "prior_sha256", "prior_bytes",
                "prior_identity", "completed_sha256", "completed_bytes",
                "completed_identity", "prior_rows", "completed_rows",
            }
            assert receipt["kind"] == "trace"
            assert receipt["execution_id"] == "execution-2"
            assert receipt["relative_path"] == phase_two.trace.path.name
            assert receipt["sequence"] == trace_row["sequence"] == index
            assert receipt["trace_kind"] == trace_row["kind"]
            assert receipt["prior_rows"] == index - 1
            assert receipt["completed_rows"] == index
            prior_bytes = receipt["prior_bytes"]
            completed_bytes = receipt["completed_bytes"]
            assert type(prior_bytes) is int and type(completed_bytes) is int
            line = trace_raw[prior_bytes:completed_bytes]
            assert line == _encoded(trace_row) + b"\n"
            assert hashlib.sha256(line).hexdigest() == receipt["row_sha256"]
            assert len(line) == receipt["row_bytes"]
            if prior_receipt is None:
                assert receipt["prior_sha256"] == hashlib.sha256(b"").hexdigest()
                assert prior_bytes == 0
            else:
                assert (
                    receipt["prior_sha256"], receipt["prior_bytes"], receipt["prior_identity"],
                ) == (
                    prior_receipt["completed_sha256"], prior_receipt["completed_bytes"],
                    prior_receipt["completed_identity"],
                )
            prior_receipt = receipt
        assert prior_receipt is not None
        assert prior_receipt["completed_sha256"] == hashlib.sha256(trace_raw).hexdigest()
        assert prior_receipt["completed_bytes"] == len(trace_raw)

        # The sidecars are private closure inputs only; the isolated launcher
        # remains pre-publication despite exercising a real command capture.
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_post_launch_token_freezes_nested_raw_control_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local nested shim yields one frozen, receipt-bound handoff surface."""

    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("frozen phase-two handoff reached public route " + name)
        return reject

    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    phase_one, diagnostic, receipt_gate, phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        token = launched.post_launch_token
        handoff = token.raw_control_handoff
        assert type(handoff) is runner._RegisteredActualPhaseTwoRawControlHandoff
        assert type(handoff.raw_snapshots) is MappingProxyType
        assert type(handoff.control_snapshots) is MappingProxyType
        with pytest.raises(TypeError):
            handoff.__reduce__()
        with pytest.raises(runner.RunnerError):
            handoff.read_relative(phase_two.control_pin, "not-a-retained-path")

        raw_index, _index_identity = handoff.raw_snapshots[
            "brain-shim-capture-index.jsonl"
        ]
        raw_command_log, _command_identity = handoff.raw_snapshots[
            "brain-shim-command-log.jsonl"
        ]
        assert raw_index == (writer.control_root / "brain-shim-capture-index.jsonl").read_bytes()
        assert raw_command_log == (writer.control_root / "brain-shim-command-log.jsonl").read_bytes()
        assert handoff.phase_one_raw_index == diagnostic.conversion.raw_index
        assert handoff.phase_one_raw_command_log == diagnostic.conversion.raw_command_log
        assert handoff.phase_one_raw_preapply_manifests == (
            diagnostic.conversion.raw_preapply_manifests
        )
        assert raw_index.startswith(handoff.phase_one_raw_index)
        assert raw_command_log.startswith(handoff.phase_one_raw_command_log)
        phase_index = raw_index[len(handoff.phase_one_raw_index):]
        phase_commands = raw_command_log[len(handoff.phase_one_raw_command_log):]
        assert phase_index and phase_index.endswith(b"\n")
        assert phase_commands and phase_commands.endswith(b"\n")
        index_rows = [json.loads(line) for line in phase_index.splitlines()]
        command_rows = [json.loads(line) for line in phase_commands.splitlines()]
        assert len(index_rows) == len(command_rows) == 1
        row = index_rows[0]
        assert row["phase"] == "approved_capture"
        assert command_rows == [{
            "argv": row["argv"],
            "exit_code": row["exit_code"],
            "result_sha256": row["result_sha256"],
            "timestamp": row["timestamp"],
        }]

        # Derive the complete nested raw closure from the retained phase-two
        # index rather than accepting an arbitrary path list from the token.
        command_id = row["command_id"]
        assert type(command_id) is str and command_id.startswith("shim-command-")
        named_paths = {row["result_path"]}
        state_path = row["source_state_path"]
        receipt_path = row["receipt_artifacts_path"]
        assert type(state_path) is str and state_path.endswith("/source-state.json")
        assert type(receipt_path) is str and receipt_path.endswith("/receipt-artifacts.json")
        named_paths.add(state_path)
        named_paths.add(receipt_path)
        state = json.loads(handoff.raw_snapshots[state_path][0])
        assert state["command_id"] == command_id
        for entry in [*state["records"], *state["files"]]:
            named_paths.add(entry["capture_path"])
        if state["pending"] is not None:
            named_paths.add(state["pending"]["capture_path"])
        receipt = json.loads(handoff.raw_snapshots[receipt_path][0])
        assert receipt["command_id"] == command_id
        for label in ("stream", "durable", "delivery"):
            if receipt[label] is not None:
                named_paths.add(receipt[label]["capture_path"])
        dynamic_paths = {
            relative for relative in handoff.raw_snapshots
            if relative.startswith("brain-shim-captures/")
            or relative.startswith("runner-preapply-manifests/")
        }
        assert dynamic_paths == named_paths
        assert any(path.endswith("/result.json") for path in dynamic_paths)
        assert any(path.endswith("/source-state.json") for path in dynamic_paths)
        assert any(path.endswith("/receipt-artifacts.json") for path in dynamic_paths)
        assert "runner-preapply-manifests.jsonl" in handoff.raw_absent_paths

        for relative, snapshot in handoff.raw_snapshots.items():
            _task7d_assert_frozen_handoff_snapshot(writer.control_root, relative, snapshot)
        assert handoff.trace_snapshot[0] == phase_two.trace.path.read_bytes()
        assert handoff.trace_snapshot[1] == token.trace_identity
        assert hashlib.sha256(handoff.trace_snapshot[0]).hexdigest() == token.trace_sha256

        assert phase_two.mcp_config is not None
        expected_control_paths = {
            path.relative_to(writer.control_root).as_posix()
            for path in (
                phase_two.phase_control.config.path,
                phase_two.runner_state.path,
                phase_two.mcp_config.path,
                phase_two.phase_prompt.seal.path,
                *(seal.path for seal, _raw in phase_two.phase_control.fixture_assets.values()),
            )
        }
        assert set(handoff.control_snapshots) == expected_control_paths
        for relative, snapshot in handoff.control_snapshots.items():
            _task7d_assert_frozen_handoff_snapshot(writer.control_root, relative, snapshot)

        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_adopts_nested_host_evidence_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frozen nested phase two can enter only one private diagnostic stage."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    public_or_semantic_calls: list[str] = []

    def reject_public_or_semantic_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_or_semantic_calls.append(name)
            pytest.fail("private phase-two diagnostic reached " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_names = {
            "evidence-index.json",
            "log-attestation.json",
            "event-log.json",
            "run-manifest.json",
        }
        assert terminal_names.isdisjoint(before_tree)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))

        for name in (
            "run_scenario",
            "run_registered_actual_scenario",
            "_run_registered_actual_plan",
            "_finalize_log",
            "_finalize_registered_actual_phase_one_diagnostic",
            "_seal_and_attest_log",
            "_publish_sealed_log",
            "_atomic_validate_and_publish_candidate",
            "_private_semantic_overlay",
            "_indexed_semantic_replay",
            "plan_semantic_events",
            "_semantic_commands_from_conversion",
            "_emit_semantic_plan",
            # The phase-two indexer must reconstruct the phase-one release
            # through the token's frozen facade, never the mutable legacy
            # reader or its semantic marker extractor.
            "_reconstruct_actual_phase_one_receipt_gate",
            "extract_event_markers",
        ):
            monkeypatch.setattr(runner, name, reject_public_or_semantic_path(name))

        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            provenance,
        )

        assert type(diagnostic) is runner._RegisteredActualPhaseTwoDiagnostic
        stages = list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert len(stages) == 1
        stage = stages[0]
        assert stage.is_dir()

        after_entries = [dict(entry) for entry in writer.entries]
        assert after_entries[:len(before_entries)] == before_entries
        adopted_entries = after_entries[len(before_entries):]
        assert adopted_entries
        assert all(
            str(entry["relative_path"]).startswith(stage.name + "/")
            for entry in adopted_entries
        )

        # The diagnostic retains a private, immutable descriptor snapshot
        # after its short-lived stage pin has closed.  A future consumer can
        # revalidate the exact staged graph without consulting writer state.
        stage_snapshot = diagnostic.stage_snapshot
        assert type(stage_snapshot) is runner._RegisteredActualPhaseTwoDiagnosticStage
        assert stage_snapshot.stage_name == diagnostic.stage_name == stage.name
        assert type(stage_snapshot.entries) is MappingProxyType
        assert type(stage_snapshot.tree) is MappingProxyType
        assert set(stage_snapshot.entries) == {entry["id"] for entry in adopted_entries}
        assert {
            str(entry["relative_path"]).removeprefix(stage.name + "/")
            for entry in stage_snapshot.entries.values()
        } == {
            str(entry["relative_path"]).removeprefix(stage.name + "/")
            for entry in adopted_entries
        }
        expected_stage_tree = {
            path.relative_to(stage).as_posix() + ("/" if path.is_dir() else ""):
            runner.PinnedDirectory._regular_identity(path.lstat())
            for path in stage.rglob("*")
        }
        assert dict(stage_snapshot.tree) == expected_stage_tree
        with pytest.raises(TypeError):
            stage_snapshot.tree["unexpected"] = (0, 0, 0, 0, 0, 0, 0, 0)  # type: ignore[index]
        assert stage_snapshot.verify() is None
        assert diagnostic.__post_init__() is None

        after_tree = _task7d_run_tree_bytes(writer)
        added_paths = set(after_tree) - set(before_tree)
        assert added_paths
        assert all(path.startswith(stage.name + "/") for path in added_paths)
        assert {
            path: raw for path, raw in after_tree.items()
            if not path.startswith(stage.name + "/")
        } == before_tree
        assert terminal_names.isdisjoint(after_tree)
        assert public_or_semantic_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_diagnostic_normalizes_malformed_post_launch_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged direct token cannot escape the diagnostic's RunnerError boundary."""

    (
        phase_one,
        phase_one_diagnostic,
        _receipt_gate,
        _phase_two,
        launched,
        nested_results,
    ) = _task7d_actual_phase_two_receipt_fake_launch(
        tmp_path,
        monkeypatch,
        nested_shim=True,
    )
    diagnostic: runner._RegisteredActualPhaseTwoDiagnostic | None = None
    original_token: runner._RegisteredActualPhaseTwoPostLaunchToken | None = None
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        assert diagnostic.__post_init__() is None
        original_token = diagnostic.post_launch_token

        # Bypass dataclass construction exactly as an in-process attacker
        # could, while leaving every other diagnostic field valid.
        malformed = object.__new__(runner._RegisteredActualPhaseTwoPostLaunchToken)
        object.__setattr__(diagnostic, "post_launch_token", malformed)

        with pytest.raises(runner.RunnerError, match="phase-two diagnostic is invalid"):
            diagnostic.__post_init__()
    finally:
        if diagnostic is not None and original_token is not None:
            object.__setattr__(diagnostic, "post_launch_token", original_token)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_retains_run_root_without_repinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexing descends from the retained control root, not ``writer.run_root``.

    The phase-two token has already established the private control descriptor.
    A subsequent pathname pin of the growing run directory would silently make
    a replacement directory a new baseline, so the diagnostic must use the
    descriptor-relative retention route instead.
    """

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_pin = runner.PinnedDirectory.pin
    run_root = writer.run_root.resolve(strict=True)
    forbidden_pin_paths: list[Path] = []

    def reject_run_root_repin(
        _cls: type[runner.PinnedDirectory],
        path: Path,
        *,
        private: bool,
    ) -> runner.PinnedDirectory:
        candidate = Path(os.fspath(path)).resolve(strict=False)
        if candidate == run_root:
            forbidden_pin_paths.append(candidate)
            pytest.fail("host diagnostic re-pinned writer.run_root after token launch")
        return original_pin(path, private=private)

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "pin",
            classmethod(reject_run_root_repin),
        )

        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            provenance,
        )

        assert type(diagnostic) is runner._RegisteredActualPhaseTwoDiagnostic
        assert (writer.run_root / diagnostic.stage_name).is_dir()
        assert forbidden_pin_paths == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_run_root_swap_after_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained phase-one run root cannot authorize its pathname replacement."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_retain_relative_directory = runner.PinnedDirectory.retain_relative_directory
    run_root = writer.run_root.resolve(strict=True)
    run_relative = PurePosixPath("runs", writer.run_id).as_posix()
    before_run_inode = run_root.stat().st_ino
    before_entries = [dict(entry) for entry in writer.entries]
    before_identities = dict(writer.write_identities)
    before_tree = _task7d_run_tree_bytes(writer)
    retained_paths: list[Path] = []
    displaced_roots: list[Path] = []
    stage_attempts: list[str] = []

    def retain_then_replace_phase_one_run_root(
        _cls: type[runner.PinnedDirectory],
        source: runner.PinnedDirectory,
        relative: str,
        *,
        private: bool,
    ) -> runner.PinnedDirectory:
        retained = original_retain_relative_directory(source, relative, private=private)
        if relative == run_relative:
            assert source is launched.post_launch_token.live_phase.control_pin
            assert private is True
            assert retained_paths == []
            displaced_root = run_root.with_name(run_root.name + ".after-retain-original")
            assert not displaced_root.exists()
            os.rename(run_root, displaced_root)
            shutil.copytree(displaced_root, run_root)
            assert run_root.stat().st_ino != before_run_inode
            assert _task7d_run_tree_bytes(writer) == before_tree
            retained_paths.append(retained.path)
            displaced_roots.append(displaced_root)
        return retained

    def reject_stage_allocation(
        _run_pin: runner.PinnedDirectory,
        prefix: str,
    ) -> object:
        stage_attempts.append(prefix)
        pytest.fail("run-root replacement reached phase-two stage allocation")

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        assert not list(run_root.glob(".phase-two-diagnostic-*"))
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "retain_relative_directory",
            classmethod(retain_then_replace_phase_one_run_root),
        )
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "create_private_child",
            reject_stage_allocation,
        )

        with pytest.raises(runner.RunnerError, match="trusted root|retained trusted"):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic,
                launched.post_launch_token,
            )

        assert retained_paths == [run_root]
        assert len(displaced_roots) == 1
        assert displaced_roots[0].is_dir()
        assert stage_attempts == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(run_root.glob(".phase-two-diagnostic-*"))
        assert not list(displaced_roots[0].glob(".phase-two-diagnostic-*"))
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_private_bridge_conversion_drift_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied phase-one conversion must still match the frozen raw handoff."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    retain_attempts: list[str] = []
    stage_attempts: list[str] = []

    def reject_run_retention(
        _cls: type[runner.PinnedDirectory],
        _source: runner.PinnedDirectory,
        relative: str,
        *,
        private: bool,
    ) -> runner.PinnedDirectory:
        retain_attempts.append(relative)
        pytest.fail(
            "private bridge conversion drift reached descriptor-relative retention "
            f"({private=})",
        )

    def reject_stage_allocation(
        _run_pin: runner.PinnedDirectory,
        prefix: str,
    ) -> object:
        stage_attempts.append(prefix)
        pytest.fail("private bridge conversion drift reached phase-two stage allocation")

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        token = launched.post_launch_token
        bridge = token.phase_one_bridge
        conversion = bridge.conversion
        assert conversion is not phase_one_diagnostic.conversion
        original_raw_index = conversion.raw_index
        assert original_raw_index == phase_one_diagnostic.conversion.raw_index
        raw_index_lines = original_raw_index.splitlines()
        assert raw_index_lines
        copied_row = json.loads(raw_index_lines[-1])
        assert type(copied_row) is dict
        copied_string_key = next(
            key for key, value in copied_row.items()
            if type(key) is str and type(value) is str
        )
        copied_row[copied_string_key] += "-private-bridge-drift"
        replacement_raw_index = b"\n".join(
            (*raw_index_lines[:-1], _encoded(copied_row)),
        ) + (b"\n" if original_raw_index.endswith(b"\n") else b"")
        assert replacement_raw_index != original_raw_index
        assert all(json.loads(line) for line in replacement_raw_index.splitlines())

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "retain_relative_directory",
            classmethod(reject_run_retention),
        )
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "create_private_child",
            reject_stage_allocation,
        )

        object.__setattr__(conversion, "raw_index", replacement_raw_index)
        try:
            with pytest.raises(runner.RunnerError, match="raw/control handoff changed"):
                runner._index_registered_actual_phase_two_host_diagnostic(
                    phase_one_diagnostic,
                    token,
                )
        finally:
            object.__setattr__(conversion, "raw_index", original_raw_index)

        assert retain_attempts == []
        assert stage_attempts == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_uses_frozen_fixture_metadata_after_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The indexer cannot reread mutable scenario/fixture dictionaries."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    prepared = phase_one.prepared_run.prepared
    original_scenario = dict(prepared.scenario)
    original_fixture = dict(prepared.fixture)
    scenario_calls: list[str] = []

    def reject_late_scenario_lookup(scenario_id: str) -> dict[str, object]:
        scenario_calls.append(scenario_id)
        pytest.fail("phase-two indexer reread source scenario metadata")

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        token = launched.post_launch_token
        metadata = phase_one.prepared_run.metadata
        assert type(metadata) is runner._RegisteredActualScenarioFixtureMetadata
        original_fixture_id = metadata.fixture_id
        original_fixture_sha256 = metadata.fixture_sha256
        assert original_scenario["fixture_id"] == original_fixture_id
        assert original_fixture["tree_sha256"] == original_fixture_sha256

        replacement_fixture_id = next(
            fixture_id for fixture_id in runner._FIXTURE_IDS.values()
            if fixture_id != original_fixture_id
        )
        replacement_fixture_sha256 = (
            "0" * 64 if original_fixture_sha256 != "0" * 64 else "f" * 64
        )
        assert replacement_fixture_id != original_fixture_id
        assert replacement_fixture_sha256 != original_fixture_sha256

        # Both mutable containers now advertise a different, syntactically
        # valid fixture.  The completed token must carry the original facade,
        # and the diagnostic must not consult the source scenario loader.
        prepared.scenario["fixture_id"] = replacement_fixture_id
        prepared.fixture["tree_sha256"] = replacement_fixture_sha256
        assert prepared.scenario["fixture_id"] == replacement_fixture_id
        assert prepared.fixture["tree_sha256"] == replacement_fixture_sha256
        monkeypatch.setattr(runner, "_scenario", reject_late_scenario_lookup)

        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            token,
        )
        assert type(diagnostic) is runner._RegisteredActualPhaseTwoDiagnostic
        assert scenario_calls == []

        stage_root = writer.run_root / diagnostic.stage_name
        for artifact_id in (diagnostic.policy_id, diagnostic.process_id):
            entry = diagnostic.stage_snapshot.entries[artifact_id]
            row = json.loads((stage_root / str(entry["relative_path"])).read_bytes())
            assert row["fixture_id"] == original_fixture_id
            assert row["fixture_sha256"] == original_fixture_sha256
            assert row["fixture_id"] != replacement_fixture_id
            assert row["fixture_sha256"] != replacement_fixture_sha256
        assert not {
            "evidence-index.json", "log-attestation.json", "event-log.json", "run-manifest.json",
        } & set(_task7d_run_tree_bytes(writer))
    finally:
        prepared.scenario.clear()
        prepared.scenario.update(original_scenario)
        prepared.fixture.clear()
        prepared.fixture.update(original_fixture)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rechecks_receipt_gate_after_fixture_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid gate mutation after fixture binding cannot allocate the diagnostic stage."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_fixture_binding = (
        runner._registered_actual_phase_one_private_fixture_metadata_binding
    )
    original_facts = receipt_gate.receipt_facts
    replacement_effect_digest = (
        "0" * 64 if original_facts.effect_digest != "0" * 64 else "f" * 64
    )
    replacement_facts = replace(
        original_facts, effect_digest=replacement_effect_digest,
    )

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        assert replacement_facts.selection is original_facts.selection
        assert replacement_facts.effect_digest != original_facts.effect_digest
        provenance = launched.post_launch_token
        assert provenance.receipt_gate is receipt_gate
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        binding_calls: list[tuple[object, object]] = []
        mutations: list[object] = []

        def bind_then_mutate_gate(
            observed_bridge: runner._RegisteredActualPhaseOnePrivateLaunchBridge,
            observed_metadata: runner._RegisteredActualScenarioFixtureMetadata,
        ) -> tuple[str, str]:
            result = original_fixture_binding(observed_bridge, observed_metadata)
            binding_calls.append((observed_bridge, observed_metadata))
            if len(binding_calls) == 2:
                object.__setattr__(receipt_gate, "receipt_facts", replacement_facts)
                assert receipt_gate.__post_init__() is None
                mutations.append(receipt_gate.receipt_facts)
            return result

        stage_attempts: list[str] = []

        def reject_stage_allocation(
            _run_pin: runner.PinnedDirectory,
            prefix: str,
        ) -> object:
            stage_attempts.append(prefix)
            pytest.fail("receipt-gate mutation reached phase-two stage allocation")

        monkeypatch.setattr(
            runner,
            "_registered_actual_phase_one_private_fixture_metadata_binding",
            bind_then_mutate_gate,
        )
        monkeypatch.setattr(
            runner.PinnedDirectory,
            "create_private_child",
            reject_stage_allocation,
        )
        with pytest.raises(runner.RunnerError, match="receipt gate changed"):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic, provenance,
            )

        assert binding_calls == [
            (provenance.phase_one_bridge, phase_one.prepared_run.metadata),
            (provenance.phase_one_bridge, phase_one.prepared_run.metadata),
            (provenance.phase_one_bridge, phase_one.prepared_run.metadata),
        ]
        assert mutations == [replacement_facts]
        assert receipt_gate.receipt_facts is replacement_facts
        assert stage_attempts == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
    finally:
        object.__setattr__(receipt_gate, "receipt_facts", original_facts)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_phase_one_metadata_hash_tamper_before_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced frozen facade cannot seed phase-two staging."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    metadata = phase_one.prepared_run.metadata
    original_fixture_sha256 = metadata.fixture_sha256
    replacement_fixture_sha256 = (
        "0" * 64 if original_fixture_sha256 != "0" * 64 else "f" * 64
    )
    impure_calls: list[str] = []

    def reject_impure_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            impure_calls.append(name)
            pytest.fail("phase-one metadata tamper reached " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        assert type(metadata) is runner._RegisteredActualScenarioFixtureMetadata
        assert len(original_fixture_sha256) == 64
        assert replacement_fixture_sha256 != original_fixture_sha256
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_names = {
            "evidence-index.json", "log-attestation.json", "event-log.json", "run-manifest.json",
        }
        assert terminal_names.isdisjoint(before_tree)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))

        for owner, name in (
            (runner.PinnedDirectory, "create_private_child"),
            (runner._PrivatePhaseOneDiagnosticWriter, "add_bytes"),
            (runner._PrivatePhaseOneDiagnosticWriter, "add_json"),
            (runner, "_finalize_registered_actual_two_phase_host_diagnostic"),
            (runner, "_finalize_log"),
        ):
            monkeypatch.setattr(owner, name, reject_impure_path(name))

        object.__setattr__(metadata, "fixture_sha256", replacement_fixture_sha256)
        with pytest.raises(runner.RunnerError):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic,
                launched.post_launch_token,
            )

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert terminal_names.isdisjoint(_task7d_run_tree_bytes(writer))
        assert impure_calls == []
    finally:
        object.__setattr__(metadata, "fixture_sha256", original_fixture_sha256)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_adopted_stage_same_byte_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained staged graph rejects an identical-byte file substitution."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        stage_snapshot = diagnostic.stage_snapshot
        assert type(stage_snapshot) is runner._RegisteredActualPhaseTwoDiagnosticStage
        assert stage_snapshot.verify() is None

        stage_entry = next(
            entry for entry in writer.entries if entry["id"] == diagnostic.policy_id
        )
        relative = str(stage_entry["relative_path"])
        stage_relative = relative.removeprefix(diagnostic.stage_name + "/")
        assert relative == diagnostic.stage_name + "/" + stage_relative
        target = writer.run_root / relative
        frozen_identity = stage_snapshot.tree[stage_relative]
        assert frozen_identity == runner.PinnedDirectory._regular_identity(target.lstat())

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        before_inode, after_inode = _task7d_replace_same_bytes(target)
        assert before_inode == frozen_identity[1]
        assert after_inode != before_inode
        assert target.read_bytes() == before_tree[relative]
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert runner.PinnedDirectory._regular_identity(target.lstat()) != frozen_identity

        # Both the explicit future-read boundary and the diagnostic's own
        # validation reject identity drift even when every staged byte matches.
        with pytest.raises(runner.RunnerError):
            stage_snapshot.verify()
        with pytest.raises(runner.RunnerError):
            diagnostic.__post_init__()

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_host_preflight_is_private_and_rejects_retained_stage_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight replays only frozen host facts and cannot write a new stage."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    impure_calls: list[str] = []

    def reject_impure_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            impure_calls.append(name)
            pytest.fail("two-phase preflight reached impure path " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        assert type(diagnostic) is runner._RegisteredActualPhaseTwoDiagnostic

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        before_stages = sorted(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert len(before_stages) == 1

        # These paths all allocate evidence, launch a process, or cross the
        # semantic/public boundary.  The preflight may only re-read its
        # retained receipt, stage, and descriptor-pinned host graph.
        for owner, name in (
            (runner.EvidenceWriter, "add_bytes"),
            (runner.EvidenceWriter, "add_json"),
            (runner.EvidenceWriter, "add_fixed_json"),
            (runner.EvidenceWriter, "seal"),
            (runner.PinnedDirectory, "create_private_child"),
            (runner.subprocess, "Popen"),
            (runner, "_prepare_registered_actual_phase_two"),
            (runner, "_launch_registered_actual_phase_two"),
            (runner, "_reconstruct_actual_phase_one_receipt_gate"),
            (runner, "extract_event_markers"),
            (runner, "plan_semantic_events"),
            (runner, "_semantic_commands_from_conversion"),
            (runner, "_private_semantic_overlay"),
            (runner, "_indexed_semantic_replay"),
            (runner, "_emit_semantic_plan"),
            (runner, "run_scenario"),
            (runner, "run_registered_actual_scenario"),
            (runner, "_run_registered_actual_plan"),
            (runner, "_finalize_log"),
            (runner, "_seal_and_attest_log"),
            (runner, "_publish_sealed_log"),
            (runner, "_atomic_validate_and_publish_candidate"),
        ):
            monkeypatch.setattr(owner, name, reject_impure_path(name))

        proof = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            diagnostic,
            receipt_gate,
        )
        assert type(proof) is runner._RegisteredActualTwoPhaseHostPreflight
        assert proof.phase_one_diagnostic is phase_one_diagnostic
        assert proof.phase_two_diagnostic is diagnostic
        assert proof.receipt_gate is receipt_gate
        assert type(proof.host_join) is runner._RegisteredActualTwoPhaseHostJoin
        assert proof.verify() is None
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert sorted(writer.run_root.glob(".phase-two-diagnostic-*")) == before_stages
        assert impure_calls == []

        # A future preflight detects an inode substitution in an adopted
        # stage even though its bytes and all writer-owned lists are intact.
        stage_entry = next(
            entry for entry in writer.entries if entry["id"] == diagnostic.policy_id
        )
        relative = str(stage_entry["relative_path"])
        target = writer.run_root / relative
        frozen_identity = diagnostic.stage_snapshot.tree[
            relative.removeprefix(diagnostic.stage_name + "/")
        ]
        before_inode, after_inode = _task7d_replace_same_bytes(target)
        assert before_inode == frozen_identity[1]
        assert after_inode != before_inode
        assert _task7d_run_tree_bytes(writer) == before_tree

        with pytest.raises(runner.RunnerError):
            runner._preflight_registered_actual_two_phase_host_diagnostic(
                phase_one_diagnostic,
                diagnostic,
                receipt_gate,
            )

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert sorted(writer.run_root.glob(".phase-two-diagnostic-*")) == before_stages
        assert impure_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_host_diagnostic_finalizer_seals_only_incomplete_host_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planned finalizer may publish only the exact two-phase diagnostic."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    impure_calls: list[str] = []

    def reject_impure_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            impure_calls.append(name)
            pytest.fail("two-phase diagnostic finalizer reached " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert type(preflight) is runner._RegisteredActualTwoPhaseHostPreflight
        assert preflight.verify() is None

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_files = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json",
            "log-attestation.json",
            "event-log.json",
        }
        assert terminal_paths.isdisjoint(before_files)
        assert "run-manifest.json" not in before_files
        run_pin = runner.PinnedDirectory.pin(writer.run_root, private=True)
        try:
            before_tree = run_pin.snapshot_tree()

            # This remains a specialized incomplete diagnostic boundary, not
            # a semantic replay, manifest/binding path, generic finalizer, or
            # candidate-pass publisher.
            for name in (
                "_finalize_log",
                "_atomic_validate_and_publish_candidate",
                "run_scenario",
                "run_registered_actual_scenario",
                "_run_registered_actual_plan",
                "_reconstruct_actual_phase_one_receipt_gate",
                "extract_event_markers",
                "plan_semantic_events",
                "_semantic_commands_from_conversion",
                "_private_semantic_overlay",
                "_indexed_semantic_replay",
                "_emit_semantic_plan",
                "_indexed_run_manifest",
                "_indexed_execution_binding",
                "_project_repository_assertions",
            ):
                monkeypatch.setattr(runner, name, reject_impure_path(name))

            log_path = runner._finalize_registered_actual_two_phase_host_diagnostic(
                preflight,
            )
            assert isinstance(log_path, Path)
            assert log_path == writer.run_root / "event-log.json"
            assert log_path.is_file()

            after_files = _task7d_run_tree_bytes(writer)
            assert set(after_files) == set(before_files) | terminal_paths
            assert {
                relative: raw for relative, raw in after_files.items()
                if relative not in terminal_paths
            } == before_files
            assert not (writer.run_root / "run-manifest.json").exists()
            after_tree = run_pin.snapshot_tree()
            expected_tree = dict(before_tree)
            expected_tree.update({relative: after_tree[relative] for relative in terminal_paths})
            assert after_tree == expected_tree
            assert writer.entries == before_entries
            assert writer.write_identities == before_identities
            assert impure_calls == []

            index_raw = (writer.run_root / "evidence-index.json").read_bytes()
            index = json.loads(index_raw)
            assert index_raw == runner._canonical_json(index)
            assert index == {
                "schema_version": 1,
                "run_id": writer.run_id,
                "entries": sorted(before_entries, key=lambda entry: entry["id"]),
            }
            assert not {
                "marker", "event_record", "interpretation_decision", "approval",
                "fixture_capability", "run_manifest", "assertion",
            } & {entry["type"] for entry in index["entries"]}

            attestation_raw = (writer.run_root / "log-attestation.json").read_bytes()
            attestation = json.loads(attestation_raw)
            assert attestation_raw == runner._canonical_json(attestation)

            log_raw = log_path.read_bytes()
            log = json.loads(log_raw)
            assert log_raw == runner._canonical_json(log)
            assert set(log) == {
                "schema_version", "run_id", "scenario_id", "client", "client_version",
                "fixture_sha256", "network_mode", "evidence_index_sha256",
                "log_attestation_sha256", "executions", "receipts", "deliveries",
                "events", "repository_assertions", "result", "incomplete_reasons",
            }
            assert log["schema_version"] == 1
            assert log["run_id"] == writer.run_id
            assert log["scenario_id"] == "web-approval-and-capture"
            assert log["client"] == "claude"
            assert log["client_version"] == phase_one_diagnostic.live_phase.reported_version
            assert log["fixture_sha256"] == phase_one.prepared_run.prepared.fixture["tree_sha256"]
            assert log["network_mode"] == "mock_only"
            assert log["result"] == "incomplete"
            assert log["incomplete_reasons"] == ["public_semantic_projection_unavailable"]
            assert log["executions"] == [
                {
                    "id": "execution-1",
                    "phase": "approval",
                    "kind": "actual_client_process",
                    "policy_id": phase_one_diagnostic.policy_id,
                    "process_id": phase_one_diagnostic.process_id,
                    "transcript_id": phase_one_diagnostic.transcript_id,
                },
                {
                    "id": "execution-2",
                    "phase": "approved_capture",
                    "kind": "actual_client_process",
                    "policy_id": phase_two_diagnostic.policy_id,
                    "process_id": phase_two_diagnostic.process_id,
                    "transcript_id": phase_two_diagnostic.transcript_id,
                },
            ]
            assert log["events"] == []
            assert log["receipts"] == []
            assert log["deliveries"] == []
            assert log["repository_assertions"] == []
            assert log["evidence_index_sha256"] == hashlib.sha256(index_raw).hexdigest()
            assert log["log_attestation_sha256"] == hashlib.sha256(attestation_raw).hexdigest()
            unsigned_log = dict(log)
            unsigned_log.pop("log_attestation_sha256")
            assert attestation == {
                "schema_version": 1,
                "run_id": writer.run_id,
                "client": "claude",
                "scenario_id": "web-approval-and-capture",
                "fixture_sha256": phase_one.prepared_run.prepared.fixture["tree_sha256"],
                "evidence_index_sha256": hashlib.sha256(index_raw).hexdigest(),
                "log_sha256": hashlib.sha256(runner._canonical_json(unsigned_log)).hexdigest(),
            }
            validate_against_schema(
                log,
                json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text()),
            )
        finally:
            run_pin.close()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_host_pipeline_uses_private_roots_after_public_pin_method_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host index through finalization never dereferences returned pin methods.

    The public phase-one aggregate and returned phase-two token deliberately
    remain available after launch for diagnostics.  They are not authority for
    the retained host path: index, preflight, and finalization must instead
    use the launch token's private bridge and live descriptor roots.
    """

    (
        phase_one,
        phase_one_diagnostic,
        receipt_gate,
        phase_two,
        launched,
        nested_results,
    ) = _task7d_actual_phase_two_receipt_fake_launch(
        tmp_path, monkeypatch, nested_shim=True,
    )
    shadowed_methods: list[tuple[runner.PinnedDirectory, str]] = []
    shadow_calls: list[str] = []
    poisoned_pin_ids: set[int] = set()

    def poison_method(root_name: str, method_name: str) -> Callable[..., None]:
        def poisoned(*_args: object, **_kwargs: object) -> None:
            shadow_calls.append(root_name + "." + method_name)
            pytest.fail(
                "host index/finalizer consulted a public token pin method: "
                + shadow_calls[-1],
            )

        return poisoned

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        assert provenance.prepared_phase is phase_two
        assert phase_one_diagnostic.prepared_phase is phase_one
        assert phase_one_diagnostic.live_phase.control_pin is phase_one.control_pin
        assert phase_one_diagnostic.live_phase.workspace_pin is phase_one.workspace_pin
        assert provenance.prepared_phase.control_pin is phase_two.control_pin
        assert provenance.prepared_phase.workspace_pin is phase_two.workspace_pin

        # The live phase and phase-one bridge retain duplicate roots.  The
        # public objects below are intentionally hostile only after launch,
        # so fixture setup and the actual process are outside this boundary.
        public_pins = (
            phase_one.control_pin,
            phase_one.workspace_pin,
            phase_two.control_pin,
            phase_two.workspace_pin,
        )
        private_pins = (
            launched.live_phase.control_pin,
            launched.live_phase.workspace_pin,
            provenance.phase_one_bridge.live_phase.control_pin,
            provenance.phase_one_bridge.live_phase.workspace_pin,
        )
        assert all(type(pin) is runner.PinnedDirectory for pin in public_pins)
        assert all(type(pin) is runner.PinnedDirectory for pin in private_pins)
        assert all(
            private_pin is not public_pin
            for private_pin in private_pins
            for public_pin in public_pins
        )

        for root_name, pin in (
                ("phase-one-control", phase_one.control_pin),
                ("phase-one-workspace", phase_one.workspace_pin),
                ("phase-two-token-control", phase_two.control_pin),
                ("phase-two-token-workspace", phase_two.workspace_pin)):
            # The public phase-one and phase-two facades may deliberately
            # alias a descriptor object.  Poisoning that object once covers
            # every alias while preserving teardown's original class methods.
            if id(pin) in poisoned_pin_ids:
                continue
            poisoned_pin_ids.add(id(pin))
            for method_name in (
                    "verify", "read_relative", "snapshot_relative",
                    "reverify_snapshot", "snapshot_tree", "reverify_tree"):
                assert method_name not in pin.__dict__
                object.__setattr__(
                    pin, method_name, poison_method(root_name, method_name),
                )
                shadowed_methods.append((pin, method_name))

        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            provenance,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert type(preflight) is runner._RegisteredActualTwoPhaseHostPreflight
        assert preflight.verify() is None

        log_path = runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)
        assert log_path == phase_one.prepared_run.writer.run_root / "event-log.json"
        assert log_path.is_file()
        log = json.loads(log_path.read_bytes())
        assert log["result"] == "incomplete"
        assert log["incomplete_reasons"] == ["public_semantic_projection_unavailable"]
        assert shadow_calls == []
    finally:
        for pin, method_name in reversed(shadowed_methods):
            if method_name in pin.__dict__:
                object.__delattr__(pin, method_name)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_host_pipeline_uses_class_methods_after_private_pin_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained live roots cannot replace host descriptor operations.

    The post-launch token intentionally exposes the private live phase and
    its detached phase-one bridge to in-process host diagnostics.  Those
    objects may retain the descriptor authority, but their mutable Python
    instance dictionaries must not select the descriptor operation that an
    index, preflight, or finalizer performs.
    """

    (
        phase_one,
        phase_one_diagnostic,
        receipt_gate,
        _phase_two,
        launched,
        nested_results,
    ) = _task7d_actual_phase_two_receipt_fake_launch(
        tmp_path, monkeypatch, nested_shim=True,
    )
    shadowed_methods: list[tuple[runner.PinnedDirectory, str]] = []
    shadow_calls: list[str] = []

    def poison_method(root_name: str, method_name: str) -> Callable[..., None]:
        def poisoned(*_args: object, **_kwargs: object) -> None:
            shadow_calls.append(root_name + "." + method_name)
            pytest.fail(
                "host index/preflight/finalizer consulted a private live pin method: "
                + shadow_calls[-1],
            )

        return poisoned

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        live_control_pin = launched.live_phase.control_pin
        live_workspace_pin = launched.live_phase.workspace_pin
        bridge_live_phase = provenance.phase_one_bridge.live_phase
        assert type(live_control_pin) is runner.PinnedDirectory
        assert type(live_workspace_pin) is runner.PinnedDirectory
        assert live_control_pin.private is True
        assert live_workspace_pin.private is False
        assert bridge_live_phase.control_pin is live_control_pin
        assert bridge_live_phase.workspace_pin is live_workspace_pin
        assert live_control_pin is not phase_one.control_pin
        assert live_workspace_pin is not phase_one.workspace_pin

        # Do not shadow the returned phase-two aggregate's public descriptor
        # sentinels: those are intentionally checked class-qualified.  These
        # are only the private roots whose ordinary descriptor reads and the
        # terminal exclusive writes are consumed by the host pipeline.
        for root_name, pin, method_names in (
            (
                "private-control",
                live_control_pin,
                (
                    "verify",
                    "read_relative",
                    "snapshot_relative",
                    "reverify_snapshot",
                    "write_relative_exclusive",
                ),
            ),
            ("private-workspace", live_workspace_pin, ("verify",)),
        ):
            for method_name in method_names:
                assert method_name not in pin.__dict__
                object.__setattr__(
                    pin, method_name, poison_method(root_name, method_name),
                )
                shadowed_methods.append((pin, method_name))

        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            provenance,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert type(preflight) is runner._RegisteredActualTwoPhaseHostPreflight
        assert preflight.verify() is None

        log_path = runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)
        assert log_path == phase_one.prepared_run.writer.run_root / "event-log.json"
        assert log_path.is_file()
        log = json.loads(log_path.read_bytes())
        assert log["result"] == "incomplete"
        assert log["incomplete_reasons"] == ["public_semantic_projection_unavailable"]
        assert shadow_calls == []
    finally:
        for pin, method_name in reversed(shadowed_methods):
            if method_name in pin.__dict__:
                object.__delattr__(pin, method_name)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_uses_retained_fixture_hash_after_mutable_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal fixture provenance comes from pinned policy/process evidence."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    impure_calls: list[str] = []

    def reject_impure_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            impure_calls.append(name)
            pytest.fail("fixture-provenance finalizer reached " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert preflight.verify() is None

        original_fixture_hash = phase_one.prepared_run.prepared.fixture["tree_sha256"]
        assert type(original_fixture_hash) is str and len(original_fixture_hash) == 64
        retained_fixture_hashes: set[str] = set()
        for artifact_id in (
            phase_one_diagnostic.policy_id,
            phase_one_diagnostic.process_id,
            phase_two_diagnostic.policy_id,
            phase_two_diagnostic.process_id,
        ):
            _artifact_type, relative, _digest, _byte_count, _identity = (
                preflight.host_join.artifact_provenance[artifact_id]
            )
            raw = preflight.host_join.control_pin.read_relative(
                PurePosixPath("runs", writer.run_id, relative).as_posix(),
            )
            retained_fixture_hashes.add(json.loads(raw)["fixture_sha256"])
        assert retained_fixture_hashes == {original_fixture_hash}

        replacement_fixture_hash = (
            "0" * 64 if original_fixture_hash != "0" * 64 else "f" * 64
        )
        fixture = phase_one.prepared_run.prepared.fixture
        assert type(fixture) is dict
        fixture["tree_sha256"] = replacement_fixture_hash
        assert fixture["tree_sha256"] != original_fixture_hash

        for name in (
            "_finalize_log",
            "_atomic_validate_and_publish_candidate",
            "run_scenario",
            "run_registered_actual_scenario",
            "_run_registered_actual_plan",
            "_reconstruct_actual_phase_one_receipt_gate",
            "extract_event_markers",
            "plan_semantic_events",
            "_semantic_commands_from_conversion",
            "_private_semantic_overlay",
            "_indexed_semantic_replay",
            "_emit_semantic_plan",
            "_indexed_run_manifest",
            "_indexed_execution_binding",
            "_project_repository_assertions",
        ):
            monkeypatch.setattr(runner, name, reject_impure_path(name))

        log_path = runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)
        log = json.loads(log_path.read_bytes())
        attestation = json.loads((writer.run_root / "log-attestation.json").read_bytes())
        assert log["fixture_sha256"] == original_fixture_hash
        assert attestation["fixture_sha256"] == original_fixture_hash
        assert log["fixture_sha256"] != replacement_fixture_hash
        assert attestation["fixture_sha256"] != replacement_fixture_hash
        assert log["result"] == "incomplete"
        assert log["events"] == log["receipts"] == log["deliveries"] == []
        assert log["repository_assertions"] == []
        assert impure_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_rejects_tampered_process_trace_before_terminal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged process row must agree with its pinned execution trace."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_add_json = runner._PrivatePhaseOneDiagnosticWriter.add_json
    tampered_rows: list[dict[str, object]] = []

    def tamper_process_trace(
        stage_writer: runner._PrivatePhaseOneDiagnosticWriter,
        artifact_id: str,
        artifact_type: str,
        value: object,
    ) -> str:
        if artifact_id != "process-2":
            return original_add_json(stage_writer, artifact_id, artifact_type, value)
        assert artifact_type == "process"
        assert type(value) is dict
        original_row = dict(value)
        assert type(original_row["trace_sha256"]) is str
        assert original_row["trace_sha256"] != "0" * 64
        tampered = dict(original_row)
        tampered["trace_sha256"] = "0" * 64
        assert {
            key: item for key, item in tampered.items() if key != "trace_sha256"
        } == {
            key: item for key, item in original_row.items() if key != "trace_sha256"
        }
        tampered_rows.append(tampered)
        return original_add_json(stage_writer, artifact_id, artifact_type, tampered)

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        monkeypatch.setattr(
            runner._PrivatePhaseOneDiagnosticWriter,
            "add_json",
            tamper_process_trace,
        )

        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert len(tampered_rows) == 1
        assert tampered_rows[0]["trace_sha256"] == "0" * 64
        assert preflight.verify() is None

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json", "log-attestation.json", "event-log.json",
        }
        assert terminal_paths.isdisjoint(before_tree)

        terminal_writes: list[str] = []

        def reject_terminal_write(
            _pin: runner.PinnedDirectory,
            relative: str,
            _raw: bytes,
        ) -> tuple[int, int, int, int, int, int, int, int]:
            terminal_writes.append(relative)
            pytest.fail("trace-mismatched finalizer attempted terminal write")

        monkeypatch.setattr(
            runner.PinnedDirectory,
            "write_relative_exclusive",
            reject_terminal_write,
        )
        with pytest.raises(runner.RunnerError):
            runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)

        assert terminal_writes == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert terminal_paths.isdisjoint(_task7d_run_tree_bytes(writer))
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_rejects_tampered_process_fixture_id_before_terminal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned process fixture ID must agree with the retained facade."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_add_json = runner._PrivatePhaseOneDiagnosticWriter.add_json
    original_fixture_id = phase_one.prepared_run.metadata.fixture_id
    replacement_fixture_id = next(
        fixture_id for fixture_id in runner._FIXTURE_IDS.values()
        if fixture_id != original_fixture_id
    )
    tampered_rows: list[dict[str, object]] = []

    def tamper_process_fixture(
        stage_writer: runner._PrivatePhaseOneDiagnosticWriter,
        artifact_id: str,
        artifact_type: str,
        value: object,
    ) -> str:
        if artifact_id != "process-2":
            return original_add_json(stage_writer, artifact_id, artifact_type, value)
        assert artifact_type == "process"
        assert type(value) is dict
        original_row = dict(value)
        assert original_row["fixture_id"] == original_fixture_id
        tampered = dict(original_row)
        tampered["fixture_id"] = replacement_fixture_id
        assert {
            key: item for key, item in tampered.items() if key != "fixture_id"
        } == {
            key: item for key, item in original_row.items() if key != "fixture_id"
        }
        tampered_rows.append(tampered)
        return original_add_json(stage_writer, artifact_id, artifact_type, tampered)

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        monkeypatch.setattr(
            runner._PrivatePhaseOneDiagnosticWriter,
            "add_json",
            tamper_process_fixture,
        )

        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert len(tampered_rows) == 1
        assert tampered_rows[0]["fixture_id"] == replacement_fixture_id
        assert preflight.verify() is None

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json", "log-attestation.json", "event-log.json",
        }
        assert terminal_paths.isdisjoint(before_tree)

        terminal_writes: list[str] = []

        def reject_terminal_write(
            _pin: runner.PinnedDirectory,
            relative: str,
            _raw: bytes,
        ) -> tuple[int, int, int, int, int, int, int, int]:
            terminal_writes.append(relative)
            pytest.fail("fixture-mismatched finalizer attempted terminal write")

        monkeypatch.setattr(
            runner.PinnedDirectory,
            "write_relative_exclusive",
            reject_terminal_write,
        )
        with pytest.raises(runner.RunnerError):
            runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)

        assert terminal_writes == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert terminal_paths.isdisjoint(_task7d_run_tree_bytes(writer))
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_rejects_tampered_executable_build_before_terminal_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalization redundantly binds executable evidence to its live phase."""

    original_add_json = runner.EvidenceWriter.add_json
    original_live_artifact_join = runner._registered_actual_phase_two_live_artifact_join
    tampered_rows: list[dict[str, object]] = []
    bypass_calls: list[tuple[str, str]] = []
    replacement_build_id = "test-only-mismatched-supported-build"

    def tamper_phase_two_executable(
        writer: runner.EvidenceWriter,
        artifact_id: str,
        artifact_type: str,
        value: object,
    ) -> str:
        if artifact_id != "client-executable-2":
            return original_add_json(writer, artifact_id, artifact_type, value)
        assert artifact_type == "client_executable"
        assert type(value) is dict
        original_row = dict(value)
        original_build_id = original_row["supported_build_id"]
        assert type(original_build_id) is str and original_build_id
        assert original_build_id != replacement_build_id
        tampered = dict(original_row)
        tampered["supported_build_id"] = replacement_build_id
        assert {
            key: item for key, item in tampered.items() if key != "supported_build_id"
        } == {
            key: item for key, item in original_row.items() if key != "supported_build_id"
        }
        tampered_rows.append(tampered)
        return original_add_json(writer, artifact_id, artifact_type, tampered)

    def bypass_only_injected_live_join(
        prepared_phase: runner._RegisteredActualPreparedPhase,
        live_phase: runner.RegisteredLivePhase,
        *,
        plan: runner.RegisteredActualScenarioPlan,
        registration: runner.RunnerOwnedExecutableRegistration,
        trusted_context: object,
        phase_two_trusted: object,
        entries: Mapping[str, Mapping[str, object]],
    ) -> None:
        try:
            original_live_artifact_join(
                prepared_phase,
                live_phase,
                plan=plan,
                registration=registration,
                trusted_context=trusted_context,
                phase_two_trusted=phase_two_trusted,
                entries=entries,
            )
        except runner.RunnerError as error:
            # The ordinary post-launch reader is intentionally stronger than
            # this finalizer test and catches the injected row first.  Execute
            # it normally, suppressing only this known mismatch so the
            # finalizer's independent pre-write closure is exercised.
            if (prepared_phase.execution_id != "execution-2"
                    or len(tampered_rows) != 1):
                raise
            entry = entries.get("client-executable-2")
            assert entry is not None
            raw = runner._writer_artifact_bytes(
                prepared_phase.writer,
                "client-executable-2",
                "client_executable",
                control_pin=prepared_phase.control_pin,
            )
            assert json.loads(raw) == tampered_rows[0]
            if live_phase.supported_build_id == replacement_build_id:
                assert str(error) == "registered actual phase-two live host join is invalid"
            else:
                assert str(error) == "registered actual phase-two executable evidence is invalid"
            bypass_calls.append((prepared_phase.execution_id, str(error)))

    monkeypatch.setattr(runner.EvidenceWriter, "add_json", tamper_phase_two_executable)
    monkeypatch.setattr(
        runner,
        "_registered_actual_phase_two_live_artifact_join",
        bypass_only_injected_live_join,
    )
    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    phase_two_live: runner.RegisteredLivePhase | None = None
    original_live_build_id: str | None = None

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        assert len(tampered_rows) == 1
        phase_two_live = launched.post_launch_token.live_phase
        original_live_build_id = phase_two_live.supported_build_id
        assert original_live_build_id != replacement_build_id
        # Match the forged row at the mutable live facade.  The scoped
        # earlier-join bypass below admits this one controlled inconsistency,
        # leaving finalization's direct trusted-executable binding as the
        # boundary under test.
        object.__setattr__(phase_two_live, "supported_build_id", replacement_build_id)
        assert phase_two_live.supported_build_id == replacement_build_id
        assert phase_two_live.trusted_executable.supported_build_id != replacement_build_id
        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert preflight.verify() is None
        assert (
            "execution-2", "registered actual phase-two executable evidence is invalid",
        ) in bypass_calls
        assert (
            "execution-2", "registered actual phase-two live host join is invalid",
        ) in bypass_calls

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json", "log-attestation.json", "event-log.json",
        }
        assert terminal_paths.isdisjoint(before_tree)

        terminal_writes: list[str] = []

        def reject_terminal_write(
            _pin: runner.PinnedDirectory,
            relative: str,
            _raw: bytes,
        ) -> tuple[int, int, int, int, int, int, int, int]:
            terminal_writes.append(relative)
            pytest.fail("executable-mismatched finalizer attempted terminal write")

        monkeypatch.setattr(
            runner.PinnedDirectory,
            "write_relative_exclusive",
            reject_terminal_write,
        )
        with pytest.raises(runner.RunnerError, match="finalization execution"):
            runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)

        assert terminal_writes == []
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert terminal_paths.isdisjoint(_task7d_run_tree_bytes(writer))
    finally:
        if phase_two_live is not None and original_live_build_id is not None:
            object.__setattr__(phase_two_live, "supported_build_id", original_live_build_id)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_stops_after_raw_handoff_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-index raw-handoff change fails closed before later terminals."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_write_relative_exclusive = runner.PinnedDirectory.write_relative_exclusive

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert preflight.verify() is None

        handoff = phase_two_diagnostic.post_launch_token.raw_control_handoff
        raw_relative = "brain-shim-capture-index.jsonl"
        frozen_raw, frozen_identity = handoff.raw_snapshots[raw_relative]
        raw_target = writer.control_root / raw_relative
        assert raw_target.read_bytes() == frozen_raw
        assert runner.PinnedDirectory._regular_identity(raw_target.lstat()) == frozen_identity

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json", "log-attestation.json", "event-log.json",
        }
        terminal_relative = PurePosixPath(
            "runs", writer.run_id, "evidence-index.json",
        ).as_posix()
        assert terminal_paths.isdisjoint(before_tree)
        writes: list[str] = []
        mutations: list[bytes] = []

        def write_then_mutate_raw_handoff(
            pin: runner.PinnedDirectory,
            relative: str,
            raw: bytes,
        ) -> tuple[int, int, int, int, int, int, int, int]:
            identity = original_write_relative_exclusive(pin, relative, raw)
            writes.append(relative)
            if relative == terminal_relative:
                assert pin.path == writer.control_root
                with raw_target.open("ab") as handle:
                    handle.write(b'{"test_only":"handoff-drift"}\n')
                mutations.append(raw_target.read_bytes())
            return identity

        monkeypatch.setattr(
            runner.PinnedDirectory,
            "write_relative_exclusive",
            write_then_mutate_raw_handoff,
        )
        with pytest.raises(runner.RunnerError):
            runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)

        assert writes == [terminal_relative]
        assert len(mutations) == 1
        assert mutations[0].startswith(frozen_raw)
        assert mutations[0] != frozen_raw
        assert raw_target.read_bytes() == mutations[0]
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        after_tree = _task7d_run_tree_bytes(writer)
        # Identity-guarded deletion has an unavoidable portable
        # check-to-unlink race, so this safe failure preserves the already
        # written index rather than risking removal of an attacker replacement.
        assert set(after_tree) == set(before_tree) | {"evidence-index.json"}
        assert (writer.run_root / "evidence-index.json").is_file()
        assert not (writer.run_root / "log-attestation.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_two_phase_finalizer_stops_after_receipt_gate_facts_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-index receipt-facts change cannot reach later terminal writes."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    original_write_relative_exclusive = runner.PinnedDirectory.write_relative_exclusive
    original_facts = receipt_gate.receipt_facts
    replacement_effect_digest = (
        "0" * 64 if original_facts.effect_digest != "0" * 64 else "f" * 64
    )
    replacement_facts = replace(
        original_facts, effect_digest=replacement_effect_digest,
    )

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        assert replacement_facts.selection is original_facts.selection
        assert replacement_facts.effect_digest != original_facts.effect_digest
        phase_two_diagnostic = runner._index_registered_actual_phase_two_host_diagnostic(
            phase_one_diagnostic,
            launched.post_launch_token,
        )
        preflight = runner._preflight_registered_actual_two_phase_host_diagnostic(
            phase_one_diagnostic,
            phase_two_diagnostic,
            receipt_gate,
        )
        assert preflight.verify() is None

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_paths = {
            "evidence-index.json", "log-attestation.json", "event-log.json",
        }
        terminal_relative = PurePosixPath(
            "runs", writer.run_id, "evidence-index.json",
        ).as_posix()
        assert terminal_paths.isdisjoint(before_tree)
        writes: list[str] = []
        mutations: list[object] = []

        def write_then_mutate_receipt_facts(
            pin: runner.PinnedDirectory,
            relative: str,
            raw: bytes,
        ) -> tuple[int, int, int, int, int, int, int, int]:
            identity = original_write_relative_exclusive(pin, relative, raw)
            writes.append(relative)
            if relative == terminal_relative:
                assert pin.path == writer.control_root
                object.__setattr__(receipt_gate, "receipt_facts", replacement_facts)
                assert receipt_gate.__post_init__() is None
                mutations.append(receipt_gate.receipt_facts)
            return identity

        monkeypatch.setattr(
            runner.PinnedDirectory,
            "write_relative_exclusive",
            write_then_mutate_receipt_facts,
        )
        with pytest.raises(runner.RunnerError, match="receipt gate changed"):
            runner._finalize_registered_actual_two_phase_host_diagnostic(preflight)

        assert writes == [terminal_relative]
        assert mutations == [replacement_facts]
        assert receipt_gate.receipt_facts is replacement_facts
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        after_tree = _task7d_run_tree_bytes(writer)
        # Once evidence-index.json exists, safe failure preserves the support
        # residue rather than deleting a path that could be attacker-replaced.
        assert set(after_tree) == set(before_tree) | {"evidence-index.json"}
        assert (writer.run_root / "evidence-index.json").is_file()
        assert not (writer.run_root / "log-attestation.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
    finally:
        object.__setattr__(receipt_gate, "receipt_facts", original_facts)
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_token_expected_artifact_types_close_host_join_before_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-key token type-map substitution cannot reach diagnostic staging."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    impure_calls: list[str] = []

    def reject_impure_path(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            impure_calls.append(name)
            pytest.fail("corrupted phase-two token reached " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        token = launched.post_launch_token
        assert type(token) is runner._RegisteredActualPhaseTwoPostLaunchToken
        expected_types = {
            artifact_id: record[0]
            for artifact_id, record in token.artifact_provenance.items()
        }
        assert type(token.expected_artifact_types) is MappingProxyType
        assert dict(token.expected_artifact_types) == expected_types
        with pytest.raises(TypeError):
            token.expected_artifact_types["unexpected"] = "file_capture"  # type: ignore[index]

        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not {
            "evidence-index.json", "log-attestation.json", "event-log.json",
            "run-manifest.json",
        } & set(before_tree)

        for owner, name in (
            (runner.PinnedDirectory, "create_private_child"),
            (runner, "_finalize_registered_actual_two_phase_host_diagnostic"),
            (runner, "_finalize_log"),
            (runner, "_atomic_validate_and_publish_candidate"),
            (runner, "run_scenario"),
            (runner, "run_registered_actual_scenario"),
            (runner, "_run_registered_actual_plan"),
        ):
            monkeypatch.setattr(owner, name, reject_impure_path(name))

        original_types = token.expected_artifact_types
        corrupted_types = dict(expected_types)
        corrupted_id = next(iter(corrupted_types))
        corrupted_types[corrupted_id] = "wrong-host-artifact-type"
        object.__setattr__(
            token,
            "expected_artifact_types",
            MappingProxyType(corrupted_types),
        )
        try:
            with pytest.raises(runner.RunnerError):
                runner._read_registered_actual_two_phase_host_join(
                    phase_one_diagnostic,
                    token,
                )
            assert writer.entries == before_entries
            assert writer.write_identities == before_identities
            assert _task7d_run_tree_bytes(writer) == before_tree
            assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
            assert impure_calls == []
        finally:
            object.__setattr__(token, "expected_artifact_types", original_types)

        # Restore the exact immutable map before the fixture closes its pins;
        # a clean token can again prove the same host-only join without writes.
        assert dict(token.expected_artifact_types) == expected_types
        assert type(runner._read_registered_actual_two_phase_host_join(
            phase_one_diagnostic,
            token,
        )) is runner._RegisteredActualTwoPhaseHostJoin
        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert impure_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_without_nested_shim_before_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native-only launch cannot create a private phase-two diagnostic."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=False,
        )
    )
    writer = phase_one.prepared_run.writer
    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("native-only phase-two diagnostic reached public route " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert nested_results == []
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_names = {
            "evidence-index.json",
            "log-attestation.json",
            "event-log.json",
            "run-manifest.json",
        }
        assert terminal_names.isdisjoint(before_tree)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))

        for name in (
            "run_scenario",
            "run_registered_actual_scenario",
            "_run_registered_actual_plan",
            "_finalize_log",
            "_seal_and_attest_log",
            "_publish_sealed_log",
            "_atomic_validate_and_publish_candidate",
        ):
            monkeypatch.setattr(runner, name, reject_public_route(name))

        with pytest.raises(runner.RunnerError):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic,
                launched.post_launch_token,
            )

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert terminal_names.isdisjoint(_task7d_run_tree_bytes(writer))
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_frozen_dynamic_capture_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-token dynamic capture replacement fails before diagnostic staging."""

    phase_one, phase_one_diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("frozen dynamic capture replacement reached public route " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        handoff = provenance.raw_control_handoff
        result_relative = next(
            relative for relative in handoff.raw_snapshots
            if relative.startswith("brain-shim-captures/") and relative.endswith("/result.json")
        )
        frozen_raw, frozen_identity = handoff.raw_snapshots[result_relative]
        target = writer.control_root / result_relative
        assert target.read_bytes() == frozen_raw
        before_inode, after_inode = _task7d_replace_same_bytes(target)
        assert before_inode == frozen_identity[1]
        assert after_inode != frozen_identity[1]
        assert target.read_bytes() == frozen_raw

        # The attack itself has changed the control tree; this snapshot proves
        # the indexer adds no stage, base evidence, or terminal output after it.
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_names = {
            "evidence-index.json",
            "log-attestation.json",
            "event-log.json",
            "run-manifest.json",
        }
        assert terminal_names.isdisjoint(before_tree)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))

        for name in (
            "run_scenario",
            "run_registered_actual_scenario",
            "_run_registered_actual_plan",
            "_finalize_log",
            "_seal_and_attest_log",
            "_publish_sealed_log",
            "_atomic_validate_and_publish_candidate",
        ):
            monkeypatch.setattr(runner, name, reject_public_route(name))

        with pytest.raises(runner.RunnerError):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic,
                provenance,
            )

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert terminal_names.isdisjoint(_task7d_run_tree_bytes(writer))
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_host_diagnostic_rejects_phase_one_receipt_replacement_after_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained phase-one receipt artifact cannot be re-adopted by staging."""

    phase_one, phase_one_diagnostic, receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("phase-one receipt replacement reached public route " + name)
        return reject

    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(nested_results) == 1
        provenance = launched.post_launch_token
        assert provenance.receipt_gate is receipt_gate
        durable_command_id = phase_one_diagnostic.conversion.observation_ids[2]
        durable_artifact_id = phase_one_diagnostic.conversion.receipt_artifact_ids[
            durable_command_id
        ]["durable"]
        assert durable_artifact_id == receipt_gate.selection.durable_artifact_id
        durable_entry = next(
            entry for entry in writer.entries if entry["id"] == durable_artifact_id
        )
        assert durable_entry["type"] == "consumption_receipt"
        target = writer.run_root / str(durable_entry["relative_path"])
        frozen_bytes = target.read_bytes()
        before_inode, after_inode = _task7d_replace_same_bytes(target)
        assert before_inode != after_inode
        assert target.read_bytes() == frozen_bytes

        # Snapshot after the attack itself.  The indexer must reject before
        # allocating a private stage or changing any retained base evidence.
        before_entries = [dict(entry) for entry in writer.entries]
        before_identities = dict(writer.write_identities)
        before_tree = _task7d_run_tree_bytes(writer)
        terminal_names = {
            "evidence-index.json",
            "log-attestation.json",
            "event-log.json",
            "run-manifest.json",
        }
        assert terminal_names.isdisjoint(before_tree)
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))

        for name in (
            "run_scenario",
            "run_registered_actual_scenario",
            "_run_registered_actual_plan",
            "_finalize_log",
            "_seal_and_attest_log",
            "_publish_sealed_log",
            "_atomic_validate_and_publish_candidate",
            # The receipt replacement must be rejected by the frozen facade
            # before staging can fall back to a live phase-one reader.
            "_reconstruct_actual_phase_one_receipt_gate",
        ):
            monkeypatch.setattr(runner, name, reject_public_route(name))

        with pytest.raises(runner.RunnerError):
            runner._index_registered_actual_phase_two_host_diagnostic(
                phase_one_diagnostic,
                provenance,
            )

        assert writer.entries == before_entries
        assert writer.write_identities == before_identities
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert terminal_names.isdisjoint(_task7d_run_tree_bytes(writer))
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_post_launch_handoff_retains_optional_preapply_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional preapply stream is frozen when phase two actually uses it."""

    from brainlib.contracts import compute_corpus_revision

    manifest_relative = (
        ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
    )
    staged_manifests: list[bytes] = []
    public_route_calls: list[str] = []

    def stage_manifest(phase_one: runner._RegisteredActualPreparedPhase) -> None:
        workspace = phase_one.prepared_run.prepared.workspace
        records = runner.LedgerStore(runner.RepoPaths.discover(workspace)).load_all()
        raw = _encoded({
            "schema_version": 1,
            "expected_corpus_revision": compute_corpus_revision(records.values()),
            "change_intent": "routine",
            "approval_event_id": None,
            "citation_rewrites": [],
            "link_candidate_runs": [],
            "changes": [],
        }) + b"\n"
        manifest = workspace / manifest_relative
        manifest.parent.mkdir(mode=0o700, parents=True)
        manifest.write_bytes(raw)
        staged_manifests.append(raw)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("optional preapply handoff reached public route " + name)
        return reject

    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    phase_one, diagnostic, _receipt_gate, _phase_two, launched, nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=True,
            before_phase_two_prepare=stage_manifest,
            nested_argv=("--json", "wiki", "apply", "--manifest", manifest_relative),
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        assert len(staged_manifests) == len(nested_results) == 1
        handoff = launched.post_launch_token.raw_control_handoff
        assert handoff.phase_one_raw_preapply_manifests == (
            diagnostic.conversion.raw_preapply_manifests
        )
        assert "runner-preapply-manifests.jsonl" not in handoff.raw_absent_paths
        journal, _identity = handoff.raw_snapshots["runner-preapply-manifests.jsonl"]
        assert journal == (writer.control_root / "runner-preapply-manifests.jsonl").read_bytes()
        assert journal.startswith(handoff.phase_one_raw_preapply_manifests)
        phase_preapply = journal[len(handoff.phase_one_raw_preapply_manifests):]
        assert phase_preapply and phase_preapply.endswith(b"\n")
        rows = [json.loads(line) for line in phase_preapply.splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["argv"] == [
            "./brain", "--json", "wiki", "apply", "--manifest", manifest_relative,
        ]
        capture_path = row["capture_path"]
        assert type(capture_path) is str
        captured, _capture_identity = handoff.raw_snapshots[capture_path]
        assert captured == staged_manifests[0]
        _task7d_assert_frozen_handoff_snapshot(
            writer.control_root, "runner-preapply-manifests.jsonl",
            handoff.raw_snapshots["runner-preapply-manifests.jsonl"],
        )
        _task7d_assert_frozen_handoff_snapshot(
            writer.control_root, capture_path, handoff.raw_snapshots[capture_path],
        )
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    "attack",
    ("result-replacement", "runner-state-replacement", "result-deletion"),
)
def test_actual_phase_two_post_launch_handoff_rejects_post_freeze_drift_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """Only the existing provenance verifier may observe frozen-handoff drift."""

    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("post-freeze handoff drift reached public route " + name)
        return reject

    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    phase_one, diagnostic, receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        handoff = token.raw_control_handoff
        result_relative = next(
            relative for relative in handoff.raw_snapshots
            if relative.startswith("brain-shim-captures/") and relative.endswith("/result.json")
        )
        if attack == "runner-state-replacement":
            target = phase_two.runner_state.path
        else:
            target = writer.control_root / result_relative
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)
        if attack.endswith("replacement"):
            before, after = _task7d_replace_same_bytes(target)
            assert after != before
        else:
            assert attack == "result-deletion"
            target.unlink()
            assert not target.exists()

        with pytest.raises(runner.RunnerError):
            runner._verify_registered_actual_phase_two_post_launch_provenance(
                prepared_run, diagnostic, receipt_gate, phase_two, launched.live_phase, token,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_receipt_verifiers_reject_forged_in_memory_receipt_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private receipt verifiers recompute raw and trace ranges, not just chains."""

    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("forged in-memory receipt fact reached public route " + name)
        return reject

    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    phase_one, _diagnostic, _receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        ledgers = token.receipt_ledgers
        raw_ledger = phase_two.control_pin.read_relative(ledgers.raw_ledger.relative_path)
        raw_rows = runner._validate_private_receipt_ledger_rows(
            raw_ledger, ledgers.raw_ledger,
        )
        trace_ledger = phase_two.control_pin.read_relative(ledgers.trace_ledger.relative_path)
        trace_rows = runner._validate_private_receipt_ledger_rows(
            trace_ledger, ledgers.trace_ledger,
        )
        assert raw_rows and len(trace_rows) >= 3

        # Start from the exact decoded sidecar rows.  The direct verifier
        # accepts them before we alter only a metadata fact in memory.
        original_raw = runner._verify_private_raw_receipt_records(
            phase_two.control_pin, ledgers, raw_rows,
        )
        assert original_raw[0]
        assert runner._verify_private_trace_receipt_records(
            phase_two.control_pin, trace_rows,
        ) == token.raw_control_handoff.trace_snapshot

        forged_raw_rows: list[dict[str, object]] = []
        forged_append = False
        for row in raw_rows:
            copied_row = dict(row)
            receipt = row["receipt"]
            assert type(receipt) is dict
            if not forged_append and receipt["kind"] == "append":
                relative = receipt["relative_path"]
                assert type(relative) is str
                raw = phase_two.control_pin.read_relative(relative)
                prior = receipt["prior_bytes"]
                completed = receipt["completed_bytes"]
                original_digest = receipt["appended_sha256"]
                assert type(prior) is int and type(completed) is int
                assert type(original_digest) is str
                assert hashlib.sha256(raw[prior:completed]).hexdigest() == original_digest
                forged_digest = "0" * 64 if original_digest != "0" * 64 else "f" * 64
                copied_receipt = dict(receipt)
                copied_receipt["appended_sha256"] = forged_digest
                copied_row["receipt"] = copied_receipt
                forged_append = True
            forged_raw_rows.append(copied_row)
        assert forged_append is True

        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)
        with pytest.raises(runner.RunnerError):
            runner._verify_private_raw_receipt_records(
                phase_two.control_pin, ledgers, tuple(forged_raw_rows),
            )

        # This is an actual child-created one-shot capture and a real write
        # receipt, but it did not exist in the retained pre-Popen tree.
        dynamic_relative = next(
            row["receipt"]["relative_path"]
            for row in raw_rows
            if (type(row["receipt"]) is dict
                and row["receipt"]["kind"] == "write"
                and row["receipt"]["relative_path"].startswith("brain-shim-captures/"))
        )
        assert type(dynamic_relative) is str
        assert dynamic_relative not in ledgers.prelaunch_tree
        dynamic_snapshot = token.raw_control_handoff.raw_snapshots[dynamic_relative]
        forged_ledgers = runner._RegisteredActualPhaseTwoReceiptLedgers(
            run_id=ledgers.run_id,
            execution_id=ledgers.execution_id,
            raw_ledger=ledgers.raw_ledger,
            trace_ledger=ledgers.trace_ledger,
            raw_prelaunch=ledgers.raw_prelaunch,
            prelaunch_tree={
                **ledgers.prelaunch_tree,
                dynamic_relative: dynamic_snapshot[1],
            },
        )
        with pytest.raises(runner.RunnerError):
            runner._verify_private_raw_receipt_records(
                phase_two.control_pin, forged_ledgers, raw_rows,
            )

        # Preserve the ledger's syntactic completed/prior chain across a
        # middle pair.  Only recomputing the retained trace prefixes can catch
        # this forged pair; the final trace digest remains untouched.
        forged_trace_rows = [dict(row) for row in trace_rows]
        middle = dict(forged_trace_rows[1]["receipt"])
        successor = dict(forged_trace_rows[2]["receipt"])
        original_completed = middle["completed_sha256"]
        assert type(original_completed) is str
        forged_completed = (
            "0" * 64 if original_completed != "0" * 64 else "f" * 64
        )
        middle["completed_sha256"] = forged_completed
        successor["prior_sha256"] = forged_completed
        forged_trace_rows[1]["receipt"] = middle
        forged_trace_rows[2]["receipt"] = successor
        assert forged_trace_rows[2]["receipt"]["prior_sha256"] == (
            forged_trace_rows[1]["receipt"]["completed_sha256"]
        )
        with pytest.raises(runner.RunnerError):
            runner._verify_private_trace_receipt_records(
                phase_two.control_pin, tuple(forged_trace_rows),
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize("bound", ("start_sequence", "end_sequence"))
def test_actual_phase_two_handoff_freeze_rejects_raw_index_trace_interval_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
) -> None:
    """An in-memory raw index cannot point outside its retained trace interval."""

    phase_one, diagnostic, _receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        verification = runner._verify_registered_actual_phase_two_receipt_ledgers(
            phase_two, token.receipt_ledgers,
        )
        index_relative = "brain-shim-capture-index.jsonl"
        index_raw, index_identity = verification.raw_snapshots[index_relative]
        prefix = diagnostic.conversion.raw_index
        assert index_raw.startswith(prefix)
        rows = index_raw[len(prefix):].splitlines(keepends=True)
        assert len(rows) == 1 and rows[0].endswith(b"\n")
        row = json.loads(rows[0])
        assert type(row) is dict
        start = row["start_sequence"]
        end = row["end_sequence"]
        assert type(start) is int and type(end) is int and end > start + 1
        if bound == "start_sequence":
            row[bound] = start + 1
        else:
            assert bound == "end_sequence"
            row[bound] = end - 1
        forged_index = prefix + _encoded(row) + b"\n"
        assert len(forged_index) == len(index_raw)
        forged_identity = (
            *index_identity[:5], len(forged_index), *index_identity[6:],
        )
        forged_verification = runner._RegisteredActualPhaseTwoReceiptLedgerVerification(
            trace_snapshot=verification.trace_snapshot,
            raw_receipt_paths=verification.raw_receipt_paths,
            raw_snapshots={
                **verification.raw_snapshots,
                index_relative: (forged_index, forged_identity),
            },
            raw_absent_paths=verification.raw_absent_paths,
            raw_append_command_ids=verification.raw_append_command_ids,
        )
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)

        with pytest.raises(runner.RunnerError):
            runner._freeze_registered_actual_phase_two_raw_control_handoff(
                diagnostic,
                phase_two,
                forged_verification,
                trace=runner._registered_actual_phase_two_canonical_retained_trace(
                    phase_two,
                ),
                expected_trace_identity=token.trace_identity,
                expected_trace_sha256=token.trace_sha256,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_handoff_freeze_rejects_missing_required_preapply_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained wiki-apply command requires its matching frozen preapply row."""

    from brainlib.contracts import compute_corpus_revision

    manifest_relative = (
        ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
    )

    def stage_manifest(phase_one: runner._RegisteredActualPreparedPhase) -> None:
        workspace = phase_one.prepared_run.prepared.workspace
        records = runner.LedgerStore(runner.RepoPaths.discover(workspace)).load_all()
        raw = _encoded({
            "schema_version": 1,
            "expected_corpus_revision": compute_corpus_revision(records.values()),
            "change_intent": "routine",
            "approval_event_id": None,
            "citation_rewrites": [],
            "link_candidate_runs": [],
            "changes": [],
        }) + b"\n"
        manifest = workspace / manifest_relative
        manifest.parent.mkdir(mode=0o700, parents=True)
        manifest.write_bytes(raw)

    phase_one, diagnostic, _receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=True,
            before_phase_two_prepare=stage_manifest,
            nested_argv=("--json", "wiki", "apply", "--manifest", manifest_relative),
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        verification = runner._verify_registered_actual_phase_two_receipt_ledgers(
            phase_two, token.receipt_ledgers,
        )
        preapply_relative = "runner-preapply-manifests.jsonl"
        preapply_snapshot = verification.raw_snapshots[preapply_relative]
        assert preapply_relative in verification.raw_receipt_paths
        assert preapply_snapshot[0].startswith(diagnostic.conversion.raw_preapply_manifests)
        assert preapply_snapshot[0] != diagnostic.conversion.raw_preapply_manifests

        # Model an otherwise well-formed private verification that forgot the
        # required phase-two append entirely.  This changes no raw file or
        # sidecar and drives the freeze closure directly.
        forged_snapshots = dict(verification.raw_snapshots)
        del forged_snapshots[preapply_relative]
        forged_verification = runner._RegisteredActualPhaseTwoReceiptLedgerVerification(
            trace_snapshot=verification.trace_snapshot,
            raw_receipt_paths=frozenset(
                relative for relative in verification.raw_receipt_paths
                if relative != preapply_relative
            ),
            raw_snapshots=forged_snapshots,
            raw_absent_paths=frozenset({
                *verification.raw_absent_paths,
                preapply_relative,
            }),
            raw_append_command_ids={
                relative: command_ids
                for relative, command_ids in verification.raw_append_command_ids.items()
                if relative != preapply_relative
            },
        )
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)

        with pytest.raises(runner.RunnerError):
            runner._freeze_registered_actual_phase_two_raw_control_handoff(
                diagnostic,
                phase_two,
                forged_verification,
                trace=runner._registered_actual_phase_two_canonical_retained_trace(
                    phase_two,
                ),
                expected_trace_identity=token.trace_identity,
                expected_trace_sha256=token.trace_sha256,
            )

        # A present row is still insufficient if its outer sidecar ownership
        # is detached from the inner preapply command id.
        preapply_owner_ids = verification.raw_append_command_ids[preapply_relative]
        assert len(preapply_owner_ids) == 1
        forged_owner_id = (
            "shim-command-" + "0" * 32
            if preapply_owner_ids[0] != "shim-command-" + "0" * 32
            else "shim-command-" + "f" * 32
        )
        forged_owner_verification = runner._RegisteredActualPhaseTwoReceiptLedgerVerification(
            trace_snapshot=verification.trace_snapshot,
            raw_receipt_paths=verification.raw_receipt_paths,
            raw_snapshots=verification.raw_snapshots,
            raw_absent_paths=verification.raw_absent_paths,
            raw_append_command_ids={
                **verification.raw_append_command_ids,
                preapply_relative: (forged_owner_id,),
            },
        )
        with pytest.raises(runner.RunnerError):
            runner._freeze_registered_actual_phase_two_raw_control_handoff(
                diagnostic,
                phase_two,
                forged_owner_verification,
                trace=runner._registered_actual_phase_two_canonical_retained_trace(
                    phase_two,
                ),
                expected_trace_identity=token.trace_identity,
                expected_trace_sha256=token.trace_sha256,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_handoff_freeze_rejects_forged_fixed_raw_append_owner_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixed raw journals retain their outer sidecar command ownership."""

    phase_one, diagnostic, _receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        verification = runner._verify_registered_actual_phase_two_receipt_ledgers(
            phase_two, token.receipt_ledgers,
        )
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)

        for relative in (
            "brain-shim-capture-index.jsonl",
            "brain-shim-command-log.jsonl",
        ):
            owner_ids = verification.raw_append_command_ids[relative]
            assert len(owner_ids) == 1
            forged_owner_id = (
                "shim-command-" + "0" * 32
                if owner_ids[0] != "shim-command-" + "0" * 32
                else "shim-command-" + "f" * 32
            )
            forged_verification = runner._RegisteredActualPhaseTwoReceiptLedgerVerification(
                trace_snapshot=verification.trace_snapshot,
                raw_receipt_paths=verification.raw_receipt_paths,
                raw_snapshots=verification.raw_snapshots,
                raw_absent_paths=verification.raw_absent_paths,
                raw_append_command_ids={
                    **verification.raw_append_command_ids,
                    relative: (forged_owner_id,),
                },
            )
            with pytest.raises(runner.RunnerError):
                runner._freeze_registered_actual_phase_two_raw_control_handoff(
                    diagnostic,
                    phase_two,
                    forged_verification,
                    trace=runner._registered_actual_phase_two_canonical_retained_trace(
                        phase_two,
                    ),
                    expected_trace_identity=token.trace_identity,
                    expected_trace_sha256=token.trace_sha256,
                )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_handoff_freeze_rejects_legacy_raw_trace_receipt_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handoff refuses a phase-two receipt for the legacy shim trace."""

    phase_one, diagnostic, _receipt_gate, phase_two, launched, _nested_results = (
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True,
        )
    )
    writer = phase_one.prepared_run.writer
    try:
        assert type(launched) is runner._RegisteredActualPhaseTwoLiveLaunch
        token = launched.post_launch_token
        verification = runner._verify_registered_actual_phase_two_receipt_ledgers(
            phase_two, token.receipt_ledgers,
        )
        legacy_relative = "brain-shim-trace.jsonl"
        source_relative = "brain-shim-capture-index.jsonl"
        assert legacy_relative in verification.raw_absent_paths
        assert legacy_relative not in verification.raw_receipt_paths
        assert legacy_relative not in verification.raw_snapshots
        # Re-key a genuine fixed-journal snapshot and its genuine valid owner
        # tuple.  The constructed verification is otherwise internally
        # well-formed; freeze must reject this unsupported raw route itself.
        forged_verification = runner._RegisteredActualPhaseTwoReceiptLedgerVerification(
            trace_snapshot=verification.trace_snapshot,
            raw_receipt_paths=frozenset({
                *verification.raw_receipt_paths,
                legacy_relative,
            }),
            raw_snapshots={
                **verification.raw_snapshots,
                legacy_relative: verification.raw_snapshots[source_relative],
            },
            raw_absent_paths=frozenset(
                relative for relative in verification.raw_absent_paths
                if relative != legacy_relative
            ),
            raw_append_command_ids={
                **verification.raw_append_command_ids,
                legacy_relative: verification.raw_append_command_ids[source_relative],
            },
        )
        before_entries = [dict(entry) for entry in writer.entries]
        before_tree = _task7d_run_tree_bytes(writer)

        with pytest.raises(runner.RunnerError):
            runner._freeze_registered_actual_phase_two_raw_control_handoff(
                diagnostic,
                phase_two,
                forged_verification,
                trace=runner._registered_actual_phase_two_canonical_retained_trace(
                    phase_two,
                ),
                expected_trace_identity=token.trace_identity,
                expected_trace_sha256=token.trace_sha256,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_tree
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    "target",
    ("raw-capture", "trace", "raw-sidecar", "trace-sidecar"),
)
def test_actual_phase_two_receipt_closure_rejects_same_byte_replacements_before_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """No raw, trace, or receipt-sidecar replacement may become a token baseline."""

    replacements: list[tuple[Path, int, int]] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    writer_seen: list[runner.EvidenceWriter] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("receipt replacement reached public route " + name)
        return reject

    def replace_at_wait(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        writer_seen.append(writer)
        if target == "raw-capture":
            candidates = sorted(
                path for path in (writer.control_root / "brain-shim-captures").rglob("*")
                if path.is_file() and path.suffix in {".bin", ".json"}
            )
            assert candidates
            path = candidates[0]
        elif target == "trace":
            path = phase_two.trace.path
        elif target == "raw-sidecar":
            path = _task7d_phase_two_receipt_paths(writer.control_root)["raw"]
        else:
            assert target == "trace-sidecar"
            path = _task7d_phase_two_receipt_paths(writer.control_root)["trace"]
        before, after = _task7d_replace_same_bytes(path)
        replacements.append((path, before, after))

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True, on_wait=replace_at_wait,
        )

    assert len(replacements) == 1
    assert replacements[0][1] != replacements[0][2]
    assert token_captures == []
    assert public_route_calls == []
    assert len(writer_seen) == 1
    writer = writer_seen[0]
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()


def test_actual_phase_two_receipt_closure_rejects_post_popen_retained_raw_journal_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phase-one raw-journal predecessor cannot be silently re-adopted.

    Capture-index exists before this phase-two launch. Replacing it with the
    same bytes after parent sidecar allocation but before the nested child
    reaches ``finish()`` must fail its expected-present append preflight, not
    merely be noticed after an attacker-owned append has been accepted.
    """

    replacements: list[tuple[Path, int, int, bytes]] = []
    writer_seen: list[runner.EvidenceWriter] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("retained raw journal replacement reached public route " + name)
        return reject

    def replace_retained_journal(
        writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        writer_seen.append(writer)
        raw_header, raw_rows = _task7d_assert_receipt_ledger_chain(
            _task7d_phase_two_receipt_paths(writer.control_root)["raw"],
            ledger_kind="raw",
            run_id=writer.run_id,
        )
        assert raw_rows == []
        raw_prelaunch = raw_header["raw_prelaunch"]
        assert type(raw_prelaunch) is dict
        predecessor = raw_prelaunch["brain-shim-capture-index.jsonl"]
        assert type(predecessor) is dict
        journal = writer.control_root / "brain-shim-capture-index.jsonl"
        original = journal.read_bytes()
        assert predecessor["sha256"] == hashlib.sha256(original).hexdigest()
        assert predecessor["byte_count"] == len(original)
        before = journal.stat().st_ino
        replacement = journal.with_name(journal.name + ".same-byte-replacement")
        replacement.write_bytes(original)
        os.replace(replacement, journal)
        after = journal.stat().st_ino
        assert after != before
        replacements.append((journal, before, after, original))

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=True,
            on_popen=replace_retained_journal,
        )

    assert len(replacements) == 1
    journal, before, after, original = replacements[0]
    assert after != before
    assert journal.read_bytes() == original
    assert journal.stat().st_ino == after
    assert token_captures == []
    assert public_route_calls == []
    assert len(writer_seen) == 1
    writer = writer_seen[0]
    _header, raw_rows = _task7d_assert_receipt_ledger_chain(
        _task7d_phase_two_receipt_paths(writer.control_root)["raw"],
        ledger_kind="raw",
        run_id=writer.run_id,
    )
    assert all(
        row["receipt"]["relative_path"] != "brain-shim-capture-index.jsonl"
        for row in raw_rows
    )
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()


def test_actual_phase_two_receipt_closure_rejects_post_popen_raw_journal_first_create_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty journal planted after Popen preparation is never child-owned.

    The parent has already retained a metadata-only ``None`` predecessor for
    this RunnerHostRecorder journal and has created both sidecar headers. A
    staged local manifest makes the phase-two child first-use that otherwise
    absent append target. The attacker then creates the exact journal name
    before the nested local shim reaches its first append. An armed child must
    use exclusive creation rather than silently append to that attacker-owned
    inode.
    """

    planted: list[tuple[Path, int]] = []
    writer_seen: list[runner.EvidenceWriter] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance
    manifest_relative = (
        ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
    )

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("raw first-create collision reached public route " + name)
        return reject

    def stage_manifest(phase_one: runner._RegisteredActualPreparedPhase) -> None:
        manifest = phase_one.prepared_run.prepared.workspace / manifest_relative
        manifest.parent.mkdir(mode=0o700, parents=True)
        # The runner captures this before product dispatch; its semantic
        # validity is deliberately irrelevant to the append preflight.
        manifest.write_bytes(b"{}\n")

    def plant_empty_journal(
        writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        writer_seen.append(writer)
        raw_header, raw_rows = _task7d_assert_receipt_ledger_chain(
            _task7d_phase_two_receipt_paths(writer.control_root)["raw"],
            ledger_kind="raw",
            run_id=writer.run_id,
        )
        assert raw_rows == []
        raw_prelaunch = raw_header["raw_prelaunch"]
        assert type(raw_prelaunch) is dict
        assert raw_prelaunch["runner-preapply-manifests.jsonl"] is None
        journal = writer.control_root / "runner-preapply-manifests.jsonl"
        assert not journal.exists()
        journal.write_bytes(b"")
        planted.append((journal, journal.stat().st_ino))

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=True,
            before_phase_two_prepare=stage_manifest,
            nested_argv=("--json", "wiki", "apply", "--manifest", manifest_relative),
            on_popen=plant_empty_journal,
        )

    assert len(planted) == 1
    journal, planted_inode = planted[0]
    assert journal.read_bytes() == b""
    assert journal.stat().st_ino == planted_inode
    assert token_captures == []
    assert public_route_calls == []
    assert len(writer_seen) == 1
    writer = writer_seen[0]
    _header, raw_rows = _task7d_assert_receipt_ledger_chain(
        _task7d_phase_two_receipt_paths(writer.control_root)["raw"],
        ledger_kind="raw",
        run_id=writer.run_id,
    )
    assert all(
        row["receipt"]["relative_path"] != "runner-preapply-manifests.jsonl"
        for row in raw_rows
    )
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()


@pytest.mark.parametrize(
    "attack",
    ("result-leaf", "capture-directory-symlink"),
)
def test_actual_phase_two_receipt_closure_rejects_dynamic_one_shot_capture_precreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    """A child-side command boundary cannot race a foreign result pathname.

    A test-only ``sitecustomize`` hook pauses the *nested* local shim just
    after its real ``command_start`` write.  The parent therefore learns the
    fresh random command id without guessing it, plants either a foreign
    ``result.json`` leaf or a symlink in its command-scoped capture parent,
    then releases the child into ``finish()``.  This is deliberately after
    Popen and after the trace sidecar has accepted ``command_start``.
    """

    hook_directory = tmp_path / "nested-child-sitecustomize"
    hook_directory.mkdir(mode=0o700)
    signal_path = tmp_path / "nested-child-command-id"
    release_path = tmp_path / "nested-child-release"
    hook = hook_directory / "sitecustomize.py"
    hook.write_text(
        """
import os
import time
from pathlib import Path

from tests.evals import run_cross_client as _runner

_signal = Path(os.environ["_TASK7D_DYNAMIC_START_SIGNAL"])
_release = Path(os.environ["_TASK7D_DYNAMIC_START_RELEASE"])
_original_start = _runner.RunnerHostRecorder.start


def _paused_start(self):
    command_id = _original_start(self)
    staged = _signal.with_name(_signal.name + ".tmp")
    staged.write_text(command_id, encoding="utf-8")
    os.replace(staged, _signal)
    deadline = time.monotonic() + 5.0
    while not _release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("dynamic receipt test release timed out")
        time.sleep(0.01)
    return command_id


_runner.RunnerHostRecorder.start = _paused_start
""".lstrip(),
        encoding="utf-8",
    )

    attacker_threads: list[threading.Thread] = []
    attacker_errors: list[BaseException] = []
    observed_command_ids: list[str] = []
    writer_seen: list[runner.EvidenceWriter] = []
    phase_two_seen: list[runner._RegisteredActualPreparedPhase] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    foreign_leaf = b'{"foreign":"post-popen-result-leaf"}\n'
    outside = tmp_path / "outside-capture-directory"
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("dynamic one-shot collision reached public route " + name)
        return reject

    def release_child() -> None:
        if not release_path.exists():
            release_path.write_text("release\n", encoding="utf-8")

    def plant_after_command_start(writer: runner.EvidenceWriter) -> None:
        try:
            for _ in range(500):
                if signal_path.exists():
                    break
                threading.Event().wait(0.01)
            else:
                raise AssertionError("nested shim did not publish its command id")
            command_id = signal_path.read_text(encoding="utf-8")
            assert command_id.startswith("shim-command-")
            suffix = command_id.removeprefix("shim-command-")
            assert len(suffix) == 32 and all(character in "0123456789abcdef" for character in suffix)
            capture_directory = writer.control_root / "brain-shim-captures" / command_id
            result_path = capture_directory / "result.json"
            if attack == "result-leaf":
                capture_directory.mkdir(mode=0o700, parents=True)
                assert not result_path.exists()
                result_path.write_bytes(foreign_leaf)
            else:
                assert attack == "capture-directory-symlink"
                capture_directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                outside.mkdir(mode=0o700)
                assert not capture_directory.exists()
                capture_directory.symlink_to(outside, target_is_directory=True)
            observed_command_ids.append(command_id)
        except BaseException as error:
            attacker_errors.append(error)
        finally:
            # A child failure is the expected result.  Never leave its
            # deterministic start barrier waiting for this test's assertion.
            release_child()

    def arm_nested_child(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
        environment: dict[str, str],
    ) -> None:
        writer_seen.append(writer)
        phase_two_seen.append(phase_two)
        # This dict is copied only into ReceiptProcess's real nested shim;
        # do not mutate the parent process environment or phase-one probes.
        inherited_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(hook_directory), str(ROOT), inherited_pythonpath) if value
        )
        environment["_TASK7D_DYNAMIC_START_SIGNAL"] = str(signal_path)
        environment["_TASK7D_DYNAMIC_START_RELEASE"] = str(release_path)
        attacker = threading.Thread(
            target=plant_after_command_start,
            args=(writer,),
            daemon=True,
        )
        attacker_threads.append(attacker)
        attacker.start()

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    try:
        with pytest.raises(runner.RunnerError):
            _task7d_actual_phase_two_receipt_fake_launch(
                tmp_path,
                monkeypatch,
                nested_shim=True,
                on_popen=arm_nested_child,
            )
    finally:
        # If setup/launch itself raises, unblock the nested interpreter and
        # wait for the test-owned attacker before pytest moves on.
        release_child()
        for attacker in attacker_threads:
            attacker.join(timeout=5)

    assert len(attacker_threads) == 1
    assert all(not attacker.is_alive() for attacker in attacker_threads)
    assert attacker_errors == []
    assert len(writer_seen) == len(phase_two_seen) == len(observed_command_ids) == 1
    writer = writer_seen[0]
    phase_two = phase_two_seen[0]
    command_id = observed_command_ids[0]
    target_relative = f"brain-shim-captures/{command_id}/result.json"

    trace_rows = [json.loads(line) for line in phase_two.trace.path.read_bytes().splitlines()]
    assert any(
        row.get("kind") == "command_start" and row.get("command_id") == command_id
        for row in trace_rows
    )
    _raw_header, raw_rows = _task7d_assert_receipt_ledger_chain(
        _task7d_phase_two_receipt_paths(writer.control_root)["raw"],
        ledger_kind="raw",
        run_id=writer.run_id,
    )
    _trace_header, trace_rows = _task7d_assert_receipt_ledger_chain(
        _task7d_phase_two_receipt_paths(writer.control_root)["trace"],
        ledger_kind="trace",
        run_id=writer.run_id,
    )
    assert trace_rows
    assert all(row["receipt"]["relative_path"] != target_relative for row in raw_rows)
    assert token_captures == []
    assert public_route_calls == []
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()

    target = writer.control_root / target_relative
    if attack == "result-leaf":
        assert target.read_bytes() == foreign_leaf
        assert target.is_file() and not target.is_symlink()
    else:
        assert attack == "capture-directory-symlink"
        assert target.parent.is_symlink()
        assert not (outside / "result.json").exists()
        assert list(outside.rglob("*")) == []


@pytest.mark.parametrize(
    "journal",
    ("brain-shim-capture-index.jsonl", "brain-shim-command-log.jsonl"),
)
def test_actual_phase_two_prepare_rejects_phase_one_raw_predecessor_same_byte_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
) -> None:
    """Phase-two preparation cannot adopt a same-byte phase-one journal inode."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    public_route_calls: list[str] = []

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("phase-one raw predecessor replacement reached public route " + name)
        return reject

    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    try:
        predecessor = diagnostic.raw_predecessors.values[journal]
        assert predecessor is not None
        target = writer.control_root / journal
        if journal == "brain-shim-capture-index.jsonl":
            assert target.read_bytes() == diagnostic.conversion.raw_index
        else:
            assert journal == "brain-shim-command-log.jsonl"
            assert target.read_bytes() == diagnostic.conversion.raw_command_log
        before, after = _task7d_replace_same_bytes(target)
        assert predecessor.identity[1] == before
        assert after != before
        assert target.stat().st_ino == after
        before_entries = [dict(entry) for entry in writer.entries]
        before_run_tree = _task7d_run_tree_bytes(writer)
        attacked_control_tree = phase_one.control_pin.snapshot_tree()

        with pytest.raises(runner.RunnerError):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_run_tree
        assert phase_one.control_pin.snapshot_tree() == attacked_control_tree
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
        assert public_route_calls == []
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


@pytest.mark.parametrize(
    "journal",
    ("brain-shim-trace.jsonl", "runner-preapply-manifests.jsonl"),
)
def test_actual_phase_two_prepare_rejects_absent_phase_one_raw_predecessor_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    journal: str,
) -> None:
    """An absent phase-one journal cannot first appear as phase-two input."""

    phase_one, diagnostic, receipt_gate = _task7d_actual_phase_two_launcher_seed(
        tmp_path, monkeypatch,
    )
    prepared_run = phase_one.prepared_run
    writer = prepared_run.writer
    try:
        assert diagnostic.raw_predecessors.values[journal] is None
        target = writer.control_root / journal
        assert not target.exists()
        target.write_bytes(b'{"foreign":"late-phase-one-journal"}\n')
        before_entries = [dict(entry) for entry in writer.entries]
        before_run_tree = _task7d_run_tree_bytes(writer)
        attacked_control_tree = phase_one.control_pin.snapshot_tree()

        with pytest.raises(runner.RunnerError):
            runner._prepare_registered_actual_phase_two(
                prepared_run, diagnostic, receipt_gate,
            )

        assert writer.entries == before_entries
        assert _task7d_run_tree_bytes(writer) == before_run_tree
        assert phase_one.control_pin.snapshot_tree() == attacked_control_tree
        assert not (writer.control_root / "runner-trace-execution-2.jsonl").exists()
        assert all(
            not path.exists()
            for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
        )
        assert not list(writer.run_root.glob(".phase-two-diagnostic-*"))
        assert not (writer.run_root / "evidence-index.json").exists()
        assert not (writer.run_root / "event-log.json").exists()
        assert not (writer.run_root / "run-manifest.json").exists()
    finally:
        phase_one.control_pin.close()
        phase_one.workspace_pin.close()


def test_actual_phase_two_receipt_ledgers_allocation_rejects_raw_predecessor_replaced_between_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw predecessor metadata must describe the already-snapshotted tree.

    This creates the narrow allocation race: phase two snapshots the pinned
    control tree, then the same-byte-replaced phase-one capture-index inode is
    observed by its individual raw-baseline reader.  It must fail before
    either private sidecar header is adopted or a client can be spawned.
    """

    replacements: list[tuple[Path, int, int, bytes]] = []
    optional_reads: list[str] = []
    writer_seen: list[runner.EvidenceWriter] = []
    popen_calls: list[object] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("allocation-order raw predecessor race reached public route " + name)
        return reject

    def replace_between_tree_and_raw_baseline(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        writer_seen.append(writer)
        original_optional = runner._private_optional_raw_baseline

        def replace_before_capture_index_read(
            control_pin: runner.PinnedDirectory,
            relative: str,
        ) -> object:
            optional_reads.append(relative)
            if (control_pin is phase_two.control_pin
                    and relative == "brain-shim-capture-index.jsonl"
                    and not replacements):
                journal = writer.control_root / relative
                original = journal.read_bytes()
                before = journal.stat().st_ino
                replacement = journal.with_name(journal.name + ".between-baselines")
                replacement.write_bytes(original)
                os.replace(replacement, journal)
                after = journal.stat().st_ino
                assert after != before
                replacements.append((journal, before, after, original))
            return original_optional(control_pin, relative)

        monkeypatch.setattr(
            runner, "_private_optional_raw_baseline", replace_before_capture_index_read,
        )

    def should_not_spawn(
        _writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        popen_calls.append("popen")
        pytest.fail("allocation-order raw predecessor race reached Popen")

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=False,
            before_launch=replace_between_tree_and_raw_baseline,
            on_popen=should_not_spawn,
        )

    assert optional_reads
    assert len(replacements) == 1
    journal, before, after, original = replacements[0]
    assert after != before
    assert journal.read_bytes() == original
    assert journal.stat().st_ino == after
    assert popen_calls == []
    assert token_captures == []
    assert public_route_calls == []
    assert len(writer_seen) == 1
    writer = writer_seen[0]
    assert all(not path.exists() for path in _task7d_phase_two_receipt_paths(writer.control_root).values())
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()


def test_actual_phase_two_receipt_allocation_rechecks_pre_popen_control_tree_before_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw-baseline reader cannot replace sealed phase-two MCP before Popen.

    The MCP replacement occurs after the pre-Popen launch gate has returned
    but while allocation is reading raw journal predecessors.  Its bytes are
    preserved, so only the retained control-tree identity baseline can stop
    the handoff before either private receipt header is written.
    """

    optional_reads: list[str] = []
    replacements: list[tuple[Path, int, int, bytes]] = []
    writer_seen: list[runner.EvidenceWriter] = []
    phase_two_seen: list[runner._RegisteredActualPreparedPhase] = []
    popen_calls: list[object] = []
    token_captures: list[object] = []
    public_route_calls: list[str] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def reject_public_route(name: str) -> Callable[..., object]:
        def reject(*_args: object, **_kwargs: object) -> object:
            public_route_calls.append(name)
            pytest.fail("allocation-time MCP replacement reached public route " + name)
        return reject

    def replace_mcp_during_raw_baseline(
        writer: runner.EvidenceWriter,
        phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        writer_seen.append(writer)
        phase_two_seen.append(phase_two)
        original_optional = runner._private_optional_raw_baseline

        def read_raw_then_replace_mcp(
            control_pin: runner.PinnedDirectory,
            relative: str,
        ) -> object:
            optional_reads.append(relative)
            if control_pin is phase_two.control_pin and not replacements:
                receipt_paths = _task7d_phase_two_receipt_paths(writer.control_root)
                assert all(not path.exists() for path in receipt_paths.values())
                target = phase_two.mcp_config.path
                original = target.read_bytes()
                before = target.stat().st_ino
                replacement = target.with_name(
                    target.name + ".allocation-same-byte-mcp-replacement",
                )
                replacement.write_bytes(original)
                replacement.chmod(stat.S_IMODE(target.stat().st_mode))
                os.replace(replacement, target)
                after = target.stat().st_ino
                assert after != before
                replacements.append((target, before, after, original))
            return original_optional(control_pin, relative)

        monkeypatch.setattr(
            runner, "_private_optional_raw_baseline", read_raw_then_replace_mcp,
        )

    def should_not_spawn(
        _writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        popen_calls.append("popen")
        pytest.fail("allocation-time MCP replacement reached Popen")

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    for name in (
        "run_scenario",
        "run_registered_actual_scenario",
        "_run_registered_actual_plan",
    ):
        monkeypatch.setattr(runner, name, reject_public_route(name))

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=False,
            before_launch=replace_mcp_during_raw_baseline,
            on_popen=should_not_spawn,
        )

    assert optional_reads
    assert len(replacements) == 1
    target, before, after, original = replacements[0]
    assert after != before
    assert target.read_bytes() == original
    assert target.stat().st_ino == after
    assert popen_calls == []
    assert token_captures == []
    assert public_route_calls == []
    assert len(writer_seen) == len(phase_two_seen) == 1
    writer = writer_seen[0]
    phase_two = phase_two_seen[0]
    assert not phase_two.trace.path.exists()
    assert all(
        not path.exists()
        for path in _task7d_phase_two_receipt_paths(writer.control_root).values()
    )
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()


@pytest.mark.parametrize("ledger_kind", ("raw", "trace"))
def test_actual_phase_two_receipt_ledgers_reject_pre_popen_collision_without_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_kind: str,
) -> None:
    """A precreated exact ledger path cannot become the parent's baseline."""

    collisions: list[Path] = []
    spawned: list[object] = []

    def plant_collision(
        writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        path = _task7d_phase_two_receipt_paths(writer.control_root)[ledger_kind]
        path.write_bytes(b'{"foreign":"same-name-ledger"}\n')
        collisions.append(path)

    def should_not_spawn(
        _writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
        _environment: dict[str, str],
    ) -> None:
        spawned.append("popen")
        pytest.fail("precreated receipt ledger reached Popen")

    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path,
            monkeypatch,
            nested_shim=False,
            before_launch=plant_collision,
            on_popen=should_not_spawn,
        )

    assert len(collisions) == 1
    assert collisions[0].read_bytes() == b'{"foreign":"same-name-ledger"}\n'
    assert spawned == []


@pytest.mark.parametrize(
    "ledger_kind,attack",
    (
        ("raw", "missing"),
        ("trace", "missing"),
        ("raw", "malformed"),
        ("trace", "malformed"),
        ("raw", "duplicate"),
        ("trace", "duplicate"),
        ("raw", "out-of-order"),
        ("trace", "out-of-order"),
    ),
)
def test_actual_phase_two_receipt_ledgers_reject_malformed_or_nonmonotonic_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_kind: str,
    attack: str,
) -> None:
    """The final closure must parse and chain every private ledger row strictly."""

    token_captures: list[object] = []
    writer_seen: list[runner.EvidenceWriter] = []
    original_capture = runner._capture_registered_actual_phase_two_post_launch_provenance

    def capture_token(*args: object, **kwargs: object) -> object:
        token_captures.append((args, kwargs))
        return original_capture(*args, **kwargs)

    def tamper_at_wait(
        writer: runner.EvidenceWriter,
        _phase_two: runner._RegisteredActualPreparedPhase,
    ) -> None:
        writer_seen.append(writer)
        path = _task7d_phase_two_receipt_paths(writer.control_root)[ledger_kind]
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        assert len(lines) > 1
        if attack == "missing":
            path.unlink()
        elif attack == "malformed":
            path.write_bytes(lines[0] + b"{not-json}\n")
        elif attack == "duplicate":
            path.write_bytes(raw + lines[-1])
        else:
            assert attack == "out-of-order"
            assert len(lines) > 2
            path.write_bytes(lines[0] + b"".join(reversed(lines[1:])))

    monkeypatch.setattr(
        runner, "_capture_registered_actual_phase_two_post_launch_provenance", capture_token,
    )
    with pytest.raises(runner.RunnerError):
        _task7d_actual_phase_two_receipt_fake_launch(
            tmp_path, monkeypatch, nested_shim=True, on_wait=tamper_at_wait,
        )

    assert len(writer_seen) == 1
    assert token_captures == []
    writer = writer_seen[0]
    assert not (writer.run_root / "evidence-index.json").exists()
    assert not (writer.run_root / "event-log.json").exists()
    assert not (writer.run_root / "run-manifest.json").exists()
