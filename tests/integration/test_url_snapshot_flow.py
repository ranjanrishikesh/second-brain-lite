from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

from brainlib.contracts import Anchor, SourceRecord, SourceState
from brainlib.extractors.handoff import (
    AgentRegistrationError,
    HandoffItem,
    collect_durable_handoffs,
    load_handoff_item,
    register_staged_agent_extraction,
)
from brainlib.inventory import parse_url_descriptor
from brainlib.layout import RepoPaths
from brainlib.ledger import ActivationGuard, LedgerStore
from brainlib.registry import ExtractorRegistry
from brainlib.sources.web import (
    ApprovalClaim,
    SnapshotResult,
)
from tests.helpers_extractors import (
    FIXED_NOW,
    ROUTES,
    mock_web_transport,
    record_for_handoff,
    render_handoff_id,
    run_brain_json as _run_brain_json,
    run_snapshot_fixture,
    web_test_processor,
    web_test_services,
)


def run_brain_json(repo_root, *argv, **kwargs):
    kwargs.setdefault("services", web_test_services())
    return _run_brain_json(repo_root, *argv, **kwargs)


def consume_sync_result(repo_root, result_id):
    payload = run_brain_json(
        repo_root,
        "source",
        "consume-sync-result",
        "--result-id",
        result_id,
    )
    assert payload["ok"], payload
    return payload


def snapshot_args(source_id=None):
    target = (
        ("--url", "https://example.test/report", "--description", "fixture")
        if source_id is None
        else ("--source-id", source_id)
    )
    return (
        "source",
        "snapshot-url",
        *target,
        "--approval-event-id",
        "evt_command",
        "--approval-scope",
        "one URL",
        "--approval-note",
        "approved",
    )


def _assert_compact_snapshot_replay(payload, continuation, records):
    snapshot = payload["data"]["snapshot"]
    assert {
        key: value for key, value in snapshot.items() if key != "source_version"
    } == continuation.result_recipe["snapshot"]
    record = records[continuation.source_id]
    assert (
        snapshot["source_version"]
        == record.to_dict()["versions"][record.active_content_sha256]
    )
    core = {
        **payload,
        "data": {
            key: value
            for key, value in payload["data"].items()
            if key != "result_manifest"
        },
    }
    encoded = json.dumps(
        core, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == continuation.response_sha256


def test_command_rejects_approval_before_transport_factory_or_staging(repo_root):
    factory = Mock(side_effect=AssertionError("factory must not be called"))
    payload = run_brain_json(
        repo_root,
        *snapshot_args(),
        "--approval-note",
        "",
        services=web_test_services(factory),
    )
    assert not payload["ok"]
    assert payload["errors"][0]["code"] == "approval_required"
    factory.assert_not_called()
    assert not list((repo_root / "sources/raw/urls").glob("*.url.md"))


def test_descriptor_is_durable_and_final_named_before_factory(repo_root, repo_paths):
    observed = []

    def factory():
        records = LedgerStore(repo_paths).load_all()
        assert len(records) == 1
        record = next(iter(records.values()))
        assert record.state is SourceState.AWAITING_APPROVAL
        assert record.current_raw_path.name.endswith(".url.md")
        descriptor = parse_url_descriptor(
            repo_paths.raw / record.current_raw_path, record.current_raw_path
        )
        from brainlib.inventory import source_id_for_url_descriptor

        assert record.source_id == source_id_for_url_descriptor(descriptor)
        observed.append(record)
        raise OSError("fixture request failed")

    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(factory)
    )
    assert not payload["ok"]
    assert len(observed) == 1
    assert (
        LedgerStore(repo_paths).load(observed[0].source_id).url_descriptor.url
        == "https://example.test/report"
    )


@pytest.mark.parametrize(
    "state",
    (SourceState.OK, SourceState.WARNING, SourceState.FAILED, SourceState.NEEDS_AGENT),
)
def test_refresh_accepts_processed_descriptor_states(
    captured_ok_descriptor_record, repo_root, repo_paths, state
):
    record = replace(captured_ok_descriptor_record, state=state)
    LedgerStore(repo_paths).save(record)
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: transport),
    )
    assert payload["ok"], payload
    assert payload["data"]["snapshot"]["source_id"] == record.source_id


def test_refresh_uses_edited_descriptor_url_preserving_history(
    captured_ok_descriptor_record, repo_root, repo_paths
):
    record = captured_ok_descriptor_record
    descriptor = repo_paths.raw / record.current_raw_path
    descriptor.write_text(
        '---\nkind: url\nurl: "https://example.test/edited"\ndescription: "fixture"\nadded: 2026-09-04\n---\n'
    )
    transport = mock_web_transport(repo_paths, b"changed bytes\n")
    payload = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: transport),
    )
    assert payload["ok"], payload
    assert transport.capture.call_args.args[0] == "https://example.test/edited"
    final = LedgerStore(repo_paths).load(record.source_id)
    assert len(final.versions) == 2
    assert final.current_raw_path == record.current_raw_path


def test_rendered_direct_capture_never_constructs_transport(
    descriptor_record, rendered_staging_file, repo_root, repo_paths
):
    factory = Mock(side_effect=AssertionError("rendered transport constructed"))
    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        "--rendered-staging-path",
        str(rendered_staging_file),
        "--retrieved-at",
        "2026-09-04T12:01:00Z",
        "--final-url",
        "https://example.test/final",
        "--redirect-url",
        "https://example.test/hop",
        "--redirect-url",
        "https://example.test/final",
        "--detected-media-type",
        "text/html",
        services=web_test_services(factory),
    )
    assert payload["ok"], payload
    retrieval = payload["data"]["snapshot"]["retrieval"]
    assert retrieval["redirects"] == [
        "https://example.test/hop",
        "https://example.test/final",
    ]
    assert retrieval["retrieved_at"] == "2026-09-04T12:01:00Z"
    factory.assert_not_called()


def test_raw_and_pending_record_exist_before_processing(
    descriptor_record, recording_transport, repo_paths
):
    delegate = web_test_processor()
    calls = []

    class InspectingProcessor:
        def process(self, record, item, extractor, *, paths, context):
            saved = LedgerStore(paths).load(record.source_id)
            assert saved.state is SourceState.EXTRACTING
            version = saved.versions[context.input_sha256]
            assert item.fingerprint.path == version.raw_path
            assert version.raw_path.parts[0] == "_web"
            assert (
                os.pread(context.input_descriptor, version.byte_size, 0)
                == b"same bytes\n"
            )
            calls.append(context.input_path)
            return delegate.process(
                record, item, extractor, paths=paths, context=context
            )

    result = run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_process", "one URL", "approved"),
        paths=repo_paths,
        transport=recording_transport,
        processor=InspectingProcessor(),
    )
    assert result.active_representation is not None
    assert len(calls) == 1
    assert (
        LedgerStore(repo_paths).load(descriptor_record.source_id).current_raw_path
        == descriptor_record.current_raw_path
    )


@pytest.mark.parametrize(
    "failure", ("input", "output", "noop_candidate", "noop_rollback")
)
def test_web_activation_proves_both_authorities_and_exact_checkpoint(
    descriptor_record, recording_transport, repo_paths, failure
):
    store = LedgerStore(repo_paths)
    saw_active = False

    def checkpoint(record):
        nonlocal saw_active
        if record.active_derivation_id is not None:
            saw_active = True
            if failure == "noop_candidate":
                return
            store.save(record)
            if failure in {"output", "noop_rollback"}:
                target = (
                    repo_paths.root
                    / record.derivations[record.active_derivation_id].output_path
                )
                target.write_bytes(b"tampered output")
            elif failure == "input":
                target = (
                    repo_paths.raw
                    / record.versions[record.active_content_sha256].raw_path
                )
                target.write_bytes(b"changed input")
        elif saw_active and failure == "noop_rollback":
            return
        else:
            store.save(record)

    with pytest.raises((OSError, ValueError)):
        run_snapshot_fixture(
            descriptor_record,
            approval=ApprovalClaim("evt_guard", "one URL", "approved"),
            paths=repo_paths,
            transport=recording_transport,
            checkpoint=checkpoint,
        )
    if failure == "noop_rollback":
        with pytest.raises(ValueError, match="activation recovery"):
            store.load(descriptor_record.source_id)
        guard = ActivationGuard.load(repo_paths, descriptor_record.source_id)
        assert guard.rollback.active_derivation_id is None
        store.recover_activation_guards()
    assert store.load(descriptor_record.source_id).active_derivation_id is None


@pytest.mark.parametrize(
    "failure", (None, "input", "output", "noop_candidate", "noop_rollback")
)
def test_snapshot_activation_journal_orders_prepare_proofs_and_commit(
    descriptor_record, repo_root, repo_paths, monkeypatch, failure
):
    from brainlib.inventory import SnapshotNamespace
    from brainlib.sources import web
    from brainlib.sync_results import (
        SyncResultReference,
        SyncResultStore,
        SyncResultWriter,
    )

    actions = []
    prepared_events = []
    original_emit = SyncResultWriter.emit
    original_commit = SyncResultWriter.commit
    original_save = LedgerStore.save
    original_use = web.use_stable_file
    original_clear = ActivationGuard._clear_after_checkpoint

    def emit(writer, kind, data):
        original_emit(writer, kind, data)
        prepared_events.append((kind, dict(data)))
        actions.append("prepare")

    def commit(writer, kind, data):
        assert (kind, dict(data)) in prepared_events
        original_commit(writer, kind, data)
        actions.append("commit")

    def save(store, record):
        if record.active_derivation_id is not None:
            actions.append("candidate")
            if failure == "noop_candidate":
                return
            original_save(store, record)
            if failure in {"output", "noop_rollback"}:
                derivation = record.derivations[record.active_derivation_id]
                (repo_paths.root / derivation.output_path).write_bytes(b"corrupt")
            elif failure == "input":
                version = record.versions[record.active_content_sha256]
                (repo_paths.raw / version.raw_path).write_bytes(b"corrupt")
        elif "candidate" in actions:
            actions.append("rollback")
            if failure != "noop_rollback":
                original_save(store, record)
        else:
            original_save(store, record)

    def use(paths, namespace, *args, **kwargs):
        result = original_use(paths, namespace, *args, **kwargs)
        if namespace is SnapshotNamespace.EXTRACTED:
            actions.append("output_proven")
        elif namespace is SnapshotNamespace.RAW_WEB:
            actions.append("input_proven")
        return result

    def clear(guard, record):
        original_clear(guard, record)
        actions.append("clear")

    monkeypatch.setattr(SyncResultWriter, "emit", emit)
    monkeypatch.setattr(SyncResultWriter, "commit", commit)
    monkeypatch.setattr(LedgerStore, "save", save)
    monkeypatch.setattr(web, "use_stable_file", use)
    monkeypatch.setattr(ActivationGuard, "_clear_after_checkpoint", clear)
    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert actions[:2] == ["prepare", "candidate"]
    result_store = SyncResultStore(repo_paths)
    if failure is not None:
        assert not payload["ok"]
        assert "commit" not in actions
        assert "rollback" in actions
        assert result_store.load_pending() is None
        journal = repo_paths.root / ".brain/sync-results/inflight.jsonl"
        entries = [json.loads(line) for line in journal.read_text().splitlines()]
        assert not any(entry["type"] == "commit" for entry in entries)
        if failure == "noop_rollback":
            with pytest.raises(ValueError, match="activation recovery"):
                LedgerStore(repo_paths).load(descriptor_record.source_id)
        else:
            assert (
                LedgerStore(repo_paths)
                .load(descriptor_record.source_id)
                .active_derivation_id
                is None
            )
        monkeypatch.setattr(LedgerStore, "save", original_save)
        factory = Mock(
            side_effect=AssertionError("aborted continuation must not capture")
        )
        retry = run_brain_json(
            repo_root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(factory),
        )
        assert not retry["ok"]
        assert retry["errors"][0]["code"] == "snapshot_interrupted"
        assert "commit" not in actions
        factory.assert_not_called()
        return
    assert payload["ok"], payload
    assert actions == [
        "prepare",
        "candidate",
        "output_proven",
        "input_proven",
        "clear",
        "commit",
    ]
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    pending = result_store.load_pending()
    assert pending.command == "source snapshot-url"
    assert pending.reference == reference
    events = list(result_store.iter_events(reference))
    assert len(events) == 1
    assert events[0].kind == "new_active_representation"
    record = LedgerStore(repo_paths).load(descriptor_record.source_id)
    from brainlib.commands import _ledger_record_sha256

    assert events[0].data == payload["data"]["snapshot"]["active_representation"]
    assert prepared_events == [
        (
            "new_active_representation",
            {
                **payload["data"]["snapshot"]["active_representation"],
                "record_sha256": _ledger_record_sha256(record),
            },
        )
    ]
    consume_sync_result(repo_root, reference.result_id)
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference.result_id,
    )
    assert acknowledged["ok"], acknowledged
    assert result_store.load_pending() is None
    assert result_store.load_acknowledged().reference == reference


def test_snapshot_pending_event_result_replays_without_capture(
    descriptor_record, repo_root, repo_paths
):
    from brainlib.sync_results import SyncResultStore

    first = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    pending = SyncResultStore(repo_paths).load_pending()
    assert pending is not None
    records = LedgerStore(repo_paths).load_all()
    factory = Mock(side_effect=AssertionError("replay must not capture"))
    replay = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert replay == first
    factory.assert_not_called()
    assert LedgerStore(repo_paths).load_all() == records


def test_snapshot_acknowledgement_requires_consumption_and_keeps_continuation(
    descriptor_record, repo_root, repo_paths
):
    from brainlib.sync_results import SyncResultStore

    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    result_id = payload["data"]["result_manifest"]["result_id"]
    store = SyncResultStore(repo_paths)

    early = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )
    assert not early["ok"]
    assert store.load_pending() is not None
    assert store.load_snapshot_continuation() is not None

    consume_sync_result(repo_root, result_id)
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        result_id,
    )
    assert acknowledged["ok"], acknowledged
    assert store.load_pending() is None
    assert store.load_snapshot_continuation() is None


@pytest.mark.parametrize(
    "phase", ("finalize", "save_pending", "complete_inflight", "write_summary")
)
def test_snapshot_event_result_recovers_after_publication_failure(
    descriptor_record, repo_root, repo_paths, monkeypatch, phase
):
    from brainlib.sync_results import SyncResultStore, SyncResultWriter

    target = (
        SyncResultWriter
        if phase == "finalize"
        else LedgerStore
        if phase == "write_summary"
        else SyncResultStore
    )
    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(
            target, phase, Mock(side_effect=OSError("injected result failure"))
        )
        first = run_brain_json(
            repo_root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(lambda: mock_web_transport(repo_paths)),
        )
    assert not first["ok"]
    store = SyncResultStore(repo_paths)
    recovery = store.load_pending() or store.load_staged()
    assert recovery is not None
    before = LedgerStore(repo_paths).load_all()
    factory = Mock(side_effect=AssertionError("recovery must not capture"))
    recovered = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert recovered["ok"], recovered
    _assert_compact_snapshot_replay(
        recovered, store.load_snapshot_continuation(), before
    )
    reference = store.load_pending().reference
    assert [event.kind for event in store.iter_events(reference)] == [
        "new_active_representation"
    ]
    assert store.load_staged() is None
    assert LedgerStore(repo_paths).load_all() == before
    factory.assert_not_called()


@pytest.mark.parametrize("phase", ("before", "after"))
def test_snapshot_activation_commit_failure_replays_without_new_retrieval(
    descriptor_record, repo_root, repo_paths, monkeypatch, phase
):
    from brainlib.sync_results import SyncResultStore, SyncResultWriter

    original_commit = SyncResultWriter.commit

    def fail_commit(writer, kind, data):
        if phase == "after":
            original_commit(writer, kind, data)
        raise OSError("injected activation commit failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(SyncResultWriter, "commit", fail_commit)
        first = run_brain_json(
            repo_root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(lambda: mock_web_transport(repo_paths)),
        )
    assert not first["ok"]
    records = LedgerStore(repo_paths).load_all()
    assert records[descriptor_record.source_id].active_derivation_id is not None
    store = SyncResultStore(repo_paths)
    continuation = store.load_snapshot_continuation()
    assert continuation is not None
    factory = Mock(side_effect=AssertionError("commit recovery must not capture"))
    recovered = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert recovered["ok"], recovered
    _assert_compact_snapshot_replay(recovered, continuation, records)
    events = list(store.iter_events(store.load_pending().reference))
    assert [event.kind for event in events] == ["new_active_representation"]
    assert LedgerStore(repo_paths).load_all() == records
    factory.assert_not_called()


@pytest.mark.parametrize("inactive", (False, True))
def test_snapshot_reuse_journals_only_reactivation_with_one_checkpoint(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch, inactive
):
    from brainlib.sync_results import SyncResultReference, SyncResultStore

    record = captured_ok_descriptor_record
    if inactive:
        record = replace(record, state=SourceState.PENDING, active_derivation_id=None)
    LedgerStore(repo_paths).save(record)
    checkpoints = []
    original_save = LedgerStore.save

    def save(store, candidate):
        checkpoints.append(candidate)
        original_save(store, candidate)

    monkeypatch.setattr(LedgerStore, "save", save)
    processor = Mock()
    services = replace(
        web_test_services(lambda: mock_web_transport(repo_paths)),
        processor_factory=lambda: processor,
    )
    payload = run_brain_json(
        repo_root, *snapshot_args(record.source_id), services=services
    )
    assert payload["ok"], payload
    assert len(checkpoints) == 1
    processor.process.assert_not_called()
    assert checkpoints[0].last_attempt == record.last_attempt
    snapshot = payload["data"]["snapshot"]
    assert snapshot["extraction_result"] is None
    assert snapshot["active_representation"] is not None
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    events = list(SyncResultStore(repo_paths).iter_events(reference))
    assert [event.kind for event in events] == (
        ["new_active_representation"] if inactive else []
    )
    if inactive:
        assert events[0].data == snapshot["active_representation"]


@pytest.mark.parametrize("phase", ("guard_clear", "commit", "staged"))
@pytest.mark.skipif(not hasattr(os, "fork"), reason="hard-crash fixture requires fork")
def test_snapshot_hard_interruption_replays_durable_continuation_without_capture(
    descriptor_record, repo_root, repo_paths, monkeypatch, phase
):
    from brainlib.sync_results import SyncResultStore, SyncResultWriter

    target, method = {
        "guard_clear": (ActivationGuard, "_clear_after_checkpoint"),
        "commit": (SyncResultWriter, "commit"),
        "staged": (SyncResultStore, "save_staged"),
    }[phase]
    original = getattr(target, method)

    def hard_stop(*args, **kwargs):
        if phase != "staged":
            original(*args, **kwargs)
        os._exit(71)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(target, method, hard_stop)
        child = os.fork()
        if child == 0:
            try:
                run_brain_json(
                    repo_root,
                    *snapshot_args(descriptor_record.source_id),
                    services=web_test_services(lambda: mock_web_transport(repo_paths)),
                )
            finally:
                os._exit(72)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 71
    # Exercise the existing stale/dead-owner lock policy with its test clock;
    # production does not bypass or shorten the lock's one-hour stale threshold.
    from datetime import timedelta
    from brainlib.locking import LockMetadata, SourceWriteLock

    lock_owner = LockMetadata.from_bytes(repo_paths.lock.read_bytes())
    with SourceWriteLock.acquire(
        repo_paths.lock, now=lambda: lock_owner.started_at + timedelta(hours=2)
    ):
        pass
    store = SyncResultStore(repo_paths)
    continuation = store.load_snapshot_continuation()
    assert continuation is not None
    records = LedgerStore(repo_paths).load_all()
    factory = Mock(side_effect=AssertionError("hard-crash recovery must not capture"))
    replay = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert replay["ok"], replay
    _assert_compact_snapshot_replay(replay, continuation, records)
    events = list(store.iter_events(store.load_pending().reference))
    assert [event.kind for event in events] == ["new_active_representation"]
    assert LedgerStore(repo_paths).load_all() == records
    factory.assert_not_called()


@pytest.mark.parametrize("change", ("approval", "selector", "rendered"))
def test_snapshot_pending_request_identity_mismatch_fails_before_capture(
    descriptor_record, repo_root, repo_paths, change
):
    from brainlib.sync_results import SyncResultStore

    first = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    args = list(snapshot_args(descriptor_record.source_id))
    if change == "approval":
        args[args.index("--approval-event-id") + 1] = "evt_other"
    elif change == "selector":
        args = list(snapshot_args())
    else:
        args.extend(
            (
                "--rendered-staging-path",
                ".brain/web-staging/missing.html",
                "--retrieved-at",
                "2026-09-04T12:01:00Z",
                "--final-url",
                "https://example.test/final",
                "--detected-media-type",
                "text/html",
            )
        )
    factory = Mock(side_effect=AssertionError("mismatch must not capture"))
    before = LedgerStore(repo_paths).load_all()
    mismatch = run_brain_json(repo_root, *args, services=web_test_services(factory))
    assert not mismatch["ok"]
    assert mismatch["errors"][0]["code"] == "snapshot_request_mismatch"
    assert SyncResultStore(repo_paths).load_pending() is not None
    assert LedgerStore(repo_paths).load_all() == before
    factory.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="hard-crash fixture requires fork")
def test_snapshot_metadata_checkpoint_hard_crash_replays_without_another_checkpoint(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch
):
    from datetime import timedelta
    from brainlib.locking import LockMetadata, SourceWriteLock
    from brainlib.sync_results import SyncResultStore

    original_save = LedgerStore.save

    def stop_after_checkpoint(store, candidate):
        original_save(store, candidate)
        if candidate.active_derivation_id is not None:
            os._exit(71)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(LedgerStore, "save", stop_after_checkpoint)
        child = os.fork()
        if child == 0:
            try:
                run_brain_json(
                    repo_root,
                    *snapshot_args(captured_ok_descriptor_record.source_id),
                    services=web_test_services(lambda: mock_web_transport(repo_paths)),
                )
            finally:
                os._exit(72)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 71
    lock_owner = LockMetadata.from_bytes(repo_paths.lock.read_bytes())
    with SourceWriteLock.acquire(
        repo_paths.lock, now=lambda: lock_owner.started_at + timedelta(hours=2)
    ):
        pass
    store = SyncResultStore(repo_paths)
    continuation = store.load_snapshot_continuation()
    assert continuation.event is None
    before = LedgerStore(repo_paths).load_all()
    save = Mock(side_effect=AssertionError("metadata replay must not checkpoint again"))
    monkeypatch.setattr(LedgerStore, "save", save)
    factory = Mock(side_effect=AssertionError("metadata replay must not capture"))
    replay = run_brain_json(
        repo_root,
        *snapshot_args(captured_ok_descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert replay["ok"], replay
    _assert_compact_snapshot_replay(replay, continuation, before)
    assert list(store.iter_events(store.load_pending().reference)) == []
    assert LedgerStore(repo_paths).load_all() == before
    save.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "argument,value",
    (
        ("--rendered-staging-path", ".brain/web-staging/other.html"),
        ("--retrieved-at", "2026-09-04T12:02:00Z"),
        ("--final-url", "https://example.test/other"),
        ("--redirect-url", "https://example.test/redirect"),
        ("--detected-media-type", "application/pdf"),
        ("--handoff-id", "hnd_" + "f" * 64),
    ),
)
def test_snapshot_replay_distinguishes_every_rendered_argument_without_staging_read(
    descriptor_record,
    rendered_staging_file,
    repo_root,
    repo_paths,
    monkeypatch,
    argument,
    value,
):
    from brainlib.sources import web

    args = [
        *snapshot_args(descriptor_record.source_id),
        "--rendered-staging-path",
        str(rendered_staging_file),
        "--retrieved-at",
        "2026-09-04T12:01:00Z",
        "--final-url",
        "https://example.test/final",
        "--detected-media-type",
        "text/html",
    ]
    first = run_brain_json(repo_root, *args)
    assert first["ok"], first
    if argument in args:
        args[args.index(argument) + 1] = value
    else:
        args.extend((argument, value))
    staging = Mock(side_effect=AssertionError("replay mismatch must not read staging"))
    monkeypatch.setattr(web, "_read_capture_staging", staging)
    factory = Mock(
        side_effect=AssertionError("rendered replay must not construct transport")
    )
    replay = run_brain_json(repo_root, *args, services=web_test_services(factory))
    assert not replay["ok"]
    assert replay["errors"][0]["code"] == "snapshot_request_mismatch"
    staging.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "change",
    (
        "boolean_schema",
        "duplicate",
        "unknown",
        "snapshot",
        "warnings",
        "event",
        "identity",
        "digest",
        "timestamp",
    ),
)
def test_snapshot_malformed_continuation_fails_closed_before_transport(
    descriptor_record, repo_root, repo_paths, change
):
    first = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    path = repo_paths.root / ".brain/sync-results/snapshot-continuation.json"
    value = json.loads(path.read_text())
    if change == "boolean_schema":
        value["schema_version"] = True
    elif change == "unknown":
        value["unexpected"] = True
    elif change == "snapshot":
        value["result_recipe"]["snapshot"]["raw_path"] = "_web/wrong"
    elif change == "warnings":
        value["warnings"] = [{"code": "made_up"}]
    elif change == "event":
        value["event"]["data"]["record_sha256"] = "f" * 64
    elif change == "identity":
        value["request_identity"]["rendered"] = {}
    elif change == "digest":
        value["journal_sha256"] = "z" * 64
    elif change == "timestamp":
        value["generated_at"] = "2000-01-01T00:00:00+00:00"
    body = json.dumps(value)
    if change == "duplicate":
        body = '{"schema_version": 1, ' + body[1:]
    path.write_text(body)
    before = LedgerStore(repo_paths).load_all()
    factory = Mock(
        side_effect=AssertionError("malformed continuation must not capture")
    )
    replay = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert not replay["ok"]
    assert LedgerStore(repo_paths).load_all() == before
    factory.assert_not_called()


@pytest.mark.parametrize("method", ("clear_pending", "clear_snapshot_continuation"))
@pytest.mark.parametrize("phase", ("before", "after"))
def test_snapshot_acknowledgement_fault_is_retryable(
    descriptor_record, repo_root, repo_paths, monkeypatch, method, phase
):
    from brainlib.sync_results import SyncResultStore

    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert payload["ok"], payload
    identifier = payload["data"]["result_manifest"]["result_id"]
    consume_sync_result(repo_root, identifier)
    original = getattr(SyncResultStore, method)

    def fail(store, reference):
        if phase == "after":
            original(store, reference)
        raise OSError("injected acknowledgement failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(SyncResultStore, method, fail)
        failed = run_brain_json(
            repo_root, "source", "acknowledge-sync-result", "--result-id", identifier
        )
    assert not failed["ok"]
    acknowledged = run_brain_json(
        repo_root, "source", "acknowledge-sync-result", "--result-id", identifier
    )
    assert acknowledged["ok"], acknowledged
    store = SyncResultStore(repo_paths)
    assert store.load_pending() is None
    assert store.load_snapshot_continuation() is None
    again = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert again["ok"], again
    assert (
        again["data"]["result_manifest"]["event_counts"]["new_active_representation"]
        == 0
    )


@pytest.mark.parametrize("field", ("journal_sha256", "committed_journal_sha256"))
def test_snapshot_continuation_rejects_either_tampered_journal_binding(
    descriptor_record, repo_root, repo_paths, monkeypatch, field
):
    from brainlib.sync_results import SyncResultStore

    with monkeypatch.context() as interruption:
        interruption.setattr(
            SyncResultStore,
            "save_staged",
            Mock(side_effect=OSError("stop before staging")),
        )
        first = run_brain_json(
            repo_root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(lambda: mock_web_transport(repo_paths)),
        )
    assert not first["ok"]
    path = repo_paths.root / ".brain/sync-results/snapshot-continuation.json"
    value = json.loads(path.read_text())
    value[field] = "f" * 64
    path.write_text(json.dumps(value))
    factory = Mock(side_effect=AssertionError("tampered journal must not capture"))
    replay = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert not replay["ok"]
    assert SyncResultStore(repo_paths).load_pending() is None
    factory.assert_not_called()


def test_snapshot_request_identity_canonicalizes_existing_descriptor_url(
    descriptor_record, repo_root, repo_paths
):
    descriptor = repo_paths.raw / descriptor_record.current_raw_path
    descriptor.write_text(
        descriptor.read_text().replace(
            descriptor_record.url_descriptor.url,
            "HTTPS://EXAMPLE.TEST:443/report#fragment",
        )
    )
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: transport),
    )
    assert payload["ok"], payload
    assert (
        payload["data"]["snapshot"]["retrieval"]["requested_url"]
        == "https://example.test/report"
    )
    factory = Mock(
        side_effect=AssertionError("canonical request replay must not capture")
    )
    replay = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(factory),
    )
    assert replay == payload
    factory.assert_not_called()


def test_snapshot_continuation_journal_binding_is_source_specific(repo_paths):
    from brainlib.sync_results import SyncResultStore

    with SyncResultStore(repo_paths).writer(
        "sync", FIXED_NOW, recoverable=True
    ) as writer:
        with pytest.raises(ValueError, match="snapshot"):
            writer.snapshot_journal_digests(None)


@pytest.mark.parametrize("writer", ("registration", "adoption"))
def test_unstaged_snapshot_defers_other_source_writes_until_recovered_and_acked(
    descriptor_record,
    extraction_handoff,
    staging_markdown,
    repo_paths,
    monkeypatch,
    writer,
):
    from brainlib import commands
    from brainlib.extractors.handoff import write_handoff_manifest
    from brainlib.sync_results import SyncResultStore
    from tests.helpers import StaticResolver, make_integrity_inputs
    from tests.helpers_extractors import resolve_test_converter

    store = LedgerStore(repo_paths)
    if writer == "registration":
        store.save(record_for_handoff(extraction_handoff, repo_paths))
        write_handoff_manifest(
            repo_paths,
            run_id="deferred",
            created_at=FIXED_NOW,
            items=(extraction_handoff,),
        )

        def deferred():
            return commands.register_source_extraction(
                repo_paths.root,
                handoff_id=extraction_handoff.handoff_id,
                staging_path=staging_markdown,
                anchors_json='[{"kind":"block","value":"image-1"}]',
                quality_state="ok",
                note="faithful transcription",
                services=commands.CommandServices(
                    web_test_processor,
                    lambda spec: resolve_test_converter(spec).prerequisite_digest,
                ),
            )
    else:
        record, item, _ = make_integrity_inputs(repo_paths.root)
        store.save(record)

        def deferred():
            return commands.adopt_source_version(
                repo_paths.root,
                record.source_id,
                candidate_sha256=item.sha256,
                approval_note="approved",
                resolver=StaticResolver(b"old"),
            )

    with monkeypatch.context() as interruption:
        interruption.setattr(
            SyncResultStore,
            "save_staged",
            Mock(side_effect=OSError("interrupted before receipt publication")),
        )
        first = run_brain_json(
            repo_paths.root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(lambda: mock_web_transport(repo_paths)),
        )
    assert not first["ok"]
    results = SyncResultStore(repo_paths)
    assert results.load_snapshot_continuation() is not None
    assert results.load_pending() is None and results.load_staged() is None
    before = store.load_all()
    blocked = deferred()
    assert not blocked.ok
    assert "snapshot" in blocked.errors[0].message
    assert store.load_all() == before
    assert staging_markdown.is_file()
    assert not list((repo_paths.raw / "_versions").glob("**/*.txt"))
    assert not repo_paths.lock.exists()
    replay = run_brain_json(
        repo_paths.root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(
            Mock(side_effect=AssertionError("recovery must not capture"))
        ),
    )
    assert replay["ok"], replay
    consume_sync_result(
        repo_paths.root, replay["data"]["result_manifest"]["result_id"]
    )
    acknowledged = run_brain_json(
        repo_paths.root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        replay["data"]["result_manifest"]["result_id"],
    )
    assert acknowledged["ok"]
    completed = deferred()
    assert completed.ok, completed
    assert store.load_all() != before


def test_reuse_noop_checkpoint_cannot_return_unpersisted_retrieval(
    captured_ok_descriptor_record, recording_transport, repo_paths
):
    with pytest.raises((OSError, ValueError)):
        run_snapshot_fixture(
            captured_ok_descriptor_record,
            approval=ApprovalClaim("evt_noop", "one URL", "approved"),
            paths=repo_paths,
            transport=recording_transport,
            checkpoint=lambda record: None,
        )


def _with_retrieval_history(record, count):
    version = record.versions[record.active_content_sha256]
    events = tuple(
        replace(
            version.retrieval_events[0],
            approval_event_id=f"evt_retained_{index:04d}",
            approval_note="Approved local fixture capture retained for reproducible snapshot-history regression coverage.",
        )
        for index in range(count)
    )
    return replace(
        record,
        versions={
            **record.versions,
            version.sha256: replace(version, retrieval_events=events),
        },
    )


def _with_retained_versions(record, count, paths):
    version = record.versions[record.active_content_sha256]
    versions = dict(record.versions)
    for index in range(count):
        payload = f"retained prior version {index}\n".encode()
        digest = hashlib.sha256(payload).hexdigest()
        raw_path = PurePosixPath("_web", record.source_id, digest, "snapshot.txt")
        target = paths.raw / raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        versions[digest] = replace(
            version,
            sha256=digest,
            raw_path=raw_path,
            byte_size=len(payload),
            fingerprint=replace(
                version.fingerprint,
                path=raw_path,
                byte_size=len(payload),
                mtime_ns=target.stat().st_mtime_ns,
            ),
            retrieval_events=(
                replace(
                    version.retrieval_events[0],
                    sha256=digest,
                    byte_size=len(payload),
                    approval_event_id=f"evt_prior_version_{index:04d}",
                ),
            ),
        )
    return replace(record, versions=versions)


def test_large_snapshot_history_replays_acknowledges_and_allows_later_capture(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch
):
    from brainlib.sync_results import SyncResultStore

    record = _with_retrieval_history(captured_ok_descriptor_record, 600)
    assert len(json.dumps(record.to_dict()["versions"]).encode("utf-8")) > 256 * 1024
    LedgerStore(repo_paths).save(record)
    first = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    assert len(first["data"]["snapshot"]["source_version"]["retrieval_events"]) == 601
    before = LedgerStore(repo_paths).load_all()
    factory = Mock(side_effect=AssertionError("large-history replay must not capture"))
    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(
            LedgerStore,
            "save",
            Mock(side_effect=AssertionError("replay must not checkpoint")),
        )
        replay = run_brain_json(
            repo_root,
            *snapshot_args(record.source_id),
            services=web_test_services(factory),
        )
    assert replay == first
    assert LedgerStore(repo_paths).load_all() == before
    factory.assert_not_called()
    consume_sync_result(repo_root, first["data"]["result_manifest"]["result_id"])
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        first["data"]["result_manifest"]["result_id"],
    )
    assert acknowledged["ok"], acknowledged
    assert SyncResultStore(repo_paths).load_snapshot_continuation() is None
    later = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        "--approval-event-id",
        "evt_later_capture",
        services=web_test_services(
            lambda: mock_web_transport(repo_paths, b"different later bytes\n")
        ),
    )
    assert later["ok"], later
    final = LedgerStore(repo_paths).load(record.source_id)
    assert len(final.versions) == 2
    assert len(final.versions[record.active_content_sha256].retrieval_events) == 601


def test_compact_snapshot_receipts_do_not_grow_with_retained_history(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch
):
    from brainlib import sync_results
    from brainlib.sync_results import SyncResultStore

    store = LedgerStore(repo_paths)
    # A receipt limit 32x smaller than the legacy result limit must still fit
    # complete results whose retained ledger history exceeds that legacy limit.
    monkeypatch.setattr(sync_results, "_MAX_SNAPSHOT_RECEIPT_BYTES", 8 * 1024)
    receipt_sizes = []
    original_complete = SyncResultStore.complete_inflight

    def observe_receipts(result_store, *args, **kwargs):
        directory = repo_paths.root / ".brain/sync-results"
        payloads = {
            name: (directory / name).read_bytes()
            for name in (
                "snapshot-continuation.json",
                "staged.json",
                "pending.json",
            )
        }
        for payload in payloads.values():
            assert len(payload) <= 8 * 1024
            if any(
                field in payload
                for field in (b'"retrieval_events"', b'"versions"', b'"source_version"')
            ):
                raise AssertionError("retained history leaked into compact receipt")
        receipt_sizes.append({name: len(body) for name, body in payloads.items()})
        original_complete(result_store, *args, **kwargs)

    monkeypatch.setattr(SyncResultStore, "complete_inflight", observe_receipts)
    for count, prior_versions in ((1, 0), (600, 0), (600, 80)):
        record = _with_retrieval_history(captured_ok_descriptor_record, count)
        record = _with_retained_versions(record, prior_versions, repo_paths)
        if count == 600:
            assert len(json.dumps(record.to_dict()).encode()) > 256 * 1024
        store.save(record)
        payload = run_brain_json(
            repo_root,
            *snapshot_args(record.source_id),
            services=web_test_services(lambda: mock_web_transport(repo_paths)),
        )
        assert payload["ok"], payload
        consume_sync_result(repo_root, payload["data"]["result_manifest"]["result_id"])
        acknowledged = run_brain_json(
            repo_root,
            "source",
            "acknowledge-sync-result",
            "--result-id",
            payload["data"]["result_manifest"]["result_id"],
        )
        assert acknowledged["ok"], acknowledged
        acknowledgement = (
            repo_paths.root / ".brain/sync-results/acknowledged.json"
        ).read_bytes()
        assert len(acknowledgement) <= 8 * 1024
        assert b'"source_version"' not in acknowledgement
        receipt_sizes[-1]["acknowledged.json"] = len(acknowledgement)
    assert len(receipt_sizes) == 3
    for name in receipt_sizes[0]:
        sizes = [item[name] for item in receipt_sizes]
        assert max(sizes) - min(sizes) <= 12


@pytest.mark.parametrize("inactive", (False, True))
def test_oversized_compact_snapshot_metadata_fails_before_reuse_checkpoint(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch, inactive
):
    from brainlib.diagnostics import Diagnostic
    from brainlib.sync_results import SyncResultWriter

    # The metadata on its own fits the cap: only the full canonical UTF-8
    # continuation envelope makes it too large (including wrapper fields).
    note = Diagnostic("retained_note", "雪" * ((256 * 1024 - 600) // 3))
    assert (
        len(json.dumps(dataclasses.asdict(note), ensure_ascii=False).encode())
        < 256 * 1024
    )
    record = replace(captured_ok_descriptor_record, diagnostics=(note,))
    if inactive:
        record = replace(record, active_derivation_id=None, state=SourceState.PENDING)
    LedgerStore(repo_paths).save(record)
    original_save = LedgerStore.save
    checkpoints = []
    commits = []

    def save(store, candidate):
        checkpoints.append(candidate)
        original_save(store, candidate)

    monkeypatch.setattr(LedgerStore, "save", save)
    monkeypatch.setattr(SyncResultWriter, "commit", lambda *args: commits.append(args))
    payload = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert not payload["ok"]
    assert checkpoints == []
    assert commits == []
    assert LedgerStore(repo_paths).load(record.source_id) == record


@pytest.mark.parametrize("inactive", (False, True))
@pytest.mark.parametrize(
    "kind",
    (
        "source_snapshot_url",
        "snapshot_staged",
        "snapshot_pending",
        "snapshot_acknowledged",
    ),
)
def test_every_compact_envelope_is_preflighted_before_final_checkpoint(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch, kind, inactive
):
    from brainlib import sync_results

    record = captured_ok_descriptor_record
    if inactive:
        record = replace(record, active_derivation_id=None, state=SourceState.PENDING)
    LedgerStore(repo_paths).save(record)
    original = sync_results._bounded_snapshot_payload
    observed = []

    def reject_envelope(value):
        payload = original(value)
        observed.append(value["kind"])
        if value["kind"] == kind:
            raise ValueError("injected exact-envelope bound failure")
        return payload

    monkeypatch.setattr(sync_results, "_bounded_snapshot_payload", reject_envelope)
    checkpoint = Mock(
        side_effect=AssertionError("preflight must precede final checkpoint")
    )
    commit = Mock(side_effect=AssertionError("preflight must precede event commit"))
    monkeypatch.setattr(LedgerStore, "save", checkpoint)
    monkeypatch.setattr(sync_results.SyncResultWriter, "commit", commit)
    payload = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert not payload["ok"], payload
    assert kind in observed
    checkpoint.assert_not_called()
    commit.assert_not_called()
    assert LedgerStore(repo_paths).load(record.source_id) == record


@pytest.mark.parametrize(
    "change",
    (
        "response",
        "candidate_digest",
        "checkpoint",
        "candidate_history",
        "continuation_reference",
        "reference_digest",
    ),
)
def test_compact_snapshot_digests_and_receipt_bindings_fail_closed(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch, change
):
    from brainlib.sync_results import SyncResultStore

    record = _with_retrieval_history(captured_ok_descriptor_record, 600)
    LedgerStore(repo_paths).save(record)
    first = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    directory = repo_paths.root / ".brain/sync-results"
    continuation_path = directory / "snapshot-continuation.json"
    receipt_path = directory / "pending.json"
    if change in {"response", "candidate_digest", "checkpoint"}:
        value = json.loads(continuation_path.read_text())
        field = {
            "response": "response_sha256",
            "candidate_digest": "candidate_sha256",
            "checkpoint": "checkpoint_sha256",
        }[change]
        value[field] = "f" * 64
        continuation_path.write_text(json.dumps(value))
        # Update the pointer too: response/candidate proof must still reject
        # a well-formed internally linked receipt, not only a stale pointer.
        receipt = json.loads(receipt_path.read_text())
        receipt["continuation_sha256"] = (
            SyncResultStore(repo_paths).load_snapshot_continuation().sha256
        )
        receipt_path.write_text(json.dumps(receipt))
    elif change == "candidate_history":
        candidate = LedgerStore(repo_paths).load(record.source_id)
        version = candidate.versions[candidate.active_content_sha256]
        events = (
            replace(
                version.retrieval_events[0], approval_note="tampered historical note"
            ),
            *version.retrieval_events[1:],
        )
        LedgerStore(repo_paths).save(
            replace(
                candidate,
                versions={
                    **candidate.versions,
                    version.sha256: replace(version, retrieval_events=events),
                },
            )
        )
    else:
        value = json.loads(receipt_path.read_text())
        if change == "continuation_reference":
            value["continuation_sha256"] = "f" * 64
        else:
            value["reference"]["sha256"] = "f" * 64
        receipt_path.write_text(json.dumps(value))
    evidence = continuation_path.read_bytes()
    before = LedgerStore(repo_paths).load_all()
    factory = Mock(side_effect=AssertionError("tamper recovery must not capture"))
    checkpoint = Mock(side_effect=AssertionError("tamper recovery must not checkpoint"))
    monkeypatch.setattr(LedgerStore, "save", checkpoint)
    replay = run_brain_json(
        repo_root, *snapshot_args(record.source_id), services=web_test_services(factory)
    )
    assert not replay["ok"], replay
    assert continuation_path.read_bytes() == evidence
    assert LedgerStore(repo_paths).load_all() == before
    factory.assert_not_called()
    checkpoint.assert_not_called()


@pytest.mark.parametrize("target", ("pending", "acknowledged"))
def test_snapshot_compact_receipt_rejects_substituted_valid_manifest(
    descriptor_record, repo_root, repo_paths, monkeypatch, target
):
    from brainlib.sync_results import SyncResultStore

    first = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: mock_web_transport(repo_paths)),
    )
    assert first["ok"], first
    store = SyncResultStore(repo_paths)
    pending = store.load_pending()
    continuation_path = (
        repo_paths.root / ".brain/sync-results/snapshot-continuation.json"
    )
    original_continuation = continuation_path.read_bytes()
    # A different valid manifest with the same command, timestamp, corpus and
    # event count must not substitute for this continuation's exact event.
    with store.writer("source snapshot-url", pending.generated_at) as writer:
        data = dict(next(store.iter_events(pending.reference)).data)
        data["output_sha256"] = "f" * 64
        writer.emit("new_active_representation", data)
        substituted = writer.finalize(pending.reference.corpus_revision)
    assert substituted != pending.reference
    if target == "acknowledged":
        store.clear_pending(pending.reference)
    receipt_path = repo_paths.root / f".brain/sync-results/{target}.json"
    value = json.loads(receipt_path.read_text())
    value["reference"] = substituted.to_dict()
    receipt_path.write_text(json.dumps(value))
    factory = Mock(side_effect=AssertionError("reference tamper must not capture"))
    monkeypatch.setattr(
        LedgerStore, "save", Mock(side_effect=AssertionError("must not checkpoint"))
    )
    if target == "pending":
        result = run_brain_json(
            repo_root,
            *snapshot_args(descriptor_record.source_id),
            services=web_test_services(factory),
        )
    else:
        result = run_brain_json(
            repo_root,
            "source",
            "acknowledge-sync-result",
            "--result-id",
            substituted.result_id,
        )
    assert not result["ok"], result
    assert continuation_path.read_bytes() == original_continuation
    factory.assert_not_called()


@pytest.mark.parametrize(
    "phase", ("candidate", "commit", "staged", "finalize", "pending")
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="hard-crash fixture requires fork")
def test_large_snapshot_reactivation_hard_failures_preserve_compact_recovery(
    captured_ok_descriptor_record, repo_root, repo_paths, monkeypatch, phase
):
    from datetime import timedelta
    from brainlib.locking import LockMetadata, SourceWriteLock
    from brainlib.sync_results import SyncResultStore, SyncResultWriter

    record = replace(
        _with_retrieval_history(captured_ok_descriptor_record, 600),
        active_derivation_id=None,
        state=SourceState.PENDING,
    )
    LedgerStore(repo_paths).save(record)
    target, method = {
        "candidate": (LedgerStore, "save"),
        "commit": (SyncResultWriter, "commit"),
        "staged": (SyncResultStore, "save_staged"),
        "finalize": (SyncResultWriter, "finalize"),
        "pending": (SyncResultStore, "save_pending"),
    }[phase]
    original = getattr(target, method)

    def stop_after_durable_write(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(71)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(target, method, stop_after_durable_write)
        child = os.fork()
        if child == 0:
            try:
                run_brain_json(
                    repo_root,
                    *snapshot_args(record.source_id),
                    services=web_test_services(lambda: mock_web_transport(repo_paths)),
                )
            finally:
                os._exit(72)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 71
    owner = LockMetadata.from_bytes(repo_paths.lock.read_bytes())
    with SourceWriteLock.acquire(
        repo_paths.lock, now=lambda: owner.started_at + timedelta(hours=2)
    ):
        pass
    result_store = SyncResultStore(repo_paths)
    continuation = result_store.load_snapshot_continuation()
    factory = Mock(
        side_effect=AssertionError("large-history recovery must not capture")
    )
    before = None if phase == "candidate" else LedgerStore(repo_paths).load_all()
    with monkeypatch.context() as replay_patch:
        if phase != "candidate":
            replay_patch.setattr(
                LedgerStore,
                "save",
                Mock(side_effect=AssertionError("must not checkpoint")),
            )
        replay = run_brain_json(
            repo_root,
            *snapshot_args(record.source_id),
            services=web_test_services(factory),
        )
    factory.assert_not_called()
    if phase == "candidate":
        assert not replay["ok"], replay
        assert replay["errors"][0]["code"] == "snapshot_interrupted"
        rollback = LedgerStore(repo_paths).load(record.source_id)
        assert rollback.active_derivation_id is None
        assert (
            len(rollback.versions[rollback.active_content_sha256].retrieval_events)
            == 601
        )
        journal = (repo_paths.root / ".brain/sync-results/inflight.jsonl").read_text()
        assert '"type":"commit"' not in journal
        assert result_store.load_pending() is None
        return
    assert replay["ok"], replay
    assert len(replay["data"]["snapshot"]["source_version"]["retrieval_events"]) == 601
    _assert_compact_snapshot_replay(replay, continuation, before)
    assert LedgerStore(repo_paths).load_all() == before
    events = list(result_store.iter_events(result_store.load_pending().reference))
    assert [event.kind for event in events] == ["new_active_representation"]
    consume_sync_result(
        repo_root, replay["data"]["result_manifest"]["result_id"]
    )
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        replay["data"]["result_manifest"]["result_id"],
    )
    assert acknowledged["ok"], acknowledged
    assert result_store.load_snapshot_continuation() is None


def test_returning_to_older_bytes_reactivates_retained_derivation_once(
    descriptor_record, repo_paths
):
    from tests.helpers_extractors import capture_fixture, LocalWebServer

    with LocalWebServer() as server:
        first = capture_fixture(
            descriptor_record,
            server + "/changed-v1",
            event_id="evt_old",
            paths=repo_paths,
        )
        second = capture_fixture(
            first.record, server + "/changed-v2", event_id="evt_new", paths=repo_paths
        )
    transport = mock_web_transport(repo_paths, b"version one\n")
    store = LedgerStore(repo_paths)
    records = []

    def save(record):
        records.append(record)
        store.save(record)

    processor = Mock()
    result = run_snapshot_fixture(
        second.record,
        approval=ApprovalClaim("evt_back", "one URL", "approved"),
        paths=repo_paths,
        transport=transport,
        processor=processor,
        checkpoint=save,
    )
    assert len(records) == 1
    assert len(records[0].versions) == 2
    assert result.active_representation == first.result.active_representation
    assert result.extraction_result is None
    assert records[0].last_attempt == second.record.last_attempt
    processor.process.assert_not_called()


def test_shell_handoff_recovery_retains_rendered_kind_and_raw_version(
    descriptor_with_render_handoff, repo_paths
):
    record = descriptor_with_render_handoff
    item = load_handoff_item(repo_paths, render_handoff_id(record))
    assert item.kind == "rendered_web_capture"
    assert item.raw_path == record.versions[record.active_content_sha256].raw_path
    assert item.raw_path.parts[0] == "_web"
    recovered = collect_durable_handoffs(
        LedgerStore(repo_paths).load_all(), ExtractorRegistry.load(repo_paths.registry)
    )
    assert recovered == (item,)
    assert record.current_raw_path.suffixes == [".url", ".md"]


def test_sync_reconstructs_rendered_handoff_without_network(
    descriptor_with_render_handoff, repo_paths, repo_root
):
    record = descriptor_with_render_handoff
    factory = Mock(side_effect=AssertionError("sync must not construct web transport"))
    payload = run_brain_json(repo_root, "sync", services=web_test_services(factory))
    assert payload["data"]["handoffs"][0]["kind"] == "rendered_web_capture"
    assert payload["data"]["handoffs"][0]["handoff_id"] == render_handoff_id(record)
    reference = payload["data"]["result_manifest"]
    consumed = consume_sync_result(repo_root, reference["result_id"])
    delivery = consumed["data"]["handoff_delivery"]
    assert delivery is not None
    assert delivery["result_id"] == reference["result_id"]
    assert delivery["item_count"] == 1
    delivery_path = repo_root / delivery["path"]
    delivered = json.loads(delivery_path.read_text(encoding="utf-8"))
    assert delivered["reference"] == reference
    assert delivered["items"][0]["kind"] == "rendered_web_capture"
    assert delivered["items"][0]["handoff_id"] == render_handoff_id(record)
    acknowledged = run_brain_json(
        repo_root, "source", "acknowledge-sync-result", "--result-id", reference["result_id"]
    )
    assert acknowledged["ok"], acknowledged
    retained = LedgerStore(repo_paths).load(record.source_id)
    assert retained.versions[record.active_content_sha256].raw_path.parts[0] == "_web"
    factory.assert_not_called()


def test_snapshot_manifest_binds_rendered_handoff_delivery(
    descriptor_record, repo_paths, repo_root
):
    """A deferred snapshot handoff is a manifest effect, not a response summary."""

    transport = mock_web_transport(
        repo_paths, ROUTES["/render-shell"][2], "text/html", "shell.html"
    )
    payload = run_brain_json(
        repo_root,
        *snapshot_args(descriptor_record.source_id),
        services=web_test_services(lambda: transport),
    )
    assert payload["ok"], payload
    reference = payload["data"]["result_manifest"]
    assert reference["event_counts"]["handoff_source_id"] == 1

    consumed = consume_sync_result(repo_root, reference["result_id"])
    delivery = consumed["data"]["handoff_delivery"]
    assert delivery is not None
    delivered = json.loads((repo_root / delivery["path"]).read_text(encoding="utf-8"))
    assert delivered["reference"] == reference
    assert [item["kind"] for item in delivered["items"]] == [
        "rendered_web_capture"
    ]
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference["result_id"],
    )
    assert acknowledged["ok"], acknowledged


def test_acknowledgement_replay_validates_delivery_after_registration(
    repo_root,
):
    """Registration changes the source, but cannot invalidate an ack replay."""

    from tests.helpers_extractors import PNG_BYTES

    (repo_root / "sources/raw/diagram.png").write_bytes(PNG_BYTES)
    payload = run_brain_json(repo_root, "sync")
    reference = payload["data"]["result_manifest"]
    handoff = payload["data"]["handoffs"][0]
    staging_markdown = (
        repo_root / ".brain/agent-staging" / handoff["handoff_id"] / "result.md"
    )
    staging_markdown.parent.mkdir(parents=True)
    staging_markdown.write_text(
        '<a id="block:image-1"></a>\n## Image 1\nDiagram text\n', encoding="utf-8"
    )
    consumed = consume_sync_result(repo_root, reference["result_id"])
    assert consumed["data"]["handoff_delivery"] is not None
    first_ack = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference["result_id"],
    )
    assert first_ack["ok"], first_ack

    registered = run_brain_json(
        repo_root,
        "source",
        "register-extraction",
        "--handoff-id",
        handoff["handoff_id"],
        "--staging-path",
        str(staging_markdown),
        "--anchors-json",
        '[{"kind":"block","value":"image-1"}]',
        "--quality-state",
        "ok",
        "--note",
        "registered for replay test",
    )
    assert registered["ok"], registered
    replay = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference["result_id"],
    )
    assert replay["ok"], replay
    assert replay["data"]["status"] == "already_acknowledged"


def test_tampered_handoff_delivery_blocks_acknowledgement(repo_root, repo_paths):
    """A receipt cannot repair or acknowledge a swapped delivery artifact."""

    from brainlib.sync_results import SyncResultStore
    from tests.helpers_extractors import PNG_BYTES

    (repo_root / "sources/raw/diagram.png").write_bytes(PNG_BYTES)
    payload = run_brain_json(repo_root, "sync")
    reference = payload["data"]["result_manifest"]
    consumed = consume_sync_result(repo_root, reference["result_id"])
    delivery = consumed["data"]["handoff_delivery"]
    assert delivery is not None
    (repo_root / delivery["path"]).write_text("{}", encoding="utf-8")

    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference["result_id"],
    )
    assert not acknowledged["ok"]
    assert SyncResultStore(repo_paths).load_pending() is not None


def test_captured_web_evidence_passes_full_ledger_validation(repo_root, repo_paths):
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(lambda: transport)
    )
    assert payload["ok"], payload
    validation = run_brain_json(repo_root, "validate", "--full")
    assert validation["ok"], validation


@pytest.mark.parametrize("change", ({"byte_size": 9999}, {"media_type": "image/png"}))
def test_web_active_metadata_must_match_retained_retrieval(
    repo_root, repo_paths, change
):
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(lambda: transport)
    )
    store = LedgerStore(repo_paths)
    record = store.load(payload["data"]["snapshot"]["source_id"])
    changed = replace(record, **change)
    store.save(changed)
    store.write_summary((changed,), generated_at=FIXED_NOW)
    validation = run_brain_json(repo_root, "validate", "--full")
    assert "active_version_metadata_mismatch" in {
        error["code"] for error in validation["errors"]
    }


def test_web_descriptor_metadata_corruption_is_still_rejected(repo_root, repo_paths):
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(lambda: transport)
    )
    record = LedgerStore(repo_paths).load(payload["data"]["snapshot"]["source_id"])
    descriptor = repo_paths.raw / record.current_raw_path
    descriptor.write_text(descriptor.read_text().replace("fixture", "changed"))
    validation = run_brain_json(repo_root, "validate", "--full")
    assert "url_descriptor_mismatch" in {
        error["code"] for error in validation["errors"]
    }


def test_web_version_and_retrieval_size_mismatch_fails_record_validation(
    repo_root, repo_paths
):
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(lambda: transport)
    )
    identifier = payload["data"]["snapshot"]["source_id"]
    path = repo_paths.ledger_dir / (identifier + ".json")
    document = json.loads(path.read_text())
    version = document["versions"][document["active_content_sha256"]]
    version["byte_size"] += 1
    version["fingerprint"]["byte_size"] += 1
    path.write_text(json.dumps(document))
    validation = run_brain_json(repo_root, "validate", "--full")
    assert not validation["ok"]
    assert "retrieval event must match its owning content version bytes" in json.dumps(
        validation
    )


def test_url_edit_without_active_capture_restores_control_metadata(
    repo_root, repo_paths
):
    transport = mock_web_transport(repo_paths)
    payload = run_brain_json(
        repo_root, *snapshot_args(), services=web_test_services(lambda: transport)
    )
    consume_sync_result(repo_root, payload["data"]["result_manifest"]["result_id"])
    acknowledged = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        payload["data"]["result_manifest"]["result_id"],
    )
    assert acknowledged["ok"], acknowledged
    identifier = payload["data"]["snapshot"]["source_id"]
    record = LedgerStore(repo_paths).load(identifier)
    descriptor = repo_paths.raw / record.current_raw_path
    descriptor.write_text(
        descriptor.read_text().replace("example.test/report", "example.test/edited")
    )
    run_brain_json(repo_root, "sync")
    pending = LedgerStore(repo_paths).load(identifier)
    assert pending.active_content_sha256 is None
    assert pending.media_type == "application/x.second-brain-url-descriptor"
    assert pending.byte_size == descriptor.stat().st_size
    validation = run_brain_json(repo_root, "validate", "--full")
    assert validation["ok"], validation


@pytest.mark.parametrize("change", ("content", "raw_path", "recipe"))
def test_rendered_handoff_rejects_stale_provenance_before_staging(
    descriptor_with_render_handoff, repo_paths, repo_root, change
):
    from brainlib.extractors.handoff import _identify, publish_handoff_data

    record = descriptor_with_render_handoff
    item = load_handoff_item(repo_paths, render_handoff_id(record))
    if change == "content":
        item = replace(item, content_sha256="f" * 64)
    elif change == "raw_path":
        item = replace(
            item,
            raw_path=PurePosixPath(
                "_web", record.source_id, record.active_content_sha256, "wrong.html"
            ),
        )
    else:
        item = replace(item, config_sha256="f" * 64)
    item = _identify(item)
    publish_handoff_data(repo_paths, items=(item,), now=FIXED_NOW)
    payload = run_brain_json(
        repo_root,
        *snapshot_args(record.source_id),
        "--rendered-staging-path",
        str(repo_paths.root / ".brain/web-staging/missing.html"),
        "--handoff-id",
        item.handoff_id,
        "--retrieved-at",
        "2026-09-04T12:01:00Z",
        "--final-url",
        "https://example.test/final",
        "--detected-media-type",
        "text/html",
    )
    assert not payload["ok"]
    assert payload["errors"][0]["code"] in {
        "handoff_source_stale",
        "handoff_recipe_stale",
    }


def test_unavailable_html_converter_yields_extraction_handoff(
    descriptor_record, repo_paths
):
    from brainlib.extractors.processor import DeterministicSourceProcessor

    transport = mock_web_transport(
        repo_paths, ROUTES["/static"][2], "text/html", "fact.html"
    )
    result = run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_gap", "one URL", "approved"),
        paths=repo_paths,
        transport=transport,
        processor=DeterministicSourceProcessor(resolve=lambda extractor: None),
    )
    assert result.extraction_result.state is SourceState.NEEDS_AGENT
    handoffs = collect_durable_handoffs(
        LedgerStore(repo_paths).load_all(), ExtractorRegistry.load(repo_paths.registry)
    )
    assert handoffs[0].kind == "extraction"
    assert handoffs[0].raw_path == result.raw_path


@pytest.mark.parametrize(
    "failure", (None, "noop_candidate", "noop_rollback", "output", "input")
)
def test_inactive_web_reactivation_uses_one_checkpoint_and_guard(
    captured_ok_descriptor_record, recording_transport, repo_paths, failure
):
    record = replace(
        captured_ok_descriptor_record,
        state=SourceState.PENDING,
        active_derivation_id=None,
    )
    store = LedgerStore(repo_paths)
    store.save(record)
    calls = []
    processor = Mock()

    def checkpoint(candidate):
        calls.append(candidate)
        if candidate.active_derivation_id is not None:
            guard = ActivationGuard.load(repo_paths, record.source_id)
            assert guard.predecessor == record
            assert guard.rollback.state is SourceState.PENDING
            assert guard.rollback.last_attempt == record.last_attempt
            if failure == "noop_candidate":
                return
            store.save(candidate)
            if failure in {"noop_rollback", "output"}:
                (
                    repo_paths.root
                    / candidate.derivations[candidate.active_derivation_id].output_path
                ).write_bytes(b"corrupt")
            elif failure == "input":
                (
                    repo_paths.raw
                    / candidate.versions[candidate.active_content_sha256].raw_path
                ).write_bytes(b"corrupt")
        elif failure != "noop_rollback":
            store.save(candidate)

    def run():
        return run_snapshot_fixture(
            record,
            approval=ApprovalClaim("evt_reactivate", "one URL", "approved"),
            paths=repo_paths,
            transport=recording_transport,
            processor=processor,
            checkpoint=checkpoint,
        )

    if failure is None:
        result = run()
        assert len(calls) == 1
        assert result.extraction_result is None
        assert (
            result.active_representation.derivation_id
            == captured_ok_descriptor_record.active_derivation_id
        )
        assert store.load(record.source_id).last_attempt == record.last_attempt
    else:
        with pytest.raises((OSError, ValueError)):
            run()
        if failure == "noop_rollback":
            with pytest.raises(ValueError, match="activation recovery"):
                store.load(record.source_id)
            store.recover_activation_guards()
        assert store.load(record.source_id).active_derivation_id is None
    processor.process.assert_not_called()


def _reactivation_records(record):
    from brainlib.ledger import activate_derivation

    predecessor = replace(record, state=SourceState.PENDING, active_derivation_id=None)
    version = predecessor.versions[predecessor.active_content_sha256]
    event = replace(version.retrieval_events[0], approval_event_id="evt_second")
    version = replace(version, retrieval_events=version.retrieval_events + (event,))
    rollback = replace(predecessor, versions={version.sha256: version})
    candidate = activate_derivation(
        rollback,
        record.derivations[record.active_derivation_id],
        now=rollback.updated_at,
    )
    return predecessor, rollback, candidate


@pytest.mark.parametrize("persisted", ("predecessor", "rollback", "candidate"))
def test_reactivation_guard_recovers_any_valid_checkpoint(
    captured_ok_descriptor_record, repo_paths, persisted
):
    predecessor, rollback, candidate = _reactivation_records(
        captured_ok_descriptor_record
    )
    store = LedgerStore(repo_paths)
    store.save(predecessor)
    ActivationGuard.prepare_reactivation(repo_paths, predecessor, rollback, candidate)
    store.save(
        {"predecessor": predecessor, "rollback": rollback, "candidate": candidate}[
            persisted
        ]
    )
    with pytest.raises(ValueError, match="activation recovery"):
        store.load(predecessor.source_id)
    store.recover_activation_guards()
    assert store.load(predecessor.source_id) == rollback


@pytest.mark.parametrize(
    "tamper",
    ("unknown", "bool_schema", "raw_path", "attempt", "derivations", "event_count"),
)
def test_reactivation_guard_decoder_rejects_mismatched_fields(
    captured_ok_descriptor_record, repo_paths, tamper
):
    predecessor, rollback, candidate = _reactivation_records(
        captured_ok_descriptor_record
    )
    store = LedgerStore(repo_paths)
    store.save(predecessor)
    ActivationGuard.prepare_reactivation(repo_paths, predecessor, rollback, candidate)
    guard_path = next(repo_paths.ledger_dir.glob("*.activation-pending"))
    payload = json.loads(guard_path.read_text())
    if tamper == "unknown":
        payload["extra"] = True
    elif tamper == "bool_schema":
        payload["schema_version"] = True
    elif tamper == "raw_path":
        payload["rollback"]["versions"][rollback.active_content_sha256]["raw_path"] = (
            "different"
        )
    elif tamper == "attempt":
        payload["rollback"]["last_attempt"] = None
    elif tamper == "derivations":
        payload["predecessor"]["derivations"] = {}
    else:
        payload["rollback"]["versions"][rollback.active_content_sha256][
            "retrieval_events"
        ].pop()
    guard_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="invalid activation guard"):
        ActivationGuard.load(repo_paths, predecessor.source_id)


def test_rendered_staging_is_saved_as_raw_before_extraction(
    repo_root: Path,
    rendered_staging_file: Path,
    descriptor_with_render_handoff: SourceRecord,
) -> None:
    before = rendered_staging_file.read_bytes()
    payload = run_brain_json(
        repo_root,
        "source",
        "snapshot-url",
        "--source-id",
        descriptor_with_render_handoff.source_id,
        "--rendered-staging-path",
        str(rendered_staging_file),
        "--handoff-id",
        render_handoff_id(descriptor_with_render_handoff),
        "--retrieved-at",
        "2026-09-04T12:01:00Z",
        "--final-url",
        "https://example.test/report",
        "--detected-media-type",
        "text/html",
        "--approval-event-id",
        "evt_render_1",
        "--approval-scope",
        "capture rendered report",
        "--approval-note",
        "user approved this bounded event",
    )
    snapshot = payload["data"]["snapshot"]
    raw = repo_root / "sources/raw" / snapshot["raw_path"]
    assert raw.read_bytes() == before
    assert snapshot["source_version"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert snapshot["active_representation"] is not None
    assert snapshot["corpus_revision"]


def test_render_handoff_cannot_publish_markdown_without_raw_snapshot(
    repo_paths: RepoPaths,
    render_handoff: HandoffItem,
    staging_markdown: Path,
) -> None:
    with pytest.raises(AgentRegistrationError, match="not an extraction handoff"):
        register_staged_agent_extraction(
            handoff=render_handoff,
            record=record_for_handoff(render_handoff, repo_paths),
            staging_path=staging_markdown,
            anchors=(Anchor("section", "one"),),
            quality_state="ok",
            note="must use snapshot-url",
            paths=repo_paths,
            now=FIXED_NOW,
        )


def test_snapshot_json_uses_canonical_envelope(
    repo_root: Path,
    local_web_server: str,
) -> None:
    payload = run_brain_json(
        repo_root,
        "source",
        "snapshot-url",
        "--url",
        f"{local_web_server}/static",
        "--description",
        "fixture",
        "--approval-event-id",
        "evt_envelope",
        "--approval-scope",
        "one fixture URL",
        "--approval-note",
        "user approved",
    )
    assert set(payload) == {"command", "ok", "data", "warnings", "errors"}
    assert payload["command"] == "source snapshot-url"
    assert set(payload["data"]["snapshot"]) >= {
        "source_id",
        "raw_path",
        "content_sha256",
        "source_version",
        "retrieval",
        "extraction_result",
        "active_representation",
        "corpus_revision",
    }


def test_snapshot_result_field_contract_is_exact_for_consumers() -> None:
    assert tuple(field.name for field in dataclasses.fields(SnapshotResult)) == (
        "source_id",
        "raw_path",
        "content_sha256",
        "source_version",
        "retrieval",
        "extraction_result",
        "active_representation",
        "corpus_revision",
    )
