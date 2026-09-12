from __future__ import annotations

import hashlib
import io
import json
import os
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path, PurePosixPath

import pytest

import brainlib.inventory as inventory_module
import brainlib.ledger as ledger_module
from brainlib.cli import main
from brainlib.contracts import (
    Anchor,
    ContentVersion,
    Derivation,
    FileFingerprint,
    SourceRecord,
    SourceState,
    UrlDescriptorMetadata,
    VersionAdoptionEvent,
    compute_corpus_revision,
    derivation_id,
)
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore, derive_extraction_path
from brainlib.diagnostics import Diagnostic
from brainlib.registry import ExtractorRegistry, effective_extractor_version
from brainlib.validation import ChecksumCache, validate_source_ledger
from brainlib.inventory import (
    InventoryReport,
    MediaDetector,
    SnapshotNamespace,
    inventory_raw_sources,
    source_id_for_first_seen,
    source_id_for_url_descriptor,
)
from tests.helpers import FIXED_NOW, make_retrieval_metadata


def run_json(repo_root: Path, *argv: str) -> tuple[int, dict[str, object]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    return_code = main(
        ["--json", *argv],
        cwd=repo_root,
        stdout=stdout,
        stderr=stderr,
    )
    assert stderr.getvalue() == ""
    return return_code, json.loads(stdout.getvalue())


def initialize_one_active_record(repo_root: Path) -> SourceRecord:
    paths = RepoPaths.discover(repo_root)
    raw = paths.raw / "notes/a.txt"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"original")
    raw_stat = raw.stat()
    raw_sha = hashlib.sha256(b"original").hexdigest()
    registry = ExtractorRegistry.load(paths.registry)
    extractor = registry.select("text/plain", PurePosixPath("notes/a.txt"))
    assert extractor is not None
    prerequisite = hashlib.sha256(
        f"converter-v1\0builtin.text\0builtin:builtin.text:{extractor.extractor_version}".encode()
    ).hexdigest()
    version = effective_extractor_version(extractor, prerequisite)
    identifier = derivation_id(
        source_sha256=raw_sha,
        extractor_id=extractor.extractor_id,
        extractor_version=version,
        config_sha256=extractor.config_sha256,
    )
    relative_output = derive_extraction_path(
        PurePosixPath("notes/a.txt"), raw_sha, identifier
    )
    output = paths.extracted / relative_output
    output.parent.mkdir(parents=True)
    anchor_kind = extractor.expected_anchors[0]
    output.write_bytes(f'<a id="{anchor_kind}:1"></a>\noriginal\n'.encode())
    output_stat = output.stat()
    output_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    derivation = Derivation(
        identifier,
        raw_sha,
        extractor.extractor_id,
        version,
        extractor.config_sha256,
        PurePosixPath("sources/extracted", relative_output),
        output_sha,
        output_stat.st_size,
        output_stat.st_mtime_ns,
        "ok",
        (Anchor(anchor_kind, "1"),),
        FIXED_NOW,
        method="deterministic",
        method_metadata={
            "converter_id": "builtin.text",
            "converter_version": f"builtin:builtin.text:{extractor.extractor_version}",
        },
    )
    source_id = source_id_for_first_seen(PurePosixPath("notes/a.txt"), raw_sha)
    content_version = ContentVersion(
        raw_sha,
        PurePosixPath("notes/a.txt"),
        raw_stat.st_size,
        FileFingerprint(
            PurePosixPath("notes/a.txt"), raw_stat.st_size, raw_stat.st_mtime_ns
        ),
        FIXED_NOW,
        (),
    )
    record = SourceRecord(
        1,
        source_id,
        PurePosixPath("notes/a.txt"),
        (),
        "text/plain",
        raw_stat.st_size,
        SourceState.OK,
        {raw_sha: content_version},
        raw_sha,
        {identifier: derivation},
        identifier,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
    )
    store = LedgerStore(paths)
    store.save(record)
    store.write_summary([record], generated_at=FIXED_NOW)
    return record


def with_historical_root(
    record: SourceRecord,
    version: ContentVersion,
) -> SourceRecord:
    active_sha256 = record.active_content_sha256
    assert active_sha256 is not None
    active_version = record.versions[active_sha256]
    return replace(
        record,
        source_id=source_id_for_first_seen(record.current_raw_path, version.sha256),
        versions={version.sha256: version, **record.versions},
        adoption_events=(
            VersionAdoptionEvent(
                version.sha256,
                active_sha256,
                "Approved test replacement.",
                active_version.first_seen_at,
            ),
        ),
    )


def test_pristine_uninitialized_ledger_summary_is_valid(repo_root: Path) -> None:
    return_code, payload = run_json(repo_root, "validate", "--full")

    assert return_code == 0
    assert payload["ok"] is True
    assert {
        check
        for report in payload["data"]["reports"]
        for check in report["checks"]
    } == {
        "template-layout",
        "source-ledger",
        "wiki-transaction",
        "citations",
        "wiki-interpretations",
        "wiki-graph",
        "instruction-architecture",
    }
    assert all(not report["issues"] for report in payload["data"]["reports"])


def test_generated_empty_ledger_summary_is_valid_and_has_empty_revision(
    repo_root: Path,
) -> None:
    LedgerStore(RepoPaths.discover(repo_root)).write_summary([], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 0
    assert {
        report["corpus_revision"]
        for report in payload["data"]["reports"]
        if report["corpus_revision"] is not None
    } == {compute_corpus_revision([])}


def test_sentinel_is_invalid_when_discoverable_sources_exist(repo_root: Path) -> None:
    (repo_root / "sources/raw/a.txt").write_bytes(b"a")

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "ledger_not_initialized_with_sources" in json.dumps(payload)


def test_validation_rejects_live_url_descriptor_metadata_drift(
    repo_root: Path,
) -> None:
    descriptor = repo_root / "sources/raw/reference.url.md"
    descriptor.write_text(
        "---\n"
        "kind: url\n"
        "url: https://example.test/one\n"
        "description: Example one\n"
        "added: 2026-09-04\n"
        "---\n",
        encoding="utf-8",
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    assert (
        main(
            ["--json", "init"],
            cwd=repo_root,
            stdout=stdout,
            stderr=stderr,
        )
        == 1
    )
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace("Example one", "Example two"),
        encoding="utf-8",
    )

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "url_descriptor_mismatch" in json.dumps(payload)


def test_validate_full_reports_raw_checksum_mismatch(repo_root: Path) -> None:
    initialize_one_active_record(repo_root)
    (repo_root / "sources/raw/notes/a.txt").write_bytes(b"changed!")

    return_code, payload = run_json(repo_root, "validate", "--full")

    assert return_code == 1
    assert "raw_checksum_mismatch" in json.dumps(payload)


@pytest.mark.parametrize("full", (False, True))
def test_validation_rejects_escaping_retained_paths_without_reading_them(
    repo_root: Path,
    full: bool,
) -> None:
    record = initialize_one_active_record(repo_root)
    checksum = record.active_content_sha256 or ""
    version = record.versions[checksum]
    escaped = PurePosixPath("../outside-secret.txt")
    invalid_version = replace(
        version,
        raw_path=escaped,
        fingerprint=replace(version.fingerprint, path=escaped),
    )
    invalid_record = replace(record, versions={checksum: invalid_version})
    observed: list[Path] = []

    def forbidden_hash(path: Path) -> str:
        observed.append(path)
        raise AssertionError("invalid path reached hashing")

    paths = RepoPaths.discover(repo_root)
    report = validate_source_ledger(
        paths,
        {record.source_id: invalid_record},
        full=full,
        checksum_cache=ChecksumCache(hash_file=forbidden_hash),
    )

    assert not report.ok
    assert "ledger_record_invalid" in {issue.code for issue in report.issues}
    assert observed == []


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize("swap", ("parent", "final"))
def test_validation_detects_parent_and_final_swaps_without_reading_outside(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
    swap: str,
) -> None:
    initialize_one_active_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    records = LedgerStore(paths).load_all()
    inventory = inventory_raw_sources(paths, MediaDetector())
    registry = ExtractorRegistry.load(paths.registry)
    summary = LedgerStore(paths).read_summary()
    source = paths.raw / "notes/a.txt"
    outside = repo_root.parent / "outside-secret.txt"
    outside.write_bytes(b"secret")
    moved_parent = repo_root.parent / "detached-notes"
    original_check = inventory_module._open_content_is_unchanged
    checks = 0

    def swap_after_first_check(*args: object, **kwargs: object) -> bool:
        nonlocal checks
        result = original_check(*args, **kwargs)  # type: ignore[arg-type]
        checks += 1
        if checks == 1:
            if swap == "parent":
                source.parent.rename(moved_parent)
                source.parent.mkdir()
                (source.parent / source.name).symlink_to(outside)
            else:
                source.unlink()
                source.symlink_to(outside)
        return result

    observed: list[bytes] = []

    def inspect_hash(path: Path) -> str:
        value = path.read_bytes()
        observed.append(value)
        return hashlib.sha256(value).hexdigest()

    monkeypatch.setattr(
        inventory_module,
        "_open_content_is_unchanged",
        swap_after_first_check,
    )
    report = validate_source_ledger(
        paths,
        records,
        full=full,
        checksum_cache=ChecksumCache(hash_file=inspect_hash),
        inventory=inventory,
        registry=registry,
        summary_bytes=summary,
    )

    assert not report.ok
    assert "raw_file_unavailable" in {issue.code for issue in report.issues}
    assert b"secret" not in observed


def test_normal_validation_hashes_zero_bytes_but_checks_output_metadata(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    output = (
        repo_root / record.derivations[record.active_derivation_id or ""].output_path
    )
    output.write_bytes(b"same-size-but-different-content!")

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "output_" in json.dumps(payload)
    assert "output_checksum_mismatch" not in json.dumps(payload)


def test_validate_full_hashes_archived_content_versions(repo_root: Path) -> None:
    record = initialize_one_active_record(repo_root)
    old_bytes = b"older"
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    updated_source_id = source_id_for_first_seen(record.current_raw_path, old_sha)
    archive_path = PurePosixPath("_versions", updated_source_id, old_sha, "a.txt")
    archive = repo_root / "sources/raw" / archive_path
    archive.parent.mkdir(parents=True)
    archive.write_bytes(old_bytes)
    archive_stat = archive.stat()
    old_version = ContentVersion(
        old_sha,
        archive_path,
        archive_stat.st_size,
        FileFingerprint(archive_path, archive_stat.st_size, archive_stat.st_mtime_ns),
        FIXED_NOW,
        (),
    )
    updated = with_historical_root(record, old_version)
    store = LedgerStore(RepoPaths.discover(repo_root))
    (store.paths.ledger_dir / f"{record.source_id}.json").unlink()
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    archive.write_bytes(b"tampered")

    return_code, payload = run_json(repo_root, "validate", "--full")

    assert return_code == 1
    assert "raw_checksum_mismatch" in json.dumps(payload)
    assert archive_path.as_posix() in json.dumps(payload)


def test_validation_checks_inactive_retained_derivation(repo_root: Path) -> None:
    record = initialize_one_active_record(repo_root)
    active = record.derivations[record.active_derivation_id or ""]
    inactive_id = derivation_id(
        source_sha256=active.source_sha256,
        extractor_id=active.extractor_id,
        extractor_version=active.extractor_version + ".old",
        config_sha256=active.config_sha256,
    )
    inactive_path = PurePosixPath(
        "sources/extracted/notes/a.txt",
        active.source_sha256,
        inactive_id + ".md",
    )
    inactive_file = repo_root / inactive_path
    inactive_file.parent.mkdir(parents=True, exist_ok=True)
    inactive_file.write_bytes(b"inactive")
    inactive_stat = inactive_file.stat()
    inactive = replace(
        active,
        derivation_id=inactive_id,
        extractor_version=active.extractor_version + ".old",
        output_path=inactive_path,
        output_sha256=hashlib.sha256(b"inactive").hexdigest(),
        output_byte_size=inactive_stat.st_size,
        output_mtime_ns=inactive_stat.st_mtime_ns,
    )
    updated = replace(record, derivations={**record.derivations, inactive_id: inactive})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    inactive_file.unlink()

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert inactive_id in json.dumps(payload)


def test_validation_allows_older_active_representation_while_retry_pending(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    pending = replace(record, state=SourceState.PENDING)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(pending)
    store.write_summary([pending], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 0
    assert payload["ok"] is True


def test_validation_rejects_searchable_state_that_disagrees_with_active_quality(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    warning_derivation = replace(record.derivations[active_id], quality_state="warning")
    inconsistent = replace(record, derivations={active_id: warning_derivation})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(inconsistent)
    store.write_summary([inconsistent], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "active_quality_state_mismatch" in json.dumps(payload)


@pytest.mark.parametrize("state", (SourceState.OK, SourceState.WARNING))
def test_validation_requires_searchable_records_to_have_an_active_representation(
    repo_root: Path, state: SourceState
) -> None:
    record = initialize_one_active_record(repo_root)
    diagnostics = (
        ()
        if state is SourceState.OK
        else (Diagnostic("partial_text", "Artifact quality is partial."),)
    )
    invalid = replace(
        record,
        state=state,
        derivations={},
        active_derivation_id=None,
        diagnostics=diagnostics,
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(invalid)
    store.write_summary([invalid], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "active_representation_missing" in json.dumps(payload)

    status_code, status_payload = run_json(repo_root, "status")

    assert status_code == 1
    assert status_payload["data"]["status"] == "complete_with_gaps"
    assert "active_representation_missing" in json.dumps(status_payload)


@pytest.mark.parametrize(
    "state", (SourceState.PENDING, SourceState.FAILED, SourceState.NEEDS_AGENT)
)
def test_validation_allows_retry_state_to_retain_older_active_representation(
    repo_root: Path, state: SourceState
) -> None:
    record = initialize_one_active_record(repo_root)
    retry = replace(record, state=state)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(retry)
    store.write_summary([retry], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 0
    assert payload["ok"] is True


def test_validation_rejects_record_metadata_not_linked_to_active_version(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    inconsistent = replace(record, byte_size=record.byte_size + 1)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(inconsistent)
    store.write_summary([inconsistent], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "active_version_metadata_mismatch" in json.dumps(payload)


def test_validation_rejects_summary_tampering(repo_root: Path) -> None:
    initialize_one_active_record(repo_root)
    summary = repo_root / "sources/ledger.md"
    summary.write_text(summary.read_text().replace("- ok: 1", "- ok: 2"))

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "ledger_summary_mismatch" in json.dumps(payload)


def test_validation_rejects_an_empty_ledger_summary(repo_root: Path) -> None:
    (repo_root / "sources/ledger.md").write_bytes(b"")

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "ledger_summary_mismatch" in json.dumps(payload)


def test_validation_rejects_summary_replaced_between_named_stat_and_open(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    store.write_summary([], generated_at=FIXED_NOW)
    replacement = repo_root / "replacement-ledger.md"
    replacement.write_text(
        store.render_summary([], generated_at=FIXED_NOW + timedelta(seconds=1)),
        encoding="utf-8",
    )
    sources = paths.ledger_summary.parent
    sources_inode = sources.stat().st_ino
    real_open = ledger_module.os.open
    swapped = False

    def replace_before_open(
        name: str | os.PathLike[str], *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        directory_fd = kwargs.get("dir_fd")
        if (
            not swapped
            and os.fspath(name) == "ledger.md"
            and isinstance(directory_fd, int)
            and os.fstat(directory_fd).st_ino == sources_inode
        ):
            swapped = True
            os.replace(replacement, paths.ledger_summary)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(ledger_module.os, "open", replace_before_open)
    monkeypatch.setattr(ledger_module, "_OPEN_SUPPORT_MARKER", replace_before_open)

    report = validate_source_ledger(paths, {})

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"ledger_summary_unreadable"}


def test_validation_rejects_duplicate_current_paths(repo_root: Path) -> None:
    record = initialize_one_active_record(repo_root)
    creation_path = PurePosixPath("notes/original-b.txt")
    duplicate = replace(
        record,
        source_id=source_id_for_first_seen(
            creation_path,
            record.active_content_sha256 or "",
        ),
        previous_raw_paths=(creation_path,),
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(duplicate)
    store.write_summary([record, duplicate], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "duplicate_current_raw_path" in json.dumps(payload)


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize(
    "raw_path",
    (
        PurePosixPath("_versions", "src_" + "b" * 64, "{sha}", "a.txt"),
        PurePosixPath("_versions", "src_" + "a" * 64, "0" * 64, "a.txt"),
        PurePosixPath("_web", "src_" + "b" * 64, "{sha}", "a.txt"),
        PurePosixPath("_web", "src_" + "a" * 64, "0" * 64, "a.txt"),
        PurePosixPath("inactive-user-path.txt"),
    ),
)
def test_validation_rejects_retained_raw_paths_with_the_wrong_semantic_role_before_io(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
    raw_path: PurePosixPath,
) -> None:
    record = initialize_one_active_record(repo_root)
    historical_bytes = b"historical"
    checksum = hashlib.sha256(historical_bytes).hexdigest()
    logical_path = PurePosixPath(
        *(checksum if part == "{sha}" else part for part in raw_path.parts)
    )
    materialization = repo_root / "sources/raw" / logical_path
    materialization.parent.mkdir(parents=True, exist_ok=True)
    materialization.write_bytes(historical_bytes)
    metadata = materialization.stat()
    version = ContentVersion(
        checksum,
        logical_path,
        metadata.st_size,
        FileFingerprint(logical_path, metadata.st_size, metadata.st_mtime_ns),
        FIXED_NOW,
        (),
    )
    updated = with_historical_root(record, version)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    observed: list[PurePosixPath] = []
    real_snapshot = inventory_module.stable_file_snapshot

    def observe(*args: object, **kwargs: object) -> object:
        observed.append(args[2])  # type: ignore[arg-type]
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("brainlib.validation.stable_file_snapshot", observe)

    report = validate_source_ledger(
        RepoPaths.discover(repo_root), {updated.source_id: updated}, full=full
    )

    assert "raw_path_role_invalid" in {issue.code for issue in report.issues}
    assert logical_path not in observed


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize("prefix", ("_versions", "_web"))
def test_validation_rejects_reserved_active_raw_path_role_before_io(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, full: bool, prefix: str
) -> None:
    record = initialize_one_active_record(repo_root)
    checksum = record.active_content_sha256 or ""
    active = record.versions[checksum]
    logical_path = PurePosixPath(prefix, record.source_id, checksum, "a.txt")
    invalid_version = replace(
        active,
        raw_path=logical_path,
        fingerprint=replace(active.fingerprint, path=logical_path),
    )
    invalid = replace(
        record,
        current_raw_path=logical_path,
        previous_raw_paths=(record.current_raw_path,),
        versions={checksum: invalid_version},
    )
    observed: list[PurePosixPath] = []
    real_snapshot = inventory_module.stable_file_snapshot

    def observe(*args: object, **kwargs: object) -> object:
        observed.append(args[2])  # type: ignore[arg-type]
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("brainlib.validation.stable_file_snapshot", observe)
    report = validate_source_ledger(
        RepoPaths.discover(repo_root),
        {record.source_id: invalid},
        full=full,
        inventory=InventoryReport((), ()),
        summary_bytes=LedgerStore(RepoPaths.discover(repo_root))
        .render_summary([invalid], generated_at=FIXED_NOW)
        .encode(),
    )

    assert "raw_path_role_invalid" in {issue.code for issue in report.issues}
    assert logical_path not in observed


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize("mismatch", ("owner", "checksum", "namespace"))
def test_validation_binds_web_materialization_to_descriptor_record_before_io(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
    mismatch: str,
) -> None:
    base = initialize_one_active_record(repo_root)
    descriptor_path = PurePosixPath("reference.url.md")
    descriptor_file = repo_root / "sources/raw" / descriptor_path
    descriptor_file.write_text(
        "---\nkind: url\nurl: https://example.test/page\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        encoding="utf-8",
    )
    descriptor_stat = descriptor_file.stat()
    web_bytes = b"captured"
    checksum = hashlib.sha256(web_bytes).hexdigest()
    owner = "src_" + "b" * 64 if mismatch == "owner" else base.source_id
    embedded = "0" * 64 if mismatch == "checksum" else checksum
    prefix = "_versions" if mismatch == "namespace" else "_web"
    raw_path = PurePosixPath(prefix, owner, embedded, "page.html")
    materialization = repo_root / "sources/raw" / raw_path
    materialization.parent.mkdir(parents=True)
    materialization.write_bytes(web_bytes)
    raw_stat = materialization.stat()
    version = ContentVersion(
        checksum,
        raw_path,
        raw_stat.st_size,
        FileFingerprint(raw_path, raw_stat.st_size, raw_stat.st_mtime_ns),
        FIXED_NOW,
        (),
    )
    record = replace(
        base,
        current_raw_path=descriptor_path,
        media_type="application/x.second-brain-url-descriptor",
        byte_size=descriptor_stat.st_size,
        state=SourceState.PENDING,
        versions={checksum: version},
        active_content_sha256=checksum,
        derivations={},
        active_derivation_id=None,
        url_descriptor=UrlDescriptorMetadata(
            "https://example.test/page",
            "Example",
            date(2026, 9, 4),
            FileFingerprint(
                descriptor_path, descriptor_stat.st_size, descriptor_stat.st_mtime_ns
            ),
        ),
    )
    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    store.save(record)
    store.write_summary([record], generated_at=FIXED_NOW)
    observed: list[PurePosixPath] = []
    real_snapshot = inventory_module.stable_file_snapshot

    def observe(*args: object, **kwargs: object) -> object:
        observed.append(args[2])  # type: ignore[arg-type]
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("brainlib.validation.stable_file_snapshot", observe)

    report = validate_source_ledger(paths, {record.source_id: record}, full=full)

    assert "raw_path_role_invalid" in {issue.code for issue in report.issues}
    assert raw_path not in observed


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize("suffix", (".bin", ".extra.md", ".md.backup"))
def test_validation_rejects_noncanonical_derivation_output_suffix(
    repo_root: Path, full: bool, suffix: str
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    derivation = record.derivations[active_id]
    old_output = repo_root / derivation.output_path
    bad_path = derivation.output_path.with_name(active_id + suffix)
    bad_output = repo_root / bad_path
    old_output.rename(bad_output)
    bad_stat = bad_output.stat()
    malformed = replace(
        derivation,
        output_path=bad_path,
        output_mtime_ns=bad_stat.st_mtime_ns,
    )
    updated = replace(record, derivations={active_id: malformed})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 1
    assert "derivation_output_path_invalid" in json.dumps(payload)


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize(
    ("mutation", "value", "expected_code"),
    (
        ("extractor_id", "impossible/id", "derivation_extractor_id_invalid"),
        ("extractor_id", "nul\0id", "derivation_extractor_id_invalid"),
        ("converter_id", "impossible/id", "derivation_converter_id_invalid"),
        ("converter_id", "nul\0id", "derivation_converter_id_invalid"),
        (
            "converter_version",
            "binary\0version",
            "derivation_converter_version_invalid",
        ),
    ),
)
def test_validation_rejects_self_consistent_impossible_stored_provenance(
    repo_root: Path,
    full: bool,
    mutation: str,
    value: str,
    expected_code: str,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    extractor_id = active.extractor_id
    converter_id = str(active.method_metadata["converter_id"])
    converter_version = str(active.method_metadata["converter_version"])
    if mutation == "extractor_id":
        extractor_id = value
    elif mutation == "converter_id":
        converter_id = value
    else:
        converter_version = value
    digest = hashlib.sha256(
        f"converter-v1\0{converter_id}\0{converter_version}".encode()
    ).hexdigest()
    base_version = active.extractor_version.rpartition("+")[0]
    extractor_version = f"{base_version}+{digest}"
    identifier = (
        "drv_"
        + hashlib.sha256(
            "\0".join(
                (
                    active.source_sha256,
                    extractor_id,
                    extractor_version,
                    active.config_sha256,
                )
            ).encode()
        ).hexdigest()
    )
    output_path = active.output_path.with_name(identifier + ".md")
    (repo_root / active.output_path).rename(repo_root / output_path)
    output_stat = (repo_root / output_path).stat()
    malformed = replace(
        active,
        derivation_id=identifier,
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        output_path=output_path,
        output_mtime_ns=output_stat.st_mtime_ns,
        method_metadata={
            "converter_id": converter_id,
            "converter_version": converter_version,
        },
    )
    updated = replace(
        record,
        derivations={identifier: malformed},
        active_derivation_id=identifier,
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 1
    assert expected_code in json.dumps(payload)


def test_full_validation_accepts_canonical_historical_agent_provenance(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    historical_agent = replace(
        active,
        method="agent",
        method_metadata={
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "2",
            "note": "Approved historical visual extraction.",
        },
    )
    updated = replace(record, derivations={active_id: historical_agent})
    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    paths.registry.write_text(
        paths.registry.read_text(encoding="utf-8")
        + "\n# The current registry may evolve independently of stored agent evidence.\n",
        encoding="utf-8",
    )

    report = validate_source_ledger(paths, {updated.source_id: updated}, full=True)

    assert report.ok


def _retarget_converter_provenance(
    repo_root: Path,
    *,
    converter_id: str,
    converter_version: str,
) -> SourceRecord:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    prerequisite = hashlib.sha256(
        f"converter-v1\0{converter_id}\0{converter_version}".encode()
    ).hexdigest()
    base_version = active.extractor_version.rpartition("+")[0]
    extractor_version = f"{base_version}+{prerequisite}"
    identifier = derivation_id(
        source_sha256=active.source_sha256,
        extractor_id=active.extractor_id,
        extractor_version=extractor_version,
        config_sha256=active.config_sha256,
    )
    output_path = active.output_path.with_name(identifier + ".md")
    (repo_root / active.output_path).rename(repo_root / output_path)
    output_stat = (repo_root / output_path).stat()
    retargeted = replace(
        active,
        derivation_id=identifier,
        extractor_version=extractor_version,
        output_path=output_path,
        output_mtime_ns=output_stat.st_mtime_ns,
        method_metadata={
            "converter_id": converter_id,
            "converter_version": converter_version,
        },
    )
    updated = replace(
        record,
        derivations={identifier: retargeted},
        active_derivation_id=identifier,
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    return updated


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize(
    ("converter_id", "converter_version", "expected_code"),
    (
        (
            "builtin.not-implemented",
            "builtin:builtin.not-implemented:1",
            "derivation_converter_id_invalid",
        ),
        (
            "builtin.text",
            "bogus-version",
            "derivation_converter_version_invalid",
        ),
        (
            "external.converter",
            " external 1.0",
            "derivation_converter_version_invalid",
        ),
        (
            "external.converter",
            "external  1.0",
            "derivation_converter_version_invalid",
        ),
        (
            "external.converter",
            "external\n1.0",
            "derivation_converter_version_invalid",
        ),
    ),
)
def test_validation_rejects_impossible_converter_producer_provenance(
    repo_root: Path,
    full: bool,
    converter_id: str,
    converter_version: str,
    expected_code: str,
) -> None:
    _retarget_converter_provenance(
        repo_root,
        converter_id=converter_id,
        converter_version=converter_version,
    )

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 1
    assert expected_code in json.dumps(payload)


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize(
    ("converter_id", "converter_version"),
    (
        ("builtin.json", "builtin:builtin.json:2"),
        ("external.converter", "external converter 1.0"),
    ),
)
def test_validation_accepts_invariant_builtin_and_external_provenance(
    repo_root: Path,
    full: bool,
    converter_id: str,
    converter_version: str,
) -> None:
    _retarget_converter_provenance(
        repo_root,
        converter_id=converter_id,
        converter_version=converter_version,
    )

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 0
    assert payload["ok"] is True


def test_validation_rejects_derivation_stored_below_an_unrelated_raw_path(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    prior_output = repo_root / active.output_path
    unrelated_path = PurePosixPath(
        "sources/extracted/unrelated/name.txt",
        active.source_sha256,
        active_id + ".md",
    )
    unrelated_output = repo_root / unrelated_path
    unrelated_output.parent.mkdir(parents=True)
    prior_output.rename(unrelated_output)
    unrelated_stat = unrelated_output.stat()
    malformed = replace(
        active,
        output_path=unrelated_path,
        output_mtime_ns=unrelated_stat.st_mtime_ns,
    )
    updated = replace(record, derivations={active_id: malformed})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "derivation_output_path_invalid" in json.dumps(payload)


@pytest.mark.parametrize("full", (False, True))
@pytest.mark.parametrize(
    "fabricated_origin",
    (
        PurePosixPath("_versions", "{owner}", "{sha}", "other.txt"),
        PurePosixPath("_web", "src_" + "b" * 64, "{sha}", "other.html"),
        PurePosixPath("_web", "{owner}", "0" * 64, "other.html"),
        PurePosixPath("_web", "src_" + "b" * 64, "0" * 64, "other.html"),
    ),
)
def test_validation_rejects_fabricated_reserved_previous_origin_before_observation(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
    fabricated_origin: PurePosixPath,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    origin = PurePosixPath(
        *(
            record.source_id
            if part == "{owner}"
            else active.source_sha256
            if part == "{sha}"
            else part
            for part in fabricated_origin.parts
        )
    )
    invalid_path = PurePosixPath(
        "sources/extracted", origin, active.source_sha256, active_id + ".md"
    )
    invalid_output = repo_root / invalid_path
    invalid_output.parent.mkdir(parents=True)
    (repo_root / active.output_path).rename(invalid_output)
    invalid_stat = invalid_output.stat()
    invalid_derivation = replace(
        active,
        output_path=invalid_path,
        output_mtime_ns=invalid_stat.st_mtime_ns,
    )
    invalid = replace(
        record,
        previous_raw_paths=(record.current_raw_path, origin),
        derivations={active_id: invalid_derivation},
    )
    paths = RepoPaths.discover(repo_root)
    summary = (
        LedgerStore(paths).render_summary([invalid], generated_at=FIXED_NOW).encode()
    )
    observed: list[PurePosixPath] = []
    real_snapshot = inventory_module.stable_file_snapshot

    def observe(*args: object, **kwargs: object) -> object:
        observed.append(args[2])  # type: ignore[arg-type]
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("brainlib.validation.stable_file_snapshot", observe)

    report = validate_source_ledger(
        paths,
        {record.source_id: invalid},
        full=full,
        summary_bytes=summary,
    )

    assert "raw_origin_path_invalid" in {issue.code for issue in report.issues}
    assert invalid_path not in observed


@pytest.mark.parametrize("full", (False, True))
def test_validation_preserves_genuine_file_rename_output_ancestry(
    repo_root: Path,
    full: bool,
) -> None:
    original = initialize_one_active_record(repo_root)
    old_raw = repo_root / "sources/raw/notes/a.txt"
    new_raw = repo_root / "sources/raw/notes/renamed.txt"
    old_raw.rename(new_raw)

    sync_code, sync_payload = run_json(repo_root, "sync")

    assert sync_code == 0
    assert sync_payload["data"]["citation_rewrites"] == [
        {
            "source_id": original.source_id,
            "content_sha256": original.active_content_sha256,
            "raw_path": "notes/renamed.txt",
        }
    ]
    renamed = LedgerStore(RepoPaths.discover(repo_root)).load(original.source_id)
    assert renamed.previous_raw_paths == (PurePosixPath("notes/a.txt"),)
    assert renamed.derivations[renamed.active_derivation_id or ""].output_path == (
        original.derivations[original.active_derivation_id or ""].output_path
    )

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 0
    assert payload["ok"] is True


def _initialize_captured_url_record(repo_root: Path) -> SourceRecord:
    paths = RepoPaths.discover(repo_root)
    original_path = PurePosixPath("urls/original.url.md")
    descriptor_file = paths.raw / original_path
    descriptor_file.parent.mkdir(parents=True)
    descriptor_file.write_text(
        "---\nkind: url\nurl: https://example.test/page\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        encoding="utf-8",
    )
    item = inventory_raw_sources(paths, MediaDetector()).items[0]
    assert item.url_descriptor is not None
    source_id = source_id_for_url_descriptor(item.url_descriptor)
    capture = b"second captured page"
    checksum = hashlib.sha256(capture).hexdigest()
    raw_path = PurePosixPath("_web", source_id, checksum, "page.html")
    raw_file = paths.raw / raw_path
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(capture)
    raw_stat = raw_file.stat()
    registry = ExtractorRegistry.load(paths.registry)
    extractor = registry.select("text/html", PurePosixPath("page.html"))
    assert extractor is not None
    converter_id = "builtin.html"
    converter_version = "builtin:builtin.html:1"
    prerequisite = hashlib.sha256(
        f"converter-v1\0{converter_id}\0{converter_version}".encode()
    ).hexdigest()
    version = effective_extractor_version(extractor, prerequisite)
    identifier = derivation_id(
        source_sha256=checksum,
        extractor_id=extractor.extractor_id,
        extractor_version=version,
        config_sha256=extractor.config_sha256,
    )
    relative_output = derive_extraction_path(raw_path, checksum, identifier)
    output_path = PurePosixPath("sources/extracted", relative_output)
    output = repo_root / output_path
    output.parent.mkdir(parents=True)
    output.write_bytes(b'<a id="section:1"></a>\ncaptured page\n')
    output_stat = output.stat()
    content_version = ContentVersion(
        checksum,
        raw_path,
        raw_stat.st_size,
        FileFingerprint(raw_path, raw_stat.st_size, raw_stat.st_mtime_ns),
        FIXED_NOW,
        (make_retrieval_metadata(sha256=checksum, byte_size=raw_stat.st_size),),
    )
    derivation = Derivation(
        identifier,
        checksum,
        extractor.extractor_id,
        version,
        extractor.config_sha256,
        output_path,
        hashlib.sha256(output.read_bytes()).hexdigest(),
        output_stat.st_size,
        output_stat.st_mtime_ns,
        "ok",
        (Anchor("section", "1"),),
        FIXED_NOW,
        method="deterministic",
        method_metadata={
            "converter_id": converter_id,
            "converter_version": converter_version,
        },
    )
    record = SourceRecord(
        1,
        source_id,
        original_path,
        (),
        "text/html",
        raw_stat.st_size,
        SourceState.OK,
        {checksum: content_version},
        checksum,
        {identifier: derivation},
        identifier,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
        url_descriptor=UrlDescriptorMetadata(
            item.url_descriptor.url,
            item.url_descriptor.description,
            item.url_descriptor.added,
            item.fingerprint,
        ),
    )
    store = LedgerStore(paths)
    store.save(record)
    store.write_summary([record], generated_at=FIXED_NOW)
    return record


def _retain_second_url_capture(repo_root: Path, record: SourceRecord) -> SourceRecord:
    paths = RepoPaths.discover(repo_root)
    capture = b"captured page"
    checksum = hashlib.sha256(capture).hexdigest()
    assert checksum not in record.versions
    raw_path = PurePosixPath("_web", record.source_id, checksum, "second.html")
    raw_file = paths.raw / raw_path
    raw_file.parent.mkdir(parents=True)
    raw_file.write_bytes(capture)
    raw_stat = raw_file.stat()
    prior = record.derivations[record.active_derivation_id or ""]
    identifier = derivation_id(
        source_sha256=checksum,
        extractor_id=prior.extractor_id,
        extractor_version=prior.extractor_version,
        config_sha256=prior.config_sha256,
    )
    relative_output = derive_extraction_path(raw_path, checksum, identifier)
    output_path = PurePosixPath("sources/extracted", relative_output)
    output = repo_root / output_path
    output.parent.mkdir(parents=True)
    output.write_bytes(b'<a id="section:1"></a>\nsecond captured page\n')
    output_stat = output.stat()
    content_version = ContentVersion(
        checksum,
        raw_path,
        raw_stat.st_size,
        FileFingerprint(raw_path, raw_stat.st_size, raw_stat.st_mtime_ns),
        FIXED_NOW,
        (make_retrieval_metadata(sha256=checksum, byte_size=raw_stat.st_size),),
    )
    derivation = replace(
        prior,
        derivation_id=identifier,
        source_sha256=checksum,
        output_path=output_path,
        output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        output_byte_size=output_stat.st_size,
        output_mtime_ns=output_stat.st_mtime_ns,
    )
    updated = replace(
        record,
        versions={**record.versions, checksum: content_version},
        active_content_sha256=checksum,
        byte_size=raw_stat.st_size,
        derivations={**record.derivations, identifier: derivation},
        active_derivation_id=identifier,
    )
    store = LedgerStore(paths)
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    return updated


@pytest.mark.parametrize("full", (False, True))
def test_validation_preserves_each_url_derivation_under_its_version_origin(
    repo_root: Path,
    full: bool,
) -> None:
    record = _retain_second_url_capture(
        repo_root, _initialize_captured_url_record(repo_root)
    )

    assert len(record.versions) == 2
    assert len({version.sha256 for version in record.versions.values()}) == 2
    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 0, json.dumps(payload, indent=2)
    assert payload["ok"] is True


@pytest.mark.parametrize("full", (False, True))
def test_validation_rejects_url_derivation_under_another_version_origin(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: bool,
) -> None:
    record = _retain_second_url_capture(
        repo_root, _initialize_captured_url_record(repo_root)
    )
    first_id = next(
        identifier
        for identifier in record.derivations
        if identifier != record.active_derivation_id
    )
    first = record.derivations[first_id]
    second = record.versions[record.active_content_sha256 or ""]
    invalid_path = PurePosixPath(
        "sources/extracted",
        second.raw_path,
        first.source_sha256,
        first_id + ".md",
    )
    invalid_output = repo_root / invalid_path
    invalid_output.parent.mkdir(parents=True)
    (repo_root / first.output_path).rename(invalid_output)
    invalid_stat = invalid_output.stat()
    invalid_derivation = replace(
        first,
        output_path=invalid_path,
        output_mtime_ns=invalid_stat.st_mtime_ns,
    )
    invalid = replace(
        record,
        derivations={**record.derivations, first_id: invalid_derivation},
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(invalid)
    store.write_summary([invalid], generated_at=FIXED_NOW)
    observed: list[PurePosixPath] = []
    real_snapshot = inventory_module.stable_file_snapshot

    def observe(*args: object, **kwargs: object) -> object:
        observed.append(args[2])  # type: ignore[arg-type]
        return real_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("brainlib.validation.stable_file_snapshot", observe)

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 1
    assert "derivation_output_path_invalid" in json.dumps(payload)
    assert invalid_path not in observed


@pytest.mark.parametrize("full", (False, True))
def test_validation_preserves_genuine_url_descriptor_rename_provenance(
    repo_root: Path,
    full: bool,
) -> None:
    record = _initialize_captured_url_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    original_path = record.current_raw_path
    renamed_path = PurePosixPath("urls/renamed.url.md")
    (paths.raw / original_path).rename(paths.raw / renamed_path)

    sync_code, _payload = run_json(repo_root, "sync")

    assert sync_code == 0
    renamed = store.load(record.source_id)
    assert renamed.current_raw_path == renamed_path
    assert renamed.previous_raw_paths == (original_path,)
    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )
    assert return_code == 0
    assert payload["ok"] is True


def test_captured_url_change_preserves_history_and_status_gap_payload(
    repo_root: Path,
) -> None:
    record = _initialize_captured_url_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    descriptor = paths.raw / record.current_raw_path
    descriptor.write_text(
        "---\nkind: url\nurl: https://example.test/edit\n"
        "description: Example\nadded: 2026-09-04\n---\n",
        encoding="utf-8",
    )

    sync_code, sync_payload = run_json(repo_root, "sync")

    assert sync_code == 1
    assert sync_payload["data"]["status"] == "complete_with_gaps"
    updated = LedgerStore(paths).load(record.source_id)
    assert updated.source_id == record.source_id
    assert updated.state is SourceState.AWAITING_APPROVAL
    assert updated.active_content_sha256 is None
    assert updated.active_derivation_id is None
    assert updated.versions == record.versions
    assert updated.derivations == record.derivations
    validate_code, validate_payload = run_json(repo_root, "validate", "--full")
    assert validate_code == 0, json.dumps(validate_payload, indent=2)
    assert validate_payload["ok"] is True
    status_code, status_payload = run_json(repo_root, "status")
    assert status_code == 1
    assert status_payload["data"]["status"] == "complete_with_gaps"
    assert status_payload["data"]["state_counts"]["awaiting_approval"] == 1
    assert status_payload["errors"][0]["code"] == "source_coverage_gaps"
    assert "web_capture_awaiting_approval" in json.dumps(status_payload)


@pytest.mark.parametrize("full", (False, True))
def test_url_derivation_requires_retained_web_materialization_ancestry(
    repo_root: Path,
    full: bool,
) -> None:
    record = _initialize_captured_url_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    invalid_path = PurePosixPath(
        "sources/extracted",
        record.current_raw_path,
        active.source_sha256,
        active_id + ".md",
    )
    (repo_root / invalid_path).parent.mkdir(parents=True)
    (repo_root / active.output_path).rename(repo_root / invalid_path)
    output_stat = (repo_root / invalid_path).stat()
    invalid_derivation = replace(
        active,
        output_path=invalid_path,
        output_mtime_ns=output_stat.st_mtime_ns,
    )
    invalid = replace(record, derivations={active_id: invalid_derivation})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(invalid)
    store.write_summary([invalid], generated_at=FIXED_NOW)

    return_code, payload = run_json(
        repo_root, "validate", *(("--full",) if full else ())
    )

    assert return_code == 1
    assert "derivation_output_path_invalid" in json.dumps(payload)


def test_validation_rejects_noncanonical_effective_extractor_version(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    active = record.derivations[record.active_derivation_id or ""]
    malformed_version = "1+not-a-prerequisite-digest"
    malformed_id = derivation_id(
        source_sha256=active.source_sha256,
        extractor_id=active.extractor_id,
        extractor_version=malformed_version,
        config_sha256=active.config_sha256,
    )
    malformed_path = active.output_path.with_name(malformed_id + ".md")
    output = repo_root / active.output_path
    output.rename(repo_root / malformed_path)
    output_stat = (repo_root / malformed_path).stat()
    malformed = replace(
        active,
        derivation_id=malformed_id,
        extractor_version=malformed_version,
        output_path=malformed_path,
        output_mtime_ns=output_stat.st_mtime_ns,
    )
    updated = replace(
        record,
        derivations={malformed_id: malformed},
        active_derivation_id=malformed_id,
    )
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "derivation_extractor_version_invalid" in json.dumps(payload)


def test_validation_rejects_derivation_converter_not_bound_to_version_digest(
    repo_root: Path,
) -> None:
    record = initialize_one_active_record(repo_root)
    active_id = record.active_derivation_id or ""
    active = record.derivations[active_id]
    malformed = replace(
        active,
        method_metadata={
            "converter_id": "external.converter",
            "converter_version": "external converter wrong",
        },
    )
    updated = replace(record, derivations={active_id: malformed})
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)

    return_code, payload = run_json(repo_root, "validate")

    assert return_code == 1
    assert "derivation_prerequisite_mismatch" in json.dumps(payload)


def test_full_validation_preserves_old_and_new_derivations_after_registry_evolution(
    repo_root: Path,
) -> None:
    registry_path = repo_root / "config/extractors.toml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            'anchors = ["line"]', 'anchors = ["page"]', 1
        ),
        encoding="utf-8",
    )
    record = initialize_one_active_record(repo_root)
    old_id = record.active_derivation_id or ""
    old = record.derivations[old_id]
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8")
        .replace('anchors = ["page"]', 'anchors = ["line"]', 1)
        .replace("timeout_seconds = 30", "timeout_seconds = 31", 1),
        encoding="utf-8",
    )
    extractor = ExtractorRegistry.load(registry_path).select(
        "text/plain", PurePosixPath("notes/a.txt")
    )
    assert extractor is not None
    converter_id = str(old.method_metadata["converter_id"])
    converter_version = str(old.method_metadata["converter_version"])
    digest = hashlib.sha256(
        f"converter-v1\0{converter_id}\0{converter_version}".encode()
    ).hexdigest()
    new_version = effective_extractor_version(extractor, digest)
    new_id = derivation_id(
        source_sha256=old.source_sha256,
        extractor_id=extractor.extractor_id,
        extractor_version=new_version,
        config_sha256=extractor.config_sha256,
    )
    new_path = old.output_path.with_name(new_id + extractor.output_suffix)
    new_file = repo_root / new_path
    new_file.write_bytes(b'<a id="line:1"></a>\nnew policy\n')
    metadata = new_file.stat()
    new = replace(
        old,
        derivation_id=new_id,
        extractor_version=new_version,
        config_sha256=extractor.config_sha256,
        output_path=new_path,
        output_sha256=hashlib.sha256(new_file.read_bytes()).hexdigest(),
        output_byte_size=metadata.st_size,
        output_mtime_ns=metadata.st_mtime_ns,
        anchors=(Anchor("line", "1"),),
    )
    updated = replace(
        record,
        derivations={old_id: old, new_id: new},
        active_derivation_id=new_id,
    )
    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    store.save(updated)
    store.write_summary([updated], generated_at=FIXED_NOW)
    hashed: Counter[str] = Counter()

    def counted_hash(path: Path) -> str:
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        hashed[checksum] += 1
        return checksum

    report = validate_source_ledger(
        paths,
        {record.source_id: updated},
        full=True,
        checksum_cache=ChecksumCache(hash_file=counted_hash),
    )

    assert report.ok
    assert old.anchors == (Anchor("page", "1"),)
    assert new.anchors == (Anchor("line", "1"),)
    assert sum(hashed.values()) == 3
    assert hashed[old.output_sha256] == 1
    assert hashed[new.output_sha256] == 1


def test_full_validation_hashes_once_per_path_per_transaction_and_not_across_reuse(
    repo_root: Path,
) -> None:
    initialize_one_active_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    records = LedgerStore(paths).load_all()
    calls: Counter[Path] = Counter()

    def counted_hash(path: Path) -> str:
        calls[path] += 1
        return hashlib.sha256(path.read_bytes()).hexdigest()

    cache = ChecksumCache(hash_file=counted_hash)
    cache.begin_transaction()
    assert validate_source_ledger(paths, records, full=True, checksum_cache=cache).ok
    assert set(calls.values()) == {1}

    cache.begin_transaction()
    assert validate_source_ledger(paths, records, full=True, checksum_cache=cache).ok
    assert set(calls.values()) == {2}


def test_full_validation_preserves_caller_owned_checksum_transaction(
    repo_root: Path,
) -> None:
    initialize_one_active_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    records = LedgerStore(paths).load_all()
    calls = 0

    def counted_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        return hashlib.sha256(path.read_bytes()).hexdigest()

    cache = ChecksumCache(hash_file=counted_hash)
    cache.begin_transaction()
    cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("notes/a.txt"))
    assert calls == 1

    assert validate_source_ledger(
        paths,
        records,
        full=True,
        checksum_cache=cache,
    ).ok
    assert calls == 2


def test_checksum_cache_revalidates_repeated_key_and_never_reuses_across_transactions(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = paths.raw / "a.txt"
    source.write_bytes(b"first")
    hashes = 0
    identity_checks = 0
    real_check = inventory_module._open_content_is_unchanged

    def counted_hash(path: Path) -> str:
        nonlocal hashes
        hashes += 1
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def counted_check(*args: object, **kwargs: object) -> bool:
        nonlocal identity_checks
        identity_checks += 1
        return real_check(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(inventory_module, "_open_content_is_unchanged", counted_check)
    cache = ChecksumCache(hash_file=counted_hash)
    cache.begin_transaction()
    first = cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))
    repeated = cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))

    assert first == repeated
    assert hashes == 1
    assert identity_checks >= 4

    source.write_bytes(b"second")
    cache.begin_transaction()
    second = cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))

    assert second == hashlib.sha256(b"second").hexdigest()
    assert hashes == 2


def test_checksum_cache_poisoned_identity_never_certifies_replacement(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = repo_root / "sources/raw/a.txt"
    source.write_bytes(b"first")
    cache = ChecksumCache()
    cache.begin_transaction()
    first = cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))
    source.unlink()
    source.write_bytes(b"later")

    try:
        cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))
    except OSError:
        pass
    else:
        raise AssertionError("replacement was certified in the original transaction")

    assert first == hashlib.sha256(b"first").hexdigest()


def test_checksum_cache_poisoned_first_race_never_certifies_new_generation(
    repo_root: Path,
) -> None:
    paths = RepoPaths.discover(repo_root)
    source = repo_root / "sources/raw/a.txt"
    source.write_bytes(b"first")
    calls = 0

    def replace_while_hashing(path: Path) -> str:
        nonlocal calls
        calls += 1
        value = path.read_bytes()
        if calls == 1:
            source.unlink()
            source.write_bytes(b"later")
        return hashlib.sha256(value).hexdigest()

    cache = ChecksumCache(hash_file=replace_while_hashing)
    cache.begin_transaction()
    with pytest.raises(OSError):
        cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))
    with pytest.raises(OSError):
        cache.sha256(paths, SnapshotNamespace.RAW_USER, PurePosixPath("a.txt"))

    assert calls == 1
