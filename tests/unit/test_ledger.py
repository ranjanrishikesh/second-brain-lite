from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import pytest

import brainlib.ledger as ledger_module
from brainlib.contracts import (
    Anchor,
    ContentVersion,
    FileFingerprint,
    ProcessingAttempt,
    SourceRecord,
    SourceRepresentation,
    SourceState,
    UrlDescriptorMetadata,
    VersionAdoptionEvent,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic
from brainlib.inventory import InventoryItem
from brainlib.layout import RepoPaths
from brainlib.ledger import (
    CitationRewrite,
    GitHistoryResolver,
    LedgerStore,
    activate_derivation,
    adopt_version,
    derive_extraction_path,
    recover_stale_extraction,
    retry_is_eligible,
)
from brainlib.registry import ExtractorRegistry, effective_extractor_version
from tests.helpers import (
    FIXED_NOW,
    StaticResolver,
    make_integrity_inputs,
    make_retrieval_metadata,
    make_source_record,
    write_bytes,
)


def _registry(repo_root: Path) -> ExtractorRegistry:
    return ExtractorRegistry.load(repo_root / "config/extractors.toml")


def test_load_all_accepts_only_reserved_pinned_handoffs_directory(
    repo_paths: RepoPaths,
) -> None:
    store = LedgerStore(repo_paths)
    record = make_source_record()
    store.save(record)
    (repo_paths.ledger_dir / "handoffs").mkdir()
    assert store.load_all() == {record.source_id: record}


@pytest.mark.parametrize("entry", ("regular", "symlink", "arbitrary_directory"))
def test_load_all_rejects_invalid_handoff_directory_entries(
    repo_paths: RepoPaths, entry: str
) -> None:
    path = repo_paths.ledger_dir / "handoffs"
    if entry == "regular":
        path.write_text("not a manifest directory")
    elif entry == "symlink":
        path.symlink_to(repo_paths.extracted, target_is_directory=True)
    else:
        path = repo_paths.ledger_dir / "other"
        path.mkdir()
    with pytest.raises((OSError, ValueError)):
        LedgerStore(repo_paths).load_all()


def test_handoffs_directory_does_not_hide_activation_guards(
    repo_paths: RepoPaths,
) -> None:
    (repo_paths.ledger_dir / "handoffs").mkdir()
    guard = _guarded_activation(repo_paths)
    with pytest.raises(ValueError, match="activation"):
        LedgerStore(repo_paths).load_all()
    from brainlib.locking import SourceWriteLock

    with SourceWriteLock.acquire(repo_paths.lock):
        LedgerStore(repo_paths).recover_activation_guards()
    assert LedgerStore(repo_paths).load_all() == {
        guard.rollback.source_id: guard.rollback
    }


def test_load_all_keeps_handoff_directory_pinned_through_shard_read(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LedgerStore(repo_paths)
    record = make_source_record()
    store.save(record)
    handoffs = repo_paths.ledger_dir / "handoffs"
    handoffs.mkdir()
    original = ledger_module._read_regular_at

    def swap(directory_fd, name, **kwargs):
        result = original(directory_fd, name, **kwargs)
        if name == record.source_id + ".json":
            handoffs.rename(repo_paths.root / "detached-handoffs")
            handoffs.mkdir()
        return result

    monkeypatch.setattr(ledger_module, "_read_regular_at", swap)
    with pytest.raises(OSError, match="directory anchor changed"):
        store.load_all()


def test_save_writes_canonical_record_and_deterministic_summary(
    repo_root: Path,
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()

    store.save(record)
    record_path = repo_root / "sources/ledger" / f"{record.source_id}.json"
    expected = (
        json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert record_path.read_text(encoding="utf-8") == expected

    first = store.write_summary([record], generated_at=FIXED_NOW)
    second = store.write_summary([record], generated_at=FIXED_NOW)
    assert first == second
    assert first == (repo_root / "sources/ledger.md").read_text(encoding="utf-8")
    assert "Corpus revision" in first
    assert "Last synchronized: 2026-09-04T00:00:00Z" in first
    assert "ok: 1" in first
    assert f"ledger/{record.source_id}.json" in first
    assert (
        record.derivations[record.active_derivation_id or ""].output_path.as_posix()
        in first
    )


def test_malformed_activation_guard_blocks_readers_and_locked_recovery(
    repo_root: Path,
) -> None:
    import brainlib.commands as commands

    paths = RepoPaths.discover(repo_root)
    store = LedgerStore(paths)
    record = make_source_record()
    shard = store.save(record)
    original = shard.read_bytes()
    guard = paths.ledger_dir / f"{record.source_id}.activation-pending"
    guard.write_bytes(b"malformed recovery evidence")

    for read in (
        lambda: LedgerStore(paths).load(record.source_id),
        lambda: LedgerStore(paths).load_all(),
        lambda: LedgerStore(paths).active_representations(),
        lambda: LedgerStore(paths).find_representation(
            record.source_id,
            record.active_content_sha256 or "",
            record.active_derivation_id or "",
        ),
    ):
        with pytest.raises((OSError, ValueError), match="activation"):
            read()
    assert not LedgerStore(paths).is_canonical_record_current(record)
    result = commands.sync_sources(repo_root)
    assert not result.ok
    assert guard.read_bytes() == b"malformed recovery evidence"
    assert shard.read_bytes() == original


def _guarded_activation(paths: RepoPaths) -> ledger_module.ActivationGuard:
    candidate = make_source_record()
    derivation = candidate.derivations[candidate.active_derivation_id or ""]
    attempt = ProcessingAttempt(
        derivation.source_sha256,
        derivation.extractor_id,
        derivation.extractor_version,
        derivation.config_sha256,
        "f" * 64,
        SourceState.OK,
        FIXED_NOW,
        (),
    )
    candidate = replace(candidate, last_attempt=attempt)
    extracting = replace(
        candidate,
        state=SourceState.EXTRACTING,
        active_derivation_id=None,
        derivations={},
        last_attempt=replace(attempt, outcome=SourceState.EXTRACTING),
    )
    store = LedgerStore(paths)
    store.save(extracting)
    guard = ledger_module.ActivationGuard.prepare(paths, extracting, candidate)
    store.save(candidate)
    return guard


def test_activation_recovery_requires_durable_rollback_before_clearing_guard(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib.locking import SourceWriteLock

    token = _guarded_activation(repo_paths)
    shard = repo_paths.ledger_dir / (token.candidate.source_id + ".json")
    guard = repo_paths.ledger_dir / (token.candidate.source_id + ".activation-pending")
    candidate_bytes, evidence = shard.read_bytes(), guard.read_bytes()
    monkeypatch.setattr(LedgerStore, "save", lambda _store, _record: shard)
    with SourceWriteLock.acquire(repo_paths.lock):
        with pytest.raises(ValueError, match="activation.*checkpoint"):
            LedgerStore(repo_paths).recover_activation_guards()
    assert shard.read_bytes() == candidate_bytes
    assert guard.read_bytes() == evidence
    with pytest.raises(ValueError, match="activation"):
        LedgerStore(repo_paths).load_all()


@pytest.mark.parametrize(
    "tamper",
    (
        "unsafe_rollback",
        "candidate_mismatch",
        "shard_mismatch",
        "wrong_source",
        "symlink",
        "duplicate_key",
    ),
)
def test_activation_recovery_preserves_mismatched_guard_evidence(
    repo_root: Path, tamper: str
) -> None:
    import brainlib.commands as commands

    paths = RepoPaths.discover(repo_root)
    token = _guarded_activation(paths)
    guard = paths.ledger_dir / f"{token.rollback.source_id}.activation-pending"
    shard = paths.ledger_dir / f"{token.rollback.source_id}.json"
    document = json.loads(guard.read_bytes())
    if tamper == "unsafe_rollback":
        document["rollback"]["active_derivation_id"] = (
            token.candidate.active_derivation_id
        )
        guard.write_text(json.dumps(document))
    elif tamper == "candidate_mismatch":
        document["candidate"]["diagnostics"] = [
            {"code": "swapped", "message": "Swapped.", "path": None, "details": {}}
        ]
        guard.write_text(json.dumps(document))
    elif tamper == "shard_mismatch":
        LedgerStore(paths).save(
            replace(token.candidate, diagnostics=(Diagnostic("swapped", "Swapped."),))
        )
    elif tamper == "wrong_source":
        moved = paths.ledger_dir / ("src_" + "0" * 64 + ".activation-pending")
        guard.rename(moved)
        guard = moved
    elif tamper == "symlink":
        retained = paths.ledger_dir / "retained-activation-evidence"
        guard.rename(retained)
        guard.symlink_to(retained)
    else:
        guard.write_bytes(b'{"schema_version":1,' + guard.read_bytes()[1:])
    evidence = guard.read_bytes()
    original = shard.read_bytes()

    with pytest.raises((OSError, ValueError), match="activation"):
        LedgerStore(paths).load_all()
    result = commands.sync_sources(repo_root)
    assert not result.ok
    assert "activation" in result.errors[0].message
    assert guard.read_bytes() == evidence
    assert shard.read_bytes() == original


def test_guard_swapped_during_recovery_checkpoint_remains_evidence(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib.locking import SourceWriteLock

    paths = RepoPaths.discover(repo_root)
    token = _guarded_activation(paths)
    guard = paths.ledger_dir / f"{token.rollback.source_id}.activation-pending"
    evidence = guard.read_bytes()
    real_save = LedgerStore.save

    def swap_after_save(store: LedgerStore, record: SourceRecord) -> Path:
        result = real_save(store, record)
        replacement = write_bytes(guard.with_name("replacement.tmp"), evidence)
        metadata = guard.stat()
        os.utime(replacement, ns=(metadata.st_mtime_ns, metadata.st_mtime_ns))
        os.replace(replacement, guard)
        return result

    monkeypatch.setattr(LedgerStore, "save", swap_after_save)
    with SourceWriteLock.acquire(paths.lock):
        with pytest.raises(ValueError, match="activation guard changed"):
            LedgerStore(paths).recover_activation_guards()
    assert guard.read_bytes() == evidence
    with pytest.raises(ValueError, match="activation"):
        LedgerStore(paths).load_all()
    shard = paths.ledger_dir / f"{token.rollback.source_id}.json"
    assert SourceRecord.from_dict(json.loads(shard.read_bytes())) == token.rollback

    monkeypatch.setattr(LedgerStore, "save", real_save)
    with SourceWriteLock.acquire(paths.lock):
        LedgerStore(paths).recover_activation_guards()
    assert LedgerStore(paths).load(token.rollback.source_id) == token.rollback
    assert not LedgerStore(paths).active_representations()
    assert not guard.exists()


def test_summary_writer_never_exceeds_its_reader_protocol_limit(
    repo_root: Path,
) -> None:
    base = make_source_record()
    checksum = base.active_content_sha256 or ""
    derivation_id = base.active_derivation_id or ""
    records: list[SourceRecord] = []
    for index in range(1_800):
        raw_path = PurePosixPath("notes", f"{index:04d}-{'x' * 3_000}.txt")
        version = replace(
            base.versions[checksum],
            raw_path=raw_path,
            fingerprint=replace(
                base.versions[checksum].fingerprint,
                path=raw_path,
            ),
        )
        derivation = replace(
            base.derivations[derivation_id],
            output_path=PurePosixPath(
                "sources/extracted",
                *raw_path.parts,
                checksum,
                f"{derivation_id}.md",
            ),
        )
        records.append(
            replace(
                base,
                source_id=source_id_for_first_seen(raw_path, checksum),
                current_raw_path=raw_path,
                versions={checksum: version},
                derivations={derivation_id: derivation},
            )
        )

    store = LedgerStore(RepoPaths.discover(repo_root))
    summary = store.write_summary(records, generated_at=FIXED_NOW)

    assert len(summary.encode("utf-8")) <= ledger_module._MAX_LEDGER_SUMMARY_BYTES
    assert store.read_summary() == summary.encode("utf-8")
    assert "sources omitted from this bounded projection" in summary


def test_commit_proof_requires_the_exact_canonical_record_payload(
    repo_root: Path,
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    store.save(record)

    assert store.is_canonical_record_current(record) is True

    record_path = repo_root / "sources/ledger" / f"{record.source_id}.json"
    record_path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert store.load(record.source_id) == record
    assert store.is_canonical_record_current(record) is False


def test_save_cleans_temporary_file_if_atomic_replace_fails(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    monkeypatch.setattr(
        ledger_module.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no replace")),
    )

    with pytest.raises(OSError, match="no replace"):
        store.save(make_source_record())

    assert not list((repo_root / "sources/ledger").glob(".brain-tmp-*"))


def test_load_all_is_sorted_and_validates_filename_and_record_id(
    repo_root: Path,
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record_a = make_source_record()
    record_b = make_source_record(
        raw_path=PurePosixPath("notes/b.txt"),
        content_sha256="b" * 64,
    )
    store.save(record_b)
    store.save(record_a)

    assert tuple(store.load_all()) == (record_a.source_id, record_b.source_id)

    wrong = repo_root / "sources/ledger" / ("src_" + "c" * 64 + ".json")
    wrong.write_text(json.dumps(record_a.to_dict()), encoding="utf-8")
    with pytest.raises(ValueError, match="filename.*source_id"):
        store.load_all()


@pytest.mark.parametrize("entry_kind", ["symlink", "directory"])
def test_load_all_rejects_unsafe_json_entries(repo_root: Path, entry_kind: str) -> None:
    ledger_dir = repo_root / "sources/ledger"
    entry = ledger_dir / ("src_" + "c" * 64 + ".json")
    if entry_kind == "symlink":
        target = repo_root / "outside.json"
        target.write_text("{}", encoding="utf-8")
        entry.symlink_to(target)
    else:
        entry.mkdir()

    with pytest.raises(ValueError, match="regular ledger file"):
        LedgerStore(RepoPaths.discover(repo_root)).load_all()


def test_active_and_historical_representations_are_independent_and_immutable(
    repo_root: Path,
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    active_id = record.active_derivation_id or ""
    descriptor_fingerprint = FileFingerprint(record.current_raw_path, 99, 1)
    inactive = replace(
        record,
        active_content_sha256=None,
        active_derivation_id=None,
        url_descriptor=UrlDescriptorMetadata(
            "https://example.test/changed",
            "Changed URL",
            date(2026, 9, 4),
            descriptor_fingerprint,
        ),
    )
    store.save(inactive)

    assert store.active_representations() == ()
    historical = store.find_representation(record.source_id, "a" * 64, active_id)
    assert historical is not None
    assert historical.raw_path == PurePosixPath("notes/a.txt")
    assert historical.extracted_path == record.derivations[active_id].output_path
    assert historical.anchors == (Anchor("page", "1"),)
    with pytest.raises(FrozenInstanceError):
        historical.derivation_id = "drv_" + "f" * 64  # type: ignore[misc]


def test_activate_derivation_is_immutable_and_queryable(repo_root: Path) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    active = next(iter(record.derivations.values()))
    replacement = replace(
        active,
        derivation_id="drv_" + "e" * 64,
        config_sha256="f" * 64,
        output_path=PurePosixPath(
            "sources/extracted/notes/a.txt",
            "a" * 64,
            "drv_" + "e" * 64 + ".md",
        ),
    )

    activated = activate_derivation(record, replacement, now=FIXED_NOW)

    assert replacement.derivation_id not in record.derivations
    assert activated is not record
    assert activated.derivations[replacement.derivation_id] == replacement
    assert activated.active_derivation_id == replacement.derivation_id
    assert activated.state is SourceState.OK
    store.save(activated)
    assert (
        store.find_representation(
            activated.source_id,
            activated.active_content_sha256 or "",
            activated.active_derivation_id or "",
        )
        is not None
    )
    assert (
        store.find_representation(activated.source_id, "a" * 64, active.derivation_id)
        is not None
    )


def test_activate_derivation_validates_cross_link_and_sets_warning_state() -> None:
    record = make_source_record()
    active = next(iter(record.derivations.values()))
    invalid = replace(active, source_sha256="f" * 64)
    with pytest.raises(ValueError, match="retained content version"):
        activate_derivation(record, invalid, now=FIXED_NOW)

    warning = replace(
        active,
        derivation_id="drv_" + "e" * 64,
        quality_state="warning",
        output_path=PurePosixPath(
            "sources/extracted/notes/a.txt",
            "a" * 64,
            "drv_" + "e" * 64 + ".md",
        ),
    )
    assert (
        activate_derivation(record, warning, now=FIXED_NOW).state is SourceState.WARNING
    )


def test_activate_derivation_does_not_silently_switch_active_content() -> None:
    record = make_source_record()
    active = next(iter(record.derivations.values()))
    historical_sha = "f" * 64
    source_id = source_id_for_first_seen(record.current_raw_path, historical_sha)
    historical = ContentVersion(
        historical_sha,
        PurePosixPath("_versions", source_id, historical_sha, "a.txt"),
        3,
        FileFingerprint(
            PurePosixPath("_versions", source_id, historical_sha, "a.txt"),
            3,
            1,
        ),
        FIXED_NOW,
        (),
    )
    historical_derivation = replace(
        active,
        derivation_id="drv_" + "f" * 64,
        source_sha256=historical_sha,
        output_path=PurePosixPath(
            "sources/extracted/notes/a.txt",
            historical_sha,
            "drv_" + "f" * 64 + ".md",
        ),
    )
    record = replace(
        record,
        source_id=source_id,
        versions={historical_sha: historical, **record.versions},
        adoption_events=(
            VersionAdoptionEvent(
                historical_sha,
                record.active_content_sha256 or "",
                "Approved current version.",
                FIXED_NOW,
            ),
        ),
    )

    with pytest.raises(ValueError, match="active content"):
        activate_derivation(record, historical_derivation, now=FIXED_NOW)


def test_extraction_path_keeps_complete_basename_and_hierarchy() -> None:
    assert derive_extraction_path(
        PurePosixPath("nested/example.txt"), "a" * 64, "drv_" + "b" * 64
    ) == PurePosixPath("nested/example.txt", "a" * 64, "drv_" + "b" * 64 + ".md")


def test_retry_eligibility_uses_only_exact_selected_extractor_identity(
    repo_root: Path,
) -> None:
    extractor = _registry(repo_root).select("text/plain", "notes/a.txt")
    assert extractor is not None
    prerequisite = "e" * 64
    attempt = ProcessingAttempt(
        "a" * 64,
        extractor.extractor_id,
        effective_extractor_version(extractor, prerequisite),
        extractor.config_sha256,
        prerequisite,
        SourceState.FAILED,
        FIXED_NOW,
        ("conversion_failed",),
    )
    record = replace(
        make_source_record(), state=SourceState.FAILED, last_attempt=attempt
    )

    assert not retry_is_eligible(
        record,
        input_sha256="a" * 64,
        extractor=extractor,
        prerequisite_digest=prerequisite,
    )
    assert retry_is_eligible(
        replace(record, state=SourceState.EXTRACTING),
        input_sha256="a" * 64,
        extractor=extractor,
        prerequisite_digest=prerequisite,
    )
    assert retry_is_eligible(
        record,
        input_sha256="b" * 64,
        extractor=extractor,
        prerequisite_digest=prerequisite,
    )
    assert retry_is_eligible(
        record,
        input_sha256="a" * 64,
        extractor=extractor,
        prerequisite_digest="f" * 64,
    )
    assert not retry_is_eligible(
        record,
        input_sha256="a" * 64,
        extractor=extractor,
        prerequisite_digest=prerequisite,
    )


def test_adoption_materializes_prior_bytes_before_new_version_activation(
    repo_root: Path,
) -> None:
    record, changed_item, old_sha = make_integrity_inputs(repo_root)

    adoption = adopt_version(
        record,
        changed_item,
        paths=RepoPaths.discover(repo_root),
        resolver=StaticResolver(b"old"),
        approval_note="User approved replacement in chat",
        now=FIXED_NOW,
    )

    archived = (
        repo_root / "sources/raw/_versions" / record.source_id / old_sha / "note.txt"
    )
    archive_path = PurePosixPath("_versions", record.source_id, old_sha, "note.txt")
    assert archived.read_bytes() == b"old"
    assert adoption.record.active_content_sha256 == changed_item.sha256
    assert adoption.record.versions[old_sha].raw_path == archive_path
    assert adoption.record.versions[old_sha].fingerprint.path == archive_path
    assert adoption.record.versions[old_sha].retrieval_events == ()
    assert adoption.record.active_derivation_id is None
    assert adoption.record.state is SourceState.PENDING
    assert adoption.citation_rewrites == (
        CitationRewrite(record.source_id, old_sha, archive_path),
    )
    assert adoption.record.adoption_events[-1] == VersionAdoptionEvent(
        old_sha,
        changed_item.sha256 or "",
        "User approved replacement in chat",
        FIXED_NOW,
    )


def test_adoption_preserves_retrieval_bytes_and_unrelated_diagnostics(
    repo_root: Path,
) -> None:
    record, changed_item, old_sha = make_integrity_inputs(repo_root)
    retrieval = replace(make_retrieval_metadata(), sha256=old_sha, byte_size=3)
    old_version = replace(record.versions[old_sha], retrieval_events=(retrieval,))
    record = replace(
        record,
        versions={old_sha: old_version},
        diagnostics=record.diagnostics + (Diagnostic("manual_warning", "keep me"),),
    )

    adoption = adopt_version(
        record,
        changed_item,
        paths=RepoPaths.discover(repo_root),
        resolver=StaticResolver(b"old"),
        approval_note="Approved",
        now=FIXED_NOW,
    )

    assert adoption.record.versions[old_sha].retrieval_events == (retrieval,)
    assert adoption.record.versions[changed_item.sha256 or ""].retrieval_events == ()
    assert adoption.record.diagnostics == (Diagnostic("manual_warning", "keep me"),)


def test_adoption_requires_integrity_state_nonempty_approval_and_exact_candidate(
    repo_root: Path,
) -> None:
    record, changed_item, _ = make_integrity_inputs(repo_root)
    paths = RepoPaths.discover(repo_root)
    with pytest.raises(ValueError, match="integrity_error"):
        adopt_version(
            replace(record, state=SourceState.WARNING),
            changed_item,
            paths=paths,
            resolver=StaticResolver(b"old"),
            approval_note="Approved",
            now=FIXED_NOW,
        )
    with pytest.raises(ValueError, match="approval note"):
        adopt_version(
            record,
            changed_item,
            paths=paths,
            resolver=StaticResolver(b"old"),
            approval_note=" ",
            now=FIXED_NOW,
        )
    with pytest.raises(ValueError, match="candidate checksum"):
        adopt_version(
            record,
            changed_item,
            paths=paths,
            resolver=StaticResolver(b"old"),
            approval_note="Approved",
            candidate_sha256="f" * 64,
            now=FIXED_NOW,
        )


def test_adoption_rechecks_live_bytes_and_observed_fingerprint(repo_root: Path) -> None:
    record, changed_item, _ = make_integrity_inputs(repo_root)
    live = repo_root / "sources/raw/notes/note.txt"
    live.write_bytes(b"changed again")

    with pytest.raises(ValueError, match="observed inventory item"):
        adopt_version(
            record,
            changed_item,
            paths=RepoPaths.discover(repo_root),
            resolver=StaticResolver(b"old"),
            approval_note="Approved",
            now=FIXED_NOW,
        )

    assert not (repo_root / "sources/raw/_versions" / record.source_id).exists()


def test_adoption_blocks_when_prior_bytes_cannot_be_proven(repo_root: Path) -> None:
    record, changed_item, _ = make_integrity_inputs(repo_root)
    with pytest.raises(ValueError, match="cannot recover prior exact bytes"):
        adopt_version(
            record,
            changed_item,
            paths=RepoPaths.discover(repo_root),
            resolver=StaticResolver(None),
            approval_note="User approved",
            now=FIXED_NOW,
        )


def test_adoption_never_overwrites_mismatching_archive(repo_root: Path) -> None:
    record, changed_item, old_sha = make_integrity_inputs(repo_root)
    archive = write_bytes(
        repo_root / "sources/raw/_versions" / record.source_id / old_sha / "note.txt",
        b"wrong",
    )

    with pytest.raises(ValueError, match="archive.*checksum"):
        adopt_version(
            record,
            changed_item,
            paths=RepoPaths.discover(repo_root),
            resolver=StaticResolver(b"old"),
            approval_note="Approved",
            now=FIXED_NOW,
        )

    assert archive.read_bytes() == b"wrong"


def test_git_history_resolver_uses_repo_relative_raw_path(repo_root: Path) -> None:
    subprocess.run(("git", "init"), cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ("git", "config", "user.email", "fixture@example.test"),
        cwd=repo_root,
        check=True,
    )
    subprocess.run(("git", "config", "user.name", "Fixture"), cwd=repo_root, check=True)
    original = write_bytes(repo_root / "sources/raw/notes/history.txt", b"old")
    subprocess.run(
        ("git", "add", "sources/raw/notes/history.txt"), cwd=repo_root, check=True
    )
    subprocess.run(
        ("git", "commit", "-m", "fixture"),
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    original.write_bytes(b"new")
    checksum = hashlib.sha256(b"old").hexdigest()

    assert (
        GitHistoryResolver(repo_root).read_exact(
            "src_" + "a" * 64, PurePosixPath("notes/history.txt"), checksum
        )
        == b"old"
    )


def test_git_history_resolver_rejects_paths_and_archive_symlinks(
    repo_root: Path,
) -> None:
    resolver = GitHistoryResolver(repo_root)
    checksum = hashlib.sha256(b"outside").hexdigest()
    with pytest.raises(ValueError, match="raw_path"):
        resolver.read_exact("src_" + "a" * 64, PurePosixPath("../outside"), checksum)

    outside = write_bytes(repo_root / "outside", b"outside")
    archive = (
        repo_root
        / "sources/raw/_versions"
        / ("src_" + "a" * 64)
        / checksum
        / "note.txt"
    )
    archive.parent.mkdir(parents=True)
    archive.symlink_to(outside)
    with pytest.raises(ValueError, match="regular archive file"):
        resolver.read_exact(
            "src_" + "a" * 64, PurePosixPath("notes/note.txt"), checksum
        )


def test_representation_has_canonical_consumer_shape_and_derivation_integrity(
    repo_root: Path,
) -> None:
    record = make_source_record()
    LedgerStore(RepoPaths.discover(repo_root)).save(record)
    representation = LedgerStore(RepoPaths.discover(repo_root)).find_representation(
        record.source_id,
        record.active_content_sha256 or "",
        record.active_derivation_id or "",
    )
    assert representation is not None
    assert tuple(field.name for field in fields(SourceRepresentation)) == (
        "source_id",
        "content_sha256",
        "derivation_id",
        "raw_path",
        "extracted_path",
        "output_sha256",
        "quality_state",
        "anchors",
    )
    derivation = record.derivations[record.active_derivation_id or ""]
    assert representation.extracted_path == derivation.output_path
    assert representation.output_sha256 == derivation.output_sha256
    with pytest.raises(FrozenInstanceError):
        representation.output_sha256 = "f" * 64  # type: ignore[misc]


def test_transition_matches_keyword_only_consumer_contract() -> None:
    signature = inspect.signature(ledger_module.transition)
    assert signature.parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["diagnostics"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["diagnostics"].default == ()

    with pytest.raises(TypeError):
        ledger_module.transition(  # type: ignore[misc]
            make_source_record(), SourceState.WARNING, FIXED_NOW
        )


@pytest.mark.parametrize("diagnostic_input", ["list", "generator", "one-shot"])
def test_transition_freezes_any_diagnostic_iterable_exactly_once(
    diagnostic_input: str,
) -> None:
    record = make_source_record()
    diagnostic = Diagnostic("coverage_gap", "Needs attention")
    iterations: list[int] = []

    class OneShotDiagnostics:
        def __iter__(self):
            iterations.append(1)
            if len(iterations) > 1:
                raise AssertionError("diagnostics iterable was consumed more than once")
            return iter((diagnostic,))

    diagnostics: object
    if diagnostic_input == "list":
        diagnostics = [diagnostic]
    elif diagnostic_input == "generator":
        diagnostics = (value for value in (diagnostic,))
    else:
        diagnostics = OneShotDiagnostics()

    transitioned = ledger_module.transition(
        record,
        SourceState.WARNING,
        now=FIXED_NOW,
        diagnostics=diagnostics,  # type: ignore[arg-type]
    )

    assert transitioned is not record
    assert transitioned.state is SourceState.WARNING
    assert transitioned.diagnostics == (diagnostic,)
    assert transitioned.updated_at == FIXED_NOW
    assert record.state is SourceState.OK
    assert record.diagnostics == ()
    if diagnostic_input == "one-shot":
        assert len(iterations) == 1
    with pytest.raises(ValueError, match="SourceState"):
        ledger_module.transition(
            record,
            "warning",  # type: ignore[arg-type]
            now=FIXED_NOW,
        )


def test_stale_extraction_recovery_is_immutable_and_preserves_history() -> None:
    retained = Diagnostic("existing_warning", "Keep this diagnostic")
    record = replace(
        make_source_record(),
        state=SourceState.EXTRACTING,
        diagnostics=(retained,),
    )

    recovered = recover_stale_extraction(record, now=FIXED_NOW)

    assert recovered is not record
    assert recovered.state is SourceState.PENDING
    assert recovered.updated_at == FIXED_NOW
    assert recovered.diagnostics[:-1] == (retained,)
    assert recovered.diagnostics[-1] == Diagnostic(
        "stale_extracting_recovered",
        "Recovered an interrupted extraction; source is pending retry.",
    )
    assert recovered.versions == record.versions
    assert recovered.derivations == record.derivations
    assert recovered.last_attempt == record.last_attempt
    assert record.state is SourceState.EXTRACTING
    assert record.diagnostics == (retained,)


def test_stale_extraction_recovery_is_idempotent_only_for_its_pending_result() -> None:
    extracting = replace(make_source_record(), state=SourceState.EXTRACTING)
    recovered = recover_stale_extraction(extracting, now=FIXED_NOW)

    assert recover_stale_extraction(recovered, now=FIXED_NOW) is recovered
    future = FIXED_NOW.replace(year=2027)
    assert recover_stale_extraction(recovered, now=future) is recovered
    with pytest.raises(ValueError):
        recover_stale_extraction(recovered, now=datetime(2026, 9, 4))
    with pytest.raises(ValueError, match="extracting"):
        recover_stale_extraction(make_source_record(), now=FIXED_NOW)
    with pytest.raises(ValueError):
        recover_stale_extraction(
            extracting,
            now=datetime(2026, 9, 4),
        )
    with pytest.raises(ValueError):
        recover_stale_extraction(
            replace(extracting, byte_size=-1),
            now=FIXED_NOW,
        )


@pytest.mark.parametrize(
    "diagnostics",
    [
        (Diagnostic("stale_extracting_recovered", "wrong message"),),
        (
            Diagnostic(
                "stale_extracting_recovered",
                "Recovered an interrupted extraction; source is pending retry.",
                PurePosixPath("notes/a.txt"),
            ),
        ),
        (
            Diagnostic(
                "stale_extracting_recovered",
                "Recovered an interrupted extraction; source is pending retry.",
                details={"wrong": True},
            ),
        ),
        (
            Diagnostic(
                "stale_extracting_recovered",
                "Recovered an interrupted extraction; source is pending retry.",
            ),
            Diagnostic("later", "marker is not final"),
        ),
        (
            Diagnostic(
                "stale_extracting_recovered",
                "Recovered an interrupted extraction; source is pending retry.",
            ),
            Diagnostic(
                "stale_extracting_recovered",
                "Recovered an interrupted extraction; source is pending retry.",
            ),
        ),
    ],
)
def test_pending_recovery_rejects_noncanonical_marker_shapes(
    diagnostics: tuple[Diagnostic, ...],
) -> None:
    pending = replace(
        make_source_record(),
        state=SourceState.PENDING,
        diagnostics=diagnostics,
    )

    with pytest.raises(ValueError, match="canonical recovery diagnostic"):
        recover_stale_extraction(pending, now=FIXED_NOW)


def test_extracting_recovery_rejects_preexisting_marker_code_collision() -> None:
    extracting = replace(
        make_source_record(),
        state=SourceState.EXTRACTING,
        diagnostics=(Diagnostic("stale_extracting_recovered", "collision"),),
    )

    with pytest.raises(ValueError, match="colliding recovery diagnostic"):
        recover_stale_extraction(extracting, now=FIXED_NOW)


@pytest.mark.parametrize(
    "changed",
    [
        {"output_path": PurePosixPath("sources/extracted/other.md")},
        {"output_sha256": "f" * 64},
        {"anchors": (Anchor("line", "9"),)},
        {
            "method_metadata": {
                "converter_id": "builtin.text",
                "converter_version": "different",
            }
        },
    ],
)
def test_activate_derivation_rejects_same_identity_with_changed_history(
    changed: dict[str, object],
) -> None:
    record = make_source_record()
    derivation = record.derivations[record.active_derivation_id or ""]
    collision = replace(derivation, **changed)

    with pytest.raises(ValueError, match="derivation_id collision"):
        activate_derivation(record, collision, now=FIXED_NOW)

    assert record.derivations[derivation.derivation_id] == derivation


def test_activate_derivation_allows_exact_idempotent_identity() -> None:
    record = make_source_record()
    derivation = record.derivations[record.active_derivation_id or ""]

    activated = activate_derivation(record, derivation, now=FIXED_NOW)

    assert activated.derivations[derivation.derivation_id] is derivation
    assert activated is not record


def test_summary_percent_encodes_destinations_and_escapes_hostile_display(
    repo_root: Path,
) -> None:
    raw_path = PurePosixPath("notes/a space (β)#%`\n.txt")
    record = make_source_record()
    checksum = record.active_content_sha256 or ""
    derivation_id = record.active_derivation_id or ""
    version = replace(
        record.versions[checksum],
        raw_path=raw_path,
        fingerprint=replace(record.versions[checksum].fingerprint, path=raw_path),
    )
    extracted_path = PurePosixPath(
        "sources/extracted/notes/a space (β)#%`\n.txt", checksum, f"{derivation_id}.md"
    )
    derivation = replace(record.derivations[derivation_id], output_path=extracted_path)
    hostile = replace(
        record,
        source_id=source_id_for_first_seen(raw_path, checksum),
        current_raw_path=raw_path,
        versions={checksum: version},
        derivations={derivation_id: derivation},
        state=SourceState.WARNING,
        diagnostics=(Diagnostic("bad`]\n#", "unsafe display"),),
    )

    summary = LedgerStore(RepoPaths.discover(repo_root)).render_summary(
        [hostile], generated_at=FIXED_NOW
    )

    source_id = hostile.source_id
    assert f"ledger/{source_id}.json" in summary
    assert "raw/notes/a%20space%20%28%CE%B2%29%23%25%60%0A.txt" in summary
    assert (
        "extracted/notes/a%20space%20%28%CE%B2%29%23%25%60%0A.txt/"
        f"{checksum}/{derivation_id}.md"
    ) in summary
    assert "<code>notes/a space (β)#%`\\x0a.txt</code>" in summary
    assert "<code>bad`&#93;\\x0a#</code>" in summary
    assert "bad`]\n#" not in summary


class _MappingResolver:
    def __init__(
        self, values: dict[str, bytes], callback: object | None = None
    ) -> None:
        self.values = values
        self.callback = callback

    def read_exact(
        self, source_id: str, raw_path: PurePosixPath, sha256: str
    ) -> bytes | None:
        del source_id, raw_path
        if callable(self.callback):
            self.callback(sha256)
        return self.values.get(sha256)


def _add_historical_version(record: object, content: bytes, name: str = "older.txt"):
    assert hasattr(record, "versions")
    assert hasattr(record, "active_content_sha256")
    assert hasattr(record, "current_raw_path")
    checksum = hashlib.sha256(content).hexdigest()
    path = PurePosixPath("notes", name)
    version = ContentVersion(
        checksum,
        path,
        len(content),
        FileFingerprint(path, len(content), 1),
        FIXED_NOW,
        (),
    )
    active_sha256 = record.active_content_sha256
    assert isinstance(active_sha256, str)
    return (
        replace(
            record,
            source_id=source_id_for_first_seen(record.current_raw_path, checksum),
            versions={checksum: version, **record.versions},
            adoption_events=(
                VersionAdoptionEvent(
                    checksum,
                    active_sha256,
                    "Approved retained version.",
                    FIXED_NOW,
                ),
            ),
        ),
        checksum,
    )


def test_adoption_rechecks_live_candidate_after_resolver_work(repo_root: Path) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    live = repo_root / "sources/raw/notes/note.txt"

    def mutate_live(_sha256: str) -> None:
        live.write_bytes(b"changed after initial read")

    with pytest.raises(ValueError, match="live source changed during adoption"):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=_MappingResolver({old_sha: b"old"}, mutate_live),
            approval_note="Approved",
            now=FIXED_NOW,
        )


def test_adoption_materializes_and_finally_verifies_multiple_versions(
    repo_root: Path,
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    record, older_sha = _add_historical_version(record, b"older")

    adoption = adopt_version(
        record,
        item,
        paths=RepoPaths.discover(repo_root),
        resolver=_MappingResolver({old_sha: b"old", older_sha: b"older"}),
        approval_note="Approved",
        now=FIXED_NOW,
    )

    assert tuple(
        rewrite.content_sha256 for rewrite in adoption.citation_rewrites
    ) == tuple(sorted((old_sha, older_sha)))
    for checksum in (old_sha, older_sha):
        version = adoption.record.versions[checksum]
        archived = repo_root / "sources/raw" / version.raw_path
        metadata = archived.stat()
        assert version.fingerprint == FileFingerprint(
            version.raw_path, metadata.st_size, metadata.st_mtime_ns
        )


@pytest.mark.parametrize("substitution", ["wrong-file", "symlink", "fifo"])
def test_adoption_final_pass_rejects_earlier_archive_substitution(
    repo_root: Path, substitution: str
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    record, older_sha = _add_historical_version(record, b"older")
    archive = write_bytes(
        repo_root / "sources/raw/_versions" / record.source_id / old_sha / "note.txt",
        b"old",
    )
    outside = write_bytes(repo_root / "outside", b"old")

    def substitute(checksum: str) -> None:
        if checksum != older_sha:
            return
        archive.unlink()
        if substitution == "wrong-file":
            archive.write_bytes(b"wrong")
        elif substitution == "symlink":
            archive.symlink_to(outside)
        else:
            os.mkfifo(archive)

    with pytest.raises((OSError, ValueError), match="archive|regular"):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=_MappingResolver({older_sha: b"older"}, substitute),
            approval_note="Approved",
            now=FIXED_NOW,
        )


def test_adoption_rejects_same_fingerprint_current_name_replacement(
    repo_root: Path,
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    live = repo_root / "sources/raw/notes/note.txt"
    original_mtime = item.fingerprint.mtime_ns

    def replace_name(_checksum: str) -> None:
        replacement = live.with_name("replacement")
        replacement.write_bytes(b"new")
        os.utime(replacement, ns=(original_mtime, original_mtime))
        os.replace(replacement, live)

    with pytest.raises(ValueError, match="live source changed during adoption"):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=_MappingResolver({old_sha: b"old"}, replace_name),
            approval_note="Approved",
            now=FIXED_NOW,
        )


def test_archive_directory_entries_are_fsynced_in_creation_order(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    events: list[tuple[str, int]] = []
    real_mkdir = ledger_module.os.mkdir
    real_fsync_directory = ledger_module._fsync_directory

    def observed_mkdir(path: str, *args: object, **kwargs: object) -> None:
        real_mkdir(path, *args, **kwargs)
        directory_fd = kwargs.get("dir_fd")
        assert isinstance(directory_fd, int)
        events.append(("mkdir", os.fstat(directory_fd).st_ino))

    def observed_fsync(directory_fd: int) -> None:
        events.append(("fsync", os.fstat(directory_fd).st_ino))
        real_fsync_directory(directory_fd)

    monkeypatch.setattr(ledger_module.os, "mkdir", observed_mkdir)
    monkeypatch.setattr(ledger_module, "_fsync_directory", observed_fsync)

    adopt_version(
        record,
        item,
        paths=RepoPaths.discover(repo_root),
        resolver=_MappingResolver({old_sha: b"old"}),
        approval_note="Approved",
        now=FIXED_NOW,
    )

    mkdir_indexes = [index for index, event in enumerate(events) if event[0] == "mkdir"]
    assert len(mkdir_indexes) == 2
    for index in mkdir_indexes:
        assert events[index + 1] == ("fsync", events[index][1])
    assert events[-1][0] == "fsync"


def _leave_exact_archive_after_failed_directory_sync(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SourceRecord, InventoryItem, str, Path]:
    record, item, old_sha = make_integrity_inputs(repo_root)
    archive = (
        repo_root / "sources/raw/_versions" / record.source_id / old_sha / "note.txt"
    )
    real_fsync_directory = ledger_module._fsync_directory

    def fail_post_link_directory_sync(directory_fd: int) -> None:
        if (
            archive.exists()
            and os.fstat(directory_fd).st_ino == archive.parent.stat().st_ino
        ):
            raise OSError("first post-link directory sync failed")
        real_fsync_directory(directory_fd)

    monkeypatch.setattr(
        ledger_module, "_fsync_directory", fail_post_link_directory_sync
    )
    with pytest.raises(
        ledger_module.PublishedWriteError,
        match="first post-link directory sync failed",
    ):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=_MappingResolver({old_sha: b"old"}),
            approval_note="Approved",
            now=FIXED_NOW,
        )
    monkeypatch.setattr(ledger_module, "_fsync_directory", real_fsync_directory)
    assert archive.read_bytes() == b"old"
    assert not tuple(archive.parent.glob(".brain-tmp-*"))
    return record, item, old_sha, archive


def test_retry_durably_syncs_an_exact_archive_left_by_failed_publication(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, item, _old_sha, archive = _leave_exact_archive_after_failed_directory_sync(
        repo_root, monkeypatch
    )
    archive_identity = (archive.stat().st_dev, archive.stat().st_ino)
    archive_parent_identity = (
        archive.parent.stat().st_dev,
        archive.parent.stat().st_ino,
    )
    real_fsync = ledger_module.os.fsync
    sync_events: list[str] = []

    def observe_archive_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity == archive_identity:
            sync_events.append("archive-file")
        elif identity == archive_parent_identity:
            sync_events.append("archive-directory")
        real_fsync(descriptor)

    monkeypatch.setattr(ledger_module.os, "fsync", observe_archive_sync)

    adoption = adopt_version(
        record,
        item,
        paths=RepoPaths.discover(repo_root),
        resolver=StaticResolver(None),
        approval_note="Approved on retry",
        now=FIXED_NOW,
    )

    assert adoption.record.state is SourceState.PENDING
    assert sync_events.index("archive-file") < sync_events.index("archive-directory")
    assert not tuple(archive.parent.glob(".brain-tmp-*"))


@pytest.mark.parametrize("retry_failure", ["archive-file", "archive-directory"])
def test_retry_propagates_existing_archive_sync_failure_before_returning_record(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_failure: str,
) -> None:
    record, item, _old_sha, archive = _leave_exact_archive_after_failed_directory_sync(
        repo_root, monkeypatch
    )
    archive_identity = (archive.stat().st_dev, archive.stat().st_ino)
    archive_parent_identity = (
        archive.parent.stat().st_dev,
        archive.parent.stat().st_ino,
    )
    real_fsync = ledger_module.os.fsync
    real_fsync_directory = ledger_module._fsync_directory

    def fail_archive_file_sync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == archive_identity:
            raise OSError("retry archive file sync failed")
        real_fsync(descriptor)

    def fail_archive_directory_sync(directory_fd: int) -> None:
        metadata = os.fstat(directory_fd)
        if (metadata.st_dev, metadata.st_ino) == archive_parent_identity:
            raise OSError("retry archive directory sync failed")
        real_fsync_directory(directory_fd)

    if retry_failure == "archive-file":
        monkeypatch.setattr(ledger_module.os, "fsync", fail_archive_file_sync)
    else:
        monkeypatch.setattr(
            ledger_module, "_fsync_directory", fail_archive_directory_sync
        )

    with pytest.raises(OSError, match=f"retry {retry_failure.replace('-', ' ')} sync"):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=StaticResolver(None),
            approval_note="Approved on retry",
            now=FIXED_NOW,
        )

    assert archive.read_bytes() == b"old"
    assert not tuple(archive.parent.glob(".brain-tmp-*"))


def test_atomic_file_and_directory_fsync_failures_clean_temporary(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    real_fsync = ledger_module.os.fsync
    calls = 0

    def fail_file_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("file fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(ledger_module.os, "fsync", fail_file_fsync)
    with pytest.raises(OSError, match="file fsync failed"):
        store.save(make_source_record())
    assert not list((repo_root / "sources/ledger").glob(".brain-tmp-*"))

    monkeypatch.setattr(ledger_module.os, "fsync", real_fsync)
    monkeypatch.setattr(
        ledger_module,
        "_fsync_directory",
        lambda _descriptor: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )
    with pytest.raises(OSError, match="directory fsync failed"):
        store.save(make_source_record())
    assert not list((repo_root / "sources/ledger").glob(".brain-tmp-*"))


class _ScriptedPopen:
    def __init__(
        self,
        original: object,
        scripts: list[tuple[bytes, bytes, int, float]],
    ) -> None:
        self.original = original
        self.scripts = scripts
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.processes: list[object] = []

    def __call__(self, argv: list[str], **kwargs: object):
        self.calls.append((argv, kwargs))
        stdout, stderr, exit_code, delay = self.scripts.pop(0)

        def bytes_expression(value: bytes) -> str:
            if value and len(set(value)) == 1:
                return f"bytes([{value[0]}])*{len(value)}"
            return f"base64.b64decode({base64.b64encode(value)!r})"

        program = (
            "import base64,sys,time;"
            f"time.sleep({delay!r});"
            f"sys.stdout.buffer.write({bytes_expression(stdout)});"
            f"sys.stderr.buffer.write({bytes_expression(stderr)});"
            f"raise SystemExit({exit_code})"
        )
        process = self.original(
            [sys.executable, "-c", program],
            cwd=kwargs.get("cwd"),
            env=kwargs.get("env"),
            stdin=kwargs.get("stdin"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            shell=False,
            start_new_session=bool(kwargs.get("start_new_session", False)),
        )
        self.processes.append(process)
        return process


def test_git_history_resolver_uses_bounded_literal_argv_and_history_order(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRAIN_GIT_TEST_INHERITED", "present")
    revision_a = "a" * 40
    revision_b = "b" * 40
    router = _ScriptedPopen(
        subprocess.Popen,
        [
            (f"{revision_a}\n{revision_b}\n".encode(), b"x" * 1_000_000, 0, 0),
            (b"not old", b"ignored", 0, 0),
            (b"old", b"ignored", 0, 0),
        ],
    )
    monkeypatch.setattr(ledger_module.subprocess, "Popen", router)
    checksum = hashlib.sha256(b"old").hexdigest()

    assert (
        GitHistoryResolver(repo_root).read_exact(
            "src_" + "a" * 64, PurePosixPath("notes/a space.txt"), checksum
        )
        == b"old"
    )

    assert [call[0] for call in router.calls] == [
        ["git", "rev-list", "--all", "--", "sources/raw/notes/a space.txt"],
        ["git", "show", f"{revision_a}:sources/raw/notes/a space.txt", "--"],
        ["git", "show", f"{revision_b}:sources/raw/notes/a space.txt", "--"],
    ]
    assert all(call[1]["shell"] is False for call in router.calls)
    assert all(call[1]["stderr"] is subprocess.DEVNULL for call in router.calls)
    for _argv, options in router.calls:
        environment = options["env"]
        assert isinstance(environment, dict)
        assert environment["BRAIN_GIT_TEST_INHERITED"] == "present"
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        if os.name == "posix":
            assert options["start_new_session"] is True


@pytest.mark.parametrize(
    "timeout_seconds",
    [0, -1, math.nan, math.inf, -math.inf],
)
def test_git_history_resolver_rejects_nonpositive_or_nonfinite_deadline(
    repo_root: Path, timeout_seconds: float
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        GitHistoryResolver(repo_root, timeout_seconds=timeout_seconds)


def test_git_runner_never_waits_unboundedly_for_a_stubborn_leader(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubbornProcess:
        def __init__(self) -> None:
            self.args = ["git", "rev-list"]
            self.pid = 999_999_999
            self.stdout = io.BytesIO()
            self.returncode = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            if timeout is None:
                raise AssertionError("caller attempted an unbounded wait")
            raise subprocess.TimeoutExpired(self.args, timeout)

    process = StubbornProcess()
    monkeypatch.setattr(
        ledger_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    started = time.monotonic()

    result = ledger_module._run_git_bounded(
        ["git", "rev-list", "--all"],
        cwd=repo_root,
        deadline=started + 0.03,
        max_stdout_bytes=1024,
    )

    assert result is None
    assert time.monotonic() - started < 0.2


def _terminate_controlled_git_group(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    try:
        direct_pid, descendant_pid = (
            int(value) for value in pid_path.read_text().split()
        )
    except (OSError, ValueError):
        return
    for pid in (direct_pid, descendant_pid):
        try:
            process_group = os.getpgid(pid)
        except OSError:
            continue
        if process_group != direct_pid:
            continue
        try:
            os.killpg(direct_pid, signal.SIGKILL)
        except OSError:
            pass
        return


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_controlled_git_cleanup_does_not_signal_reused_or_unverifiable_pids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    direct_pid = 41_001
    descendant_pid = 41_002
    pid_path = tmp_path / "reused-git-pids"
    pid_path.write_text(f"{direct_pid} {descendant_pid}")
    signals: list[tuple[str, int, int]] = []

    def current_process_group(pid: int) -> int:
        if pid == direct_pid:
            return 51_001
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "getpgid", current_process_group)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, signal_number: signals.append(
            ("group", process_group, signal_number)
        ),
    )
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, signal_number: signals.append(("process", pid, signal_number)),
    )

    _terminate_controlled_git_group(pid_path)

    assert signals == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_controlled_git_cleanup_signals_group_after_live_descendant_verifies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    direct_pid = 42_001
    descendant_pid = 42_002
    pid_path = tmp_path / "live-git-pids"
    pid_path.write_text(f"{direct_pid} {descendant_pid}")
    verified_process_groups: set[int] = set()
    signals: list[tuple[str, int, int]] = []

    def current_process_group(pid: int) -> int:
        if pid == direct_pid:
            raise ProcessLookupError(pid)
        verified_process_groups.add(direct_pid)
        return direct_pid

    def record_group_signal(process_group: int, signal_number: int) -> None:
        assert process_group in verified_process_groups
        signals.append(("group", process_group, signal_number))

    monkeypatch.setattr(os, "getpgid", current_process_group)
    monkeypatch.setattr(os, "killpg", record_group_signal)
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, signal_number: signals.append(("process", pid, signal_number)),
    )

    _terminate_controlled_git_group(pid_path)

    assert signals == [("group", direct_pid, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_git_runner_terminates_descendant_that_inherits_stdout_before_deadline(
    repo_root: Path,
) -> None:
    child_pid_path = repo_root / "git-descendant.pid"
    revision = "a" * 40
    child_program = "import time; time.sleep(30)"
    parent_program = (
        "import os,pathlib,subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_program!r}]);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text("
        "f'{os.getpid()} {child.pid}');"
        f"sys.stdout.write({(revision + chr(10))!r});sys.stdout.flush()"
    )
    started = time.monotonic()
    expected = (0, f"{revision}\n".encode())
    cleanup_required = True
    try:
        result = ledger_module._run_git_bounded(
            [sys.executable, "-c", parent_program],
            cwd=repo_root,
            deadline=started + 0.25,
            max_stdout_bytes=1024,
        )
        cleanup_required = result != expected
    finally:
        if cleanup_required:
            _terminate_controlled_git_group(child_pid_path)

    assert result == expected
    assert time.monotonic() - started < 0.35


@pytest.mark.parametrize(
    ("scripts", "options"),
    [
        ([(b"", b"", 0, 2.0)], {"timeout_seconds": 0.05}),
        ([(b"a" * 41 + b"\n", b"", 0, 0)], {"max_history_bytes": 40}),
        (
            [(b"not-a-revision\n" + b"a" * 40 + b"\n", b"", 0, 0)],
            {},
        ),
        ([(b"", b"", 7, 0)], {}),
        (
            [(b"a" * 40 + b"\n", b"", 0, 0), (b"old", b"", 0, 0)],
            {"max_blob_bytes": 2},
        ),
    ],
)
def test_git_history_resolver_fails_closed_on_bounded_process_errors(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripts: list[tuple[bytes, bytes, int, float]],
    options: dict[str, object],
) -> None:
    router = _ScriptedPopen(subprocess.Popen, list(scripts))
    monkeypatch.setattr(ledger_module.subprocess, "Popen", router)
    checksum = hashlib.sha256(b"old").hexdigest()
    started = time.monotonic()

    assert (
        GitHistoryResolver(repo_root, **options).read_exact(
            "src_" + "a" * 64, PurePosixPath("notes/a.txt"), checksum
        )
        is None
    )

    assert time.monotonic() - started < 1
    assert all(process.poll() is not None for process in router.processes)


def test_filesystem_capability_gate_is_deterministic(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_module, "_OPEN_SUPPORT_MARKER", object())

    with pytest.raises(
        ledger_module.UnsafeFilesystemError,
        match="safe descriptor-relative filesystem operations are unavailable",
    ):
        LedgerStore(RepoPaths.discover(repo_root)).load_all()


def test_ledger_rejects_symlinked_repository_ancestor(repo_root: Path) -> None:
    alias = repo_root.parent / "alias"
    alias.symlink_to(repo_root, target_is_directory=True)
    paths = RepoPaths(
        root=alias,
        raw=alias / "sources/raw",
        extracted=alias / "sources/extracted",
        ledger_dir=alias / "sources/ledger",
        ledger_summary=alias / "sources/ledger.md",
        wiki_pages=alias / "wiki/pages",
        wiki_questions=alias / "wiki/questions",
        registry=alias / "config/extractors.toml",
        lock=alias / ".brain/source-write.lock",
    )

    with pytest.raises(ValueError, match="real directory"):
        LedgerStore(paths).load_all()


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_direct_load_rejects_nonregular_entry_without_blocking(
    repo_root: Path, kind: str
) -> None:
    source_id = "src_" + "c" * 64
    cleanup_root: Path | None = None
    paths = RepoPaths.discover(repo_root)
    if kind == "socket":
        short_temp = Path("/private/tmp")
        if not short_temp.is_dir():
            short_temp = Path(tempfile.gettempdir()).resolve()
        cleanup_root = Path(tempfile.mkdtemp(prefix="brain-ledger-", dir=short_temp))
        (cleanup_root / "sources/ledger").mkdir(parents=True)
        paths = replace(
            paths,
            root=cleanup_root,
            ledger_dir=cleanup_root / "sources/ledger",
            ledger_summary=cleanup_root / "sources/ledger.md",
        )
    entry = paths.ledger_dir / f"{source_id}.json"
    open_socket: socket.socket | None = None
    if kind == "fifo":
        os.mkfifo(entry)
    else:
        open_socket = socket.socket(socket.AF_UNIX)
        previous_cwd = Path.cwd()
        try:
            os.chdir(entry.parent)
            open_socket.bind(entry.name)
        finally:
            os.chdir(previous_cwd)
    try:
        with pytest.raises(ValueError, match="regular ledger file"):
            LedgerStore(paths).load(source_id)
    finally:
        if open_socket is not None:
            open_socket.close()
        if cleanup_root is not None:
            shutil.rmtree(cleanup_root)


def test_repeated_unsafe_reads_close_all_owned_descriptors(repo_root: Path) -> None:
    if not Path("/dev/fd").is_dir():
        pytest.skip("descriptor inventory is unavailable")
    source_id = "src_" + "c" * 64
    outside = write_bytes(repo_root / "outside.json", b"{}")
    (repo_root / "sources/ledger" / f"{source_id}.json").symlink_to(outside)
    before = len(tuple(Path("/dev/fd").iterdir()))

    for _ in range(20):
        with pytest.raises(ValueError, match="regular ledger file"):
            LedgerStore(RepoPaths.discover(repo_root)).load(source_id)

    assert len(tuple(Path("/dev/fd").iterdir())) == before


def test_directory_identity_failure_closes_just_opened_descriptor(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path("/dev/fd").is_dir():
        pytest.skip("descriptor inventory is unavailable")
    real_fstat = ledger_module.os.fstat
    calls = 0

    def fail_first_child_identity(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("child identity failed")
        return real_fstat(descriptor)

    before = len(tuple(Path("/dev/fd").iterdir()))
    monkeypatch.setattr(ledger_module.os, "fstat", fail_first_child_identity)

    with pytest.raises(OSError, match="child identity failed"):
        LedgerStore(RepoPaths.discover(repo_root)).load_all()

    assert len(tuple(Path("/dev/fd").iterdir())) == before


def test_ledger_detects_sources_anchor_swap_during_read(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    store.save(record)
    real_read = ledger_module._read_regular_at
    moved_sources = repo_root / "sources-before-swap"

    def read_then_swap(
        directory_fd: int, name: str, *, label: str
    ) -> tuple[bytes, os.stat_result]:
        result = real_read(directory_fd, name, label=label)
        (repo_root / "sources").rename(moved_sources)
        (repo_root / "sources/ledger").mkdir(parents=True)
        return result

    monkeypatch.setattr(ledger_module, "_read_regular_at", read_then_swap)

    with pytest.raises(
        ledger_module.UnsafeFilesystemError,
        match="directory anchor changed",
    ):
        store.load(record.source_id)


def test_archive_write_rejects_intermediate_symlink_race_without_escape(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, item, old_sha = make_integrity_inputs(repo_root)
    outside = repo_root / "outside-archive"
    outside.mkdir()
    real_mkdir = ledger_module.os.mkdir
    raced = False

    def mkdir_then_substitute(path: str, *args: object, **kwargs: object) -> None:
        nonlocal raced
        real_mkdir(path, *args, **kwargs)
        if path == record.source_id and not raced:
            raced = True
            directory_fd = kwargs.get("dir_fd")
            assert isinstance(directory_fd, int)
            os.rmdir(path, dir_fd=directory_fd)
            os.symlink(outside, path, dir_fd=directory_fd)

    monkeypatch.setattr(ledger_module.os, "mkdir", mkdir_then_substitute)

    with pytest.raises((OSError, ValueError), match="real directory|anchor"):
        adopt_version(
            record,
            item,
            paths=RepoPaths.discover(repo_root),
            resolver=_MappingResolver({old_sha: b"old"}),
            approval_note="Approved",
            now=FIXED_NOW,
        )

    assert not tuple(outside.iterdir())
