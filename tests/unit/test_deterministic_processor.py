from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from brainlib.contracts import (
    SourceRecord,
    SourceState,
)
from brainlib.extractors.processor import (
    DeterministicSourceProcessor,
    Job,
    build_source_processor,
    stage_pinned_input,
)
from brainlib.inventory import InventoryAccessError, InventoryItem, InventoryReport
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore
from brainlib.registry import (
    ExtractorRegistry,
    ExtractorSpec,
    ResolvedConverter,
    effective_extractor_version,
)
from brainlib.sync import (
    ProcessResult,
    ProcessingContext,
    SyncReport,
    _validate_process_result,
    reconcile_inventory,
)
from brainlib.validation import _validate_derivation_provenance
from tests.helpers_extractors import FIXED_NOW, successful_process_result


def test_factory_returns_deterministic_processor() -> None:
    assert isinstance(build_source_processor(), DeterministicSourceProcessor)


class TrackingJobs:
    def __init__(self, jobs: Sequence[Job]) -> None:
        self.jobs = jobs
        self.consumed = 0

    def __iter__(self) -> Iterator[Job]:
        for job in self.jobs:
            self.consumed += 1
            yield job


class ImmediateProcessor(DeterministicSourceProcessor):
    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        with stage_pinned_input(
            context,
            paths=paths,
            logical_path=item.fingerprint.path,
            expected_byte_size=item.fingerprint.byte_size,
        ) as staged_input:
            return successful_process_result(
                Job(record, item, extractor, context, staged_input), paths=paths
            )


def reconcile_jobs_fixture(
    jobs: Sequence[Job],
    *,
    paths: RepoPaths,
    checkpoint: Callable[[SourceRecord], None],
    max_workers: int,
    processor: DeterministicSourceProcessor | None = None,
    event_commit=None,
) -> tuple[dict[str, SourceRecord], SyncReport]:
    extractor_by_id = {job.extractor.extractor_id: job.extractor for job in jobs}
    return reconcile_inventory(
        InventoryReport(tuple(job.item for job in jobs), ()),
        {job.record.source_id: job.record for job in jobs},
        registry=ExtractorRegistry(1, tuple(extractor_by_id.values()), "f" * 64),
        processor=ImmediateProcessor() if processor is None else processor,
        paths=paths,
        prerequisite_digests={
            job.extractor.extractor_id: job.context.prerequisite_digest for job in jobs
        },
        max_workers=max_workers,
        checkpoint=checkpoint,
        now=FIXED_NOW,
        event_commit=event_commit,
    )


def test_iter_batch_does_not_consume_more_than_worker_bound_before_yield(
    jobs, repo_paths
) -> None:
    tracking = TrackingJobs(jobs)
    iterator = ImmediateProcessor().iter_batch(
        tracking, paths=repo_paths, max_workers=2
    )
    first = next(iterator)
    assert tracking.consumed == 2
    results = [first, *iterator]
    assert {source_id for source_id, _ in results} == {
        job.record.source_id for job in jobs
    }
    for job in jobs:
        with pytest.raises(OSError):
            os.fstat(job.staged_input.descriptor)


@pytest.mark.parametrize("boundary", ["processor", "retained"])
def test_direct_scheduler_success_passes_plan2_provenance_checks(
    jobs,
    repo_paths,
    boundary,
) -> None:
    jobs_by_id = {job.record.source_id: job for job in jobs}
    results = list(
        ImmediateProcessor().iter_batch(jobs, paths=repo_paths, max_workers=2)
    )
    assert {source_id for source_id, _ in results} == set(jobs_by_id)
    for source_id, result in results:
        job = jobs_by_id[source_id]
        if boundary == "processor":
            _validate_process_result(
                result,
                extractor=job.extractor,
                context=job.context,
                raw_path=job.item.fingerprint.path,
                paths=repo_paths,
            )
        else:
            assert result.derivation is not None
            issues = []
            assert _validate_derivation_provenance(
                result.derivation.derivation_id,
                result.derivation,
                issues,
            ), [issue.code for issue in issues]
            assert issues == []


@pytest.mark.parametrize("value", [0, 17, -1, True, 1.5])
def test_worker_bound_rejects_values_outside_one_to_sixteen(
    value, processor, jobs, repo_paths
) -> None:
    tracking = TrackingJobs(jobs)
    with pytest.raises(ValueError, match="1..16"):
        list(processor.iter_batch(tracking, paths=repo_paths, max_workers=value))
    assert tracking.consumed == 0


def test_default_worker_bound_uses_cpu_count(jobs, repo_paths, monkeypatch) -> None:
    monkeypatch.setattr("brainlib.extractors.processor.os.cpu_count", lambda: 3)
    tracking = TrackingJobs(jobs)
    iterator = ImmediateProcessor().iter_batch(tracking, paths=repo_paths)
    next(iterator)
    assert tracking.consumed == 3
    iterator.close()


def test_closing_iterator_closes_only_consumed_job_stages(jobs, repo_paths) -> None:
    tracking = TrackingJobs(jobs)
    iterator = ImmediateProcessor().iter_batch(
        tracking, paths=repo_paths, max_workers=2
    )
    next(iterator)
    iterator.close()
    assert tracking.consumed == 2
    for job in jobs[:2]:
        with pytest.raises(OSError):
            os.fstat(job.staged_input.descriptor)
    assert os.fstat(jobs[2].staged_input.descriptor).st_size == 7


def test_reconciliation_uses_checkpoint_and_does_not_construct_ledger_store(
    repo_paths, jobs, monkeypatch
) -> None:
    checkpointed: list[SourceRecord] = []
    store = LedgerStore(repo_paths)

    def checkpoint(record: SourceRecord) -> None:
        store.save(record)
        checkpointed.append(record)

    monkeypatch.setattr(
        "brainlib.sync.LedgerStore",
        lambda *_: pytest.fail("reconciliation must not construct LedgerStore"),
        raising=False,
    )
    records, report = reconcile_jobs_fixture(
        jobs, paths=repo_paths, checkpoint=checkpoint, max_workers=2
    )
    assert {record.source_id for record in checkpointed} == set(records)
    assert all(record.state is SourceState.OK for record in records.values())
    assert report.new_active_representation_count == len(jobs)
    assert store.load_all() == records


def test_checkpointed_result_survives_interruption(repo_paths, jobs) -> None:
    durable: dict[str, SourceRecord] = {}
    store = LedgerStore(repo_paths)

    def checkpoint(record: SourceRecord) -> None:
        store.save(record)
        durable[record.source_id] = record

    def interrupt_after_activation(kind, data) -> None:
        # Event commit follows the durable checkpoint and all authority checks.
        if kind == "new_active_representation":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reconcile_jobs_fixture(
            jobs,
            paths=repo_paths,
            checkpoint=checkpoint,
            max_workers=2,
            event_commit=interrupt_after_activation,
        )
    assert (
        sum(record.active_derivation_id is not None for record in durable.values()) == 1
    )
    assert store.load_all() == durable
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_staging_uses_only_pinned_descriptor(repo_paths, jobs) -> None:
    job = jobs[0]
    (repo_paths.raw / job.item.fingerprint.path).write_bytes(b"changed")
    context = replace(job.context, input_path=Path("/a/path/that/does/not/exist"))
    with stage_pinned_input(
        context,
        paths=repo_paths,
        logical_path=job.item.fingerprint.path,
        expected_byte_size=7,
    ) as stage:
        assert os.pread(stage.descriptor, 7, 0) == b"item 0\n"
        assert (
            stage.sha256
            == "1d76f51ee7971d1542270b1efd9345632f27a8a1b4a92e510b6ff6b01c7873dc"
        )
        assert stage._stage_path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(repo_paths.root.glob(".brain-stage-*"))


@pytest.mark.parametrize("mismatch", ["sha256", "size"])
def test_staging_rejects_mismatched_verified_content(
    repo_paths, jobs, mismatch
) -> None:
    job = jobs[0]
    context = (
        replace(job.context, input_sha256="0" * 64)
        if mismatch == "sha256"
        else job.context
    )
    with pytest.raises(InventoryAccessError):
        stage_pinned_input(
            context,
            paths=repo_paths,
            logical_path=job.item.fingerprint.path,
            expected_byte_size=8 if mismatch == "size" else 7,
        )
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_worker_stage_mutation_is_rejected_before_yield(jobs, repo_paths) -> None:
    class MutatingProcessor(ImmediateProcessor):
        def process(self, record, item, extractor, *, paths, context):
            result = super().process(
                record, item, extractor, paths=paths, context=context
            )
            stage_path = next(
                job.staged_input._stage_path
                for job in jobs
                if job.record.source_id == record.source_id
            )
            stage_path.chmod(0o600)
            stage_path.write_bytes(b"changed")
            return result

    with pytest.raises(InventoryAccessError):
        list(MutatingProcessor().iter_batch(jobs, paths=repo_paths, max_workers=1))


@pytest.mark.parametrize("target", ["raw", "stage", "output"])
def test_batch_checkpoint_revalidates_authorities_and_compensates(
    repo_paths, jobs, target
) -> None:
    durable: dict[str, SourceRecord] = {}
    store = LedgerStore(repo_paths)
    altered = False

    def checkpoint(record: SourceRecord) -> None:
        nonlocal altered
        store.save(record)
        durable[record.source_id] = record
        if record.active_derivation_id is None or altered:
            return
        altered = True
        if target == "raw":
            (repo_paths.raw / record.current_raw_path).write_bytes(b"changed")
        elif target == "output":
            derivation = record.derivations[record.active_derivation_id]
            (repo_paths.root / derivation.output_path).write_bytes(b"changed")
        else:
            stages = list(repo_paths.root.glob(".brain-stage-*/*"))
            assert stages
            for stage in stages:
                stage.chmod(0o600)
                stage.write_bytes(b"changed")

    with pytest.raises(InventoryAccessError):
        reconcile_jobs_fixture(
            jobs, paths=repo_paths, checkpoint=checkpoint, max_workers=2
        )
    assert altered is True
    assert all(record.active_derivation_id is None for record in durable.values())
    assert store.load_all() == durable
    assert not list(repo_paths.root.glob(".brain-stage-*"))


@pytest.mark.parametrize("agent_fallback", [False, True])
def test_unavailable_converter_only_requests_configured_agent_fallback(
    repo_paths, jobs, agent_fallback
) -> None:
    job = jobs[0]
    extractor = replace(
        job.extractor,
        agent_fallback=agent_fallback,
        agent_revision="1" if agent_fallback else None,
    )
    result = DeterministicSourceProcessor(resolve=lambda spec: None).process(
        job.record,
        job.item,
        extractor,
        paths=repo_paths,
        context=job.context,
    )
    assert result.state is (
        SourceState.NEEDS_AGENT if agent_fallback else SourceState.PENDING
    )
    assert result.derivation is None
    assert result.diagnostics[0].code == "extractor_processor_unavailable"
    assert not list(repo_paths.root.glob(".brain-tmp-*"))


@pytest.mark.parametrize("mismatch", ["prerequisite", "effective_version", "config"])
def test_processor_rejects_changed_resolution_before_conversion(
    repo_paths, jobs, mismatch
) -> None:
    job = jobs[0]
    version = "builtin:builtin.text:1"
    digest = hashlib.sha256(
        f"converter-v1\0builtin.text\0{version}".encode()
    ).hexdigest()
    resolved = ResolvedConverter(job.extractor.preferred, version, digest)
    context = replace(
        job.context,
        prerequisite_digest=digest,
        extractor_version=effective_extractor_version(job.extractor, digest),
    )
    if mismatch == "prerequisite":
        context = replace(context, prerequisite_digest="0" * 64)
    elif mismatch == "effective_version":
        context = replace(context, extractor_version="stale")
    else:
        context = replace(context, config_sha256="0" * 64)
    resolutions = iter((resolved,))
    result = DeterministicSourceProcessor(
        resolve=lambda spec: next(resolutions),
        run=lambda *args, **kwargs: pytest.fail("drift must not execute the converter"),
    ).process(job.record, job.item, job.extractor, paths=repo_paths, context=context)
    assert result.state is SourceState.PENDING
    assert result.attempt.outcome is SourceState.PENDING
    assert result.derivation is None
    assert result.diagnostics[0].code == "prerequisite_changed"
    assert result.attempt.diagnostic_codes == ("prerequisite_changed",)
    assert not list(repo_paths.root.glob(".brain-tmp-*"))


def test_invalid_stage_digest_cannot_remove_a_sibling_file(repo_paths, jobs) -> None:
    sibling = repo_paths.root / "sentinel"
    sibling.write_bytes(b"retain me")
    context = replace(jobs[0].context, input_sha256="../sentinel")
    with pytest.raises(ValueError, match="sha256"):
        stage_pinned_input(
            context,
            paths=repo_paths,
            logical_path=jobs[0].item.fingerprint.path,
            expected_byte_size=7,
        )
    assert sibling.read_bytes() == b"retain me"
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_reconciliation_uses_batch_interface_and_bounds_live_stages(
    repo_paths, jobs
) -> None:
    class BatchOnlyProcessor(DeterministicSourceProcessor):
        def process(self, *args, **kwargs):
            pytest.fail("batch reconciliation must consume iter_batch results")

        def iter_batch(self, source, *, paths, max_workers=None):
            iterator = iter(source)
            while True:
                chunk = []
                for _ in range(max_workers):
                    try:
                        chunk.append(next(iterator))
                    except StopIteration:
                        break
                if not chunk:
                    return
                assert len(list(paths.root.glob(".brain-stage-*"))) <= max_workers
                for job in chunk:
                    assert job.record.state is SourceState.EXTRACTING
                    assert os.pread(job.context.input_descriptor, 7, 0).startswith(
                        b"item "
                    )
                    yield (
                        job.record.source_id,
                        successful_process_result(job, paths=paths),
                    )

    records, _ = reconcile_jobs_fixture(
        jobs,
        paths=repo_paths,
        checkpoint=LedgerStore(repo_paths).save,
        max_workers=2,
        processor=BatchOnlyProcessor(),
    )
    assert len(records) == 6
    assert all(record.state is SourceState.OK for record in records.values())
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_batch_stage_change_with_repeated_compensation_failure_keeps_guard(
    repo_paths, jobs
) -> None:
    store = LedgerStore(repo_paths)
    activated = False

    def checkpoint(record):
        nonlocal activated
        if activated:
            raise OSError("compensation persistence unavailable")
        store.save(record)
        if record.active_derivation_id is not None:
            activated = True
            for stage in repo_paths.root.glob(".brain-stage-*/*"):
                stage.chmod(0o600)
                stage.write_bytes(b"changed")

    with pytest.raises(OSError, match="compensation persistence unavailable"):
        reconcile_jobs_fixture(
            jobs[:1], paths=repo_paths, checkpoint=checkpoint, max_workers=1
        )
    with pytest.raises(ValueError, match="activation"):
        store.load_all()
    store.recover_activation_guards()
    recovered = store.load_all()
    record = recovered[jobs[0].record.source_id]
    assert record.state is SourceState.EXTRACTING
    assert record.active_derivation_id is None
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_worker_bound_allows_two_jobs_to_execute_concurrently(jobs, repo_paths) -> None:
    barrier = threading.Barrier(2, timeout=3)

    class ConcurrentProcessor(ImmediateProcessor):
        def process(self, record, item, extractor, *, paths, context):
            barrier.wait()
            return super().process(
                record, item, extractor, paths=paths, context=context
            )

    results = list(
        ConcurrentProcessor().iter_batch(jobs, paths=repo_paths, max_workers=2)
    )
    assert len(results) == 6


def test_batch_retries_authority_acquisition_after_sibling_directory_creation(
    repo_paths,
    jobs,
    monkeypatch,
) -> None:
    import brainlib.inventory as inventory_module

    open_directory = inventory_module._open_directory_entry
    changed = False

    def create_sibling_before_open(parent_descriptor, name, observed):
        nonlocal changed
        if (
            not changed
            and name == "notes"
            and os.fstat(parent_descriptor).st_ino == repo_paths.extracted.stat().st_ino
        ):
            changed = True
            (repo_paths.extracted / "notes" / "worker-sibling").mkdir()
        return open_directory(parent_descriptor, name, observed)

    monkeypatch.setattr(
        inventory_module, "_open_directory_entry", create_sibling_before_open
    )
    records, _ = reconcile_jobs_fixture(
        jobs[:1],
        paths=repo_paths,
        checkpoint=LedgerStore(repo_paths).save,
        max_workers=1,
    )
    assert changed is True
    assert records[jobs[0].record.source_id].state is SourceState.OK


def test_eager_batch_setup_failure_closes_its_consumed_stage(repo_paths, jobs) -> None:
    class FailingBatchProcessor(DeterministicSourceProcessor):
        def iter_batch(self, source, *, paths, max_workers=None):
            next(iter(source))
            raise RuntimeError("batch setup failed")

    with pytest.raises(RuntimeError, match="batch setup failed"):
        reconcile_jobs_fixture(
            jobs,
            paths=repo_paths,
            checkpoint=lambda record: None,
            max_workers=2,
            processor=FailingBatchProcessor(),
        )
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_batch_authority_acquisition_exhaustion_preserves_inventory_error(
    repo_paths,
    jobs,
    monkeypatch,
) -> None:
    import brainlib.inventory as inventory_module

    open_directory = inventory_module._open_directory_entry
    sibling_count = 0

    def create_sibling_before_every_open(parent_descriptor, name, observed):
        nonlocal sibling_count
        if (
            name == "notes"
            and os.fstat(parent_descriptor).st_ino == repo_paths.extracted.stat().st_ino
        ):
            assert sibling_count < 3, "authority acquisition exceeded its fixed cap"
            sibling_count += 1
            (repo_paths.extracted / "notes" / f"sibling-{sibling_count}").mkdir()
        return open_directory(parent_descriptor, name, observed)

    monkeypatch.setattr(
        inventory_module, "_open_directory_entry", create_sibling_before_every_open
    )
    with pytest.raises(InventoryAccessError) as caught:
        reconcile_jobs_fixture(
            jobs[:1],
            paths=repo_paths,
            checkpoint=lambda record: None,
            max_workers=1,
        )
    assert sibling_count == 3
    assert str(caught.value.__cause__) == "directory changed before open"
    assert not list(repo_paths.root.glob(".brain-stage-*"))


@pytest.mark.parametrize("target", ["file", "directory"])
def test_stage_publication_is_rechecked_after_hashing(
    repo_paths, jobs, monkeypatch, target
) -> None:
    import brainlib.extractors.processor as processor_module

    stage = jobs[0].staged_input
    assert stage._stage_path is not None
    directory = stage._stage_path.parent
    moved = directory.with_name(directory.name + "-moved")
    original_hash = processor_module._descriptor_sha256

    def hash_then_replace(descriptor, byte_size):
        digest = original_hash(descriptor, byte_size)
        assert stage._stage_path is not None
        if target == "directory":
            directory.rename(moved)
            directory.mkdir(mode=0o700)
            stage._stage_path.write_bytes(b"item 0\n")
        else:
            replacement = directory / "replacement"
            replacement.write_bytes(b"item 0\n")
            replacement.chmod(0o400)
            os.replace(replacement, stage._stage_path)
        return digest

    monkeypatch.setattr(processor_module, "_descriptor_sha256", hash_then_replace)
    try:
        with pytest.raises(InventoryAccessError):
            stage.revalidate()
    finally:
        stage.close()
        if target == "directory":
            stage._stage_path.unlink()
            directory.rmdir()
            moved.rmdir()
