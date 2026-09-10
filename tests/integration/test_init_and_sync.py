from __future__ import annotations

import io
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import brainlib.commands as commands
from brainlib.cli import main
from brainlib.contracts import (
    Anchor,
    Derivation,
    ProcessingAttempt,
    SourceState,
    compute_corpus_revision,
    derivation_id,
)
from brainlib.diagnostics import Diagnostic, ValidationReport
from brainlib.inventory import InventoryReport
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore, derive_extraction_path
from brainlib.registry import ExtractorRegistry, ExtractorSpec
from brainlib.sync import ProcessResult, ProcessingContext, UnavailableProcessor
from brainlib.sync_results import (
    PendingSyncResult,
    StagedSyncResult,
    SyncResultReference,
    SyncResultStore,
    SyncResultWriter,
)


SYNC_DATA_KEYS = {
    "status",
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
    "handoff_manifest",
    "handoffs",
    "coverage_gaps",
    "coverage_gap_count",
    "sample_limits",
    "result_manifest",
}


@dataclass(frozen=True)
class InProcessResult:
    returncode: int
    stdout: str
    stderr: str


def unavailable_services() -> object:
    service_type = getattr(commands, "CommandServices")
    return service_type(
        processor_factory=UnavailableProcessor,
        prerequisite_digest=lambda _extractor: "0" * 64,
    )


class SuccessfulTextProcessor:
    def process(
        self,
        record: object,
        item: object,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        raw_path = getattr(record, "current_raw_path")
        identifier = derivation_id(
            source_sha256=context.input_sha256,
            extractor_id=extractor.extractor_id,
            extractor_version=context.extractor_version,
            config_sha256=context.config_sha256,
        )
        relative = derive_extraction_path(raw_path, context.input_sha256, identifier)
        relative = relative.with_name(identifier + extractor.output_suffix)
        output_path = PurePosixPath("sources/extracted", relative)
        output = paths.root / output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'<a id="line:1"></a>\nprocessed\n')
        metadata = output.stat()
        derivation = Derivation(
            identifier,
            context.input_sha256,
            extractor.extractor_id,
            context.extractor_version,
            context.config_sha256,
            output_path,
            hashlib.sha256(output.read_bytes()).hexdigest(),
            metadata.st_size,
            metadata.st_mtime_ns,
            "ok",
            (Anchor("line", "1"),),
            context.attempted_at,
            method="deterministic",
            method_metadata={
                "converter_id": extractor.preferred.converter_id,
                "converter_version": (
                    f"builtin:{extractor.preferred.converter_id}:"
                    f"{extractor.extractor_version}"
                ),
            },
        )
        attempt = ProcessingAttempt(
            context.input_sha256,
            extractor.extractor_id,
            context.extractor_version,
            context.config_sha256,
            context.prerequisite_digest,
            SourceState.OK,
            context.attempted_at,
            (),
        )
        return ProcessResult(SourceState.OK, derivation, attempt, ())


def successful_services() -> object:
    service_type = getattr(commands, "CommandServices")

    def digest(extractor: ExtractorSpec) -> str:
        converter_version = (
            f"builtin:{extractor.preferred.converter_id}:{extractor.extractor_version}"
        )
        return hashlib.sha256(
            (
                f"converter-v1\0{extractor.preferred.converter_id}\0{converter_version}"
            ).encode()
        ).hexdigest()

    return service_type(SuccessfulTextProcessor, digest)


def run_brain(
    repo_root: Path,
    *argv: str,
    services: object | None = None,
    acknowledge: bool = True,
) -> InProcessResult:
    stdout, stderr = io.StringIO(), io.StringIO()
    options = {} if services is None else {"services": services}
    result = InProcessResult(
        main(
            list(argv),
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
            **options,
        ),
        stdout.getvalue(),
        stderr.getvalue(),
    )
    if acknowledge and any(argument in {"init", "sync"} for argument in argv):
        pending = SyncResultStore(RepoPaths.discover(repo_root)).load_pending()
        if pending is not None:
            if "--json" in argv:
                payload = json.loads(result.stdout)
                error_codes = {item["code"] for item in payload["errors"]}
                reference_data = payload["data"].get("result_manifest")
            else:
                error_codes = {
                    line.split(":", 1)[0]
                    for line in result.stderr.splitlines()
                    if ":" in line
                }
                reference_data = pending.reference.to_dict()
            if (
                error_codes <= {"source_coverage_gaps"}
                and reference_data == pending.reference.to_dict()
            ):
                list(
                    SyncResultStore(RepoPaths.discover(repo_root)).iter_events(
                        pending.reference
                    )
                )
                assert commands.consume_sync_result_id(
                    repo_root, pending.reference.result_id
                ).ok
                assert commands.acknowledge_sync_result_id(
                    repo_root, pending.reference.result_id
                ).ok
    return result


def write_url_descriptor(path: Path, *, url: str) -> None:
    path.write_text(
        f"---\nkind: url\nurl: {url}\ndescription: Example\nadded: 2026-09-04\n---\n",
        encoding="utf-8",
    )


def test_init_records_all_inputs_and_reports_exact_complete_with_gaps_envelope(
    repo_root: Path,
) -> None:
    source = repo_root / "sources/raw/notes/a.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"hello")

    result = run_brain(
        repo_root,
        "--json",
        "init",
        services=unavailable_services(),
    )

    assert result.returncode == 1
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"]["status"] == "complete_with_gaps"
    assert set(payload["data"]) == SYNC_DATA_KEYS
    assert payload["data"]["handoff_source_ids"] == []
    assert payload["data"]["hashed_paths"] == ["notes/a.txt"]
    records = LedgerStore(RepoPaths.discover(repo_root)).load_all()
    assert len(records) == 1
    assert next(iter(records.values())).state is SourceState.PENDING


def test_init_treats_inventory_skips_as_coverage_gaps(repo_root: Path) -> None:
    outside = repo_root.parent / "outside.txt"
    outside.write_bytes(b"outside")
    (repo_root / "sources/raw/unsafe.txt").symlink_to(outside)

    result = run_brain(
        repo_root,
        "--json",
        "init",
        services=unavailable_services(),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert "unsafe_source_symlink" in result.stdout


def test_sync_is_idempotent_and_reports_no_hashed_paths_for_unchanged_tree(
    repo_root: Path,
) -> None:
    source = repo_root / "sources/raw/notes/a.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"hello")
    services = unavailable_services()
    run_brain(repo_root, "init", services=services)

    result = run_brain(repo_root, "--json", "sync", services=services)

    assert result.returncode == 1
    assert json.loads(result.stdout)["data"]["hashed_paths"] == []


def test_init_prevalidates_complete_digest_map_before_mutating_ledger(
    repo_root: Path,
) -> None:
    source = repo_root / "sources/raw/a.txt"
    source.write_bytes(b"hello")
    calls: Counter[str] = Counter()
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")

    def invalid_last_digest(extractor: object) -> str:
        extractor_id = getattr(extractor, "extractor_id")
        calls[extractor_id] += 1
        if extractor_id == registry.extractors[-1].extractor_id:
            return "invalid"
        return "0" * 64

    processor_factories = 0

    def processor_factory() -> UnavailableProcessor:
        nonlocal processor_factories
        processor_factories += 1
        return UnavailableProcessor()

    service_type = getattr(commands, "CommandServices")
    result = run_brain(
        repo_root,
        "--json",
        "init",
        services=service_type(processor_factory, invalid_last_digest),
    )

    assert result.returncode == 1
    assert calls == Counter(extractor.extractor_id for extractor in registry.extractors)
    assert processor_factories == 0
    assert LedgerStore(RepoPaths.discover(repo_root)).load_all() == {}
    assert (
        repo_root / "sources/ledger.md"
    ).read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"


def test_init_constructs_one_processor_and_uses_one_operation_timestamp(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    (repo_root / "sources/raw/b.txt").write_bytes(b"b")
    processor_factories = 0

    def processor_factory() -> UnavailableProcessor:
        nonlocal processor_factories
        processor_factories += 1
        return UnavailableProcessor()

    service_type = getattr(commands, "CommandServices")
    result = run_brain(
        repo_root,
        "--json",
        "init",
        services=service_type(processor_factory, lambda _extractor: "0" * 64),
    )

    assert result.returncode == 1
    assert processor_factories == 1
    records = LedgerStore(RepoPaths.discover(repo_root)).load_all().values()
    assert len({record.created_at for record in records}) == 1
    assert len({record.inspected_at for record in records}) == 1
    assert len({record.updated_at for record in records}) == 1


def test_sync_checkpoint_failure_keeps_prior_shard_and_publishes_no_summary(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    (repo_root / "sources/raw/b.txt").write_bytes(b"b")
    real_save = LedgerStore.save
    calls = 0

    def fail_second_save(self: LedgerStore, record: object) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("checkpoint failed")
        return real_save(self, record)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerStore, "save", fail_second_save)

    result = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
    )

    assert result.returncode == 1
    assert len(tuple((repo_root / "sources/ledger").glob("src_*.json"))) == 1
    assert (
        repo_root / "sources/ledger.md"
    ).read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"


def test_status_distinguishes_pristine_from_initialized_empty(repo_root: Path) -> None:
    pristine = run_brain(repo_root, "--json", "status")

    assert pristine.returncode == 0
    pristine_data = json.loads(pristine.stdout)["data"]
    assert pristine_data["status"] == "not_initialized"
    assert pristine_data["corpus_revision"] is None
    assert pristine_data["state_counts"] == {state.value: 0 for state in SourceState}

    initialized = run_brain(
        repo_root,
        "--json",
        "init",
        services=unavailable_services(),
    )

    assert initialized.returncode == 0
    status = run_brain(repo_root, "--json", "status")
    assert status.returncode == 0
    data = json.loads(status.stdout)["data"]
    assert data["status"] == "complete"
    assert data["corpus_revision"] == compute_corpus_revision([])
    assert set(data) >= {"state_counts", "warnings", "failures", "needs_agent"}


def test_status_never_constructs_processor_or_runs_prerequisite_probe(
    repo_root: Path,
) -> None:
    service_type = getattr(commands, "CommandServices")

    def forbidden() -> object:
        raise AssertionError("status constructed a processor")

    def forbidden_probe(_extractor: object) -> str:
        raise AssertionError("status ran a prerequisite probe")

    result = run_brain(
        repo_root,
        "--json",
        "status",
        services=service_type(forbidden, forbidden_probe),
    )

    assert result.returncode == 0


def test_status_reports_inventory_skips_as_bounded_coverage_gaps(
    repo_root: Path,
) -> None:
    outside = repo_root.parent / "outside.txt"
    outside.write_bytes(b"outside")
    (repo_root / "sources/raw/unsafe.txt").symlink_to(outside)

    result = run_brain(repo_root, "--json", "status")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["state_counts"] == {state.value: 0 for state in SourceState}
    assert len(payload["data"]["warnings"]) <= payload["data"]["detail_limit"]
    assert "unsafe_source_symlink" in result.stdout


def test_status_is_deterministic_and_bounded_for_five_thousand_records(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [
        SimpleNamespace(
            source_id=f"src_{number:064x}",
            state=SourceState.PENDING,
            diagnostics=(Diagnostic("pending", f"Pending {number}."),),
        )
        for number in range(1, 5_001)
    ]
    calls = 0

    def load_reversed_then_forward(_store: LedgerStore) -> dict[str, object]:
        nonlocal calls
        calls += 1
        ordered = reversed(records) if calls == 1 else records
        return {record.source_id: record for record in ordered}

    monkeypatch.setattr(LedgerStore, "load_all", load_reversed_then_forward)
    monkeypatch.setattr(LedgerStore, "read_summary", lambda _store: b"generated")
    monkeypatch.setattr(
        commands,
        "inventory_raw_sources",
        lambda *_args: InventoryReport((), ()),
    )
    monkeypatch.setattr(commands.ExtractorRegistry, "load", lambda _path: object())
    monkeypatch.setattr(
        commands,
        "validate_source_ledger",
        lambda *_args, **_kwargs: ValidationReport(("source-ledger",), (), "f" * 64),
    )
    monkeypatch.setattr(commands, "compute_corpus_revision", lambda _records: "f" * 64)

    first = run_brain(repo_root, "--json", "status")
    second = run_brain(repo_root, "--json", "status")

    first_data = json.loads(first.stdout)["data"]
    second_data = json.loads(second.stdout)["data"]
    assert first.returncode == second.returncode == 1
    assert first_data == second_data
    assert first_data["state_counts"]["pending"] == 5_000
    assert len(first_data["warnings"]) == first_data["detail_limit"] == 100


def test_status_json_is_independent_of_python_hash_seed(repo_root: Path) -> None:
    outputs: list[str] = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "brain", "--json", "status"],
            cwd=repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stderr == ""
        outputs.append(result.stdout)

    assert outputs[0] == outputs[1]


def test_url_descriptor_sync_does_not_perform_network_or_processor_work(
    repo_root: Path,
) -> None:
    descriptor = repo_root / "sources/raw/reference.url.md"
    descriptor.write_text(
        "---\n"
        "kind: url\n"
        "url: https://example.test/page\n"
        "description: Example\n"
        "added: 2026-09-04\n"
        "---\n",
        encoding="utf-8",
    )
    service_type = getattr(commands, "CommandServices")

    class ForbiddenProcessor:
        def process(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("URL descriptor reached processor work")

    result = run_brain(
        repo_root,
        "--json",
        "sync",
        services=service_type(ForbiddenProcessor, lambda _extractor: "0" * 64),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["handoff_source_ids"] == []
    assert "web_capture_awaiting_approval" in result.stdout


def test_zero_version_url_edit_remains_a_status_coverage_gap(
    repo_root: Path,
) -> None:
    descriptor = repo_root / "sources/raw/reference.url.md"
    write_url_descriptor(descriptor, url="https://example.test/alpha")
    initialized = run_brain(
        repo_root, "--json", "init", services=unavailable_services()
    )
    assert initialized.returncode == 1
    store = LedgerStore(RepoPaths.discover(repo_root))
    original = next(iter(store.load_all().values()))
    assert original.versions == {}

    write_url_descriptor(descriptor, url="https://example.test/bravo")
    synchronized = run_brain(
        repo_root, "--json", "sync", services=unavailable_services()
    )

    assert synchronized.returncode == 1
    updated = store.load(original.source_id)
    assert updated.source_id == original.source_id
    assert updated.url_descriptor is not None
    assert updated.url_descriptor.url == "https://example.test/bravo"
    assert updated.versions == {}
    validated = run_brain(repo_root, "--json", "validate", "--full")
    assert validated.returncode == 0
    assert json.loads(validated.stdout)["ok"] is True
    current = run_brain(repo_root, "--json", "status")
    assert current.returncode == 1
    payload = json.loads(current.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["state_counts"]["awaiting_approval"] == 1
    assert payload["errors"][0]["code"] == "source_coverage_gaps"
    assert "web_capture_awaiting_approval" in current.stdout


def test_case_normalized_url_descriptor_creation_and_rename_remain_valid(
    repo_root: Path,
) -> None:
    initial_path = repo_root / "sources/raw/REFERENCE.URL.MD"
    write_url_descriptor(initial_path, url="https://example.test/page")

    initialized = run_brain(
        repo_root, "--json", "init", services=unavailable_services()
    )

    assert initialized.returncode == 1
    store = LedgerStore(RepoPaths.discover(repo_root))
    original = next(iter(store.load_all().values()))
    assert original.current_raw_path == PurePosixPath("REFERENCE.URL.MD")
    initial_validation = run_brain(repo_root, "--json", "validate", "--full")
    assert initial_validation.returncode == 0

    renamed_path = repo_root / "sources/raw/reference.Url.Md"
    initial_path.rename(renamed_path)
    synchronized = run_brain(
        repo_root, "--json", "sync", services=unavailable_services()
    )

    assert synchronized.returncode == 1
    renamed = store.load(original.source_id)
    assert renamed.current_raw_path == PurePosixPath("reference.Url.Md")
    assert renamed.previous_raw_paths == (PurePosixPath("REFERENCE.URL.MD"),)
    renamed_validation = run_brain(repo_root, "--json", "validate", "--full")
    assert renamed_validation.returncode == 0
    current = run_brain(repo_root, "--json", "status")
    assert current.returncode == 1
    payload = json.loads(current.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["state_counts"]["awaiting_approval"] == 1
    assert payload["errors"][0]["code"] == "source_coverage_gaps"


def test_init_lock_contention_is_one_canonical_operational_failure(
    repo_root: Path,
) -> None:
    from brainlib.locking import SourceWriteLock

    paths = RepoPaths.discover(repo_root)
    lock = SourceWriteLock.acquire(paths.lock)
    try:
        result = run_brain(
            repo_root,
            "--json",
            "init",
            services=unavailable_services(),
        )
    finally:
        assert lock.release()

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["code"] == "source_operation_failed"
    assert payload["data"] == {}


@pytest.mark.parametrize("command", ("init", "sync"))
@pytest.mark.parametrize("has_gap", (False, True))
@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_completed_sync_report_survives_lock_cleanup_failure(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    has_gap: bool,
    release_mode: str,
) -> None:
    if has_gap:
        (repo_root / "sources/raw/a.txt").write_bytes(b"a")

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

    result = run_brain(
        repo_root,
        "--json",
        command,
        services=unavailable_services(),
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == (
        "complete_with_gaps" if has_gap else "complete"
    )
    assert set(payload["data"]) == SYNC_DATA_KEYS
    assert [error["code"] for error in payload["errors"]] == (
        ["source_coverage_gaps", "source_lock_cleanup_failed"]
        if has_gap
        else ["source_lock_cleanup_failed"]
    )
    cleanup_message = payload["errors"][-1]["message"]
    assert "\n" not in cleanup_message
    assert "\x1b" not in cleanup_message


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_committed_rename_rewrite_survives_sync_lock_cleanup_failure(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    original = repo_root / "sources/raw/notes/a.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"a")
    initialized = run_brain(repo_root, "--json", "init", services=successful_services())
    assert initialized.returncode == 0
    initial_record = next(
        iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values())
    )
    renamed = repo_root / "sources/raw/notes/renamed.txt"
    original.rename(renamed)

    class CleanupFailureLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError("cleanup failed")
            return False

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: CleanupFailureLock(),
    )

    result = run_brain(repo_root, "--json", "sync", services=successful_services())

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["citation_rewrites"] == [
        {
            "source_id": initial_record.source_id,
            "content_sha256": initial_record.active_content_sha256,
            "raw_path": "notes/renamed.txt",
        }
    ]
    assert payload["data"]["status"] == "complete"
    assert [error["code"] for error in payload["errors"]] == [
        "source_lock_cleanup_failed"
    ]
    persisted = LedgerStore(RepoPaths.discover(repo_root)).load(
        initial_record.source_id
    )
    assert persisted.current_raw_path == PurePosixPath("notes/renamed.txt")

    result_reference = payload["data"]["result_manifest"]
    monkeypatch.undo()
    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    replayed_payload = json.loads(replayed.stdout)

    assert replayed.returncode == 0
    assert replayed_payload["data"]["result_manifest"] == result_reference
    assert (
        replayed_payload["data"]["citation_rewrites"]
        == payload["data"]["citation_rewrites"]
    )


def test_validate_contends_while_status_remains_lock_free(repo_root: Path) -> None:
    from brainlib.locking import SourceWriteLock

    paths = RepoPaths.discover(repo_root)
    lock = SourceWriteLock.acquire(paths.lock)
    try:
        validation = run_brain(repo_root, "--json", "validate")
        status = run_brain(repo_root, "--json", "status")
    finally:
        assert lock.release()

    assert validation.returncode == 2
    validation_payload = json.loads(validation.stdout)
    assert validation_payload["data"] == {}
    assert [item["code"] for item in validation_payload["errors"]] == [
        "source_operation_failed"
    ]
    assert status.returncode == 0
    assert json.loads(status.stdout)["data"]["status"] == "not_initialized"


def test_processor_failure_retains_extracting_checkpoint_and_no_summary(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    class FailingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("processor failed")

    service_type = getattr(commands, "CommandServices")
    result = run_brain(
        repo_root,
        "--json",
        "sync",
        services=service_type(FailingProcessor, lambda _extractor: "0" * 64),
    )

    assert result.returncode == 1
    record = next(iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values()))
    assert record.state is SourceState.EXTRACTING
    assert (
        repo_root / "sources/ledger.md"
    ).read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"


def test_keyboard_interrupt_releases_lock_but_keeps_durable_checkpoint(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    class InterruptingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

    service_type = getattr(commands, "CommandServices")
    with pytest.raises(KeyboardInterrupt):
        run_brain(
            repo_root,
            "--json",
            "sync",
            services=service_type(InterruptingProcessor, lambda _extractor: "0" * 64),
        )

    paths = RepoPaths.discover(repo_root)
    record = next(iter(LedgerStore(paths).load_all().values()))
    assert record.state is SourceState.EXTRACTING
    assert not paths.lock.exists()


def test_successful_retry_after_interrupt_has_no_status_or_summary_gap(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    class InterruptingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

    service_type = getattr(commands, "CommandServices")
    interrupted_services = service_type(
        InterruptingProcessor,
        lambda extractor: successful_services().prerequisite_digest(extractor),
    )
    with pytest.raises(KeyboardInterrupt):
        run_brain(repo_root, "sync", services=interrupted_services)

    synchronized = run_brain(
        repo_root, "--json", "sync", services=successful_services()
    )
    assert synchronized.returncode == 0
    record = next(iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values()))
    assert record.state is SourceState.OK
    assert [diagnostic.code for diagnostic in record.diagnostics] == [
        "stale_extracting_recovered"
    ]
    summary = (repo_root / "sources/ledger.md").read_text(encoding="utf-8")
    assert "stale_extracting_recovered" not in summary
    assert "## Coverage gaps\n\n- None." in summary

    current = run_brain(repo_root, "--json", "status")

    assert current.returncode == 0
    data = json.loads(current.stdout)["data"]
    assert data["status"] == "complete"
    assert data["warnings"] == []


def test_raw_missing_warning_with_retained_ok_representation_is_a_visible_gap(
    repo_root: Path,
) -> None:
    raw = repo_root / "sources/raw/a.txt"
    raw.write_bytes(b"a")
    initialized = run_brain(repo_root, "--json", "init", services=successful_services())
    assert initialized.returncode == 0
    raw.unlink()

    synchronized = run_brain(
        repo_root, "--json", "sync", services=successful_services()
    )

    assert synchronized.returncode == 1
    record = next(iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values()))
    assert record.state is SourceState.WARNING
    assert record.active_derivation_id is not None
    assert record.derivations[record.active_derivation_id].quality_state == "ok"
    assert [diagnostic.code for diagnostic in record.diagnostics] == ["raw_missing"]

    current = run_brain(repo_root, "--json", "status")

    assert current.returncode == 1
    payload = json.loads(current.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["state_counts"]["warning"] == 1
    assert "raw_missing" in current.stdout
    assert "active_quality_state_mismatch" not in current.stdout


def test_summary_failure_reports_recovery_failure_after_durable_shards(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    def fail_summary(*_args: object, **_kwargs: object) -> str:
        raise OSError("summary publish failed")

    monkeypatch.setattr(LedgerStore, "write_summary", fail_summary)
    result = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
    )

    assert result.returncode == 1
    assert "summary publish failed" in result.stdout
    assert "rerun sync to recover" in result.stdout
    assert LedgerStore(RepoPaths.discover(repo_root)).load_all()
    assert (
        repo_root / "sources/ledger.md"
    ).read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"


def test_summary_failure_preserves_and_replays_exact_bounded_sync_result(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_summary = LedgerStore.write_summary
    real_reconcile = commands.reconcile_inventory
    calls = Counter[str]()

    def counted_reconcile(*args: object, **kwargs: object) -> object:
        calls["reconcile"] += 1
        return real_reconcile(*args, **kwargs)

    def fail_once(store: LedgerStore, *args: object, **kwargs: object) -> str:
        calls["summary"] += 1
        if calls["summary"] == 1:
            raise OSError("summary publish failed")
        return real_summary(store, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(commands, "reconcile_inventory", counted_reconcile)
    monkeypatch.setattr(LedgerStore, "write_summary", fail_once)

    failed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
    )
    failed_payload = json.loads(failed.stdout)

    assert failed.returncode == 1
    assert failed_payload["data"]["hashed_path_count"] == 1
    assert len(json.dumps(failed_payload)) < 128 * 1024
    reference_data = failed_payload["data"]["result_manifest"]
    from brainlib.sync_results import SyncResultReference, SyncResultStore

    reference = SyncResultReference.from_dict(reference_data)
    result_store = SyncResultStore(RepoPaths.discover(repo_root))
    events = list(result_store.iter_events(reference))
    assert [event.kind for event in events].count("hashed_path") == 1
    assert [event.kind for event in events].count("coverage_gap") == 1
    assert result_store.load_pending() is not None

    recovered = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
    )
    recovered_payload = json.loads(recovered.stdout)

    assert calls == Counter({"summary": 2, "reconcile": 1})
    assert recovered_payload["data"]["result_manifest"] == reference_data
    assert result_store.load_pending() is None


def test_summary_failure_after_rename_replays_the_exact_rewrite_manifest(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = repo_root / "sources/raw/notes/a.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"a")
    initialized = run_brain(repo_root, "--json", "init", services=successful_services())
    assert initialized.returncode == 0
    original.rename(repo_root / "sources/raw/notes/renamed.txt")

    real_summary = LedgerStore.write_summary
    real_reconcile = commands.reconcile_inventory
    calls = Counter[str]()

    def counted_reconcile(*args: object, **kwargs: object) -> object:
        calls["reconcile"] += 1
        return real_reconcile(*args, **kwargs)

    def fail_once(store: LedgerStore, *args: object, **kwargs: object) -> str:
        calls["summary"] += 1
        if calls["summary"] == 1:
            raise OSError("summary publish failed")
        return real_summary(store, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(commands, "reconcile_inventory", counted_reconcile)
    monkeypatch.setattr(LedgerStore, "write_summary", fail_once)

    failed = run_brain(repo_root, "--json", "sync", services=successful_services())
    failed_payload = json.loads(failed.stdout)
    reference_data = failed_payload["data"]["result_manifest"]

    from brainlib.sync_results import SyncResultReference, SyncResultStore

    reference = SyncResultReference.from_dict(reference_data)
    result_store = SyncResultStore(RepoPaths.discover(repo_root))
    rewrites = [
        event.data
        for event in result_store.iter_events(reference)
        if event.kind == "citation_rewrite"
    ]

    assert failed.returncode == 1
    assert failed_payload["data"]["citation_rewrite_count"] == 1
    assert len(rewrites) == 1
    assert result_store.load_pending() is not None

    recovered = run_brain(repo_root, "--json", "sync", services=successful_services())
    recovered_payload = json.loads(recovered.stdout)

    assert calls == Counter({"summary": 2, "reconcile": 1})
    assert recovered.returncode == 0
    assert recovered_payload["data"]["result_manifest"] == reference_data
    assert recovered_payload["data"]["citation_rewrite_count"] == 1
    assert result_store.load_pending() is None


def test_pending_result_is_recovered_only_by_its_bound_command(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    def fail_summary(*_args: object, **_kwargs: object) -> str:
        raise OSError("summary publish failed")

    monkeypatch.setattr(LedgerStore, "write_summary", fail_summary)
    failed = run_brain(repo_root, "--json", "init", services=successful_services())
    reference_data = json.loads(failed.stdout)["data"]["result_manifest"]
    monkeypatch.undo()

    wrong_command = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    result_store = SyncResultStore(RepoPaths.discover(repo_root))

    assert wrong_command.returncode == 1
    assert "must be recovered by rerunning init" in wrong_command.stdout
    assert result_store.load_pending() is not None

    recovered = run_brain(
        repo_root,
        "--json",
        "init",
        services=successful_services(),
    )

    assert recovered.returncode == 0
    assert json.loads(recovered.stdout)["data"]["result_manifest"] == reference_data
    assert result_store.load_pending() is None


def test_failed_cli_output_keeps_exact_sync_result_pending_for_replay(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    class FailingOutput(io.StringIO):
        def write(self, _value: str) -> int:
            raise OSError("stdout failed")

    with pytest.raises(OSError, match="stdout failed"):
        main(
            ["--json", "sync"],
            cwd=repo_root,
            stdout=FailingOutput(),
            stderr=io.StringIO(),
            services=successful_services(),
        )

    result_store = SyncResultStore(RepoPaths.discover(repo_root))
    pending = result_store.load_pending()
    assert pending is not None
    first_events = list(result_store.iter_events(pending.reference))
    assert [event.kind for event in first_events].count(
        "new_active_representation"
    ) == 1

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
        acknowledge=False,
    )
    replayed_payload = json.loads(replayed.stdout)

    assert replayed.returncode == 0
    assert replayed_payload["data"]["result_manifest"] == pending.reference.to_dict()
    assert replayed_payload["data"]["new_active_representation_count"] == 1
    assert list(result_store.iter_events(pending.reference)) == first_events
    assert commands.consume_sync_result_id(repo_root, pending.reference.result_id).ok
    assert commands.acknowledge_sync_result_id(
        repo_root, pending.reference.result_id
    ).ok
    assert result_store.load_pending() is None


def test_direct_sync_result_requires_explicit_idempotent_consumer_acknowledgement(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    result = commands.sync_sources(repo_root, services=successful_services())
    result_store = SyncResultStore(RepoPaths.discover(repo_root))
    pending = result_store.load_pending()

    assert pending is not None
    assert result.data["result_manifest"] == pending.reference.to_dict()
    assert commands.consume_sync_result_id(repo_root, pending.reference.result_id).ok
    commands.acknowledge_sync_result(repo_root, result)
    commands.acknowledge_sync_result(repo_root, result)
    assert result_store.load_pending() is None


def test_unacknowledged_cli_result_replays_until_explicit_idempotent_ack(
    repo_root: Path,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    def invoke(*argv: str) -> InProcessResult:
        stdout, stderr = io.StringIO(), io.StringIO()
        returncode = main(
            list(argv),
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
            services=successful_services(),
        )
        return InProcessResult(returncode, stdout.getvalue(), stderr.getvalue())

    first = invoke("--json", "sync")
    first_payload = json.loads(first.stdout)
    reference = first_payload["data"]["result_manifest"]
    result_id = reference["result_id"]
    replayed = invoke("--json", "sync")

    assert replayed.returncode == 0
    assert json.loads(replayed.stdout)["data"]["result_manifest"] == reference
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is not None

    wrong_consumption = invoke(
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        "sync_" + "0" * 64,
    )
    assert wrong_consumption.returncode == 1
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is not None

    early_acknowledgement = invoke(
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )
    assert early_acknowledgement.returncode == 1
    assert "consumed before acknowledgement" in early_acknowledgement.stdout
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is not None

    consumed = invoke(
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        result_id,
    )
    consumed_payload = json.loads(consumed.stdout)
    assert consumed.returncode == 0
    assert consumed_payload["ok"] is True
    assert consumed_payload["data"] == {
        **{
            "result_id": result_id,
            "status": "consumed",
            "manifest_path": reference["path"],
            "corpus_revision": reference["corpus_revision"],
            "event_counts": reference["event_counts"],
        },
        "effect_digest": consumed_payload["data"]["effect_digest"],
        "handoff_delivery": None,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", consumed_payload["data"]["effect_digest"])

    repeated_consumption = invoke(
        "--json",
        "source",
        "consume-sync-result",
        "--result-id",
        result_id,
    )
    assert repeated_consumption.returncode == 0
    repeated_payload = json.loads(repeated_consumption.stdout)
    assert repeated_payload["data"] == {
        **consumed_payload["data"],
        "status": "already_consumed",
    }

    wrong = invoke(
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        "sync_" + "0" * 64,
    )
    assert wrong.returncode == 1
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is not None

    malformed = invoke(
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        "not-a-result",
    )
    assert malformed.returncode == 2
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is not None

    acknowledged = invoke(
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )
    acknowledged_again = invoke(
        "--json",
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )

    assert acknowledged.returncode == 0
    assert acknowledged_again.returncode == 0
    assert SyncResultStore(RepoPaths.discover(repo_root)).load_pending() is None

    next_result = invoke("--json", "sync")
    next_payload = json.loads(next_result.stdout)
    assert next_payload["data"]["result_manifest"]["result_id"] != result_id
    assert next_payload["data"]["new_active_representation_count"] == 0


@pytest.mark.parametrize(
    ("failed_kind", "expected_kinds"),
    (
        (
            "new_active_representation",
            {"hashed_path", "new_active_representation"},
        ),
        (
            "coverage_gap",
            {"hashed_path", "coverage_gap"},
        ),
    ),
)
def test_event_write_failure_retries_one_exact_new_active_result(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_kind: str,
    expected_kinds: set[str],
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_emit = SyncResultWriter.emit
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        kind: str,
        data: object,
    ) -> None:
        nonlocal failed
        if kind == failed_kind and not failed:
            failed = True
            raise OSError(f"injected {kind} event failure")
        real_emit(writer, kind, data)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultWriter, "emit", fail_once)
    services = (
        unavailable_services()
        if failed_kind == "coverage_gap"
        else successful_services()
    )
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=services,
    )
    assert interrupted.returncode == 1

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=services,
    )
    payload = json.loads(replayed.stdout)
    reference = payload["data"]["result_manifest"]
    store = SyncResultStore(RepoPaths.discover(repo_root))
    events = list(store.iter_events(SyncResultReference.from_dict(reference)))

    assert replayed.returncode == (1 if "coverage_gap" in expected_kinds else 0)
    assert {event.kind for event in events} == expected_kinds
    assert [event.kind for event in events].count("new_active_representation") == (
        1 if "new_active_representation" in expected_kinds else 0
    )


@pytest.mark.parametrize("failure_phase", ("before", "after"))
def test_new_active_commit_failure_recovers_one_exact_event(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_commit = SyncResultWriter.commit
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        kind: str,
        data: object,
    ) -> None:
        nonlocal failed
        if kind != "new_active_representation" or failed:
            real_commit(writer, kind, data)  # type: ignore[arg-type]
            return
        failed = True
        if failure_phase == "after":
            real_commit(writer, kind, data)  # type: ignore[arg-type]
        raise OSError("injected representation commit failure")

    monkeypatch.setattr(SyncResultWriter, "commit", fail_once)
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    assert interrupted.returncode == 1

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    events = list(SyncResultStore(RepoPaths.discover(repo_root)).iter_events(reference))

    assert replayed.returncode == 0
    assert [event.kind for event in events].count("new_active_representation") == 1
    assert (
        next(iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values())).state
        is SourceState.OK
    )


@pytest.mark.parametrize("failure_phase", ("before", "after"))
def test_rename_commit_failure_recovers_one_exact_rewrite(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    original = repo_root / "sources/raw/notes/a.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"a")
    assert (
        run_brain(
            repo_root,
            "--json",
            "init",
            services=successful_services(),
        ).returncode
        == 0
    )
    original.rename(repo_root / "sources/raw/notes/renamed.txt")
    real_commit = SyncResultWriter.commit
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        kind: str,
        data: object,
    ) -> None:
        nonlocal failed
        if kind != "citation_rewrite" or failed:
            real_commit(writer, kind, data)  # type: ignore[arg-type]
            return
        failed = True
        if failure_phase == "after":
            real_commit(writer, kind, data)  # type: ignore[arg-type]
        raise OSError("injected rewrite commit failure")

    monkeypatch.setattr(SyncResultWriter, "commit", fail_once)
    assert (
        run_brain(
            repo_root,
            "--json",
            "sync",
            services=successful_services(),
        ).returncode
        == 1
    )

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    reference = SyncResultReference.from_dict(
        json.loads(replayed.stdout)["data"]["result_manifest"]
    )
    events = list(SyncResultStore(RepoPaths.discover(repo_root)).iter_events(reference))

    assert replayed.returncode == 0
    assert [event.kind for event in events].count("citation_rewrite") == 1


def test_duplicate_coverage_event_write_failure_recovers_both_occurrences(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = Diagnostic("inventory_scan_error", "inventory could not be read")
    monkeypatch.setattr(
        commands,
        "inventory_raw_sources",
        lambda *_args: InventoryReport((), (duplicate, duplicate)),
    )
    real_emit = SyncResultWriter.emit
    coverage_calls = 0

    def fail_second_coverage(
        writer: SyncResultWriter,
        kind: str,
        data: object,
    ) -> None:
        nonlocal coverage_calls
        if kind == "coverage_gap":
            coverage_calls += 1
            if coverage_calls == 2:
                raise OSError("injected second coverage event failure")
        real_emit(writer, kind, data)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultWriter, "emit", fail_second_coverage)
    assert run_brain(repo_root, "--json", "sync").returncode == 1

    replayed = run_brain(repo_root, "--json", "sync")
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    events = list(SyncResultStore(RepoPaths.discover(repo_root)).iter_events(reference))
    coverage_events = [event for event in events if event.kind == "coverage_gap"]

    assert replayed.returncode == 1
    assert payload["data"]["coverage_gap_count"] == 2
    assert len(coverage_events) == 2
    assert coverage_events[0].data == coverage_events[1].data


def test_pre_stage_retry_does_not_duplicate_unchanged_record_coverage_gap(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    (repo_root / "sources/raw/b.txt").write_bytes(b"a")
    real_save_staged = SyncResultStore.save_staged
    failed = False

    def fail_before_staging(store: SyncResultStore, staged: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected pre-stage failure")
        real_save_staged(store, staged)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultStore, "save_staged", fail_before_staging)
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
        acknowledge=False,
    )
    assert interrupted.returncode == 1

    paths = RepoPaths.discover(repo_root)
    ledger = LedgerStore(paths)
    records = ledger.load_all()
    earlier_source_id = min(records)
    earlier = records[earlier_source_id]
    changed_diagnostic = Diagnostic(
        "earlier_source_changed",
        "Only the earlier source changed before recovery.",
    )
    ledger.save(
        replace(
            earlier,
            state=SourceState.WARNING,
            diagnostics=(changed_diagnostic,),
        )
    )

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=unavailable_services(),
        acknowledge=False,
    )
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    gaps = [
        event.data
        for event in SyncResultStore(paths).iter_events(reference)
        if event.kind == "coverage_gap"
    ]

    assert replayed.returncode == 1
    assert payload["data"]["coverage_gap_count"] == 2
    assert [gap["code"] for gap in gaps].count("earlier_source_changed") == 1
    assert [gap["code"] for gap in gaps].count("extractor_processor_unavailable") == 1


def test_manifest_finalization_failure_replays_exact_rename_rewrite(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = repo_root / "sources/raw/notes/a.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"a")
    initialized = run_brain(
        repo_root,
        "--json",
        "init",
        services=successful_services(),
    )
    assert initialized.returncode == 0
    initialized_reference = json.loads(initialized.stdout)["data"]["result_manifest"]
    assert commands.acknowledge_sync_result_id(
        repo_root, initialized_reference["result_id"]
    ).ok
    original.rename(repo_root / "sources/raw/notes/renamed.txt")
    real_finalize = SyncResultWriter.finalize
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        corpus_revision: str,
        **kwargs: object,
    ) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected manifest finalization failure")
        return real_finalize(writer, corpus_revision, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultWriter, "finalize", fail_once)
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    assert interrupted.returncode == 1

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    rewrites = [
        event.data
        for event in SyncResultStore(RepoPaths.discover(repo_root)).iter_events(
            reference
        )
        if event.kind == "citation_rewrite"
    ]

    assert replayed.returncode == 0
    assert len(rewrites) == 1
    assert rewrites[0]["raw_path"] == "notes/renamed.txt"


def test_staged_recovery_fails_closed_if_full_ledger_checkpoint_changes(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_finalize = SyncResultWriter.finalize
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        corpus_revision: str,
        **kwargs: object,
    ) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected manifest finalization failure")
        return real_finalize(writer, corpus_revision, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultWriter, "finalize", fail_once)
    assert (
        run_brain(
            repo_root,
            "--json",
            "sync",
            services=successful_services(),
        ).returncode
        == 1
    )

    ledger = LedgerStore(RepoPaths.discover(repo_root))
    record = next(iter(ledger.load_all().values()))
    public_revision = compute_corpus_revision((record,))
    changed = replace(
        record,
        diagnostics=(
            *record.diagnostics,
            Diagnostic("external_checkpoint_change", "checkpoint changed"),
        ),
    )
    ledger.save(changed)
    assert compute_corpus_revision((changed,)) == public_revision

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    payload = json.loads(replayed.stdout)
    result_store = SyncResultStore(RepoPaths.discover(repo_root))

    assert replayed.returncode == 1
    assert payload["errors"][0]["code"] == "source_operation_failed"
    assert "canonical ledger checkpoint" in payload["errors"][0]["message"]
    assert result_store.load_staged() is not None
    assert result_store.load_pending() is None


def interrupt_after_pending_publication(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RepoPaths, SyncResultStore, PendingSyncResult, StagedSyncResult]:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_save_pending = SyncResultStore.save_pending
    failed = False

    def fail_after_publication(
        store: SyncResultStore,
        pending: PendingSyncResult,
    ) -> None:
        nonlocal failed
        real_save_pending(store, pending)
        if not failed:
            failed = True
            raise OSError("injected post-publication pending failure")

    monkeypatch.setattr(SyncResultStore, "save_pending", fail_after_publication)
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
        acknowledge=False,
    )
    assert interrupted.returncode == 1

    paths = RepoPaths.discover(repo_root)
    result_store = SyncResultStore(paths)
    pending = result_store.load_pending()
    staged = result_store.load_staged()
    assert pending is not None
    assert staged is not None
    assert (repo_root / ".brain/sync-results/inflight.jsonl").is_file()
    return paths, result_store, pending, staged


def test_pending_staged_recovery_validates_checkpoint_before_replay(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, result_store, pending, staged = interrupt_after_pending_publication(
        repo_root,
        monkeypatch,
    )

    ledger = LedgerStore(paths)
    record = next(iter(ledger.load_all().values()))
    public_revision = compute_corpus_revision((record,))
    changed = replace(
        record,
        diagnostics=(
            *record.diagnostics,
            Diagnostic("external_checkpoint_change", "checkpoint changed"),
        ),
    )
    ledger.save(changed)
    assert compute_corpus_revision((changed,)) == public_revision

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
        acknowledge=False,
    )
    payload = json.loads(replayed.stdout)

    assert replayed.returncode == 1
    assert payload["errors"][0]["code"] == "source_operation_failed"
    assert "canonical ledger checkpoint" in payload["errors"][0]["message"]
    assert result_store.load_pending() == pending
    assert result_store.load_staged() == staged
    assert (repo_root / ".brain/sync-results/inflight.jsonl").is_file()
    assert list(result_store.iter_events(pending.reference))


def test_pending_staged_acknowledgement_validates_checkpoint_before_cleanup(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, result_store, pending, staged = interrupt_after_pending_publication(
        repo_root,
        monkeypatch,
    )

    ledger = LedgerStore(paths)
    record = next(iter(ledger.load_all().values()))
    changed = replace(
        record,
        diagnostics=(
            *record.diagnostics,
            Diagnostic("external_checkpoint_change", "checkpoint changed"),
        ),
    )
    ledger.save(changed)
    assert compute_corpus_revision((changed,)) == pending.reference.corpus_revision
    assert commands.consume_sync_result_id(repo_root, pending.reference.result_id).ok

    acknowledged = commands.acknowledge_sync_result_id(
        repo_root,
        pending.reference.result_id,
    )

    assert not acknowledged.ok
    assert "canonical ledger checkpoint" in acknowledged.errors[0].message
    assert result_store.load_pending() == pending
    assert result_store.load_staged() == staged
    assert (repo_root / ".brain/sync-results/inflight.jsonl").is_file()
    assert result_store.load_acknowledged() is None


def test_pending_staged_acknowledgement_completes_recovery_once(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, result_store, pending, _staged = interrupt_after_pending_publication(
        repo_root,
        monkeypatch,
    )
    assert commands.consume_sync_result_id(repo_root, pending.reference.result_id).ok

    acknowledged = commands.acknowledge_sync_result_id(
        repo_root,
        pending.reference.result_id,
    )
    acknowledged_again = commands.acknowledge_sync_result_id(
        repo_root,
        pending.reference.result_id,
    )

    assert acknowledged.ok
    assert acknowledged.data["status"] == "acknowledged"
    assert acknowledged_again.ok
    assert acknowledged_again.data["status"] == "already_acknowledged"
    assert result_store.load_pending() is None
    assert result_store.load_staged() is None
    assert not (repo_root / ".brain/sync-results/inflight.jsonl").exists()


def test_staged_recovery_fails_closed_if_exact_event_journal_changes(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")
    real_finalize = SyncResultWriter.finalize
    failed = False

    def fail_once(
        writer: SyncResultWriter,
        corpus_revision: str,
        **kwargs: object,
    ) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected manifest finalization failure")
        return real_finalize(writer, corpus_revision, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncResultWriter, "finalize", fail_once)
    assert (
        run_brain(
            repo_root,
            "--json",
            "sync",
            services=successful_services(),
        ).returncode
        == 1
    )

    journal = repo_root / ".brain/sync-results/inflight.jsonl"
    objects = [json.loads(line) for line in journal.read_text().splitlines()]
    next_sequence = 1 + sum(item["type"] == "event" for item in objects)
    with journal.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event",
                    "sequence": next_sequence,
                    "kind": "hashed_path",
                    "data": {"path": "tampered.txt"},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    payload = json.loads(replayed.stdout)
    result_store = SyncResultStore(RepoPaths.discover(repo_root))

    assert replayed.returncode == 1
    assert "journal changed" in payload["errors"][0]["message"]
    assert result_store.load_staged() is not None
    assert result_store.load_pending() is None


def test_uncommitted_rename_intent_is_not_replayed_after_checkpoint_failure(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = repo_root / "sources/raw/notes/a.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"a")
    initialized = run_brain(
        repo_root,
        "--json",
        "init",
        services=successful_services(),
    )
    initialized_reference = json.loads(initialized.stdout)["data"]["result_manifest"]
    assert commands.acknowledge_sync_result_id(
        repo_root, initialized_reference["result_id"]
    ).ok
    renamed = repo_root / "sources/raw/notes/renamed.txt"
    original.rename(renamed)
    real_save = LedgerStore.save
    failed = False

    def fail_before_rename_publish(
        store: LedgerStore,
        record: object,
    ) -> Path:
        nonlocal failed
        if not failed and getattr(record, "current_raw_path") == PurePosixPath(
            "notes/renamed.txt"
        ):
            failed = True
            raise OSError("injected pre-publication checkpoint failure")
        return real_save(store, record)  # type: ignore[arg-type]

    monkeypatch.setattr(LedgerStore, "save", fail_before_rename_publish)
    interrupted = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    assert interrupted.returncode == 1
    renamed.unlink()

    replayed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    rewrites = [
        event
        for event in SyncResultStore(RepoPaths.discover(repo_root)).iter_events(
            reference
        )
        if event.kind == "citation_rewrite"
    ]
    record = next(iter(LedgerStore(RepoPaths.discover(repo_root)).load_all().values()))

    assert record.current_raw_path == PurePosixPath("notes/a.txt")
    assert rewrites == []


@pytest.mark.parametrize("publication_mode", ("before", "after"))
def test_pending_publication_failure_replays_exact_handoff_and_coverage_result(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_mode: str,
) -> None:
    (repo_root / "sources/raw/a.png").write_bytes(b"\x89PNG\r\n\x1a\nrest")
    real_save_pending = SyncResultStore.save_pending
    real_reconcile = commands.reconcile_inventory
    reconcile_calls = 0
    failed = False

    def counted_reconcile(*args: object, **kwargs: object) -> object:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return real_reconcile(*args, **kwargs)

    def fail_once(store: SyncResultStore, pending: object) -> None:
        nonlocal failed
        if failed:
            real_save_pending(store, pending)  # type: ignore[arg-type]
            return
        failed = True
        if publication_mode == "after":
            real_save_pending(store, pending)  # type: ignore[arg-type]
        raise OSError("injected pending publication failure")

    monkeypatch.setattr(SyncResultStore, "save_pending", fail_once)
    monkeypatch.setattr(commands, "reconcile_inventory", counted_reconcile)

    class AgentProcessor:
        def process(
            self,
            _record: object,
            _item: object,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            del paths
            diagnostic = Diagnostic(
                "agent_extraction_required", "Agent extraction is required."
            )
            return ProcessResult(
                SourceState.NEEDS_AGENT,
                None,
                ProcessingAttempt(
                    context.input_sha256,
                    extractor.extractor_id,
                    context.extractor_version,
                    context.config_sha256,
                    context.prerequisite_digest,
                    SourceState.NEEDS_AGENT,
                    context.attempted_at,
                    (diagnostic.code,),
                ),
                (diagnostic,),
            )

    services = commands.CommandServices(
        processor_factory=AgentProcessor,
        prerequisite_digest=successful_services().prerequisite_digest,
    )
    interrupted = run_brain(repo_root, "--json", "sync", services=services)
    assert interrupted.returncode == 1

    replayed = run_brain(repo_root, "--json", "sync", services=services)
    payload = json.loads(replayed.stdout)
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    events = list(SyncResultStore(RepoPaths.discover(repo_root)).iter_events(reference))

    assert replayed.returncode == 1
    assert reconcile_calls == 1
    assert [event.kind for event in events].count("handoff_source_id") == 1
    assert [event.kind for event in events].count("coverage_gap") == 1


@pytest.mark.parametrize("release_mode", ("false", "raise"))
def test_summary_and_lock_failure_replay_the_same_completed_sync_result(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_mode: str,
) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    class CleanupFailureLock:
        def release(self) -> bool:
            if release_mode == "raise":
                raise RuntimeError("release failed")
            return False

    def fail_summary(*_args: object, **_kwargs: object) -> str:
        raise OSError("summary publish failed")

    monkeypatch.setattr(
        commands.SourceWriteLock,
        "acquire",
        lambda _path: CleanupFailureLock(),
    )
    monkeypatch.setattr(LedgerStore, "write_summary", fail_summary)

    failed = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    failed_payload = json.loads(failed.stdout)
    reference_data = failed_payload["data"]["result_manifest"]

    assert failed.returncode == 1
    assert failed_payload["data"]["new_active_representation_count"] == 1
    assert [error["code"] for error in failed_payload["errors"]] == [
        "ledger_summary_refresh_failed",
        "source_lock_cleanup_failed",
    ]

    monkeypatch.undo()
    recovered = run_brain(
        repo_root,
        "--json",
        "sync",
        services=successful_services(),
    )
    recovered_payload = json.loads(recovered.stdout)

    assert recovered.returncode == 0
    assert recovered_payload["data"]["result_manifest"] == reference_data
    assert recovered_payload["data"]["new_active_representation_count"] == 1
