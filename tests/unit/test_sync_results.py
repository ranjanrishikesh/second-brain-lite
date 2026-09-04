from __future__ import annotations

from datetime import timedelta
from pathlib import PurePosixPath

import pytest

from brainlib.layout import RepoPaths
from brainlib.sync_results import (
    PendingSyncResult,
    StagedSyncResult,
    SyncResultReference,
    SyncResultStore,
)
from tests.helpers import FIXED_NOW


CORPUS_REVISION = "c" * 64


def _write_result(
    paths: RepoPaths,
    *,
    generated_at=FIXED_NOW,
) -> tuple[SyncResultStore, SyncResultReference]:
    store = SyncResultStore(paths)
    with store.writer("sync", generated_at) as writer:
        writer.emit("hashed_path", {"path": "notes/a.txt"})
        writer.emit("coverage_gap", {"code": "pending", "message": "Pending."})
        reference = writer.finalize(CORPUS_REVISION)
    return store, reference


def _result_data(reference: SyncResultReference) -> dict[str, object]:
    return {
        "status": "complete_with_gaps",
        "corpus_revision": reference.corpus_revision,
        "hashed_path_count": 1,
        "new_active_representation_count": 0,
        "citation_rewrite_count": 0,
        "handoff_source_id_count": 0,
        "coverage_gap_count": 1,
        "result_manifest": reference.to_dict(),
    }


def test_result_manifest_streams_exact_events_and_detects_tampering(
    repo_root,
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)

    assert [event.kind for event in store.iter_events(reference)] == [
        "hashed_path",
        "coverage_gap",
    ]
    assert dict(reference.event_counts) == {
        "citation_rewrite": 0,
        "coverage_gap": 1,
        "handoff_source_id": 0,
        "hashed_path": 1,
        "new_active_representation": 0,
    }

    manifest = paths.root / reference.path
    payload = manifest.read_bytes()
    manifest.write_bytes(payload.replace(b"notes/a.txt", b"notes/b.txt"))

    with pytest.raises(ValueError, match="digest"):
        store.verify(reference)


def test_pending_result_rejects_an_envelope_that_disagrees_with_manifest(
    repo_root,
) -> None:
    store, reference = _write_result(RepoPaths.discover(repo_root))
    del store
    data = _result_data(reference)
    data["hashed_path_count"] = 2

    with pytest.raises(ValueError, match="manifest"):
        PendingSyncResult("sync", FIXED_NOW, reference, data)


def test_pending_result_header_must_match_recovery_metadata(repo_root) -> None:
    store, reference = _write_result(RepoPaths.discover(repo_root))
    pending = PendingSyncResult(
        "sync",
        FIXED_NOW + timedelta(seconds=1),
        reference,
        _result_data(reference),
    )
    store.save_pending(pending)

    with pytest.raises(ValueError, match="metadata"):
        store.load_pending()


def test_pending_result_round_trips_only_after_verified_binding(repo_root) -> None:
    store, reference = _write_result(RepoPaths.discover(repo_root))
    pending = PendingSyncResult(
        "sync",
        FIXED_NOW,
        reference,
        _result_data(reference),
    )
    store.save_pending(pending)

    assert store.load_pending() == pending
    store.clear_pending(reference)
    assert store.load_pending() is None


def test_result_reference_rejects_noncanonical_path() -> None:
    with pytest.raises(ValueError, match="path"):
        SyncResultReference(
            "sync_" + "a" * 64,
            PurePosixPath("elsewhere/result.jsonl"),
            "a" * 64,
            CORPUS_REVISION,
            {
                "citation_rewrite": 0,
                "coverage_gap": 0,
                "handoff_source_id": 0,
                "hashed_path": 0,
                "new_active_representation": 0,
            },
        )


@pytest.mark.parametrize("command", ("init", "sync"))
def test_legacy_result_receipts_keep_256_kib_bounds_and_acknowledgement(
    repo_root, command
):
    store = SyncResultStore(RepoPaths.discover(repo_root))
    with store.writer(command, FIXED_NOW) as writer:
        writer.emit("hashed_path", {"path": "notes/a.txt"})
        writer.emit("coverage_gap", {"code": "pending", "message": "Pending."})
        reference = writer.finalize(CORPUS_REVISION)
    data = _result_data(reference)
    oversized = {**data, "notes": "x" * (256 * 1024)}
    with pytest.raises(ValueError, match="byte limit"):
        PendingSyncResult(command, FIXED_NOW, reference, oversized)
    staged_data = {
        key: value for key, value in data.items() if key != "result_manifest"
    }
    with pytest.raises(ValueError, match="byte limit"):
        StagedSyncResult(
            command,
            FIXED_NOW,
            CORPUS_REVISION,
            "a" * 64,
            "b" * 64,
            {**staged_data, "notes": "x" * (256 * 1024)},
        )
    staged = StagedSyncResult(
        command, FIXED_NOW, CORPUS_REVISION, "a" * 64, "b" * 64, staged_data
    )
    store.save_staged(staged)
    assert store.load_staged() == staged
    pending = PendingSyncResult(command, FIXED_NOW, reference, data)
    store.save_pending(pending)
    assert store.load_pending() == pending
    store.clear_pending(reference)
    assert store.load_pending() is None
    assert store.load_acknowledged().reference == reference


@pytest.mark.parametrize("model", (PendingSyncResult, StagedSyncResult))
def test_legacy_receipt_models_do_not_accept_snapshot_as_sync_data(model):
    args = (
        ("source snapshot-url", FIXED_NOW, None, {})
        if model is PendingSyncResult
        else ("source snapshot-url", FIXED_NOW, CORPUS_REVISION, "a" * 64, "b" * 64, {})
    )
    with pytest.raises(ValueError, match="command"):
        model(*args)
