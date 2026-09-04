from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, Self, cast

from .diagnostics import Diagnostic, JSONValue, freeze_json_mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{64}$")
_DERIVATION_ID_RE = re.compile(r"^drv_[0-9a-f]{64}$")
_HANDOFF_ID_RE = re.compile(r"^hnd_[0-9a-f]{64}$")
_AGENT_REVISION_RE = re.compile(r"^[1-9][0-9]*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ANCHOR_KINDS = frozenset({"line", "page", "slide", "sheet", "section", "row", "block"})
_METHOD_METADATA_KEYS = {
    "deterministic": frozenset({"converter_id", "converter_version"}),
    "agent": frozenset({"handoff_id", "agent_revision", "note"}),
}


class SourceState(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    OK = "ok"
    WARNING = "warning"
    NEEDS_AGENT = "needs_agent"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    INTEGRITY_ERROR = "integrity_error"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass(frozen=True)
class FileFingerprint:
    path: PurePosixPath
    byte_size: int
    mtime_ns: int


@dataclass(frozen=True)
class Anchor:
    kind: Literal["line", "page", "slide", "sheet", "section", "row", "block"]
    value: str


@dataclass(frozen=True)
class RetrievalMetadata:
    requested_url: str
    final_url: str
    redirects: tuple[str, ...]
    retrieved_at: datetime
    detected_media_type: str
    byte_size: int
    sha256: str
    approval_event_id: str
    approval_recorded_at: datetime
    approval_scope: str
    approval_note: str


@dataclass(frozen=True)
class UrlDescriptorMetadata:
    url: str
    description: str
    added: date
    fingerprint: FileFingerprint


@dataclass(frozen=True)
class VersionAdoptionEvent:
    prior_sha256: str
    adopted_sha256: str
    approval_note: str
    recorded_at: datetime


@dataclass(frozen=True)
class ContentVersion:
    sha256: str
    raw_path: PurePosixPath
    byte_size: int
    fingerprint: FileFingerprint
    first_seen_at: datetime
    retrieval_events: tuple[RetrievalMetadata, ...]


@dataclass(frozen=True)
class Derivation:
    derivation_id: str
    source_sha256: str
    extractor_id: str
    extractor_version: str
    config_sha256: str
    output_path: PurePosixPath
    output_sha256: str
    output_byte_size: int
    output_mtime_ns: int
    quality_state: Literal["ok", "warning"]
    anchors: tuple[Anchor, ...]
    created_at: datetime
    method: Literal["deterministic", "agent"] = "deterministic"
    method_metadata: Mapping[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "method_metadata",
            freeze_json_mapping(self.method_metadata),
        )


@dataclass(frozen=True)
class SourceRepresentation:
    source_id: str
    content_sha256: str
    derivation_id: str
    raw_path: PurePosixPath
    extracted_path: PurePosixPath
    output_sha256: str
    quality_state: Literal["ok", "warning"]
    anchors: tuple[Anchor, ...]


@dataclass(frozen=True)
class ProcessingAttempt:
    input_sha256: str
    extractor_id: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    outcome: SourceState
    attempted_at: datetime
    diagnostic_codes: tuple[str, ...]


@dataclass(frozen=True)
class SourceRecord:
    schema_version: int
    source_id: str
    current_raw_path: PurePosixPath
    previous_raw_paths: tuple[PurePosixPath, ...]
    media_type: str
    byte_size: int
    state: SourceState
    versions: Mapping[str, ContentVersion]
    active_content_sha256: str | None
    derivations: Mapping[str, Derivation]
    active_derivation_id: str | None
    last_attempt: ProcessingAttempt | None
    diagnostics: tuple[Diagnostic, ...]
    created_at: datetime
    inspected_at: datetime
    updated_at: datetime
    adoption_events: tuple[VersionAdoptionEvent, ...] = ()
    url_descriptor: UrlDescriptorMetadata | None = None

    def __post_init__(self) -> None:
        if isinstance(self.versions, Mapping):
            object.__setattr__(
                self,
                "versions",
                MappingProxyType(dict(self.versions)),
            )
        if isinstance(self.derivations, Mapping):
            object.__setattr__(
                self,
                "derivations",
                MappingProxyType(dict(self.derivations)),
            )

    def to_dict(self) -> dict[str, JSONValue]:
        _validate_record(self)
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "current_raw_path": self.current_raw_path.as_posix(),
            "previous_raw_paths": [path.as_posix() for path in self.previous_raw_paths],
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "state": self.state.value,
            "versions": {
                checksum: _content_version_to_dict(version)
                for checksum, version in sorted(self.versions.items())
            },
            "active_content_sha256": self.active_content_sha256,
            "derivations": {
                identifier: _derivation_to_dict(derivation)
                for identifier, derivation in sorted(self.derivations.items())
            },
            "active_derivation_id": self.active_derivation_id,
            "last_attempt": (
                None
                if self.last_attempt is None
                else _attempt_to_dict(self.last_attempt)
            ),
            "diagnostics": [_diagnostic_to_dict(item) for item in self.diagnostics],
            "created_at": _datetime_to_json(self.created_at),
            "inspected_at": _datetime_to_json(self.inspected_at),
            "updated_at": _datetime_to_json(self.updated_at),
            "adoption_events": [
                _adoption_to_dict(item) for item in self.adoption_events
            ],
            "url_descriptor": (
                None
                if self.url_descriptor is None
                else _url_descriptor_to_dict(self.url_descriptor)
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, JSONValue]) -> Self:
        obj = _object(value, "source record")
        _exact_fields(
            obj,
            {
                "schema_version",
                "source_id",
                "current_raw_path",
                "previous_raw_paths",
                "media_type",
                "byte_size",
                "state",
                "versions",
                "active_content_sha256",
                "derivations",
                "active_derivation_id",
                "last_attempt",
                "diagnostics",
                "created_at",
                "inspected_at",
                "updated_at",
                "adoption_events",
                "url_descriptor",
            },
            "source record",
        )
        versions_obj = _object(obj["versions"], "versions")
        derivations_obj = _object(obj["derivations"], "derivations")
        record = cls(
            _integer(obj["schema_version"], "schema_version"),
            _string(obj["source_id"], "source_id"),
            _parse_raw_path(obj["current_raw_path"], "current_raw_path"),
            tuple(
                _parse_raw_path(item, "previous_raw_paths")
                for item in _array(obj["previous_raw_paths"], "previous_raw_paths")
            ),
            _string(obj["media_type"], "media_type"),
            _integer(obj["byte_size"], "byte_size"),
            _source_state(obj["state"]),
            {key: _parse_content_version(item) for key, item in versions_obj.items()},
            _optional_string(obj["active_content_sha256"], "active_content_sha256"),
            {key: _parse_derivation(item) for key, item in derivations_obj.items()},
            _optional_string(obj["active_derivation_id"], "active_derivation_id"),
            None
            if obj["last_attempt"] is None
            else _parse_attempt(obj["last_attempt"]),
            tuple(
                _parse_diagnostic(item)
                for item in _array(obj["diagnostics"], "diagnostics")
            ),
            _parse_datetime(obj["created_at"], "created_at"),
            _parse_datetime(obj["inspected_at"], "inspected_at"),
            _parse_datetime(obj["updated_at"], "updated_at"),
            tuple(
                _parse_adoption(item)
                for item in _array(obj["adoption_events"], "adoption_events")
            ),
            (
                None
                if obj["url_descriptor"] is None
                else _parse_url_descriptor(obj["url_descriptor"])
            ),
        )
        _validate_record(record)
        return record


def source_id_for_first_seen(raw_path: PurePosixPath, content_sha256: str) -> str:
    _validate_raw_path(raw_path, "raw_path")
    _validate_sha256(content_sha256, "content_sha256")
    digest = hashlib.sha256(
        b"source-v1\0"
        + raw_path.as_posix().encode("utf-8")
        + b"\0"
        + content_sha256.encode("ascii")
    ).hexdigest()
    return f"src_{digest}"


def compute_sha256(path: Path, *, chunk_size: int = 1_048_576) -> str:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derivation_id(
    *, source_sha256: str, extractor_id: str, extractor_version: str, config_sha256: str
) -> str:
    _validate_sha256(source_sha256, "source_sha256")
    _validate_sha256(config_sha256, "config_sha256")
    fields = (source_sha256, extractor_id, extractor_version, config_sha256)
    if any(type(item) is not str or not item or "\0" in item for item in fields):
        raise ValueError("derivation identity fields must be nonempty NUL-free strings")
    digest = hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()
    return f"drv_{digest}"


def compute_corpus_revision(records: Iterable[SourceRecord]) -> str:
    rows = sorted(
        f"{record.source_id}\0{record.active_content_sha256 or ''}\0{record.active_derivation_id or ''}"
        for record in records
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _validate_record(record: SourceRecord) -> None:
    if type(record.schema_version) is not int or record.schema_version != 1:
        raise ValueError("schema_version must be 1")
    if type(record.source_id) is not str or not _SOURCE_ID_RE.fullmatch(
        record.source_id
    ):
        raise ValueError(
            "source_id must be src_ followed by 64 lower-case hexadecimal characters"
        )
    _validate_raw_path(record.current_raw_path, "current_raw_path")
    if type(record.previous_raw_paths) is not tuple:
        raise ValueError("previous_raw_paths must be immutable")
    for path in record.previous_raw_paths:
        _validate_raw_path(path, "previous_raw_paths")
    if len(set(record.previous_raw_paths)) != len(record.previous_raw_paths):
        raise ValueError("previous_raw_paths must be unique")
    _validate_nonempty(record.media_type, "media_type")
    _validate_nonnegative(record.byte_size, "byte_size")
    if not isinstance(record.state, SourceState):
        raise ValueError("state must be a SourceState")
    if not isinstance(record.versions, Mapping) or not isinstance(
        record.derivations, Mapping
    ):
        raise ValueError("versions and derivations must be mappings")

    if not record.versions:
        if not (
            record.url_descriptor is not None
            and record.state is SourceState.AWAITING_APPROVAL
            and record.active_content_sha256 is None
            and record.active_derivation_id is None
            and not record.derivations
        ):
            raise ValueError(
                "zero versions are allowed only for an inactive URL descriptor awaiting approval"
            )

    for checksum, version in record.versions.items():
        _validate_sha256(checksum, "versions key")
        _validate_content_version(version)
        if checksum != version.sha256:
            raise ValueError("versions key must match content version sha256")
    for identifier, derivation in record.derivations.items():
        _validate_derivation(derivation)
        if identifier != derivation.derivation_id:
            raise ValueError("derivations key must match derivation_id")

    if record.active_content_sha256 is not None:
        _validate_sha256(record.active_content_sha256, "active_content_sha256")
        if record.active_content_sha256 not in record.versions:
            raise ValueError("active_content_sha256 must name an existing version")
    elif record.active_derivation_id is not None:
        raise ValueError("active derivation requires active content")
    elif record.versions and record.url_descriptor is None:
        raise ValueError("file-backed records require an active_content_sha256")

    if record.active_derivation_id is not None:
        if not _DERIVATION_ID_RE.fullmatch(record.active_derivation_id):
            raise ValueError("active_derivation_id must be a canonical derivation_id")
        active = record.derivations.get(record.active_derivation_id)
        if active is None:
            raise ValueError("active_derivation_id must name an existing derivation")
        if active.source_sha256 != record.active_content_sha256:
            raise ValueError(
                "active derivation must reference the active content checksum"
            )
    for derivation in record.derivations.values():
        if derivation.source_sha256 not in record.versions:
            raise ValueError("derivation source_sha256 must name an existing version")

    if record.last_attempt is not None:
        _validate_attempt(record.last_attempt)
    if type(record.diagnostics) is not tuple:
        raise ValueError("diagnostics must be immutable")
    for diagnostic in record.diagnostics:
        _validate_diagnostic(diagnostic)
    _validate_datetime(record.created_at, "created_at")
    _validate_datetime(record.inspected_at, "inspected_at")
    _validate_datetime(record.updated_at, "updated_at")
    if type(record.adoption_events) is not tuple:
        raise ValueError("adoption_events must be immutable")
    for event in record.adoption_events:
        _validate_adoption(event)
    if record.url_descriptor is not None:
        _validate_url_descriptor(record.url_descriptor)
        if record.url_descriptor.fingerprint.path != record.current_raw_path:
            raise ValueError("URL descriptor fingerprint must match current_raw_path")
    else:
        _validate_file_history(record)


def _validate_file_history(record: SourceRecord) -> None:
    """Validate the permanent file identity root and its complete linear chain."""

    events = record.adoption_events
    if len(record.versions) != len(events) + 1:
        raise ValueError(
            "file versions and adoption events must form one complete ordered chain"
        )
    root_sha256 = events[0].prior_sha256 if events else next(iter(record.versions), "")
    if root_sha256 not in record.versions:
        raise ValueError("file adoption ordered chain root must be retained")
    expected_prior = root_sha256
    seen = {root_sha256}
    prior_time = record.versions[root_sha256].first_seen_at
    for event in events:
        if (
            event.prior_sha256 != expected_prior
            or event.adopted_sha256 in seen
            or event.adopted_sha256 not in record.versions
            or event.prior_sha256 not in record.versions
            or event.recorded_at < prior_time
        ):
            raise ValueError("file adoption events must form one ordered chain")
        adopted = record.versions[event.adopted_sha256]
        if adopted.first_seen_at != event.recorded_at:
            raise ValueError(
                "file adoption ordered chain timestamps must match version history"
            )
        expected_prior = event.adopted_sha256
        prior_time = event.recorded_at
        seen.add(event.adopted_sha256)
    if seen != set(record.versions) or record.active_content_sha256 != expected_prior:
        raise ValueError(
            "file versions and adoption events must form one complete ordered chain"
        )
    creation_path = (
        record.previous_raw_paths[0]
        if record.previous_raw_paths
        else record.current_raw_path
    )
    if source_id_for_first_seen(creation_path, root_sha256) != record.source_id:
        raise ValueError(
            "file source_id must bind the creation identity and adoption root"
        )


def _validate_content_version(version: ContentVersion) -> None:
    if not isinstance(version, ContentVersion):
        raise ValueError("versions values must be ContentVersion objects")
    _validate_sha256(version.sha256, "content version sha256")
    _validate_raw_path(version.raw_path, "content version raw_path")
    _validate_nonnegative(version.byte_size, "content version byte_size")
    _validate_fingerprint(version.fingerprint)
    if (
        version.fingerprint.path != version.raw_path
        or version.fingerprint.byte_size != version.byte_size
    ):
        raise ValueError(
            "content version fingerprint must describe its materialized bytes"
        )
    _validate_datetime(version.first_seen_at, "first_seen_at")
    if type(version.retrieval_events) is not tuple:
        raise ValueError("retrieval_events must be immutable")
    for event in version.retrieval_events:
        _validate_retrieval(event)
        if event.sha256 != version.sha256 or event.byte_size != version.byte_size:
            raise ValueError(
                "retrieval event must match its owning content version bytes"
            )


def _validate_fingerprint(fingerprint: FileFingerprint) -> None:
    if not isinstance(fingerprint, FileFingerprint):
        raise ValueError("fingerprint must be a FileFingerprint")
    _validate_raw_path(fingerprint.path, "fingerprint path")
    _validate_nonnegative(fingerprint.byte_size, "fingerprint byte_size")
    _validate_nonnegative(fingerprint.mtime_ns, "fingerprint mtime_ns")


def _validate_retrieval(event: RetrievalMetadata) -> None:
    if not isinstance(event, RetrievalMetadata):
        raise ValueError("retrieval_events values must be RetrievalMetadata objects")
    for name in (
        "requested_url",
        "final_url",
        "detected_media_type",
        "approval_event_id",
        "approval_scope",
        "approval_note",
    ):
        _validate_nonempty(getattr(event, name), name)
    if type(event.redirects) is not tuple:
        raise ValueError("redirects must be immutable")
    for redirect in event.redirects:
        _validate_nonempty(redirect, "redirect")
    _validate_datetime(event.retrieved_at, "retrieved_at")
    _validate_datetime(event.approval_recorded_at, "approval_recorded_at")
    _validate_nonnegative(event.byte_size, "retrieval byte_size")
    _validate_sha256(event.sha256, "retrieval sha256")


def _validate_url_descriptor(descriptor: UrlDescriptorMetadata) -> None:
    if not isinstance(descriptor, UrlDescriptorMetadata):
        raise ValueError("url_descriptor must be UrlDescriptorMetadata")
    _validate_nonempty(descriptor.url, "url_descriptor url")
    _validate_nonempty(descriptor.description, "url_descriptor description")
    if type(descriptor.added) is not date or isinstance(descriptor.added, datetime):
        raise ValueError("url_descriptor added must be a date")
    _validate_fingerprint(descriptor.fingerprint)


def _validate_adoption(event: VersionAdoptionEvent) -> None:
    if not isinstance(event, VersionAdoptionEvent):
        raise ValueError("adoption_events values must be VersionAdoptionEvent objects")
    _validate_sha256(event.prior_sha256, "prior_sha256")
    _validate_sha256(event.adopted_sha256, "adopted_sha256")
    _validate_nonempty(event.approval_note, "approval_note")
    _validate_datetime(event.recorded_at, "recorded_at")


def _validate_derivation(derivation: Derivation) -> None:
    if not isinstance(derivation, Derivation):
        raise ValueError("derivations values must be Derivation objects")
    if type(derivation.derivation_id) is not str or not _DERIVATION_ID_RE.fullmatch(
        derivation.derivation_id
    ):
        raise ValueError(
            "derivation_id must be drv_ followed by 64 lower-case hexadecimal characters"
        )
    _validate_sha256(derivation.source_sha256, "source_sha256")
    _validate_sha256(derivation.config_sha256, "config_sha256")
    _validate_sha256(derivation.output_sha256, "output_sha256")
    _validate_nonempty(derivation.extractor_id, "extractor_id")
    _validate_nonempty(derivation.extractor_version, "extractor_version")
    _validate_output_path(derivation.output_path)
    _validate_nonnegative(derivation.output_byte_size, "output_byte_size")
    _validate_nonnegative(derivation.output_mtime_ns, "output_mtime_ns")
    if derivation.quality_state not in {"ok", "warning"}:
        raise ValueError("quality_state must be ok or warning")
    if type(derivation.anchors) is not tuple:
        raise ValueError("anchors must be immutable")
    for anchor in derivation.anchors:
        if not isinstance(anchor, Anchor) or anchor.kind not in _ANCHOR_KINDS:
            raise ValueError("anchor kind is invalid")
        _validate_nonempty(anchor.value, "anchor value")
    _validate_datetime(derivation.created_at, "derivation created_at")
    _validate_method_metadata(derivation.method, derivation.method_metadata)


def _validate_method_metadata(method: object, metadata: object) -> None:
    if method not in _METHOD_METADATA_KEYS or not isinstance(metadata, Mapping):
        raise ValueError("method_metadata must match a supported derivation method")
    expected = _METHOD_METADATA_KEYS[cast(str, method)]
    if set(metadata) != expected or any(
        type(value) is not str or not value.strip() for value in metadata.values()
    ):
        raise ValueError(
            f"method_metadata for {method} must contain exactly {sorted(expected)} as nonempty strings"
        )
    if method == "agent":
        handoff_id = metadata["handoff_id"]
        agent_revision = metadata["agent_revision"]
        note = metadata["note"]
        if (
            type(handoff_id) is not str
            or _HANDOFF_ID_RE.fullmatch(handoff_id) is None
            or type(agent_revision) is not str
            or _AGENT_REVISION_RE.fullmatch(agent_revision) is None
            or type(note) is not str
            or note != note.strip()
            or not note
            or "\0" in note
        ):
            raise ValueError(
                "method_metadata for agent requires a permanent handoff_id, "
                "positive decimal agent_revision, and trimmed NUL-free note"
            )


def _validate_attempt(attempt: ProcessingAttempt) -> None:
    if not isinstance(attempt, ProcessingAttempt):
        raise ValueError("last_attempt must be a ProcessingAttempt")
    _validate_sha256(attempt.input_sha256, "attempt input_sha256")
    _validate_sha256(attempt.config_sha256, "attempt config_sha256")
    _validate_sha256(attempt.prerequisite_digest, "attempt prerequisite_digest")
    _validate_nonempty(attempt.extractor_id, "attempt extractor_id")
    _validate_nonempty(attempt.extractor_version, "attempt extractor_version")
    if not isinstance(attempt.outcome, SourceState):
        raise ValueError("attempt outcome must be a SourceState")
    _validate_datetime(attempt.attempted_at, "attempted_at")
    if type(attempt.diagnostic_codes) is not tuple:
        raise ValueError("diagnostic_codes must be immutable")
    for code in attempt.diagnostic_codes:
        _validate_nonempty(code, "diagnostic code")


def _validate_diagnostic(diagnostic: Diagnostic) -> None:
    if not isinstance(diagnostic, Diagnostic):
        raise ValueError("diagnostics values must be Diagnostic objects")
    _validate_nonempty(diagnostic.code, "diagnostic code")
    _validate_nonempty(diagnostic.message, "diagnostic message")
    if diagnostic.path is not None:
        _validate_relative_path(diagnostic.path, "diagnostic path")
    _json_copy(diagnostic.details)


def _validate_raw_path(path: object, name: str) -> None:
    _validate_relative_path(path, name)
    assert isinstance(path, PurePosixPath)
    if path.parts[:2] == ("sources", "raw"):
        raise ValueError(
            f"{name} must be a canonical repository-relative raw path without sources/raw"
        )


def _validate_output_path(path: object) -> None:
    _validate_relative_path(path, "output_path")
    assert isinstance(path, PurePosixPath)
    if path.parts[:2] != ("sources", "extracted") or len(path.parts) < 3:
        raise ValueError(
            "output_path must be a canonical repository-relative path under sources/extracted"
        )


def _validate_relative_path(path: object, name: str) -> None:
    if not isinstance(path, PurePosixPath) or path.is_absolute():
        raise ValueError(f"{name} must be a repository-relative POSIX path")
    text = path.as_posix()
    if (
        not text
        or text == "."
        or _WINDOWS_DRIVE_RE.match(text)
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in text
        or "\0" in text
    ):
        raise ValueError(f"{name} must be a canonical repository-relative POSIX path")


def _parse_raw_path(value: object, name: str) -> PurePosixPath:
    path = _parse_relative_path(value, name)
    _validate_raw_path(path, name)
    return path


def _parse_relative_path(value: object, name: str) -> PurePosixPath:
    text = _string(value, name)
    if text.startswith("/"):
        raise ValueError(f"{name} must be repository-relative")
    path = PurePosixPath(text)
    if path.as_posix() != text:
        raise ValueError(f"{name} must be a canonical repository-relative POSIX path")
    _validate_relative_path(path, name)
    return path


def _parse_output_path(value: object) -> PurePosixPath:
    text = _string(value, "output_path")
    path = PurePosixPath(text)
    if path.as_posix() != text:
        raise ValueError(
            "output_path must be a canonical repository-relative POSIX path"
        )
    _validate_output_path(path)
    return path


def _validate_sha256(value: object, name: str) -> None:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be 64 lower-case hexadecimal characters")


def _validate_nonempty(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _validate_nonnegative(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _validate_datetime(value: object, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be an aware datetime")


def _datetime_to_json(value: datetime) -> str:
    _validate_datetime(value, "timestamp")
    utc = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_datetime(value: object, name: str) -> datetime:
    text = _string(value, name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from error
    if _datetime_to_json(parsed) != text:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return parsed


def _date_to_json(value: date) -> str:
    if type(value) is not date:
        raise ValueError("date value must be a date")
    return value.isoformat()


def _parse_date(value: object, name: str) -> date:
    text = _string(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical date") from error
    if parsed.isoformat() != text:
        raise ValueError(f"{name} must be a canonical date")
    return parsed


def _json_copy(value: object) -> JSONValue:
    if value is None or type(value) in {str, bool, int}:
        return cast(JSONValue, value)
    if type(value) is float and math.isfinite(cast(float, value)):
        return cast(float, value)
    if isinstance(value, list):
        return [_json_copy(item) for item in cast(list[object], value)]
    if isinstance(value, Mapping) and all(type(key) is str for key in value):
        return {cast(str, key): _json_copy(item) for key, item in sorted(value.items())}
    raise ValueError("value is not canonical JSON data")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{name} must be a JSON object")
    return cast(Mapping[str, object], value)


def _array(value: object, name: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{name} must be a JSON array")
    return cast(list[object], value)


def _string(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    return cast(str, value)


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return cast(int, value)


def _exact_fields(value: Mapping[str, object], expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ValueError(
            f"{name} has missing or unexpected fields: missing={missing}, unexpected={extra}"
        )


def _source_state(value: object) -> SourceState:
    try:
        return SourceState(_string(value, "state"))
    except ValueError as error:
        raise ValueError("state is not a recognized source state") from error


def _fingerprint_to_dict(value: FileFingerprint) -> dict[str, JSONValue]:
    return {
        "path": value.path.as_posix(),
        "byte_size": value.byte_size,
        "mtime_ns": value.mtime_ns,
    }


def _parse_fingerprint(value: object) -> FileFingerprint:
    obj = _object(value, "fingerprint")
    _exact_fields(obj, {"path", "byte_size", "mtime_ns"}, "fingerprint")
    return FileFingerprint(
        _parse_raw_path(obj["path"], "fingerprint path"),
        _integer(obj["byte_size"], "fingerprint byte_size"),
        _integer(obj["mtime_ns"], "fingerprint mtime_ns"),
    )


def _retrieval_to_dict(value: RetrievalMetadata) -> dict[str, JSONValue]:
    return {
        "requested_url": value.requested_url,
        "final_url": value.final_url,
        "redirects": list(value.redirects),
        "retrieved_at": _datetime_to_json(value.retrieved_at),
        "detected_media_type": value.detected_media_type,
        "byte_size": value.byte_size,
        "sha256": value.sha256,
        "approval_event_id": value.approval_event_id,
        "approval_recorded_at": _datetime_to_json(value.approval_recorded_at),
        "approval_scope": value.approval_scope,
        "approval_note": value.approval_note,
    }


def _parse_retrieval(value: object) -> RetrievalMetadata:
    obj = _object(value, "retrieval event")
    fields = {
        "requested_url",
        "final_url",
        "redirects",
        "retrieved_at",
        "detected_media_type",
        "byte_size",
        "sha256",
        "approval_event_id",
        "approval_recorded_at",
        "approval_scope",
        "approval_note",
    }
    _exact_fields(obj, fields, "retrieval event")
    return RetrievalMetadata(
        _string(obj["requested_url"], "requested_url"),
        _string(obj["final_url"], "final_url"),
        tuple(
            _string(item, "redirect") for item in _array(obj["redirects"], "redirects")
        ),
        _parse_datetime(obj["retrieved_at"], "retrieved_at"),
        _string(obj["detected_media_type"], "detected_media_type"),
        _integer(obj["byte_size"], "retrieval byte_size"),
        _string(obj["sha256"], "retrieval sha256"),
        _string(obj["approval_event_id"], "approval_event_id"),
        _parse_datetime(obj["approval_recorded_at"], "approval_recorded_at"),
        _string(obj["approval_scope"], "approval_scope"),
        _string(obj["approval_note"], "approval_note"),
    )


def _content_version_to_dict(value: ContentVersion) -> dict[str, JSONValue]:
    return {
        "sha256": value.sha256,
        "raw_path": value.raw_path.as_posix(),
        "byte_size": value.byte_size,
        "fingerprint": _fingerprint_to_dict(value.fingerprint),
        "first_seen_at": _datetime_to_json(value.first_seen_at),
        "retrieval_events": [
            _retrieval_to_dict(item) for item in value.retrieval_events
        ],
    }


def _parse_content_version(value: object) -> ContentVersion:
    obj = _object(value, "content version")
    _exact_fields(
        obj,
        {
            "sha256",
            "raw_path",
            "byte_size",
            "fingerprint",
            "first_seen_at",
            "retrieval_events",
        },
        "content version",
    )
    return ContentVersion(
        _string(obj["sha256"], "content version sha256"),
        _parse_raw_path(obj["raw_path"], "content version raw_path"),
        _integer(obj["byte_size"], "content version byte_size"),
        _parse_fingerprint(obj["fingerprint"]),
        _parse_datetime(obj["first_seen_at"], "first_seen_at"),
        tuple(
            _parse_retrieval(item)
            for item in _array(obj["retrieval_events"], "retrieval_events")
        ),
    )


def _anchor_to_dict(value: Anchor) -> dict[str, JSONValue]:
    return {"kind": value.kind, "value": value.value}


def _parse_anchor(value: object) -> Anchor:
    obj = _object(value, "anchor")
    _exact_fields(obj, {"kind", "value"}, "anchor")
    return Anchor(
        cast(object, _string(obj["kind"], "anchor kind")),
        _string(obj["value"], "anchor value"),
    )  # type: ignore[arg-type]


def _derivation_to_dict(value: Derivation) -> dict[str, JSONValue]:
    return {
        "derivation_id": value.derivation_id,
        "source_sha256": value.source_sha256,
        "extractor_id": value.extractor_id,
        "extractor_version": value.extractor_version,
        "config_sha256": value.config_sha256,
        "output_path": value.output_path.as_posix(),
        "output_sha256": value.output_sha256,
        "output_byte_size": value.output_byte_size,
        "output_mtime_ns": value.output_mtime_ns,
        "quality_state": value.quality_state,
        "anchors": [_anchor_to_dict(item) for item in value.anchors],
        "created_at": _datetime_to_json(value.created_at),
        "method": value.method,
        "method_metadata": cast(
            dict[str, JSONValue], _json_copy(value.method_metadata)
        ),
    }


def _parse_derivation(value: object) -> Derivation:
    obj = _object(value, "derivation")
    fields = {
        "derivation_id",
        "source_sha256",
        "extractor_id",
        "extractor_version",
        "config_sha256",
        "output_path",
        "output_sha256",
        "output_byte_size",
        "output_mtime_ns",
        "quality_state",
        "anchors",
        "created_at",
        "method",
        "method_metadata",
    }
    _exact_fields(obj, fields, "derivation")
    return Derivation(
        _string(obj["derivation_id"], "derivation_id"),
        _string(obj["source_sha256"], "source_sha256"),
        _string(obj["extractor_id"], "extractor_id"),
        _string(obj["extractor_version"], "extractor_version"),
        _string(obj["config_sha256"], "config_sha256"),
        _parse_output_path(obj["output_path"]),
        _string(obj["output_sha256"], "output_sha256"),
        _integer(obj["output_byte_size"], "output_byte_size"),
        _integer(obj["output_mtime_ns"], "output_mtime_ns"),
        cast(object, _string(obj["quality_state"], "quality_state")),  # type: ignore[arg-type]
        tuple(_parse_anchor(item) for item in _array(obj["anchors"], "anchors")),
        _parse_datetime(obj["created_at"], "derivation created_at"),
        cast(object, _string(obj["method"], "method")),  # type: ignore[arg-type]
        cast(
            Mapping[str, JSONValue], _object(obj["method_metadata"], "method_metadata")
        ),
    )


def _attempt_to_dict(value: ProcessingAttempt) -> dict[str, JSONValue]:
    return {
        "input_sha256": value.input_sha256,
        "extractor_id": value.extractor_id,
        "extractor_version": value.extractor_version,
        "config_sha256": value.config_sha256,
        "prerequisite_digest": value.prerequisite_digest,
        "outcome": value.outcome.value,
        "attempted_at": _datetime_to_json(value.attempted_at),
        "diagnostic_codes": list(value.diagnostic_codes),
    }


def _parse_attempt(value: object) -> ProcessingAttempt:
    obj = _object(value, "processing attempt")
    fields = {
        "input_sha256",
        "extractor_id",
        "extractor_version",
        "config_sha256",
        "prerequisite_digest",
        "outcome",
        "attempted_at",
        "diagnostic_codes",
    }
    _exact_fields(obj, fields, "processing attempt")
    return ProcessingAttempt(
        _string(obj["input_sha256"], "input_sha256"),
        _string(obj["extractor_id"], "extractor_id"),
        _string(obj["extractor_version"], "extractor_version"),
        _string(obj["config_sha256"], "config_sha256"),
        _string(obj["prerequisite_digest"], "prerequisite_digest"),
        _source_state(obj["outcome"]),
        _parse_datetime(obj["attempted_at"], "attempted_at"),
        tuple(
            _string(item, "diagnostic code")
            for item in _array(obj["diagnostic_codes"], "diagnostic_codes")
        ),
    )


def _diagnostic_to_dict(value: Diagnostic) -> dict[str, JSONValue]:
    return {
        "code": value.code,
        "message": value.message,
        "path": None if value.path is None else value.path.as_posix(),
        "details": cast(dict[str, JSONValue], _json_copy(value.details)),
    }


def _parse_diagnostic(value: object) -> Diagnostic:
    obj = _object(value, "diagnostic")
    _exact_fields(obj, {"code", "message", "path", "details"}, "diagnostic")
    path = (
        None
        if obj["path"] is None
        else _parse_relative_path(obj["path"], "diagnostic path")
    )
    return Diagnostic(
        _string(obj["code"], "diagnostic code"),
        _string(obj["message"], "diagnostic message"),
        path,
        cast(Mapping[str, JSONValue], _object(obj["details"], "diagnostic details")),
    )


def _adoption_to_dict(value: VersionAdoptionEvent) -> dict[str, JSONValue]:
    return {
        "prior_sha256": value.prior_sha256,
        "adopted_sha256": value.adopted_sha256,
        "approval_note": value.approval_note,
        "recorded_at": _datetime_to_json(value.recorded_at),
    }


def _parse_adoption(value: object) -> VersionAdoptionEvent:
    obj = _object(value, "version adoption event")
    _exact_fields(
        obj,
        {"prior_sha256", "adopted_sha256", "approval_note", "recorded_at"},
        "version adoption event",
    )
    return VersionAdoptionEvent(
        _string(obj["prior_sha256"], "prior_sha256"),
        _string(obj["adopted_sha256"], "adopted_sha256"),
        _string(obj["approval_note"], "approval_note"),
        _parse_datetime(obj["recorded_at"], "recorded_at"),
    )


def _url_descriptor_to_dict(value: UrlDescriptorMetadata) -> dict[str, JSONValue]:
    return {
        "url": value.url,
        "description": value.description,
        "added": _date_to_json(value.added),
        "fingerprint": _fingerprint_to_dict(value.fingerprint),
    }


def _parse_url_descriptor(value: object) -> UrlDescriptorMetadata:
    obj = _object(value, "url_descriptor")
    _exact_fields(obj, {"url", "description", "added", "fingerprint"}, "url_descriptor")
    return UrlDescriptorMetadata(
        _string(obj["url"], "url_descriptor url"),
        _string(obj["description"], "url_descriptor description"),
        _parse_date(obj["added"], "url_descriptor added"),
        _parse_fingerprint(obj["fingerprint"]),
    )
