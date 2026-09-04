from __future__ import annotations

import errno
import hashlib
import html
import inspect
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Protocol
from urllib.parse import quote

from .contracts import (
    ContentVersion,
    Derivation,
    FileFingerprint,
    SourceRecord,
    SourceRepresentation,
    SourceState,
    VersionAdoptionEvent,
    compute_corpus_revision,
)
from .diagnostics import Diagnostic, JSONValue
from .inventory import InventoryItem
from .layout import RepoPaths
from .registry import (
    ExtractorSpec,
    _bounded_wait_or_reap_later,
    _create_probe_tree,
    effective_extractor_version,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{64}$")
_DERIVATION_ID_RE = re.compile(r"^drv_[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_STATE_ORDER = tuple(SourceState)
_DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_GIT_HISTORY_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_GIT_BLOB_BYTES = 1024 * 1024 * 1024
_MAX_LEDGER_SUMMARY_BYTES = 16 * 1024 * 1024
_MAX_LEDGER_SUMMARY_GAP_BYTES = 4 * 1024 * 1024
_MAX_LEDGER_SUMMARY_SOURCE_BYTES = 8 * 1024 * 1024
_ACTIVATION_GUARD_SUFFIX = ".activation-pending"
_PROCESS_POLL_SECONDS = 0.01
_GIT_CLEANUP_RESERVE_SECONDS = 0.05
_OPEN_SUPPORT_MARKER = os.open
_REPLACE_HAS_DIR_FD = {"src_dir_fd", "dst_dir_fd"} <= set(
    inspect.signature(os.replace).parameters
)
_SAFE_PLATFORM_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and os.O_NOFOLLOW != 0
    and hasattr(os, "O_DIRECTORY")
    and os.O_DIRECTORY != 0
    and hasattr(os, "O_NONBLOCK")
    and os.O_NONBLOCK != 0
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.mkdir, os.unlink, os.link)
    )
    and os.listdir in os.supports_fd
    and os.stat in os.supports_follow_symlinks
    and os.link in os.supports_follow_symlinks
    and _REPLACE_HAS_DIR_FD
)
_RESOLVED_INTEGRITY_DIAGNOSTICS = frozenset(
    {
        "raw_checksum_mismatch",
        "raw_fingerprint_mismatch",
        "raw_size_mismatch",
        "raw_replaced",
        "source_integrity_error",
    }
)
_STALE_EXTRACTION_RECOVERY_DIAGNOSTIC = Diagnostic(
    "stale_extracting_recovered",
    "Recovered an interrupted extraction; source is pending retry.",
)


def _source_has_coverage_gap(record: SourceRecord) -> bool:
    """Return whether a structurally valid record lacks complete source coverage."""

    return record.state is not SourceState.OK


class UnsafeFilesystemError(OSError):
    pass


class PublishedWriteError(OSError):
    pass


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _DirectoryEdge:
    parent_fd: int
    child_fd: int
    name: str
    parent_identity: _DirectoryIdentity
    child_identity: _DirectoryIdentity


@dataclass(frozen=True)
class _FileSnapshot:
    data: bytes
    metadata: os.stat_result
    anchors: tuple[_DirectoryIdentity, ...]


class _PinnedDirectory:
    def __init__(self, descriptors: list[int], edges: list[_DirectoryEdge]) -> None:
        self._descriptors = descriptors
        self._edges = edges

    @classmethod
    def open(cls, path: Path) -> _PinnedDirectory:
        _require_safe_filesystem()
        if not path.is_absolute() or any(
            part in {"", ".", ".."} for part in path.parts[1:]
        ):
            raise ValueError("repository paths must be canonical absolute paths")
        descriptors: list[int] = []
        edges: list[_DirectoryEdge] = []
        try:
            root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(root_fd)
            parent_fd = root_fd
            for component in path.parts[1:]:
                child_fd, edge = _open_directory_edge(parent_fd, component)
                descriptors.append(child_fd)
                edges.append(edge)
                parent_fd = child_fd
            pinned = cls(descriptors, edges)
            pinned.validate()
            return pinned
        except BaseException:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    @property
    def descriptor(self) -> int:
        return self._descriptors[-1]

    @property
    def identities(self) -> tuple[_DirectoryIdentity, ...]:
        return tuple(edge.child_identity for edge in self._edges)

    def open_child(self, parent_fd: int, name: str) -> int:
        child_fd, edge = _open_directory_edge(parent_fd, name)
        self._descriptors.append(child_fd)
        self._edges.append(edge)
        return child_fd

    def validate(self) -> None:
        for edge in self._edges:
            try:
                parent = os.fstat(edge.parent_fd)
                named = os.stat(edge.name, dir_fd=edge.parent_fd, follow_symlinks=False)
                child = os.fstat(edge.child_fd)
            except OSError as error:
                raise UnsafeFilesystemError(
                    "directory anchor changed during filesystem operation"
                ) from error
            if (
                _directory_identity(parent) != edge.parent_identity
                or _directory_identity(named) != edge.child_identity
                or _directory_identity(child) != edge.child_identity
                or not stat.S_ISDIR(named.st_mode)
            ):
                raise UnsafeFilesystemError(
                    "directory anchor changed during filesystem operation"
                )

    def close(self) -> None:
        descriptors, self._descriptors = self._descriptors, []
        self._edges = []
        first_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self) -> _PinnedDirectory:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _open_directory_edge(parent_fd: int, name: str) -> tuple[int, _DirectoryEdge]:
    if not name or "/" in name or "\0" in name:
        raise ValueError("directory name must be one canonical path component")
    try:
        parent = os.fstat(parent_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode):
            raise ValueError(f"{name} must be a real directory")
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{name} must be a real directory") from error
        raise
    try:
        child = os.fstat(child_fd)
    except BaseException:
        os.close(child_fd)
        raise
    parent_identity = _directory_identity(parent)
    child_identity = _directory_identity(child)
    if _directory_identity(named) != child_identity or not stat.S_ISDIR(child.st_mode):
        os.close(child_fd)
        raise UnsafeFilesystemError("directory changed while it was opened")
    return child_fd, _DirectoryEdge(
        parent_fd,
        child_fd,
        name,
        parent_identity,
        child_identity,
    )


def _directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(metadata.st_dev, metadata.st_ino, metadata.st_mode)


@dataclass(frozen=True)
class CitationRewrite:
    source_id: str
    content_sha256: str
    raw_path: PurePosixPath

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_sha256(self.content_sha256, "content_sha256")
        _validate_raw_path(self.raw_path)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "source_id": self.source_id,
            "content_sha256": self.content_sha256,
            "raw_path": self.raw_path.as_posix(),
        }


@dataclass(frozen=True)
class VersionAdoption:
    record: SourceRecord
    citation_rewrites: tuple[CitationRewrite, ...]


class HistoricalBytesResolver(Protocol):
    def read_exact(
        self, source_id: str, raw_path: PurePosixPath, sha256: str
    ) -> bytes | None: ...


class LedgerStore:
    def __init__(self, paths: RepoPaths) -> None:
        self.paths = paths
        _validate_ledger_paths(paths)

    def save(self, record: SourceRecord) -> Path:
        target = self.paths.ledger_dir / f"{record.source_id}.json"
        payload = _canonical_record_payload(record)
        _atomic_write_repo_relative(
            self.paths.root,
            PurePosixPath("sources/ledger", target.name),
            payload,
        )
        return target

    def is_canonical_record_current(self, record: SourceRecord) -> bool:
        try:
            expected = _canonical_record_payload(record)
            payload = self._read_record_payload(record.source_id)
            if payload != expected:
                return False
            return (
                _decode_record(
                    payload,
                    expected_source_id=record.source_id,
                )
                == record
            )
        except (FileNotFoundError, OSError, ValueError):
            return False

    def load(self, source_id: str) -> SourceRecord:
        payload = self._read_record_payload(source_id)
        return _decode_record(payload, expected_source_id=source_id)

    def _read_record_payload(self, source_id: str) -> bytes:
        _validate_source_id(source_id)
        with _PinnedDirectory.open(self.paths.ledger_dir) as ledger:
            _reject_activation_guard(ledger.descriptor, source_id)
            payload, _metadata = _read_regular_at(
                ledger.descriptor, f"{source_id}.json", label="ledger"
            )
            _reject_activation_guard(ledger.descriptor, source_id)
            ledger.validate()
            return payload

    def recover_activation_guards(self) -> None:
        """Restore guarded inactive checkpoints while the caller owns the source lock."""

        with _PinnedDirectory.open(self.paths.ledger_dir) as ledger:
            for name in sorted(os.listdir(ledger.descriptor)):
                if not name.endswith(_ACTIVATION_GUARD_SUFFIX):
                    continue
                guard = ActivationGuard.load(
                    self.paths, name.removesuffix(_ACTIVATION_GUARD_SUFFIX)
                )
                payload, _metadata = _read_regular_at(
                    ledger.descriptor,
                    f"{guard.rollback.source_id}.json",
                    label="activation checkpoint",
                )
                if payload not in (
                    _canonical_record_payload(guard.predecessor),
                    _canonical_record_payload(guard.rollback),
                    _canonical_record_payload(guard.candidate),
                ):
                    raise ValueError("activation guard does not match its ledger shard")
                ledger.validate()
                # Errors before or after atomic publication retain the guard. A
                # later locked invocation can repeat this exact inactive post-image.
                self.save(guard.rollback)
                guard._clear_after_checkpoint(guard.rollback)
                ledger.validate()

    def load_all(self) -> dict[str, SourceRecord]:
        entries: list[str] = []
        with _PinnedDirectory.open(self.paths.root / "sources/ledger") as ledger:
            ledger_fd = ledger.descriptor
            for name in sorted(os.listdir(ledger_fd)):
                if name.endswith(_ACTIVATION_GUARD_SUFFIX):
                    raise ValueError(f"activation recovery is pending for {name}")
                try:
                    metadata = os.stat(
                        name,
                        dir_fd=ledger_fd,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise UnsafeFilesystemError(
                        "ledger entry changed during listing"
                    ) from error
                if name == "handoffs":
                    # The manifest namespace is the sole directory exception.
                    # Retain its pinned edge until the entire shard read finishes.
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise ValueError("handoffs must be a real directory")
                    ledger.open_child(ledger_fd, name)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"{name} must be a regular ledger file")
                if not name.endswith(".json"):
                    continue
                entries.append(name)
            ledger.validate()

            records: dict[str, SourceRecord] = {}
            for name in entries:
                source_id = name.removesuffix(".json")
                _validate_source_id(source_id)
                _reject_activation_guard(ledger_fd, source_id)
                payload, _metadata = _read_regular_at(ledger_fd, name, label="ledger")
                ledger.validate()
                records[source_id] = _decode_record(
                    payload, expected_source_id=source_id
                )
            for name in os.listdir(ledger_fd):
                if name.endswith(_ACTIVATION_GUARD_SUFFIX):
                    raise ValueError(f"activation recovery is pending for {name}")
            ledger.validate()
            return records

    def active_representations(self) -> tuple[SourceRepresentation, ...]:
        representations: list[SourceRepresentation] = []
        for record in self.load_all().values():
            if (
                record.active_content_sha256 is None
                or record.active_derivation_id is None
            ):
                continue
            representation = representation_for(
                record,
                record.active_content_sha256,
                record.active_derivation_id,
            )
            if representation is not None:
                representations.append(representation)
        return tuple(representations)

    def find_representation(
        self,
        source_id: str,
        content_sha256: str,
        derivation_id: str,
    ) -> SourceRepresentation | None:
        _validate_source_id(source_id)
        _validate_sha256(content_sha256, "content_sha256")
        _validate_derivation_id(derivation_id)
        try:
            record = self.load(source_id)
        except FileNotFoundError:
            return None
        return representation_for(record, content_sha256, derivation_id)

    def write_summary(
        self,
        records: Iterable[SourceRecord],
        *,
        generated_at: datetime,
    ) -> str:
        summary = self.render_summary(records, generated_at=generated_at)
        _atomic_write_repo_relative(
            self.paths.root,
            PurePosixPath("sources/ledger.md"),
            summary.encode("utf-8"),
        )
        return summary

    def read_summary(self) -> bytes:
        """Read the bounded summary through pinned, no-follow repository access."""

        with _PinnedDirectory.open(self.paths.root / "sources") as sources:
            payload, _metadata = _read_regular_at(
                sources.descriptor,
                "ledger.md",
                label="ledger summary",
                max_bytes=_MAX_LEDGER_SUMMARY_BYTES,
            )
            sources.validate()
            return payload

    def render_summary(
        self,
        records: Iterable[SourceRecord],
        *,
        generated_at: datetime,
    ) -> str:
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be an aware datetime")
        ordered = sorted(records, key=lambda record: record.source_id)
        for record in ordered:
            record.to_dict()
        revision = compute_corpus_revision(ordered)
        counts = {state: 0 for state in _STATE_ORDER}
        for record in ordered:
            counts[record.state] += 1

        lines = [
            "# Source Ledger",
            "",
            f"Last synchronized: {_format_datetime(generated_at)}",
            f"Corpus revision: `{revision}`",
            "",
            "## State counts",
            "",
        ]
        lines.extend(f"- {state.value}: {counts[state]}" for state in _STATE_ORDER)
        lines.extend(["", "## Coverage gaps", ""])
        gap_count = sum(_source_has_coverage_gap(record) for record in ordered)
        if not gap_count:
            lines.append("- None.")
        else:
            gap_bytes = 0
            omitted_gaps = 0
            for record in ordered:
                if not _source_has_coverage_gap(record):
                    continue
                diagnostic_codes = (
                    ", ".join(
                        _markdown_code(diagnostic.code)
                        for diagnostic in record.diagnostics
                    )
                    or "no diagnostic code"
                )
                row = (
                    f"- [record {_markdown_code(record.source_id)}]"
                    f"({_markdown_destination(f'ledger/{record.source_id}.json')}): "
                    f"{record.state.value} ({diagnostic_codes})"
                )
                row_bytes = len((row + "\n").encode("utf-8"))
                if gap_bytes + row_bytes <= _MAX_LEDGER_SUMMARY_GAP_BYTES:
                    lines.append(row)
                    gap_bytes += row_bytes
                else:
                    omitted_gaps += 1
            if omitted_gaps:
                lines.append(
                    f"- {omitted_gaps} coverage-gap records omitted from this "
                    "bounded projection."
                )

        lines.extend(["", "## Sources", ""])
        if not ordered:
            lines.append("- None.")
        source_bytes = 0
        omitted_sources = 0
        for record in ordered:
            raw_path = record.current_raw_path.as_posix()
            row = (
                f"- [record {_markdown_code(record.source_id)}]"
                f"({_markdown_destination(f'ledger/{record.source_id}.json')}) — "
                f"{record.state.value} — "
                f"[raw {_markdown_code(raw_path)}]"
                f"({_markdown_destination(f'raw/{raw_path}')})"
            )
            if (
                record.active_content_sha256 is not None
                and record.active_derivation_id is not None
            ):
                derivation = record.derivations.get(record.active_derivation_id)
                if (
                    derivation is not None
                    and derivation.source_sha256 == record.active_content_sha256
                ):
                    output = derivation.output_path.as_posix()
                    relative_output = output.removeprefix("sources/")
                    row += (
                        f" — [active artifact {_markdown_code(output)}]"
                        f"({_markdown_destination(relative_output)})"
                    )
            row_bytes = len((row + "\n").encode("utf-8"))
            if source_bytes + row_bytes <= _MAX_LEDGER_SUMMARY_SOURCE_BYTES:
                lines.append(row)
                source_bytes += row_bytes
            else:
                omitted_sources += 1
        if omitted_sources:
            lines.append(
                f"- {omitted_sources} sources omitted from this bounded projection."
            )
        summary = "\n".join(lines) + "\n"
        if len(summary.encode("utf-8")) > _MAX_LEDGER_SUMMARY_BYTES:
            raise ValueError("bounded ledger summary exceeds its protocol limit")
        return summary


def transition(
    record: SourceRecord,
    next_state: SourceState,
    *,
    now: datetime,
    diagnostics: Iterable[Diagnostic] = (),
) -> SourceRecord:
    record.to_dict()
    if not isinstance(next_state, SourceState):
        raise ValueError("next_state must be a SourceState")
    frozen_diagnostics = tuple(diagnostics)
    transitioned = replace(
        record,
        state=next_state,
        diagnostics=frozen_diagnostics,
        updated_at=now,
    )
    transitioned.to_dict()
    return transitioned


def recover_stale_extraction(
    record: SourceRecord,
    *,
    now: datetime,
) -> SourceRecord:
    record.to_dict()
    transition(
        record,
        record.state,
        now=now,
        diagnostics=record.diagnostics,
    )
    recovery_code = _STALE_EXTRACTION_RECOVERY_DIAGNOSTIC.code
    collisions = tuple(
        index
        for index, diagnostic in enumerate(record.diagnostics)
        if diagnostic.code == recovery_code
    )
    if record.state is SourceState.PENDING:
        if (
            collisions == (len(record.diagnostics) - 1,)
            and record.diagnostics[-1] == _STALE_EXTRACTION_RECOVERY_DIAGNOSTIC
        ):
            return record
        raise ValueError(
            "pending stale recovery requires exactly one canonical recovery "
            "diagnostic in final position"
        )
    if record.state is not SourceState.EXTRACTING:
        raise ValueError("stale recovery requires an extracting source record")
    if collisions:
        raise ValueError("extracting record has a colliding recovery diagnostic")
    return transition(
        record,
        SourceState.PENDING,
        now=now,
        diagnostics=(
            *record.diagnostics,
            _STALE_EXTRACTION_RECOVERY_DIAGNOSTIC,
        ),
    )


def derive_extraction_path(
    raw_path: PurePosixPath, content_sha256: str, derivation_id: str
) -> PurePosixPath:
    _validate_raw_path(raw_path)
    _validate_sha256(content_sha256, "content_sha256")
    _validate_derivation_id(derivation_id)
    return PurePosixPath(
        raw_path.parent,
        raw_path.name,
        content_sha256,
        f"{derivation_id}.md",
    )


def activate_derivation(
    record: SourceRecord, derivation: Derivation, *, now: datetime
) -> SourceRecord:
    record.to_dict()
    if derivation.source_sha256 not in record.versions:
        raise ValueError("derivation must reference a retained content version")
    if derivation.source_sha256 != record.active_content_sha256:
        raise ValueError("derivation must reference the active content checksum")
    existing = record.derivations.get(derivation.derivation_id)
    if existing is not None and existing != derivation:
        raise ValueError("derivation_id collision would replace retained history")
    derivations = dict(record.derivations)
    if existing is None:
        derivations[derivation.derivation_id] = derivation
    activated = replace(
        record,
        derivations=derivations,
        active_derivation_id=derivation.derivation_id,
        state=(
            SourceState.OK if derivation.quality_state == "ok" else SourceState.WARNING
        ),
        updated_at=now,
    )
    activated.to_dict()
    return activated


def retry_is_eligible(
    record: SourceRecord,
    *,
    input_sha256: str,
    extractor: ExtractorSpec,
    prerequisite_digest: str,
    explicit_retry: bool = False,
) -> bool:
    _validate_sha256(input_sha256, "input_sha256")
    if explicit_retry:
        return True
    if record.state is SourceState.EXTRACTING:
        return True
    attempt = record.last_attempt
    if attempt is None:
        return True
    expected = (
        input_sha256,
        extractor.extractor_id,
        effective_extractor_version(extractor, prerequisite_digest),
        extractor.config_sha256,
        prerequisite_digest,
    )
    observed = (
        attempt.input_sha256,
        attempt.extractor_id,
        attempt.extractor_version,
        attempt.config_sha256,
        attempt.prerequisite_digest,
    )
    if observed != expected:
        return True
    return attempt.outcome is SourceState.EXTRACTING


class GitHistoryResolver:
    def __init__(
        self,
        repo_root: Path,
        *,
        timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
        max_history_bytes: int = _DEFAULT_MAX_GIT_HISTORY_BYTES,
        max_blob_bytes: int = _DEFAULT_MAX_GIT_BLOB_BYTES,
    ) -> None:
        if not repo_root.is_absolute():
            raise ValueError("repo_root must be an absolute path")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if type(max_history_bytes) is not int or max_history_bytes <= 0:
            raise ValueError("max_history_bytes must be a positive integer")
        if type(max_blob_bytes) is not int or max_blob_bytes <= 0:
            raise ValueError("max_blob_bytes must be a positive integer")
        self.repo_root = repo_root
        self.timeout_seconds = float(timeout_seconds)
        self.max_history_bytes = max_history_bytes
        self.max_blob_bytes = max_blob_bytes

    def read_exact(
        self, source_id: str, raw_path: PurePosixPath, sha256: str
    ) -> bytes | None:
        _validate_source_id(source_id)
        _validate_raw_path(raw_path)
        _validate_sha256(sha256, "sha256")
        archive_path = PurePosixPath("_versions", source_id, sha256, raw_path.name)
        archived = _read_optional_repo_file(
            self.repo_root,
            PurePosixPath("sources/raw") / archive_path,
            label="archive",
        )
        if archived is not None:
            return archived.data if _sha256_bytes(archived.data) == sha256 else None

        repo_raw_path = PurePosixPath("sources/raw") / raw_path
        deadline = time.monotonic() + self.timeout_seconds
        history = _run_git_bounded(
            ["git", "rev-list", "--all", "--", repo_raw_path.as_posix()],
            cwd=self.repo_root,
            deadline=deadline,
            max_stdout_bytes=self.max_history_bytes,
        )
        if history is None or history[0] != 0:
            return None
        history_bytes = history[1]
        try:
            history_text = history_bytes.decode("ascii")
        except UnicodeDecodeError:
            return None
        if history_text and not history_text.endswith("\n"):
            return None
        revisions = history_text.splitlines()
        if any(not _GIT_REVISION_RE.fullmatch(revision) for revision in revisions):
            return None
        for revision in revisions:
            shown = _run_git_bounded(
                ["git", "show", f"{revision}:{repo_raw_path.as_posix()}", "--"],
                cwd=self.repo_root,
                deadline=deadline,
                max_stdout_bytes=self.max_blob_bytes,
            )
            if shown is None:
                return None
            return_code, blob = shown
            if return_code == 0 and _sha256_bytes(blob) == sha256:
                return blob
        return None


def _run_git_bounded(
    argv: list[str],
    *,
    cwd: Path,
    deadline: float,
    max_stdout_bytes: int,
) -> tuple[int, bytes] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    work_deadline = deadline - min(_GIT_CLEANUP_RESERVE_SECONDS, remaining / 2)
    try:
        process_tree = _create_probe_tree()
    except OSError:
        return None
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            **process_tree.popen_options,
        )
    except OSError:
        process_tree.terminate_tree()
        return None
    assert process.stdout is not None
    stdout = process.stdout
    output = bytearray()
    oversized = threading.Event()
    reader_error: list[BaseException] = []

    def read_stdout() -> None:
        try:
            descriptor = stdout.fileno()
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    return
                if len(output) <= max_stdout_bytes:
                    remaining_capacity = max_stdout_bytes + 1 - len(output)
                    output.extend(chunk[:remaining_capacity])
                if len(output) > max_stdout_bytes:
                    oversized.set()
        except BaseException as error:
            reader_error.append(error)

    reader = threading.Thread(target=read_stdout, daemon=True)
    failed = False
    try:
        process_tree.attach(process)
        reader.start()
        while not _git_leader_exited(
            process,
            preserve_identity=process_tree.preserve_leader_identity,
        ):
            if oversized.is_set() or time.monotonic() >= work_deadline:
                failed = True
                break
            time.sleep(
                min(
                    _PROCESS_POLL_SECONDS,
                    max(0.0, work_deadline - time.monotonic()),
                )
            )
    except (OSError, ValueError):
        failed = True
    finally:
        try:
            process_tree.terminate_tree()
        except OSError:
            failed = True
        if process.returncode is None:
            try:
                process.kill()
            except OSError:
                pass
            _bounded_wait_or_reap_later(process, deadline=deadline)
        if reader.ident is not None:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
            if reader.is_alive():
                failed = True
        try:
            stdout.close()
        except OSError:
            failed = True
    if (
        failed
        or reader_error
        or reader.is_alive()
        or oversized.is_set()
        or time.monotonic() > deadline
        or process.returncode is None
    ):
        return None
    return process.returncode, bytes(output)


def _git_leader_exited(
    process: subprocess.Popen[bytes],
    *,
    preserve_identity: bool,
) -> bool:
    if not preserve_identity:
        return process.poll() is not None
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            return os.waitid(os.P_PID, process.pid, flags) is not None
        except InterruptedError:
            continue
        except ChildProcessError:
            return process.poll() is not None


def adopt_version(
    record: SourceRecord,
    candidate: InventoryItem,
    *,
    paths: RepoPaths,
    resolver: HistoricalBytesResolver,
    approval_note: str,
    now: datetime,
    candidate_sha256: str | None = None,
) -> VersionAdoption:
    if record.state is not SourceState.INTEGRITY_ERROR:
        raise ValueError("source version adoption requires integrity_error state")
    if not approval_note.strip():
        raise ValueError("a nonempty factual approval note is required")
    if record.url_descriptor is not None:
        raise ValueError("URL descriptor control bytes cannot be adopted as content")
    if candidate.sha256 is None:
        raise ValueError("candidate checksum is required")
    _validate_sha256(candidate.sha256, "candidate checksum")
    requested_sha256 = (
        candidate.sha256 if candidate_sha256 is None else candidate_sha256
    )
    _validate_sha256(requested_sha256, "candidate checksum")
    if requested_sha256 != candidate.sha256:
        raise ValueError(
            "candidate checksum does not match the observed inventory item"
        )
    if candidate.fingerprint.path != record.current_raw_path:
        raise ValueError("candidate path does not match the source record")
    if candidate.sha256 in record.versions:
        raise ValueError("candidate checksum is already a retained content version")
    if record.active_content_sha256 is None:
        raise ValueError("integrity_error record has no active content version")

    _validate_raw_paths(paths)
    live_path = PurePosixPath("sources/raw") / candidate.fingerprint.path
    live = _read_optional_repo_file(
        paths.root,
        live_path,
        label="source",
    )
    if live is None:
        raise ValueError("live source no longer matches the observed inventory item")
    if (
        live.metadata.st_size != candidate.fingerprint.byte_size
        or live.metadata.st_mtime_ns != candidate.fingerprint.mtime_ns
        or _sha256_bytes(live.data) != candidate.sha256
    ):
        raise ValueError("live source no longer matches the observed inventory item")

    proofs: list[
        tuple[
            str,
            ContentVersion,
            PurePosixPath,
            bytes | None,
            _FileSnapshot | None,
        ]
    ] = []
    for checksum, version in sorted(record.versions.items()):
        archive_path = PurePosixPath(
            "_versions", record.source_id, checksum, version.raw_path.name
        )
        existing = _read_optional_repo_file(
            paths.root,
            PurePosixPath("sources/raw") / archive_path,
            label="archive",
        )
        if existing is not None:
            if _sha256_bytes(existing.data) != checksum:
                raise ValueError(
                    f"archive checksum mismatch for retained version {checksum}"
                )
            proofs.append((checksum, version, archive_path, None, existing))
            continue
        recovered = resolver.read_exact(record.source_id, version.raw_path, checksum)
        if recovered is None or _sha256_bytes(recovered) != checksum:
            raise ValueError(
                f"cannot recover prior exact bytes for retained version {checksum}"
            )
        proofs.append((checksum, version, archive_path, recovered, None))

    published: list[tuple[str, ContentVersion, PurePosixPath, _FileSnapshot]] = []
    for checksum, version, archive_path, recovered, existing in proofs:
        if recovered is not None:
            archive_bytes = recovered
        elif existing is not None:
            archive_bytes = existing.data
        else:
            raise AssertionError("archive proof is missing both retained byte sources")
        _atomic_write_archive(paths.root, archive_path, archive_bytes)
        materialized = _read_optional_repo_file(
            paths.root,
            PurePosixPath("sources/raw") / archive_path,
            label="archive",
        )
        if materialized is None:
            raise OSError(f"failed to materialize archive {archive_path}")
        if _sha256_bytes(materialized.data) != checksum:
            raise ValueError(
                f"archive checksum mismatch for retained version {checksum}"
            )
        published.append((checksum, version, archive_path, materialized))

    final_archives: list[tuple[str, ContentVersion, PurePosixPath, _FileSnapshot]] = []
    for checksum, version, archive_path, earlier in published:
        final = _read_required_repo_file(
            paths.root,
            PurePosixPath("sources/raw") / archive_path,
            label="archive",
        )
        if (
            _sha256_bytes(final.data) != checksum
            or not _same_file_observation(earlier.metadata, final.metadata)
            or earlier.anchors != final.anchors
        ):
            raise ValueError(
                f"archive changed during adoption for retained version {checksum}"
            )
        final_archives.append((checksum, version, archive_path, final))

    final_live = _read_required_repo_file(paths.root, live_path, label="source")
    if (
        _sha256_bytes(final_live.data) != candidate.sha256
        or final_live.metadata.st_size != candidate.fingerprint.byte_size
        or final_live.metadata.st_mtime_ns != candidate.fingerprint.mtime_ns
        or not _same_file_observation(live.metadata, final_live.metadata)
        or live.anchors != final_live.anchors
    ):
        raise ValueError("live source changed during adoption")

    versions: dict[str, ContentVersion] = {}
    rewrites: list[CitationRewrite] = []
    for checksum, version, archive_path, final in final_archives:
        archive_fingerprint = FileFingerprint(
            archive_path,
            final.metadata.st_size,
            final.metadata.st_mtime_ns,
        )
        versions[checksum] = replace(
            version,
            raw_path=archive_path,
            byte_size=final.metadata.st_size,
            fingerprint=archive_fingerprint,
        )
        rewrites.append(CitationRewrite(record.source_id, checksum, archive_path))

    live_fingerprint = FileFingerprint(
        candidate.fingerprint.path,
        final_live.metadata.st_size,
        final_live.metadata.st_mtime_ns,
    )
    versions[candidate.sha256] = ContentVersion(
        candidate.sha256,
        candidate.fingerprint.path,
        final_live.metadata.st_size,
        live_fingerprint,
        now,
        (),
    )
    adopted = replace(
        record,
        media_type=candidate.media_type,
        byte_size=final_live.metadata.st_size,
        state=SourceState.PENDING,
        versions=versions,
        active_content_sha256=candidate.sha256,
        active_derivation_id=None,
        last_attempt=None,
        diagnostics=tuple(
            diagnostic
            for diagnostic in record.diagnostics
            if diagnostic.code not in _RESOLVED_INTEGRITY_DIAGNOSTICS
        ),
        inspected_at=now,
        updated_at=now,
        adoption_events=record.adoption_events
        + (
            VersionAdoptionEvent(
                record.active_content_sha256,
                candidate.sha256,
                approval_note,
                now,
            ),
        ),
    )
    adopted.to_dict()
    return VersionAdoption(
        adopted,
        tuple(
            sorted(
                rewrites,
                key=lambda rewrite: (
                    rewrite.source_id,
                    rewrite.content_sha256,
                    rewrite.raw_path.as_posix(),
                ),
            )
        ),
    )


def representation_for(
    record: SourceRecord, content_sha256: str, derivation_id: str
) -> SourceRepresentation | None:
    record.to_dict()
    version = record.versions.get(content_sha256)
    derivation = record.derivations.get(derivation_id)
    if (
        version is None
        or derivation is None
        or derivation.source_sha256 != content_sha256
    ):
        return None
    return SourceRepresentation(
        record.source_id,
        content_sha256,
        derivation_id,
        version.raw_path,
        derivation.output_path,
        derivation.output_sha256,
        derivation.quality_state,
        derivation.anchors,
    )


@dataclass(frozen=True)
class ActivationGuard:
    paths: RepoPaths
    rollback: SourceRecord
    predecessor: SourceRecord
    candidate: SourceRecord
    snapshot: _FileSnapshot

    @classmethod
    def prepare_reactivation(
        cls,
        paths: RepoPaths,
        predecessor: SourceRecord,
        rollback: SourceRecord,
        candidate: SourceRecord,
    ) -> ActivationGuard:
        """Guard a retained web activation without inventing a processing attempt."""
        _validate_ledger_paths(paths)
        document = {
            "schema_version": 2,
            "kind": "retained_web_reactivation",
            "predecessor": predecessor.to_dict(),
            "rollback": rollback.to_dict(),
            "candidate": candidate.to_dict(),
        }
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        _decode_activation_records(payload, predecessor.source_id)
        name = predecessor.source_id + _ACTIVATION_GUARD_SUFFIX
        with _PinnedDirectory.open(paths.ledger_dir) as ledger:
            _reject_activation_guard(ledger.descriptor, predecessor.source_id)
            prior, _ = _read_regular_at(
                ledger.descriptor,
                predecessor.source_id + ".json",
                label="reactivation predecessor",
                synchronize=True,
            )
            if prior != _canonical_record_payload(predecessor):
                raise ValueError("reactivation predecessor is not the durable record")
            _atomic_write_at(ledger.descriptor, name, payload, replace_existing=False)
            observed, metadata = _read_regular_at(
                ledger.descriptor, name, label="activation guard"
            )
            if observed != payload:
                raise ValueError("activation guard changed during publication")
            ledger.validate()
            return cls(
                paths,
                rollback,
                predecessor,
                candidate,
                _FileSnapshot(payload, metadata, ledger.identities),
            )

    @classmethod
    def prepare(
        cls, paths: RepoPaths, extracting: SourceRecord, candidate: SourceRecord
    ) -> ActivationGuard:
        """Publish rollback authority before the candidate's active checkpoint."""

        _validate_ledger_paths(paths)
        rollback = replace(extracting, active_derivation_id=None)
        document = {
            "schema_version": 1,
            "rollback": rollback.to_dict(),
            "previous_active_derivation_id": extracting.active_derivation_id,
            "candidate": candidate.to_dict(),
        }
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        _decode_activation_records(payload, rollback.source_id)
        name = rollback.source_id + _ACTIVATION_GUARD_SUFFIX
        with _PinnedDirectory.open(paths.ledger_dir) as ledger:
            _reject_activation_guard(ledger.descriptor, rollback.source_id)
            _atomic_write_at(ledger.descriptor, name, payload, replace_existing=False)
            observed, metadata = _read_regular_at(
                ledger.descriptor, name, label="activation guard"
            )
            if observed != payload:
                raise ValueError("activation guard changed during publication")
            ledger.validate()
            snapshot = _FileSnapshot(payload, metadata, ledger.identities)
            return cls(paths, rollback, extracting, candidate, snapshot)

    @classmethod
    def load(cls, paths: RepoPaths, source_id: str) -> ActivationGuard:
        _validate_source_id(source_id)
        snapshot = _read_required_repo_file(
            paths.root,
            PurePosixPath("sources/ledger", source_id + _ACTIVATION_GUARD_SUFFIX),
            label="activation guard",
        )
        rollback, predecessor, candidate = _decode_activation_records(
            snapshot.data, source_id
        )
        return cls(paths, rollback, predecessor, candidate, snapshot)

    def _clear_after_checkpoint(self, expected: SourceRecord) -> None:
        """Prove the exact candidate/rollback shard while its guard still exists."""

        expected_payload = _canonical_record_payload(expected)
        if expected_payload not in (
            _canonical_record_payload(self.candidate),
            _canonical_record_payload(self.rollback),
        ):
            raise ValueError(
                "activation checkpoint is not the guarded candidate or rollback"
            )
        name = self.rollback.source_id + _ACTIVATION_GUARD_SUFFIX
        shard_name = self.rollback.source_id + ".json"
        with _PinnedDirectory.open(self.paths.ledger_dir) as ledger:
            payload, metadata = _read_regular_at(
                ledger.descriptor, name, label="activation guard", synchronize=True
            )
            if (
                payload != self.snapshot.data
                or not _same_file_observation(metadata, self.snapshot.metadata)
                or ledger.identities != self.snapshot.anchors
            ):
                raise ValueError("activation guard changed before cleanup")
            checkpoint, checkpoint_metadata = _read_regular_at(
                ledger.descriptor,
                shard_name,
                label="activation checkpoint",
                synchronize=True,
            )
            if checkpoint != expected_payload:
                raise ValueError(
                    "activation checkpoint does not match the expected durable record"
                )
            # Both names must still designate the files proved through this same
            # pinned directory. Ordinary LedgerStore reads intentionally reject
            # the guard and cannot supply this proof.
            if not _same_file_observation(
                metadata,
                os.stat(name, dir_fd=ledger.descriptor, follow_symlinks=False),
            ):
                raise ValueError("activation guard changed before cleanup")
            if not _same_file_observation(
                checkpoint_metadata,
                os.stat(shard_name, dir_fd=ledger.descriptor, follow_symlinks=False),
            ):
                raise ValueError("activation checkpoint changed before guard cleanup")
            ledger.validate()
            os.unlink(name, dir_fd=ledger.descriptor)
            _fsync_directory(ledger.descriptor)
            ledger.validate()


def _reject_activation_guard(directory_fd: int, source_id: str) -> None:
    try:
        os.stat(
            source_id + _ACTIVATION_GUARD_SUFFIX,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    raise ValueError(f"activation recovery is pending for {source_id}")


def _decode_activation_records(
    payload: bytes, source_id: str
) -> tuple[SourceRecord, SourceRecord, SourceRecord]:
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        if (
            isinstance(document, dict)
            and type(document.get("schema_version")) is int
            and document["schema_version"] == 2
        ):
            return _decode_reactivation_records(document, source_id)
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "schema_version",
                "rollback",
                "previous_active_derivation_id",
                "candidate",
            }
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
        ):
            raise ValueError("invalid activation guard envelope")
        rollback = SourceRecord.from_dict(document["rollback"])
        candidate = SourceRecord.from_dict(document["candidate"])
        if (
            rollback.source_id != source_id
            or candidate.source_id != source_id
            or rollback.state is not SourceState.EXTRACTING
            or rollback.active_derivation_id is not None
            or rollback.active_content_sha256 is None
            or rollback.last_attempt is None
            or rollback.last_attempt.outcome is not SourceState.EXTRACTING
            or rollback.last_attempt.input_sha256 != rollback.active_content_sha256
            or candidate.active_derivation_id is None
            or candidate.last_attempt is None
            or candidate.last_attempt.outcome is not candidate.state
        ):
            raise ValueError("invalid activation guard records")
        predecessor = replace(
            rollback, active_derivation_id=document["previous_active_derivation_id"]
        )
        predecessor.to_dict()
        prepared = replace(
            predecessor,
            last_attempt=candidate.last_attempt,
            diagnostics=candidate.diagnostics,
        )
        expected = activate_derivation(
            prepared,
            candidate.derivations[candidate.active_derivation_id],
            now=rollback.updated_at,
        )
        if expected != candidate:
            raise ValueError("activation candidate does not match its rollback record")
        return rollback, predecessor, candidate
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as error:
        raise ValueError(
            f"invalid activation guard for {source_id}: {error}"
        ) from error


def _decode_reactivation_records(document, source_id):
    if (
        set(document)
        != {"schema_version", "kind", "predecessor", "rollback", "candidate"}
        or document["kind"] != "retained_web_reactivation"
    ):
        raise ValueError("invalid retained web reactivation envelope")
    predecessor = SourceRecord.from_dict(document["predecessor"])
    rollback = SourceRecord.from_dict(document["rollback"])
    candidate = SourceRecord.from_dict(document["candidate"])
    checksum = rollback.active_content_sha256
    selected = rollback.versions.get(checksum)
    previous = predecessor.versions.get(checksum)
    derivation = predecessor.derivations.get(candidate.active_derivation_id)
    if (
        predecessor.source_id != source_id
        or predecessor.url_descriptor is None
        or selected is None
        or previous is None
        or derivation is None
        or candidate.active_derivation_id == predecessor.active_derivation_id
        or derivation.source_sha256 != checksum
        or selected.raw_path.parts[:3] != ("_web", source_id, checksum)
        or len(selected.raw_path.parts) != 4
        or len(selected.retrieval_events) != len(previous.retrieval_events) + 1
        or selected.retrieval_events[:-1] != previous.retrieval_events
        or replace(selected, retrieval_events=previous.retrieval_events) != previous
        or derivation.output_path
        != PurePosixPath("sources/extracted")
        / derive_extraction_path(selected.raw_path, checksum, derivation.derivation_id)
    ):
        raise ValueError(
            "reactivation must bind a retained inactive web derivation and one retrieval"
        )
    retrieval = selected.retrieval_events[-1]
    expected_rollback = replace(
        predecessor,
        versions={**predecessor.versions, checksum: selected},
        active_content_sha256=checksum,
        active_derivation_id=None,
        state=SourceState.PENDING,
        media_type=retrieval.detected_media_type,
        byte_size=selected.byte_size,
        inspected_at=rollback.updated_at,
        updated_at=rollback.updated_at,
    )
    if rollback != expected_rollback or candidate != activate_derivation(
        rollback, derivation, now=rollback.updated_at
    ):
        raise ValueError(
            "reactivation post-images differ from the exact retained history"
        )
    return rollback, predecessor, candidate


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _decode_record(payload: bytes, *, expected_source_id: str) -> SourceRecord:

    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid ledger JSON for {expected_source_id}") from error
    if not isinstance(document, dict):
        raise ValueError("ledger record must be a JSON object")
    record = SourceRecord.from_dict(document)
    if record.source_id != expected_source_id:
        raise ValueError("ledger filename must match record source_id")
    return record


def _canonical_record_payload(record: SourceRecord) -> bytes:
    document = record.to_dict()
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _format_datetime(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def _validate_source_id(source_id: object) -> None:
    if type(source_id) is not str or not _SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError(
            "source_id must be src_ followed by 64 lowercase hex characters"
        )


def _validate_sha256(value: object, name: str) -> None:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be 64 lowercase hex characters")


def _validate_derivation_id(value: object) -> None:
    if type(value) is not str or not _DERIVATION_ID_RE.fullmatch(value):
        raise ValueError(
            "derivation_id must be drv_ followed by 64 lowercase hex characters"
        )


def _validate_raw_path(path: object) -> None:
    if not isinstance(path, PurePosixPath) or path.is_absolute():
        raise ValueError("raw_path must be a repository-relative POSIX path")
    text = path.as_posix()
    if (
        not text
        or text == "."
        or "\\" in text
        or "\0" in text
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[:2] == ("sources", "raw")
    ):
        raise ValueError("raw_path must be a canonical raw-relative POSIX path")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_ledger_paths(paths: RepoPaths) -> None:
    if paths.ledger_dir != paths.root / "sources/ledger":
        raise ValueError("ledger directory must be repository-relative sources/ledger")
    if paths.ledger_summary != paths.root / "sources/ledger.md":
        raise ValueError("ledger summary must be repository-relative sources/ledger.md")


def _validate_raw_paths(paths: RepoPaths) -> None:
    if paths.raw != paths.root / "sources/raw":
        raise ValueError("raw directory must be repository-relative sources/raw")


def _require_safe_filesystem() -> None:
    if _OPEN_SUPPORT_MARKER is not os.open or not _SAFE_PLATFORM_SUPPORTED:
        raise UnsafeFilesystemError(
            "safe descriptor-relative filesystem operations are unavailable"
        )


def _validate_repo_relative(path: PurePosixPath) -> None:
    if path.is_absolute():
        raise ValueError("repository path must be relative")
    text = path.as_posix()
    if (
        not text
        or text == "."
        or "\\" in text
        or "\0" in text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("repository path must be canonical and relative")


def _atomic_write_repo_relative(
    repo_root: Path,
    relative_path: PurePosixPath,
    payload: bytes,
) -> None:
    _validate_repo_relative(relative_path)
    with _PinnedDirectory.open(repo_root / Path(*relative_path.parts[:-1])) as parent:
        _atomic_write_at(parent.descriptor, relative_path.name, payload)
        try:
            parent.validate()
        except (OSError, ValueError) as error:
            raise PublishedWriteError(
                f"published {relative_path.as_posix()} but its pinned parent "
                f"could not be revalidated: {error}"
            ) from error


def _atomic_write_archive(
    repo_root: Path,
    archive_path: PurePosixPath,
    payload: bytes,
) -> None:
    _validate_raw_path(archive_path)
    with _PinnedDirectory.open(repo_root / "sources/raw") as pinned:
        directory_fd = pinned.descriptor
        for component in archive_path.parts[:-1]:
            try:
                os.mkdir(component, mode=0o755, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = pinned.open_child(directory_fd, component)
            _fsync_directory(directory_fd)
            directory_fd = child_fd
        _atomic_write_at(
            directory_fd,
            archive_path.name,
            payload,
            replace_existing=False,
        )
        pinned.validate()


def _atomic_write_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    replace_existing: bool = True,
) -> None:
    if not name or "/" in name or "\0" in name:
        raise ValueError("atomic target name must be a single path component")
    published = False
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise ValueError(f"{name} must be a regular file")
    if current is not None and not replace_existing:
        existing, _metadata = _read_regular_at(
            directory_fd,
            name,
            label="archive",
            synchronize=True,
        )
        if existing != payload:
            raise ValueError(f"archive checksum mismatch for {name}")
        _fsync_directory(directory_fd)
        return

    temporary = f".brain-tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace_existing:
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            published = True
        else:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                published = True
            except FileExistsError:
                existing, _metadata = _read_regular_at(
                    directory_fd,
                    name,
                    label="archive",
                    synchronize=True,
                )
                if existing != payload:
                    raise ValueError(f"archive checksum mismatch for {name}")
            os.unlink(temporary, dir_fd=directory_fd)
        _fsync_directory(directory_fd)
    except BaseException as error:
        if published:
            raise PublishedWriteError(
                f"published {name} but directory synchronization failed: {error}"
            ) from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _read_regular_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    synchronize: bool = False,
    max_bytes: int | None = None,
) -> tuple[bytes, os.stat_result]:
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer or None")
    descriptor: int | None = None
    try:
        named_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(named_before.st_mode):
            raise ValueError(f"{name} must be a regular {label} file")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{name} must be a regular {label} file")
        if not _same_file_observation(named_before, before):
            raise OSError(f"{label} file changed while it was being opened")
        chunks: list[bytes] = []
        observed_bytes = 0
        while True:
            remaining = (
                1_048_576
                if max_bytes is None
                else min(1_048_576, max_bytes + 1 - observed_bytes)
            )
            if remaining <= 0:
                raise ValueError(f"{label} file exceeds the byte limit")
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            observed_bytes += len(chunk)
            if max_bytes is not None and observed_bytes > max_bytes:
                raise ValueError(f"{label} file exceeds the byte limit")
        if synchronize:
            os.fsync(descriptor)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not (
            _same_file_observation(before, after)
            and _same_file_observation(after, named)
        ):
            raise OSError(f"{label} file changed while it was being read")
        return b"".join(chunks), after
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{name} must be a regular {label} file") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as error:
        unsupported = {
            errno.EBADF,
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if error.errno not in unsupported:
            raise


def _read_required_repo_file(
    repo_root: Path,
    relative_path: PurePosixPath,
    *,
    label: str,
) -> _FileSnapshot:
    result = _read_optional_repo_file(repo_root, relative_path, label=label)
    if result is None:
        raise FileNotFoundError(relative_path.as_posix())
    return result


def _read_optional_repo_file(
    repo_root: Path,
    relative_path: PurePosixPath,
    *,
    label: str,
) -> _FileSnapshot | None:
    _validate_repo_relative(relative_path)
    try:
        pinned = _PinnedDirectory.open(repo_root / Path(*relative_path.parts[:-1]))
    except FileNotFoundError:
        return None
    with pinned:
        try:
            data, metadata = _read_regular_at(
                pinned.descriptor,
                relative_path.name,
                label=label,
            )
        except FileNotFoundError:
            return None
        pinned.validate()
        return _FileSnapshot(data, metadata, pinned.identities)


def _same_file_observation(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _markdown_destination(value: str) -> str:
    return quote(value, safe="/-._~")


def _markdown_code(value: str) -> str:
    visible = "".join(
        (
            f"\\x{ord(character):02x}"
            if ord(character) <= 0xFF
            else f"\\u{ord(character):04x}"
        )
        if unicodedata.category(character).startswith("C")
        else character
        for character in value
    )
    escaped = (
        html.escape(visible, quote=False).replace("[", "&#91;").replace("]", "&#93;")
    )
    return f"<code>{escaped}</code>"
