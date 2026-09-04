from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import brainlib.cli as cli
import brainlib.commands as commands
import brainlib.ledger as ledger_module
from brainlib.cli import main
from brainlib.commands import adopt_source_version, doctor
from brainlib.contracts import (
    Anchor,
    FileFingerprint,
    SourceRepresentation,
    SourceState,
)
from brainlib.diagnostics import Diagnostic
from brainlib.layout import RepoPaths
from brainlib.ledger import CitationRewrite, LedgerStore
from brainlib.inventory import InventoryItem
from brainlib.output import CommandResult, render_human, render_json, sync_report_data
from brainlib.sync import SyncAction, SyncDecision, SyncReport, UnavailableProcessor
from tests.helpers import FIXED_NOW, StaticResolver, make_integrity_inputs


SYNC_REPORT_KEYS = {
    "corpus_revision",
    "decision_counts",
    "sampled_decisions",
    "hashed_paths",
    "hashed_path_count",
    "new_active_representations",
    "new_active_representation_count",
    "citation_rewrites",
    "citation_rewrite_count",
    "handoff_source_ids",
    "handoff_source_id_count",
    "coverage_gaps",
    "coverage_gap_count",
    "sample_limits",
    "result_manifest",
}


def test_help_lists_every_public_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "adopt-version" in capsys.readouterr().out


def test_help_doctor_import_without_fcntl_and_writes_fail_closed(
    repo_root: Path,
) -> None:
    script = """
import builtins
import io
import json
import pathlib

real_import = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_fcntl

from brainlib.cli import main

root = pathlib.Path(__import__('sys').argv[1])
help_output = io.StringIO()
doctor_output = io.StringIO()
sync_output = io.StringIO()
assert main(['--help'], cwd=root, stdout=help_output, stderr=io.StringIO()) == 0
assert main(['--json', 'doctor'], cwd=root, stdout=doctor_output, stderr=io.StringIO()) == 0
assert main(['--json', 'sync'], cwd=root, stdout=sync_output, stderr=io.StringIO()) == 1
print(json.dumps({
    'help': help_output.getvalue(),
    'doctor': json.loads(doctor_output.getvalue()),
    'sync': json.loads(sync_output.getvalue()),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo_root)],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "adopt-version" in payload["help"]
    assert payload["doctor"]["data"]["source_write_lock"]["available"] is False
    assert "safe source lock backend is unavailable" in json.dumps(payload["sync"])


def test_doctor_json_never_installs_or_executes_a_converter(tmp_path: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert main(["--json", "doctor"], cwd=tmp_path, stdout=stdout, stderr=stderr) == 0

    result = json.loads(stdout.getvalue())
    assert result["command"] == "doctor"
    assert result["ok"] is True
    assert set(result) == {"command", "ok", "data", "warnings", "errors"}
    assert stderr.getvalue() == ""


def test_adoption_parser_requires_complete_strict_arguments(tmp_path: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(
            ["--json", "source", "adopt-version", "src_" + "a" * 64],
            cwd=tmp_path,
            stdout=stdout,
            stderr=stderr,
        )
        == 2
    )

    assert stdout.getvalue() == ""
    assert "--candidate-sha256" in stderr.getvalue()


@pytest.mark.parametrize(
    "argv",
    (
        ["--j", "validate"],
        ["validate", "--fu"],
        [
            "source",
            "adopt-version",
            "src_" + "a" * 64,
            "--candidate-sha",
            "b" * 64,
            "--approval-note",
            "approved",
        ],
        [
            "source",
            "adopt-version",
            "src_" + "a" * 64,
            "--candidate-sha256",
            "b" * 64,
            "--approval-n",
            "approved",
        ],
    ),
)
def test_parser_rejects_abbreviated_flags_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("abbreviated flag reached command dispatch")

    monkeypatch.setattr(cli, "_dispatch", forbidden_dispatch)

    assert main(argv, cwd=tmp_path, stdout=stdout, stderr=stderr) == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue().startswith("brain: error:")


@pytest.mark.parametrize(
    ("argv", "expected_usage"),
    (
        (["init", "--help"], "usage: brain init"),
        (
            ["source", "adopt-version", "--help"],
            "usage: brain source adopt-version",
        ),
    ),
)
def test_subcommand_help_uses_injected_stdout_without_system_exit(
    tmp_path: Path,
    argv: list[str],
    expected_usage: str,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert main(argv, cwd=tmp_path, stdout=stdout, stderr=stderr) == 0
    assert expected_usage in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_adoption_command_handler_returns_json_safe_rewrites(
    repo_root: Path,
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    assert result.ok is True
    assert result.data["citation_rewrites"] == [
        {
            "source_id": record.source_id,
            "content_sha256": old_sha,
            "raw_path": f"_versions/{record.source_id}/{old_sha}/note.txt",
        }
    ]
    assert store.load(record.source_id).active_content_sha256 == item.sha256


def test_adoption_handler_preflights_unrelated_shards_before_commit(
    repo_root: Path,
) -> None:
    record, item, _old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)
    corrupt = repo_root / "sources/ledger" / ("src_" + "c" * 64 + ".json")
    corrupt.write_text("{truncated", encoding="utf-8")

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    assert result.ok is False
    corrupt.unlink()
    assert store.load(record.source_id).state is SourceState.INTEGRITY_ERROR
    assert not (repo_root / "sources/raw/_versions" / record.source_id).exists()


@pytest.mark.parametrize(
    "failure_stage",
    [
        "temporary-open",
        "temporary-write",
        "replace",
        "directory-fsync",
    ],
)
def test_adoption_handler_reports_committed_record_when_summary_publication_fails(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)
    summary_parent_inode = (repo_root / "sources").stat().st_ino
    if failure_stage == "temporary-open":
        real_open = ledger_module.os.open

        def fail_summary_temporary_open(
            path: str | os.PathLike[str], *args: object, **kwargs: object
        ) -> int:
            directory_fd = kwargs.get("dir_fd")
            if (
                os.fspath(path).startswith(".brain-tmp-")
                and isinstance(directory_fd, int)
                and os.fstat(directory_fd).st_ino == summary_parent_inode
            ):
                raise OSError("summary temporary open failed")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(ledger_module.os, "open", fail_summary_temporary_open)
        monkeypatch.setattr(
            ledger_module, "_OPEN_SUPPORT_MARKER", fail_summary_temporary_open
        )
    elif failure_stage == "temporary-write":
        real_open = ledger_module.os.open
        real_fdopen = ledger_module.os.fdopen
        summary_temporary_descriptors: set[int] = set()

        def observe_summary_temporary_open(
            path: str | os.PathLike[str], *args: object, **kwargs: object
        ) -> int:
            descriptor = real_open(path, *args, **kwargs)
            directory_fd = kwargs.get("dir_fd")
            if (
                os.fspath(path).startswith(".brain-tmp-")
                and isinstance(directory_fd, int)
                and os.fstat(directory_fd).st_ino == summary_parent_inode
            ):
                summary_temporary_descriptors.add(descriptor)
            return descriptor

        def fail_summary_temporary_write(
            descriptor: int, *args: object, **kwargs: object
        ) -> object:
            if descriptor in summary_temporary_descriptors:
                raise OSError("summary temporary write failed")
            return real_fdopen(descriptor, *args, **kwargs)

        monkeypatch.setattr(ledger_module.os, "open", observe_summary_temporary_open)
        monkeypatch.setattr(
            ledger_module, "_OPEN_SUPPORT_MARKER", observe_summary_temporary_open
        )
        monkeypatch.setattr(ledger_module.os, "fdopen", fail_summary_temporary_write)
    elif failure_stage == "replace":
        real_replace = ledger_module.os.replace

        def fail_summary_replace(
            source: str, destination: str, **kwargs: object
        ) -> None:
            if destination == "ledger.md":
                raise OSError("summary replace failed")
            real_replace(source, destination, **kwargs)

        monkeypatch.setattr(ledger_module.os, "replace", fail_summary_replace)
    else:
        real_fsync_directory = ledger_module._fsync_directory

        def fail_summary_directory_fsync(directory_fd: int) -> None:
            if os.fstat(directory_fd).st_ino == summary_parent_inode:
                raise OSError("summary directory fsync failed")
            real_fsync_directory(directory_fd)

        monkeypatch.setattr(
            ledger_module, "_fsync_directory", fail_summary_directory_fsync
        )

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    assert result.ok is True
    assert result.data["citation_rewrites"] == [
        {
            "source_id": record.source_id,
            "content_sha256": old_sha,
            "raw_path": f"_versions/{record.source_id}/{old_sha}/note.txt",
        }
    ]
    assert [warning.code for warning in result.warnings] == [
        "ledger_summary_refresh_failed"
    ]
    assert store.load(record.source_id).state is SourceState.PENDING
    assert not tuple((repo_root / "sources").glob(".brain-tmp-*"))


@pytest.mark.parametrize("swapped_edge", ["sources", "ledger"])
def test_adoption_handler_rejects_record_published_only_to_a_detached_anchor(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped_edge: str,
) -> None:
    record, item, _old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)
    shard = repo_root / "sources/ledger" / f"{record.source_id}.json"
    prior_payload = shard.read_bytes()
    prior_summary = (repo_root / "sources/ledger.md").read_bytes()
    replacement_summary = b"replacement summary must remain untouched\n"
    real_replace = ledger_module.os.replace
    swapped = False

    def replace_then_swap_anchor(
        source: str, destination: str, **kwargs: object
    ) -> None:
        nonlocal swapped
        real_replace(source, destination, **kwargs)
        if swapped or destination != shard.name:
            return
        swapped = True
        if swapped_edge == "sources":
            (repo_root / "sources").rename(repo_root / "detached-sources")
            (repo_root / "sources/ledger").mkdir(parents=True)
            shard.write_bytes(prior_payload)
            (repo_root / "sources/ledger.md").write_bytes(replacement_summary)
        else:
            (repo_root / "sources/ledger").rename(repo_root / "detached-ledger")
            (repo_root / "sources/ledger").mkdir()
            shard.write_bytes(prior_payload)

    monkeypatch.setattr(ledger_module.os, "replace", replace_then_swap_anchor)

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    current = LedgerStore(RepoPaths.discover(repo_root)).load(record.source_id)
    assert current.state is SourceState.INTEGRITY_ERROR
    assert result.ok is False
    assert result.data == {"source_id": record.source_id, "citation_rewrites": []}
    assert result.warnings == ()
    if swapped_edge == "sources":
        assert (repo_root / "sources/ledger.md").read_bytes() == replacement_summary
    else:
        assert (repo_root / "sources/ledger.md").read_bytes() == prior_summary


def test_adoption_handler_proves_canonical_record_after_context_close_failure(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)
    shard = repo_root / "sources/ledger" / f"{record.source_id}.json"
    ledger_inode = (repo_root / "sources/ledger").stat().st_ino
    real_close = ledger_module.os.close
    failed = False

    def close_then_report_failure(descriptor: int) -> None:
        nonlocal failed
        metadata = os.fstat(descriptor)
        should_fail = (
            not failed
            and metadata.st_ino == ledger_inode
            and shard.exists()
            and '"state":"pending"' in shard.read_text(encoding="utf-8")
        )
        real_close(descriptor)
        if should_fail:
            failed = True
            raise OSError("ledger descriptor close failed after publication")

    monkeypatch.setattr(ledger_module.os, "close", close_then_report_failure)

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    current = LedgerStore(RepoPaths.discover(repo_root)).load(record.source_id)
    assert current.state is SourceState.PENDING
    assert current.active_content_sha256 == item.sha256
    assert result.ok is True
    assert result.data["active_content_sha256"] == current.active_content_sha256
    assert result.data["citation_rewrites"] == [
        {
            "source_id": record.source_id,
            "content_sha256": old_sha,
            "raw_path": f"_versions/{record.source_id}/{old_sha}/note.txt",
        }
    ]
    assert [warning.code for warning in result.warnings] == [
        "ledger_record_recovery_required"
    ]


@pytest.mark.parametrize("extra_arguments", [[], ["--full"]])
def test_validate_runs_source_ledger_validation(
    repo_root: Path, extra_arguments: list[str]
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(
            ["--json", "validate", *extra_arguments],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
        )
        == 0
    )

    result = json.loads(stdout.getvalue())
    assert result["command"] == "validate"
    assert result["ok"] is True
    assert {
        check
        for report in result["data"]["reports"]
        for check in report["checks"]
    } == {
        "template-layout",
        "source-ledger",
        "wiki-transaction",
        "citations",
        "wiki-graph",
    }
    assert result["errors"] == []
    assert stderr.getvalue() == ""


def test_invalid_structure_returns_exit_one_through_the_existing_classifier(
    repo_root: Path,
) -> None:
    (repo_root / "wiki/index.md").unlink()
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(["--json", "validate"], cwd=repo_root, stdout=stdout, stderr=stderr) == 1
    )

    result = json.loads(stdout.getvalue())
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "wiki_index_missing"
    assert stderr.getvalue() == ""


def test_validate_outside_repository_returns_canonical_operational_failure(
    tmp_path: Path,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert main(["--json", "validate"], cwd=tmp_path, stdout=stdout, stderr=stderr) == 2

    assert json.loads(stdout.getvalue()) == {
        "command": "validate",
        "ok": False,
        "data": {"reports": []},
        "warnings": [],
        "errors": [
            {
                "code": "repository_not_found",
                "message": "second-brain repository root not found",
                "path": None,
                "details": {},
            }
        ],
    }
    assert stderr.getvalue() == ""


def test_validate_rejects_unexpected_positional_arguments(repo_root: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(
            ["--json", "validate", "unexpected"],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
        )
        == 2
    )

    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue())["errors"][0]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("argv", "command", "returncode"),
    [
        (["wiki", "apply"], "wiki apply", 2),
        (["wiki", "recover"], "wiki recover", 2),
        (["links", "candidates"], "links candidates", 2),
        (["links", "check"], "links check", 2),
    ],
)
def test_each_registered_task_five_route_returns_a_canonical_failure_when_unusable(
    tmp_path: Path, argv: list[str], command: str, returncode: int
) -> None:
    stdout = io.StringIO()

    assert main(["--json", *argv], cwd=tmp_path, stdout=stdout) == returncode

    payload = json.loads(stdout.getvalue())
    assert payload["command"] == command
    assert payload["errors"][0]["code"] != "command_not_available"


def test_human_result_data_stays_on_stdout_and_diagnostics_stay_on_stderr() -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    result = CommandResult(
        "validate",
        ok=False,
        data={"checks": ["template-layout"]},
        warnings=(Diagnostic("stale", "A cached fingerprint is stale."),),
        errors=(Diagnostic("validation_gap", "One source remains unresolved."),),
    )

    render_human(result, stdout=stdout, stderr=stderr)

    assert stdout.getvalue() == (
        'validate: failed\n{\n  "checks": [\n    "template-layout"\n  ]\n}\n'
    )
    assert stderr.getvalue() == (
        "stale: A cached fingerprint is stale.\n"
        "validation_gap: One source remains unresolved.\n"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_rendering_rejects_nonfinite_numbers(value: float) -> None:
    stream = io.StringIO()

    with pytest.raises(ValueError, match="Out of range float values"):
        render_json(CommandResult("doctor", ok=True, data={"value": value}), stream)

    assert stream.getvalue() == ""


def test_human_wiki_apply_requires_its_manifest_argument(
    tmp_path: Path,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert main(["wiki", "apply"], cwd=tmp_path, stdout=stdout, stderr=stderr) == 2

    assert stdout.getvalue() == ""
    assert "--manifest" in stderr.getvalue()


@pytest.mark.parametrize("command", ["init", "sync", "status"])
def test_implemented_top_level_commands_reject_trailing_arguments(
    repo_root: Path, command: str
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main([command, "unexpected"], cwd=repo_root, stdout=stdout, stderr=stderr) == 2
    )
    assert stdout.getvalue() == ""
    assert "unrecognized arguments: unexpected" in stderr.getvalue()


def test_adoption_parser_rejects_invalid_checksum_before_handler(
    repo_root: Path,
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(
            [
                "source",
                "adopt-version",
                "src_" + "a" * 64,
                "--candidate-sha256",
                "ABC",
                "--approval-note",
                "approved",
            ],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
        )
        == 2
    )
    assert stdout.getvalue() == ""
    assert "64 lower-case hexadecimal" in stderr.getvalue()


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_lock_release_failure_cannot_report_clean_init_success(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, release_mode: str
) -> None:
    class UnreleasableLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError("release failed")
            return False

    monkeypatch.setattr(
        "brainlib.commands.SourceWriteLock.acquire",
        lambda _path: UnreleasableLock(),
    )
    stdout = io.StringIO()

    assert main(["--json", "init"], cwd=repo_root, stdout=stdout) == 1
    payload = json.loads(stdout.getvalue())
    assert payload["ok"] is False
    assert payload["data"]["status"] == "complete"
    assert set(payload["data"]) == {
        "status",
        "handoff_manifest",
        "handoffs",
        *SYNC_REPORT_KEYS,
    }
    assert [error["code"] for error in payload["errors"]] == [
        "source_lock_cleanup_failed"
    ]


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_validation_lock_context_failure_is_an_operational_failure(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, release_mode: str
) -> None:
    class CleanupFailureLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError("cleanup\n\x1b[31mfailed")
            return False

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: CleanupFailureLock(),
    )
    stdout, stderr = io.StringIO(), io.StringIO()

    assert (
        main(
            ["--json", "validate"],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
        )
        == 2
    )
    payload = json.loads(stdout.getvalue())
    assert payload["data"] == {}
    assert [error["code"] for error in payload["errors"]] == [
        "source_operation_failed"
    ]
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_lock_cleanup_preserves_body_failure_and_sanitizes_separate_diagnostic(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    hostile = "cleanup\n\x1b[31m" + "x" * 1000

    class BadCleanupLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError(hostile)
            return False

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: BadCleanupLock(),
    )

    def fail_body(_extractor: object) -> str:
        raise ValueError("primary digest failure")

    stdout, stderr = io.StringIO(), io.StringIO()
    services = commands.CommandServices(UnavailableProcessor, fail_body)

    assert (
        main(
            ["--json", "init"],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
            services=services,
        )
        == 1
    )
    payload = json.loads(stdout.getvalue())
    assert [error["code"] for error in payload["errors"]] == [
        "source_operation_failed",
        "source_lock_cleanup_failed",
    ]
    assert payload["errors"][0]["message"] == "primary digest failure"
    cleanup = payload["errors"][1]["message"]
    assert "\n" not in cleanup
    assert "\x1b" not in cleanup
    assert len(cleanup) < 400
    assert stderr.getvalue() == ""


def test_keyboard_interrupt_cleanup_note_is_sanitized_and_bounded(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "cleanup\n\x1b[31m" + "x" * 1000

    class BadCleanupLock:
        def release(self) -> bool:
            raise RuntimeError(hostile)

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: BadCleanupLock(),
    )

    def interrupt(_extractor: object) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        main(
            ["init"],
            cwd=repo_root,
            services=commands.CommandServices(UnavailableProcessor, interrupt),
        )

    notes = getattr(raised.value, "__notes__", ())
    assert len(notes) == 1
    assert "\n" not in notes[0]
    assert "\x1b" not in notes[0]
    assert len(notes[0]) < 400


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_adoption_release_failure_preserves_committed_result_and_recovery_details(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(record)
    hostile = "release\n\x1b[31m" + "x" * 1000

    class BadCleanupLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError(hostile)
            return False

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: BadCleanupLock(),
    )

    result = adopt_source_version(
        repo_root,
        record.source_id,
        candidate_sha256=item.sha256 or "",
        approval_note="User approved",
        resolver=StaticResolver(b"old"),
        now=FIXED_NOW,
    )

    current = LedgerStore(RepoPaths.discover(repo_root)).load(record.source_id)
    assert result.ok is False
    assert result.data == {
        "source_id": record.source_id,
        "active_content_sha256": item.sha256,
        "citation_rewrites": [
            {
                "source_id": record.source_id,
                "content_sha256": old_sha,
                "raw_path": f"_versions/{record.source_id}/{old_sha}/note.txt",
            }
        ],
    }
    assert current.active_content_sha256 == item.sha256
    assert [error.code for error in result.errors] == [
        "source_lock_cleanup_recovery_required"
    ]
    assert "rerun status" in result.errors[0].message
    assert "\n" not in result.errors[0].message
    assert "\x1b" not in result.errors[0].message
    assert len(result.errors[0].message) < 500


def test_sync_report_codec_serializes_all_bounded_fields_json_safely() -> None:
    source_id = "src_" + "a" * 64
    checksum = "b" * 64
    derivation_id = "drv_" + "c" * 64
    item = InventoryItem(
        FileFingerprint(Path("note.txt"), 4, 7),  # type: ignore[arg-type]
        "text/plain",
        ".txt",
        checksum,
    )
    report = SyncReport(
        "d" * 64,
        {action: (1 if action is SyncAction.CREATE else 0) for action in SyncAction},
        (SyncDecision(source_id, SyncAction.CREATE, "note.txt: create", item),),
        (item.fingerprint.path,),
        (
            SourceRepresentation(
                source_id,
                checksum,
                derivation_id,
                item.fingerprint.path,
                Path("sources/extracted/note.txt/hash/output.md"),  # type: ignore[arg-type]
                "e" * 64,
                "ok",
                (Anchor("line", "1"),),
            ),
        ),
        (CitationRewrite(source_id, checksum, item.fingerprint.path),),
        (source_id,),
        (Diagnostic("pending", "Pending.", item.fingerprint.path),),
        1,
        1,
        1,
        1,
        1,
    )

    data = sync_report_data(report)

    assert set(data) == SYNC_REPORT_KEYS
    json.dumps(data, allow_nan=False)
    assert data["citation_rewrites"] == [
        {
            "source_id": source_id,
            "content_sha256": checksum,
            "raw_path": "note.txt",
        }
    ]


def test_adoption_holds_source_lock_across_resolver_and_mutation(
    repo_root: Path,
) -> None:
    from brainlib.locking import SourceWriteLock

    record, item, _old_sha = make_integrity_inputs(repo_root)
    paths = RepoPaths.discover(repo_root)
    LedgerStore(paths).save(record)
    lock = SourceWriteLock.acquire(paths.lock)
    try:
        result = adopt_source_version(
            repo_root,
            record.source_id,
            candidate_sha256=item.sha256 or "",
            approval_note="User approved",
            resolver=StaticResolver(b"old"),
            now=FIXED_NOW,
        )
    finally:
        assert lock.release()

    assert result.ok is False
    assert not (repo_root / "sources/raw/_versions" / record.source_id).exists()


def test_operational_failure_uses_exit_code_one_while_unavailable_uses_64() -> None:
    operational_failure = CommandResult(
        "validate",
        ok=False,
        data={},
        errors=(Diagnostic("validation_gap", "A source is unresolved."),),
    )
    unavailable_result = CommandResult(
        "validate",
        ok=False,
        data={},
        errors=(Diagnostic("command_not_available", "Not available."),),
    )

    assert cli.exit_code_for(operational_failure) == 1
    assert cli.exit_code_for(unavailable_result) == 64


def test_generic_command_execution_failure_exits_two(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()

    def fail_validation(*_args: object, **_kwargs: object) -> object:
        raise OSError("injected validation operation failure")

    monkeypatch.setattr(commands, "validate_repository", fail_validation)

    assert main(["--json", "validate"], cwd=repo_root, stdout=stdout, stderr=stderr) == 2

    payload = json.loads(stdout.getvalue())
    assert stderr.getvalue() == ""
    assert payload["errors"][0]["code"] == "source_operation_failed"


def test_missing_ripgrep_reports_static_recipes_without_executing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    stdout = io.StringIO()

    assert main(["--json", "doctor"], cwd=tmp_path, stdout=stdout) == 0

    result = json.loads(stdout.getvalue())
    assert result["data"]["ripgrep"] == {"available": False}
    assert result["data"]["install_recipes"] == {
        "debian_ubuntu_apt": ["sudo", "apt-get", "install", "ripgrep"],
        "macos_homebrew": ["brew", "install", "ripgrep"],
        "windows_winget": ["winget", "install", "BurntSushi.ripgrep.MSVC"],
    }


def test_available_ripgrep_and_missing_registry_need_no_install_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "brainlib.commands.shutil.which", lambda _command: "/usr/bin/rg"
    )

    result = doctor(tmp_path)

    assert result.data["ripgrep"] == {"available": True}
    assert result.data["install_recipes"] == {}
    assert [warning.code for warning in result.warnings] == [
        "extractor_registry_missing"
    ]


def test_doctor_recipe_results_are_isolated_between_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brainlib.commands.shutil.which", lambda _command: None)
    first = doctor(tmp_path)
    recipes = first.data["install_recipes"]
    assert isinstance(recipes, Mapping)
    homebrew = recipes["macos_homebrew"]

    mutated = False
    try:
        append = getattr(homebrew, "append")
        append("poison-later-results")
        mutated = True
    except (AttributeError, TypeError):
        pass

    try:
        second = doctor(tmp_path)
        assert second.data["install_recipes"] == {
            "macos_homebrew": ["brew", "install", "ripgrep"],
            "debian_ubuntu_apt": ["sudo", "apt-get", "install", "ripgrep"],
            "windows_winget": ["winget", "install", "BurntSushi.ripgrep.MSVC"],
        }
    finally:
        if mutated:
            getattr(homebrew, "pop")()
