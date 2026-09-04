from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, TextIO

from .diagnostics import Diagnostic, JSONValue
from .inventory import InventoryItem
from .sync import SyncAction, SyncDecision, SyncReport
from .sync_results import SyncResultReference


@dataclass(frozen=True)
class CommandResult:
    command: str
    ok: bool
    data: Mapping[str, JSONValue]
    warnings: tuple[Diagnostic, ...] = ()
    errors: tuple[Diagnostic, ...] = ()
    acknowledgement: SyncResultReference | None = None


def render_json(result: CommandResult, stream: TextIO) -> None:
    stream.write(
        json.dumps(
            result_to_dict(result),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def render_human(result: CommandResult, *, stdout: TextIO, stderr: TextIO) -> None:
    stdout.write(f"{result.command}: {'ok' if result.ok else 'failed'}\n")
    if result.data:
        stdout.write(
            json.dumps(
                dict(result.data),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    for diagnostic in (*result.warnings, *result.errors):
        location = "" if diagnostic.path is None else f" ({diagnostic.path.as_posix()})"
        stderr.write(f"{diagnostic.code}{location}: {diagnostic.message}\n")


def result_to_dict(result: CommandResult) -> dict[str, JSONValue]:
    return {
        "command": result.command,
        "ok": result.ok,
        "data": dict(result.data),
        "warnings": [_diagnostic_to_dict(item) for item in result.warnings],
        "errors": [_diagnostic_to_dict(item) for item in result.errors],
    }


def sync_report_data(report: SyncReport) -> dict[str, JSONValue]:
    """Encode bounded SyncReport samples, exact counts, and its durable reference."""

    return {
        "corpus_revision": report.corpus_revision,
        "decision_counts": {
            action.value: report.decision_counts.get(action, 0) for action in SyncAction
        },
        "sampled_decisions": [
            _sync_decision_to_dict(item)
            for item in sorted(report.sampled_decisions, key=_sync_decision_key)
        ],
        "hashed_paths": [
            path.as_posix()
            for path in sorted(report.hashed_paths, key=lambda path: path.as_posix())
        ],
        "hashed_path_count": report.hashed_path_count,
        "new_active_representations": [
            {
                "source_id": item.source_id,
                "content_sha256": item.content_sha256,
                "derivation_id": item.derivation_id,
                "raw_path": item.raw_path.as_posix(),
                "extracted_path": item.extracted_path.as_posix(),
                "output_sha256": item.output_sha256,
                "quality_state": item.quality_state,
                "anchors": [
                    {"kind": anchor.kind, "value": anchor.value}
                    for anchor in item.anchors
                ],
            }
            for item in sorted(
                report.new_active_representations,
                key=lambda item: (
                    item.source_id,
                    item.content_sha256,
                    item.derivation_id,
                ),
            )
        ],
        "new_active_representation_count": (report.new_active_representation_count),
        "citation_rewrites": [
            item.to_dict()
            for item in sorted(
                report.citation_rewrites,
                key=lambda item: (
                    item.source_id,
                    item.content_sha256,
                    item.raw_path.as_posix(),
                ),
            )
        ],
        "citation_rewrite_count": report.citation_rewrite_count,
        "handoff_source_ids": sorted(report.handoff_source_ids),
        "handoff_source_id_count": report.handoff_source_id_count,
        "coverage_gaps": [
            _diagnostic_to_dict(item)
            for item in sorted(report.coverage_gaps, key=_diagnostic_key)
        ],
        "coverage_gap_count": report.coverage_gap_count,
        "sample_limits": {
            "max_items_per_field": 100,
            "max_bytes_per_field": 32 * 1024,
        },
        "result_manifest": (
            None if report.result_manifest is None else report.result_manifest.to_dict()
        ),
    }


def _sync_decision_to_dict(decision: SyncDecision) -> dict[str, JSONValue]:
    return {
        "source_id": decision.source_id,
        "action": decision.action.value,
        "reason": decision.reason,
        "item": (
            None if decision.item is None else _inventory_item_to_dict(decision.item)
        ),
    }


def _sync_decision_key(decision: SyncDecision) -> tuple[str, ...]:
    return (
        "" if decision.item is None else decision.item.fingerprint.path.as_posix(),
        "" if decision.source_id is None else decision.source_id,
        decision.action.value,
        decision.reason,
    )


def _inventory_item_to_dict(item: InventoryItem) -> dict[str, JSONValue]:
    descriptor = item.url_descriptor
    return {
        "fingerprint": {
            "path": item.fingerprint.path.as_posix(),
            "byte_size": item.fingerprint.byte_size,
            "mtime_ns": item.fingerprint.mtime_ns,
        },
        "media_type": item.media_type,
        "extension": item.extension,
        "sha256": item.sha256,
        "url_descriptor": (
            None
            if descriptor is None
            else {
                "path": descriptor.path.as_posix(),
                "url": descriptor.url,
                "description": descriptor.description,
                "added": descriptor.added.isoformat(),
            }
        ),
    }


def _diagnostic_to_dict(diagnostic: Diagnostic) -> dict[str, JSONValue]:
    path: str | None = None
    if diagnostic.path is not None:
        path = diagnostic.path.as_posix()
    return {
        "code": diagnostic.code,
        "message": diagnostic.message,
        "path": path,
        "details": dict(diagnostic.details),
    }


def _diagnostic_key(diagnostic: Diagnostic) -> tuple[str, ...]:
    return (
        "" if diagnostic.path is None else diagnostic.path.as_posix(),
        diagnostic.code,
        diagnostic.message,
        json.dumps(
            dict(diagnostic.details),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
