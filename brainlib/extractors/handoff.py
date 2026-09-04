"""Immutable agent work items and staged, provenance-bound Markdown publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from ..contracts import (
    Anchor,
    Derivation,
    ProcessingAttempt,
    SourceRecord,
    SourceState,
    derivation_id,
)
from ..diagnostics import Diagnostic, JSONValue, ValidationIssue
from ..inventory import SnapshotNamespace, stable_file_snapshot
from ..layout import RepoPaths
from ..ledger import (
    _PinnedDirectory,
    _atomic_write_at,
    _read_regular_at,
    _reject_duplicate_keys,
    _same_file_observation,
    _validate_raw_path,
    derive_extraction_path,
)
from ..registry import (
    ExtractorRegistry,
    ExtractorSpec,
    effective_extractor_version,
    is_normalized_identifier,
)
from ..sync import ProcessResult
from .adapters import publish_markdown_artifact, validate_markdown

if TYPE_CHECKING:
    from .processor import Job

HandoffKind = Literal["extraction", "rendered_web_capture"]
MAX_AGENT_STAGING_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HANDOFF_ID = re.compile(r"hnd_[0-9a-f]{64}\Z")
_SOURCE_ID = re.compile(r"src_[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[1-9][0-9]*\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ANCHOR_KINDS = frozenset({"line", "page", "slide", "sheet", "section", "row", "block"})
_ITEM_FIELDS = frozenset(
    {
        "handoff_id",
        "kind",
        "source_id",
        "content_sha256",
        "raw_path",
        "media_type",
        "reason",
        "required_anchor_kinds",
        "extractor_id",
        "extractor_version",
        "config_sha256",
        "prerequisite_digest",
        "agent_revision",
        "diagnostics",
    }
)


class AgentRegistrationError(ValueError):
    def __init__(
        self, message: str, *, code: str = "agent_registration_invalid"
    ) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class HandoffItem:
    handoff_id: str
    kind: HandoffKind
    source_id: str
    content_sha256: str
    raw_path: PurePosixPath
    media_type: str
    reason: str
    required_anchor_kinds: tuple[str, ...]
    extractor_id: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    agent_revision: str
    diagnostics: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_anchor_kinds", tuple(self.required_anchor_kinds)
        )
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


@dataclass(frozen=True)
class HandoffManifest:
    schema_version: int
    run_id: str
    created_at: datetime
    items: tuple[HandoffItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))


@dataclass(frozen=True)
class HandoffSummary:
    handoff_id: str
    kind: HandoffKind
    source_id: str
    content_sha256: str
    reason: str

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "handoff_id": self.handoff_id,
            "kind": self.kind,
            "source_id": self.source_id,
            "content_sha256": self.content_sha256,
            "reason": self.reason,
        }


def _canonical(document: Mapping[str, JSONValue]) -> bytes:
    return json.dumps(
        dict(document), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def handoff_id_for(item_without_id: Mapping[str, JSONValue]) -> str:
    if set(item_without_id) != _ITEM_FIELDS - {"handoff_id"}:
        raise AgentRegistrationError(
            "handoff fields are incomplete or unexpected", code="handoff_invalid"
        )
    return "hnd_" + hashlib.sha256(_canonical(item_without_id)).hexdigest()


def handoff_to_dict(item: HandoffItem) -> dict[str, JSONValue]:
    """The sole canonical item serializer; field order is fixed by canonical JSON."""
    return {
        "handoff_id": item.handoff_id,
        "kind": item.kind,
        "source_id": item.source_id,
        "content_sha256": item.content_sha256,
        "raw_path": item.raw_path.as_posix(),
        "media_type": item.media_type,
        "reason": item.reason,
        "required_anchor_kinds": list(item.required_anchor_kinds),
        "extractor_id": item.extractor_id,
        "extractor_version": item.extractor_version,
        "config_sha256": item.config_sha256,
        "prerequisite_digest": item.prerequisite_digest,
        "agent_revision": item.agent_revision,
        "diagnostics": [
            {
                "code": d.code,
                "message": d.message,
                "path": None if d.path is None else d.path.as_posix(),
                "details": dict(d.details),
            }
            for d in item.diagnostics
        ],
    }


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or "\0" in value:
        raise ValueError(f"{label} must be nonblank text")
    return value


def _path(value: object, label: str) -> PurePosixPath:
    text = _text(value, label)
    path = PurePosixPath(text)
    if path.as_posix() != text:
        raise ValueError(f"{label} must be canonical")
    _validate_raw_path(path)
    return path


def _decode_item(document: object) -> HandoffItem:
    if type(document) is not dict or set(document) != _ITEM_FIELDS:
        raise ValueError("invalid handoff item fields")
    for field, pattern in (
        ("handoff_id", _HANDOFF_ID),
        ("source_id", _SOURCE_ID),
        ("content_sha256", _SHA256),
        ("config_sha256", _SHA256),
        ("prerequisite_digest", _SHA256),
        ("agent_revision", _REVISION),
    ):
        if not pattern.fullmatch(_text(document[field], field)):
            raise ValueError(f"invalid handoff {field}")
    if document["kind"] not in ("extraction", "rendered_web_capture"):
        raise ValueError("invalid handoff kind")
    for field in ("media_type", "reason", "extractor_version"):
        _text(document[field], field)
    if not is_normalized_identifier(document["extractor_id"]):
        raise ValueError("invalid handoff extractor_id")
    base, separator, digest = document["extractor_version"].rpartition("+")
    if not base or separator != "+" or digest != document["prerequisite_digest"]:
        raise ValueError("handoff extractor version must bind its prerequisite digest")
    kinds = document["required_anchor_kinds"]
    if (
        type(kinds) is not list
        or not kinds
        or any(type(k) is not str or k not in _ANCHOR_KINDS for k in kinds)
        or len(kinds) != len(set(kinds))
    ):
        raise ValueError("invalid handoff required anchor kinds")
    raw_path = _path(document["raw_path"], "raw_path")
    diagnostics = document["diagnostics"]
    if type(diagnostics) is not list:
        raise ValueError("handoff diagnostics must be an array")
    decoded = []
    for item in diagnostics:
        if type(item) is not dict or set(item) != {
            "code",
            "message",
            "path",
            "details",
        }:
            raise ValueError("invalid handoff diagnostic")
        if type(item["details"]) is not dict:
            raise ValueError("invalid handoff diagnostic details")
        _canonical(item["details"])
        decoded.append(
            Diagnostic(
                _text(item["code"], "diagnostic code"),
                _text(item["message"], "diagnostic message"),
                None
                if item["path"] is None
                else _path(item["path"], "diagnostic path"),
                item["details"],
            )
        )
    return HandoffItem(
        **{
            **document,
            "raw_path": raw_path,
            "required_anchor_kinds": tuple(kinds),
            "diagnostics": tuple(decoded),
        }
    )


def _validate_item(item: HandoffItem) -> None:
    try:
        fields = handoff_to_dict(item)
        _decode_item(fields)
        identifier = fields.pop("handoff_id")
        if identifier != handoff_id_for(fields):
            raise ValueError("handoff ID does not match its immutable fields")
    except (ValueError, TypeError, AttributeError) as error:
        raise AgentRegistrationError(str(error), code="handoff_invalid") from error


@contextmanager
def _manifest_directory(
    paths: RepoPaths, *, create: bool = False
) -> Iterator[tuple[_PinnedDirectory, int]]:
    with _PinnedDirectory.open(paths.ledger_dir) as pinned:
        ledger_fd = pinned.descriptor
        if create:
            try:
                os.mkdir("handoffs", mode=0o700, dir_fd=ledger_fd)
                os.fsync(ledger_fd)
            except FileExistsError:
                pass
        manifest_fd = pinned.open_child(ledger_fd, "handoffs")
        pinned.validate()
        yield pinned, manifest_fd
        pinned.validate()


def write_handoff_manifest(
    paths: RepoPaths,
    *,
    run_id: str,
    created_at: datetime,
    items: Sequence[HandoffItem],
) -> Path:
    if type(run_id) is not str or not _RUN_ID.fullmatch(run_id):
        raise AgentRegistrationError("invalid manifest run ID", code="handoff_invalid")
    if (
        not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        raise AgentRegistrationError(
            "manifest creation time must have a timezone", code="handoff_invalid"
        )
    ordered = sorted(
        items, key=lambda item: (item.source_id, item.kind, item.handoff_id)
    )
    for item in ordered:
        _validate_item(item)
    unique: dict[str, HandoffItem] = {}
    for item in ordered:
        if item.handoff_id in unique and unique[item.handoff_id] != item:
            raise AgentRegistrationError(
                "conflicting handoff payloads", code="handoff_id_collision"
            )
        unique[item.handoff_id] = item
    envelope = HandoffManifest(1, run_id, created_at, tuple(unique.values()))
    body = (
        _canonical(
            {
                "schema_version": envelope.schema_version,
                "run_id": envelope.run_id,
                "created_at": envelope.created_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "items": [handoff_to_dict(item) for item in envelope.items],
            }
        )
        + b"\n"
    )
    if len(body) > _MAX_MANIFEST_BYTES:
        raise AgentRegistrationError(
            "handoff manifest exceeds the byte limit", code="handoff_invalid"
        )
    with _manifest_directory(paths, create=True) as (pinned, directory):
        _atomic_write_at(directory, run_id + ".json", body, replace_existing=False)
        observed, _ = _read_regular_at(
            directory,
            run_id + ".json",
            label="handoff manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        if observed != body:
            raise AgentRegistrationError(
                "manifest changed during publication", code="handoff_invalid"
            )
        pinned.validate()
    return paths.ledger_dir / "handoffs" / (run_id + ".json")


def _load_manifest_items(paths: RepoPaths) -> dict[str, HandoffItem]:
    items: dict[str, HandoffItem] = {}
    try:
        with _manifest_directory(paths) as (pinned, directory):
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".json"):
                    raise ValueError("handoff directory contains a non-manifest entry")
                body, _ = _read_regular_at(
                    directory,
                    name,
                    label="handoff manifest",
                    max_bytes=_MAX_MANIFEST_BYTES,
                )
                document = json.loads(
                    body,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"invalid JSON constant {value}")
                    ),
                )
                if (
                    type(document) is not dict
                    or set(document)
                    != {"schema_version", "run_id", "created_at", "items"}
                    or type(document["schema_version"]) is not int
                    or document["schema_version"] != 1
                    or type(document["items"]) is not list
                    or not _RUN_ID.fullmatch(_text(document["run_id"], "run_id"))
                ):
                    raise ValueError("invalid handoff manifest envelope")
                timestamp = datetime.fromisoformat(
                    _text(document["created_at"], "created_at")
                )
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("handoff manifest timestamp lacks timezone")
                for fields in document["items"]:
                    item = _decode_item(fields)
                    prior = items.get(item.handoff_id)
                    if prior is not None and _canonical(
                        handoff_to_dict(prior)
                    ) != _canonical(handoff_to_dict(item)):
                        raise AgentRegistrationError(
                            "same handoff ID has conflicting payloads",
                            code="handoff_id_collision",
                        )
                    items[item.handoff_id] = item
                pinned.validate()
    except FileNotFoundError:
        # Only absence of the reserved directory is a pristine empty namespace.
        with _PinnedDirectory.open(paths.ledger_dir) as ledger:
            try:
                os.stat("handoffs", dir_fd=ledger.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                ledger.validate()
                return {}
        raise AgentRegistrationError(
            "handoff manifest disappeared", code="handoff_invalid"
        )
    except AgentRegistrationError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise AgentRegistrationError(str(error), code="handoff_invalid") from error
    # Compare repeated IDs first so payload conflicts cannot be hidden by a
    # secondary checksum error on whichever manifest happens to sort first.
    for item in items.values():
        _validate_item(item)
    return items


def load_handoff_item(paths: RepoPaths, handoff_id: str) -> HandoffItem:
    if type(handoff_id) is not str or not _HANDOFF_ID.fullmatch(handoff_id):
        raise AgentRegistrationError("invalid handoff ID", code="handoff_invalid")
    item = _load_manifest_items(paths).get(handoff_id)
    if item is None:
        raise AgentRegistrationError(
            "no manifest contains this handoff ID", code="handoff_not_found"
        )
    return item


def validate_handoff_manifests(
    paths: RepoPaths, records: Mapping[str, SourceRecord]
) -> tuple[ValidationIssue, ...]:
    try:
        items = _load_manifest_items(paths)
        for item in items.values():
            record = records.get(item.source_id)
            if record is None or item.content_sha256 not in record.versions:
                raise AgentRegistrationError(
                    "handoff source/content version is not retained",
                    code="handoff_source_missing",
                )
        return ()
    except (OSError, ValueError) as error:
        return (
            ValidationIssue(
                "error",
                getattr(error, "code", "handoff_invalid"),
                str(error),
                PurePosixPath("sources/ledger/handoffs"),
            ),
        )


def handoff_for_job(
    job: Job, *, kind: HandoffKind, reason: str, diagnostics: tuple[Diagnostic, ...]
) -> HandoffItem:
    if not job.extractor.agent_fallback or job.extractor.agent_revision is None:
        raise AgentRegistrationError(
            "selected extractor has no agent revision", code="handoff_invalid"
        )
    item = HandoffItem(
        "",
        kind,
        job.record.source_id,
        job.context.input_sha256,
        job.item.fingerprint.path,
        job.item.media_type,
        reason,
        job.extractor.expected_anchors,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        job.context.prerequisite_digest,
        job.extractor.agent_revision,
        diagnostics,
    )
    return _identify(item)


def _identify(item: HandoffItem) -> HandoffItem:
    from dataclasses import replace

    fields = handoff_to_dict(item)
    fields.pop("handoff_id")
    item = replace(item, handoff_id=handoff_id_for(fields))
    _validate_item(item)
    return item


def collect_durable_handoffs(
    records: Mapping[str, SourceRecord], registry: ExtractorRegistry
) -> tuple[HandoffItem, ...]:
    items = []
    for record in records.values():
        if record.state is not SourceState.NEEDS_AGENT:
            continue
        raw_path = source_content_path(record)
        extractor = registry.select(record.media_type, raw_path)
        attempt = record.last_attempt
        if (
            extractor is None
            or not extractor.agent_fallback
            or extractor.agent_revision is None
            or attempt is None
            or attempt.outcome is not SourceState.NEEDS_AGENT
            or attempt.input_sha256 != record.active_content_sha256
            or attempt.extractor_id != extractor.extractor_id
            or attempt.config_sha256 != extractor.config_sha256
            or attempt.extractor_version
            != effective_extractor_version(extractor, attempt.prerequisite_digest)
        ):
            raise AgentRegistrationError(
                "durable needs-agent attempt has no matching recipe",
                code="handoff_recipe_stale",
            )
        diagnostics = tuple(
            d for d in record.diagnostics if d.code in attempt.diagnostic_codes
        )
        reason = diagnostics[0].code if diagnostics else "agent_required"
        items.append(
            _identify(
                HandoffItem(
                    "",
                    "rendered_web_capture"
                    if record.url_descriptor is not None
                    and record.media_type in {"text/html", "application/xhtml+xml"}
                    and "web_rendering_required" in attempt.diagnostic_codes
                    and any(d.code == "web_rendering_required" for d in diagnostics)
                    else "extraction",
                    record.source_id,
                    attempt.input_sha256,
                    raw_path,
                    record.media_type,
                    reason,
                    extractor.expected_anchors,
                    attempt.extractor_id,
                    attempt.extractor_version,
                    attempt.config_sha256,
                    attempt.prerequisite_digest,
                    extractor.agent_revision,
                    diagnostics,
                )
            )
        )
    return tuple(
        sorted(items, key=lambda item: (item.source_id, item.kind, item.handoff_id))
    )


def publish_handoff_data(
    paths: RepoPaths, *, items: Sequence[HandoffItem], now: datetime
) -> dict[str, JSONValue]:
    ordered = sorted(
        items, key=lambda item: (item.source_id, item.kind, item.handoff_id)
    )
    path = None
    if ordered:
        run_id = now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12]
        path = write_handoff_manifest(
            paths, run_id=run_id, created_at=now, items=ordered
        )
    return {
        "handoff_manifest": None
        if path is None
        else paths.repo_relative(path).as_posix(),
        "handoffs": [
            HandoffSummary(
                item.handoff_id,
                item.kind,
                item.source_id,
                item.content_sha256,
                item.reason,
            ).to_dict()
            for item in ordered
        ],
    }


def require_current_recipe(
    handoff: HandoffItem,
    record: SourceRecord,
    extractor: ExtractorSpec | None,
    digest: str,
) -> None:
    _validate_item(handoff)
    _require_source(handoff, record)
    if (
        extractor is None
        or not extractor.agent_fallback
        or extractor.extractor_id != handoff.extractor_id
        or extractor.config_sha256 != handoff.config_sha256
        or extractor.agent_revision != handoff.agent_revision
        or extractor.expected_anchors != handoff.required_anchor_kinds
        or digest != handoff.prerequisite_digest
        or effective_extractor_version(extractor, digest) != handoff.extractor_version
    ):
        raise AgentRegistrationError(
            "selected recipe or prerequisites changed", code="handoff_recipe_stale"
        )


def source_content_path(record: SourceRecord) -> PurePosixPath:
    """Keep URL control files distinct from their retained, claim-bearing input."""
    if record.url_descriptor is None:
        return record.current_raw_path
    version = record.versions.get(record.active_content_sha256)
    if (
        version is None
        or version.raw_path.parts[:3]
        != ("_web", record.source_id, record.active_content_sha256)
        or len(version.raw_path.parts) != 4
    ):
        raise AgentRegistrationError(
            "web handoff requires active retained web evidence",
            code="handoff_source_stale",
        )
    return version.raw_path


def _require_source(handoff: HandoffItem, record: SourceRecord) -> None:
    if handoff.kind != "extraction":
        raise AgentRegistrationError("not an extraction handoff")
    if (
        record.source_id != handoff.source_id
        or record.active_content_sha256 != handoff.content_sha256
        or source_content_path(record) != handoff.raw_path
        or record.media_type != handoff.media_type
        or handoff.content_sha256 not in record.versions
    ):
        raise AgentRegistrationError(
            "handoff no longer matches the current source", code="handoff_source_stale"
        )


@dataclass
class _StagedAgentFile:
    pinned: _PinnedDirectory
    parent_fd: int
    name: str
    body: bytes
    metadata: os.stat_result
    limit: int

    def revalidate(self) -> None:
        self.pinned.validate()
        body, metadata = _read_regular_at(
            self.parent_fd, self.name, label="agent staging", max_bytes=self.limit
        )
        if body != self.body or not _same_file_observation(metadata, self.metadata):
            raise AgentRegistrationError("agent staging changed during registration")
        self.pinned.validate()

    def remove(self) -> None:
        self.revalidate()
        os.unlink(self.name, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        self.pinned.validate()


@contextmanager
def staged_agent_file(
    paths: RepoPaths, handoff: HandoffItem, staging_path: Path
) -> Iterator[_StagedAgentFile]:
    """A bounded no-follow authority in the exact handoff staging directory."""
    scope = paths.root / ".brain/agent-staging" / handoff.handoff_id
    absolute = staging_path if staging_path.is_absolute() else paths.root / staging_path
    try:
        relative = absolute.relative_to(scope)
        if not relative.parts or any(part in {".", ".."} for part in absolute.parts):
            raise ValueError("not a file strictly below its handoff directory")
        extractor = ExtractorRegistry.load(paths.registry).select(
            handoff.media_type, handoff.raw_path
        )
        limit = min(
            MAX_AGENT_STAGING_BYTES,
            extractor.max_output_bytes
            if extractor is not None
            else MAX_AGENT_STAGING_BYTES,
        )
        with _PinnedDirectory.open(absolute.parent) as pinned:
            body, metadata = _read_regular_at(
                pinned.descriptor, absolute.name, label="agent staging", max_bytes=limit
            )
            staged = _StagedAgentFile(
                pinned, pinned.descriptor, absolute.name, body, metadata, limit
            )
            staged.revalidate()
            yield staged
    except AgentRegistrationError:
        raise
    except (OSError, ValueError) as error:
        raise AgentRegistrationError(
            f"agent staging scope/read failed: {error}"
        ) from error


def register_staged_agent_extraction(
    *,
    handoff: HandoffItem,
    record: SourceRecord,
    staging_path: Path,
    anchors: tuple[Anchor, ...],
    quality_state: Literal["ok", "warning"],
    note: str,
    paths: RepoPaths,
    now: datetime,
) -> ProcessResult:
    _validate_item(handoff)
    _require_source(handoff, record)
    try:
        _text(note, "note")
        if quality_state not in ("ok", "warning"):
            raise ValueError("quality must be ok or warning")
        for anchor in anchors:
            if not isinstance(anchor, Anchor) or anchor.kind not in _ANCHOR_KINDS:
                raise ValueError("invalid anchor kind")
            _text(anchor.value, "anchor value")
        if not set(handoff.required_anchor_kinds) <= {a.kind for a in anchors}:
            raise ValueError("missing required anchor kind")
        identifier = derivation_id(
            source_sha256=handoff.content_sha256,
            extractor_id=handoff.extractor_id,
            extractor_version=handoff.extractor_version,
            config_sha256=handoff.config_sha256,
        )
        metadata = {
            "handoff_id": handoff.handoff_id,
            "agent_revision": handoff.agent_revision,
            "note": note,
        }
        prior = record.derivations.get(identifier)
        if prior is None:
            attempt = record.last_attempt
            if (
                record.state is not SourceState.NEEDS_AGENT
                or attempt is None
                or attempt.outcome is not SourceState.NEEDS_AGENT
                or (
                    attempt.input_sha256,
                    attempt.extractor_id,
                    attempt.extractor_version,
                    attempt.config_sha256,
                    attempt.prerequisite_digest,
                )
                != (
                    handoff.content_sha256,
                    handoff.extractor_id,
                    handoff.extractor_version,
                    handoff.config_sha256,
                    handoff.prerequisite_digest,
                )
            ):
                raise ValueError("record has no matching NEEDS_AGENT attempt")
        elif (
            record.active_derivation_id != identifier
            or prior.source_sha256 != handoff.content_sha256
            or prior.extractor_id != handoff.extractor_id
            or prior.extractor_version != handoff.extractor_version
            or prior.config_sha256 != handoff.config_sha256
            or prior.method != "agent"
            or prior.method_metadata != metadata
            or prior.anchors != anchors
            or prior.quality_state != quality_state
            or record.state
            is not (SourceState.OK if quality_state == "ok" else SourceState.WARNING)
        ):
            raise ValueError("consumed handoff requires an exact active replay")
        destination = paths.extracted / derive_extraction_path(
            handoff.raw_path, handoff.content_sha256, identifier
        )
        with staged_agent_file(paths, handoff, staging_path) as staged:
            expected_kinds = tuple(dict.fromkeys(a.kind for a in anchors))
            if prior is not None:
                # Never reconstruct missing or altered retained output during replay.
                observation = stable_file_snapshot(
                    paths,
                    SnapshotNamespace.EXTRACTED,
                    prior.output_path,
                    include_sha256=True,
                )
                if (
                    prior.output_path != paths.repo_relative(destination)
                    or hashlib.sha256(staged.body).hexdigest() != prior.output_sha256
                    or len(staged.body) != prior.output_byte_size
                    or observation.sha256 != prior.output_sha256
                    or observation.byte_size != prior.output_byte_size
                    or observation.mtime_ns != prior.output_mtime_ns
                ):
                    raise AgentRegistrationError(
                        "retained or staged output differs",
                        code="output_path_collision",
                    )
                validate_markdown(
                    staged.body,
                    anchors,
                    expected_anchors=expected_kinds,
                    max_output_bytes=staged.limit,
                )
                staged.revalidate()
                return ProcessResult(record.state, prior, record.last_attempt, ())
            artifact = publish_markdown_artifact(
                staged.body,
                anchors,
                paths=paths,
                destination=destination,
                expected_anchors=expected_kinds,
                max_output_bytes=staged.limit,
            )
            staged.revalidate()
        derivation = Derivation(
            identifier,
            handoff.content_sha256,
            handoff.extractor_id,
            handoff.extractor_version,
            handoff.config_sha256,
            artifact.output_path,
            artifact.sha256,
            artifact.byte_size,
            artifact.mtime_ns,
            quality_state,
            anchors,
            now,
            method="agent",
            method_metadata=metadata,
        )
        state = SourceState.OK if quality_state == "ok" else SourceState.WARNING
        attempt = ProcessingAttempt(
            handoff.content_sha256,
            handoff.extractor_id,
            handoff.extractor_version,
            handoff.config_sha256,
            handoff.prerequisite_digest,
            state,
            now,
            (),
        )
        return ProcessResult(state, derivation, attempt, ())
    except AgentRegistrationError:
        raise
    except (ValueError, OSError, TypeError) as error:
        raise AgentRegistrationError(
            str(error), code=getattr(error, "code", "agent_registration_invalid")
        ) from error
