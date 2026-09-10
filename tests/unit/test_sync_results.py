from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import PurePosixPath

import pytest

import brainlib.commands as commands
import brainlib.sync_results as sync_results_module
from brainlib.layout import RepoPaths
from brainlib.sync_results import (
    PendingSyncResult,
    StagedSyncResult,
    SyncResultConsumption,
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


def test_pending_result_consumption_binds_the_verified_full_stream_and_is_idempotent(
    repo_root,
) -> None:
    store, reference = _write_result(RepoPaths.discover(repo_root))
    pending = PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    store.save_pending(pending)

    consumed = store.consume_pending(reference.result_id)

    assert consumed.status == "consumed"
    assert consumed.reference == reference
    assert consumed.event_counts == reference.event_counts
    assert len(consumed.effect_digest) == 64
    assert consumed.effect_digest == hashlib.sha256(
        b'{"data":{"path":"notes/a.txt"},"kind":"hashed_path"}\n'
        b'{"data":{"code":"pending","message":"Pending."},"kind":"coverage_gap"}\n'
    ).hexdigest()
    assert consumed.manifest_path == reference.path
    assert consumed.handoff_delivery is None
    assert store.load_consumption_receipt(reference) == consumed

    repeated = store.consume_pending(reference.result_id)
    assert repeated.status == "already_consumed"
    assert repeated.reference == reference
    assert repeated.event_counts == reference.event_counts
    assert repeated.effect_digest == consumed.effect_digest
    assert store.load_consumption_receipt(reference).effect_digest == consumed.effect_digest


def test_consumption_refuses_a_malformed_persisted_receipt(repo_root) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    receipt = paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    receipt.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="consumption receipt"):
        store.consume_pending(reference.result_id)
    with pytest.raises(ValueError, match="consumption receipt"):
        store.require_consumption_receipt(reference)
    assert store.load_pending() is not None


def test_consumption_receipt_with_a_wrong_effect_digest_cannot_authorize_acknowledgement(
    repo_root,
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    receipt = paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    receipt.write_text(
        json.dumps(
            SyncResultConsumption(
                "consumed", reference, reference.event_counts, "0" * 64
            ).receipt_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match events"):
        store.require_consumption_receipt(reference)
    with pytest.raises(ValueError, match="does not match events"):
        store.consume_pending(reference.result_id)
    assert store.load_pending() is not None


@pytest.mark.parametrize(
    "field, malformed",
    (
        ("schema_version", True),
        ("schema_version", 1.0),
        ("event_counts.hashed_path", True),
        ("event_counts.hashed_path", 1.0),
        ("reference.path", ".brain//sync-results/{result_id}.jsonl"),
        ("reference.path", ".brain/./sync-results/{result_id}.jsonl"),
    ),
)
def test_noncanonical_consumption_receipt_cannot_be_consumed_or_acknowledged(
    repo_root, field: str, malformed: object
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    store.consume_pending(reference.result_id)
    receipt_path = (
        paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    if field == "schema_version":
        payload[field] = malformed
    elif field == "event_counts.hashed_path":
        payload["event_counts"]["hashed_path"] = malformed
    else:
        payload["reference"]["path"] = str(malformed).format(
            result_id=reference.result_id
        )
    receipt_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    with pytest.raises(ValueError):
        store.consume_pending(reference.result_id)
    acknowledgement = commands.acknowledge_sync_result_id(repo_root, reference.result_id)
    assert not acknowledgement.ok
    assert store.load_pending() is not None


def test_published_consumption_receipt_requires_a_new_durability_barrier_on_retry(
    repo_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    real_fsync_directory = sync_results_module._fsync_directory
    attempts = 0

    def fail_directory_sync(directory_fd: int) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("simulated consumption receipt directory fsync failure")

    monkeypatch.setattr(
        sync_results_module, "_fsync_directory", fail_directory_sync
    )
    with pytest.raises(OSError, match="consumption receipt directory fsync failure"):
        store.consume_pending(reference.result_id)
    assert (
        paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    ).is_file()

    with pytest.raises(OSError, match="consumption receipt directory fsync failure"):
        store.consume_pending(reference.result_id)
    assert attempts >= 2

    monkeypatch.setattr(sync_results_module, "_fsync_directory", real_fsync_directory)
    recovered = store.consume_pending(reference.result_id)
    assert recovered.status == "already_consumed"


@pytest.mark.parametrize("operation", ("consume", "acknowledge"))
def test_durable_receipt_barrier_cannot_authorize_bytes_reopened_after_the_sync(
    repo_root, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    store.consume_pending(reference.result_id)
    receipt_path = (
        paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    )
    valid_payload = receipt_path.read_bytes()
    real_read_regular_at = sync_results_module._read_regular_at

    def synchronize_malformed_then_restore(directory_fd: int, name: str, **kwargs):
        if name == receipt_path.name and kwargs.get("synchronize"):
            receipt_path.write_text("{}", encoding="utf-8")
            try:
                return real_read_regular_at(directory_fd, name, **kwargs)
            finally:
                receipt_path.write_bytes(valid_payload)
        return real_read_regular_at(directory_fd, name, **kwargs)

    monkeypatch.setattr(
        sync_results_module, "_read_regular_at", synchronize_malformed_then_restore
    )
    if operation == "consume":
        with pytest.raises(
            (ValueError, sync_results_module.UnsafeFilesystemError),
            match="consumption receipt",
        ):
            store.consume_pending(reference.result_id)
    else:
        acknowledgement = commands.acknowledge_sync_result_id(
            repo_root, reference.result_id
        )
        assert not acknowledgement.ok
    assert store.load_pending() is not None


def test_consumption_rejects_a_tampered_manifest_without_creating_a_receipt(
    repo_root,
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    manifest = paths.root / reference.path
    manifest.write_bytes(manifest.read_bytes().replace(b"notes/a.txt", b"notes/b.txt"))

    with pytest.raises(ValueError, match="digest"):
        store.consume_pending(reference.result_id)
    assert (paths.root / ".brain/sync-results/pending.json").is_file()
    assert not (
        paths.root / ".brain/sync-results" / f"consumed_{reference.result_id}.json"
    ).exists()


def test_consumption_rejects_an_incomplete_stream_count(
    repo_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RepoPaths.discover(repo_root)
    store, reference = _write_result(paths)
    store.save_pending(
        PendingSyncResult("sync", FIXED_NOW, reference, _result_data(reference))
    )
    monkeypatch.setattr(store, "iter_events", lambda _reference: iter(()))

    with pytest.raises(ValueError, match="counts"):
        store.consume_pending(reference.result_id)
    assert store.load_consumption_receipt(reference) is None


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
