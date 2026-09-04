from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from types import MappingProxyType

from .contracts import SourceRecord
from .diagnostics import JSONValue
from .layout import RepoPaths
from .ledger import (
    PublishedWriteError,
    UnsafeFilesystemError,
    _PinnedDirectory,
    _fsync_directory,
    _read_regular_at,
    _same_file_observation,
    _canonical_record_payload,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESULT_ID_RE = re.compile(r"^sync_[0-9a-f]{64}$")
_EVENT_KINDS = frozenset(
    {
        "hashed_path",
        "new_active_representation",
        "citation_rewrite",
        "handoff_source_id",
        "coverage_gap",
    }
)
_MAX_EVENT_LINE_BYTES = 2 * 1024 * 1024
_MAX_PENDING_BYTES = 256 * 1024
# Handoff responses have their own immutable artifact, bounded like the
# underlying handoff manifest; generic init/sync receipt limits stay unchanged.
_MAX_HANDOFF_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_SNAPSHOT_RECEIPT_BYTES = 256 * 1024
_INFLIGHT_NAME = "inflight.jsonl"
_STAGED_NAME = "staged.json"
_ACKNOWLEDGED_NAME = "acknowledged.json"
_SNAPSHOT_CONTINUATION_NAME = "snapshot-continuation.json"
_SNAPSHOT_COMMAND = "source snapshot-url"
_RESULT_COMMANDS = frozenset({"init", "sync", _SNAPSHOT_COMMAND})
_COUNT_FIELD_BY_EVENT = {
    "hashed_path": "hashed_path_count",
    "new_active_representation": "new_active_representation_count",
    "citation_rewrite": "citation_rewrite_count",
    "handoff_source_id": "handoff_source_id_count",
    "coverage_gap": "coverage_gap_count",
}


@dataclass(frozen=True)
class SyncEvent:
    kind: str
    data: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError("unknown sync-result event kind")
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class SyncResultReference:
    result_id: str
    path: PurePosixPath
    sha256: str
    corpus_revision: str
    event_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        if _RESULT_ID_RE.fullmatch(self.result_id) is None:
            raise ValueError("sync result_id must be content-addressed")
        if _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("sync result sha256 must be canonical")
        if self.result_id != f"sync_{self.sha256}":
            raise ValueError("sync result_id must match manifest sha256")
        expected_path = PurePosixPath(
            ".brain", "sync-results", f"{self.result_id}.jsonl"
        )
        if self.path != expected_path:
            raise ValueError("sync result path must be canonical")
        if _SHA256_RE.fullmatch(self.corpus_revision) is None:
            raise ValueError("sync result corpus_revision must be canonical")
        counts = dict(self.event_counts)
        if set(counts) != _EVENT_KINDS or any(
            type(value) is not int or value < 0 for value in counts.values()
        ):
            raise ValueError("sync result event_counts must cover every event kind")
        object.__setattr__(self, "event_counts", MappingProxyType(counts))

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "result_id": self.result_id,
            "path": self.path.as_posix(),
            "sha256": self.sha256,
            "corpus_revision": self.corpus_revision,
            "event_counts": dict(sorted(self.event_counts.items())),
        }

    @classmethod
    def from_dict(cls, value: object) -> SyncResultReference:
        if not isinstance(value, dict) or set(value) != {
            "result_id",
            "path",
            "sha256",
            "corpus_revision",
            "event_counts",
        }:
            raise ValueError("sync result reference has invalid fields")
        counts = value["event_counts"]
        if not isinstance(counts, dict):
            raise ValueError("sync result event_counts must be an object")
        return cls(
            value["result_id"] if isinstance(value["result_id"], str) else "",
            PurePosixPath(value["path"] if isinstance(value["path"], str) else ""),
            value["sha256"] if isinstance(value["sha256"], str) else "",
            (
                value["corpus_revision"]
                if isinstance(value["corpus_revision"], str)
                else ""
            ),
            counts,
        )


@dataclass(frozen=True)
class PendingSyncResult:
    command: str
    generated_at: datetime
    reference: SyncResultReference
    result_data: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if self.command not in {"init", "sync"}:
            raise ValueError("pending sync command is invalid")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("pending sync timestamp must be aware")
        serialized = _canonical_json(dict(self.result_data))
        if len(serialized) > _MAX_PENDING_BYTES:
            raise ValueError("pending sync result exceeds its byte limit")
        data = _load_json_object(serialized, "pending sync result data")
        if data.get("corpus_revision") != self.reference.corpus_revision:
            raise ValueError("pending result does not match its manifest corpus")
        if data.get("result_manifest") != self.reference.to_dict():
            raise ValueError("pending result does not match its manifest reference")
        for event_kind, field_name in _COUNT_FIELD_BY_EVENT.items():
            if data.get(field_name) != self.reference.event_counts[event_kind]:
                raise ValueError("pending result counts do not match its manifest")
        expected_status = (
            "complete_with_gaps"
            if self.reference.event_counts["coverage_gap"]
            else "complete"
        )
        if data.get("status") != expected_status:
            raise ValueError("pending result status does not match its manifest")
        object.__setattr__(self, "result_data", MappingProxyType(data))


@dataclass(frozen=True)
class AcknowledgedSyncResult:
    command: str
    reference: SyncResultReference

    def __post_init__(self) -> None:
        if self.command not in {"init", "sync"}:
            raise ValueError("acknowledged sync command is invalid")


@dataclass(frozen=True)
class StagedSyncResult:
    command: str
    generated_at: datetime
    corpus_revision: str
    checkpoint_sha256: str
    journal_sha256: str
    result_data: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if self.command not in {"init", "sync"}:
            raise ValueError("staged sync command is invalid")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("staged sync timestamp must be aware")
        if _SHA256_RE.fullmatch(self.corpus_revision) is None:
            raise ValueError("staged sync corpus revision must be canonical")
        if _SHA256_RE.fullmatch(self.checkpoint_sha256) is None:
            raise ValueError("staged sync checkpoint digest must be canonical")
        if _SHA256_RE.fullmatch(self.journal_sha256) is None:
            raise ValueError("staged sync journal digest must be canonical")
        serialized = _canonical_json(dict(self.result_data))
        if len(serialized) > _MAX_PENDING_BYTES:
            raise ValueError("staged sync result exceeds its byte limit")
        data = _load_json_object(serialized, "staged sync result data")
        if data.get("corpus_revision") != self.corpus_revision:
            raise ValueError("staged result does not match its corpus")
        if data.get("result_manifest") is not None:
            raise ValueError("staged result must not have a manifest reference")
        for field_name in _COUNT_FIELD_BY_EVENT.values():
            value = data.get(field_name)
            if type(value) is not int or value < 0:
                raise ValueError("staged result event count is invalid")
        expected_status = (
            "complete_with_gaps" if data["coverage_gap_count"] else "complete"
        )
        if data.get("status") != expected_status:
            raise ValueError("staged result status does not match its event counts")
        object.__setattr__(self, "result_data", MappingProxyType(data))


@dataclass(frozen=True)
class _ManifestMetadata:
    command: str
    generated_at: datetime


def _snapshot_payload(
    data: Mapping[str, JSONValue], *, compact: bool = False
) -> dict[str, JSONValue]:
    snapshot = data.get("snapshot")
    expected = {
        "source_id",
        "raw_path",
        "content_sha256",
        "source_version",
        "retrieval",
        "extraction_result",
        "active_representation",
        "corpus_revision",
    }
    if compact:
        expected.remove("source_version")
    if not isinstance(snapshot, dict) or set(snapshot) != expected:
        raise ValueError("snapshot result fields are invalid")
    revision = snapshot["corpus_revision"]
    if not isinstance(revision, str) or _SHA256_RE.fullmatch(revision) is None:
        raise ValueError("snapshot corpus revision is invalid")
    return snapshot


@dataclass(frozen=True)
class SnapshotContinuation:
    """History-independent receipt bound to the canonical ledger authority."""

    source_id: str
    candidate_sha256: str
    checkpoint_sha256: str
    corpus_revision: str
    request_identity: Mapping[str, JSONValue]
    generated_at: datetime
    result_recipe: Mapping[str, JSONValue]
    warnings: tuple[Mapping[str, JSONValue], ...]
    response_sha256: str
    journal_sha256: str
    committed_journal_sha256: str
    event: SyncEvent | None

    def __post_init__(self) -> None:
        from .sources.web import validate_snapshot_request_identity

        identity = _load_json_object(
            _canonical_json(self.request_identity), "snapshot identity"
        )
        validate_snapshot_request_identity(identity)
        if (
            not isinstance(self.source_id, str)
            or re.fullmatch(r"src_[0-9a-f]{64}", self.source_id) is None
        ):
            raise ValueError("snapshot continuation source ID is invalid")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("snapshot continuation timestamp must be aware")
        for digest in (
            self.candidate_sha256,
            self.checkpoint_sha256,
            self.corpus_revision,
            self.response_sha256,
            self.journal_sha256,
            self.committed_journal_sha256,
        ):
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ValueError("snapshot continuation digest is invalid")
        data = _load_json_object(_canonical_json(self.result_recipe), "snapshot recipe")
        if "result_manifest" in data:
            raise ValueError("snapshot continuation must not have a manifest")
        snapshot = _snapshot_payload(data, compact=True)
        if (
            snapshot["source_id"] != self.source_id
            or snapshot["corpus_revision"] != self.corpus_revision
        ):
            raise ValueError("snapshot recipe identity does not match continuation")
        warnings = _load_json_object(
            _canonical_json({"warnings": list(self.warnings)}), "snapshot warnings"
        )["warnings"]
        if not isinstance(warnings, list) or any(
            not isinstance(item, dict) for item in warnings
        ):
            raise ValueError("snapshot continuation warnings are invalid")
        if self.event is not None:
            representation = snapshot["active_representation"]
            if (
                not isinstance(representation, dict)
                or self.event.kind != "new_active_representation"
                or self.event.data
                != {
                    **representation,
                    "record_sha256": self.candidate_sha256,
                }
            ):
                raise ValueError("snapshot continuation activation event is invalid")
        elif self.journal_sha256 != self.committed_journal_sha256:
            raise ValueError(
                "snapshot continuation without event cannot append a commit"
            )
        object.__setattr__(self, "request_identity", MappingProxyType(identity))
        object.__setattr__(self, "result_recipe", MappingProxyType(data))
        object.__setattr__(self, "warnings", tuple(warnings))

    @classmethod
    def prepare(
        cls,
        *,
        request_identity,
        generated_at,
        candidate,
        checkpoint_sha256,
        result_data,
        journal_sha256,
        committed_journal_sha256,
        event,
    ):
        from .sources.web import snapshot_result_data

        data = _load_json_object(
            _canonical_json(result_data), "planned snapshot result"
        )
        _snapshot_payload(data)
        warnings = snapshot_result_data(candidate.diagnostics)
        response = _snapshot_response(data, warnings)
        response_sha256 = hashlib.sha256(_canonical_json(response)).hexdigest()
        del data["snapshot"]["source_version"]
        continuation = cls(
            candidate.source_id,
            hashlib.sha256(_canonical_record_payload(candidate)).hexdigest(),
            checkpoint_sha256,
            data["snapshot"]["corpus_revision"],
            request_identity,
            generated_at,
            data,
            tuple(warnings),
            response_sha256,
            journal_sha256,
            committed_journal_sha256,
            event,
        )
        continuation.restore_response(candidate)
        return continuation

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_snapshot_continuation_payload(self)).hexdigest()

    def restore_response(self, candidate: SourceRecord) -> dict[str, JSONValue]:
        from .sources.web import (
            _activation_event,
            active_representation_for,
            canonicalize_url,
            snapshot_result_data,
        )

        if (
            candidate.source_id != self.source_id
            or hashlib.sha256(_canonical_record_payload(candidate)).hexdigest()
            != self.candidate_sha256
        ):
            raise ValueError(
                "snapshot continuation does not match canonical candidate digest"
            )
        data = _load_json_object(_canonical_json(self.result_recipe), "snapshot recipe")
        recipe = _snapshot_payload(data, compact=True)
        version = candidate.versions.get(candidate.active_content_sha256)
        if version is None or not version.retrieval_events:
            raise ValueError("snapshot continuation requires retained captured bytes")
        # Retain the public SnapshotResult field order as well as its full content.
        snapshot = {
            key: (
                snapshot_result_data(version)
                if key == "source_version"
                else recipe[key]
            )
            for key in (
                "source_id",
                "raw_path",
                "content_sha256",
                "source_version",
                "retrieval",
                "extraction_result",
                "active_representation",
                "corpus_revision",
            )
        }
        data["snapshot"] = snapshot
        expected = {
            "source_id": candidate.source_id,
            "raw_path": version.raw_path.as_posix(),
            "content_sha256": version.sha256,
            "source_version": snapshot_result_data(version),
            "retrieval": snapshot_result_data(version.retrieval_events[-1]),
            "active_representation": snapshot_result_data(
                active_representation_for(candidate)
            ),
        }
        if any(snapshot[key] != value for key, value in expected.items()):
            raise ValueError("snapshot continuation does not match candidate")
        selector = self.request_identity["selector"]
        approval = self.request_identity["approval"]
        retrieval = version.retrieval_events[-1]
        descriptor = candidate.url_descriptor
        if (
            descriptor is None
            or selector["url"] != canonicalize_url(descriptor.url)
            or (
                selector["kind"] == "source_id"
                and selector["source_id"] != candidate.source_id
            )
            or (
                selector["kind"] == "url"
                and selector["description"] != descriptor.description
            )
            or approval
            != {
                "event_id": retrieval.approval_event_id,
                "scope": retrieval.approval_scope,
                "note": retrieval.approval_note,
            }
        ):
            raise ValueError("snapshot continuation identity does not match candidate")
        processing = snapshot["extraction_result"]
        if processing is not None:
            if (
                not isinstance(processing, dict)
                or set(processing) != {"state", "derivation", "attempt", "diagnostics"}
                or processing["state"] != candidate.state.value
                or processing["attempt"] != snapshot_result_data(candidate.last_attempt)
                or processing["derivation"]
                != snapshot_result_data(
                    candidate.derivations.get(candidate.active_derivation_id)
                )
                or not isinstance(processing["diagnostics"], list)
                or any(
                    d not in snapshot_result_data(candidate.diagnostics)
                    for d in processing["diagnostics"]
                )
            ):
                raise ValueError(
                    "snapshot continuation process result does not match candidate"
                )
        if self.event is not None:
            if (
                self.event.kind != "new_active_representation"
                or self.event.data != _activation_event(candidate)
            ):
                raise ValueError("snapshot continuation activation event is invalid")
        elif processing is not None and processing["derivation"] is not None:
            raise ValueError("snapshot continuation is missing its activation event")
        if list(self.warnings) != snapshot_result_data(candidate.diagnostics):
            raise ValueError("snapshot continuation warnings do not match candidate")
        response = _snapshot_response(data, list(self.warnings))
        if (
            hashlib.sha256(_canonical_json(response)).hexdigest()
            != self.response_sha256
        ):
            raise ValueError(
                "snapshot continuation response digest does not match reconstructed response"
            )
        return response


def _snapshot_response(data, warnings):
    return {
        "command": _SNAPSHOT_COMMAND,
        "ok": True,
        "data": data,
        "warnings": warnings,
        "errors": [],
    }


@dataclass(frozen=True)
class StagedSnapshotResult:
    generated_at: datetime
    corpus_revision: str
    checkpoint_sha256: str
    journal_sha256: str
    continuation_sha256: str

    @property
    def command(self) -> str:
        return _SNAPSHOT_COMMAND

    def __post_init__(self):
        _snapshot_timestamp(self.generated_at)
        for digest in (
            self.corpus_revision,
            self.checkpoint_sha256,
            self.journal_sha256,
            self.continuation_sha256,
        ):
            _snapshot_digest(digest)


@dataclass(frozen=True)
class PendingSnapshotResult:
    generated_at: datetime
    reference: SyncResultReference
    continuation_sha256: str

    @property
    def command(self) -> str:
        return _SNAPSHOT_COMMAND

    def __post_init__(self):
        _snapshot_timestamp(self.generated_at)
        _snapshot_digest(self.continuation_sha256)
        if not isinstance(self.reference, SyncResultReference):
            raise ValueError("snapshot pending reference is invalid")


@dataclass(frozen=True)
class AcknowledgedSnapshotResult:
    reference: SyncResultReference
    continuation_sha256: str

    @property
    def command(self) -> str:
        return _SNAPSHOT_COMMAND

    def __post_init__(self):
        _snapshot_digest(self.continuation_sha256)
        if not isinstance(self.reference, SyncResultReference):
            raise ValueError("snapshot acknowledgement reference is invalid")


def _snapshot_digest(value):
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("snapshot receipt digest is invalid")


def _snapshot_timestamp(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("snapshot receipt timestamp must be aware")


def _bounded_snapshot_payload(value):
    payload = _canonical_json(value)
    if len(payload) > _MAX_SNAPSHOT_RECEIPT_BYTES:
        raise ValueError("compact snapshot receipt exceeds its byte limit")
    return payload


def _snapshot_continuation_payload(value):
    return _bounded_snapshot_payload(
        {
            "schema_version": 2,
            "kind": "source_snapshot_url",
            "source_id": value.source_id,
            "candidate_sha256": value.candidate_sha256,
            "checkpoint_sha256": value.checkpoint_sha256,
            "corpus_revision": value.corpus_revision,
            "request_identity": dict(value.request_identity),
            "generated_at": value.generated_at.isoformat(),
            "result_recipe": dict(value.result_recipe),
            "warnings": list(value.warnings),
            "response_sha256": value.response_sha256,
            "journal_sha256": value.journal_sha256,
            "committed_journal_sha256": value.committed_journal_sha256,
            "event": None
            if value.event is None
            else {"kind": value.event.kind, "data": dict(value.event.data)},
        }
    )


def _snapshot_receipt_payload(receipt):
    value = {
        "schema_version": 2,
        "command": _SNAPSHOT_COMMAND,
        "continuation_sha256": receipt.continuation_sha256,
    }
    if isinstance(receipt, StagedSnapshotResult):
        value.update(
            kind="snapshot_staged",
            generated_at=receipt.generated_at.isoformat(),
            corpus_revision=receipt.corpus_revision,
            checkpoint_sha256=receipt.checkpoint_sha256,
            journal_sha256=receipt.journal_sha256,
        )
    elif isinstance(receipt, PendingSnapshotResult):
        value.update(
            kind="snapshot_pending",
            generated_at=receipt.generated_at.isoformat(),
            reference=receipt.reference.to_dict(),
        )
    elif isinstance(receipt, AcknowledgedSnapshotResult):
        value.update(
            kind="snapshot_acknowledged", reference=receipt.reference.to_dict()
        )
    else:
        raise ValueError("snapshot receipt type is invalid")
    return _bounded_snapshot_payload(value)


def _decode_snapshot_receipt(value, kind):
    expected = {"schema_version", "command", "kind", "continuation_sha256"}
    expected.update(
        {
            "snapshot_staged": {
                "generated_at",
                "corpus_revision",
                "checkpoint_sha256",
                "journal_sha256",
            },
            "snapshot_pending": {"generated_at", "reference"},
            "snapshot_acknowledged": {"reference"},
        }[kind]
    )
    if (
        set(value) != expected
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or value["command"] != _SNAPSHOT_COMMAND
        or value["kind"] != kind
    ):
        raise ValueError("compact snapshot receipt fields are invalid")
    if "generated_at" in value:
        if not isinstance(value["generated_at"], str):
            raise ValueError("snapshot receipt timestamp is invalid")
        generated_at = datetime.fromisoformat(value["generated_at"])
    if kind == "snapshot_staged":
        return StagedSnapshotResult(
            generated_at,
            value["corpus_revision"],
            value["checkpoint_sha256"],
            value["journal_sha256"],
            value["continuation_sha256"],
        )
    reference = SyncResultReference.from_dict(value["reference"])
    if kind == "snapshot_pending":
        return PendingSnapshotResult(
            generated_at, reference, value["continuation_sha256"]
        )
    return AcknowledgedSnapshotResult(reference, value["continuation_sha256"])


class SyncResultWriter:
    def __init__(
        self,
        store: SyncResultStore,
        command: str,
        generated_at: datetime,
        *,
        recoverable: bool = False,
    ) -> None:
        if command not in _RESULT_COMMANDS:
            raise ValueError("sync result command is invalid")
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("sync result timestamp must be aware")
        self._store = store
        self._directory = store._open_directory()
        self._recoverable = recoverable
        self._name = (
            _INFLIGHT_NAME
            if recoverable
            else f".brain-tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        self._event_digest = hashlib.sha256()
        self._event_counts: Counter[str] = Counter()
        self._committed_event_ids: set[tuple[str, ...]] = set()
        self._sequence = 0
        self._finished = False
        self._trailer_corpus_revision: str | None = None
        self._resumed = False
        try:
            self._descriptor = self._open_existing_inflight()
        except FileNotFoundError:
            self._descriptor = os.open(
                self._name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._directory.descriptor,
            )
            self.generated_at = generated_at
            self.command = command
            self._write_object(
                {
                    "type": "header",
                    "schema_version": 1,
                    "command": command,
                    "generated_at": generated_at.isoformat(),
                }
            )
            if recoverable:
                os.fsync(self._descriptor)
                _fsync_directory(self._directory.descriptor)
                self._directory.validate()
        else:
            self.command, self.generated_at = self._resume_state(command)
            self._resumed = True

    def emit(self, kind: str, data: Mapping[str, JSONValue]) -> None:
        if self._finished:
            raise ValueError("sync result writer is already finalized")
        if kind not in _EVENT_KINDS:
            raise ValueError("unknown sync-result event kind")
        canonical_data = _load_json_object(
            _canonical_json(dict(data)), "sync result event data"
        )
        if self._resumed:
            if self._existing_event(kind, canonical_data):
                return
            if self._trailer_corpus_revision is not None:
                raise ValueError("finalized sync result cannot accept a new event")
        self._sequence += 1
        line = _canonical_line(
            {
                "type": "event",
                "sequence": self._sequence,
                "kind": kind,
                "data": canonical_data,
            }
        )
        _validate_line_size(line)
        _write_all(self._descriptor, line)
        if self._recoverable:
            os.fsync(self._descriptor)
        self._event_digest.update(line)
        self._event_counts[kind] += 1

    def commit(self, kind: str, data: Mapping[str, JSONValue]) -> None:
        """Commit a shard-coupled event after its checkpoint is durable."""

        if self._finished:
            raise ValueError("sync result writer is already finalized")
        if not self._recoverable:
            raise ValueError("event commits require a recoverable sync result")
        if kind not in {"new_active_representation", "citation_rewrite"}:
            raise ValueError("sync result event kind is not shard-coupled")
        canonical_data = _load_json_object(
            _canonical_json(dict(data)), "sync result event data"
        )
        record_sha256 = canonical_data.get("record_sha256")
        if (
            not isinstance(record_sha256, str)
            or _SHA256_RE.fullmatch(record_sha256) is None
        ):
            raise ValueError("shard-coupled event must name its record checkpoint")
        identity = _event_identity(kind, canonical_data)
        if identity in self._committed_event_ids:
            return
        if not self._existing_event(kind, canonical_data):
            raise ValueError("sync result event must be prepared before commit")
        self._write_object(
            {
                "type": "commit",
                "event_kind": kind,
                "event_sha256": identity[1],
                "record_sha256": record_sha256,
            }
        )
        os.fsync(self._descriptor)
        self._committed_event_ids.add(identity)

    def journal_sha256(self) -> str:
        if self._finished or not self._recoverable:
            raise ValueError("recoverable sync result journal is not available")
        os.fsync(self._descriptor)
        before = os.fstat(self._descriptor)
        digest = _hash_descriptor(self._descriptor)
        after = os.fstat(self._descriptor)
        named = os.stat(
            _INFLIGHT_NAME,
            dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if not (
            _same_file_observation(before, after)
            and _same_file_observation(after, named)
        ):
            raise UnsafeFilesystemError(
                "in-flight sync result changed while it was hashed"
            )
        self._directory.validate()
        return digest

    def snapshot_journal_digests(self, event: SyncEvent | None) -> tuple[str, str]:
        """Bind a continuation to exactly the prepared or single-commit journal."""
        if self.command != _SNAPSHOT_COMMAND:
            raise ValueError("snapshot journal binding requires source snapshot-url")
        before = self.journal_sha256()
        if event is None:
            return before, before
        if event.kind != "new_active_representation" or not self._existing_event(
            event.kind, event.data
        ):
            raise ValueError("snapshot activation event is not prepared")
        identity = _event_identity(event.kind, event.data)
        suffix = _canonical_line(
            {
                "type": "commit",
                "event_kind": event.kind,
                "event_sha256": identity[1],
                "record_sha256": event.data["record_sha256"],
            }
        )
        if identity in self._committed_event_ids:
            size = os.fstat(self._descriptor).st_size - len(suffix)
            prepared = _hash_descriptor(self._descriptor, limit=size)
            if (
                os.read(self._descriptor, len(suffix)) != suffix
                or self.journal_sha256() != before
            ):
                raise ValueError(
                    "snapshot journal does not end in its exact activation commit"
                )
            return prepared, before
        after = _hash_descriptor(self._descriptor, suffix=suffix)
        if self.journal_sha256() != before:
            raise ValueError("snapshot journal changed during continuation preparation")
        return before, after

    def finalize(
        self,
        corpus_revision: str,
        *,
        event_filter: Callable[[SyncEvent], bool] | None = None,
    ) -> SyncResultReference:
        if self._finished:
            raise ValueError("sync result writer is already finalized")
        if _SHA256_RE.fullmatch(corpus_revision) is None:
            raise ValueError("sync result corpus_revision must be canonical")
        if self._recoverable:
            return self._finalize_recoverable(corpus_revision, event_filter)
        if event_filter is not None:
            raise ValueError("event filters require a recoverable sync result")
        counts = {kind: self._event_counts[kind] for kind in sorted(_EVENT_KINDS)}
        if self._trailer_corpus_revision is None:
            self._write_object(
                {
                    "type": "trailer",
                    "event_count": self._sequence,
                    "event_counts": counts,
                    "events_sha256": self._event_digest.hexdigest(),
                    "corpus_revision": corpus_revision,
                }
            )
            self._trailer_corpus_revision = corpus_revision
        elif self._trailer_corpus_revision != corpus_revision:
            raise ValueError("in-flight sync result corpus revision changed")
        os.fsync(self._descriptor)
        manifest_sha256 = _hash_descriptor(self._descriptor)
        result_id = f"sync_{manifest_sha256}"
        final_name = f"{result_id}.jsonl"
        published = False
        try:
            try:
                os.link(
                    self._name,
                    final_name,
                    src_dir_fd=self._directory.descriptor,
                    dst_dir_fd=self._directory.descriptor,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                pass
            if not self._recoverable:
                os.unlink(self._name, dir_fd=self._directory.descriptor)
                self._name = ""
            _fsync_directory(self._directory.descriptor)
            self._directory.validate()
        except BaseException as error:
            if published:
                raise PublishedWriteError(
                    "sync result was published but directory durability was not proven"
                ) from error
            raise
        reference = SyncResultReference(
            result_id,
            PurePosixPath(".brain", "sync-results", final_name),
            manifest_sha256,
            corpus_revision,
            counts,
        )
        self._finished = True
        os.close(self._descriptor)
        self._descriptor = -1
        self._directory.close()
        self._store.verify(reference)
        return reference

    def _finalize_recoverable(
        self,
        corpus_revision: str,
        event_filter: Callable[[SyncEvent], bool] | None,
    ) -> SyncResultReference:
        temporary_name = f".brain-tmp-{os.getpid()}-{uuid.uuid4().hex}"
        temporary_descriptor = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._directory.descriptor,
        )
        published = False
        temporary_exists = True
        try:
            _write_all(
                temporary_descriptor,
                _canonical_line(
                    {
                        "type": "header",
                        "schema_version": 1,
                        "command": self.command,
                        "generated_at": self.generated_at.isoformat(),
                    }
                ),
            )
            before = os.fstat(self._descriptor)
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            event_digest = hashlib.sha256()
            event_counts: Counter[str] = Counter()
            sequence = 0
            saw_header = False
            prepared_record_sha256: dict[tuple[str, ...], str | None] = {}
            for value, _line in _iter_objects(self._descriptor):
                object_kind = value.get("type")
                if not saw_header:
                    if object_kind != "header":
                        raise ValueError("in-flight sync result header is invalid")
                    saw_header = True
                    continue
                if object_kind == "commit":
                    if set(value) != {
                        "type",
                        "event_kind",
                        "event_sha256",
                        "record_sha256",
                    }:
                        raise ValueError("in-flight sync result commit is invalid")
                    commit_identity = (
                        str(value["event_kind"]),
                        str(value["event_sha256"]),
                    )
                    if (
                        commit_identity not in prepared_record_sha256
                        or value["record_sha256"]
                        != prepared_record_sha256[commit_identity]
                    ):
                        raise ValueError(
                            "in-flight sync result commit has no prepared event"
                        )
                    self._committed_event_ids.add(commit_identity)
                    continue
                if object_kind != "event" or not isinstance(value.get("data"), dict):
                    raise ValueError("in-flight sync result ordering is invalid")
                event = SyncEvent(str(value.get("kind")), value["data"])
                event_identity = _event_identity(event.kind, event.data)
                shard_coupled = event.kind in {
                    "new_active_representation",
                    "citation_rewrite",
                }
                if shard_coupled:
                    event_record_sha256 = event.data.get("record_sha256")
                    prepared_record_sha256[event_identity] = (
                        event_record_sha256
                        if isinstance(event_record_sha256, str)
                        else None
                    )
                final_record_observation = event.kind == "handoff_source_id" or (
                    event.kind == "coverage_gap"
                    and isinstance(event.data.get("source_id"), str)
                )
                if shard_coupled and event_identity in self._committed_event_ids:
                    pass
                elif (shard_coupled or final_record_observation) and (
                    event_filter is None or not event_filter(event)
                ):
                    continue
                sequence += 1
                public_data = _public_event_data(event)
                event_line = _canonical_line(
                    {
                        "type": "event",
                        "sequence": sequence,
                        "kind": event.kind,
                        "data": public_data,
                    }
                )
                _validate_line_size(event_line)
                _write_all(temporary_descriptor, event_line)
                event_digest.update(event_line)
                event_counts[event.kind] += 1
            if not saw_header:
                raise ValueError("in-flight sync result is incomplete")
            after = os.fstat(self._descriptor)
            named = os.stat(
                _INFLIGHT_NAME,
                dir_fd=self._directory.descriptor,
                follow_symlinks=False,
            )
            if not (
                _same_file_observation(before, after)
                and _same_file_observation(after, named)
            ):
                raise UnsafeFilesystemError(
                    "in-flight sync result changed during finalization"
                )
            counts = {kind: event_counts[kind] for kind in sorted(_EVENT_KINDS)}
            _write_all(
                temporary_descriptor,
                _canonical_line(
                    {
                        "type": "trailer",
                        "event_count": sequence,
                        "event_counts": counts,
                        "events_sha256": event_digest.hexdigest(),
                        "corpus_revision": corpus_revision,
                    }
                ),
            )
            os.fsync(temporary_descriptor)
            manifest_sha256 = _hash_descriptor(temporary_descriptor)
            result_id = f"sync_{manifest_sha256}"
            final_name = f"{result_id}.jsonl"
            try:
                os.link(
                    temporary_name,
                    final_name,
                    src_dir_fd=self._directory.descriptor,
                    dst_dir_fd=self._directory.descriptor,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                pass
            os.unlink(temporary_name, dir_fd=self._directory.descriptor)
            temporary_exists = False
            _fsync_directory(self._directory.descriptor)
            self._directory.validate()
        except BaseException as error:
            if published:
                raise PublishedWriteError(
                    "sync result was published but directory durability was not proven"
                ) from error
            raise
        finally:
            os.close(temporary_descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=self._directory.descriptor)
                except FileNotFoundError:
                    pass
        reference = SyncResultReference(
            result_id,
            PurePosixPath(".brain", "sync-results", final_name),
            manifest_sha256,
            corpus_revision,
            counts,
        )
        self._finished = True
        os.close(self._descriptor)
        self._descriptor = -1
        self._directory.close()
        self._store.verify(reference)
        return reference

    def _open_existing_inflight(self) -> int:
        if not self._recoverable:
            raise FileNotFoundError(self._name)
        named = os.stat(
            self._name,
            dir_fd=self._directory.descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(named.st_mode):
            raise ValueError("in-flight sync result must be a regular file")
        descriptor = os.open(
            self._name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=self._directory.descriptor,
        )
        opened = os.fstat(descriptor)
        if not _same_file_observation(named, opened):
            os.close(descriptor)
            raise UnsafeFilesystemError("in-flight sync result changed while opened")
        return descriptor

    def _resume_state(self, expected_command: str) -> tuple[str, datetime]:
        saw_header = False
        command = ""
        generated_at: datetime | None = None
        expected_sequence = 1
        event_record_sha256: dict[tuple[str, ...], str | None] = {}
        for value, line in _iter_recoverable_objects(self._descriptor):
            kind = value.get("type")
            if not saw_header:
                if (
                    kind != "header"
                    or set(value)
                    != {"type", "schema_version", "command", "generated_at"}
                    or value["schema_version"] != 1
                    or value["command"] not in _RESULT_COMMANDS
                ):
                    raise ValueError("in-flight sync result header is invalid")
                command = str(value["command"])
                generated_at = datetime.fromisoformat(str(value["generated_at"]))
                if generated_at.tzinfo is None or generated_at.utcoffset() is None:
                    raise ValueError("in-flight sync timestamp must be aware")
                saw_header = True
                continue
            if kind == "event":
                if (
                    set(value) != {"type", "sequence", "kind", "data"}
                    or value["sequence"] != expected_sequence
                    or value["kind"] not in _EVENT_KINDS
                    or not isinstance(value["data"], dict)
                ):
                    raise ValueError("in-flight sync result event is invalid")
                expected_sequence += 1
                event_kind = str(value["kind"])
                self._event_counts[event_kind] += 1
                self._event_digest.update(line)
                if event_kind in {
                    "new_active_representation",
                    "citation_rewrite",
                }:
                    identity = _event_identity(
                        event_kind,
                        value["data"],  # type: ignore[arg-type]
                    )
                    record_sha256 = value["data"].get("record_sha256")
                    event_record_sha256[identity] = (
                        record_sha256 if isinstance(record_sha256, str) else None
                    )
                continue
            if kind == "commit":
                if (
                    set(value)
                    != {
                        "type",
                        "event_kind",
                        "event_sha256",
                        "record_sha256",
                    }
                    or value["event_kind"]
                    not in {"new_active_representation", "citation_rewrite"}
                    or not isinstance(value["event_sha256"], str)
                    or _SHA256_RE.fullmatch(value["event_sha256"]) is None
                    or not isinstance(value["record_sha256"], str)
                    or _SHA256_RE.fullmatch(value["record_sha256"]) is None
                ):
                    raise ValueError("in-flight sync result commit is invalid")
                identity = (
                    str(value["event_kind"]),
                    str(value["event_sha256"]),
                )
                if (
                    identity not in event_record_sha256
                    or value["record_sha256"] != event_record_sha256[identity]
                ):
                    raise ValueError(
                        "in-flight sync result commit has no prepared event"
                    )
                self._committed_event_ids.add(identity)
                continue
            raise ValueError("in-flight sync result ordering is invalid")
        if not saw_header or generated_at is None:
            raise ValueError("in-flight sync result is incomplete")
        if command != expected_command:
            raise ValueError(
                "in-flight sync result must be recovered by rerunning " + command
            )
        self._sequence = expected_sequence - 1
        os.lseek(self._descriptor, 0, os.SEEK_END)
        return command, generated_at

    def _existing_event(
        self,
        kind: str,
        data: Mapping[str, JSONValue],
    ) -> bool:
        identity = _event_identity(kind, data)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        for value, _line in _iter_objects(self._descriptor):
            if value.get("type") != "event" or value.get("kind") != kind:
                continue
            existing_data = value.get("data")
            assert isinstance(existing_data, dict)
            if _event_identity(kind, existing_data) != identity:
                continue
            if existing_data != dict(data):
                raise ValueError("in-flight sync event identity changed on recovery")
            os.lseek(self._descriptor, 0, os.SEEK_END)
            return True
        os.lseek(self._descriptor, 0, os.SEEK_END)
        return False

    def _write_object(self, value: Mapping[str, JSONValue]) -> None:
        line = _canonical_line(value)
        _validate_line_size(line)
        _write_all(self._descriptor, line)

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1
        if self._name and not self._recoverable:
            try:
                os.unlink(self._name, dir_fd=self._directory.descriptor)
            except FileNotFoundError:
                pass
            self._name = ""
        self._directory.close()

    def __enter__(self) -> SyncResultWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        if not self._finished:
            self.close()


class SyncResultStore:
    def __init__(self, paths: RepoPaths) -> None:
        self.paths = paths

    def require_no_snapshot_continuation(self) -> None:
        """Call under the source lock, before any conflicting source mutation."""
        if self.load_snapshot_continuation() is not None:
            raise ValueError("snapshot result must be recovered and acknowledged first")

    def compact_sync_data(self, data: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Keep complete handoff delivery out of the bounded recovery envelope."""
        from .ledger import _atomic_write_at

        result = dict(data)
        if "handoff_response" in result:
            raise ValueError("public sync data must not contain a handoff reference")
        if not result.get("handoffs"):
            return result
        payload = _canonical_json(
            {
                "schema_version": 1,
                "handoff_manifest": result.get("handoff_manifest"),
                "handoffs": result["handoffs"],
            }
        )
        if len(payload) > _MAX_HANDOFF_RESPONSE_BYTES:
            raise ValueError("handoff response exceeds its manifest byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        name = f"handoffs_{digest}.json"
        with self._open_directory() as directory:
            _atomic_write_at(
                directory.descriptor, name, payload, replace_existing=False
            )
            directory.validate()
        del result["handoffs"]
        result["handoff_response"] = {
            "path": f".brain/sync-results/{name}",
            "sha256": digest,
        }
        if self.restore_sync_data(result) != dict(data):
            raise ValueError("handoff response changed during publication")
        return result

    def restore_sync_data(self, data: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Reconstruct the exact public array from its pinned digest-bound bytes."""
        result = dict(data)
        if "handoff_response" not in result:
            if (
                "handoff_manifest" in result
                and type(result.get("handoffs")) is not list
            ):
                raise ValueError("handoff response is missing from its receipt")
            return result
        reference = result.pop("handoff_response")
        if (
            type(reference) is not dict
            or set(reference) != {"path", "sha256"}
            or type(reference["sha256"]) is not str
            or _SHA256_RE.fullmatch(reference["sha256"]) is None
            or "handoffs" in result
        ):
            raise ValueError("invalid handoff response reference")
        name = f"handoffs_{reference['sha256']}.json"
        if reference["path"] != f".brain/sync-results/{name}":
            raise ValueError("handoff response path must be canonical")
        with self._open_directory() as directory:
            payload, _ = _read_regular_at(
                directory.descriptor,
                name,
                label="handoff response",
                max_bytes=_MAX_HANDOFF_RESPONSE_BYTES,
            )
            directory.validate()
        if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
            raise ValueError("handoff response digest mismatch")
        document = _load_json_object(payload, "handoff response")
        if (
            set(document) != {"schema_version", "handoff_manifest", "handoffs"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["handoff_manifest"] != result.get("handoff_manifest")
            or type(document["handoffs"]) is not list
            or not document["handoffs"]
        ):
            raise ValueError("handoff response does not match its receipt")
        result["handoffs"] = document["handoffs"]
        return result

    def writer(
        self,
        command: str,
        generated_at: datetime,
        *,
        recoverable: bool = False,
    ) -> SyncResultWriter:
        if command != _SNAPSHOT_COMMAND:
            self.require_no_snapshot_continuation()
        return SyncResultWriter(
            self,
            command,
            generated_at,
            recoverable=recoverable,
        )

    def save_snapshot_continuation(self, continuation: SnapshotContinuation) -> None:
        from .ledger import _atomic_write_at

        payload = self.preflight_snapshot_receipts(continuation)
        with self._open_directory() as directory:
            _atomic_write_at(
                directory.descriptor,
                _SNAPSHOT_CONTINUATION_NAME,
                payload,
                replace_existing=False,
            )
            directory.validate()

    def preflight_snapshot_receipts(self, continuation: SnapshotContinuation) -> bytes:
        """Size every canonical UTF-8 envelope before any final checkpoint."""
        payload = _snapshot_continuation_payload(continuation)
        digest = hashlib.sha256(payload).hexdigest()
        # The eventual manifest ID/path/hash are all fixed-width hex strings;
        # the only snapshot event count is exactly zero or one. These reserved
        # bytes have exactly the same serialized length as the finalized ref.
        placeholder = "0" * 64
        reference = SyncResultReference(
            "sync_" + placeholder,
            PurePosixPath(".brain/sync-results", "sync_" + placeholder + ".jsonl"),
            placeholder,
            continuation.corpus_revision,
            {
                kind: int(
                    kind == "new_active_representation"
                    and continuation.event is not None
                )
                for kind in _EVENT_KINDS
            },
        )
        for receipt in (
            StagedSnapshotResult(
                continuation.generated_at,
                continuation.corpus_revision,
                continuation.checkpoint_sha256,
                continuation.committed_journal_sha256,
                digest,
            ),
            PendingSnapshotResult(continuation.generated_at, reference, digest),
            AcknowledgedSnapshotResult(reference, digest),
        ):
            _snapshot_receipt_payload(receipt)
        return payload

    def _require_snapshot_receipt(self, receipt) -> None:
        continuation = self.load_snapshot_continuation()
        if continuation is None or continuation.sha256 != receipt.continuation_sha256:
            raise ValueError("snapshot receipt does not match its continuation")
        if receipt.generated_at != continuation.generated_at:
            raise ValueError("snapshot receipt timestamp does not match continuation")
        if isinstance(receipt, StagedSnapshotResult):
            if (
                receipt.corpus_revision,
                receipt.checkpoint_sha256,
                receipt.journal_sha256,
            ) != (
                continuation.corpus_revision,
                continuation.checkpoint_sha256,
                continuation.committed_journal_sha256,
            ):
                raise ValueError("staged snapshot bindings do not match continuation")
        else:
            self._require_snapshot_reference(continuation, receipt.reference)

    def _require_snapshot_reference(
        self,
        continuation: SnapshotContinuation,
        reference: SyncResultReference,
    ) -> None:
        """Bind the finalized public manifest, independently of response hashing."""
        expected = (
            []
            if continuation.event is None
            else [
                SyncEvent(
                    continuation.event.kind,
                    _public_event_data(continuation.event),
                )
            ]
        )
        counts = {
            kind: sum(event.kind == kind for event in expected) for kind in _EVENT_KINDS
        }
        if (
            reference.corpus_revision != continuation.corpus_revision
            or dict(reference.event_counts) != counts
        ):
            raise ValueError("snapshot manifest bindings do not match continuation")
        metadata = self._verified_metadata(reference)
        if (
            metadata.command != _SNAPSHOT_COMMAND
            or metadata.generated_at != continuation.generated_at
            or list(self.iter_events(reference)) != expected
        ):
            raise ValueError("snapshot manifest events do not match continuation")

    def load_snapshot_continuation(self) -> SnapshotContinuation | None:
        with self._open_directory() as directory:
            try:
                payload, _ = _read_regular_at(
                    directory.descriptor,
                    _SNAPSHOT_CONTINUATION_NAME,
                    label="snapshot continuation",
                    max_bytes=_MAX_SNAPSHOT_RECEIPT_BYTES,
                )
            except FileNotFoundError:
                return None
            directory.validate()
        value = _load_json_object(payload, "snapshot continuation")
        if (
            set(value)
            != {
                "schema_version",
                "kind",
                "source_id",
                "candidate_sha256",
                "corpus_revision",
                "request_identity",
                "generated_at",
                "checkpoint_sha256",
                "result_recipe",
                "warnings",
                "response_sha256",
                "journal_sha256",
                "committed_journal_sha256",
                "event",
            }
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 2
            or value["kind"] != "source_snapshot_url"
            or not isinstance(value["request_identity"], dict)
            or not isinstance(value["result_recipe"], dict)
            or not isinstance(value["warnings"], list)
            or not isinstance(value["generated_at"], str)
        ):
            raise ValueError("snapshot continuation fields are invalid")
        raw_event = value["event"]
        event = None
        if raw_event is not None:
            if (
                not isinstance(raw_event, dict)
                or set(raw_event) != {"kind", "data"}
                or not isinstance(raw_event["data"], dict)
            ):
                raise ValueError("snapshot continuation event fields are invalid")
            event = SyncEvent(raw_event["kind"], raw_event["data"])
        return SnapshotContinuation(
            value["source_id"],
            value["candidate_sha256"],
            value["checkpoint_sha256"],
            value["corpus_revision"],
            value["request_identity"],
            datetime.fromisoformat(value["generated_at"]),
            value["result_recipe"],
            tuple(value["warnings"]),
            value["response_sha256"],
            value["journal_sha256"],
            value["committed_journal_sha256"],
            event,
        )

    def clear_snapshot_continuation(self, reference: SyncResultReference) -> None:
        continuation = self.load_snapshot_continuation()
        if continuation is None:
            return
        acknowledged = self.load_acknowledged()
        metadata = self._verified_metadata(reference)
        if (
            not isinstance(acknowledged, AcknowledgedSnapshotResult)
            or acknowledged.reference != reference
            or acknowledged.continuation_sha256 != continuation.sha256
            or acknowledged.command != _SNAPSHOT_COMMAND
            or metadata.generated_at != continuation.generated_at
            or reference.corpus_revision != continuation.corpus_revision
        ):
            raise ValueError("snapshot continuation has not been acknowledged")
        self._require_snapshot_reference(continuation, reference)
        with self._open_directory() as directory:
            os.unlink(_SNAPSHOT_CONTINUATION_NAME, dir_fd=directory.descriptor)
            _fsync_directory(directory.descriptor)
            directory.validate()

    def verify(self, reference: SyncResultReference) -> None:
        self._verified_metadata(reference)

    def _verified_metadata(
        self,
        reference: SyncResultReference,
    ) -> _ManifestMetadata:
        with self._open_directory() as directory:
            descriptor, before = _open_manifest(directory.descriptor, reference)
            try:
                metadata = _verify_descriptor(descriptor, reference)
                after = os.fstat(descriptor)
                named = os.stat(
                    reference.path.name,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                if not (
                    _same_file_observation(before, after)
                    and _same_file_observation(after, named)
                ):
                    raise UnsafeFilesystemError(
                        "sync result changed while it was verified"
                    )
                directory.validate()
            finally:
                os.close(descriptor)
        return metadata

    def iter_events(self, reference: SyncResultReference) -> Iterator[SyncEvent]:
        def iterator() -> Iterator[SyncEvent]:
            with self._open_directory() as directory:
                descriptor, before = _open_manifest(directory.descriptor, reference)
                try:
                    _verify_descriptor(descriptor, reference)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    for value, _line in _iter_objects(descriptor):
                        if value.get("type") == "event":
                            yield SyncEvent(
                                str(value["kind"]),
                                value["data"],  # type: ignore[arg-type]
                            )
                    after = os.fstat(descriptor)
                    named = os.stat(
                        reference.path.name,
                        dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                    if not (
                        _same_file_observation(before, after)
                        and _same_file_observation(after, named)
                    ):
                        raise UnsafeFilesystemError(
                            "sync result changed while events were consumed"
                        )
                    directory.validate()
                finally:
                    os.close(descriptor)

        return iterator()

    def _save_compact_receipt(self, name, receipt) -> None:
        from .ledger import _atomic_write_at

        self._require_snapshot_receipt(receipt)
        payload = _snapshot_receipt_payload(receipt)
        with self._open_directory() as directory:
            _atomic_write_at(directory.descriptor, name, payload)
            directory.validate()

    def save_pending(self, pending: PendingSyncResult | PendingSnapshotResult) -> None:
        if isinstance(pending, PendingSnapshotResult):
            self._save_compact_receipt("pending.json", pending)
            return
        self.restore_sync_data(pending.result_data)
        payload = _canonical_json(
            {
                "schema_version": 1,
                "command": pending.command,
                "generated_at": pending.generated_at.isoformat(),
                "reference": pending.reference.to_dict(),
                "result_data": dict(pending.result_data),
            }
        )
        if len(payload) > _MAX_PENDING_BYTES:
            raise ValueError("pending sync result exceeds its byte limit")
        with self._open_directory() as directory:
            from .ledger import _atomic_write_at

            _atomic_write_at(directory.descriptor, "pending.json", payload)
            directory.validate()

    def load_pending(self) -> PendingSyncResult | PendingSnapshotResult | None:
        with self._open_directory() as directory:
            try:
                payload, _metadata = _read_regular_at(
                    directory.descriptor,
                    "pending.json",
                    label="pending sync result",
                    max_bytes=_MAX_PENDING_BYTES,
                )
            except FileNotFoundError:
                return None
            directory.validate()
        value = _load_json_object(payload, "pending sync result")
        if (
            value.get("command") == _SNAPSHOT_COMMAND
            or value.get("schema_version") == 2
        ):
            pending = _decode_snapshot_receipt(value, "snapshot_pending")
            self._require_snapshot_receipt(pending)
            metadata = self._verified_metadata(pending.reference)
            if (
                metadata.command != pending.command
                or metadata.generated_at != pending.generated_at
            ):
                raise ValueError("pending snapshot metadata does not match manifest")
            return pending
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "command",
                "generated_at",
                "reference",
                "result_data",
            }
            or value["schema_version"] != 1
        ):
            raise ValueError("pending sync result has invalid fields")
        generated_at = datetime.fromisoformat(str(value["generated_at"]))
        if not isinstance(value["result_data"], dict):
            raise ValueError("pending sync result data must be an object")
        pending = PendingSyncResult(
            str(value["command"]),
            generated_at,
            SyncResultReference.from_dict(value["reference"]),
            value["result_data"],
        )
        self.restore_sync_data(pending.result_data)
        metadata = self._verified_metadata(pending.reference)
        if (
            metadata.command != pending.command
            or metadata.generated_at != pending.generated_at
        ):
            raise ValueError("pending sync result metadata does not match manifest")
        return pending

    def save_staged(self, staged: StagedSyncResult | StagedSnapshotResult) -> None:
        if isinstance(staged, StagedSnapshotResult):
            self._save_compact_receipt(_STAGED_NAME, staged)
            return
        self.restore_sync_data(staged.result_data)
        payload = _canonical_json(
            {
                "schema_version": 1,
                "command": staged.command,
                "generated_at": staged.generated_at.isoformat(),
                "corpus_revision": staged.corpus_revision,
                "checkpoint_sha256": staged.checkpoint_sha256,
                "journal_sha256": staged.journal_sha256,
                "result_data": dict(staged.result_data),
            }
        )
        if len(payload) > _MAX_PENDING_BYTES:
            raise ValueError("staged sync result exceeds its byte limit")
        with self._open_directory() as directory:
            from .ledger import _atomic_write_at

            _atomic_write_at(directory.descriptor, _STAGED_NAME, payload)
            directory.validate()

    def load_staged(self) -> StagedSyncResult | StagedSnapshotResult | None:
        with self._open_directory() as directory:
            try:
                payload, _metadata = _read_regular_at(
                    directory.descriptor,
                    _STAGED_NAME,
                    label="staged sync result",
                    max_bytes=_MAX_PENDING_BYTES,
                )
            except FileNotFoundError:
                return None
            directory.validate()
        value = _load_json_object(payload, "staged sync result")
        if (
            value.get("command") == _SNAPSHOT_COMMAND
            or value.get("schema_version") == 2
        ):
            staged = _decode_snapshot_receipt(value, "snapshot_staged")
            self._require_snapshot_receipt(staged)
            return staged
        if (
            set(value)
            != {
                "schema_version",
                "command",
                "generated_at",
                "corpus_revision",
                "checkpoint_sha256",
                "journal_sha256",
                "result_data",
            }
            or value["schema_version"] != 1
            or not isinstance(value["result_data"], dict)
        ):
            raise ValueError("staged sync result has invalid fields")
        staged = StagedSyncResult(
            str(value["command"]),
            datetime.fromisoformat(str(value["generated_at"])),
            str(value["corpus_revision"]),
            str(value["checkpoint_sha256"]),
            str(value["journal_sha256"]),
            value["result_data"],
        )
        self.restore_sync_data(staged.result_data)
        return staged

    def clear_pending(self, reference: SyncResultReference) -> None:
        pending = self.load_pending()
        if pending is None:
            acknowledged = self.load_acknowledged()
            if acknowledged is not None and acknowledged.reference == reference:
                return
            raise ValueError("pending sync result is not available to acknowledge")
        if pending.reference != reference:
            raise ValueError("pending sync result changed before acknowledgement")
        if isinstance(pending, PendingSnapshotResult):
            payload = _snapshot_receipt_payload(
                AcknowledgedSnapshotResult(reference, pending.continuation_sha256)
            )
        else:
            payload = _canonical_json(
                {
                    "schema_version": 1,
                    "command": pending.command,
                    "reference": reference.to_dict(),
                }
            )
        with self._open_directory() as directory:
            from .ledger import _atomic_write_at

            _atomic_write_at(
                directory.descriptor,
                _ACKNOWLEDGED_NAME,
                payload,
            )
            os.unlink("pending.json", dir_fd=directory.descriptor)
            _fsync_directory(directory.descriptor)
            directory.validate()

    def load_acknowledged(
        self,
    ) -> AcknowledgedSyncResult | AcknowledgedSnapshotResult | None:
        with self._open_directory() as directory:
            try:
                payload, _metadata = _read_regular_at(
                    directory.descriptor,
                    _ACKNOWLEDGED_NAME,
                    label="acknowledged sync result",
                    max_bytes=_MAX_PENDING_BYTES,
                )
            except FileNotFoundError:
                return None
            directory.validate()
        value = _load_json_object(payload, "acknowledged sync result")
        if (
            value.get("command") == _SNAPSHOT_COMMAND
            or value.get("schema_version") == 2
        ):
            acknowledged = _decode_snapshot_receipt(value, "snapshot_acknowledged")
            metadata = self._verified_metadata(acknowledged.reference)
            if metadata.command != acknowledged.command:
                raise ValueError(
                    "acknowledged snapshot metadata does not match manifest"
                )
            return acknowledged
        if (
            set(value) != {"schema_version", "command", "reference"}
            or value["schema_version"] != 1
            or not isinstance(value["command"], str)
        ):
            raise ValueError("acknowledged sync result has invalid fields")
        acknowledged = AcknowledgedSyncResult(
            value["command"],
            SyncResultReference.from_dict(value["reference"]),
        )
        metadata = self._verified_metadata(acknowledged.reference)
        if metadata.command != acknowledged.command:
            raise ValueError(
                "acknowledged sync result metadata does not match manifest"
            )
        return acknowledged

    def complete_inflight(
        self,
        reference: SyncResultReference,
        *,
        checkpoint_sha256: str,
    ) -> None:
        if _SHA256_RE.fullmatch(checkpoint_sha256) is None:
            raise ValueError("ledger checkpoint digest must be canonical")
        self.verify(reference)
        staged = self.load_staged()
        if staged is not None:
            if checkpoint_sha256 != staged.checkpoint_sha256:
                raise ValueError(
                    "staged sync result does not match the canonical ledger checkpoint"
                )
            metadata = self._verified_metadata(reference)
            if (
                staged.command != metadata.command
                or staged.generated_at != metadata.generated_at
                or staged.corpus_revision != reference.corpus_revision
            ):
                raise ValueError(
                    "staged sync result does not match its published manifest"
                )
        with self._open_directory() as directory:
            try:
                descriptor, before = _open_named_regular(
                    directory.descriptor,
                    _INFLIGHT_NAME,
                    "in-flight sync result",
                )
            except FileNotFoundError:
                descriptor = -1
            if descriptor >= 0:
                try:
                    _verify_inflight_descriptor(
                        descriptor,
                        command=None if staged is None else staged.command,
                        generated_at=(None if staged is None else staged.generated_at),
                    )
                    if (
                        staged is not None
                        and _hash_descriptor(descriptor) != staged.journal_sha256
                    ):
                        raise ValueError(
                            "staged sync result journal changed before completion"
                        )
                    after = os.fstat(descriptor)
                    named = os.stat(
                        _INFLIGHT_NAME,
                        dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                    if not (
                        _same_file_observation(before, after)
                        and _same_file_observation(after, named)
                    ):
                        raise UnsafeFilesystemError(
                            "in-flight sync result changed before completion"
                        )
                finally:
                    os.close(descriptor)
                os.unlink(_INFLIGHT_NAME, dir_fd=directory.descriptor)
            try:
                os.unlink(_STAGED_NAME, dir_fd=directory.descriptor)
            except FileNotFoundError:
                pass
            _fsync_directory(directory.descriptor)
            directory.validate()

    def _open_directory(self) -> _PinnedDirectory:
        _ensure_result_directory(self.paths)
        return _PinnedDirectory.open(self.paths.root / ".brain/sync-results")


def _ensure_result_directory(paths: RepoPaths) -> None:
    with _PinnedDirectory.open(paths.root) as root:
        parent = root.descriptor
        for component in (".brain", "sync-results"):
            try:
                os.mkdir(component, mode=0o700, dir_fd=parent)
                _fsync_directory(parent)
            except FileExistsError:
                pass
            parent = root.open_child(parent, component)
        root.validate()


def _open_manifest(
    directory_fd: int,
    reference: SyncResultReference,
) -> tuple[int, os.stat_result]:
    named = os.stat(
        reference.path.name,
        dir_fd=directory_fd,
        follow_symlinks=False,
    )
    if not stat.S_ISREG(named.st_mode):
        raise ValueError("sync result manifest must be a regular file")
    descriptor = os.open(
        reference.path.name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory_fd,
    )
    opened = os.fstat(descriptor)
    if not _same_file_observation(named, opened):
        os.close(descriptor)
        raise UnsafeFilesystemError("sync result changed while it was opened")
    return descriptor, opened


def _open_named_regular(
    directory_fd: int,
    name: str,
    label: str,
) -> tuple[int, os.stat_result]:
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode):
        raise ValueError(f"{label} must be a regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory_fd,
    )
    opened = os.fstat(descriptor)
    if not _same_file_observation(named, opened):
        os.close(descriptor)
        raise UnsafeFilesystemError(f"{label} changed while it was opened")
    return descriptor, opened


def _verify_descriptor(
    descriptor: int,
    reference: SyncResultReference,
) -> _ManifestMetadata:
    os.lseek(descriptor, 0, os.SEEK_SET)
    whole_digest = hashlib.sha256()
    event_digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    expected_sequence = 1
    saw_header = False
    saw_trailer = False
    manifest_command = ""
    manifest_generated_at: datetime | None = None
    for value, line in _iter_objects(descriptor):
        whole_digest.update(line)
        kind = value.get("type")
        if not saw_header:
            if (
                kind != "header"
                or set(value)
                != {
                    "type",
                    "schema_version",
                    "command",
                    "generated_at",
                }
                or value["schema_version"] != 1
            ):
                raise ValueError("sync result header is invalid")
            if value["command"] not in _RESULT_COMMANDS:
                raise ValueError("sync result command is invalid")
            timestamp = datetime.fromisoformat(str(value["generated_at"]))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("sync result timestamp must be aware")
            manifest_command = str(value["command"])
            manifest_generated_at = timestamp
            saw_header = True
            continue
        if kind == "event" and not saw_trailer:
            if set(value) != {"type", "sequence", "kind", "data"}:
                raise ValueError("sync result event fields are invalid")
            if value["sequence"] != expected_sequence:
                raise ValueError("sync result event sequence is invalid")
            event_kind = value["kind"]
            if event_kind not in _EVENT_KINDS or not isinstance(value["data"], dict):
                raise ValueError("sync result event payload is invalid")
            expected_sequence += 1
            counts[str(event_kind)] += 1
            event_digest.update(line)
            continue
        if kind == "trailer" and not saw_trailer:
            if set(value) != {
                "type",
                "event_count",
                "event_counts",
                "events_sha256",
                "corpus_revision",
            }:
                raise ValueError("sync result trailer fields are invalid")
            if value["event_count"] != expected_sequence - 1:
                raise ValueError("sync result event count is invalid")
            expected_counts = {kind: counts[kind] for kind in sorted(_EVENT_KINDS)}
            if value["event_counts"] != expected_counts:
                raise ValueError("sync result event counts are invalid")
            if value["events_sha256"] != event_digest.hexdigest():
                raise ValueError("sync result event digest is invalid")
            if value["corpus_revision"] != reference.corpus_revision:
                raise ValueError("sync result corpus revision is invalid")
            saw_trailer = True
            continue
        raise ValueError("sync result manifest ordering is invalid")
    if not saw_header or not saw_trailer:
        raise ValueError("sync result manifest is incomplete")
    if whole_digest.hexdigest() != reference.sha256:
        raise ValueError("sync result manifest digest is invalid")
    expected_counts = {kind: counts[kind] for kind in sorted(_EVENT_KINDS)}
    if expected_counts != dict(reference.event_counts):
        raise ValueError("sync result reference counts are invalid")
    assert manifest_generated_at is not None
    return _ManifestMetadata(manifest_command, manifest_generated_at)


def _verify_inflight_descriptor(
    descriptor: int,
    *,
    command: str | None,
    generated_at: datetime | None,
) -> None:
    saw_header = False
    expected_sequence = 1
    event_record_sha256: dict[tuple[str, ...], str | None] = {}
    for value, _line in _iter_objects(descriptor):
        kind = value.get("type")
        if not saw_header:
            if (
                kind != "header"
                or set(value) != {"type", "schema_version", "command", "generated_at"}
                or value["schema_version"] != 1
                or value["command"] not in _RESULT_COMMANDS
            ):
                raise ValueError("in-flight sync result header is invalid")
            timestamp = datetime.fromisoformat(str(value["generated_at"]))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("in-flight sync timestamp must be aware")
            if command is not None and value["command"] != command:
                raise ValueError("in-flight sync result command changed")
            if generated_at is not None and timestamp != generated_at:
                raise ValueError("in-flight sync result timestamp changed")
            saw_header = True
            continue
        if kind == "event":
            if (
                set(value) != {"type", "sequence", "kind", "data"}
                or value["sequence"] != expected_sequence
                or value["kind"] not in _EVENT_KINDS
                or not isinstance(value["data"], dict)
            ):
                raise ValueError("in-flight sync result event is invalid")
            expected_sequence += 1
            if value["kind"] in {
                "new_active_representation",
                "citation_rewrite",
            }:
                identity = _event_identity(str(value["kind"]), value["data"])
                record_sha256 = value["data"].get("record_sha256")
                event_record_sha256[identity] = (
                    record_sha256 if isinstance(record_sha256, str) else None
                )
            continue
        if kind == "commit":
            if (
                set(value)
                != {
                    "type",
                    "event_kind",
                    "event_sha256",
                    "record_sha256",
                }
                or value["event_kind"]
                not in {"new_active_representation", "citation_rewrite"}
                or not isinstance(value["event_sha256"], str)
                or _SHA256_RE.fullmatch(value["event_sha256"]) is None
                or not isinstance(value["record_sha256"], str)
                or _SHA256_RE.fullmatch(value["record_sha256"]) is None
                or (
                    str(value["event_kind"]),
                    str(value["event_sha256"]),
                )
                not in event_record_sha256
            ):
                raise ValueError("in-flight sync result commit is invalid")
            identity = (
                str(value["event_kind"]),
                str(value["event_sha256"]),
            )
            if value["record_sha256"] != event_record_sha256[identity]:
                raise ValueError("in-flight sync result commit checkpoint changed")
            continue
        raise ValueError("in-flight sync result ordering is invalid")
    if not saw_header:
        raise ValueError("in-flight sync result is incomplete")


def _iter_objects(descriptor: int) -> Iterator[tuple[dict[str, JSONValue], bytes]]:
    duplicate = os.dup(descriptor)
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            duplicate = -1
            while True:
                line = stream.readline(_MAX_EVENT_LINE_BYTES + 1)
                if not line:
                    return
                _validate_line_size(line)
                if not line.endswith(b"\n"):
                    raise ValueError("sync result line must end with a newline")
                value = _load_json_object(line, "sync result line")
                yield value, line
    finally:
        if duplicate >= 0:
            os.close(duplicate)


def _iter_recoverable_objects(
    descriptor: int,
) -> Iterator[tuple[dict[str, JSONValue], bytes]]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    duplicate = os.dup(descriptor)
    offset = 0
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            duplicate = -1
            while True:
                line = stream.readline(_MAX_EVENT_LINE_BYTES + 1)
                if not line:
                    return
                _validate_line_size(line)
                if not line.endswith(b"\n"):
                    os.ftruncate(descriptor, offset)
                    os.fsync(descriptor)
                    return
                value = _load_json_object(line, "in-flight sync result line")
                yield value, line
                offset += len(line)
    finally:
        if duplicate >= 0:
            os.close(duplicate)


def _event_identity(
    kind: str,
    data: Mapping[str, JSONValue],
) -> tuple[str, ...]:
    return (
        kind,
        hashlib.sha256(_canonical_json(dict(data))).hexdigest(),
    )


def _public_event_data(event: SyncEvent) -> dict[str, JSONValue]:
    data = dict(event.data)
    data.pop("record_sha256", None)
    if event.kind == "coverage_gap":
        data.pop("source_id", None)
        data.pop("occurrence", None)
    return data


def _canonical_line(value: Mapping[str, JSONValue]) -> bytes:
    return _canonical_json(value) + b"\n"


def _canonical_json(value: Mapping[str, JSONValue]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _load_json_object(payload: bytes, label: str) -> dict[str, JSONValue]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _unique_json_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    value: dict[str, JSONValue] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> JSONValue:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_line_size(line: bytes) -> None:
    if len(line) > _MAX_EVENT_LINE_BYTES:
        raise ValueError("sync result event exceeds its byte limit")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "sync result write made no progress")
        view = view[written:]


def _hash_descriptor(
    descriptor: int, *, suffix: bytes = b"", limit: int | None = None
) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    if limit is not None and limit < 0:
        raise ValueError("journal prefix length is invalid")
    remaining = limit
    while remaining != 0:
        chunk = os.read(
            descriptor,
            1024 * 1024 if remaining is None else min(remaining, 1024 * 1024),
        )
        if not chunk:
            if remaining is not None:
                raise ValueError("journal prefix is truncated")
            break
        digest.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    digest.update(suffix)
    return digest.hexdigest()
