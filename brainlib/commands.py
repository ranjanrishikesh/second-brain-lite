from __future__ import annotations

import copy
import hashlib
import heapq
import itertools
import json
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from .capabilities import probe_capabilities
from .contracts import Anchor, SourceRecord, SourceState, compute_corpus_revision
from .diagnostics import Diagnostic, JSONValue, ValidationIssue, ValidationReport
from .extractors.processor import build_source_processor, validate_max_workers
from .extractors import handoff as agent_handoff
from .inventory import (
    MediaDetector,
    PinnedFile,
    SnapshotNamespace,
    inventory_raw_sources,
    use_stable_file,
)
from .layout import RepoPaths
from .ledger import (
    GitHistoryResolver,
    HistoricalBytesResolver,
    LedgerStore,
    ActivationGuard,
    _reject_duplicate_keys,
    _source_has_coverage_gap,
    adopt_version,
    activate_derivation,
    representation_for,
)
from .locking import (
    SourceWriteLock,
    safe_exception_text,
    safe_lock_backend_available,
)
from .output import CommandResult, sync_report_data
from .registry import (
    ExtractorRegistry,
    ExtractorSpec,
    _run_version_probe,
    prerequisite_digest,
)
from .sync import (
    SourceProcessor,
    SyncReport,
    reconcile_inventory,
    _validate_pinned_process_artifact,
    _new_record,
    _reconcile_url_descriptor,
)
from .sources.web import (
    ApprovalClaim,
    PublicHTTPTransport,
    RenderedCapture,
    SnapshotRequest,
    WebCaptureError,
    WebTransport,
    approval_recorded_at_for,
    canonicalize_url,
    publish_url_descriptor,
    serialize_url_descriptor,
    snapshot_request_identity,
    snapshot_result_data,
    snapshot_url,
)
from .sync_results import (
    PendingSyncResult,
    StagedSyncResult,
    SyncEvent,
    SyncResultReference,
    SyncResultStore,
    SnapshotContinuation,
    PendingSnapshotResult,
    StagedSnapshotResult,
)
from .validation import (
    validate_repository,
    validate_source_ledger,
)

_RIPGREP_INSTALL_RECIPES: dict[str, JSONValue] = {
    "macos_homebrew": ["brew", "install", "ripgrep"],
    "debian_ubuntu_apt": ["sudo", "apt-get", "install", "ripgrep"],
    "windows_winget": ["winget", "install", "BurntSushi.ripgrep.MSVC"],
}
_STATUS_DETAIL_LIMIT = 100
_PRISTINE_LEDGER = b"# Source Ledger\n\nNot initialized. Run `./brain init`.\n"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUS_COVERAGE_CODES = frozenset(
    {
        "active_representation_missing",
        "derivation_output_unavailable",
        "ledger_not_initialized_with_sources",
        "ledgered_raw_path_missing",
        "raw_file_unavailable",
        "source_missing_from_ledger",
        "stale_extracting_state",
    }
)
_T = TypeVar("_T")


class _SourceLockCleanupFailure(Exception):
    def __init__(self, message: str, result: object) -> None:
        super().__init__(message)
        self.result = result


class _SourceLockBodyFailure(Exception):
    def __init__(self, body_error: Exception, cleanup_message: str) -> None:
        super().__init__(str(body_error))
        self.body_error = body_error
        self.cleanup_message = cleanup_message


@dataclass(frozen=True)
class CommandServices:
    processor_factory: Callable[[], SourceProcessor]
    prerequisite_digest: Callable[[ExtractorSpec], str]
    web_transport_factory: Callable[[], WebTransport] = PublicHTTPTransport


DEFAULT_SERVICES = CommandServices(build_source_processor, prerequisite_digest)


def doctor(cwd: Path) -> CommandResult:
    try:
        registry_path = RepoPaths.discover(cwd).registry
    except FileNotFoundError:
        registry_path = cwd / "config" / "extractors.toml"
    python_version = sys.version_info
    ripgrep_available = shutil.which("rg") is not None
    registry_present = registry_path.is_file()
    registry_valid = False
    capability_data: dict[str, JSONValue] = {}
    lock_backend_available = safe_lock_backend_available()
    warnings: list[Diagnostic] = []
    if not ripgrep_available:
        warnings.append(
            Diagnostic(
                "ripgrep_missing",
                "ripgrep is not installed; install recipes are included in data.",
            )
        )
    if not registry_present:
        warnings.append(
            Diagnostic(
                "extractor_registry_missing",
                "config/extractors.toml is missing.",
            )
        )
    else:
        try:
            registry = ExtractorRegistry.load(registry_path)
            registry_valid = True
            capabilities = probe_capabilities(registry, run=_run_version_probe)
            for extractor in registry.extractors:
                for converter in (extractor.preferred, *extractor.fallbacks):
                    capability = capabilities[converter.converter_id]
                    key = (
                        converter.executable
                        or converter.python_distribution
                        or converter.converter_id
                    )
                    capability_data[key] = {
                        "converter_id": capability.converter_id,
                        "available": capability.available,
                        "detected_version": capability.detected_version,
                        "detail": capability.detail,
                        "install_recipes": {
                            platform: list(recipe)
                            for platform, recipe in capability.install_recipes.items()
                        },
                    }
        except (OSError, ValueError) as error:
            warnings.append(
                Diagnostic(
                    "extractor_registry_invalid",
                    f"config/extractors.toml is invalid: {error}",
                )
            )
    if not lock_backend_available:
        warnings.append(
            Diagnostic(
                "source_write_lock_unavailable",
                "Safe source-write locking is unavailable; mutating and validating "
                "commands will fail closed.",
            )
        )
    return CommandResult(
        command="doctor",
        ok=True,
        data={
            "python": {
                "version": f"{python_version.major}.{python_version.minor}.{python_version.micro}",
                "supported": python_version >= (3, 11),
            },
            "ripgrep": {"available": ripgrep_available},
            "extractor_registry": {
                "path": "config/extractors.toml",
                "present": registry_present,
                "valid": registry_valid,
            },
            "source_write_lock": {"available": lock_backend_available},
            "capabilities": capability_data,
            "install_recipes": (
                {} if ripgrep_available else copy.deepcopy(_RIPGREP_INSTALL_RECIPES)
            ),
        },
        warnings=tuple(warnings),
    )


def init_sources(
    cwd: Path,
    *,
    services: CommandServices = DEFAULT_SERVICES,
    max_workers: int | None = None,
) -> CommandResult:
    return _synchronize(cwd, command="init", services=services, max_workers=max_workers)


def sync_sources(
    cwd: Path,
    *,
    services: CommandServices = DEFAULT_SERVICES,
    max_workers: int | None = None,
) -> CommandResult:
    return _synchronize(cwd, command="sync", services=services, max_workers=max_workers)


def _synchronize(
    cwd: Path,
    *,
    command: str,
    services: CommandServices,
    max_workers: int | None = None,
) -> CommandResult:
    try:
        validate_max_workers(max_workers)
        paths = RepoPaths.discover(cwd)
        result_store = SyncResultStore(paths)

        def operation() -> CommandResult:
            result_store.require_no_snapshot_continuation()
            store = LedgerStore(paths)
            store.recover_activation_guards()
            records = store.load_all()
            pending = result_store.load_pending()
            if pending is not None:
                if pending.command != command:
                    raise ValueError(
                        "pending sync result must be recovered by rerunning "
                        f"{pending.command}"
                    )
                if (
                    compute_corpus_revision(records.values())
                    != pending.reference.corpus_revision
                ):
                    raise ValueError(
                        "pending sync result does not match the committed corpus"
                    )
                result_store.complete_inflight(
                    pending.reference,
                    checkpoint_sha256=_ledger_checkpoint_sha256(records),
                )
                replay = _pending_sync_command_result(command, pending, result_store)
                try:
                    store.write_summary(
                        records.values(), generated_at=pending.generated_at
                    )
                except (OSError, ValueError) as error:
                    return _append_sync_failure(
                        replay,
                        "ledger_summary_refresh_failed",
                        "Ledger summary recovery remains pending; rerun "
                        f"{command} to recover: {safe_exception_text(error)}",
                    )
                return replace(replay, acknowledgement=pending.reference)
            staged = result_store.load_staged()
            if staged is not None:
                if staged.command != command:
                    raise ValueError(
                        "staged sync result must be recovered by rerunning "
                        f"{staged.command}"
                    )
                if _ledger_checkpoint_sha256(records) != staged.checkpoint_sha256:
                    raise ValueError(
                        "staged sync result does not match the canonical ledger "
                        "checkpoint"
                    )
                with result_store.writer(
                    command,
                    staged.generated_at,
                    recoverable=True,
                ) as result_writer:
                    if result_writer.journal_sha256() != staged.journal_sha256:
                        raise ValueError(
                            "staged sync result journal changed before recovery"
                        )
                    reference = result_writer.finalize(
                        staged.corpus_revision,
                        event_filter=lambda event: _event_checkpoint_is_current(
                            event, records
                        ),
                    )
                recovered_data = _bind_sync_result_reference(
                    staged.result_data,
                    reference,
                )
                recovered_pending = PendingSyncResult(
                    command,
                    staged.generated_at,
                    reference,
                    recovered_data,
                )
                result_store.save_pending(recovered_pending)
                result_store.complete_inflight(
                    reference,
                    checkpoint_sha256=_ledger_checkpoint_sha256(records),
                )
                replay = _pending_sync_command_result(
                    command, recovered_pending, result_store
                )
                try:
                    store.write_summary(
                        records.values(), generated_at=staged.generated_at
                    )
                except (OSError, ValueError) as error:
                    return _append_sync_failure(
                        replay,
                        "ledger_summary_refresh_failed",
                        "Canonical ledger checkpoints and the durable sync result "
                        "committed, but the generated ledger projection failed; "
                        f"rerun {command} to recover: {safe_exception_text(error)}",
                    )
                return replace(replay, acknowledgement=reference)
            registry = ExtractorRegistry.load(paths.registry)
            inventory = inventory_raw_sources(paths, MediaDetector())
            digests = {
                extractor.extractor_id: services.prerequisite_digest(extractor)
                for extractor in registry.extractors
            }
            _validate_prerequisite_map(registry, digests)
            processor = services.processor_factory()
            with result_store.writer(
                command,
                datetime.now(timezone.utc),
                recoverable=True,
            ) as result_writer:
                operation_now = result_writer.generated_at
                updated, report = reconcile_inventory(
                    inventory,
                    records,
                    registry=registry,
                    processor=processor,
                    paths=paths,
                    prerequisite_digests=digests,
                    max_workers=max_workers,
                    checkpoint=store.save,
                    now=operation_now,
                    event_sink=result_writer.emit,
                    event_commit=result_writer.commit,
                )
                committed = store.load_all()
                if (
                    compute_corpus_revision(committed.values())
                    != report.corpus_revision
                ):
                    raise ValueError(
                        "completed sync report does not match canonical ledger"
                    )
                # Every durable work item must be published before a sync result
                # can be staged, including attempts skipped during resumption.
                handoff_data = agent_handoff.publish_handoff_data(
                    paths,
                    items=agent_handoff.collect_durable_handoffs(committed, registry),
                    now=operation_now,
                )
                provisional = _sync_command_result(command, report)
                provisional = replace(
                    provisional, data={**provisional.data, **handoff_data}
                )
                staged = StagedSyncResult(
                    command,
                    operation_now,
                    report.corpus_revision,
                    _ledger_checkpoint_sha256(committed),
                    result_writer.journal_sha256(),
                    result_store.compact_sync_data(provisional.data),
                )
                result_store.save_staged(staged)
                reference = result_writer.finalize(
                    report.corpus_revision,
                    event_filter=lambda event: _event_checkpoint_is_current(
                        event, committed
                    ),
                )
                updated = committed
            report = replace(
                report,
                hashed_path_count=reference.event_counts["hashed_path"],
                new_active_representation_count=reference.event_counts[
                    "new_active_representation"
                ],
                citation_rewrite_count=reference.event_counts["citation_rewrite"],
                handoff_source_id_count=reference.event_counts["handoff_source_id"],
                coverage_gap_count=reference.event_counts["coverage_gap"],
                result_manifest=reference,
            )
            command_result = _sync_command_result(command, report)
            command_result = replace(
                command_result, data={**command_result.data, **handoff_data}
            )
            result_store.save_pending(
                PendingSyncResult(
                    command,
                    operation_now,
                    reference,
                    result_store.compact_sync_data(command_result.data),
                )
            )
            result_store.complete_inflight(
                reference,
                checkpoint_sha256=_ledger_checkpoint_sha256(updated),
            )
            try:
                store.write_summary(updated.values(), generated_at=operation_now)
            except (OSError, ValueError) as error:
                return _append_sync_failure(
                    command_result,
                    "ledger_summary_refresh_failed",
                    "Canonical ledger checkpoints and the durable sync result "
                    "committed, but the generated ledger projection failed; "
                    f"rerun {command} to recover: {safe_exception_text(error)}",
                )
            return replace(command_result, acknowledgement=reference)

        result = _run_with_source_lock(paths, operation)
        return result
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return _operation_failure(command, error)


def acknowledge_sync_result(cwd: Path, result: CommandResult) -> None:
    """Acknowledge a sync result only after its consumer has received it."""

    reference = result.acknowledgement
    if (
        result.command not in {"init", "sync", "source snapshot-url"}
        or reference is None
    ):
        raise ValueError("command result has no sync acknowledgement")
    if result.data.get("result_manifest") != reference.to_dict():
        raise ValueError("command result acknowledgement does not match its manifest")
    acknowledgement = acknowledge_sync_result_id(cwd, reference.result_id)
    if not acknowledgement.ok:
        raise ValueError(acknowledgement.errors[0].message)


def consume_sync_result_id(cwd: Path, result_id: str) -> CommandResult:
    """Durably consume the exact current sync result without acknowledging it."""

    command_name = "source consume-sync-result"
    try:
        paths = RepoPaths.discover(cwd)
        result_store = SyncResultStore(paths)

        def operation():
            return result_store.consume_pending(result_id)

        consumed = _run_with_source_lock(paths, operation)
        return CommandResult(
            command_name,
            ok=True,
            data={
                "result_id": consumed.reference.result_id,
                "status": consumed.status,
                "manifest_path": consumed.manifest_path.as_posix(),
                "corpus_revision": consumed.reference.corpus_revision,
                "event_counts": dict(sorted(consumed.event_counts.items())),
                "effect_digest": consumed.effect_digest,
                "handoff_delivery": None
                if consumed.handoff_delivery is None
                else consumed.handoff_delivery.to_dict(),
            },
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return CommandResult(
            command_name,
            ok=False,
            data={"result_id": result_id},
            errors=(
                Diagnostic(
                    "sync_result_consumption_failed",
                    safe_exception_text(error),
                ),
            ),
        )


def acknowledge_sync_result_id(cwd: Path, result_id: str) -> CommandResult:
    command_name = "source acknowledge-sync-result"
    try:
        paths = RepoPaths.discover(cwd)
        result_store = SyncResultStore(paths)

        def operation() -> tuple[str, str]:
            pending = result_store.load_pending()
            acknowledged = result_store.load_acknowledged()
            if pending is not None and pending.reference.result_id == result_id:
                result_store.require_consumption_receipt(pending.reference)
                if result_store.load_staged() is not None:
                    records = LedgerStore(paths).load_all()
                    result_store.complete_inflight(
                        pending.reference,
                        checkpoint_sha256=_ledger_checkpoint_sha256(records),
                    )
                result_store.clear_pending(pending.reference)
                if pending.command == "source snapshot-url":
                    result_store.clear_snapshot_continuation(pending.reference)
                return pending.command, "acknowledged"
            if (
                acknowledged is not None
                and acknowledged.reference.result_id == result_id
            ):
                # A client may have registered the delivered handoff after the
                # first acknowledgement. Replay still validates the immutable
                # receipt and delivery, but must not require the old source
                # record to remain current.
                result_store.require_consumption_receipt(
                    acknowledged.reference, validate_current_sources=False
                )
                if result_store.load_staged() is not None:
                    records = LedgerStore(paths).load_all()
                    result_store.complete_inflight(
                        acknowledged.reference,
                        checkpoint_sha256=_ledger_checkpoint_sha256(records),
                    )
                if acknowledged.command == "source snapshot-url":
                    result_store.clear_snapshot_continuation(acknowledged.reference)
                return acknowledged.command, "already_acknowledged"
            raise ValueError(
                "result_id does not name the pending or last acknowledged sync result"
            )

        sync_command, status_value = _run_with_source_lock(paths, operation)
        return CommandResult(
            command_name,
            ok=True,
            data={
                "result_id": result_id,
                "sync_command": sync_command,
                "status": status_value,
            },
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return CommandResult(
            command_name,
            ok=False,
            data={"result_id": result_id},
            errors=(
                Diagnostic(
                    "sync_result_acknowledgement_failed",
                    safe_exception_text(error),
                ),
            ),
        )


def _sync_command_result(command: str, report: SyncReport) -> CommandResult:
    gaps = bool(report.coverage_gap_count)
    data = sync_report_data(report)
    data = {
        "status": "complete_with_gaps" if gaps else "complete",
        **data,
    }
    return CommandResult(
        command,
        ok=not gaps,
        data=data,
        errors=(
            (
                Diagnostic(
                    "source_coverage_gaps",
                    "Synchronization completed with unresolved source coverage gaps.",
                ),
            )
            if gaps
            else ()
        ),
    )


def _pending_sync_command_result(
    command: str,
    pending: PendingSyncResult,
    result_store: SyncResultStore,
) -> CommandResult:
    gaps = bool(pending.result_data.get("coverage_gap_count", 0))
    return CommandResult(
        command,
        ok=not gaps,
        data=result_store.restore_sync_data(pending.result_data),
        errors=(
            (
                Diagnostic(
                    "source_coverage_gaps",
                    "Synchronization completed with unresolved source coverage gaps.",
                ),
            )
            if gaps
            else ()
        ),
    )


def _bind_sync_result_reference(
    result_data: Mapping[str, JSONValue],
    reference: SyncResultReference,
) -> dict[str, JSONValue]:
    data = dict(result_data)
    data["result_manifest"] = reference.to_dict()
    for event_kind, field_name in {
        "hashed_path": "hashed_path_count",
        "new_active_representation": "new_active_representation_count",
        "citation_rewrite": "citation_rewrite_count",
        "handoff_source_id": "handoff_source_id_count",
        "coverage_gap": "coverage_gap_count",
    }.items():
        data[field_name] = reference.event_counts[event_kind]
    data["status"] = (
        "complete_with_gaps" if reference.event_counts["coverage_gap"] else "complete"
    )
    return data


def _ledger_checkpoint_sha256(records: Mapping[str, SourceRecord]) -> str:
    digest = hashlib.sha256()
    for source_id, record in sorted(records.items()):
        if source_id != record.source_id:
            raise ValueError("record mapping key must match source_id")
        payload = (
            json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _event_checkpoint_is_current(
    event: SyncEvent,
    records: Mapping[str, SourceRecord],
) -> bool:
    source_id = event.data.get("source_id")
    record_sha256 = event.data.get("record_sha256")
    if not isinstance(source_id, str) or not isinstance(record_sha256, str):
        return False
    record = records.get(source_id)
    if record is None:
        return False
    return _ledger_record_sha256(record) == record_sha256


def _ledger_record_sha256(record: SourceRecord) -> str:
    payload = (
        json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _append_sync_failure(
    result: CommandResult,
    code: str,
    message: str,
) -> CommandResult:
    return CommandResult(
        result.command,
        ok=False,
        data=result.data,
        warnings=result.warnings,
        errors=(*result.errors, Diagnostic(code, message)),
    )


def status(cwd: Path) -> CommandResult:
    try:
        paths = RepoPaths.discover(cwd)
        store = LedgerStore(paths)
        records = store.load_all()
        inventory = inventory_raw_sources(paths, MediaDetector())
        summary = store.read_summary()
        source_report = validate_source_ledger(
            paths,
            records,
            inventory=inventory,
            summary_bytes=summary,
        )
        inventory_issue_keys = {
            (
                diagnostic.code,
                diagnostic.message,
                diagnostic.path,
            )
            for diagnostic in inventory.skipped
        }
        coverage_issues: list[ValidationIssue] = []
        coherence_issues: list[ValidationIssue] = []
        for issue in source_report.issues:
            destination = (
                coverage_issues
                if issue.code in _STATUS_COVERAGE_CODES
                or (issue.code, issue.message, issue.path) in inventory_issue_keys
                else coherence_issues
            )
            destination.append(issue)
        if coherence_issues:
            first = coherence_issues[0]
            raise ValueError(
                f"source snapshot is invalid ({first.code}: {first.message})"
            )
        pristine = (
            not records
            and not inventory.items
            and not inventory.skipped
            and summary == _PRISTINE_LEDGER
        )
        counts = {state.value: 0 for state in SourceState}
        for record in records.values():
            counts[record.state.value] += 1
        warning_details = heapq.nsmallest(
            _STATUS_DETAIL_LIMIT,
            itertools.chain(
                (
                    _status_diagnostic(record.source_id, diagnostic)
                    for record in records.values()
                    if _source_has_coverage_gap(record)
                    for diagnostic in record.diagnostics
                ),
                (_status_issue(issue) for issue in coverage_issues),
            ),
            key=_status_detail_key,
        )
        failure_records = sorted(
            (
                record
                for record in records.values()
                if record.state in {SourceState.FAILED, SourceState.INTEGRITY_ERROR}
            ),
            key=lambda record: record.source_id,
        )
        needs_agent = sorted(
            record.source_id
            for record in records.values()
            if record.state is SourceState.NEEDS_AGENT
        )
        has_gaps = bool(coverage_issues) or any(
            _source_has_coverage_gap(record) for record in records.values()
        )
        status_value = (
            "not_initialized"
            if pristine
            else "complete_with_gaps"
            if has_gaps
            else "complete"
        )
        return CommandResult(
            "status",
            ok=not has_gaps,
            data={
                "status": status_value,
                "corpus_revision": (
                    None if pristine else compute_corpus_revision(records.values())
                ),
                "state_counts": counts,
                "warnings": warning_details[:_STATUS_DETAIL_LIMIT],
                "failures": [
                    {
                        "source_id": record.source_id,
                        "state": record.state.value,
                        "diagnostics": [
                            _status_diagnostic(record.source_id, diagnostic)
                            for diagnostic in record.diagnostics[:_STATUS_DETAIL_LIMIT]
                        ],
                    }
                    for record in failure_records[:_STATUS_DETAIL_LIMIT]
                ],
                "needs_agent": needs_agent[:_STATUS_DETAIL_LIMIT],
                "handoffs": needs_agent[:_STATUS_DETAIL_LIMIT],
                "detail_limit": _STATUS_DETAIL_LIMIT,
            },
            errors=(
                (
                    Diagnostic(
                        "source_coverage_gaps",
                        "Source status contains unresolved coverage gaps.",
                    ),
                )
                if has_gaps
                else ()
            ),
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return _operation_failure("status", error)


def validate(cwd: Path, *, full: bool = False) -> CommandResult:
    try:
        paths = RepoPaths.discover(cwd)
    except FileNotFoundError as error:
        return CommandResult(
            command="validate",
            ok=False,
            data={"reports": []},
            errors=(Diagnostic("repository_not_found", str(error)),),
        )

    try:
        return _validation_command_result(
            validate_repository(paths, LedgerStore(paths), full=full)
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return _operation_failure("validate", error)


def _validation_command_result(
    reports: tuple[ValidationReport, ...],
) -> CommandResult:
    issues = tuple(issue for report in reports for issue in report.issues)
    warnings = tuple(
        Diagnostic(issue.code, issue.message, issue.path, issue.details)
        for issue in issues
        if issue.severity == "warning"
    )
    errors = tuple(
        Diagnostic(issue.code, issue.message, issue.path, issue.details)
        for issue in issues
        if issue.severity == "error"
    )
    return CommandResult(
        command="validate",
        ok=not errors,
        data={
            "reports": [
                {
                    "checks": list(report.checks),
                    "issues": [
                        {
                            "severity": issue.severity,
                            "code": issue.code,
                            "message": issue.message,
                            "path": None
                            if issue.path is None
                            else issue.path.as_posix(),
                            "details": dict(issue.details),
                        }
                        for issue in report.issues
                    ],
                    "corpus_revision": report.corpus_revision,
                }
                for report in reports
            ]
        },
        warnings=warnings,
        errors=errors,
    )


def adopt_source_version(
    cwd: Path,
    source_id: str,
    *,
    candidate_sha256: str,
    approval_note: str,
    resolver: HistoricalBytesResolver | None = None,
    now: datetime | None = None,
) -> CommandResult:
    try:
        paths = RepoPaths.discover(cwd)
        return _run_with_source_lock(
            paths,
            lambda: _adopt_source_version_unlocked(
                cwd,
                source_id,
                candidate_sha256=candidate_sha256,
                approval_note=approval_note,
                resolver=resolver,
                now=now,
            ),
        )
    except _SourceLockCleanupFailure as error:
        if isinstance(error.result, CommandResult):
            result = error.result
            code = (
                "source_lock_cleanup_recovery_required"
                if result.ok
                else "source_lock_cleanup_failed"
            )
            message = (
                f"{error}; the adoption result was committed, but lock cleanup "
                "could not be proven; rerun status and inspect the source lock before retrying."
                if result.ok
                else str(error)
            )
            return _append_source_lock_cleanup_failure(
                result,
                code=code,
                message=message,
            )
        raise
    except KeyboardInterrupt:
        raise
    except _SourceLockBodyFailure as error:
        return CommandResult(
            command="source adopt-version",
            ok=False,
            data={"source_id": source_id, "citation_rewrites": []},
            errors=(
                Diagnostic("source_version_adoption_failed", str(error.body_error)),
                Diagnostic("source_lock_cleanup_failed", error.cleanup_message),
            ),
        )
    except Exception as error:
        return CommandResult(
            command="source adopt-version",
            ok=False,
            data={"source_id": source_id, "citation_rewrites": []},
            errors=(Diagnostic("source_version_adoption_failed", str(error)),),
        )


def _adopt_source_version_unlocked(
    cwd: Path,
    source_id: str,
    *,
    candidate_sha256: str,
    approval_note: str,
    resolver: HistoricalBytesResolver | None = None,
    now: datetime | None = None,
) -> CommandResult:
    try:
        paths = RepoPaths.discover(cwd)
        SyncResultStore(paths).require_no_snapshot_continuation()
        store = LedgerStore(paths)
        records = store.load_all()
        record = records.get(source_id)
        if record is None:
            raise FileNotFoundError(f"ledger record not found: {source_id}")
        inventory = inventory_raw_sources(paths, MediaDetector())
        candidate = next(
            (
                item
                for item in inventory.items
                if item.fingerprint.path == record.current_raw_path
            ),
            None,
        )
        if candidate is None:
            raise ValueError("current raw source is not a safe regular inventory item")
        observed = replace(candidate, sha256=candidate_sha256)
        adoption = adopt_version(
            record,
            observed,
            paths=paths,
            resolver=(GitHistoryResolver(paths.root) if resolver is None else resolver),
            approval_note=approval_note,
            candidate_sha256=candidate_sha256,
            now=datetime.now(timezone.utc) if now is None else now,
        )
        next_records = {**records, source_id: adoption.record}
        store.render_summary(
            next_records.values(), generated_at=adoption.record.updated_at
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        return CommandResult(
            command="source adopt-version",
            ok=False,
            data={"source_id": source_id, "citation_rewrites": []},
            errors=(Diagnostic("source_version_adoption_failed", str(error)),),
        )

    warnings: list[Diagnostic] = []
    save_error: OSError | ValueError | None = None
    try:
        store.save(adoption.record)
    except (OSError, ValueError) as error:
        save_error = error

    if not store.is_canonical_record_current(adoption.record):
        reason = (
            "canonical ledger record could not be proven after save"
            if save_error is None
            else str(save_error)
        )
        return CommandResult(
            command="source adopt-version",
            ok=False,
            data={"source_id": source_id, "citation_rewrites": []},
            errors=(Diagnostic("source_version_adoption_failed", reason),),
        )

    if save_error is not None:
        warnings.append(
            Diagnostic(
                "ledger_record_recovery_required",
                "The adopted ledger record was published, but its durability or "
                f"pinned-directory verification needs recovery: {save_error}",
            )
        )

    try:
        store.write_summary(
            next_records.values(), generated_at=adoption.record.updated_at
        )
    except (OSError, ValueError) as error:
        warnings.append(
            Diagnostic(
                "ledger_summary_refresh_failed",
                f"The adoption committed, but sources/ledger.md needs regeneration: {error}",
            )
        )
    return CommandResult(
        command="source adopt-version",
        ok=True,
        data={
            "source_id": source_id,
            "active_content_sha256": adoption.record.active_content_sha256,
            "citation_rewrites": [
                rewrite.to_dict() for rewrite in adoption.citation_rewrites
            ],
        },
        warnings=tuple(warnings),
    )


def register_source_extraction(
    cwd: Path,
    *,
    handoff_id: str,
    staging_path: Path,
    anchors_json: str,
    quality_state: str,
    note: str,
    services: CommandServices = DEFAULT_SERVICES,
    now: datetime | None = None,
) -> CommandResult:
    command = "source register-extraction"
    try:
        paths = RepoPaths.discover(cwd)

        def operation() -> CommandResult:
            SyncResultStore(paths).require_no_snapshot_continuation()
            handoff = agent_handoff.load_handoff_item(paths, handoff_id)
            store = LedgerStore(paths)
            store.recover_activation_guards()
            records = store.load_all()
            record = records.get(handoff.source_id)
            if record is None:
                raise agent_handoff.AgentRegistrationError(
                    "handoff source is not retained", code="handoff_source_stale"
                )
            registry = ExtractorRegistry.load(paths.registry)
            extractor = registry.select(
                record.media_type, agent_handoff.source_content_path(record)
            )
            digest = (
                "" if extractor is None else services.prerequisite_digest(extractor)
            )
            agent_handoff.require_current_recipe(handoff, record, extractor, digest)
            document = json.loads(
                anchors_json, object_pairs_hook=_reject_duplicate_keys
            )
            if (
                type(document) is not list
                or not document
                or any(
                    type(item) is not dict or set(item) != {"kind", "value"}
                    for item in document
                )
            ):
                raise agent_handoff.AgentRegistrationError(
                    "anchors-json must be an array of kind/value objects"
                )
            anchors = tuple(Anchor(item["kind"], item["value"]) for item in document)
            timestamp = datetime.now(timezone.utc) if now is None else now
            with agent_handoff.staged_agent_file(
                paths, handoff, staging_path
            ) as staging:
                result = agent_handoff.register_staged_agent_extraction(
                    handoff=handoff,
                    record=record,
                    staging_path=staging_path,
                    anchors=anchors,
                    quality_state=quality_state,
                    note=note,
                    paths=paths,
                    now=timestamp,
                )
                derivation = result.derivation
                assert derivation is not None
                staged_checksum = hashlib.sha256(staging.body).hexdigest()
                if (
                    staged_checksum != derivation.output_sha256
                    or len(staging.body) != derivation.output_byte_size
                ):
                    raise agent_handoff.AgentRegistrationError(
                        "staging changed before publication"
                    )
                replay = record.active_derivation_id == derivation.derivation_id
                guard: ActivationGuard | None = None
                final = record

                def activate(artifact: PinnedFile) -> None:
                    nonlocal final, guard
                    _validate_pinned_process_artifact(derivation, artifact)
                    staging.revalidate()
                    if replay:
                        return
                    # ActivationGuard's recovery format requires this exact
                    # durable EXTRACTING predecessor before the active candidate.
                    extracting = replace(
                        record,
                        state=SourceState.EXTRACTING,
                        last_attempt=replace(
                            record.last_attempt,
                            outcome=SourceState.EXTRACTING,
                            attempted_at=timestamp,
                        ),
                        inspected_at=timestamp,
                        updated_at=timestamp,
                    )
                    previous_codes = set(record.last_attempt.diagnostic_codes)
                    prepared = replace(
                        extracting,
                        last_attempt=result.attempt,
                        diagnostics=tuple(
                            d
                            for d in record.diagnostics
                            if d.code not in previous_codes
                        )
                        + result.diagnostics,
                    )
                    final = activate_derivation(prepared, derivation, now=timestamp)
                    store.render_summary(
                        {**records, final.source_id: final}.values(),
                        generated_at=timestamp,
                    )
                    store.save(extracting)
                    guard = ActivationGuard.prepare(paths, extracting, final)
                    store.save(final)
                    staging.revalidate()

                try:
                    use_stable_file(
                        paths,
                        SnapshotNamespace.EXTRACTED,
                        derivation.output_path,
                        activate,
                        include_sha256=True,
                    )
                    staging.revalidate()
                    if guard is not None:
                        # The guard intentionally blocks ordinary readers until
                        # the candidate's exact bytes and artifact post-proof pass.
                        guard._clear_after_checkpoint(final)
                except BaseException:
                    if guard is not None:
                        try:
                            store.save(guard.rollback)
                        except Exception:
                            # A save may raise either side of publication. A
                            # repeated failure leaves the guard for locked recovery.
                            store.save(guard.rollback)
                        guard._clear_after_checkpoint(guard.rollback)
                    raise
                if not store.is_canonical_record_current(final):
                    raise ValueError(
                        "agent activation no longer matches the ledger checkpoint"
                    )
                committed = store.load_all()
                store.write_summary(committed.values(), generated_at=timestamp)
                representation = representation_for(
                    final, handoff.content_sha256, derivation.derivation_id
                )
                if representation is None:
                    raise ValueError("agent activation has no representation")
                data = {
                    "source_id": final.source_id,
                    "content_sha256": handoff.content_sha256,
                    "derivation_id": derivation.derivation_id,
                    "output_path": derivation.output_path.as_posix(),
                    "active_representation": {
                        "source_id": representation.source_id,
                        "content_sha256": representation.content_sha256,
                        "derivation_id": representation.derivation_id,
                        "raw_path": representation.raw_path.as_posix(),
                        "extracted_path": representation.extracted_path.as_posix(),
                        "output_sha256": representation.output_sha256,
                        "quality_state": representation.quality_state,
                        "anchors": [
                            {"kind": a.kind, "value": a.value}
                            for a in representation.anchors
                        ],
                    },
                    "corpus_revision": compute_corpus_revision(committed.values()),
                }
                staging.remove()
                return CommandResult(command, True, {"registration": data})

        return _run_with_source_lock(paths, operation)
    except agent_handoff.AgentRegistrationError as error:
        return CommandResult(
            command, False, {}, errors=(Diagnostic(error.code, str(error)),)
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return _operation_failure(command, error)


def snapshot_source_url(
    cwd: Path,
    *,
    source_id: str | None,
    url: str | None,
    description: str | None,
    approval: ApprovalClaim,
    rendered_staging_path: Path | None = None,
    handoff_id: str | None = None,
    retrieved_at: datetime | None = None,
    final_url: str | None = None,
    redirect_urls: tuple[str, ...] = (),
    detected_media_type: str | None = None,
    services: CommandServices = DEFAULT_SERVICES,
    now: datetime | None = None,
) -> CommandResult:
    command = "source snapshot-url"
    try:
        paths = RepoPaths.discover(cwd)
        timestamp = datetime.now(timezone.utc) if now is None else now

        def operation() -> CommandResult:
            from .inventory import source_id_for_url_descriptor

            store = LedgerStore(paths)
            store.recover_activation_guards()
            records = store.load_all()
            approval_recorded_at_for(records.values(), approval, now=timestamp)
            if (source_id is None) == (url is None):
                raise WebCaptureError("supply exactly one of source-id or URL")
            rendered = None
            if rendered_staging_path is not None:
                if (
                    retrieved_at is None
                    or final_url is None
                    or detected_media_type is None
                ):
                    raise WebCaptureError(
                        "rendered capture requires retrieved-at, final-url, and detected-media-type"
                    )
                rendered = RenderedCapture(
                    rendered_staging_path,
                    retrieved_at,
                    final_url,
                    redirect_urls,
                    detected_media_type,
                    handoff_id,
                )
            elif (
                handoff_id is not None
                or retrieved_at is not None
                or final_url is not None
                or redirect_urls
                or detected_media_type is not None
            ):
                raise WebCaptureError(
                    "browser capture metadata requires rendered-staging-path"
                )
            if url is not None:
                if description is None:
                    raise WebCaptureError("an ad-hoc URL requires description")
                canonical_url = canonicalize_url(url)
                serialize_url_descriptor(
                    canonical_url=canonical_url,
                    description=description,
                    added=timestamp.date(),
                )
                selector = {
                    "kind": "url",
                    "url": canonical_url,
                    "description": description.strip(),
                }
                inventory = None
            else:
                if description is not None:
                    raise WebCaptureError("description belongs to the ad-hoc URL form")
                selected_record = records.get(source_id)
                if selected_record is None or selected_record.url_descriptor is None:
                    raise WebCaptureError(
                        "source-id must name a retained URL descriptor"
                    )
                relative = selected_record.current_raw_path
                inventory = inventory_raw_sources(paths, MediaDetector())
                item = next(
                    (
                        item
                        for item in inventory.items
                        if item.fingerprint.path == relative
                    ),
                    None,
                )
                if item is None or item.url_descriptor is None:
                    raise WebCaptureError("URL descriptor is missing or invalid")
                selector = {
                    "kind": "source_id",
                    "source_id": source_id,
                    "url": canonicalize_url(item.url_descriptor.url),
                }
            identity = snapshot_request_identity(
                paths=paths, selector=selector, approval=approval, rendered=rendered
            )
            result_store = SyncResultStore(paths)
            continuation = result_store.load_snapshot_continuation()
            if continuation is not None:
                if continuation.request_identity != identity:
                    raise WebCaptureError(
                        "a different snapshot request is awaiting recovery or acknowledgement",
                        code="snapshot_request_mismatch",
                    )
                return _finish_snapshot_continuation(
                    paths, store, result_store, continuation, records
                )
            if (
                result_store.load_pending() is not None
                or result_store.load_staged() is not None
            ):
                raise WebCaptureError(
                    "an existing result must be recovered and acknowledged before capture",
                    code="snapshot_recovery_required",
                )
            if url is not None:
                descriptor = publish_url_descriptor(
                    paths,
                    canonical_url=canonical_url,
                    description=description,
                    added=timestamp.date(),
                )
                relative = descriptor.path
                selected_record = next(
                    (r for r in records.values() if r.current_raw_path == relative),
                    None,
                )
            if inventory is None:
                inventory = inventory_raw_sources(paths, MediaDetector())
            item = next(
                (item for item in inventory.items if item.fingerprint.path == relative),
                None,
            )
            if item is None or item.url_descriptor is None:
                raise WebCaptureError("URL descriptor is missing or invalid")
            if selected_record is None:
                identifier = source_id_for_url_descriptor(item.url_descriptor)
                if identifier in records:
                    raise WebCaptureError(
                        "descriptor source identity collides with an existing record"
                    )
                record = _new_record(
                    item, source_id=identifier, checksum=None, now=timestamp
                )
            else:
                record = _reconcile_url_descriptor(selected_record, item, now=timestamp)
            if record != selected_record:
                store.save(record)
                if not store.is_canonical_record_current(record):
                    raise WebCaptureError("URL descriptor checkpoint was not persisted")
            records[record.source_id] = record
            registry = ExtractorRegistry.load(paths.registry)
            with result_store.writer(
                command, timestamp, recoverable=True
            ) as result_writer:
                prepared_event = None

                def emit(kind, data):
                    nonlocal prepared_event
                    result_writer.emit(kind, data)
                    if kind == "new_active_representation":
                        prepared_event = SyncEvent(kind, data)

                def prepare(planned, candidate):
                    nonlocal continuation
                    data = {"snapshot": _snapshot_result_data(planned)}
                    data.update(
                        agent_handoff.publish_handoff_data(
                            paths,
                            items=agent_handoff.collect_durable_handoffs(
                                {candidate.source_id: candidate}, registry
                            ),
                            now=timestamp,
                        )
                    )
                    if data["handoffs"]:
                        result_writer.emit(
                            "handoff_source_id",
                            {
                                "source_id": candidate.source_id,
                                "record_sha256": _ledger_record_sha256(candidate),
                            },
                        )
                    before, after = result_writer.snapshot_journal_digests(
                        prepared_event
                    )
                    continuation = SnapshotContinuation.prepare(
                        request_identity=identity,
                        generated_at=result_writer.generated_at,
                        candidate=candidate,
                        checkpoint_sha256=_ledger_checkpoint_sha256(
                            {**records, candidate.source_id: candidate}
                        ),
                        result_data=data,
                        journal_sha256=before,
                        committed_journal_sha256=after,
                        event=prepared_event,
                    )
                    result_store.save_snapshot_continuation(continuation)

                # Recovery and the claim precede factory construction. Rendered
                # imports never construct a transport or read remote state.
                transport = (
                    services.web_transport_factory() if rendered is None else None
                )
                result = snapshot_url(
                    SnapshotRequest(record.source_id, approval, rendered),
                    descriptor=record.url_descriptor,
                    record=record,
                    paths=paths,
                    registry=registry,
                    processor=services.processor_factory(),
                    prerequisite_digest=services.prerequisite_digest,
                    records=records,
                    checkpoint=store.save,
                    transport=transport,
                    now=timestamp,
                    event_sink=emit,
                    event_commit=result_writer.commit,
                    prepare_continuation=prepare,
                )
                if continuation is None or continuation.restore_response(
                    store.load(record.source_id)
                )["data"]["snapshot"] != _snapshot_result_data(result):
                    raise ValueError(
                        "snapshot result differs from its durable continuation"
                    )
                return _finish_snapshot_continuation(
                    paths,
                    store,
                    result_store,
                    continuation,
                    store.load_all(),
                    writer=result_writer,
                )

        return _run_with_source_lock(paths, operation)
    except (WebCaptureError, agent_handoff.AgentRegistrationError) as error:
        return CommandResult(
            command, False, {}, errors=(Diagnostic(error.code, str(error)),)
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        return _operation_failure(command, error)


def _finish_snapshot_continuation(
    paths,
    store,
    result_store,
    continuation,
    records,
    *,
    writer=None,
):
    """Finish only the exact proven pre-checkpoint result; never recapture."""
    candidate = records.get(continuation.source_id)
    checkpoint = _ledger_checkpoint_sha256(records)
    if (
        candidate is None
        or _ledger_record_sha256(candidate) != continuation.candidate_sha256
        or checkpoint != continuation.checkpoint_sha256
    ):
        raise WebCaptureError(
            "the snapshot was interrupted before its planned checkpoint became durable; recovery evidence has been preserved",
            code="snapshot_interrupted",
        )
    response = continuation.restore_response(candidate)
    version = candidate.versions[candidate.active_content_sha256]

    def prove_raw(raw):
        if raw.snapshot.sha256 != version.sha256:
            raise ValueError("snapshot recovery raw bytes changed")
        if candidate.active_derivation_id is not None:
            derivation = candidate.derivations[candidate.active_derivation_id]
            use_stable_file(
                paths,
                SnapshotNamespace.EXTRACTED,
                derivation.output_path,
                lambda output: _validate_pinned_process_artifact(derivation, output),
                include_sha256=True,
            )

    use_stable_file(
        paths,
        SnapshotNamespace.RAW_WEB,
        version.raw_path,
        prove_raw,
        include_sha256=True,
        expected_fingerprint=version.fingerprint,
    )
    if not store.is_canonical_record_current(candidate):
        raise WebCaptureError(
            "snapshot recovery checkpoint changed", code="snapshot_interrupted"
        )
    corpus = continuation.corpus_revision
    if compute_corpus_revision(records.values()) != corpus:
        raise ValueError("snapshot continuation corpus does not match its checkpoint")
    pending = result_store.load_pending()
    if pending is not None:
        reference = pending.reference
        if (
            not isinstance(pending, PendingSnapshotResult)
            or pending.generated_at != continuation.generated_at
            or pending.continuation_sha256 != continuation.sha256
            or reference.corpus_revision != corpus
        ):
            raise ValueError("pending result does not match snapshot continuation")
    else:

        def publish(result_writer):
            journal = result_writer.journal_sha256()
            if result_writer.snapshot_journal_digests(continuation.event) != (
                continuation.journal_sha256,
                continuation.committed_journal_sha256,
            ):
                raise ValueError("snapshot continuation journal changed")
            staged = result_store.load_staged()
            if staged is not None and (
                not isinstance(staged, StagedSnapshotResult)
                or staged.generated_at != continuation.generated_at
                or staged.checkpoint_sha256 != checkpoint
                or staged.corpus_revision != corpus
                or staged.journal_sha256 != journal
                or staged.continuation_sha256 != continuation.sha256
            ):
                raise ValueError("staged result does not match snapshot continuation")
            if (
                continuation.event is not None
                and journal == continuation.journal_sha256
            ):
                result_writer.commit(continuation.event.kind, continuation.event.data)
            result_store.save_staged(
                StagedSnapshotResult(
                    continuation.generated_at,
                    corpus,
                    checkpoint,
                    result_writer.journal_sha256(),
                    continuation.sha256,
                )
            )
            return result_writer.finalize(
                corpus,
                event_filter=lambda event: _event_checkpoint_is_current(event, records),
            )

        if writer is None:
            with result_store.writer(
                "source snapshot-url", continuation.generated_at, recoverable=True
            ) as result_writer:
                reference = publish(result_writer)
        else:
            reference = publish(writer)
        pending = PendingSnapshotResult(
            continuation.generated_at,
            reference,
            continuation.sha256,
        )
        result_store.save_pending(pending)
    result_store.complete_inflight(reference, checkpoint_sha256=checkpoint)
    store.write_summary(records.values(), generated_at=continuation.generated_at)
    return CommandResult(
        "source snapshot-url",
        True,
        {**response["data"], "result_manifest": reference.to_dict()},
        warnings=candidate.diagnostics,
        acknowledgement=reference,
    )


def _snapshot_result_data(value):
    """Serialize the existing immutable contracts into the canonical envelope."""
    return snapshot_result_data(value)


def unavailable(command: str, milestone: int) -> CommandResult:
    return CommandResult(
        command=command,
        ok=False,
        data={"available_after_milestone": milestone},
        errors=(
            Diagnostic(
                "command_not_available",
                f"{command} is not available until milestone {milestone}.",
            ),
        ),
    )


def _run_with_source_lock(paths: RepoPaths, operation: Callable[[], _T]) -> _T:
    lock = SourceWriteLock.acquire(paths.lock)
    try:
        result = operation()
    except BaseException as error:
        cleanup_message = _source_lock_cleanup_message(lock)
        if cleanup_message is not None:
            if isinstance(error, Exception):
                raise _SourceLockBodyFailure(error, cleanup_message) from error
            error.add_note(cleanup_message)
        raise
    cleanup_message = _source_lock_cleanup_message(lock)
    if cleanup_message is not None:
        raise _SourceLockCleanupFailure(cleanup_message, result)
    return result


def _source_lock_cleanup_message(lock: object) -> str | None:
    try:
        released = lock.release()  # type: ignore[attr-defined]
    except Exception as error:
        return (
            "Source write lock cleanup failed: "
            f"{type(error).__name__}: {safe_exception_text(error)}"
        )
    if not released:
        return "Source write lock cleanup could not prove release."
    return None


def _operation_failure(command: str, error: BaseException) -> CommandResult:
    if isinstance(error, _SourceLockBodyFailure):
        diagnostics = (
            Diagnostic("source_operation_failed", str(error.body_error)),
            Diagnostic("source_lock_cleanup_failed", error.cleanup_message),
        )
    elif isinstance(error, _SourceLockCleanupFailure):
        if isinstance(error.result, CommandResult):
            return _append_source_lock_cleanup_failure(
                error.result,
                code="source_lock_cleanup_failed",
                message=str(error),
            )
        diagnostics = (Diagnostic("source_lock_cleanup_failed", str(error)),)
    else:
        diagnostics = (Diagnostic("source_operation_failed", str(error)),)
    return CommandResult(
        command,
        ok=False,
        data={},
        errors=diagnostics,
    )


def _append_source_lock_cleanup_failure(
    result: CommandResult,
    *,
    code: str,
    message: str,
) -> CommandResult:
    return CommandResult(
        command=result.command,
        ok=False,
        data=result.data,
        warnings=result.warnings,
        errors=(*result.errors, Diagnostic(code, message)),
    )


def _validate_prerequisite_map(
    registry: ExtractorRegistry, digests: dict[str, str]
) -> None:
    expected = {extractor.extractor_id for extractor in registry.extractors}
    if set(digests) != expected:
        raise ValueError("prerequisite digests must exactly match registry extractors")
    for extractor_id, digest in digests.items():
        if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(
                f"prerequisite digest for {extractor_id} must be 64 lower-case hexadecimal characters"
            )


def _status_diagnostic(source_id: str, diagnostic: Diagnostic) -> dict[str, JSONValue]:
    return {
        "source_id": source_id,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "path": None if diagnostic.path is None else diagnostic.path.as_posix(),
        "details": dict(diagnostic.details),
    }


def _status_issue(issue: ValidationIssue) -> dict[str, JSONValue]:
    source_id = issue.details.get("source_id")
    return {
        "source_id": source_id if type(source_id) is str else None,
        "code": issue.code,
        "message": issue.message,
        "path": None if issue.path is None else issue.path.as_posix(),
        "details": dict(issue.details),
    }


def _status_detail_key(item: dict[str, JSONValue]) -> tuple[str, ...]:
    return (
        "" if item["path"] is None else str(item["path"]),
        "" if item["source_id"] is None else str(item["source_id"]),
        str(item["code"]),
        str(item["message"]),
        json.dumps(item["details"], ensure_ascii=False, sort_keys=True),
    )
