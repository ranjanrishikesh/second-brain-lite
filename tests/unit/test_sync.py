from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tracemalloc
from dataclasses import FrozenInstanceError, replace
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

import brainlib.inventory as inventory_module
import brainlib.sync as sync_module
from brainlib.contracts import (
    Anchor,
    ContentVersion,
    Derivation,
    FileFingerprint,
    ProcessingAttempt,
    SourceRecord,
    SourceState,
    UrlDescriptorMetadata,
    compute_corpus_revision,
    compute_sha256,
    derivation_id,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic
from brainlib.inventory import (
    InventoryItem,
    InventoryReport,
    MediaDetector,
    UrlDescriptor,
    inventory_raw_sources,
)
from brainlib.layout import RepoPaths
from brainlib.output import sync_report_data
from brainlib.registry import (
    ConverterSpec,
    ExecutionMode,
    ExtractorRegistry,
    ExtractorSpec,
    effective_extractor_version,
)
from brainlib.sync import (
    ProcessResult,
    ProcessingContext,
    SyncAction,
    UnavailableProcessor,
    reconcile_inventory,
)
from brainlib.sync_results import SyncResultStore
from tests.helpers import FIXED_NOW, write_bytes


PREREQUISITE = "ff08162014400d58758854f133117e5a3ebfe91b1281da40cf57d977f608fb49"
CHANGED_PREREQUISITE = (
    "e36d32c8925dfe17c453764afaa17a40ff1c831b85bfbc3fba16005a6c195803"
)
CONFIG_SHA256 = "c" * 64
CONTENT_BYTES = b"content"
CONTENT_SHA256 = hashlib.sha256(CONTENT_BYTES).hexdigest()


def make_registry(
    *,
    media_type: str = "text/plain",
    extension: str = ".txt",
    extractor_id: str = "text",
    agent_fallback: bool = False,
) -> ExtractorRegistry:
    converter = ConverterSpec(
        converter_id="fixture.converter",
        executable="fixture-converter",
        argv_template=("{input}", "{output}"),
        version_args=("--version",),
        python_distribution=None,
        install_recipes={},
    )
    extractor = ExtractorSpec(
        extractor_id=extractor_id,
        extractor_version="1",
        timeout_seconds=10,
        max_output_bytes=1_000,
        media_types=(media_type,),
        extensions=(extension,),
        mode=ExecutionMode.PYTHON,
        output_suffix=".md",
        expected_anchors=("block",) if media_type == "image/png" else ("line",),
        agent_fallback=agent_fallback,
        agent_revision="1" if agent_fallback else None,
        preferred=converter,
        fallbacks=(),
        config_sha256=CONFIG_SHA256,
    )
    return ExtractorRegistry(1, (extractor,), "b" * 64)


def prerequisite_map(
    registry: ExtractorRegistry, value: str = PREREQUISITE
) -> dict[str, str]:
    return {extractor.extractor_id: value for extractor in registry.extractors}


def make_item(
    *,
    path: str = "notes/a.txt",
    content_sha256: str | None = None,
    size: int = 7,
    mtime_ns: int = 1,
    media_type: str = "text/plain",
    extension: str = ".txt",
    url_descriptor: UrlDescriptor | None = None,
) -> InventoryItem:
    return InventoryItem(
        FileFingerprint(PurePosixPath(path), size, mtime_ns),
        media_type,
        extension,
        content_sha256,
        url_descriptor,
    )


def make_aligned_record(
    *,
    path: str = "notes/a.txt",
    checksum: str = CONTENT_SHA256,
    source_id: str | None = None,
    size: int = 7,
    mtime_ns: int = 1,
    registry: ExtractorRegistry | None = None,
) -> SourceRecord:
    selected_registry = make_registry() if registry is None else registry
    extractor = selected_registry.extractors[0]
    raw_path = PurePosixPath(path)
    identifier = derivation_id(
        source_sha256=checksum,
        extractor_id=extractor.extractor_id,
        extractor_version=effective_extractor_version(extractor, PREREQUISITE),
        config_sha256=extractor.config_sha256,
    )
    version = ContentVersion(
        checksum,
        raw_path,
        size,
        FileFingerprint(raw_path, size, mtime_ns),
        FIXED_NOW,
        (),
    )
    derivation = Derivation(
        identifier,
        checksum,
        extractor.extractor_id,
        effective_extractor_version(extractor, PREREQUISITE),
        extractor.config_sha256,
        PurePosixPath(
            "sources/extracted", *raw_path.parts, checksum, identifier + ".md"
        ),
        "d" * 64,
        9,
        1,
        "ok",
        (Anchor(extractor.expected_anchors[0], "1"),),
        FIXED_NOW,
        method="deterministic",
        method_metadata={
            "converter_id": extractor.preferred.converter_id,
            "converter_version": "fixture-1",
        },
    )
    return SourceRecord(
        1,
        source_id or source_id_for_first_seen(raw_path, checksum),
        raw_path,
        (),
        extractor.media_types[0],
        size,
        SourceState.OK,
        {checksum: version},
        checksum,
        {identifier: derivation},
        identifier,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
    )


def make_url_item(
    *,
    path: str = "urls/a.url.md",
    url: str = "https://example.test/a",
    description: str = "A",
    size: int = 40,
    mtime_ns: int = 1,
) -> InventoryItem:
    raw_path = PurePosixPath(path)
    return make_item(
        path=path,
        size=size,
        mtime_ns=mtime_ns,
        media_type="application/x.second-brain-url-descriptor",
        extension=".url.md",
        url_descriptor=UrlDescriptor(
            raw_path,
            url,
            description,
            date(2026, 9, 4),
        ),
    )


def make_captured_url_record(item: InventoryItem) -> SourceRecord:
    assert item.url_descriptor is not None
    base = make_aligned_record()
    checksum = base.active_content_sha256 or ""
    snapshot_path = PurePosixPath("_web", base.source_id, checksum, "a.html")
    version = replace(
        base.versions[checksum],
        raw_path=snapshot_path,
        fingerprint=replace(base.versions[checksum].fingerprint, path=snapshot_path),
    )
    metadata = UrlDescriptorMetadata(
        item.url_descriptor.url,
        item.url_descriptor.description,
        item.url_descriptor.added,
        item.fingerprint,
    )
    return replace(
        base,
        current_raw_path=item.fingerprint.path,
        versions={checksum: version},
        url_descriptor=metadata,
    )


def never_hash(path: Path) -> str:
    raise AssertionError(f"unexpected hash for {path}")


def materialize_item(paths: RepoPaths, item: InventoryItem, content: bytes) -> None:
    assert len(content) == item.fingerprint.byte_size
    raw = write_bytes(paths.raw / item.fingerprint.path, content)
    os.utime(
        raw,
        ns=(item.fingerprint.mtime_ns, item.fingerprint.mtime_ns),
    )


class RecordingUnavailableProcessor(UnavailableProcessor):
    def __init__(self) -> None:
        self.calls: list[tuple[SourceRecord, InventoryItem, ProcessingContext]] = []

    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        self.calls.append((record, item, context))
        return super().process(
            record,
            item,
            extractor,
            paths=paths,
            context=context,
        )


class NeedsAgentProcessor:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.calls = 0

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
        self.calls += 1
        if self.events is not None:
            self.events.append("process")
        diagnostic = Diagnostic(
            "agent_extraction_required", "Agent review is required."
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


class WarningProcessor:
    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        del item
        identifier = derivation_id(
            source_sha256=context.input_sha256,
            extractor_id=extractor.extractor_id,
            extractor_version=context.extractor_version,
            config_sha256=context.config_sha256,
        )
        output_path = PurePosixPath(
            "sources/extracted",
            *record.current_raw_path.parts,
            context.input_sha256,
            identifier + ".md",
        )
        output_bytes = b"x"
        output = write_bytes(paths.root / output_path, output_bytes)
        output_mtime_ns = output.stat().st_mtime_ns
        derivation = Derivation(
            identifier,
            context.input_sha256,
            extractor.extractor_id,
            context.extractor_version,
            context.config_sha256,
            output_path,
            hashlib.sha256(output_bytes).hexdigest(),
            len(output_bytes),
            output_mtime_ns,
            "warning",
            (Anchor("line", "1"),),
            context.attempted_at,
            method="deterministic",
            method_metadata={
                "converter_id": "fixture.converter",
                "converter_version": "fixture-1",
            },
        )
        diagnostic = Diagnostic("partial_text", "Searchable with a limitation.")
        attempt = ProcessingAttempt(
            context.input_sha256,
            extractor.extractor_id,
            context.extractor_version,
            context.config_sha256,
            context.prerequisite_digest,
            SourceState.WARNING,
            context.attempted_at,
            (diagnostic.code,),
        )
        return ProcessResult(SourceState.WARNING, derivation, attempt, (diagnostic,))


def test_unchanged_tree_uses_metadata_fast_path_without_hashing(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)

    updated, report = reconcile_inventory(
        InventoryReport((make_item(),), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    assert updated[record.source_id] is record
    assert report.hashed_paths == ()
    assert report.decision_counts[SyncAction.RETAIN] == 1


def test_same_bytes_with_changed_metadata_hash_once_and_refresh_fingerprint(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    item = make_item(mtime_ns=2, content_sha256=None)
    paths = RepoPaths.discover(repo_root)
    materialize_item(paths, item, b"changed")
    calls: list[Path] = []

    def hash_once(path: Path) -> str:
        calls.append(path)
        return CONTENT_SHA256

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=hash_once,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert len(calls) == 1
    assert report.hashed_paths == (PurePosixPath("notes/a.txt"),)
    assert result.versions[CONTENT_SHA256].fingerprint == item.fingerprint
    assert result.state is SourceState.OK


def test_changed_ledgered_raw_bytes_remain_unadopted_integrity_error(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    changed_sha = "9" * 64
    changed_item = make_item(content_sha256=changed_sha, mtime_ns=2)

    updated, report = reconcile_inventory(
        InventoryReport((changed_item,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is SourceState.INTEGRITY_ERROR
    assert result.active_content_sha256 == CONTENT_SHA256
    assert result.versions == record.versions
    assert "raw_checksum_mismatch" in {item.code for item in result.diagnostics}
    assert "raw_checksum_mismatch" in {item.code for item in report.coverage_gaps}


def test_restored_integrity_bytes_rehash_even_when_metadata_matches_and_reactivate(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = replace(
        make_aligned_record(registry=registry),
        state=SourceState.INTEGRITY_ERROR,
        diagnostics=(Diagnostic("raw_checksum_mismatch", "Replacement detected."),),
    )
    restored = make_item(content_sha256=None)
    paths = RepoPaths.discover(repo_root)
    materialize_item(paths, restored, b"restore")
    hashed: list[Path] = []

    def restored_hash(path: Path) -> str:
        hashed.append(path)
        return CONTENT_SHA256

    updated, _ = reconcile_inventory(
        InventoryReport((restored,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=restored_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is SourceState.OK
    assert result.active_derivation_id == record.active_derivation_id
    assert result.versions[CONTENT_SHA256].fingerprint == restored.fingerprint
    assert "raw_checksum_mismatch" not in {item.code for item in result.diagnostics}
    assert len(hashed) == 1


def test_unavailable_processor_is_not_retried_for_the_same_attempt_identity(
    repo_root: Path,
) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    processor = RecordingUnavailableProcessor()

    first, _ = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=processor,
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    second, report = reconcile_inventory(
        InventoryReport((item,), ()),
        first,
        registry=registry,
        processor=processor,
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    record = next(iter(second.values()))
    assert processor.calls and len(processor.calls) == 1
    assert record.state is SourceState.PENDING
    assert record.last_attempt is not None
    assert record.last_attempt.prerequisite_digest == PREREQUISITE
    assert report.decision_counts[SyncAction.RETAIN] == 1


def test_prerequisite_change_retries_an_unchanged_pending_source(
    repo_root: Path,
) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    first, _ = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    processor = RecordingUnavailableProcessor()

    second, _ = reconcile_inventory(
        InventoryReport((replace(item, sha256=None),), ()),
        first,
        registry=registry,
        processor=processor,
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry, CHANGED_PREREQUISITE),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    assert len(processor.calls) == 1
    result_attempt = next(iter(second.values())).last_attempt
    assert result_attempt is not None
    assert result_attempt.prerequisite_digest == CHANGED_PREREQUISITE


def test_agent_fallback_handoff_only_comes_from_the_processor(repo_root: Path) -> None:
    registry = make_registry(
        media_type="image/png",
        extension=".png",
        extractor_id="image",
        agent_fallback=True,
    )
    item = make_item(
        path="images/a.png",
        content_sha256=CONTENT_SHA256,
        media_type="image/png",
        extension=".png",
    )
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    processor = NeedsAgentProcessor()

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=processor,
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    record = next(iter(updated.values()))
    assert processor.calls == 1
    assert record.state is SourceState.NEEDS_AGENT
    assert report.handoff_source_ids == (record.source_id,)
    assert "agent_extraction_required" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_unavailable_processor_stays_pending_even_with_agent_fallback(
    repo_root: Path,
) -> None:
    registry = make_registry(
        media_type="image/png",
        extension=".png",
        extractor_id="image",
        agent_fallback=True,
    )
    item = make_item(
        path="images/a.png",
        content_sha256=CONTENT_SHA256,
        media_type="image/png",
        extension=".png",
    )
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    assert next(iter(updated.values())).state is SourceState.PENDING
    assert report.handoff_source_ids == ()


def test_new_url_descriptor_awaits_approval_without_external_work(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = make_registry()
    item = make_url_item()

    class ExplodingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> ProcessResult:
            raise AssertionError("URL descriptor must not reach the processor")

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network access is forbidden")
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("subprocess access is forbidden")
        ),
    )

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=ExplodingProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    record = next(iter(updated.values()))
    assert record.state is SourceState.AWAITING_APPROVAL
    assert record.versions == {}
    assert report.hashed_paths == ()
    assert "web_capture_awaiting_approval" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_captured_descriptor_description_edit_preserves_corpus_identity(
    repo_root: Path,
) -> None:
    registry = make_registry()
    first = make_url_item(description="Old")
    record = make_captured_url_record(first)
    edited = make_url_item(description="New description", size=44, mtime_ns=2)
    before_revision = compute_corpus_revision([record])

    updated, report = reconcile_inventory(
        InventoryReport((edited,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is SourceState.OK
    assert result.url_descriptor is not None
    assert result.url_descriptor.description == "New description"
    assert result.url_descriptor.fingerprint == edited.fingerprint
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    assert compute_corpus_revision([result]) == before_revision
    assert report.new_active_representations == ()


@pytest.mark.parametrize(
    "attempt_state",
    (SourceState.PENDING, SourceState.FAILED, SourceState.NEEDS_AGENT),
)
def test_descriptor_metadata_edit_preserves_nonterminal_attempt_state_and_old_active_ids(
    repo_root: Path, attempt_state: SourceState
) -> None:
    registry = make_registry()
    first = make_url_item(description="Old")
    base = make_captured_url_record(first)
    attempt = ProcessingAttempt(
        base.active_content_sha256 or "",
        "text",
        effective_extractor_version(registry.extractors[0], PREREQUISITE),
        CONFIG_SHA256,
        PREREQUISITE,
        attempt_state,
        FIXED_NOW,
        ("prior_attempt",),
    )
    record = replace(
        base,
        state=attempt_state,
        last_attempt=attempt,
        diagnostics=(Diagnostic("prior_attempt", "Prior attempt remains visible."),),
    )
    edited = make_url_item(description="Edited", size=44, mtime_ns=2)

    updated, _ = reconcile_inventory(
        InventoryReport((edited,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is attempt_state
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    assert result.derivations == record.derivations


def test_descriptor_url_change_retains_history_but_clears_active_ids(
    repo_root: Path,
) -> None:
    registry = make_registry()
    first = make_url_item()
    record = make_captured_url_record(first)
    changed = make_url_item(url="https://example.test/b", description="B", mtime_ns=2)

    updated, _ = reconcile_inventory(
        InventoryReport((changed,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is SourceState.AWAITING_APPROVAL
    assert result.versions == record.versions
    assert result.derivations == record.derivations
    assert result.active_content_sha256 is None
    assert result.active_derivation_id is None
    assert result.source_id == record.source_id
    assert compute_corpus_revision([result]) != compute_corpus_revision([record])


def test_missing_zero_version_url_stays_awaiting_approval_with_raw_missing(
    repo_root: Path,
) -> None:
    registry = make_registry()
    item = make_url_item()
    records, _ = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    updated, report = reconcile_inventory(
        InventoryReport((), ()),
        records,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    result = next(iter(updated.values()))
    assert result.state is SourceState.AWAITING_APPROVAL
    assert result.versions == {}
    assert "raw_missing" in {diagnostic.code for diagnostic in result.diagnostics}
    assert "raw_missing" in {diagnostic.code for diagnostic in report.coverage_gaps}


def test_unambiguous_rename_updates_paths_and_json_safe_citation_rewrite(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    item = make_item(path="notes/renamed.txt", content_sha256=CONTENT_SHA256)

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    version = result.versions[CONTENT_SHA256]
    assert result.current_raw_path == PurePosixPath("notes/renamed.txt")
    assert result.previous_raw_paths == (PurePosixPath("notes/a.txt"),)
    assert version.raw_path == PurePosixPath("notes/renamed.txt")
    assert version.fingerprint == item.fingerprint
    assert version.first_seen_at == record.versions[CONTENT_SHA256].first_seen_at
    assert [rewrite.to_dict() for rewrite in report.citation_rewrites] == [
        {
            "source_id": record.source_id,
            "content_sha256": CONTENT_SHA256,
            "raw_path": "notes/renamed.txt",
        }
    ]
    json.dumps([rewrite.to_dict() for rewrite in report.citation_rewrites])


def test_unambiguous_rename_uses_one_cached_hash_for_normal_inventory_item(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    item = make_item(path="notes/renamed.txt", content_sha256=None)
    paths = RepoPaths.discover(repo_root)
    materialize_item(paths, item, b"renamed")
    hashed: list[Path] = []

    def counted_hash(path: Path) -> str:
        hashed.append(path)
        return CONTENT_SHA256

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=counted_hash,
        now=FIXED_NOW,
    )

    assert updated[record.source_id].current_raw_path == item.fingerprint.path
    assert len(hashed) == 1
    assert report.hashed_paths == (item.fingerprint.path,)


@pytest.mark.parametrize("shape", ("one_old_two_new", "two_old_one_new"))
def test_ambiguous_rename_shapes_create_distinct_sources_without_rewrites(
    repo_root: Path, shape: str
) -> None:
    registry = make_registry()
    first_old = make_aligned_record(path="old/one.txt", registry=registry)
    records = {first_old.source_id: first_old}
    items = [make_item(path="new/one.txt", content_sha256=CONTENT_SHA256)]
    if shape == "one_old_two_new":
        items.append(make_item(path="new/two.txt", content_sha256=CONTENT_SHA256))
    else:
        second_old = make_aligned_record(path="old/two.txt", registry=registry)
        records[second_old.source_id] = second_old
    for item in items:
        materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    updated, report = reconcile_inventory(
        InventoryReport(tuple(reversed(items)), ()),
        dict(reversed(tuple(records.items()))),
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    assert report.citation_rewrites == ()
    assert all(
        updated[source_id].current_raw_path.parts[0] == "old" for source_id in records
    )
    new_records = [
        record for source_id, record in updated.items() if source_id not in records
    ]
    assert len(new_records) == len(items)
    assert {record.current_raw_path for record in new_records} == {
        item.fingerprint.path for item in items
    }
    assert "ambiguous_rename" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_new_candidate_hash_is_deduplicated_across_rename_and_processing(
    repo_root: Path,
) -> None:
    registry = make_registry()
    item = make_item(path="notes/new.txt")
    paths = RepoPaths.discover(repo_root)
    materialize_item(paths, item, b"content")
    calls: list[Path] = []

    def counted_hash(path: Path) -> str:
        calls.append(path)
        return CONTENT_SHA256

    reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=counted_hash,
        now=FIXED_NOW,
    )

    assert len(calls) == 1


def test_unsupported_missing_and_skipped_inventory_are_coverage_gaps(
    repo_root: Path,
) -> None:
    registry = make_registry()
    missing = make_aligned_record(path="missing/a.txt", registry=registry)
    unsupported = make_item(
        path="binary/a.bin",
        content_sha256="8" * 64,
        media_type="application/octet-stream",
        extension=".bin",
    )
    skipped = Diagnostic(
        "source_inventory_error",
        "Could not inspect source.",
        PurePosixPath("broken/a.txt"),
    )

    updated, report = reconcile_inventory(
        InventoryReport((unsupported,), (skipped,)),
        {missing.source_id: missing},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    by_path = {record.current_raw_path: record for record in updated.values()}
    assert by_path[PurePosixPath("binary/a.bin")].state is SourceState.UNSUPPORTED
    assert by_path[PurePosixPath("missing/a.txt")].state is SourceState.WARNING
    codes = {diagnostic.code for diagnostic in report.coverage_gaps}
    assert {"unsupported_source", "raw_missing", "source_inventory_error"} <= codes


@pytest.mark.parametrize(
    "skip_path",
    (
        None,
        PurePosixPath("obscured"),
        PurePosixPath("obscured/nested/a.txt"),
    ),
)
def test_inventory_skip_scope_does_not_falsely_mark_hidden_records_missing(
    repo_root: Path, skip_path: PurePosixPath | None
) -> None:
    registry = make_registry()
    hidden = make_aligned_record(
        path="obscured/nested/a.txt",
        registry=registry,
    )
    visible_missing = make_aligned_record(
        path="visible/a.txt",
        registry=registry,
    )
    skipped = Diagnostic(
        "source_inventory_error",
        "Could not inspect source scope.",
        skip_path,
    )

    updated, report = reconcile_inventory(
        InventoryReport((), (skipped,)),
        {hidden.source_id: hidden, visible_missing.source_id: visible_missing},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    assert updated[hidden.source_id] is hidden
    if skip_path is None:
        assert updated[visible_missing.source_id] is visible_missing
    else:
        assert updated[visible_missing.source_id].state is SourceState.WARNING
        assert "raw_missing" in {
            item.code for item in updated[visible_missing.source_id].diagnostics
        }
    assert skipped in report.coverage_gaps


def test_repeated_missing_reconciliation_does_not_duplicate_diagnostics_or_checkpoint(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    first, _ = reconcile_inventory(
        InventoryReport((), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    checkpoints: list[SourceRecord] = []

    second, report = reconcile_inventory(
        InventoryReport((), ()),
        first,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=checkpoints.append,
        now=FIXED_NOW,
    )

    result = second[record.source_id]
    assert result is first[record.source_id]
    assert [item.code for item in result.diagnostics].count("raw_missing") == 1
    assert checkpoints == []
    assert [item.code for item in report.coverage_gaps].count("raw_missing") == 1


def test_record_backed_coverage_occurrences_are_scoped_per_source(
    repo_root: Path,
) -> None:
    registry = make_registry()
    paths = RepoPaths.discover(repo_root)
    diagnostic = Diagnostic("pending", "Pending.")
    attempt = ProcessingAttempt(
        CONTENT_SHA256,
        registry.extractors[0].extractor_id,
        effective_extractor_version(registry.extractors[0], PREREQUISITE),
        registry.extractors[0].config_sha256,
        PREREQUISITE,
        SourceState.PENDING,
        FIXED_NOW,
        (diagnostic.code,),
    )
    first = replace(
        make_aligned_record(path="notes/a.txt", registry=registry),
        state=SourceState.PENDING,
        last_attempt=attempt,
        diagnostics=(diagnostic, diagnostic),
    )
    second = replace(
        make_aligned_record(path="notes/b.txt", registry=registry),
        state=SourceState.PENDING,
        last_attempt=attempt,
        diagnostics=(diagnostic,),
    )
    items = (
        make_item(path="notes/a.txt", content_sha256=CONTENT_SHA256),
        make_item(path="notes/b.txt", content_sha256=CONTENT_SHA256),
    )
    for item in items:
        materialize_item(paths, item, CONTENT_BYTES)
    private_events: list[dict[str, object]] = []
    result_store = SyncResultStore(paths)

    with result_store.writer(
        "sync",
        FIXED_NOW,
        recoverable=True,
    ) as writer:

        def capture(kind: str, data: object) -> None:
            if kind == "coverage_gap":
                private_events.append(dict(data))  # type: ignore[arg-type]
            writer.emit(kind, data)  # type: ignore[arg-type]

        _updated, report = reconcile_inventory(
            InventoryReport(items, ()),
            {first.source_id: first, second.source_id: second},
            registry=registry,
            processor=UnavailableProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            event_sink=capture,
            now=FIXED_NOW,
        )
        reference = writer.finalize(
            report.corpus_revision,
            event_filter=lambda _event: True,
        )

    assert sorted(
        (event["source_id"], event["occurrence"]) for event in private_events
    ) == sorted(
        (
            (first.source_id, 0),
            (first.source_id, 1),
            (second.source_id, 0),
        )
    )
    public_gaps = [
        event.data
        for event in result_store.iter_events(reference)
        if event.kind == "coverage_gap"
    ]
    assert (
        public_gaps
        == [
            {
                "code": "pending",
                "details": {},
                "message": "Pending.",
                "path": None,
            }
        ]
        * 3
    )


def test_copy_with_original_present_creates_a_distinct_source_not_a_rename(
    repo_root: Path,
) -> None:
    registry = make_registry()
    original = make_aligned_record(registry=registry)
    items = (
        make_item(path="notes/a.txt"),
        make_item(path="notes/copy.txt", content_sha256=CONTENT_SHA256),
    )
    materialize_item(RepoPaths.discover(repo_root), items[1], CONTENT_BYTES)

    updated, report = reconcile_inventory(
        InventoryReport(items, ()),
        {original.source_id: original},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    assert updated[original.source_id] is original
    assert len(updated) == 2
    assert report.citation_rewrites == ()
    assert {record.current_raw_path for record in updated.values()} == {
        PurePosixPath("notes/a.txt"),
        PurePosixPath("notes/copy.txt"),
    }


def test_generated_source_id_collision_fails_before_overwrite_or_checkpoint(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = make_registry()
    collision_id = source_id_for_first_seen(PurePosixPath("existing/a.txt"), "6" * 64)
    existing = make_aligned_record(
        source_id=collision_id,
        path="existing/a.txt",
        checksum="6" * 64,
        registry=registry,
    )
    candidate = make_item(path="new/a.txt", content_sha256="7" * 64)
    checkpoints: list[SourceRecord] = []
    monkeypatch.setattr(
        sync_module,
        "source_id_for_first_seen",
        lambda *_args, **_kwargs: collision_id,
    )

    with pytest.raises(ValueError, match="source_id collision"):
        reconcile_inventory(
            InventoryReport(
                (candidate, make_item(path="existing/a.txt", content_sha256="6" * 64)),
                (),
            ),
            {collision_id: existing},
            registry=registry,
            processor=UnavailableProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=checkpoints.append,
            now=FIXED_NOW,
        )

    assert checkpoints == []


def test_checkpoint_persists_extracting_before_processor_work_and_final_outcome(
    repo_root: Path,
) -> None:
    registry = make_registry(agent_fallback=True)
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    events: list[str] = []
    processor = NeedsAgentProcessor(events)

    def checkpoint(record: SourceRecord) -> None:
        events.append(f"checkpoint:{record.state.value}")

    reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=processor,
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=checkpoint,
        now=FIXED_NOW,
    )

    assert events == [
        "checkpoint:extracting",
        "process",
        "checkpoint:needs_agent",
    ]


@pytest.mark.parametrize(
    "replacement_mode",
    ("regular_entry", "alias_target", "ancestor_directory"),
)
def test_processor_input_is_pinned_across_extracting_checkpoint_replacement(
    repo_root: Path,
    replacement_mode: str,
) -> None:
    paths = RepoPaths.discover(repo_root)
    logical_source = paths.raw / "notes/a.txt"
    if replacement_mode == "alias_target":
        source = write_bytes(paths.raw / "shared/target.txt", b"original")
        logical_source.parent.mkdir(parents=True, exist_ok=True)
        logical_source.symlink_to("../shared/target.txt")
    else:
        source = write_bytes(logical_source, b"original")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = next(
        candidate
        for candidate in inventory.items
        if candidate.fingerprint.path == PurePosixPath("notes/a.txt")
    )
    replacement_path = source.with_name("replacement.tmp")
    observed: list[bytes] = []
    checkpoint_states: list[SourceState] = []

    class ReadingProcessor(UnavailableProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            input_path = getattr(
                context,
                "input_path",
                paths.raw / item.fingerprint.path,
            )
            observed.append(input_path.read_bytes())
            return super().process(
                record,
                item,
                extractor,
                paths=paths,
                context=context,
            )

    def replace_at_extracting(record: SourceRecord) -> None:
        checkpoint_states.append(record.state)
        if record.state is not SourceState.EXTRACTING:
            return
        if replacement_mode == "ancestor_directory":
            moved_parent = paths.raw / "notes-before-replacement"
            os.replace(source.parent, moved_parent)
            replacement_file = write_bytes(logical_source, b"replaced")
            os.utime(
                replacement_file,
                ns=(item.fingerprint.mtime_ns, item.fingerprint.mtime_ns),
            )
        else:
            write_bytes(replacement_path, b"replaced")
            os.utime(
                replacement_path,
                ns=(item.fingerprint.mtime_ns, item.fingerprint.mtime_ns),
            )
            os.replace(replacement_path, source)

    registry = make_registry()
    with pytest.raises(inventory_module.InventoryAccessError):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=ReadingProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=replace_at_extracting,
            now=FIXED_NOW,
        )

    assert observed == [b"original"]
    assert checkpoint_states == [SourceState.EXTRACTING]


def test_checkpoint_interruption_prevents_processor_work(repo_root: Path) -> None:
    registry = make_registry(agent_fallback=True)
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    processor = NeedsAgentProcessor()
    checkpointed: list[SourceRecord] = []

    def interrupt(record: SourceRecord) -> None:
        checkpointed.append(record)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=processor,
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=interrupt,
            now=FIXED_NOW,
        )

    assert [record.state for record in checkpointed] == [SourceState.EXTRACTING]
    assert processor.calls == 0


def test_checkpoint_interruption_leaves_durable_extracting_record(
    repo_root: Path,
) -> None:
    from brainlib.ledger import LedgerStore

    registry = make_registry(agent_fallback=True)
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)
    processor = NeedsAgentProcessor()
    store = LedgerStore(RepoPaths.discover(repo_root))

    def save_then_interrupt(record: SourceRecord) -> None:
        store.save(record)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=processor,
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=save_then_interrupt,
            now=FIXED_NOW,
        )

    durable = store.load_all()
    assert len(durable) == 1
    assert next(iter(durable.values())).state is SourceState.EXTRACTING
    assert processor.calls == 0


def test_final_checkpoint_is_durable_before_next_source_starts(repo_root: Path) -> None:
    from brainlib.ledger import LedgerStore

    registry = make_registry()
    first_bytes = b"aaaaaaa"
    second_bytes = b"bbbbbbb"
    items = (
        make_item(path="a.txt", content_sha256=hashlib.sha256(first_bytes).hexdigest()),
        make_item(
            path="b.txt", content_sha256=hashlib.sha256(second_bytes).hexdigest()
        ),
    )
    materialize_item(RepoPaths.discover(repo_root), items[0], first_bytes)
    materialize_item(RepoPaths.discover(repo_root), items[1], second_bytes)
    store = LedgerStore(RepoPaths.discover(repo_root))

    def save_then_interrupt_on_final(record: SourceRecord) -> None:
        store.save(record)
        if (
            record.current_raw_path == PurePosixPath("a.txt")
            and record.state is SourceState.PENDING
        ):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        reconcile_inventory(
            InventoryReport(tuple(reversed(items)), ()),
            {},
            registry=registry,
            processor=UnavailableProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=save_then_interrupt_on_final,
            now=FIXED_NOW,
        )

    durable = store.load_all()
    assert len(durable) == 1
    assert next(iter(durable.values())).current_raw_path == PurePosixPath("a.txt")
    assert next(iter(durable.values())).state is SourceState.PENDING


def test_stale_extracting_record_is_checkpointed_as_recovered_before_retry(
    repo_root: Path,
) -> None:
    registry = make_registry()
    stale = replace(
        make_aligned_record(registry=registry), state=SourceState.EXTRACTING
    )
    materialize_item(
        RepoPaths.discover(repo_root),
        make_item(content_sha256=CONTENT_SHA256),
        CONTENT_BYTES,
    )
    states: list[SourceState] = []

    updated, _ = reconcile_inventory(
        InventoryReport((make_item(content_sha256=CONTENT_SHA256),), ()),
        {stale.source_id: stale},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=lambda record: states.append(record.state),
        now=FIXED_NOW,
    )

    assert states == [SourceState.PENDING, SourceState.EXTRACTING, SourceState.PENDING]
    assert "stale_extracting_recovered" in {
        diagnostic.code for diagnostic in updated[stale.source_id].diagnostics
    }


def test_missing_stale_extracting_record_recovers_before_marking_missing(
    repo_root: Path,
) -> None:
    registry = make_registry()
    stale = replace(
        make_aligned_record(registry=registry), state=SourceState.EXTRACTING
    )
    states: list[SourceState] = []

    updated, _ = reconcile_inventory(
        InventoryReport((), ()),
        {stale.source_id: stale},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=lambda record: states.append(record.state),
        now=FIXED_NOW,
    )

    assert states == [SourceState.PENDING, SourceState.WARNING]
    assert "stale_extracting_recovered" in {
        item.code for item in updated[stale.source_id].diagnostics
    }
    assert "raw_missing" in {item.code for item in updated[stale.source_id].diagnostics}


def test_report_counts_mapping_is_immutable(repo_root: Path) -> None:
    registry = make_registry()
    materialize_item(
        RepoPaths.discover(repo_root),
        make_item(content_sha256=CONTENT_SHA256),
        CONTENT_BYTES,
    )
    _, report = reconcile_inventory(
        InventoryReport((make_item(content_sha256=CONTENT_SHA256),), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    with pytest.raises(TypeError):
        report.decision_counts[SyncAction.CREATE] = 99  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        report.hashed_paths = ()  # type: ignore[misc]


def test_reversed_inputs_produce_identical_sorted_outputs(repo_root: Path) -> None:
    registry = make_registry()
    missing = make_aligned_record(
        path="missing/z.txt", checksum="2" * 64, registry=registry
    )
    retained = make_aligned_record(
        path="retained/a.txt", checksum=CONTENT_SHA256, registry=registry
    )
    records = {missing.source_id: missing, retained.source_id: retained}
    items = (
        make_item(path="new/z.txt", content_sha256=CONTENT_SHA256),
        make_item(path="retained/a.txt", content_sha256=CONTENT_SHA256),
    )
    materialize_item(RepoPaths.discover(repo_root), items[0], CONTENT_BYTES)

    first = reconcile_inventory(
        InventoryReport(items, ()),
        records,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    second = reconcile_inventory(
        InventoryReport(tuple(reversed(items)), ()),
        dict(reversed(tuple(records.items()))),
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    assert tuple(first[0]) == tuple(second[0])
    assert first[0] == second[0]
    assert first[1] == second[1]
    assert first[1].citation_rewrites == tuple(
        sorted(
            first[1].citation_rewrites,
            key=lambda item: (
                item.source_id,
                item.content_sha256,
                item.raw_path.as_posix(),
            ),
        )
    )


@pytest.mark.parametrize(
    "mismatch",
    (
        "input",
        "extractor",
        "version",
        "config",
        "prerequisite",
        "outcome",
        "attempted_at",
        "diagnostics",
    ),
)
def test_processor_result_mismatch_is_rejected(repo_root: Path, mismatch: str) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    class MismatchedProcessor:
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
            values: dict[str, object] = {
                "input_sha256": context.input_sha256,
                "extractor_id": extractor.extractor_id,
                "extractor_version": context.extractor_version,
                "config_sha256": context.config_sha256,
                "prerequisite_digest": context.prerequisite_digest,
                "outcome": SourceState.PENDING,
                "attempted_at": context.attempted_at,
                "diagnostic_codes": ("pending",),
            }
            replacements: dict[str, object] = {
                "input": "1" * 64,
                "extractor": "other",
                "version": "other",
                "config": "2" * 64,
                "prerequisite": "3" * 64,
                "outcome": SourceState.FAILED,
                "attempted_at": FIXED_NOW.replace(year=2025),
                "diagnostics": ("other",),
            }
            keys = {
                "input": "input_sha256",
                "extractor": "extractor_id",
                "version": "extractor_version",
                "config": "config_sha256",
                "prerequisite": "prerequisite_digest",
                "outcome": "outcome",
                "attempted_at": "attempted_at",
                "diagnostics": "diagnostic_codes",
            }
            values[keys[mismatch]] = replacements[mismatch]
            attempt = ProcessingAttempt(**values)  # type: ignore[arg-type]
            return ProcessResult(
                SourceState.PENDING,
                None,
                attempt,
                (Diagnostic("pending", "Still pending."),),
            )

    with pytest.raises(ValueError, match="processor result"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=MismatchedProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


def test_needs_agent_result_is_rejected_without_configured_fallback(
    repo_root: Path,
) -> None:
    registry = make_registry(agent_fallback=False)
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    with pytest.raises(ValueError, match="processor result"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=NeedsAgentProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


@pytest.mark.parametrize(
    "mismatch",
    ("source", "extractor", "version", "config", "identifier", "quality"),
)
def test_processor_derivation_identity_mismatch_is_rejected(
    repo_root: Path, mismatch: str
) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    class BadDerivationProcessor(WarningProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            result = super().process(
                record, item, extractor, paths=paths, context=context
            )
            assert result.derivation is not None
            changes: dict[str, object] = {
                "source": {"source_sha256": "1" * 64},
                "extractor": {"extractor_id": "other"},
                "version": {"extractor_version": "other"},
                "config": {"config_sha256": "2" * 64},
                "identifier": {"derivation_id": "drv_" + "3" * 64},
                "quality": {"quality_state": "ok"},
            }[mismatch]
            return replace(result, derivation=replace(result.derivation, **changes))

    with pytest.raises(ValueError, match="processor result"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=BadDerivationProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


def test_new_warning_derivation_is_searchable_and_reported_fresh(
    repo_root: Path,
) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=WarningProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    record = next(iter(updated.values()))
    assert record.state is SourceState.WARNING
    assert report.new_active_representations[0].quality_state == "warning"
    assert (
        report.new_active_representations[0].derivation_id
        == record.active_derivation_id
    )
    assert "partial_text" in {item.code for item in report.coverage_gaps}


def test_failed_processing_retains_an_older_valid_active_representation(
    repo_root: Path,
) -> None:
    registry = make_registry()
    record = make_aligned_record(registry=registry)
    item = make_item()
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    class FailedProcessor:
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
            diagnostic = Diagnostic("conversion_failed", "Conversion failed.")
            return ProcessResult(
                SourceState.FAILED,
                None,
                ProcessingAttempt(
                    context.input_sha256,
                    extractor.extractor_id,
                    context.extractor_version,
                    context.config_sha256,
                    context.prerequisite_digest,
                    SourceState.FAILED,
                    context.attempted_at,
                    (diagnostic.code,),
                ),
                (diagnostic,),
            )

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {record.source_id: record},
        registry=registry,
        processor=FailedProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry, CHANGED_PREREQUISITE),
        hash_file=never_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is SourceState.FAILED
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    assert result.derivations == record.derivations
    assert report.new_active_representations == ()


@pytest.mark.parametrize(
    ("path", "byte_size", "mtime_ns"),
    (
        (PurePosixPath("/etc/passwd"), 7, 1),
        (PurePosixPath("notes/../outside.txt"), 7, 1),
        (PurePosixPath("notes/a.txt"), -1, 1),
        (PurePosixPath("notes/a.txt"), 7, True),
    ),
)
def test_invalid_inventory_item_is_rejected_before_callbacks_or_io(
    repo_root: Path,
    path: PurePosixPath,
    byte_size: int,
    mtime_ns: int,
) -> None:
    registry = make_registry()
    item = InventoryItem(
        FileFingerprint(path, byte_size, mtime_ns),
        "text/plain",
        ".txt",
        None,
    )
    events: list[str] = []

    class ExplodingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> ProcessResult:
            events.append("process")
            raise AssertionError("invalid inventory must not be processed")

    with pytest.raises(ValueError, match="inventory"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=ExplodingProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            hash_file=lambda _path: events.append("hash") or CONTENT_SHA256,
            checkpoint=lambda _record: events.append("checkpoint"),
            now=FIXED_NOW,
        )

    assert events == []


@pytest.mark.parametrize("replacement", ("regular", "escaping_symlink"))
def test_candidate_changed_after_inventory_fails_closed_before_hashing(
    repo_root: Path, replacement: str
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", b"original")
    inventory = inventory_raw_sources(paths, MediaDetector())
    assert len(inventory.items) == 1
    source.unlink()
    if replacement == "regular":
        write_bytes(source, b"replaced")
    else:
        outside = write_bytes(repo_root / "outside-secret.txt", b"outside!")
        source.symlink_to(outside)

    events: list[str] = []

    class ExplodingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> ProcessResult:
            events.append("process")
            raise AssertionError("changed inventory must not be processed")

    registry = make_registry()
    updated, report = reconcile_inventory(
        inventory,
        {},
        registry=registry,
        processor=ExplodingProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=lambda path: events.append(f"hash:{path}") or compute_sha256(path),
        checkpoint=lambda _record: events.append("checkpoint"),
        now=FIXED_NOW,
    )

    assert updated == {}
    assert events == []
    assert report.hashed_paths == ()
    assert "source_changed_during_sync" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_candidate_changed_during_hash_is_not_adopted_or_checkpointed(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", b"original")
    inventory = inventory_raw_sources(paths, MediaDetector())
    events: list[str] = []

    def swapping_hash(path: Path) -> str:
        events.append("hash")
        checksum = compute_sha256(path)
        source.unlink()
        write_bytes(source, b"changed!")
        return checksum

    class ExplodingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> ProcessResult:
            events.append("process")
            raise AssertionError("changed inventory must not be processed")

    registry = make_registry()
    updated, report = reconcile_inventory(
        inventory,
        {},
        registry=registry,
        processor=ExplodingProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=swapping_hash,
        checkpoint=lambda _record: events.append("checkpoint"),
        now=FIXED_NOW,
    )

    assert updated == {}
    assert events == ["hash"]
    assert report.hashed_paths == ()
    assert "source_changed_during_sync" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_unverified_candidate_does_not_checkpoint_stale_extraction_recovery(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", b"original")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = inventory.items[0]
    checksum = hashlib.sha256(b"original").hexdigest()
    registry = make_registry()
    record = make_aligned_record(
        checksum=checksum,
        size=item.fingerprint.byte_size,
        mtime_ns=max(0, item.fingerprint.mtime_ns - 1),
        registry=registry,
    )
    record = replace(
        record,
        state=SourceState.EXTRACTING,
        last_attempt=ProcessingAttempt(
            checksum,
            registry.extractors[0].extractor_id,
            effective_extractor_version(registry.extractors[0], PREREQUISITE),
            registry.extractors[0].config_sha256,
            PREREQUISITE,
            SourceState.EXTRACTING,
            FIXED_NOW,
            (),
        ),
    )
    source.unlink()
    write_bytes(source, b"replaced")
    checkpoints: list[SourceRecord] = []

    updated, report = reconcile_inventory(
        inventory,
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=checkpoints.append,
        now=FIXED_NOW,
    )

    assert updated[record.source_id] is record
    assert checkpoints == []
    assert "source_changed_during_sync" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_safe_in_tree_file_alias_is_hashed_through_its_pinned_target(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    content = b"shared content"
    write_bytes(paths.raw / "shared/target.txt", content)
    alias = paths.raw / "notes/alias.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to("../shared/target.txt")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = next(
        item
        for item in inventory.items
        if item.fingerprint.path == PurePosixPath("notes/alias.txt")
    )
    registry = make_registry()

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    record = next(iter(updated.values()))
    assert record.active_content_sha256 == hashlib.sha256(content).hexdigest()
    assert report.hashed_paths == (PurePosixPath("notes/alias.txt"),)


def test_alias_target_entry_swap_during_hash_fails_closed_even_if_pinned_stat_is_stable(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RepoPaths.discover(repo_root)
    target = write_bytes(paths.raw / "shared/target.txt", b"payload")
    alias = paths.raw / "notes/alias.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to("../shared/target.txt")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = next(
        candidate
        for candidate in inventory.items
        if candidate.fingerprint.path == PurePosixPath("notes/alias.txt")
    )
    real_fstat = os.fstat
    pinned: dict[str, object] = {"swapped": False}

    def simulated_stable_fstat(descriptor: int) -> os.stat_result:
        if pinned["swapped"] and descriptor == pinned["descriptor"]:
            observed = pinned["observed"]
            assert isinstance(observed, os.stat_result)
            return observed
        return real_fstat(descriptor)

    monkeypatch.setattr(inventory_module.os, "fstat", simulated_stable_fstat)
    events: list[str] = []

    def swapping_hash(path: Path) -> str:
        events.append("hash")
        descriptor = int(path.name)
        checksum = compute_sha256(path)
        pinned["descriptor"] = descriptor
        pinned["observed"] = real_fstat(descriptor)
        replacement = write_bytes(paths.raw / "shared/replacement.tmp", b"changed")
        os.utime(
            replacement,
            ns=(item.fingerprint.mtime_ns, item.fingerprint.mtime_ns),
        )
        os.replace(replacement, target)
        pinned["swapped"] = True
        return checksum

    class RecordingProcessor(UnavailableProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            events.append("process")
            return super().process(
                record,
                item,
                extractor,
                paths=paths,
                context=context,
            )

    registry = make_registry()
    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        {},
        registry=registry,
        processor=RecordingProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=swapping_hash,
        checkpoint=lambda _record: events.append("checkpoint"),
        now=FIXED_NOW,
    )

    assert target.read_bytes() == b"changed"
    assert updated == {}
    assert events == ["hash"]
    assert report.hashed_paths == ()
    assert "source_changed_during_sync" in {
        diagnostic.code for diagnostic in report.coverage_gaps
    }


def test_alias_hash_baseexception_releases_every_owned_descriptor(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "shared/target.txt", b"payload")
    alias = paths.raw / "notes/alias.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to("../shared/target.txt")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = next(
        candidate
        for candidate in inventory.items
        if candidate.fingerprint.path == PurePosixPath("notes/alias.txt")
    )
    before = set(os.listdir("/dev/fd"))

    def interrupt_hash(_path: Path) -> str:
        raise KeyboardInterrupt

    for _ in range(10):
        with pytest.raises(KeyboardInterrupt):
            inventory_module.hash_inventory_item(
                paths,
                item,
                hash_file=interrupt_hash,
            )

    assert set(os.listdir("/dev/fd")) == before


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (OSError, inventory_module.InventoryAccessError),
        (KeyboardInterrupt, KeyboardInterrupt),
    ),
)
def test_alias_final_parent_duplication_failure_releases_pinned_content(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
    expected: type[BaseException],
) -> None:
    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "shared/target.txt", b"payload")
    alias = paths.raw / "notes/alias.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to("../shared/target.txt")
    inventory = inventory_raw_sources(paths, MediaDetector())
    item = next(
        candidate
        for candidate in inventory.items
        if candidate.fingerprint.path == PurePosixPath("notes/alias.txt")
    )
    real_open = os.open
    real_dup = os.dup
    allocation = {"content_open": False, "failed": False}

    def tracking_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is None:
            descriptor = real_open(path, flags, mode)
        else:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "target.txt":
            allocation["content_open"] = True
        return descriptor

    def failing_final_parent_dup(descriptor: int) -> int:
        if allocation["content_open"] and not allocation["failed"]:
            allocation["failed"] = True
            raise failure("injected final-parent duplication failure")
        return real_dup(descriptor)

    monkeypatch.setattr(inventory_module.os, "open", tracking_open)
    monkeypatch.setattr(inventory_module.os, "dup", failing_final_parent_dup)
    before = set(os.listdir("/dev/fd"))

    for _ in range(10):
        allocation.update(content_open=False, failed=False)
        with pytest.raises(expected):
            inventory_module.hash_inventory_item(paths, item)
        assert allocation == {"content_open": True, "failed": True}

    assert set(os.listdir("/dev/fd")) == before


def _missing_cycle_record(
    paths: RepoPaths,
    *,
    registry: ExtractorRegistry,
    content: bytes,
    state: SourceState = SourceState.OK,
    with_derivation: bool = True,
) -> tuple[SourceRecord, InventoryItem]:
    raw_path = PurePosixPath("notes/a.txt")
    raw = write_bytes(paths.raw / raw_path, content)
    observed = raw.stat()
    checksum = hashlib.sha256(content).hexdigest()
    record = make_aligned_record(
        checksum=checksum,
        size=len(content),
        mtime_ns=observed.st_mtime_ns,
        registry=registry,
    )
    if not with_derivation:
        record = replace(record, derivations={}, active_derivation_id=None)
    if state in {SourceState.PENDING, SourceState.FAILED, SourceState.NEEDS_AGENT}:
        diagnostic = Diagnostic(
            f"prior_{state.value}",
            f"Prior {state.value} attempt remains visible.",
        )
        record = replace(
            record,
            state=state,
            last_attempt=ProcessingAttempt(
                checksum,
                registry.extractors[0].extractor_id,
                effective_extractor_version(registry.extractors[0], PREREQUISITE),
                registry.extractors[0].config_sha256,
                PREREQUISITE,
                state,
                FIXED_NOW,
                (diagnostic.code,),
            ),
            diagnostics=(diagnostic,),
        )
    item = make_item(
        path=raw_path.as_posix(),
        size=len(content),
        mtime_ns=observed.st_mtime_ns,
    )
    raw.unlink()
    return record, item


@pytest.mark.parametrize("restored", (b"original", b"changed!"))
def test_raw_missing_restoration_rehashes_once_before_clearing_presence_gap(
    repo_root: Path, restored: bytes
) -> None:
    paths = RepoPaths.discover(repo_root)
    registry = make_registry()
    record, item = _missing_cycle_record(
        paths,
        registry=registry,
        content=b"original",
    )
    missing, _ = reconcile_inventory(
        InventoryReport((), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    materialize_item(paths, item, restored)
    calls: list[Path] = []

    def counted_hash(path: Path) -> str:
        calls.append(path)
        return compute_sha256(path)

    updated, report = reconcile_inventory(
        InventoryReport((item,), ()),
        missing,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=counted_hash,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert len(calls) == 1
    assert report.hashed_paths == (item.fingerprint.path,)
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    if restored == b"original":
        assert result.state is SourceState.OK
        assert "raw_missing" not in {item.code for item in result.diagnostics}
    else:
        assert result.state is SourceState.INTEGRITY_ERROR
        assert "raw_missing" in {item.code for item in result.diagnostics}
        assert "raw_checksum_mismatch" in {item.code for item in result.diagnostics}


@pytest.mark.parametrize(
    "attempt_state",
    (SourceState.PENDING, SourceState.FAILED, SourceState.NEEDS_AGENT),
)
@pytest.mark.parametrize("with_derivation", (False, True))
def test_missing_restoration_preserves_aligned_attempt_outcome_and_active_ids(
    repo_root: Path,
    attempt_state: SourceState,
    with_derivation: bool,
) -> None:
    paths = RepoPaths.discover(repo_root)
    registry = make_registry(agent_fallback=attempt_state is SourceState.NEEDS_AGENT)
    record, item = _missing_cycle_record(
        paths,
        registry=registry,
        content=b"original",
        state=attempt_state,
        with_derivation=with_derivation,
    )
    missing, _ = reconcile_inventory(
        InventoryReport((), ()),
        {record.source_id: record},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    materialize_item(paths, item, b"original")

    class ExplodingProcessor:
        def process(self, *_args: object, **_kwargs: object) -> ProcessResult:
            raise AssertionError("aligned durable attempt must not be retried")

    updated, _ = reconcile_inventory(
        InventoryReport((item,), ()),
        missing,
        registry=registry,
        processor=ExplodingProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        hash_file=compute_sha256,
        now=FIXED_NOW,
    )

    result = updated[record.source_id]
    assert result.state is attempt_state
    assert result.last_attempt == record.last_attempt
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    assert result.derivations == record.derivations
    assert "raw_missing" not in {item.code for item in result.diagnostics}
    assert f"prior_{attempt_state.value}" in {item.code for item in result.diagnostics}


@pytest.mark.parametrize(
    "mismatch",
    ("unrelated", "raw_path", "suffix", "converter_id", "converter_version"),
)
def test_processor_derivation_artifact_identity_mismatch_is_rejected(
    repo_root: Path, mismatch: str
) -> None:
    registry = make_registry()
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    class BadArtifactProcessor(WarningProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            result = super().process(
                record, item, extractor, paths=paths, context=context
            )
            assert result.derivation is not None
            changes: dict[str, object]
            if mismatch == "unrelated":
                changes = {
                    "output_path": PurePosixPath(
                        "sources/extracted/unrelated/other.txt",
                        context.input_sha256,
                        result.derivation.derivation_id + extractor.output_suffix,
                    )
                }
            elif mismatch == "raw_path":
                changes = {
                    "output_path": PurePosixPath(
                        "sources/extracted/notes/b.txt",
                        context.input_sha256,
                        result.derivation.derivation_id + extractor.output_suffix,
                    )
                }
            elif mismatch == "suffix":
                changes = {
                    "output_path": result.derivation.output_path.with_suffix(".txt")
                }
            elif mismatch == "converter_id":
                changes = {
                    "method_metadata": {
                        "converter_id": "unapproved.converter",
                        "converter_version": "fixture-1",
                    }
                }
            else:
                changes = {
                    "method_metadata": {
                        "converter_id": "fixture.converter",
                        "converter_version": "fixture-2",
                    }
                }
            return replace(result, derivation=replace(result.derivation, **changes))

    with pytest.raises(ValueError, match="processor result"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=BadArtifactProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


def test_processor_success_without_a_canonical_artifact_is_rejected(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", b"content")
    inventory = inventory_raw_sources(paths, MediaDetector())
    assert source.is_file()
    registry = make_registry()

    class MissingArtifactProcessor(WarningProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            result = super().process(
                record,
                item,
                extractor,
                paths=paths,
                context=context,
            )
            assert result.derivation is not None
            (paths.root / result.derivation.output_path).unlink()
            return result

    with pytest.raises(ValueError, match="artifact"):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=MissingArtifactProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


@pytest.mark.parametrize("mismatch", ("checksum", "byte_size", "mtime", "swap"))
def test_processor_misstated_or_swapped_artifact_is_rejected(
    repo_root: Path,
    mismatch: str,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    inventory = inventory_raw_sources(paths, MediaDetector())
    assert source.is_file()
    registry = make_registry()

    class MisstatedArtifactProcessor(WarningProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            result = super().process(
                record,
                item,
                extractor,
                paths=paths,
                context=context,
            )
            assert result.derivation is not None
            derivation = result.derivation
            if mismatch == "checksum":
                return replace(
                    result,
                    derivation=replace(derivation, output_sha256="0" * 64),
                )
            if mismatch == "byte_size":
                return replace(
                    result,
                    derivation=replace(
                        derivation,
                        output_byte_size=derivation.output_byte_size + 1,
                    ),
                )
            if mismatch == "swap":
                output = paths.root / derivation.output_path
                replacement = output.with_name("replacement.tmp")
                write_bytes(replacement, b"y")
                os.utime(
                    replacement,
                    ns=(derivation.output_mtime_ns, derivation.output_mtime_ns),
                )
                os.replace(replacement, output)
                return result
            return replace(
                result,
                derivation=replace(
                    derivation,
                    output_mtime_ns=derivation.output_mtime_ns + 1,
                ),
            )

    with pytest.raises(ValueError, match="artifact metadata"):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=MisstatedArtifactProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


def test_output_replacement_during_final_checkpoint_cannot_leave_stale_active_representation(
    repo_root: Path,
) -> None:
    from brainlib.ledger import LedgerStore

    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    inventory = inventory_raw_sources(paths, MediaDetector())
    assert source.is_file()
    registry = make_registry()
    store = LedgerStore(paths)

    def swap_output_then_save(record: SourceRecord) -> None:
        if record.state is SourceState.WARNING:
            assert record.active_derivation_id is not None
            derivation = record.derivations[record.active_derivation_id]
            output = paths.root / derivation.output_path
            replacement = output.with_name("equal-metadata-replacement.tmp")
            write_bytes(replacement, b"y")
            os.utime(
                replacement,
                ns=(derivation.output_mtime_ns, derivation.output_mtime_ns),
            )
            os.replace(replacement, output)
        store.save(record)

    with pytest.raises(inventory_module.InventoryAccessError):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=swap_output_then_save,
            now=FIXED_NOW,
        )

    durable = next(iter(store.load_all().values()))
    assert durable.state is SourceState.EXTRACTING
    assert durable.active_derivation_id is None


def test_output_replacement_rollback_checkpoint_failure_still_fails_closed(
    repo_root: Path,
) -> None:
    from brainlib.ledger import LedgerStore

    paths = RepoPaths.discover(repo_root)
    source = write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    inventory = inventory_raw_sources(paths, MediaDetector())
    assert source.is_file()
    registry = make_registry()
    store = LedgerStore(paths)
    extracting_checkpoints = 0
    rollback_failed = False

    def swap_output_and_fail_first_rollback(record: SourceRecord) -> None:
        nonlocal extracting_checkpoints, rollback_failed
        if record.state is SourceState.EXTRACTING:
            extracting_checkpoints += 1
            if extracting_checkpoints == 2 and not rollback_failed:
                rollback_failed = True
                raise OSError("injected rollback checkpoint failure")
        elif record.state is SourceState.WARNING:
            assert record.active_derivation_id is not None
            derivation = record.derivations[record.active_derivation_id]
            output = paths.root / derivation.output_path
            replacement = output.with_name("equal-metadata-replacement.tmp")
            write_bytes(replacement, b"y")
            os.utime(
                replacement,
                ns=(derivation.output_mtime_ns, derivation.output_mtime_ns),
            )
            os.replace(replacement, output)
        store.save(record)

    with pytest.raises(inventory_module.InventoryAccessError):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=swap_output_and_fail_first_rollback,
            now=FIXED_NOW,
        )

    durable = next(iter(store.load_all().values()))
    assert rollback_failed
    assert durable.state is SourceState.EXTRACTING
    assert durable.active_derivation_id is None

    retried, _report = reconcile_inventory(
        inventory,
        store.load_all(),
        registry=registry,
        processor=WarningProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=store.save,
        now=FIXED_NOW,
    )
    current = next(iter(retried.values()))
    assert current.state is SourceState.WARNING
    assert current.active_derivation_id is not None
    derivation = current.derivations[current.active_derivation_id]
    assert (
        compute_sha256(paths.root / derivation.output_path) == derivation.output_sha256
    )


@pytest.mark.parametrize("skip", ("rollback", "candidate"))
def test_sync_guard_cannot_clear_without_expected_checkpoint_bytes(
    repo_paths: RepoPaths, skip: str
) -> None:
    from brainlib.ledger import LedgerStore

    paths = repo_paths
    write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    inventory = inventory_raw_sources(paths, MediaDetector())
    registry = make_registry()
    store = LedgerStore(paths)
    candidate_seen = False
    candidate_bytes = None

    def silently_skip_expected_checkpoint(record: SourceRecord) -> None:
        nonlocal candidate_seen, candidate_bytes
        if record.active_derivation_id:
            candidate_seen = True
            if skip == "candidate":
                return
            shard = store.save(record)
            candidate_bytes = shard.read_bytes()
            output = (
                paths.root / record.derivations[record.active_derivation_id].output_path
            )
            output.write_text("mutated after active checkpoint")
        elif not candidate_seen:
            store.save(record)

    with pytest.raises((OSError, ValueError)):
        reconcile_inventory(
            inventory,
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=silently_skip_expected_checkpoint,
            now=FIXED_NOW,
        )
    assert candidate_seen
    guards = tuple(paths.ledger_dir.glob("*.activation-pending"))
    assert len(guards) == 1
    shard = next(paths.ledger_dir.glob("src_*.json"))
    if skip == "rollback":
        assert shard.read_bytes() == candidate_bytes
    else:
        assert json.loads(shard.read_bytes())["active_derivation_id"] is None
    with pytest.raises(ValueError, match="activation"):
        store.active_representations()


@pytest.mark.parametrize("already_active", (False, True))
@pytest.mark.parametrize("after_write", (False, True))
@pytest.mark.parametrize("command", ("sync", "init"))
def test_repeated_output_rollback_failures_keep_durable_recovery_authority(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_active: bool,
    after_write: bool,
    command: str,
) -> None:
    import brainlib.commands as commands
    from brainlib.ledger import LedgerStore

    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    inventory = inventory_raw_sources(paths, MediaDetector())
    registry = make_registry()
    store = LedgerStore(paths)
    if already_active:
        previous_registry = replace(
            registry,
            extractors=(replace(registry.extractors[0], config_sha256="e" * 64),),
        )
        reconcile_inventory(
            inventory,
            {},
            registry=previous_registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(previous_registry),
            checkpoint=store.save,
            now=FIXED_NOW,
        )
    records = store.load_all()
    real_save = LedgerStore.save
    activated = False
    writes_broken = True

    def swap_output_and_fail_compensation(
        ledger: LedgerStore, record: SourceRecord
    ) -> Path:
        nonlocal activated
        if activated and writes_broken and record.state is SourceState.EXTRACTING:
            if after_write:
                real_save(ledger, record)
            raise OSError("injected repeated rollback checkpoint failure")
        if record.state is SourceState.WARNING and writes_broken:
            activated = True
            derivation = record.derivations[record.active_derivation_id or ""]
            output = paths.root / derivation.output_path
            replacement = write_bytes(output.with_name("replacement.tmp"), b"y")
            os.utime(
                replacement,
                ns=(derivation.output_mtime_ns, derivation.output_mtime_ns),
            )
            os.replace(replacement, output)
        return real_save(ledger, record)

    monkeypatch.setattr(LedgerStore, "save", swap_output_and_fail_compensation)
    with pytest.raises(OSError, match="rollback checkpoint failure"):
        reconcile_inventory(
            inventory,
            records,
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=store.save,
            explicit_retry=already_active,
            now=FIXED_NOW,
        )

    with pytest.raises((OSError, ValueError), match="activation"):
        LedgerStore(paths).active_representations()
    guard = next(paths.ledger_dir.glob("*.activation-pending"))
    evidence = guard.read_bytes()
    safe = SourceRecord.from_dict(json.loads(evidence)["rollback"])
    assert safe.state is SourceState.EXTRACTING
    assert safe.active_derivation_id is None
    if not after_write:
        shard = paths.ledger_dir / f"{safe.source_id}.json"
        unguarded = SourceRecord.from_dict(json.loads(shard.read_bytes()))
        unsafe = unguarded.derivations[unguarded.active_derivation_id or ""]
        assert compute_sha256(paths.root / unsafe.output_path) != unsafe.output_sha256
    monkeypatch.setattr(commands.ExtractorRegistry, "load", lambda _path: registry)
    services = commands.CommandServices(WarningProcessor, lambda _spec: PREREQUISITE)
    synchronize = commands.sync_sources if command == "sync" else commands.init_sources
    still_broken = synchronize(repo_root, services=services)
    assert not still_broken.ok
    assert guard.read_bytes() == evidence
    with pytest.raises((OSError, ValueError), match="activation"):
        LedgerStore(paths).load_all()

    writes_broken = False

    class RecoveryWitness(WarningProcessor):
        def process(self, record: SourceRecord, *args: object, **kwargs: object):
            durable = LedgerStore(paths).load(record.source_id)
            assert durable.state is SourceState.EXTRACTING
            assert durable.active_derivation_id is None
            return super().process(record, *args, **kwargs)

    recovered = synchronize(
        repo_root,
        services=commands.CommandServices(RecoveryWitness, lambda _spec: PREREQUISITE),
    )
    assert recovered.data["decision_counts"]["process"] == 1
    assert recovered.data["decision_counts"]["retain"] == 0
    assert recovered.data["new_active_representation_count"] == 1
    assert not guard.exists()
    current = next(iter(LedgerStore(paths).load_all().values()))
    derivation = current.derivations[current.active_derivation_id or ""]
    assert (
        compute_sha256(paths.root / derivation.output_path) == derivation.output_sha256
    )


@pytest.mark.parametrize("after_write", (False, True))
def test_activation_guard_write_failure_cannot_activate_candidate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, after_write: bool
) -> None:
    import brainlib.ledger as ledger

    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    registry = make_registry()
    real_write = ledger._atomic_write_at

    def fail_guard_write(directory_fd: int, name: str, *args: object, **kwargs: object):
        if name.endswith(".activation-pending"):
            if after_write:
                real_write(directory_fd, name, *args, **kwargs)
            raise OSError("injected guard publication failure")
        return real_write(directory_fd, name, *args, **kwargs)

    monkeypatch.setattr(ledger, "_atomic_write_at", fail_guard_write)
    with pytest.raises(OSError, match="guard publication failure"):
        reconcile_inventory(
            inventory_raw_sources(paths, MediaDetector()),
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=ledger.LedgerStore(paths).save,
            now=FIXED_NOW,
        )
    shard = next(paths.ledger_dir.glob("*.json"))
    durable = SourceRecord.from_dict(json.loads(shard.read_bytes()))
    assert durable.state is SourceState.EXTRACTING
    assert durable.active_derivation_id is None
    if after_write:
        with pytest.raises((OSError, ValueError), match="activation"):
            ledger.LedgerStore(paths).load_all()
    else:
        assert ledger.LedgerStore(paths).load(durable.source_id) == durable


def test_activation_guard_cleanup_failure_leaves_recoverable_evidence(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brainlib.commands as commands
    import brainlib.ledger as ledger

    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    registry = make_registry()
    real_unlink = ledger.os.unlink

    def fail_guard_cleanup(name: str, *args: object, **kwargs: object):
        if str(name).endswith(".activation-pending"):
            raise OSError("injected activation guard cleanup failure")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(ledger.os, "unlink", fail_guard_cleanup)
    with pytest.raises(OSError, match="guard cleanup failure"):
        reconcile_inventory(
            inventory_raw_sources(paths, MediaDetector()),
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=ledger.LedgerStore(paths).save,
            now=FIXED_NOW,
        )
    guard = next(paths.ledger_dir.glob("*.activation-pending"))
    with pytest.raises((OSError, ValueError), match="activation"):
        ledger.LedgerStore(paths).load_all()
    monkeypatch.setattr(ledger.os, "unlink", real_unlink)
    monkeypatch.setattr(commands.ExtractorRegistry, "load", lambda _path: registry)
    recovered = commands.sync_sources(
        repo_root,
        services=commands.CommandServices(WarningProcessor, lambda _spec: PREREQUISITE),
    )
    assert recovered.data["new_active_representation_count"] == 1
    assert not guard.exists()
    representation = ledger.LedgerStore(paths).active_representations()[0]
    assert (
        compute_sha256(paths.root / representation.extracted_path)
        == representation.output_sha256
    )


def test_swapped_activation_guard_publication_cannot_activate_candidate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brainlib.ledger as ledger

    paths = RepoPaths.discover(repo_root)
    write_bytes(paths.raw / "notes/a.txt", CONTENT_BYTES)
    registry = make_registry()
    real_write = ledger._atomic_write_at

    def swap_guard(directory_fd: int, name: str, *args: object, **kwargs: object):
        real_write(directory_fd, name, *args, **kwargs)
        if name.endswith(".activation-pending"):
            guard = paths.ledger_dir / name
            document = json.loads(guard.read_bytes())
            document["candidate"]["diagnostics"] = []
            replacement = guard.with_name("replacement.tmp")
            replacement.write_text(json.dumps(document))
            os.replace(replacement, guard)

    monkeypatch.setattr(ledger, "_atomic_write_at", swap_guard)
    with pytest.raises((OSError, ValueError), match="activation guard"):
        reconcile_inventory(
            inventory_raw_sources(paths, MediaDetector()),
            {},
            registry=registry,
            processor=WarningProcessor(),
            paths=paths,
            prerequisite_digests=prerequisite_map(registry),
            checkpoint=ledger.LedgerStore(paths).save,
            now=FIXED_NOW,
        )
    shard = next(paths.ledger_dir.glob("*.json"))
    durable = SourceRecord.from_dict(json.loads(shard.read_bytes()))
    assert durable.active_derivation_id is None
    assert next(paths.ledger_dir.glob("*.activation-pending")).exists()
    with pytest.raises((OSError, ValueError), match="activation"):
        ledger.LedgerStore(paths).load_all()


@pytest.mark.parametrize(
    ("agent_fallback", "agent_revision"),
    ((False, "1"), (True, "2")),
)
def test_processor_agent_derivation_is_rejected_at_sync_boundary(
    repo_root: Path, agent_fallback: bool, agent_revision: str
) -> None:
    registry = make_registry(agent_fallback=agent_fallback)
    item = make_item(content_sha256=CONTENT_SHA256)
    materialize_item(RepoPaths.discover(repo_root), item, CONTENT_BYTES)

    class AgentArtifactProcessor(WarningProcessor):
        def process(
            self,
            record: SourceRecord,
            item: InventoryItem,
            extractor: ExtractorSpec,
            *,
            paths: RepoPaths,
            context: ProcessingContext,
        ) -> ProcessResult:
            result = super().process(
                record, item, extractor, paths=paths, context=context
            )
            assert result.derivation is not None
            derivation = replace(
                result.derivation,
                method="agent",
                method_metadata={
                    "handoff_id": "handoff-1",
                    "agent_revision": agent_revision,
                    "note": "agent output",
                },
            )
            return replace(result, derivation=derivation)

    with pytest.raises(ValueError, match="processor result"):
        reconcile_inventory(
            InventoryReport((item,), ()),
            {},
            registry=registry,
            processor=AgentArtifactProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            now=FIXED_NOW,
        )


def test_changed_url_approval_state_is_idempotent_across_repeat(
    repo_root: Path,
) -> None:
    registry = make_registry()
    original_item = make_url_item()
    original = make_captured_url_record(original_item)
    changed_item = make_url_item(
        url="https://example.test/b",
        description="B",
        mtime_ns=2,
    )
    first, _ = reconcile_inventory(
        InventoryReport((changed_item,), ()),
        {original.source_id: original},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    checkpoints: list[SourceRecord] = []

    second, _ = reconcile_inventory(
        InventoryReport((changed_item,), ()),
        first,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        checkpoint=checkpoints.append,
        now=FIXED_NOW,
    )

    first_result = first[original.source_id]
    result = second[original.source_id]
    assert result is first_result
    assert checkpoints == []
    assert result.state is SourceState.AWAITING_APPROVAL
    assert result.active_content_sha256 is None
    assert result.active_derivation_id is None
    assert result.versions == original.versions
    assert result.derivations == original.derivations
    assert [item.code for item in result.diagnostics].count(
        "web_capture_awaiting_approval"
    ) == 1
    assert compute_corpus_revision(second.values()) == compute_corpus_revision(
        first.values()
    )


def test_changed_url_approval_state_survives_missing_and_restoration(
    repo_root: Path,
) -> None:
    registry = make_registry()
    original_item = make_url_item()
    original = make_captured_url_record(original_item)
    changed_item = make_url_item(
        url="https://example.test/b",
        description="B",
        mtime_ns=2,
    )
    changed, _ = reconcile_inventory(
        InventoryReport((changed_item,), ()),
        {original.source_id: original},
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    absent, _ = reconcile_inventory(
        InventoryReport((), ()),
        changed,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )
    restored, _ = reconcile_inventory(
        InventoryReport((changed_item,), ()),
        absent,
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        now=FIXED_NOW,
    )

    missing_result = absent[original.source_id]
    result = restored[original.source_id]
    assert missing_result.state is SourceState.AWAITING_APPROVAL
    assert {item.code for item in missing_result.diagnostics} >= {
        "raw_missing",
        "web_capture_awaiting_approval",
    }
    assert result.state is SourceState.AWAITING_APPROVAL
    assert result.active_content_sha256 is None
    assert result.active_derivation_id is None
    assert result.versions == original.versions
    assert result.derivations == original.derivations
    assert "raw_missing" not in {item.code for item in result.diagnostics}
    assert [item.code for item in result.diagnostics].count(
        "web_capture_awaiting_approval"
    ) == 1
    assert compute_corpus_revision(restored.values()) == compute_corpus_revision(
        changed.values()
    )


def test_large_unchanged_corpus_keeps_output_and_peak_memory_bounded(
    repo_root: Path,
) -> None:
    registry = make_registry()
    records: dict[str, SourceRecord] = {}
    items: list[InventoryItem] = []
    for number in range(5_000):
        path = f"bulk/{number:05d}.txt"
        checksum = f"{number + 1:064x}"
        record = make_aligned_record(
            path=path,
            checksum=checksum,
            registry=registry,
        )
        records[record.source_id] = record
        items.append(make_item(path=path, content_sha256=None))

    tracemalloc.start()
    _, report = reconcile_inventory(
        InventoryReport(tuple(reversed(items)), ()),
        dict(reversed(tuple(records.items()))),
        registry=registry,
        processor=UnavailableProcessor(),
        paths=RepoPaths.discover(repo_root),
        prerequisite_digests=prerequisite_map(registry),
        hash_file=never_hash,
        now=FIXED_NOW,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rendered = json.dumps(
        {
            "decision_counts": {
                key.value: value for key, value in report.decision_counts.items()
            },
            "sampled_decisions": [
                decision.reason for decision in report.sampled_decisions
            ],
        }
    )

    assert len(report.sampled_decisions) <= 100
    assert len(rendered.encode("utf-8")) < 65_536
    assert peak < 64 * 1024 * 1024


def test_large_gap_corpus_has_bounded_report_and_exact_durable_events(
    repo_root: Path,
) -> None:
    registry = make_registry()
    records: dict[str, SourceRecord] = {}
    items: list[InventoryItem] = []
    for number in range(10_000):
        path = f"bulk/{number:05d}.bin"
        checksum = f"{number + 1:064x}"
        record = make_aligned_record(
            path=path,
            checksum=checksum,
            registry=registry,
        )
        records[record.source_id] = record
        items.append(
            make_item(
                path=path,
                content_sha256=None,
                media_type="application/octet-stream",
                extension=".bin",
            )
        )

    store = SyncResultStore(RepoPaths.discover(repo_root))
    with store.writer("sync", FIXED_NOW) as writer:
        _, report = reconcile_inventory(
            InventoryReport(tuple(reversed(items)), ()),
            dict(reversed(tuple(records.items()))),
            registry=registry,
            processor=UnavailableProcessor(),
            paths=RepoPaths.discover(repo_root),
            prerequisite_digests=prerequisite_map(registry),
            hash_file=never_hash,
            event_sink=writer.emit,
            now=FIXED_NOW,
        )
        reference = writer.finalize(report.corpus_revision)
    report = replace(report, result_manifest=reference)
    rendered = json.dumps(sync_report_data(report)).encode("utf-8")

    assert report.coverage_gap_count == 10_000
    assert len(report.coverage_gaps) <= 100
    assert len(rendered) < 128 * 1024
    assert reference.event_counts["coverage_gap"] == 10_000
    assert sum(1 for _event in store.iter_events(reference)) == 10_000
