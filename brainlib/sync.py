from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from .contracts import (
    ContentVersion,
    Derivation,
    ProcessingAttempt,
    SourceRecord,
    SourceRepresentation,
    SourceState,
    UrlDescriptorMetadata,
    compute_corpus_revision,
    compute_sha256,
    derivation_id,
    source_id_for_first_seen,
)
from .diagnostics import Diagnostic, JSONValue
from .inventory import (
    InventoryAccessError,
    InventoryItem,
    InventoryReport,
    PinnedFile,
    SnapshotNamespace,
    hash_inventory_item,
    source_id_for_url_descriptor,
    use_stable_file,
    validate_inventory_item,
    validate_inventory_path,
)
from .layout import RepoPaths
from .ledger import (
    ActivationGuard,
    CitationRewrite,
    activate_derivation,
    recover_stale_extraction,
    representation_for,
    retry_is_eligible,
)
from .registry import ExtractorRegistry, ExtractorSpec, effective_extractor_version
from .sync_results import SyncResultReference

if TYPE_CHECKING:
    from .extractors.processor import StagedInput


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAMPLED_DECISIONS = 100
_MAX_REPORT_SAMPLE_ITEMS = 100
_MAX_REPORT_SAMPLE_BYTES = 32 * 1024
_PROCESSOR_STATES = frozenset(
    {
        SourceState.PENDING,
        SourceState.OK,
        SourceState.WARNING,
        SourceState.NEEDS_AGENT,
        SourceState.FAILED,
    }
)
_COVERAGE_STATES = frozenset(
    {
        SourceState.PENDING,
        SourceState.WARNING,
        SourceState.NEEDS_AGENT,
        SourceState.FAILED,
        SourceState.UNSUPPORTED,
        SourceState.INTEGRITY_ERROR,
        SourceState.AWAITING_APPROVAL,
    }
)


@dataclass(frozen=True)
class ProcessResult:
    state: SourceState
    derivation: Derivation | None
    attempt: ProcessingAttempt
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class ProcessingContext:
    input_sha256: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    attempted_at: datetime
    input_path: Path
    input_descriptor: int


class SourceProcessor(Protocol):
    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult: ...


class UnavailableProcessor:
    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        del record, item, paths
        diagnostic = Diagnostic(
            "extractor_processor_unavailable",
            "No source processor is available for this extractor.",
        )
        return ProcessResult(
            SourceState.PENDING,
            None,
            ProcessingAttempt(
                context.input_sha256,
                extractor.extractor_id,
                context.extractor_version,
                context.config_sha256,
                context.prerequisite_digest,
                SourceState.PENDING,
                context.attempted_at,
                (diagnostic.code,),
            ),
            (diagnostic,),
        )


class SyncAction(StrEnum):
    CREATE = "create"
    RENAME = "rename"
    UPDATE_DESCRIPTOR = "update_descriptor"
    MARK_INTEGRITY_ERROR = "mark_integrity_error"
    MARK_MISSING = "mark_missing"
    PROCESS = "process"
    QUEUE_NEEDS_AGENT = "queue_needs_agent"
    AWAIT_WEB_APPROVAL = "await_web_approval"
    RETAIN = "retain"


@dataclass(frozen=True)
class SyncDecision:
    source_id: str | None
    action: SyncAction
    reason: str
    item: InventoryItem | None


@dataclass(frozen=True)
class SyncReport:
    corpus_revision: str
    decision_counts: Mapping[SyncAction, int]
    sampled_decisions: tuple[SyncDecision, ...]
    hashed_paths: tuple[PurePosixPath, ...]
    new_active_representations: tuple[SourceRepresentation, ...]
    citation_rewrites: tuple[CitationRewrite, ...]
    handoff_source_ids: tuple[str, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    hashed_path_count: int
    new_active_representation_count: int
    citation_rewrite_count: int
    handoff_source_id_count: int
    coverage_gap_count: int
    result_manifest: SyncResultReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_counts",
            MappingProxyType(dict(self.decision_counts)),
        )
        for name in (
            "sampled_decisions",
            "hashed_paths",
            "new_active_representations",
            "citation_rewrites",
            "handoff_source_ids",
            "coverage_gaps",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for count_name, sample_name in (
            ("hashed_path_count", "hashed_paths"),
            ("new_active_representation_count", "new_active_representations"),
            ("citation_rewrite_count", "citation_rewrites"),
            ("handoff_source_id_count", "handoff_source_ids"),
            ("coverage_gap_count", "coverage_gaps"),
        ):
            count = getattr(self, count_name)
            if type(count) is not int or count < len(getattr(self, sample_name)):
                raise ValueError(f"{count_name} must cover its bounded sample")


@dataclass(frozen=True)
class _Assignment:
    record: SourceRecord
    created: bool = False
    renamed: bool = False
    ambiguous: bool = False


class _UnverifiedInventoryItem(Exception):
    pass


class _DecisionCollector:
    def __init__(self) -> None:
        self.counts = {action: 0 for action in SyncAction}
        self._sample: list[tuple[tuple[str, str, str, str], SyncDecision]] = []

    def add(self, decision: SyncDecision) -> None:
        self.counts[decision.action] += 1
        path = (
            "" if decision.item is None else decision.item.fingerprint.path.as_posix()
        )
        key = (
            path,
            "" if decision.source_id is None else decision.source_id,
            decision.action.value,
            decision.reason,
        )
        position = bisect.bisect_left([item[0] for item in self._sample], key)
        self._sample.insert(position, (key, decision))
        while (
            len(self._sample) > _MAX_SAMPLED_DECISIONS
            or sum(_decision_sample_size(item[1]) for item in self._sample)
            > _MAX_REPORT_SAMPLE_BYTES
        ):
            self._sample.pop()

    @property
    def sample(self) -> tuple[SyncDecision, ...]:
        return tuple(item[1] for item in self._sample)


_Sample = TypeVar("_Sample")


class _BoundedCollector(Generic[_Sample]):
    def __init__(
        self,
        *,
        key: Callable[[_Sample], tuple[str, ...]],
        encoded_size: Callable[[_Sample], int],
    ) -> None:
        self.count = 0
        self._key = key
        self._encoded_size = encoded_size
        self._sample: list[tuple[tuple[str, ...], _Sample, int]] = []
        self._sample_bytes = 0

    def add(self, value: _Sample) -> None:
        self.count += 1
        size = self._encoded_size(value)
        key = self._key(value)
        keys = [item[0] for item in self._sample]
        position = bisect.bisect_left(keys, key)
        self._sample.insert(position, (key, value, size))
        self._sample_bytes += size
        while (
            len(self._sample) > _MAX_REPORT_SAMPLE_ITEMS
            or self._sample_bytes > _MAX_REPORT_SAMPLE_BYTES
        ):
            _key, _value, removed_size = self._sample.pop()
            self._sample_bytes -= removed_size

    @property
    def sample(self) -> tuple[_Sample, ...]:
        return tuple(item[1] for item in self._sample)


def reconcile_inventory(
    inventory: InventoryReport,
    records: Mapping[str, SourceRecord],
    *,
    registry: ExtractorRegistry,
    processor: SourceProcessor,
    paths: RepoPaths,
    prerequisite_digests: Mapping[str, str],
    max_workers: int | None = None,
    hash_file: Callable[[Path], str] = compute_sha256,
    checkpoint: Callable[[SourceRecord], object] | None = None,
    now: datetime,
    explicit_retry: bool = False,
    explicit_retry_source_ids: Collection[str] = (),
    event_sink: Callable[[str, Mapping[str, JSONValue]], None] | None = None,
    event_commit: Callable[[str, Mapping[str, JSONValue]], None] | None = None,
) -> tuple[dict[str, SourceRecord], SyncReport]:
    """Reconcile one inventory snapshot without acquiring the writer lock.

    The caller owns the outer lock and may supply ``LedgerStore.save`` as the
    checkpoint callback. Processor work is always bracketed by durable
    EXTRACTING and final-state checkpoints.
    """

    from .extractors.processor import (
        BatchSourceProcessor,
        Job,
        stage_pinned_input,
        validate_max_workers,
    )

    worker_count = validate_max_workers(max_workers)
    _validate_now(now)
    if event_sink is not None and not callable(event_sink):
        raise ValueError("event_sink must be callable")
    if event_commit is not None and not callable(event_commit):
        raise ValueError("event_commit must be callable")

    def emit(kind: str, data: Mapping[str, JSONValue]) -> None:
        if event_sink is not None:
            event_sink(kind, data)

    def commit_event(kind: str, data: Mapping[str, JSONValue]) -> None:
        if event_commit is not None:
            event_commit(kind, data)

    selected_digests = _validated_prerequisite_digests(registry, prerequisite_digests)
    retry_ids = frozenset(explicit_retry_source_ids)
    if any(source_id not in records for source_id in retry_ids):
        raise ValueError("explicit retry source IDs must name existing records")

    updated = _validated_record_copy(records)
    ordered_items = _validated_items(inventory)
    item_by_path = {item.fingerprint.path: item for item in ordered_items}
    record_by_path = _record_path_index(updated)
    skip_diagnostics = tuple(sorted(inventory.skipped, key=_diagnostic_key))
    obscured_records = {
        source_id
        for source_id, record in updated.items()
        if record.current_raw_path not in item_by_path
        and _path_is_obscured(record.current_raw_path, skip_diagnostics)
    }

    hash_cache: dict[PurePosixPath, str] = {}
    hashed_paths = _BoundedCollector[PurePosixPath](
        key=lambda value: (value.as_posix(),),
        encoded_size=lambda value: len(value.as_posix().encode("utf-8")),
    )
    unsafe_diagnostics: dict[PurePosixPath, Diagnostic] = {}

    def content_sha256(item: InventoryItem) -> str:
        if item.url_descriptor is not None:
            raise ValueError("URL descriptor bytes must not be hashed")
        if item.sha256 is not None:
            _validate_sha256(item.sha256, "inventory sha256")
            return item.sha256
        raw_path = item.fingerprint.path
        if raw_path in unsafe_diagnostics:
            raise _UnverifiedInventoryItem(raw_path.as_posix())
        cached = hash_cache.get(raw_path)
        if cached is not None:
            return cached
        try:
            checksum = hash_inventory_item(paths, item, hash_file=hash_file)
        except InventoryAccessError as error:
            unsafe_diagnostics[raw_path] = Diagnostic(
                "source_changed_during_sync",
                "Source no longer matches its safely inventoried fingerprint.",
                raw_path,
            )
            raise _UnverifiedInventoryItem(raw_path.as_posix()) from error
        _validate_sha256(checksum, "computed sha256")
        hash_cache[raw_path] = checksum
        hashed_paths.add(raw_path)
        emit("hashed_path", {"path": raw_path.as_posix()})
        return checksum

    exact_paths = set(item_by_path) & set(record_by_path)
    unmatched_items = [
        item for item in ordered_items if item.fingerprint.path not in exact_paths
    ]
    missing_records = [
        record
        for record in updated.values()
        if record.current_raw_path not in item_by_path
        and record.source_id not in obscured_records
    ]

    candidate_groups: dict[tuple[str, str], list[InventoryItem]] = defaultdict(list)
    for item in unmatched_items:
        try:
            key = _rename_key_for_item(item, content_sha256)
        except _UnverifiedInventoryItem:
            continue
        candidate_groups[key].append(item)
    missing_groups: dict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    for record in missing_records:
        key = _rename_key_for_record(record)
        if key is not None:
            missing_groups[key].append(record)

    assignments: dict[PurePosixPath, _Assignment] = {
        path: _Assignment(record_by_path[path]) for path in exact_paths
    }
    consumed_missing: set[str] = set()
    ambiguous_keys: set[tuple[str, str]] = set()
    citation_rewrites = _BoundedCollector[CitationRewrite](
        key=lambda value: (
            value.source_id,
            value.content_sha256,
            value.raw_path.as_posix(),
        ),
        encoded_size=lambda value: _json_size(value.to_dict()),
    )
    pending_rewrites_by_source: dict[str, tuple[CitationRewrite, ...]] = {}

    for key in sorted(set(candidate_groups) & set(missing_groups)):
        candidates = candidate_groups[key]
        missing = missing_groups[key]
        if len(candidates) == 1 and len(missing) == 1:
            item = candidates[0]
            renamed, rewrites = _rename_record(
                missing[0],
                item,
                checksum=None if key[0] == "url" else key[1],
                now=now,
            )
            assignments[item.fingerprint.path] = _Assignment(renamed, renamed=True)
            for rewrite in rewrites:
                citation_rewrites.add(rewrite)
            if rewrites:
                pending_rewrites_by_source[renamed.source_id] = rewrites
            consumed_missing.add(missing[0].source_id)
        else:
            ambiguous_keys.add(key)

    planned_ids: dict[PurePosixPath, str] = {}
    planned_id_values: set[str] = set()
    for item in unmatched_items:
        if item.fingerprint.path in assignments:
            continue
        try:
            key = _rename_key_for_item(item, content_sha256)
        except _UnverifiedInventoryItem:
            continue
        source_id = (
            source_id_for_url_descriptor(item.url_descriptor)
            if item.url_descriptor is not None
            else source_id_for_first_seen(item.fingerprint.path, content_sha256(item))
        )
        if source_id in updated or source_id in planned_id_values:
            raise ValueError(
                f"source_id collision would overwrite retained history: {source_id}"
            )
        planned_ids[item.fingerprint.path] = source_id
        planned_id_values.add(source_id)
        assignments[item.fingerprint.path] = _Assignment(
            _new_record(
                item,
                source_id=source_id,
                checksum=(
                    None if item.url_descriptor is not None else content_sha256(item)
                ),
                now=now,
            ),
            created=True,
            ambiguous=key in ambiguous_keys,
        )

    decisions = _DecisionCollector()
    new_representations = _BoundedCollector[SourceRepresentation](
        key=lambda value: (
            value.source_id,
            value.content_sha256,
            value.derivation_id,
        ),
        encoded_size=lambda value: _json_size(_representation_data(value)),
    )

    def publish(record: SourceRecord) -> None:
        record.to_dict()
        rewrite_events: list[dict[str, JSONValue]] = []
        for rewrite in pending_rewrites_by_source.get(record.source_id, ()):
            if (
                rewrite.source_id == record.source_id
                and rewrite.raw_path == record.current_raw_path
                and rewrite.content_sha256 in record.versions
            ):
                rewrite_data = {
                    **rewrite.to_dict(),
                    "record_sha256": _record_sha256(record),
                }
                emit("citation_rewrite", rewrite_data)
                rewrite_events.append(rewrite_data)
        updated[record.source_id] = record
        if checkpoint is not None:
            checkpoint(record)
        for rewrite_data in rewrite_events:
            commit_event("citation_rewrite", rewrite_data)
        if rewrite_events:
            pending_rewrites_by_source.pop(record.source_id, None)

    def prepare_representation(
        representation: SourceRepresentation,
        final: SourceRecord,
    ) -> dict[str, JSONValue]:
        new_representations.add(representation)
        return {
            **_representation_data(representation),
            "record_sha256": _record_sha256(final),
        }

    batch_requests: list[
        tuple[SourceRecord, InventoryItem, ExtractorSpec, str, _Assignment]
    ] = []
    batch_processor = isinstance(processor, BatchSourceProcessor)

    def processed_decision(
        result_record: SourceRecord,
        item: InventoryItem,
        assignment: _Assignment,
    ) -> None:
        if result_record.state is SourceState.NEEDS_AGENT:
            action = SyncAction.QUEUE_NEEDS_AGENT
        elif assignment.renamed:
            action = SyncAction.RENAME
        elif assignment.created:
            action = SyncAction.CREATE
        else:
            action = SyncAction.PROCESS
        decisions.add(
            SyncDecision(
                result_record.source_id,
                action,
                _reason(item.fingerprint.path, action),
                item,
            )
        )

    for item in ordered_items:
        unsafe = unsafe_diagnostics.get(item.fingerprint.path)
        if unsafe is not None:
            existing = record_by_path.get(item.fingerprint.path)
            decisions.add(
                SyncDecision(
                    None if existing is None else existing.source_id,
                    SyncAction.RETAIN,
                    f"{item.fingerprint.path.as_posix()}: live source is unverified",
                    item,
                )
            )
            continue
        assignment = assignments[item.fingerprint.path]
        record = assignment.record

        if item.url_descriptor is not None:
            if record.state is SourceState.EXTRACTING:
                record = recover_stale_extraction(record, now=now)
                publish(record)
            reconciled = _reconcile_url_descriptor(record, item, now=now)
            if assignment.ambiguous:
                reconciled = _with_diagnostic(
                    reconciled,
                    _ambiguous_rename(item.fingerprint.path),
                    now=now,
                )
            if reconciled != record or assignment.created or assignment.renamed:
                publish(reconciled)
            action = _url_action(
                record,
                reconciled,
                created=assignment.created,
                renamed=assignment.renamed,
            )
            decisions.add(
                SyncDecision(
                    reconciled.source_id,
                    action,
                    _reason(item.fingerprint.path, action),
                    item,
                )
            )
            continue

        try:
            checksum = _checksum_for_existing_item(
                record,
                item,
                content_sha256=content_sha256,
            )
        except _UnverifiedInventoryItem:
            decisions.add(
                SyncDecision(
                    record.source_id,
                    SyncAction.RETAIN,
                    f"{item.fingerprint.path.as_posix()}: live source is unverified",
                    item,
                )
            )
            continue
        if record.state is SourceState.EXTRACTING:
            record = recover_stale_extraction(record, now=now)
            publish(record)
        if checksum != record.active_content_sha256:
            reconciled = _mark_integrity_error(record, item, checksum, now=now)
            if reconciled != record or assignment.created or assignment.renamed:
                publish(reconciled)
            decisions.add(
                SyncDecision(
                    reconciled.source_id,
                    SyncAction.MARK_INTEGRITY_ERROR,
                    _reason(item.fingerprint.path, SyncAction.MARK_INTEGRITY_ERROR),
                    item,
                )
            )
            continue

        record = _refresh_matching_bytes(record, item, now=now)
        if assignment.ambiguous:
            record = _with_diagnostic(
                record,
                _ambiguous_rename(item.fingerprint.path),
                now=now,
            )
        extractor = registry.select(item.media_type, item.fingerprint.path)
        if extractor is None:
            reconciled = _mark_unsupported(record, item, now=now)
            if (
                reconciled != assignment.record
                or assignment.created
                or assignment.renamed
            ):
                publish(reconciled)
            action = SyncAction.CREATE if assignment.created else SyncAction.RETAIN
            if not assignment.created:
                action = SyncAction.RENAME if assignment.renamed else SyncAction.RETAIN
            decisions.add(
                SyncDecision(
                    reconciled.source_id,
                    action,
                    _reason(item.fingerprint.path, action),
                    item,
                )
            )
            continue

        prerequisite = selected_digests[extractor.extractor_id]
        forced = explicit_retry or record.source_id in retry_ids
        if (
            _processing_is_current(
                record,
                checksum,
                extractor=extractor,
                prerequisite_digest=prerequisite,
            )
            and not forced
        ):
            if record != assignment.record or assignment.created or assignment.renamed:
                publish(record)
            action = SyncAction.RENAME if assignment.renamed else SyncAction.RETAIN
            decisions.add(
                SyncDecision(
                    record.source_id,
                    action,
                    _reason(item.fingerprint.path, action),
                    item,
                )
            )
            continue

        if not retry_is_eligible(
            record,
            input_sha256=checksum,
            extractor=extractor,
            prerequisite_digest=prerequisite,
            explicit_retry=forced,
        ):
            if record != assignment.record or assignment.created or assignment.renamed:
                publish(record)
            action = SyncAction.RENAME if assignment.renamed else SyncAction.RETAIN
            decisions.add(
                SyncDecision(
                    record.source_id,
                    action,
                    _reason(item.fingerprint.path, action),
                    item,
                )
            )
            continue

        if batch_processor:
            batch_requests.append((record, item, extractor, prerequisite, assignment))
            continue

        result_record, representation = _process_record(
            record,
            item,
            extractor,
            prerequisite_digest=prerequisite,
            processor=processor,
            paths=paths,
            now=now,
            publish=publish,
            prepare_representation=prepare_representation,
            emit=emit,
            commit_event=commit_event,
            durable_checkpoint=checkpoint is not None,
        )
        processed_decision(result_record, item, assignment)

    if batch_processor:
        pending: dict[str, tuple[Job, SourceRecord, _Assignment, object]] = {}
        jobs_exhausted = False

        def staged_jobs() -> Iterator[Job]:
            nonlocal jobs_exhausted
            for record, item, extractor, prerequisite, assignment in batch_requests:
                if len(pending) >= worker_count:
                    raise ValueError("batch processor exceeded the staged worker bound")
                stages: list[StagedInput] = []

                def prepare_job(pinned: PinnedFile) -> tuple[Job, object]:
                    if pinned.snapshot.sha256 != record.active_content_sha256:
                        raise InventoryAccessError(
                            "pinned processor input does not match active content"
                        )
                    context = ProcessingContext(
                        pinned.snapshot.sha256,
                        effective_extractor_version(extractor, prerequisite),
                        extractor.config_sha256,
                        prerequisite,
                        now,
                        pinned.descriptor_path,
                        pinned.descriptor,
                    )
                    extracting = _extracting_record(record, extractor, context)
                    publish(extracting)
                    stage = stage_pinned_input(
                        context,
                        paths=paths,
                        logical_path=item.fingerprint.path,
                        expected_byte_size=item.fingerprint.byte_size,
                    )
                    stages.append(stage)
                    staged_context = replace(
                        context,
                        input_path=stage.descriptor_path,
                        input_descriptor=stage.descriptor,
                    )
                    return (
                        Job(extracting, item, extractor, staged_context, stage),
                        pinned.snapshot.identity,
                    )

                try:
                    job, input_identity = use_stable_file(
                        paths,
                        SnapshotNamespace.RAW_USER,
                        item.fingerprint.path,
                        prepare_job,
                        include_sha256=True,
                        expected_fingerprint=item.fingerprint,
                    )
                except BaseException:
                    for stage in stages:
                        stage.close()
                    raise
                pending[record.source_id] = (job, record, assignment, input_identity)
                yield job
            jobs_exhausted = True

        assert isinstance(processor, BatchSourceProcessor)
        job_iterator = staged_jobs()
        result_iterator: Iterator[tuple[str, ProcessResult]] | None = None
        try:
            result_iterator = processor.iter_batch(
                job_iterator,
                paths=paths,
                max_workers=worker_count,
            )
            for source_id, result in result_iterator:
                if source_id not in pending:
                    raise ValueError(
                        "batch processor returned an unknown or repeated source"
                    )
                job, original, assignment, input_identity = pending[source_id]
                try:
                    job.staged_input.revalidate()
                    result_record, representation = _process_record(
                        original,
                        job.item,
                        job.extractor,
                        prerequisite_digest=job.context.prerequisite_digest,
                        processor=processor,
                        paths=paths,
                        now=now,
                        publish=publish,
                        prepare_representation=prepare_representation,
                        emit=emit,
                        commit_event=commit_event,
                        durable_checkpoint=checkpoint is not None,
                        precomputed_result=result,
                        staged_input=job.staged_input,
                        expected_input_identity=input_identity,
                    )
                    processed_decision(result_record, job.item, assignment)
                finally:
                    job.staged_input.close()
                    pending.pop(source_id)
            if pending or not jobs_exhausted:
                raise ValueError("batch processor did not return every source result")
        finally:
            close_results = getattr(result_iterator, "close", None)
            try:
                if close_results is not None:
                    close_results()
            finally:
                job_iterator.close()
                for job, _, _, _ in pending.values():
                    job.staged_input.close()

    for record in sorted(
        missing_records,
        key=lambda item: (item.current_raw_path.as_posix(), item.source_id),
    ):
        if record.source_id in consumed_missing:
            continue
        if record.state is SourceState.EXTRACTING:
            record = recover_stale_extraction(record, now=now)
            publish(record)
        ambiguous = _rename_key_for_record(record) in ambiguous_keys
        marked = _mark_missing(record, ambiguous=ambiguous, now=now)
        if marked != record:
            publish(marked)
        decisions.add(
            SyncDecision(
                marked.source_id,
                SyncAction.MARK_MISSING,
                _reason(marked.current_raw_path, SyncAction.MARK_MISSING),
                None,
            )
        )

    for source_id in sorted(obscured_records):
        record = updated[source_id]
        decisions.add(
            SyncDecision(
                source_id,
                SyncAction.RETAIN,
                f"{record.current_raw_path.as_posix()}: inventory visibility is unknown",
                None,
            )
        )

    ordered_records = dict(sorted(updated.items()))
    live_gaps = tuple(
        unsafe_diagnostics[path]
        for path in sorted(unsafe_diagnostics, key=PurePosixPath.as_posix)
    )
    coverage_gaps = _BoundedCollector[Diagnostic](
        key=_diagnostic_key,
        encoded_size=lambda value: _json_size(_diagnostic_data(value)),
    )
    coverage_occurrences: Counter[str] = Counter()
    occurrence_owner: str | None | object = object()
    for source_id, diagnostic in _iter_coverage_gaps(
        ordered_records,
        tuple(sorted((*skip_diagnostics, *live_gaps), key=_diagnostic_key)),
    ):
        if source_id != occurrence_owner:
            coverage_occurrences.clear()
            occurrence_owner = source_id
        coverage_gaps.add(diagnostic)
        diagnostic_data = _diagnostic_data(diagnostic)
        occurrence_key = json.dumps(
            diagnostic_data,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        occurrence = coverage_occurrences[occurrence_key]
        coverage_occurrences[occurrence_key] += 1
        event_data: dict[str, JSONValue] = {
            **diagnostic_data,
            "source_id": source_id,
            "occurrence": occurrence,
        }
        if source_id is not None:
            event_data["record_sha256"] = _record_sha256(ordered_records[source_id])
        emit("coverage_gap", event_data)
    handoff_source_ids = _BoundedCollector[str](
        key=lambda value: (value,),
        encoded_size=lambda value: len(value.encode("utf-8")),
    )
    for current in ordered_records.values():
        if current.state is SourceState.NEEDS_AGENT:
            handoff_source_ids.add(current.source_id)
            emit(
                "handoff_source_id",
                {
                    "source_id": current.source_id,
                    "record_sha256": _record_sha256(current),
                },
            )
    report = SyncReport(
        compute_corpus_revision(ordered_records.values()),
        decisions.counts,
        decisions.sample,
        hashed_paths.sample,
        new_representations.sample,
        citation_rewrites.sample,
        handoff_source_ids.sample,
        coverage_gaps.sample,
        hashed_paths.count,
        new_representations.count,
        citation_rewrites.count,
        handoff_source_ids.count,
        coverage_gaps.count,
    )
    return ordered_records, report


def _validated_record_copy(
    records: Mapping[str, SourceRecord],
) -> dict[str, SourceRecord]:
    copied: dict[str, SourceRecord] = {}
    for source_id, record in sorted(records.items()):
        if source_id != record.source_id:
            raise ValueError("record mapping key must match source_id")
        record.to_dict()
        copied[source_id] = record
    return copied


def _validated_items(inventory: InventoryReport) -> tuple[InventoryItem, ...]:
    if not isinstance(inventory, InventoryReport):
        raise ValueError("inventory must be an InventoryReport")
    validated = tuple(validate_inventory_item(item) for item in inventory.items)
    paths: set[PurePosixPath] = set()
    for item in validated:
        path = item.fingerprint.path
        if path in paths:
            raise ValueError(f"inventory contains duplicate path: {path.as_posix()}")
        paths.add(path)
    for diagnostic in inventory.skipped:
        if not isinstance(diagnostic, Diagnostic):
            raise ValueError("inventory skipped entries must be diagnostics")
        if diagnostic.path is not None:
            validate_inventory_path(diagnostic.path, "inventory diagnostic path")
        if type(diagnostic.code) is not str or not diagnostic.code:
            raise ValueError("inventory diagnostic code must be nonempty")
        if type(diagnostic.message) is not str or not diagnostic.message:
            raise ValueError("inventory diagnostic message must be nonempty")
        try:
            json_safe_details(diagnostic)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "inventory diagnostic details must be JSON-safe"
            ) from error
    return tuple(sorted(validated, key=lambda item: item.fingerprint.path.as_posix()))


def _record_path_index(
    records: Mapping[str, SourceRecord],
) -> dict[PurePosixPath, SourceRecord]:
    result: dict[PurePosixPath, SourceRecord] = {}
    for record in records.values():
        if record.current_raw_path in result:
            raise ValueError("ledger contains duplicate current_raw_path values")
        result[record.current_raw_path] = record
    return result


def _validated_prerequisite_digests(
    registry: ExtractorRegistry,
    values: Mapping[str, str],
) -> dict[str, str]:
    expected = {extractor.extractor_id for extractor in registry.extractors}
    if set(values) != expected:
        raise ValueError("prerequisite digests must exactly match registry extractors")
    result = dict(values)
    for extractor_id, digest in result.items():
        _validate_sha256(digest, f"prerequisite digest for {extractor_id}")
    return result


def _rename_key_for_item(
    item: InventoryItem,
    content_sha256: Callable[[InventoryItem], str],
) -> tuple[str, str]:
    if item.url_descriptor is not None:
        return ("url", item.url_descriptor.url)
    return ("sha256", content_sha256(item))


def _rename_key_for_record(record: SourceRecord) -> tuple[str, str] | None:
    if record.url_descriptor is not None:
        return ("url", record.url_descriptor.url)
    if record.active_content_sha256 is None:
        return None
    return ("sha256", record.active_content_sha256)


def _rename_record(
    record: SourceRecord,
    item: InventoryItem,
    *,
    checksum: str | None,
    now: datetime,
) -> tuple[SourceRecord, tuple[CitationRewrite, ...]]:
    old_path = record.current_raw_path
    new_path = item.fingerprint.path
    previous_paths = record.previous_raw_paths
    if old_path not in previous_paths:
        previous_paths = (*previous_paths, old_path)

    if item.url_descriptor is not None:
        descriptor = record.url_descriptor
        if descriptor is None:
            raise ValueError("URL rename requires URL-backed record")
        renamed = replace(
            record,
            current_raw_path=new_path,
            previous_raw_paths=previous_paths,
            url_descriptor=replace(descriptor, fingerprint=item.fingerprint),
            inspected_at=now,
            updated_at=now,
        )
        renamed.to_dict()
        return renamed, ()

    if checksum is None:
        raise ValueError("file rename requires a resolved checksum")
    version = record.versions.get(checksum)
    if version is None or version.raw_path != old_path:
        raise ValueError("rename checksum must name materialized current content")
    updated_version = replace(
        version,
        raw_path=new_path,
        byte_size=item.fingerprint.byte_size,
        fingerprint=item.fingerprint,
    )
    versions = dict(record.versions)
    versions[checksum] = updated_version
    renamed = replace(
        record,
        current_raw_path=new_path,
        previous_raw_paths=previous_paths,
        byte_size=item.fingerprint.byte_size,
        versions=versions,
        inspected_at=now,
        updated_at=now,
    )
    renamed.to_dict()
    return renamed, (CitationRewrite(record.source_id, checksum, new_path),)


def _new_record(
    item: InventoryItem,
    *,
    source_id: str,
    checksum: str | None,
    now: datetime,
) -> SourceRecord:
    if item.url_descriptor is not None:
        descriptor = item.url_descriptor
        record = SourceRecord(
            1,
            source_id,
            item.fingerprint.path,
            (),
            item.media_type,
            item.fingerprint.byte_size,
            SourceState.AWAITING_APPROVAL,
            {},
            None,
            {},
            None,
            None,
            (
                Diagnostic(
                    "web_capture_awaiting_approval",
                    "URL capture requires explicit approval.",
                    item.fingerprint.path,
                ),
            ),
            now,
            now,
            now,
            url_descriptor=UrlDescriptorMetadata(
                descriptor.url,
                descriptor.description,
                descriptor.added,
                item.fingerprint,
            ),
        )
        record.to_dict()
        return record

    assert checksum is not None
    version = ContentVersion(
        checksum,
        item.fingerprint.path,
        item.fingerprint.byte_size,
        item.fingerprint,
        now,
        (),
    )
    record = SourceRecord(
        1,
        source_id,
        item.fingerprint.path,
        (),
        item.media_type,
        item.fingerprint.byte_size,
        SourceState.PENDING,
        {checksum: version},
        checksum,
        {},
        None,
        None,
        (),
        now,
        now,
        now,
    )
    record.to_dict()
    return record


def _checksum_for_existing_item(
    record: SourceRecord,
    item: InventoryItem,
    *,
    content_sha256: Callable[[InventoryItem], str],
) -> str:
    active_checksum = record.active_content_sha256
    if active_checksum is None:
        return content_sha256(item)
    version = record.versions[active_checksum]
    metadata_matches = (
        version.raw_path == item.fingerprint.path
        and version.fingerprint == item.fingerprint
        and record.current_raw_path == item.fingerprint.path
    )
    continuity_broken = record.state is SourceState.INTEGRITY_ERROR or any(
        diagnostic.code == "raw_missing" for diagnostic in record.diagnostics
    )
    if metadata_matches and not continuity_broken:
        return active_checksum
    return content_sha256(item)


def _refresh_matching_bytes(
    record: SourceRecord,
    item: InventoryItem,
    *,
    now: datetime,
) -> SourceRecord:
    checksum = record.active_content_sha256
    if checksum is None:
        return record
    version = record.versions[checksum]
    restoring_presence = record.state is SourceState.INTEGRITY_ERROR or any(
        diagnostic.code == "raw_missing" for diagnostic in record.diagnostics
    )
    cleaned = (
        tuple(
            diagnostic
            for diagnostic in record.diagnostics
            if diagnostic.code not in {"raw_checksum_mismatch", "raw_missing"}
        )
        if restoring_presence
        else record.diagnostics
    )
    state = _restored_state(record) if restoring_presence else record.state
    if (
        version.raw_path == item.fingerprint.path
        and version.fingerprint == item.fingerprint
        and record.byte_size == item.fingerprint.byte_size
        and record.media_type == item.media_type
        and record.state is state
        and cleaned == record.diagnostics
    ):
        return record
    versions = dict(record.versions)
    versions[checksum] = replace(
        version,
        raw_path=item.fingerprint.path,
        byte_size=item.fingerprint.byte_size,
        fingerprint=item.fingerprint,
    )
    refreshed = replace(
        record,
        current_raw_path=item.fingerprint.path,
        media_type=item.media_type,
        byte_size=item.fingerprint.byte_size,
        state=state,
        versions=versions,
        diagnostics=cleaned,
        inspected_at=now,
        updated_at=now,
    )
    refreshed.to_dict()
    return refreshed


def _active_state(record: SourceRecord) -> SourceState:
    active_id = record.active_derivation_id
    if active_id is None:
        return SourceState.PENDING
    active = record.derivations.get(active_id)
    if active is None:
        return SourceState.PENDING
    return SourceState.OK if active.quality_state == "ok" else SourceState.WARNING


def _restored_state(record: SourceRecord) -> SourceState:
    attempt = record.last_attempt
    if (
        attempt is not None
        and attempt.input_sha256 == record.active_content_sha256
        and attempt.outcome
        in {SourceState.PENDING, SourceState.FAILED, SourceState.NEEDS_AGENT}
    ):
        return attempt.outcome
    return _active_state(record)


def _processing_is_current(
    record: SourceRecord,
    checksum: str,
    *,
    extractor: ExtractorSpec,
    prerequisite_digest: str,
) -> bool:
    if record.state not in {SourceState.OK, SourceState.WARNING}:
        return False
    active_id = record.active_derivation_id
    if active_id is None:
        return False
    active = record.derivations.get(active_id)
    if active is None:
        return False
    return (
        active.source_sha256 == checksum
        and active.extractor_id == extractor.extractor_id
        and active.extractor_version
        == effective_extractor_version(extractor, prerequisite_digest)
        and active.config_sha256 == extractor.config_sha256
        and active.derivation_id
        == derivation_id(
            source_sha256=checksum,
            extractor_id=extractor.extractor_id,
            extractor_version=effective_extractor_version(
                extractor, prerequisite_digest
            ),
            config_sha256=extractor.config_sha256,
        )
    )


def _mark_integrity_error(
    record: SourceRecord,
    item: InventoryItem,
    observed_sha256: str,
    *,
    now: datetime,
) -> SourceRecord:
    diagnostic = Diagnostic(
        "raw_checksum_mismatch",
        "Raw bytes differ from the active ledgered content version.",
        item.fingerprint.path,
        {
            "active_sha256": record.active_content_sha256 or "",
            "observed_sha256": observed_sha256,
        },
    )
    return _replace_state_with_diagnostic(
        record, SourceState.INTEGRITY_ERROR, diagnostic, now=now
    )


def _mark_unsupported(
    record: SourceRecord,
    item: InventoryItem,
    *,
    now: datetime,
) -> SourceRecord:
    diagnostic = Diagnostic(
        "unsupported_source",
        "No approved extractor matches this source.",
        item.fingerprint.path,
        {"media_type": item.media_type, "extension": item.extension},
    )
    return _replace_state_with_diagnostic(
        record, SourceState.UNSUPPORTED, diagnostic, now=now
    )


def _replace_state_with_diagnostic(
    record: SourceRecord,
    state: SourceState,
    diagnostic: Diagnostic,
    *,
    now: datetime,
) -> SourceRecord:
    diagnostics = _upsert_diagnostic(record.diagnostics, diagnostic)
    if record.state is state and diagnostics == record.diagnostics:
        return record
    changed = replace(
        record,
        state=state,
        diagnostics=diagnostics,
        inspected_at=now,
        updated_at=now,
    )
    changed.to_dict()
    return changed


def _mark_missing(
    record: SourceRecord,
    *,
    ambiguous: bool,
    now: datetime,
) -> SourceRecord:
    diagnostic = Diagnostic(
        "raw_missing",
        "The current raw source path is missing.",
        record.current_raw_path,
    )
    diagnostics = _upsert_diagnostic(record.diagnostics, diagnostic)
    if ambiguous:
        diagnostics = _upsert_diagnostic(
            diagnostics, _ambiguous_rename(record.current_raw_path)
        )
    state = (
        SourceState.AWAITING_APPROVAL
        if record.url_descriptor is not None
        and (
            record.state is SourceState.AWAITING_APPROVAL
            or record.active_content_sha256 is None
        )
        else SourceState.WARNING
    )
    if record.state is state and diagnostics == record.diagnostics:
        return record
    marked = replace(
        record,
        state=state,
        diagnostics=diagnostics,
        inspected_at=now,
        updated_at=now,
    )
    marked.to_dict()
    return marked


def _reconcile_url_descriptor(
    record: SourceRecord,
    item: InventoryItem,
    *,
    now: datetime,
) -> SourceRecord:
    descriptor = item.url_descriptor
    if descriptor is None:
        raise ValueError("URL reconciliation requires descriptor metadata")
    prior = record.url_descriptor
    if prior is None:
        raise ValueError("URL inventory item must match a URL-backed record")
    metadata = UrlDescriptorMetadata(
        descriptor.url,
        descriptor.description,
        descriptor.added,
        item.fingerprint,
    )
    was_missing = any(
        diagnostic.code == "raw_missing" for diagnostic in record.diagnostics
    )
    diagnostics = tuple(
        diagnostic
        for diagnostic in record.diagnostics
        if diagnostic.code != "raw_missing"
    )
    url_changed = prior.url != descriptor.url
    approval_outstanding = (
        url_changed
        or not record.versions
        or (
            record.active_content_sha256 is None
            and record.active_derivation_id is None
            and (
                record.state is SourceState.AWAITING_APPROVAL
                or any(
                    diagnostic.code == "web_capture_awaiting_approval"
                    for diagnostic in diagnostics
                )
            )
        )
    )
    if approval_outstanding:
        state = SourceState.AWAITING_APPROVAL
        active_content_sha256 = None
        active_derivation_id = None
        diagnostics = _upsert_diagnostic(
            diagnostics,
            Diagnostic(
                "web_capture_awaiting_approval",
                "URL capture requires explicit approval.",
                item.fingerprint.path,
            ),
        )
    elif record.active_content_sha256 is not None and record.active_derivation_id:
        state = _active_state(record) if was_missing else record.state
        active_content_sha256 = record.active_content_sha256
        active_derivation_id = record.active_derivation_id
        diagnostics = tuple(
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.code != "web_capture_awaiting_approval"
        )
    else:
        state = record.state
        active_content_sha256 = record.active_content_sha256
        active_derivation_id = record.active_derivation_id
        diagnostics = tuple(
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.code != "web_capture_awaiting_approval"
        )
    if (
        metadata == prior
        and state is record.state
        and active_content_sha256 == record.active_content_sha256
        and active_derivation_id == record.active_derivation_id
        and diagnostics == record.diagnostics
    ):
        return record
    reconciled = replace(
        record,
        current_raw_path=item.fingerprint.path,
        state=state,
        active_content_sha256=active_content_sha256,
        active_derivation_id=active_derivation_id,
        media_type=item.media_type
        if active_content_sha256 is None
        else record.media_type,
        byte_size=item.fingerprint.byte_size
        if active_content_sha256 is None
        else record.byte_size,
        diagnostics=diagnostics,
        inspected_at=now,
        updated_at=now,
        url_descriptor=metadata,
    )
    reconciled.to_dict()
    return reconciled


def _extracting_record(
    record: SourceRecord,
    extractor: ExtractorSpec,
    context: ProcessingContext,
) -> SourceRecord:
    return replace(
        record,
        state=SourceState.EXTRACTING,
        last_attempt=ProcessingAttempt(
            context.input_sha256,
            extractor.extractor_id,
            context.extractor_version,
            context.config_sha256,
            context.prerequisite_digest,
            SourceState.EXTRACTING,
            context.attempted_at,
            (),
        ),
        inspected_at=context.attempted_at,
        updated_at=context.attempted_at,
    )


def _process_record(
    record: SourceRecord,
    item: InventoryItem,
    extractor: ExtractorSpec,
    *,
    prerequisite_digest: str,
    processor: SourceProcessor,
    paths: RepoPaths,
    now: datetime,
    publish: Callable[[SourceRecord], None],
    prepare_representation: Callable[
        [SourceRepresentation, SourceRecord], Mapping[str, JSONValue]
    ],
    emit: Callable[[str, Mapping[str, JSONValue]], None],
    commit_event: Callable[[str, Mapping[str, JSONValue]], None],
    durable_checkpoint: bool,
    precomputed_result: ProcessResult | None = None,
    staged_input: StagedInput | None = None,
    expected_input_identity: object | None = None,
) -> tuple[SourceRecord, SourceRepresentation | None]:
    input_sha256 = record.active_content_sha256
    if input_sha256 is None:
        raise ValueError("processor requires active file content")

    activation_rollback: list[SourceRecord] = []
    activation_guard: ActivationGuard | None = None

    def restore_extracting_checkpoint() -> None:
        if not activation_rollback:
            return
        extracting = activation_rollback[-1]
        try:
            publish(extracting)
        except Exception:
            # A checkpoint error can be raised before or after its atomic publish.
            # Retain the exact rollback post-image and retry it before allowing the
            # active derivation to escape this authority boundary.
            publish(extracting)
        if activation_guard is not None:
            activation_guard._clear_after_checkpoint(extracting)
        activation_rollback.clear()

    def process_pinned(
        pinned: PinnedFile,
    ) -> tuple[
        SourceRecord,
        ProcessResult,
        SourceRecord | None,
        SourceRepresentation | None,
        Mapping[str, JSONValue] | None,
    ]:
        if (
            expected_input_identity is not None
            and pinned.snapshot.identity != expected_input_identity
        ):
            raise InventoryAccessError("raw input identity changed after staging")
        if pinned.snapshot.sha256 != input_sha256:
            raise InventoryAccessError(
                "pinned processor input does not match active content"
            )
        context = ProcessingContext(
            input_sha256,
            effective_extractor_version(extractor, prerequisite_digest),
            extractor.config_sha256,
            prerequisite_digest,
            now,
            pinned.descriptor_path,
            pinned.descriptor,
        )
        previous_attempt = record.last_attempt
        extracting = _extracting_record(record, extractor, context)
        if precomputed_result is None:
            publish(extracting)
            result = processor.process(
                extracting,
                item,
                extractor,
                paths=paths,
                context=context,
            )
        else:
            result = precomputed_result
        if staged_input is not None:
            staged_input.revalidate()
        _validate_process_result(
            result,
            extractor=extractor,
            context=context,
            raw_path=extracting.current_raw_path,
            paths=paths,
        )
        diagnostics = _replace_attempt_diagnostics(
            record.diagnostics,
            previous_attempt,
            result.diagnostics,
        )
        prepared = replace(
            extracting,
            last_attempt=result.attempt,
            diagnostics=diagnostics,
            inspected_at=now,
            updated_at=now,
        )
        derivation = result.derivation
        if derivation is None:
            return prepared, result, None, None, None

        activation_derivation = derivation
        retained_derivation = prepared.derivations.get(derivation.derivation_id)
        if retained_derivation is not None:
            replayed_derivation = replace(
                derivation,
                created_at=retained_derivation.created_at,
            )
            if replayed_derivation != retained_derivation:
                raise ValueError(
                    "derivation_id collision would replace retained history"
                )
            activation_derivation = retained_derivation

        artifact_callback_started = False

        def activate_pinned(
            artifact: PinnedFile,
        ) -> tuple[
            SourceRecord,
            SourceRepresentation,
            Mapping[str, JSONValue],
        ]:
            nonlocal activation_guard, artifact_callback_started
            artifact_callback_started = True
            _validate_pinned_process_artifact(activation_derivation, artifact)
            final = activate_derivation(prepared, activation_derivation, now=now)
            representation = representation_for(
                final,
                final.active_content_sha256 or "",
                final.active_derivation_id or "",
            )
            if representation is None:
                raise ValueError(
                    "processor result did not create an active representation"
                )
            event_data = prepare_representation(representation, final)
            emit("new_active_representation", event_data)
            if durable_checkpoint:
                activation_guard = ActivationGuard.prepare(paths, extracting, final)
            activation_rollback.append(replace(extracting, active_derivation_id=None))
            try:
                publish(final)
            except BaseException:
                restore_extracting_checkpoint()
                raise
            if staged_input is not None:
                staged_input.revalidate()
            return final, representation, event_data

        # Other batch workers can create sibling output directories during the
        # initial stat/open handshake. Reacquire a complete authority only before
        # the activation callback has run; never repeat checkpoint side effects.
        batch_artifact = precomputed_result is not None and staged_input is not None
        artifact_open_attempts = 3 if batch_artifact else 1
        for open_attempt in range(artifact_open_attempts):
            try:
                final, representation, event_data = use_stable_file(
                    paths,
                    SnapshotNamespace.EXTRACTED,
                    derivation.output_path,
                    activate_pinned,
                    include_sha256=True,
                )
                break
            except InventoryAccessError as error:
                if activation_rollback:
                    restore_extracting_checkpoint()
                    raise
                if (
                    not artifact_callback_started
                    and open_attempt + 1 < artifact_open_attempts
                ):
                    continue
                if batch_artifact:
                    raise
                raise ValueError(
                    "processor result artifact could not be observed safely"
                ) from error
        return prepared, result, final, representation, event_data

    try:
        prepared, result, activated, representation, event_data = use_stable_file(
            paths,
            SnapshotNamespace.RAW_USER,
            item.fingerprint.path,
            process_pinned,
            include_sha256=True,
            expected_fingerprint=item.fingerprint,
        )
        if staged_input is not None:
            staged_input.revalidate()
    except BaseException:
        if activation_rollback:
            restore_extracting_checkpoint()
        raise
    if activated is not None:
        if activation_guard is not None:
            activation_guard._clear_after_checkpoint(activated)
        activation_rollback.clear()
        assert event_data is not None
        commit_event("new_active_representation", event_data)
        return activated, representation

    final = replace(prepared, state=result.state)
    final.to_dict()
    publish(final)
    return final, None


def _validate_process_result(
    result: ProcessResult,
    *,
    extractor: ExtractorSpec,
    context: ProcessingContext,
    raw_path: PurePosixPath,
    paths: RepoPaths,
) -> None:
    if not isinstance(result, ProcessResult):
        raise ValueError("processor result must be a ProcessResult")
    if result.state not in _PROCESSOR_STATES:
        raise ValueError("processor result state is not a processing outcome")
    attempt = result.attempt
    expected_attempt = (
        context.input_sha256,
        extractor.extractor_id,
        context.extractor_version,
        context.config_sha256,
        context.prerequisite_digest,
        result.state,
        context.attempted_at,
        tuple(diagnostic.code for diagnostic in result.diagnostics),
    )
    observed_attempt = (
        attempt.input_sha256,
        attempt.extractor_id,
        attempt.extractor_version,
        attempt.config_sha256,
        attempt.prerequisite_digest,
        attempt.outcome,
        attempt.attempted_at,
        attempt.diagnostic_codes,
    )
    if observed_attempt != expected_attempt:
        raise ValueError("processor result attempt does not match its context")
    if result.state is SourceState.NEEDS_AGENT and not extractor.agent_fallback:
        raise ValueError("processor result requested an unconfigured agent fallback")
    derivation = result.derivation
    if derivation is None:
        if result.state in {SourceState.OK, SourceState.WARNING}:
            raise ValueError("processor result success requires a derivation")
        return
    if result.state not in {SourceState.OK, SourceState.WARNING}:
        raise ValueError("processor result derivation requires a searchable outcome")
    expected_quality = "ok" if result.state is SourceState.OK else "warning"
    expected_identifier = derivation_id(
        source_sha256=context.input_sha256,
        extractor_id=extractor.extractor_id,
        extractor_version=context.extractor_version,
        config_sha256=context.config_sha256,
    )
    expected_output_path = PurePosixPath(
        "sources/extracted",
        *raw_path.parts,
        context.input_sha256,
        expected_identifier + extractor.output_suffix,
    )
    converter_id = derivation.method_metadata.get("converter_id")
    converter_version = derivation.method_metadata.get("converter_version")
    approved_converter_ids = {
        converter.converter_id
        for converter in (extractor.preferred, *extractor.fallbacks)
    }
    converter_digest = (
        None
        if type(converter_id) is not str or type(converter_version) is not str
        else hashlib.sha256(
            f"converter-v1\0{converter_id}\0{converter_version}".encode("utf-8")
        ).hexdigest()
    )
    if (
        derivation.source_sha256 != context.input_sha256
        or derivation.extractor_id != extractor.extractor_id
        or derivation.extractor_version != context.extractor_version
        or derivation.config_sha256 != context.config_sha256
        or derivation.derivation_id != expected_identifier
        or derivation.output_path != expected_output_path
        or derivation.method != "deterministic"
        or set(derivation.method_metadata)
        != {
            "converter_id",
            "converter_version",
        }
        or converter_id not in approved_converter_ids
        or converter_digest != context.prerequisite_digest
        or type(derivation.output_byte_size) is not int
        or derivation.output_byte_size < 0
        or derivation.output_byte_size > extractor.max_output_bytes
        or derivation.quality_state != expected_quality
        or derivation.created_at != context.attempted_at
        or not derivation.anchors
        or any(
            anchor.kind not in extractor.expected_anchors
            for anchor in derivation.anchors
        )
    ):
        raise ValueError("processor result derivation does not match its context")


def _validate_pinned_process_artifact(
    derivation: Derivation,
    artifact: PinnedFile,
) -> None:
    if (
        artifact.snapshot.sha256 != derivation.output_sha256
        or artifact.snapshot.byte_size != derivation.output_byte_size
        or artifact.snapshot.mtime_ns != derivation.output_mtime_ns
    ):
        raise ValueError("processor result artifact metadata does not match bytes")


def _replace_attempt_diagnostics(
    existing: tuple[Diagnostic, ...],
    previous_attempt: ProcessingAttempt | None,
    current: tuple[Diagnostic, ...],
) -> tuple[Diagnostic, ...]:
    previous_codes = (
        frozenset()
        if previous_attempt is None
        else frozenset(previous_attempt.diagnostic_codes)
    )
    retained = tuple(item for item in existing if item.code not in previous_codes)
    result = retained
    for diagnostic in current:
        result = _upsert_diagnostic(result, diagnostic)
    return result


def _upsert_diagnostic(
    diagnostics: tuple[Diagnostic, ...], diagnostic: Diagnostic
) -> tuple[Diagnostic, ...]:
    result = tuple(item for item in diagnostics if item.code != diagnostic.code)
    result = (*result, diagnostic)
    return tuple(sorted(result, key=_diagnostic_key))


def _with_diagnostic(
    record: SourceRecord,
    diagnostic: Diagnostic,
    *,
    now: datetime,
) -> SourceRecord:
    diagnostics = _upsert_diagnostic(record.diagnostics, diagnostic)
    if diagnostics == record.diagnostics:
        return record
    changed = replace(record, diagnostics=diagnostics, inspected_at=now, updated_at=now)
    changed.to_dict()
    return changed


def _ambiguous_rename(path: PurePosixPath) -> Diagnostic:
    return Diagnostic(
        "ambiguous_rename",
        "Checksum matching did not identify exactly one missing source and one new path.",
        path,
    )


def _path_is_obscured(
    path: PurePosixPath,
    skipped: tuple[Diagnostic, ...],
) -> bool:
    for diagnostic in skipped:
        scope = diagnostic.path
        if scope is None or path == scope or scope in path.parents:
            return True
    return False


def _iter_coverage_gaps(
    records: Mapping[str, SourceRecord],
    skipped: tuple[Diagnostic, ...],
) -> Iterator[tuple[str | None, Diagnostic]]:
    for diagnostic in skipped:
        yield None, diagnostic
    for record in records.values():
        if record.state not in _COVERAGE_STATES:
            continue
        if record.diagnostics:
            for diagnostic in record.diagnostics:
                yield record.source_id, diagnostic
        else:
            yield (
                record.source_id,
                Diagnostic(
                    f"source_{record.state.value}",
                    f"Source remains {record.state.value}.",
                    record.current_raw_path,
                ),
            )


def _url_action(
    before: SourceRecord,
    after: SourceRecord,
    *,
    created: bool,
    renamed: bool,
) -> SyncAction:
    if renamed:
        return SyncAction.RENAME
    if created or (
        after.state is SourceState.AWAITING_APPROVAL
        and before.state is not SourceState.AWAITING_APPROVAL
    ):
        return SyncAction.AWAIT_WEB_APPROVAL
    if before.url_descriptor != after.url_descriptor:
        return SyncAction.UPDATE_DESCRIPTOR
    return SyncAction.RETAIN


def _reason(path: PurePosixPath, action: SyncAction) -> str:
    return f"{path.as_posix()}: {action.value}"


def _diagnostic_key(diagnostic: Diagnostic) -> tuple[str, str, str, str]:
    return (
        "" if diagnostic.path is None else diagnostic.path.as_posix(),
        diagnostic.code,
        diagnostic.message,
        json_safe_details(diagnostic),
    )


def json_safe_details(diagnostic: Diagnostic) -> str:
    return json.dumps(
        diagnostic.details,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _diagnostic_data(diagnostic: Diagnostic) -> dict[str, JSONValue]:
    return {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "path": (None if diagnostic.path is None else diagnostic.path.as_posix()),
        "details": dict(diagnostic.details),
    }


def _representation_data(
    representation: SourceRepresentation,
) -> dict[str, JSONValue]:
    return {
        "source_id": representation.source_id,
        "content_sha256": representation.content_sha256,
        "derivation_id": representation.derivation_id,
        "raw_path": representation.raw_path.as_posix(),
        "extracted_path": representation.extracted_path.as_posix(),
        "output_sha256": representation.output_sha256,
        "quality_state": representation.quality_state,
        "anchors": [
            {"kind": anchor.kind, "value": anchor.value}
            for anchor in representation.anchors
        ],
    }


def _record_sha256(record: SourceRecord) -> str:
    payload = (
        json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decision_sample_size(decision: SyncDecision) -> int:
    item = decision.item
    item_data: JSONValue
    if item is None:
        item_data = None
    else:
        descriptor = item.url_descriptor
        item_data = {
            "path": item.fingerprint.path.as_posix(),
            "byte_size": item.fingerprint.byte_size,
            "mtime_ns": item.fingerprint.mtime_ns,
            "media_type": item.media_type,
            "extension": item.extension,
            "sha256": item.sha256,
            "url": None if descriptor is None else descriptor.url,
            "description": None if descriptor is None else descriptor.description,
        }
    return _json_size(
        {
            "source_id": decision.source_id,
            "action": decision.action.value,
            "reason": decision.reason,
            "item": item_data,
        }
    )


def _json_size(value: Mapping[str, JSONValue]) -> int:
    return len(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _validate_sha256(value: object, name: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lower-case hexadecimal characters")


def _validate_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
