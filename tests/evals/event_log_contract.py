"""Fail-closed, host-artifact validation for normalized evaluation logs.

The JSON log is deliberately only an index of claims.  A caller must provide
the runner's private control root; pass claims are derived from descriptor-read
artifacts below that root and never from event text or a client workspace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping

from markdown_it import MarkdownIt
from markdown_it.rules_block import lheading as _commonmark_lheading, paragraph as _commonmark_paragraph
from markdown_it.rules_inline import image as _commonmark_image
from markdown_it.rules_inline.backticks import backtick as _commonmark_backtick
from markdown_it.token import Token

from brainlib.sync_results import HandoffDeliveryReference, SyncResultConsumption, SyncResultReference
from brainlib.extractors.handoff import _decode_item, _validate_item, handoff_to_dict
from tests.evals.scenario_contract import ScenarioContractError, validate_against_schema
from tests.evals import semantic_evidence as semantic
from tests.evals.phase_prompt_contract import (
    PROMPT_PROTOCOL,
    canonical_phase_prompt_bytes,
    required_events_for_phase,
    scenario_sha256,
)


class EventLogContractError(ValueError):
    """A claimed pass lacks the required independently captured evidence."""


_ID = re.compile(r"[a-z][a-z0-9._-]*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NATIVE_EVENT_LINE = re.compile(rb"EVENT:([a-z][a-z0-9._-]*)\n\Z")
_INITIAL_SYNC_RECEIPT_STAGES = (
    "verified", "consumed", "durable", "acknowledged",
)


@dataclass(frozen=True)
class InitialSyncReceiptStageBoundary:
    """One parser-bound receipt-marker position from the shared trace clock.

    The caller derives these transient facts from the native parser and the
    sealed execution trace before calling the receipt reader.  This value
    deliberately carries no marker ID or artifact reference: a phase gate
    needs the parser-bound ordering position, not a synthetic event record.
    """

    stage: str
    trace_sequence: int

    def __post_init__(self) -> None:
        if (self.stage not in _INITIAL_SYNC_RECEIPT_STAGES
                or type(self.trace_sequence) is not int
                or self.trace_sequence < 1):
            raise EventLogContractError("initial sync receipt stage boundary")


@dataclass(frozen=True)
class InitialSyncReceiptSelection:
    """Closed IDs selected from sealed phase-one conversion evidence.

    The reader receives these six identities instead of inferring filenames or
    scanning for a conveniently shaped stream/receipt.  The selection itself
    contains no log, plan, marker, event, writer, or overlay authority.
    """

    verify_command_id: str
    consume_command_id: str
    durable_command_id: str
    acknowledge_command_id: str
    stream_artifact_id: str
    durable_artifact_id: str

    def __post_init__(self) -> None:
        values = (
            self.verify_command_id,
            self.consume_command_id,
            self.durable_command_id,
            self.acknowledge_command_id,
            self.stream_artifact_id,
            self.durable_artifact_id,
        )
        if any(type(value) is not str or _ID.fullmatch(value) is None for value in values):
            raise EventLogContractError("initial sync receipt selection")
        if len(set(values)) != len(values):
            raise EventLogContractError("initial sync receipt selection is ambiguous")

    @property
    def command_observation_ids(self) -> tuple[str, str, str, str]:
        return (
            self.verify_command_id,
            self.consume_command_id,
            self.durable_command_id,
            self.acknowledge_command_id,
        )


@dataclass(frozen=True)
class InitialSyncReceiptFacts:
    """Immutable receipt facts plus the exact sealed selection that proved them."""

    result_id: str
    corpus_revision: str
    event_counts: Mapping[str, int]
    effect_digest: str
    selection: InitialSyncReceiptSelection

    def __post_init__(self) -> None:
        counts = dict(sorted(dict(self.event_counts).items()))
        if (not re.fullmatch(r"sync_[0-9a-f]{64}", self.result_id)
                or type(self.corpus_revision) is not str or _SHA.fullmatch(self.corpus_revision) is None
                or type(self.effect_digest) is not str or _SHA.fullmatch(self.effect_digest) is None
                or type(self.selection) is not InitialSyncReceiptSelection
                or any(type(key) is not str or type(value) is not int or value < 0
                       for key, value in counts.items())):
            raise EventLogContractError("initial sync receipt facts")
        object.__setattr__(self, "event_counts", MappingProxyType(counts))


_TYPES = frozenset({
    "policy", "process", "transcript", "marker", "command_observation",
    "command_result", "approval", "diff", "assertion", "version", "help",
    "delivery", "mcp_config", "event_record", "fixture_capability", "run_manifest",
    "sync_stream", "consumption_receipt", "cursor_proof", "evidence_packet",
    "file_capture", "wiki_manifest", "fixture_descriptor", "source_record",
    "execution_trace", "source_state", "pending_sync_result",
    "client_executable", "workspace_snapshot",
    "phase_prompt", "interpretation_decision",
})
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
EMPTY_MCP_BYTES = b'{"mcpServers":{}}\n'
_EXECUTABLE_IDENTITY_FIELDS = frozenset({
    "device", "inode", "mode", "uid", "nlink", "bytes", "mtime_ns", "ctime_ns",
})
_COMMONMARK = MarkdownIt("commonmark")

# This is the one locally sourced Claude stream adapter/build pairing.  It is
# a digest, not a pathname: a runner may use a different installation path but
# cannot turn a self-reported version string into an approved build.
_DEFAULT_SUPPORTED_BUILDS = {
    (
        "claude",
        "2.1.251",
        "claude-stream-json-2.1.251-v1",
        "625869b01e0050f260b2980fac248fd9cef9e462612bded4ec9d3d49ff8969a5",
    ): "claude-2.1.251-37534ac5-stream-v1",
}


def _canonical_absolute_executable_path(value: object) -> str:
    if (type(value) is not str or not value.startswith("/") or value.startswith("//")
            or "\\" in value or "\0" in value):
        raise EventLogContractError("unsafe executable path")
    path = PurePosixPath(value)
    if value == "/" or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise EventLogContractError("unsafe executable path")
    return value


def _identity_value(value: object, *, label: str) -> dict[str, int]:
    if type(value) is not dict or set(value) != _EXECUTABLE_IDENTITY_FIELDS:
        raise EventLogContractError(label + " identity shape")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise EventLogContractError(label + " identity values")
    if value["nlink"] != 1 or not stat.S_ISREG(value["mode"]) or not value["mode"] & 0o111:
        raise EventLogContractError(label + " identity safety")
    return dict(value)


def _read_executable(path: str) -> tuple[bytes, dict[str, int]]:
    """Descriptor-pin every parent of a final no-follow executable read.

    A final ``O_NOFOLLOW`` alone is not sufficient: a symlink or writable
    ancestor can redirect the pathname before the final component is opened.
    The same conservative directory rules used for the private control root
    make that parent chain part of the prelaunch executable identity.
    """

    _canonical_absolute_executable_path(path)
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise EventLogContractError("secure executable descriptor support")
    owned: list[int] = []
    edges: list[tuple[int, int, str, tuple[int, int, int, int]]] = []
    try:
        parts = _relative_path(path[1:])
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC)
        owned.append(root)
        root_identity = _directory_identity(os.fstat(root))
        parent = root
        for part in parts[:-1]:
            before = os.stat(part, dir_fd=parent, follow_symlinks=False)
            identity = _directory_identity(before)
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC,
                dir_fd=parent,
            )
            owned.append(child)
            if (_directory_identity(os.fstat(child)) != identity
                    or _directory_identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) != identity):
                raise EventLogContractError("executable ancestor changed while opened")
            edges.append((parent, child, part, identity))
            parent = child
        fd, before = _open_child(parent, parts[-1], directory=False)
        owned.append(fd)
        _identity_value(_file_identity(before), label="trusted executable")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20, _MAX_EXECUTABLE_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_EXECUTABLE_BYTES:
                raise EventLogContractError("trusted executable exceeds size limit")
            chunks.append(chunk)
        after = os.fstat(fd)
        named = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if not _same(before, after) or not _same(after, named):
            raise EventLogContractError("trusted executable changed while read")
        identity = _identity_value(_file_identity(after), label="trusted executable")
        if _directory_identity(os.fstat(root)) != root_identity:
            raise EventLogContractError("executable root descriptor changed")
        for directory, child, name, expected in edges:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (_directory_identity(current) != expected
                    or _directory_identity(os.fstat(child)) != expected):
                raise EventLogContractError("executable ancestor replaced")
        return b"".join(chunks), identity
    except EventLogContractError:
        raise
    except OSError as error:
        raise EventLogContractError("trusted executable unavailable") from error
    finally:
        for fd in reversed(owned):
            os.close(fd)


@dataclass(frozen=True)
class TrustedExecutable:
    """An immutable host pre-launch expectation for one execution phase."""

    execution_id: str
    client: str
    resolved_path: Path | str
    identity: Mapping[str, int]
    sha256: str
    supported_build_id: str

    def __post_init__(self):
        if type(self.execution_id) is not str or not _ID.fullmatch(self.execution_id):
            raise EventLogContractError("trusted executable execution identity")
        if self.client not in {"claude", "codex"}:
            raise EventLogContractError("trusted executable client")
        path = _canonical_absolute_executable_path(os.fspath(self.resolved_path))
        identity = _identity_value(dict(self.identity), label="trusted executable")
        if not _sha(self.sha256) or type(self.supported_build_id) is not str or not _ID.fullmatch(self.supported_build_id):
            raise EventLogContractError("trusted executable identity")
        raw, observed = _read_executable(path)
        if observed != identity or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise EventLogContractError("trusted executable expectation is not prelaunch state")
        object.__setattr__(self, "resolved_path", Path(path))
        object.__setattr__(self, "identity", MappingProxyType(identity))


def _freeze_supported_builds(value: Mapping[tuple[str, str, str, str], str]) -> Mapping[tuple[str, str, str, str], str]:
    frozen: dict[tuple[str, str, str, str], str] = {}
    for key, build_id in value.items():
        if (type(key) is not tuple or len(key) != 4
                or key[0] not in {"claude", "codex"}
                or any(type(item) is not str or not item for item in key[1:])
                or not _sha(key[3]) or type(build_id) is not str or not _ID.fullmatch(build_id)):
            raise EventLogContractError("supported build registry")
        if key in frozen:
            raise EventLogContractError("duplicate supported build")
        frozen[key] = build_id
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class TrustedRunContext:
    """Runner-supplied, private evidence location and evaluated workspace."""

    control_root: Path
    workspace_root: Path
    trusted_executables: Mapping[str, TrustedExecutable] = dataclass_field(default_factory=dict)
    supported_builds: Mapping[tuple[str, str, str, str], str] = dataclass_field(
        default_factory=lambda: _DEFAULT_SUPPORTED_BUILDS
    )
    control_identity: tuple[int, int] = dataclass_field(init=False)
    workspace_identity: tuple[int, int] = dataclass_field(init=False)

    def __post_init__(self):
        # Construct this host context before launch and retain it through sealing.
        for path_name, identity_name in (("control_root", "control_identity"), ("workspace_root", "workspace_identity")):
            observed = os.lstat(getattr(self, path_name))
            object.__setattr__(self, identity_name, (observed.st_dev, observed.st_ino))
        trusted = dict(self.trusted_executables)
        if any(type(key) is not str or key != value.execution_id for key, value in trusted.items() if isinstance(value, TrustedExecutable)):
            raise EventLogContractError("trusted executable context binding")
        if any(not isinstance(value, TrustedExecutable) for value in trusted.values()):
            raise EventLogContractError("trusted executable context value")
        object.__setattr__(self, "trusted_executables", MappingProxyType(trusted))
        object.__setattr__(self, "supported_builds", _freeze_supported_builds(self.supported_builds))


class FilesystemTrustedEvidence:
    """Descriptor-backed compatibility facade; never accepts memory evidence."""

    def __init__(self, control_root: Path, workspace_root: Path):
        self.context = TrustedRunContext(control_root, workspace_root)

    def begin_run(self, log: dict[str, Any]) -> "_Control":
        return _Control(self.context, log)


def _canon(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_mode, a.st_uid, a.st_nlink, a.st_size,
            a.st_mtime_ns, a.st_ctime_ns) == (
        b.st_dev, b.st_ino, b.st_mode, b.st_uid, b.st_nlink, b.st_size,
        b.st_mtime_ns, b.st_ctime_ns,
    )


def _secure_directory(s: os.stat_result, *, owner: int) -> None:
    if (not stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode)
            or s.st_uid != owner or s.st_mode & 0o022):
        raise EventLogContractError("unsafe control directory")


def _component(name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."} or any(
        char in name for char in "/\\\0"
    ):
        raise EventLogContractError("unsafe control path component")
    return name


def _relative_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EventLogContractError("unsafe artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise EventLogContractError("unsafe artifact path")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise EventLogContractError("unsafe artifact path")
    return tuple(_component(part) for part in parts)


def _open_child(parent: int, name: str, *, directory: bool) -> tuple[int, os.stat_result]:
    name = _component(name)
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if directory:
            _secure_directory(before, owner=os.geteuid())
        elif (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
              or before.st_uid != os.geteuid() or before.st_mode & 0o022):
            raise EventLogContractError("unsafe control artifact")
        flags = os.O_RDONLY | os.O_NOFOLLOW | _CLOEXEC | _NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        fd = os.open(name, flags, dir_fd=parent)
    except EventLogContractError:
        raise
    except OSError as error:
        raise EventLogContractError("control artifact unavailable") from error
    try:
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same(before, after) or not _same(after, named):
            raise EventLogContractError("control path changed while opened")
        return fd, after
    except BaseException:
        os.close(fd)
        raise


def _read_child(parent: int, name: str, *, expected_bytes: int | None = None,
                expected_sha256: str | None = None, metadata: bool = False):
    if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes < 0
                                       or expected_bytes > _MAX_ARTIFACT_BYTES):
        raise EventLogContractError("unsafe artifact size")
    fd, before = _open_child(parent, name, directory=False)
    try:
        limit = _MAX_ARTIFACT_BYTES if expected_bytes is None else expected_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise EventLogContractError("artifact exceeds declared size")
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same(before, after) or not _same(after, named):
            raise EventLogContractError("artifact changed while read")
        if expected_bytes is not None and len(raw) != expected_bytes:
            raise EventLogContractError("artifact byte count")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise EventLogContractError("artifact digest")
        return (raw, after) if metadata else raw
    except OSError as error:
        raise EventLogContractError("control artifact unavailable") from error
    finally:
        os.close(fd)


def _json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (TypeError, ValueError) as error:
        raise EventLogContractError(f"{label} is not JSON") from error
    if type(value) is not dict:
        raise EventLogContractError(f"{label} must be an object")
    return value


class _Control:
    """Pinned directory-descriptor traversal of an allowlisted run."""

    def __init__(self, context: TrustedRunContext, log: dict[str, Any]):
        self.run_id = log["run_id"]
        if not _sha(self.run_id) or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
            raise EventLogContractError("secure descriptor support or run identity")
        self._owned_fds = []
        self._edges = []
        self._roots = []
        self.control_path = os.fspath(context.control_root)
        self.workspace_path = os.fspath(context.workspace_root)
        try:
            workspace, _ = self._pin_absolute(self.workspace_path)
            self.root, ancestry = self._pin_absolute(self.control_path)
            workspace_stat = os.fstat(workspace)
            root_stat = os.fstat(self.root)
            if ((workspace_stat.st_dev, workspace_stat.st_ino) != context.workspace_identity
                    or (root_stat.st_dev, root_stat.st_ino) != context.control_identity):
                raise EventLogContractError("configured root identity changed after context creation")
            if context.workspace_identity in ancestry:
                raise EventLogContractError("control root within workspace ancestry")
            _secure_directory(os.fstat(self.root), owner=os.geteuid())
            runs, runs_stat = _open_child(self.root, "runs", directory=True)
            self._owned_fds.append(runs)
            self._edges.append((self.root, runs, "runs", _directory_identity(runs_stat)))
            run, run_stat = _open_child(runs, log["run_id"], directory=True)
            self._owned_fds.append(run)
            self._edges.append((runs, run, log["run_id"], _directory_identity(run_stat)))
            self.run = run
            self.runs = runs
            self._validate_edges()
            index_raw = _read_child(self.run, "evidence-index.json")
            if hashlib.sha256(index_raw).hexdigest() != log["evidence_index_sha256"]:
                raise EventLogContractError("evidence index digest")
            index = _json(index_raw, "evidence index")
            if set(index) != {"schema_version", "run_id", "entries"} or type(index.get("schema_version")) is not int or index.get("schema_version") != 1 or index.get("run_id") != log["run_id"] or type(index.get("entries")) is not list:
                raise EventLogContractError("evidence index shape")
            self.entries: dict[str, dict[str, Any]] = {}
            indexed_paths = set()
            for entry in index["entries"]:
                if type(entry) is not dict or set(entry) != {"id", "type", "relative_path", "sha256", "bytes"}:
                    raise EventLogContractError("evidence index entry")
                if (type(entry["id"]) is not str or not _ID.fullmatch(entry["id"]) or type(entry["type"]) is not str or entry["type"] not in _TYPES
                        or not _sha(entry["sha256"]) or type(entry["bytes"]) is not int
                        or entry["bytes"] < 0 or entry["id"] in self.entries):
                    raise EventLogContractError("evidence index value")
                _relative_path(entry["relative_path"])
                if entry["relative_path"] in indexed_paths:
                    raise EventLogContractError("indexed artifact path alias")
                indexed_paths.add(entry["relative_path"])
                self.entries[entry["id"]] = entry
            self._validate_edges()
        except BaseException:
            self.close()
            raise

    def _pin_absolute(self, path):
        if type(path) is not str or not path.startswith("/") or path == "/" or str(PurePosixPath(path)) != path:
            raise EventLogContractError("configured path must be canonical absolute")
        parts = _relative_path(path[1:])
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC)
        self._owned_fds.append(fd)
        root_identity = _directory_identity(os.fstat(fd))
        self._roots.append((fd, root_identity))
        ancestry = {root_identity[:2]}
        for part in parts:
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            identity = _directory_identity(before)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC, dir_fd=fd)
            self._owned_fds.append(child)
            if (_directory_identity(os.fstat(child)) != identity
                    or _directory_identity(os.stat(part, dir_fd=fd, follow_symlinks=False)) != identity):
                raise EventLogContractError("configured ancestor changed while opened")
            self._edges.append((fd, child, part, identity))
            ancestry.add(identity[:2])
            fd = child
        self._validate_edges()
        return fd, ancestry

    def _validate_edges(self) -> None:
        for fd, expected in self._roots:
            if _directory_identity(os.fstat(fd)) != expected:
                raise EventLogContractError("root descriptor changed")
        for parent, child, name, expected in self._edges:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _directory_identity(current) != expected or _directory_identity(os.fstat(child)) != expected:
                raise EventLogContractError("control directory replaced")

    def read(self, artifact_id: str, kind: str) -> bytes:
        return self.snapshot(artifact_id, kind)[0]

    def snapshot(self, artifact_id: str, kind: str):
        if type(artifact_id) is not str:
            raise EventLogContractError("artifact ID type")
        entry = self.entries.get(artifact_id)
        if entry is None or entry["type"] != kind:
            raise EventLogContractError("untyped or unindexed evidence")
        self._validate_edges()
        parents: list[int] = []
        edges = []
        fd = self.run
        try:
            parts = _relative_path(entry["relative_path"])
            for part in parts[:-1]:
                next_fd, observed = _open_child(fd, part, directory=True)
                edges.append((fd, next_fd, part, observed))
                parents.append(next_fd)
                fd = next_fd
            raw, observed_file = _read_child(fd, parts[-1], expected_bytes=entry["bytes"], expected_sha256=entry["sha256"], metadata=True)
            for parent, child, name, observed in edges:
                if not _same(os.stat(name, dir_fd=parent, follow_symlinks=False), observed) or not _same(os.fstat(child), observed):
                    raise EventLogContractError("intermediate artifact directory replaced")
            self._validate_edges()
            canonical_path = str(PurePosixPath(self.control_path, "runs", self.run_id, *parts))
            return raw, canonical_path, _file_identity(observed_file)
        finally:
            for parent in reversed(parents):
                os.close(parent)

    def obj(self, artifact_id: str, kind: str) -> dict[str, Any]:
        return _json(self.read(artifact_id, kind), kind)

    def attestation(self) -> dict[str, Any]:
        self._validate_edges()
        raw = _read_child(self.run, "log-attestation.json")
        self._validate_edges()
        return _json(raw, "log attestation") | {"_sha256": hashlib.sha256(raw).hexdigest()}

    def fixed(self, name: str) -> dict[str, Any]:
        self._validate_edges()
        raw = _read_child(self.run, name)
        self._validate_edges()
        return _json(raw, name)

    def close(self) -> None:
        for fd in reversed(self._owned_fds):
            os.close(fd)
        self._owned_fds.clear()


def _directory_identity(value):
    if not stat.S_ISDIR(value.st_mode) or value.st_uid not in {0, os.geteuid()}:
        raise EventLogContractError("unsafe configured ancestor")
    if value.st_mode & 0o022 and not (value.st_uid == 0 and value.st_mode & stat.S_ISVTX):
        raise EventLogContractError("writable configured ancestor")
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _file_identity(value):
    return {"device": value.st_dev, "inode": value.st_ino, "mode": value.st_mode, "uid": value.st_uid,
            "nlink": value.st_nlink, "bytes": value.st_size, "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def _require_keys(value: dict[str, Any], keys: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != keys:
        raise EventLogContractError(f"{label} shape")


def _text(value: object, label: str = "text") -> str:
    if type(value) is not str or not value.strip() or any(ch in value for ch in "\0\r\n"):
        raise EventLogContractError(f"invalid {label}")
    return value


def _sha(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _captured_inventory(c, value: object, label: str) -> list[dict[str, Any]]:
    """Read a closed, content-addressed workspace inventory.

    Diffs and marker-time snapshots intentionally use the same inventory
    grammar.  Keeping the byte checks here means a snapshot cannot point at a
    later file capture with merely a matching-looking JSON claim.
    """

    if type(value) is not list:
        raise EventLogContractError(label + " inventory")
    inventory: dict[str, dict[str, Any]] = {}
    for entry in value:
        _require_keys(entry, {"path", "sha256", "bytes", "content_id"}, label + " inventory entry")
        _relative_path(entry["path"])
        if (not _sha(entry["sha256"]) or type(entry["bytes"]) is not int or entry["bytes"] < 0
                or type(entry["content_id"]) is not str or not _ID.fullmatch(entry["content_id"])
                or entry["path"] in inventory):
            raise EventLogContractError(label + " inventory identity")
        raw = c.read(entry["content_id"], "file_capture")
        if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise EventLogContractError(label + " inventory captured bytes")
        inventory[entry["path"]] = entry
    if list(inventory) != sorted(inventory):
        raise EventLogContractError(label + " inventory order")
    return list(value)


def _snapshot_staged_paths(value: object, inventory: list[dict[str, Any]], label: str) -> list[str]:
    if (type(value) is not list or any(type(path) is not str for path in value)
            or value != sorted(set(value))):
        raise EventLogContractError(label + " staged paths")
    for path in value:
        _relative_path(path)
    if not set(value) <= {entry["path"] for entry in inventory}:
        raise EventLogContractError(label + " staged inventory")
    return list(value)


def _workspace_snapshot(c, artifact_id: object, *, execution_id: str | None = None) -> dict[str, Any]:
    """Parse one immutable, indexed workspace instant.

    A snapshot is host evidence, never an agent-supplied field.  The cache is
    only a parsed view of descriptor-read bytes; subsequent graph joins still
    bind the snapshot to the particular native trace row that named it.
    """

    if type(artifact_id) is not str or not _ID.fullmatch(artifact_id):
        raise EventLogContractError("workspace snapshot reference")
    entry = getattr(c, "entries", {}).get(artifact_id)
    if entry is None or entry.get("type") != "workspace_snapshot":
        raise EventLogContractError("workspace snapshot reference")
    cache = getattr(c, "workspace_snapshots", None)
    if cache is None:
        cache = {}
        c.workspace_snapshots = cache
    value = cache.get(artifact_id)
    if value is None:
        value = c.obj(artifact_id, "workspace_snapshot")
        common = {"schema_version", "run_id", "execution_id", "anchor_kind", "trace_sequence", "inventory", "staged_paths"}
        native = common | {"transcript_id", "native_record_start", "native_record_end", "native_record_sha256"}
        if value.get("anchor_kind") == "phase_initial":
            _require_keys(value, common, "workspace snapshot")
        elif value.get("anchor_kind") == "native_record":
            _require_keys(value, native, "workspace snapshot")
        else:
            raise EventLogContractError("workspace snapshot anchor")
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["run_id"] != c.run_id or type(value["execution_id"]) is not str
                or not _ID.fullmatch(value["execution_id"]) or type(value["trace_sequence"]) is not int):
            raise EventLogContractError("workspace snapshot identity")
        inventory = _captured_inventory(c, value["inventory"], "workspace snapshot")
        _snapshot_staged_paths(value["staged_paths"], inventory, "workspace snapshot")
        if value["anchor_kind"] == "phase_initial":
            if value["trace_sequence"] != 0:
                raise EventLogContractError("workspace initial snapshot sequence")
        else:
            if (value["trace_sequence"] <= 0 or type(value["transcript_id"]) is not str
                    or not _ID.fullmatch(value["transcript_id"])
                    or type(value["native_record_start"]) is not int or value["native_record_start"] < 0
                    or type(value["native_record_end"]) is not int
                    or value["native_record_end"] <= value["native_record_start"]
                    or not _sha(value["native_record_sha256"])):
                raise EventLogContractError("workspace native snapshot tuple")
        cache[artifact_id] = value
    if execution_id is not None and value["execution_id"] != execution_id:
        raise EventLogContractError("workspace snapshot execution")
    return value


def _timestamp(value: object) -> None:
    text = _text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise EventLogContractError("timestamp") from error
    if parsed.utcoffset() is None:
        raise EventLogContractError("timestamp timezone")


def _envelope(result: dict[str, Any], command: str) -> None:
    _require_keys(result, {"ok", "command", "data", "warnings", "errors"}, "result envelope")
    if (result["ok"] is not True or type(result["command"]) is not str
            or result["command"] != command or type(result["data"]) is not dict
            or type(result["warnings"]) is not list or result["errors"] != []):
        raise EventLogContractError("unsuccessful or mismatched command result")
    for warning in result["warnings"]:
        _require_keys(warning, {"code", "message", "path", "details"}, "warning")
        _text(warning["code"])
        _text(warning["message"])
        if warning["path"] is not None:
            _relative_path(warning["path"])
        if type(warning["details"]) is not dict:
            raise EventLogContractError("warning details")


def _result(c: _Control, command_id: str, execution_id: str, run_id: str | None = None) -> tuple[list[str], dict[str, Any]]:
    observation = c.obj(command_id, "command_observation")
    _require_keys(observation, {"run_id", "execution_id", "argv", "exit_code", "result_id", "result_sha256", "timestamp", "start_sequence", "end_sequence", "source_state_id"}, "command observation")
    if (not _sha(observation["run_id"]) or observation["run_id"] != (c.run_id if run_id is None else run_id) or observation["execution_id"] != execution_id
            or type(observation["exit_code"]) is not int or observation["exit_code"] not in {0, 1} or type(observation["argv"]) is not list
            or not all(type(arg) is str for arg in observation["argv"])
            or observation["argv"][:2] != ["./brain", "--json"]
            or not _sha(observation["result_sha256"])):
        raise EventLogContractError("command observation")
    if (type(observation["start_sequence"]) is not int or type(observation["end_sequence"]) is not int
            or not 0 < observation["start_sequence"] < observation["end_sequence"]
            or observation["source_state_id"] is not None and (type(observation["source_state_id"]) is not str or not _ID.fullmatch(observation["source_state_id"]))):
        raise EventLogContractError("command ordering/state witness")
    _timestamp(observation["timestamp"])
    raw = c.read(observation["result_id"], "command_result")
    if hashlib.sha256(raw).hexdigest() != observation["result_sha256"]:
        raise EventLogContractError("command result digest")
    result = _json(raw, "command result")
    suffix = observation["argv"][2:]
    if not suffix:
        raise EventLogContractError("missing product command")
    command = " ".join(suffix[:2]) if suffix[0] in {"source", "wiki", "links", "eval"} else suffix[0]
    if command in {"sync", "init", "source snapshot-url"} or command == "eval mock-web-capture" and suffix[2:4] == ["--fixture-id", "web-approval-and-capture.initial"]:
        _semantic(semantic.parse_manifest_response, result, observed_exit_code=observation["exit_code"])
        if result["command"] != command:
            raise EventLogContractError("manifest command envelope mismatch")
    else:
        if observation["exit_code"] != 0:
            raise EventLogContractError("unsuccessful command observation")
        _envelope(result, command)
    return observation["argv"], result


def _manifest(result: dict[str, Any]) -> dict[str, Any]:
    parsed = _semantic(semantic.parse_manifest_response, result, observed_exit_code=0 if result.get("ok") is True else 1)
    return parsed.reference.to_dict()


def _semantic(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (semantic.SemanticEvidenceError, TypeError, ValueError, KeyError, UnicodeError) as error:
        raise EventLogContractError("production semantic evidence: " + str(error)) from error


def _source_state(c, command_id, *, result_id, revision):
    state = _semantic(semantic.read_source_state, c, command_id, result_id=result_id, revision=revision)
    registry = Path(__file__).parent / "fixtures/repository-development-not-archived/repo/config/extractors.toml"
    if state.files["config/extractors.toml"] != registry.read_bytes():
        raise EventLogContractError("captured registry differs from fixed fixture")
    return state


def _receipt_source_state(c, command_id, authority, role):
    """A receipt role derives identity and every optional copy from one origin."""
    state = _source_state(c, command_id, result_id=authority["result_id"], revision=authority["corpus_revision"])
    origin = authority["source_state"]
    if ({key: value.to_dict() for key, value in origin.records.items()}
            != {key: value.to_dict() for key, value in state.records.items()} or origin.files != state.files):
        raise EventLogContractError("receipt source state changed before consumption or replay")
    if state.pending_checkpoint is not None and (
            role == "acknowledge" or state.pending_checkpoint != origin.pending_checkpoint):
        raise EventLogContractError("pending checkpoint differs from validated receipt origin")
    return state


def _receipt_identity(verify: dict[str, Any], consume: dict[str, Any], acknowledge: dict[str, Any]) -> tuple[str, str, dict[str, int], str]:
    manifest = _manifest(verify)
    data = consume["data"]
    if set(data) != {"result_id", "status", "manifest_path", "corpus_revision", "event_counts", "effect_digest", "handoff_delivery"}:
        raise EventLogContractError("consume result shape")
    if any(type(data[key]) is not str for key in ("result_id", "status", "manifest_path", "corpus_revision", "effect_digest")):
        raise EventLogContractError("consumption scalar types")
    try:
        reference = SyncResultReference.from_dict(manifest)
        delivery = None if data["handoff_delivery"] is None else HandoffDeliveryReference.from_dict(data["handoff_delivery"])
        SyncResultConsumption(data["status"], reference, data["event_counts"], data["effect_digest"], delivery)
    except (TypeError, ValueError) as error:
        raise EventLogContractError("production consumption receipt") from error
    if (data["manifest_path"] != reference.path.as_posix() or data["result_id"] != reference.result_id
            or data["corpus_revision"] != reference.corpus_revision):
        raise EventLogContractError("consume manifest path")
    ack_data = acknowledge["data"]
    if (set(ack_data) != {"result_id", "sync_command", "status"}
            or any(type(ack_data[key]) is not str for key in ack_data)
            or ack_data["result_id"] != reference.result_id
            or ack_data["sync_command"] != verify.get("command")
            or ack_data["status"] not in {"acknowledged", "already_acknowledged"}):
        raise EventLogContractError("acknowledgement result identity")
    return manifest["result_id"], manifest["corpus_revision"], manifest["event_counts"], data["effect_digest"]


def _validate_marker(c: _Control, event: dict[str, Any], execution: dict[str, Any], run_id: str) -> None:
    marker = c.obj(event["transcript_marker_id"], "marker")
    _require_keys(marker, {"run_id", "execution_id", "transcript_id", "marker", "byte_start", "byte_end", "excerpt_sha256", "native_record_start", "native_record_end", "native_record_sha256", "text_pointer"}, "marker")
    if (marker["run_id"] != run_id or marker["execution_id"] != event["execution_id"]
            or marker["transcript_id"] != execution["transcript_id"] or type(marker["marker"]) is not str
            or type(marker["byte_start"]) is not int or type(marker["byte_end"]) is not int
            or marker["byte_start"] < 0 or marker["byte_end"] <= marker["byte_start"]
            or not _sha(marker["excerpt_sha256"]) or type(marker["native_record_start"]) is not int
            or type(marker["native_record_end"]) is not int or not _sha(marker["native_record_sha256"])):
        raise EventLogContractError("marker shape")
    native = c.native_records[event["execution_id"]].get(marker["native_record_start"])
    if native is None or native["end"] != marker["native_record_end"] or native["sha256"] != marker["native_record_sha256"]:
        raise EventLogContractError("marker native transcript binding")
    text = native["texts"].get(marker["text_pointer"])
    if text is None:
        raise EventLogContractError("marker is not direct assistant text")
    raw = text.encode("utf-8")
    excerpt = raw[marker["byte_start"]:marker["byte_end"]]
    exact = ("EVENT:" + event["name"] + "\n").encode()
    if (marker["byte_end"] > len(raw) or hashlib.sha256(excerpt).hexdigest() != marker["excerpt_sha256"]
            or excerpt != marker["marker"].encode("utf-8") or excerpt != exact
            or marker["byte_start"] and raw[marker["byte_start"] - 1:marker["byte_start"]] != b"\n"):
        raise EventLogContractError("marker transcript binding")
    return marker


@dataclass(frozen=True)
class EventRule:
    """Closed mode and required indexed-artifact references for one event."""

    mode: str
    references: tuple[tuple[str, str], ...] = ()


EVENT_RULES = {
    **{name: EventRule("marker_diff", (("diff_id", "diff"),)) for name in (
        "classify_repository_development", "use_software_workflow",
        "stage_handoff_scoped_extraction", "stage_existing_question_update",
        "write_wiki_question", "preserve_both_claims", "cite_both_sides",
    )},
    "select_used_source": EventRule("marker_diff", (("diff_id", "diff"), ("capability_id", "fixture_capability"))),
    **{name: EventRule("marker", (("packet_id", "evidence_packet"),)) for name in (
        "revalidate_underlying_citations", "build_wiki_evidence_packet",
        "judge_sufficient", "judge_insufficient",
    )},
    "wiki_no_supported_evidence": EventRule("marker", (("cursor_proof_id", "cursor_proof"),)),
    "report_local_evidence_gap": EventRule("marker"),
    "ask_web_approval": EventRule("marker_approval", (("approval_id", "approval"),)),
    "ask_interpretation_approval": EventRule("marker_approval", (
        ("approval_id", "approval"), ("interpretation_decision_id", "interpretation_decision"),
    )),
    **{name: EventRule("marker_delivery") for name in (
        "branch_extraction_handoff", "branch_rendered_web_capture_handoff",
    )},
    **{name: EventRule("product_cli") for name in (
        "freshness_search_batched", "source_pass_discovery", "source_pass_expansion",
        "source_pass_verification", "register_extraction_handoff",
        "verify_registration_active_representation", "rendered_snapshot_url",
        "verify_snapshot_active_representation", "links_check", "validate",
    )},
    **{name: EventRule("product_cli", (("cursor_proof_id", "cursor_proof"),)) for name in (
        "freshness_search_drained", "wiki_search_drained", "source_pass_discovery_drained",
        "source_pass_expansion_drained", "source_pass_verification_drained", "link_candidates_drained",
    )},
    "wiki_reconcile_citations": EventRule("product_cli", (("manifest_id", "wiki_manifest"),)),
    **{name: EventRule("product_cli", (("manifest_id", "wiki_manifest"), ("diff_id", "diff"))) for name in (
        "wiki_apply", "persist_claim",
    )},
    **{name: EventRule("fixture_eval", (("approval_id", "approval"), ("capability_id", "fixture_capability"))) for name in (
        "public_web_access", "snapshot_used_source", "stage_faithful_browser_capture",
    )},
}


# Assertions are anchored to the scenario's semantic completion point, not a
# conveniently latest transcript row.  This closed map is deliberately kept
# next to the event grammar so adding a scenario requires choosing its terminal
# proof explicitly.
_ASSERTION_TERMINAL_EVENTS = MappingProxyType({
    "empty-wiki-first-question": "validate",
    "current-wiki-fast-path": "validate",
    "new-binary-before-question": "validate",
    "web-approval-and-capture": "persist_claim",
    "contradictory-evidence": "validate",
    "repository-development-not-archived": "use_software_workflow",
})
for _operation in ("initial_sync", "post_registration_sync", "init", "initial_snapshot", "rendered_snapshot"):
    for _stage in ("verified", "consumed", "durable", "acknowledged"):
        EVENT_RULES[f"{_operation}_receipt_{_stage}"] = EventRule("product_cli")
    EVENT_RULES[f"{_operation}_handoff_delivery_verified"] = EventRule("marker_delivery")


def _event_command(name: str, argv: list[str], result: dict[str, Any]) -> None:
    """Validate a command's closed syntax; joins are checked across the event run."""
    args = argv[2:]
    if name.endswith("_receipt_verified"):
        operation = name.removesuffix("_receipt_verified")
        if operation in {"initial_sync", "post_registration_sync", "init"}:
            if args != (["init"] if operation == "init" else ["sync"]):
                raise EventLogContractError("receipt verify command form")
            _manifest(result)
        elif operation == "initial_snapshot":
            _fixture_argv(argv, rendered=False)
            _manifest(_snapshot_product(result))
        else:
            _rendered_argv(argv)
            _manifest(result)
    elif "_receipt_" in name:
        command = "acknowledge-sync-result" if name.endswith("_acknowledged") else "consume-sync-result"
        if (len(args) != 4 or args[:3] != ["source", command, "--result-id"]
                or re.fullmatch(r"sync_[0-9a-f]{64}", args[3]) is None):
            raise EventLogContractError("receipt command form")
    elif name in {"wiki_apply", "persist_claim", "wiki_reconcile_citations"}:
        if len(args) != 4 or args[:3] != ["wiki", "apply", "--manifest"] or not re.fullmatch(
                r"\.brain/wiki-staging/[A-Za-z0-9][A-Za-z0-9_.-]*/manifest\.json", args[3]):
            raise EventLogContractError("wiki apply command form")
        data = result["data"]
        _require_keys(data, {"corpus_revision", "changed_paths", "index_path", "recovered"}, "wiki apply data")
        if (not _sha(data["corpus_revision"]) or type(data["recovered"]) is not bool
                or type(data["changed_paths"]) is not list or data["index_path"] != "wiki/index.md"
                or data["changed_paths"] != sorted(set(data["changed_paths"]))):
            raise EventLogContractError("wiki apply result")
        for path in data["changed_paths"]:
            if path != data["index_path"]:
                _wiki_path(path)
        if name != "wiki_reconcile_citations" and not any(path != data["index_path"] for path in data["changed_paths"]):
            raise EventLogContractError("wiki apply has no changes")
    elif name == "links_check":
        if args != ["links", "check"]:
            raise EventLogContractError("links check command form")
        _require_keys(result["data"], {"report"}, "links check data")
        _report(result["data"]["report"])
    elif name == "validate":
        if args != ["validate"]:
            raise EventLogContractError("validate command form")
        data = result["data"]
        _require_keys(data, {"reports"}, "validate result")
        if type(data.get("reports")) is not list or not data["reports"]:
            raise EventLogContractError("validation reports")
        for report in data["reports"]:
            _report(report)
    elif name in {"register_extraction_handoff", "verify_registration_active_representation"}:
        flags = ("--handoff-id", "--staging-path", "--anchors-json", "--quality-state", "--note")
        if len(args) != 12 or args[:2] != ["source", "register-extraction"] or tuple(args[2::2]) != flags:
            raise EventLogContractError("registration command form")
        if not re.fullmatch(r"hnd_[0-9a-f]{64}", args[3]) or args[9] not in {"ok", "warning"}:
            raise EventLogContractError("registration identity")
        _scoped_path(args[5], ".brain/agent-staging/" + args[3] + "/")
        _text(args[11])
        try:
            anchors = json.loads(args[7])
        except ValueError as error:
            raise EventLogContractError("registration anchors") from error
        data = result["data"]
        _require_keys(data, {"registration"}, "registration result")
        registration = data["registration"]
        _require_keys(registration, {"source_id", "content_sha256", "derivation_id", "output_path", "active_representation", "corpus_revision"}, "registration")
        representation = _representation(registration["active_representation"])
        if (not _sha(registration["corpus_revision"])
                or any(registration[key] != representation[key] for key in ("source_id", "content_sha256", "derivation_id"))
                or registration["output_path"] != representation["extracted_path"]
                or anchors != representation["anchors"]):
            raise EventLogContractError("registration active representation")
    elif name in {"rendered_snapshot_url", "verify_snapshot_active_representation"}:
        _rendered_argv(argv)
        reference = _manifest(result)
        snapshot = result["data"].get("snapshot")
        _snapshot(snapshot, reference, active=name == "verify_snapshot_active_representation")
    elif name in {"public_web_access", "snapshot_used_source", "stage_faithful_browser_capture"}:
        _fixture_argv(argv, rendered=name == "stage_faithful_browser_capture")
        if name != "stage_faithful_browser_capture":
            _manifest(_snapshot_product(result))
        else:
            _require_keys(result["data"], {"fixture_id", "handoff_id", "path", "sha256", "bytes"}, "rendered staging")
            data = result["data"]
            if (data["fixture_id"] != args[3] or data["handoff_id"] != args[5]
                    or not _sha(data["sha256"]) or type(data["bytes"]) is not int or data["bytes"] <= 0):
                raise EventLogContractError("rendered staging result")
            _scoped_path(data["path"], ".brain/web-staging/" + args[5] + "/")
    elif name.endswith("_drained"):
        # The entire start/continuation chain, including final argv, is checked below.
        _search_page(result["data"], links=name == "link_candidates_drained")
    elif name.startswith("source_pass_") or name == "freshness_search_batched":
        _search_start(name, argv, result["data"])
    else:
        raise EventLogContractError("event has no command rule")


def _validate_event_record(c: _Control, event: dict[str, Any], run_id: str) -> dict[str, Any]:
    rule = EVENT_RULES.get(event["name"])
    if rule is None or event.get("evidence_mode") != rule.mode:
        raise EventLogContractError("unknown event or wrong evidence mode")
    record = c.obj(event["event_record_id"], "event_record")
    keys = {"run_id", "execution_id", "event_name", "marker_id", "evidence_mode"}
    keys |= {key for key, _ in rule.references}
    commanded = rule.mode in {"product_cli", "fixture_eval"}
    if commanded:
        keys |= {"command_id", "argv_sha256", "result_sha256"}
    _require_keys(record, keys, "event record")
    if (record["run_id"] != run_id or record["execution_id"] != event["execution_id"]
            or record["event_name"] != event["name"] or record["marker_id"] != event["transcript_marker_id"]
            or record["evidence_mode"] != rule.mode):
        raise EventLogContractError("event record binding")
    for key, kind in rule.references:
        _text(record[key], key)
        c.read(record[key], kind)
    if commanded:
        if event["corroboration_ids"] != [record["command_id"]]:
            raise EventLogContractError("event command binding")
        argv, result = _result(c, record["command_id"], event["execution_id"])
        if (_canon(argv) != record["argv_sha256"] or _canon(result) != record["result_sha256"]):
            raise EventLogContractError("event-specific command evidence")
        _event_command(event["name"], argv, result)
    elif event["corroboration_ids"]:
        raise EventLogContractError("non-command mode cannot contain command evidence")
    if "_receipt_" in event["name"]:
        _require_keys(event["data"], {"receipt_id"}, "receipt event")
    elif rule.mode == "marker_delivery":
        expected = {"delivery_id", "item_mappings"} if event["name"].endswith("_verified") else {"delivery_id", "item_id", "handoff_id", "handoff_source_id"}
        _require_keys(event["data"], expected, "delivery event")
    elif event["data"] != {}:
        raise EventLogContractError("event payload must be empty")
    return record


def _wiki_path(value):
    parts = _relative_path(value)
    if len(parts) < 3 or parts[:2] not in {("wiki", "questions"), ("wiki", "pages")} or not value.endswith(".md"):
        raise EventLogContractError("wiki target path")
    return value


def _scoped_path(value, prefix):
    _relative_path(value)
    if not value.startswith(prefix) or value == prefix:
        raise EventLogContractError("staging path scope")
    return value


def _report(value):
    _require_keys(value, {"checks", "issues", "corpus_revision"}, "validation report")
    if (type(value["checks"]) is not list or not all(type(item) is str for item in value["checks"])
            or type(value["issues"]) is not list or value["corpus_revision"] is not None and not _sha(value["corpus_revision"])):
        raise EventLogContractError("validation report types")
    for issue in value["issues"]:
        _require_keys(issue, {"severity", "code", "message", "path", "details"}, "validation issue")
        if issue["severity"] != "warning" or type(issue["details"]) is not dict:
            raise EventLogContractError("validation issue")
        _text(issue["code"])
        _text(issue["message"])
        if issue["path"] is not None:
            _relative_path(issue["path"])


def _terms(args):
    if not args or len(args) % 2 or args[::2] != ["--term"] * (len(args) // 2):
        raise EventLogContractError("exact repeated search terms")
    terms = args[1::2]
    for term in terms:
        _text(term, "search term")
    if len(set(terms)) != len(terms):
        raise EventLogContractError("duplicate terms")
    return terms


def _search_page(data, *, links=False):
    common = {"run_id", "corpus_revision", "terms", "page_index", "request_cursor", "next_cursor", "complete", "candidate_count", "candidate_manifest_sha256", "result_sha256", "coverage_gaps"}
    extra = {"page_path", "candidates"} if links else {"scope", "mode", "pass_name", "candidate_manifest", "matches", "searched_source_ids"}
    _require_keys(data, common | extra, "cursor result")
    if (not _sha(data["corpus_revision"]) or not _sha(data["candidate_manifest_sha256"])
            or not _sha(data["result_sha256"]) or type(data["complete"]) is not bool
            or type(data["page_index"]) is not int or data["page_index"] < 0
            or type(data["candidate_count"]) is not int or data["candidate_count"] < 0
            or data["coverage_gaps"] != [] or type(data["terms"]) is not list):
        raise EventLogContractError("cursor result types or coverage")
    _text(data["run_id"], "search run")
    _terms([part for term in data["terms"] for part in ("--term", term)])
    for key in ("request_cursor", "next_cursor"):
        if data[key] is not None:
            _text(data[key], key)
    if data["complete"] != (data["next_cursor"] is None):
        raise EventLogContractError("cursor completeness")
    records = data["candidates" if links else "matches"]
    if type(records) is not list:
        raise EventLogContractError("cursor records")
    for record in records:
        keys = {"path", "line", "matched_term", "context", "kind"} if links else {"path", "line_number", "text", "kind"}
        _require_keys(record, keys, "cursor match")
        _relative_path(record["path"])
        line = record["line" if links else "line_number"]
        if type(line) is not int or line < 1 or type(record["kind"]) is not str:
            raise EventLogContractError("cursor match types")
        for key in ("matched_term", "context") if links else ("text",):
            if type(record[key]) is not str:
                raise EventLogContractError("cursor match text")
    if links:
        _wiki_path(data["page_path"])
    else:
        _relative_path(data["candidate_manifest"])
        if data["scope"] not in {"sources", "wiki"} or data["mode"] not in {"research", "freshness"} or data["pass_name"] not in {None, "discovery", "expansion", "verification"}:
            raise EventLogContractError("search result scope")
        ids = data["searched_source_ids"]
        if type(ids) is not list or any(type(item) is not str or not re.fullmatch(r"src_[0-9a-f]{64}", item) for item in ids) or len(set(ids)) != len(ids):
            raise EventLogContractError("searched source IDs")
    return data


def _search_start(name, argv, data):
    args = argv[2:]
    links = name == "link_candidates"
    _search_page(data, links=links)
    if data["page_index"] != 0 or data["request_cursor"] is not None:
        raise EventLogContractError("search start page")
    if links:
        if args[:2] != ["links", "candidates"] or len(args) < 5:
            raise EventLogContractError("link candidate start")
        if _wiki_path(args[2]) != data["page_path"] or _terms(args[3:]) != data["terms"]:
            raise EventLogContractError("link candidate target")
        return
    if name == "wiki_search":
        if args[:3] != ["search", "--scope", "wiki"] or _terms(args[3:]) != data["terms"] or data["scope"] != "wiki" or data["pass_name"] is not None or data["mode"] != "research" or data["searched_source_ids"]:
            raise EventLogContractError("wiki search start")
    elif name == "freshness_search_batched":
        if args[:4] != ["search", "--scope", "sources", "--freshness"]:
            raise EventLogContractError("freshness start")
        offset, ids = 4, []
        while offset + 1 < len(args) and args[offset] == "--source-id":
            ids.append(args[offset + 1])
            offset += 2
        if (not ids or ids != sorted(set(ids)) or ids != data["searched_source_ids"]
                or _terms(args[offset:]) != data["terms"] or data["scope"] != "sources"
                or data["mode"] != "freshness" or data["pass_name"] is not None):
            raise EventLogContractError("freshness result identity")
    else:
        pass_name = name.removeprefix("source_pass_")
        if (pass_name not in {"discovery", "expansion", "verification"}
                or args[:5] != ["search", "--scope", "sources", "--pass", pass_name]
                or args[-2:] != ["--context", "3"] or _terms(args[5:-2]) != data["terms"]
                or data["scope"] != "sources" or data["mode"] != "research" or data["pass_name"] != pass_name):
            raise EventLogContractError("source pass start")


def _cursor(c, proof_id, execution_id, family, primary=None):
    proof = c.obj(proof_id, "cursor_proof")
    _require_keys(proof, {"run_id", "execution_id", "family", "command_ids"}, "cursor proof")
    ids = proof["command_ids"]
    if (proof["run_id"] != c.run_id or proof["execution_id"] != execution_id or proof["family"] != family
            or type(ids) is not list or not ids or any(type(item) is not str for item in ids)
            or len(set(ids)) != len(ids) or primary is not None and ids[-1] != primary):
        raise EventLogContractError("cursor proof binding")
    links = family == "link_candidates"
    results = []
    previous_end = 0
    for index, cid in enumerate(ids):
        argv, result = _result(c, cid, execution_id)
        observation = c.obj(cid, "command_observation")
        if observation["start_sequence"] <= previous_end:
            raise EventLogContractError("cursor command ordering")
        previous_end = observation["end_sequence"]
        _envelope(result, "links candidates" if links else "search")
        data = _search_page(result["data"], links=links)
        if index == 0:
            _search_start(family, argv, data)
        else:
            expected = ["./brain", "--json", *(["links", "candidates"] if links else ["search"]), "--cursor", results[-1]["next_cursor"]]
            stable = ["run_id", "corpus_revision", "terms", "candidate_count", "candidate_manifest_sha256", "result_sha256"]
            stable += ["page_path"] if links else ["scope", "mode", "pass_name", "searched_source_ids", "candidate_manifest"]
            if (argv != expected or results[-1]["complete"] or data["request_cursor"] != results[-1]["next_cursor"]
                    or any(data[key] != results[0][key] for key in stable)):
                raise EventLogContractError("cursor chain identity")
        if data["page_index"] != index or data["complete"] != (index == len(ids) - 1):
            raise EventLogContractError("cursor chain incomplete or out of order")
        results.append(data)
    return {"family": family, "command_ids": ids, "page_count": len(results), "first": results[0], "last": results[-1],
            "records": [match for page in results for match in page["candidates" if links else "matches"]]}


def _representation(value):
    _semantic(semantic.parse_active_representation, value)
    _require_keys(value, {"source_id", "content_sha256", "derivation_id", "raw_path", "extracted_path", "output_sha256", "quality_state", "anchors"}, "active representation")
    if (type(value["source_id"]) is not str or not re.fullmatch(r"src_[0-9a-f]{64}", value["source_id"])
            or type(value["derivation_id"]) is not str or not re.fullmatch(r"drv_[0-9a-f]{64}", value["derivation_id"])
            or not _sha(value["content_sha256"]) or not _sha(value["output_sha256"])
            or value["quality_state"] not in {"ok", "warning"} or type(value["anchors"]) is not list or not value["anchors"]):
        raise EventLogContractError("active representation identity")
    _relative_path(value["raw_path"])
    _scoped_path(value["extracted_path"], "sources/extracted/")
    for anchor in value["anchors"]:
        _require_keys(anchor, {"kind", "value"}, "anchor")
        if anchor["kind"] not in {"line", "page", "slide", "sheet", "section", "row", "block"}:
            raise EventLogContractError("anchor kind")
        _text(anchor["value"])
    return value


def _snapshot(value, reference, *, active):
    from brainlib.contracts import _parse_content_version, _parse_retrieval

    _semantic(semantic.snapshot_data, value, SimpleNamespace(corpus_revision=reference["corpus_revision"]))

    _require_keys(value, {"source_id", "raw_path", "content_sha256", "source_version", "retrieval", "extraction_result", "active_representation", "corpus_revision"}, "snapshot")
    if (not _sha(value["content_sha256"]) or type(value["source_id"]) is not str
            or not re.fullmatch(r"src_[0-9a-f]{64}", value["source_id"])
            or value["corpus_revision"] != reference["corpus_revision"]):
        raise EventLogContractError("snapshot identity")
    _relative_path(value["raw_path"])
    try:
        version = _parse_content_version(value["source_version"])
        retrieval = _parse_retrieval(value["retrieval"])
    except (ValueError, TypeError) as error:
        raise EventLogContractError("snapshot nested production types") from error
    if (version.sha256 != value["content_sha256"] or version.raw_path.as_posix() != value["raw_path"]
            or retrieval.sha256 != value["content_sha256"] or retrieval not in version.retrieval_events):
        raise EventLogContractError("snapshot version/retrieval identity")
    extraction = value["extraction_result"]
    if extraction is not None:
        _require_keys(extraction, {"state", "derivation", "attempt", "diagnostics"}, "snapshot extraction")
        if type(extraction["state"]) is not str or type(extraction["attempt"]) is not dict or type(extraction["diagnostics"]) is not list:
            raise EventLogContractError("snapshot extraction types")
    if active or value["active_representation"] is not None:
        representation = _representation(value["active_representation"])
        if any(representation[key] != value[key] for key in ("source_id", "content_sha256")):
            raise EventLogContractError("snapshot active representation")
        if extraction is not None and (type(extraction["derivation"]) is not dict
                or extraction["derivation"].get("derivation_id") != representation["derivation_id"]
                or extraction["derivation"].get("output_sha256") != representation["output_sha256"]):
            raise EventLogContractError("snapshot extraction activation identity")
    return value


def _fixture_argv(argv, *, rendered):
    args = argv[2:]
    prefix = ["eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture." + ("rendered" if rendered else "initial")]
    flags = ["--handoff-id"] if rendered else ["--approval-event-id", "--approval-scope", "--approval-note"]
    if args[:4] != prefix or len(args) != 4 + 2 * len(flags) or args[4::2] != flags:
        raise EventLogContractError("fixture-only exact command form")
    for item in args:
        _text(item)
        if re.search(r"(?i)(https?://|file:|socket:|javascript:)", item):
            raise EventLogContractError("raw URL fixture input")
    if rendered and not re.fullmatch(r"hnd_[0-9a-f]{64}", args[5]):
        raise EventLogContractError("rendered handoff identity")
    return dict(zip(args[4::2], args[5::2]))


def _rendered_argv(argv):
    args = argv[2:]
    flags = ["--source-id", "--rendered-staging-path", "--handoff-id", "--retrieved-at", "--final-url", "--detected-media-type", "--approval-event-id", "--approval-scope", "--approval-note"]
    if (args[:2] != ["source", "snapshot-url"] or len(args) < 20 or len(args) % 2
            or args[2:20:2] != flags or any(flag != "--redirect-url" for flag in args[20::2])):
        raise EventLogContractError("rendered snapshot command form")
    values = dict(zip(args[2:20:2], args[3:20:2]))
    for item in values.values():
        _text(item)
    if not re.fullmatch(r"src_[0-9a-f]{64}", values["--source-id"]) or not re.fullmatch(r"hnd_[0-9a-f]{64}", values["--handoff-id"]):
        raise EventLogContractError("rendered snapshot selectors")
    _scoped_path(values["--rendered-staging-path"], ".brain/web-staging/" + values["--handoff-id"] + "/")
    _timestamp(values["--retrieved-at"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", values["--retrieved-at"]):
        raise EventLogContractError("rendered retrieval UTC")
    values["redirect_urls"] = args[21::2]
    return values


def _diff(c, diff_id, execution_id=None, *, require_snapshot_bindings=False):
    """Validate a captured diff and, when present, its immutable endpoints.

    The no-binding form remains available to small unit helpers that do not
    claim a public pass.  Every pass-facing caller opts into the closed
    workspace-snapshot form below.
    """

    value = c.obj(diff_id, "diff")
    base_keys = {"run_id", "execution_id", "before", "after", "changed_paths", "staged_paths"}
    snapshot_keys = base_keys | {"before_snapshot_id", "after_snapshot_id"}
    if frozenset(value) not in {frozenset(base_keys), frozenset(snapshot_keys)}:
        raise EventLogContractError("diff shape")
    has_snapshot_bindings = set(value) == snapshot_keys
    if require_snapshot_bindings and not has_snapshot_bindings:
        raise EventLogContractError("diff workspace snapshot binding")
    if value["run_id"] != c.run_id or execution_id is not None and value["execution_id"] != execution_id:
        raise EventLogContractError("diff run/execution")
    inventories: list[list[dict[str, Any]]] = []
    for side in ("before", "after"):
        inventories.append(_captured_inventory(c, value[side], "diff"))
    before, after = ({entry["path"]: entry for entry in inventory} for inventory in inventories)
    paths = sorted(path for path in before.keys() | after.keys()
                   if (before.get(path, {}).get("sha256"), before.get(path, {}).get("bytes"))
                   != (after.get(path, {}).get("sha256"), after.get(path, {}).get("bytes")))
    if (value["changed_paths"] != paths or type(value["staged_paths"]) is not list
            or value["staged_paths"] != sorted(set(value["staged_paths"]))
            or not set(value["staged_paths"]) <= set(paths)):
        raise EventLogContractError("diff changed/staged inventory mismatch")
    if has_snapshot_bindings:
        before_snapshot = _workspace_snapshot(c, value["before_snapshot_id"], execution_id=value["execution_id"])
        after_snapshot = _workspace_snapshot(c, value["after_snapshot_id"], execution_id=value["execution_id"])
        if (value["before"] != before_snapshot["inventory"]
                or value["after"] != after_snapshot["inventory"]
                or value["staged_paths"] != after_snapshot["staged_paths"]
                or before_snapshot["trace_sequence"] >= after_snapshot["trace_sequence"]
                or after_snapshot["anchor_kind"] != "native_record"):
            raise EventLogContractError("diff workspace snapshot content binding")
    return value


def _marker_workspace_snapshot(c, event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the one trace-bound snapshot for an already validated marker."""

    marker = c.obj(event["transcript_marker_id"], "marker")
    try:
        native = c.native_records[event["execution_id"]][marker["native_record_start"]]
        snapshot_id = native["workspace_snapshot_id"]
    except (KeyError, TypeError) as error:
        raise EventLogContractError("marker workspace snapshot binding") from error
    snapshot = _workspace_snapshot(c, snapshot_id, execution_id=event["execution_id"])
    if snapshot["anchor_kind"] != "native_record":
        raise EventLogContractError("marker workspace snapshot anchor")
    return snapshot_id, snapshot


def _event_diff(c, diff_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """A diff witness is useful only when it ends at this exact marker row."""

    value = _diff(c, diff_id, event["execution_id"], require_snapshot_bindings=True)
    snapshot_id, _ = _marker_workspace_snapshot(c, event)
    if value["after_snapshot_id"] != snapshot_id:
        raise EventLogContractError("marker diff workspace snapshot binding")
    return value


def _exact_profile(policy: dict[str, Any], context: TrustedRunContext, client: str, executable_path: str,
                   phase_prompt: bytes) -> None:
    workspace = os.fspath(context.workspace_root)
    argv = policy["argv"]
    codex = [executable_path, "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false", "-C", workspace, "-"]
    if client == "codex":
        if policy["profile"] != "codex-workspace-egress-denied-v1" or argv != codex or policy["network_attestation"] != "workspace-egress-denied":
            raise EventLogContractError("Codex policy profile")
        return
    fixed = [executable_path, "--print", "--output-format", "stream-json", "--restricted", "--strict-mcp-config", "--mcp-config"]
    tail = ["--no-chrome", "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)", "--verbose"]
    try:
        decoded_prompt = phase_prompt.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise EventLogContractError("phase prompt encoding") from error
    if (policy["profile"] != "claude-restricted-tool-surface-v1" or policy["network_attestation"] != "mock-only"
            or argv[:len(fixed)] != fixed or len(argv) != len(fixed) + 1 + len(tail) + 1
            or argv[len(fixed) + 1:-1] != tail or not argv[len(fixed)] or argv[-1] != decoded_prompt):
        raise EventLogContractError("Claude policy profile")


def _reported_version(raw: bytes) -> str:
    """Normalize only the version token which a selected adapter understands."""

    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise EventLogContractError("client version output encoding") from error
    match = re.search(r"(?<![0-9A-Za-z._-])v?(\d+(?:\.\d+)+)(?![0-9A-Za-z._-])", text)
    if match is None:
        raise EventLogContractError("client version output")
    return match.group(1)


def _client_probe(c: _Control, executable: dict[str, Any], *, field: str, artifact_kind: str,
                  expected_id: str, resolved_path: str) -> bytes:
    probe = executable[field]
    _require_keys(probe, {"argv", "exit_code", "output_id", "output_sha256"}, field)
    flag = "--version" if field == "version_probe" else "--help"
    if (type(probe["argv"]) is not list or probe["argv"] != [resolved_path, flag]
            or type(probe["exit_code"]) is not int or probe["exit_code"] != 0
            or probe["output_id"] != expected_id or not _sha(probe["output_sha256"])):
        raise EventLogContractError("client executable " + field)
    raw = c.read(expected_id, artifact_kind)
    if (not raw or c.entries[expected_id]["sha256"] != probe["output_sha256"]
            or hashlib.sha256(raw).hexdigest() != probe["output_sha256"]):
        raise EventLogContractError("client executable " + field + " output")
    return raw


def _validate_client_executable(
    c: _Control,
    execution: dict[str, Any],
    log: dict[str, Any],
    context: TrustedRunContext,
    policy: dict[str, Any],
    process: dict[str, Any],
    *,
    seen_artifacts: set[str],
    seen_probe_artifacts: set[str],
) -> str:
    """Join host prelaunch trust to sealed executable/probe/process evidence."""

    executable_id = policy["executable_id"]
    if (type(executable_id) is not str or not _ID.fullmatch(executable_id)
            or process["executable_id"] != executable_id or executable_id in seen_artifacts):
        raise EventLogContractError("client executable reference")
    seen_artifacts.add(executable_id)
    executable = c.obj(executable_id, "client_executable")
    _require_keys(executable, {
        "schema_version", "run_id", "execution_id", "phase", "client", "resolved_path",
        "launch_identity", "launch_sha256", "seal_identity", "seal_sha256", "reported_version",
        "version_probe", "help_probe", "supported_build_id",
    }, "client executable")
    if (type(executable["schema_version"]) is not int or executable["schema_version"] != 1
            or executable["run_id"] != log["run_id"] or executable["execution_id"] != execution["id"]
            or executable["phase"] != execution["phase"] or executable["client"] != log["client"]
            or not _sha(executable["launch_sha256"]) or not _sha(executable["seal_sha256"])
            or type(executable["reported_version"]) is not str
            or type(executable["supported_build_id"]) is not str
            or not _ID.fullmatch(executable["supported_build_id"])):
        raise EventLogContractError("client executable binding")
    resolved_path = _canonical_absolute_executable_path(executable["resolved_path"])
    launch_identity = _identity_value(executable["launch_identity"], label="client executable launch")
    seal_identity = _identity_value(executable["seal_identity"], label="client executable seal")
    if (launch_identity != seal_identity or executable["launch_sha256"] != executable["seal_sha256"]
            or policy["argv"][0] != resolved_path or process["argv"][0] != resolved_path):
        raise EventLogContractError("client executable launch/seal binding")
    expected = context.trusted_executables.get(execution["id"])
    if expected is None:
        raise EventLogContractError("trusted executable expectation unavailable")
    if (expected.client != log["client"] or os.fspath(expected.resolved_path) != resolved_path
            or dict(expected.identity) != launch_identity or expected.sha256 != executable["launch_sha256"]
            or expected.supported_build_id != executable["supported_build_id"]):
        raise EventLogContractError("trusted executable expectation mismatch")
    raw, current_identity = _read_executable(resolved_path)
    if current_identity != launch_identity or hashlib.sha256(raw).hexdigest() != executable["launch_sha256"]:
        raise EventLogContractError("trusted executable changed after seal")
    version = _client_probe(c, executable, field="version_probe", artifact_kind="version",
                            expected_id=policy["version_id"], resolved_path=resolved_path)
    _client_probe(c, executable, field="help_probe", artifact_kind="help",
                  expected_id=policy["help_id"], resolved_path=resolved_path)
    if policy["version_id"] == policy["help_id"]:
        raise EventLogContractError("client executable probe artifact reuse")
    probe_artifacts = {policy["version_id"], policy["help_id"]}
    if probe_artifacts & seen_probe_artifacts:
        raise EventLogContractError("client executable probe artifact reuse")
    seen_probe_artifacts.update(probe_artifacts)
    if (executable["reported_version"] != _reported_version(version)
            or executable["reported_version"] != process["client_version"]
            or executable["reported_version"] != log["client_version"]):
        raise EventLogContractError("client executable reported version")
    build_key = (log["client"], executable["reported_version"], process["native_format"], executable["launch_sha256"])
    if context.supported_builds.get(build_key) != executable["supported_build_id"]:
        raise EventLogContractError("unsupported client executable build")
    return resolved_path


def _validate_execution(c: _Control, execution: dict[str, Any], log: dict[str, Any], context: TrustedRunContext,
                        scenario: dict[str, Any], phase_prompt: dict[str, Any], *,
                        seen_executable_artifacts: set[str], seen_probe_artifacts: set[str],
                        seen_phase_prompts: set[str]) -> None:
    policy = c.obj(execution["policy_id"], "policy")
    policy_keys = {"run_id", "execution_id", "phase", "client", "profile", "network_attestation", "argv", "argv_sha256", "version_id", "help_id", "executable_id", "phase_prompt_id", "phase_prompt_sha256", "phase_prompt_transport"}
    phase_keys = {"fixture_id", "fixture_sha256", "fixture_capability_id", "approval"}
    policy_keys |= phase_keys
    if log["client"] == "claude":
        policy_keys |= {"mcp_config_id", "mcp_config_sha256", "mcp_path", "mcp_identity"}
    _require_keys(policy, policy_keys, "policy")
    manifest = log["_manifest"]
    phase_binding = {
        "fixture_id": manifest["fixture_id"], "fixture_sha256": log["fixture_sha256"],
        "fixture_capability_id": manifest["fixture_capability_id"] if execution["phase"] == "approved_capture" else None,
        "approval": manifest["approval"] if execution["phase"] == "approved_capture" else None,
    }
    if any(policy[key] != phase_binding[key] for key in phase_keys):
        raise EventLogContractError("policy fixture/approval phase binding")
    if (policy["run_id"] != log["run_id"] or policy["execution_id"] != execution["id"] or policy["phase"] != execution["phase"]
            or policy["client"] != log["client"] or _canon(policy["argv"]) != policy["argv_sha256"]):
        raise EventLogContractError("policy binding")
    if log["client"] == "claude":
        mcp, mcp_path, mcp_identity = c.snapshot(policy["mcp_config_id"], "mcp_config")
        identity = policy["mcp_identity"]
        if (type(identity) is not dict or set(identity) != set(mcp_identity)
                or any(type(value) is not int for value in identity.values())
                or identity != mcp_identity or mcp != EMPTY_MCP_BYTES
                or _json(mcp, "MCP") != {"mcpServers": {}}
                or hashlib.sha256(mcp).hexdigest() != policy["mcp_config_sha256"]
                or policy["argv"][7] != policy["mcp_path"] or policy["mcp_path"] != mcp_path):
            raise EventLogContractError("Claude MCP binding")
    process = c.obj(execution["process_id"], "process")
    _require_keys(process, {"run_id", "execution_id", "phase", "client", "client_version", "argv", "argv_sha256", "exit_code", "transcript_id", "transcript_sha256", "trace_id", "trace_sha256", "native_format", "executable_id", "phase_prompt_id", "phase_prompt_sha256", "phase_prompt_transport"} | phase_keys | ({"mcp_identity"} if log["client"] == "claude" else set()), "process")
    if log["client"] == "claude" and (process["mcp_identity"] != mcp_identity or any(type(value) is not int for value in process["mcp_identity"].values())):
        raise EventLogContractError("Claude process MCP identity")
    if any(process[key] != phase_binding[key] for key in phase_keys):
        raise EventLogContractError("process fixture/approval phase binding")
    expected_transport = "stdin_utf8" if log["client"] == "codex" else "argv_final_utf8"
    prompt_id = phase_prompt["phase_prompt_id"]
    if (type(prompt_id) is not str or re.fullmatch(r"phase-prompt-[1-9][0-9]*", prompt_id) is None
            or prompt_id in seen_phase_prompts or phase_prompt["phase_prompt_transport"] != expected_transport
            or any(value[key] != phase_prompt[key] for value in (policy, process)
                   for key in ("phase_prompt_id", "phase_prompt_sha256", "phase_prompt_transport"))):
        raise EventLogContractError("phase prompt binding")
    seen_phase_prompts.add(prompt_id)
    prompt = c.read(prompt_id, "phase_prompt")
    try:
        expected_prompt = canonical_phase_prompt_bytes(scenario, execution["phase"])
    except ValueError as error:
        raise EventLogContractError("phase prompt scenario") from error
    if (not _sha(phase_prompt["phase_prompt_sha256"])
            or hashlib.sha256(prompt).hexdigest() != phase_prompt["phase_prompt_sha256"]
            or prompt != expected_prompt):
        raise EventLogContractError("phase prompt bytes")
    executable_path = _validate_client_executable(
        c, execution, log, context, policy, process, seen_artifacts=seen_executable_artifacts,
        seen_probe_artifacts=seen_probe_artifacts,
    )
    _exact_profile(policy, context, log["client"], executable_path, prompt)
    transcript = c.read(execution["transcript_id"], "transcript")
    if (process["run_id"] != log["run_id"] or process["execution_id"] != execution["id"] or process["phase"] != execution["phase"]
            or process["client"] != log["client"] or process["client_version"] != log["client_version"]
            or process["argv"] != policy["argv"] or process["argv_sha256"] != policy["argv_sha256"]
            or type(process["exit_code"]) is not int or process["exit_code"] != 0 or process["transcript_id"] != execution["transcript_id"]
            or process["transcript_sha256"] != hashlib.sha256(transcript).hexdigest()):
        raise EventLogContractError("actual client process")
    _execution_trace(c, execution, process, log)


def _native_transcript(raw, client, version, format_name):
    """Conservative sourced CLI adapter; unsupported formats cannot prove a pass."""
    adapter = _NATIVE_ADAPTERS.get((client, version, format_name))
    if adapter is None:
        raise EventLogContractError("unsupported_native_format")
    return adapter(raw)


def _claude_stream_2_1_251(raw):
    version = "2.1.251"
    lines = raw.splitlines(keepends=True)
    if not lines or any(not line.endswith(b"\n") for line in lines):
        raise EventLogContractError("incomplete native transcript")
    records, offset, session, uuids, complete, user_message_uuid = {}, 0, None, set(), False, None
    for line in lines:
        value = _json(line, "native transcript record")
        if type(value) is not dict:
            raise EventLogContractError("native transcript record object")
        if type(value.get("session_id")) is not str or not value["session_id"] or type(value.get("uuid")) is not str or not value["uuid"]:
            raise EventLogContractError("native transcript session")
        if value["uuid"] in uuids:
            raise EventLogContractError("duplicate native record identity")
        uuids.add(value["uuid"])
        if session is None:
            if value.get("type") != "system" or value.get("subtype") != "init":
                raise EventLogContractError("native session initialization missing")
            session = value["session_id"]
        if value["session_id"] != session or "supersedes" in value or value.get("error") is not None:
            raise EventLogContractError("unsupported native replacement/error/session")
        if "timestamp" in value:
            _text(value["timestamp"], "native display timestamp")
        if "user_message_uuid" in value:
            _text(value["user_message_uuid"], "native user message identity")
            if user_message_uuid is not None and user_message_uuid != value["user_message_uuid"]:
                raise EventLogContractError("native user message identity mismatch")
            user_message_uuid = value["user_message_uuid"]
        texts = {}
        kind = value.get("type")
        if complete and (kind != "system" or value.get("subtype") not in {"informational", "session_state_changed", "notification"}):
            raise EventLogContractError("unsupported native transcript after completion")
        if kind == "assistant":
            allowed = {"type", "message", "parent_tool_use_id", "session_id", "uuid", "timestamp", "error", "user_message_uuid"}
            if set(value) - allowed or not {"type", "message", "parent_tool_use_id", "session_id", "uuid"} <= value.keys() or value["parent_tool_use_id"] is not None:
                raise EventLogContractError("unsupported native assistant record")
            message = value["message"]
            if type(message) is not dict or not {"model", "content"} <= message.keys() or set(message) - {"model", "content", "id", "type", "role", "stop_reason", "stop_sequence", "usage"}:
                raise EventLogContractError("unsupported native assistant message")
            _text(message["model"])
            if message.get("role", "assistant") != "assistant" or type(message["content"]) is not list:
                raise EventLogContractError("native assistant content")
            if (message.get("type", "message") != "message"
                    or message.get("stop_reason") not in (None, "end_turn", "tool_use", "stop_sequence")
                    or message.get("stop_sequence") is not None and type(message["stop_sequence"]) is not str
                    or "usage" in message and type(message["usage"]) is not dict):
                raise EventLogContractError("unsupported native assistant metadata/refusal")
            if "id" in message:
                _text(message["id"], "native API message identity")
            for index, block in enumerate(message["content"]):
                if type(block) is not dict:
                    raise EventLogContractError("native assistant block")
                if block.get("type") == "text":
                    _require_keys(block, {"type", "text"}, "native text block")
                    if type(block["text"]) is not str:
                        raise EventLogContractError("native text type")
                    texts[f"/message/content/{index}/text"] = block["text"]
                elif block.get("type") == "tool_use":
                    _require_keys(block, {"type", "id", "name", "input"}, "native tool block")
                    _text(block["id"])
                    _text(block["name"])
                    if type(block["input"]) is not dict:
                        raise EventLogContractError("native tool input")
                else:
                    raise EventLogContractError("unsupported native content block")
        elif kind == "result":
            required = {"type", "subtype", "duration_ms", "duration_api_ms", "num_turns", "is_error", "session_id", "uuid", "result", "stop_reason", "total_cost_usd", "usage", "modelUsage", "permission_denials"}
            allowed = required | {"errors", "api_error_status", "structured_output", "deferred_tool_use", "user_message_uuid", "terminal_reason"}
            if (not required <= value.keys() or set(value) - allowed or value["subtype"] != "success" or value["is_error"] is not False
                    or any(type(value[key]) is not int or value[key] < 0 for key in ("duration_ms", "duration_api_ms", "num_turns"))
                    or value["permission_denials"] != [] or "errors" in value and value["errors"] != []
                    or value.get("deferred_tool_use") not in (None, [])
                    or value.get("api_error_status") is not None):
                raise EventLogContractError("native result incomplete or failed")
            if (type(value["total_cost_usd"]) not in {int, float} or value["total_cost_usd"] < 0
                    or type(value["usage"]) is not dict or type(value["modelUsage"]) is not dict
                    or value.get("terminal_reason") is not None or value.get("structured_output") is not None):
                raise EventLogContractError("unsupported native result metadata")
            if type(value["result"]) is not str:
                raise EventLogContractError("native result text")
            if value["stop_reason"] not in (None, "end_turn", "stop_sequence"):
                raise EventLogContractError("unsupported native completion stop/refusal")
            for key in ("stop_reason", "uuid"):
                if value.get(key) is not None and type(value[key]) is not str:
                    raise EventLogContractError("native result text")
            complete = True
        else:
            _native_nonmarker(value, version, initialized=bool(records))
        records[offset] = {"end": offset + len(line), "sha256": hashlib.sha256(line).hexdigest(), "texts": texts}
        offset += len(line)
    if not complete:
        raise EventLogContractError("native completion missing")
    return records


_NATIVE_ADAPTERS = {("claude", "2.1.251", "claude-stream-json-2.1.251-v1"): _claude_stream_2_1_251}


def _native_nonmarker(value, version, *, initialized):
    """Typed non-marker projections sourced from the exact 2.1.251 SDK union."""
    common = {"type", "uuid", "session_id"}
    kind = value["type"]
    if kind == "system":
        subtype = value.get("subtype")
        common |= {"subtype"}
        if subtype == "init":
            fields = {"claude_code_version", "cwd", "model", "tools", "mcp_servers", "permissionMode", "apiKeySource", "slash_commands", "output_style", "skills", "plugins"}
            _require_keys(value, common | fields, "native system init")
            if initialized or value["claude_code_version"] != version or value["permissionMode"] != "dontAsk":
                raise EventLogContractError("native initialization profile")
            for key in ("cwd", "model", "apiKeySource", "output_style"):
                _text(value[key])
            for key in ("tools", "slash_commands", "skills", "plugins", "mcp_servers"):
                if type(value[key]) is not list:
                    raise EventLogContractError("native init array")
            if value["mcp_servers"] or any(type(tool) is not str for tool in value["tools"]) or set(value["tools"]) != {"Read", "Edit", "Write", "Glob", "Grep", "Bash"} or len(value["tools"]) != 6:
                raise EventLogContractError("native init expanded tools/MCP")
        elif subtype == "status":
            required, optional = common | {"status"}, {"permissionMode", "compact_result", "compact_error"}
            if not required <= value.keys() or set(value) - required - optional or value["status"] not in (None, "requesting") or any(value.get(key) is not None for key in ("compact_result", "compact_error")):
                raise EventLogContractError("unsupported native compaction/status")
            if "permissionMode" in value and value["permissionMode"] != "dontAsk":
                raise EventLogContractError("native permission mode changed")
        elif subtype == "informational":
            required, optional = common | {"content", "level"}, {"prevent_continuation", "tool_use_id"}
            if not required <= value.keys() or set(value) - required - optional or value["level"] not in {"info", "notice", "suggestion", "warning"}:
                raise EventLogContractError("native informational record")
            if type(value["content"]) is not str or "prevent_continuation" in value and value["prevent_continuation"] is not False:
                raise EventLogContractError("native continuation prevented")
            if "tool_use_id" in value:
                _text(value["tool_use_id"])
        elif subtype == "session_state_changed":
            _require_keys(value, common | {"state"}, "native session state")
            if value["state"] not in {"idle", "running"}:
                raise EventLogContractError("native session requires action")
        elif subtype == "notification":
            required, optional = common | {"key", "text", "priority"}, set()
            if not required <= value.keys() or set(value) - required - optional or value["priority"] not in {"low", "medium", "high", "immediate"}:
                raise EventLogContractError("native notification")
            for key in ("key", "text"):
                _text(value[key])
        elif subtype in {"hook_started", "hook_progress", "hook_response"}:
            required = common | {"hook_id", "hook_name", "hook_event"}
            if subtype != "hook_started":
                required |= {"stdout", "stderr", "output"}
            if subtype == "hook_response":
                required |= {"outcome"}
            optional = {"exit_code"} if subtype == "hook_response" else set()
            if not required <= value.keys() or set(value) - required - optional:
                raise EventLogContractError("native hook record")
            for key in required - common - {"outcome"}:
                if type(value[key]) is not str:
                    raise EventLogContractError("native hook text")
            if subtype == "hook_response" and (value["outcome"] != "success" or "exit_code" in value and (type(value["exit_code"]) is not int or value["exit_code"] != 0)):
                raise EventLogContractError("native hook failure")
        else:
            raise EventLogContractError("unsupported_native_format system subtype")
    elif kind == "user":
        required = common | {"message", "parent_tool_use_id"}
        optional = {"tool_use_result", "isReplay", "timestamp", "user_message_uuid"}
        if not required <= value.keys() or set(value) - required - optional or value["parent_tool_use_id"] is not None:
            raise EventLogContractError("unsupported native user record")
        message = value["message"]
        if type(message) is not dict or message.get("role") != "user" or type(message.get("content")) not in {str, list}:
            raise EventLogContractError("native user message")
        if "isReplay" in value and type(value["isReplay"]) is not bool:
            raise EventLogContractError("native replay type")
    elif kind == "tool_use_summary":
        required = common | {"summary", "preceding_tool_use_ids"}
        if not required <= value.keys() or set(value) - required - {"timestamp"} or type(value["summary"]) is not str or type(value["preceding_tool_use_ids"]) is not list or any(type(item) is not str for item in value["preceding_tool_use_ids"]):
            raise EventLogContractError("native tool summary")
    elif kind == "tool_progress":
        required = common | {"tool_use_id", "tool_name", "parent_tool_use_id", "elapsed_time_seconds"}
        if not required <= value.keys() or set(value) - required - {"task_id"} or value["parent_tool_use_id"] is not None or type(value["elapsed_time_seconds"]) is not int or value["elapsed_time_seconds"] < 0:
            raise EventLogContractError("native tool progress")
        _text(value["tool_use_id"])
        _text(value["tool_name"])
        if "task_id" in value:
            _text(value["task_id"])
    else:
        # stream_event requires --include-partial-messages, which is absent
        # from this frozen profile; unknown/retraction/rate-limit variants fail.
        raise EventLogContractError("unsupported_native_format record")


def _execution_trace(c, execution, process, log):
    raw = c.read(process["trace_id"], "execution_trace")
    if hashlib.sha256(raw).hexdigest() != process["trace_sha256"]:
        raise EventLogContractError("execution trace digest")
    trace = _json(raw, "execution trace")
    _require_keys(trace, {"schema_version", "run_id", "execution_id", "records"}, "execution trace")
    if (type(trace["schema_version"]) is not int or trace["schema_version"] != 1 or trace["run_id"] != c.run_id
            or trace["execution_id"] != execution["id"] or type(trace["records"]) is not list):
        raise EventLogContractError("execution trace binding")
    transcript = c.read(execution["transcript_id"], "transcript")
    native = _native_transcript(transcript, log["client"], log["client_version"], process["native_format"])
    if _json(transcript.splitlines()[0], "native initialization")["cwd"] != c.workspace_path:
        raise EventLogContractError("native workspace identity")
    observed = {cid: c.obj(cid, "command_observation") for cid, entry in c.entries.items() if entry["type"] == "command_observation"}
    observed = {cid: value for cid, value in observed.items() if value.get("execution_id") == execution["id"]}
    started, ended, state_ids, native_offsets, active = set(), set(), set(), [], None
    native_snapshot_ids: set[str] = set()
    for sequence, entry in enumerate(trace["records"], 1):
        if type(entry) is not dict or type(entry.get("sequence")) is not int or entry["sequence"] != sequence:
            raise EventLogContractError("execution trace ordering sequence")
        kind = entry.get("kind")
        if kind in {"command_start", "command_end"}:
            _require_keys(entry, {"sequence", "kind", "command_id"}, "trace command")
            cid = entry["command_id"]
            if type(cid) is not str or cid not in observed:
                raise EventLogContractError("trace has unindexed command")
            observation = observed[cid]
            if kind == "command_start":
                if cid in started or active is not None or observation["start_sequence"] != sequence:
                    raise EventLogContractError("command start ordering")
                started.add(cid)
                active = cid
            else:
                if active != cid or cid in ended or observation["end_sequence"] != sequence:
                    raise EventLogContractError("command end ordering")
                if observation["source_state_id"] is not None and observation["source_state_id"] not in state_ids:
                    raise EventLogContractError("command source state absent from trace")
                ended.add(cid)
                active = None
        elif kind == "source_state":
            _require_keys(entry, {"sequence", "kind", "command_id", "source_state_id"}, "trace source state")
            if entry["command_id"] != active or active is None or entry["source_state_id"] != observed[active]["source_state_id"] or entry["source_state_id"] in state_ids:
                raise EventLogContractError("source state command ordering")
            state_ids.add(entry["source_state_id"])
        elif kind == "native_record":
            native_keys = {"sequence", "kind", "transcript_id", "byte_start", "byte_end", "sha256", "workspace_snapshot_id"}
            if set(entry) != native_keys:
                if set(entry) == native_keys - {"workspace_snapshot_id"}:
                    raise EventLogContractError("native trace workspace snapshot binding")
                raise EventLogContractError("trace native record shape")
            if type(entry["byte_start"]) is not int or type(entry["byte_end"]) is not int or entry["byte_start"] not in native:
                raise EventLogContractError("native transcript ordering span")
            item = native[entry["byte_start"]]
            if entry["transcript_id"] != execution["transcript_id"] or item["end"] != entry["byte_end"] or item["sha256"] != entry["sha256"]:
                raise EventLogContractError("native transcript trace binding")
            snapshot_id = entry["workspace_snapshot_id"]
            snapshot = _workspace_snapshot(c, snapshot_id, execution_id=execution["id"])
            if (snapshot["anchor_kind"] != "native_record"
                    or snapshot["trace_sequence"] != sequence
                    or snapshot["transcript_id"] != entry["transcript_id"]
                    or snapshot["native_record_start"] != entry["byte_start"]
                    or snapshot["native_record_end"] != entry["byte_end"]
                    or snapshot["native_record_sha256"] != entry["sha256"]):
                raise EventLogContractError("native trace workspace snapshot binding")
            if snapshot_id in native_snapshot_ids:
                raise EventLogContractError("native trace workspace snapshot reused")
            native_snapshot_ids.add(snapshot_id)
            item["sequence"] = sequence
            item["workspace_snapshot_id"] = snapshot_id
            native_offsets.append(entry["byte_start"])
        else:
            raise EventLogContractError("unsupported execution trace record")
    if active is not None or started != ended or ended != observed.keys() or native_offsets != list(native):
        raise EventLogContractError("incomplete execution trace ordering")
    if not hasattr(c, "native_records"):
        c.native_records = {}
    c.native_records[execution["id"]] = native
    bindings = getattr(c, "native_snapshot_bindings", None)
    if bindings is None:
        bindings = {}
        c.native_snapshot_bindings = bindings
    for offset, item in native.items():
        snapshot_id = item["workspace_snapshot_id"]
        if snapshot_id in bindings:
            raise EventLogContractError("native trace workspace snapshot reused")
        bindings[snapshot_id] = (execution["id"], offset)


def _audit_workspace_snapshots(c, executions: Mapping[str, dict[str, Any]]) -> None:
    """Close the index/trace graph after every execution trace has been read."""

    bound = getattr(c, "native_snapshot_bindings", {})
    initial_by_execution: dict[str, str] = {}
    for artifact_id, entry in c.entries.items():
        if entry["type"] != "workspace_snapshot":
            continue
        snapshot = _workspace_snapshot(c, artifact_id)
        if snapshot["execution_id"] not in executions:
            raise EventLogContractError("workspace snapshot unknown execution")
        if snapshot["anchor_kind"] == "native_record":
            if artifact_id not in bound:
                raise EventLogContractError("unreferenced native workspace snapshot")
        else:
            if snapshot["execution_id"] in initial_by_execution:
                raise EventLogContractError("duplicate initial workspace snapshot")
            initial_by_execution[snapshot["execution_id"]] = artifact_id
    if set(initial_by_execution) != set(executions):
        raise EventLogContractError("workspace initial snapshot missing")


def _native_text_line_spans(raw: bytes) -> list[tuple[int, int]]:
    """Match CommonMark's CR/LF/CRLF lines while retaining original byte offsets."""

    return [
        match.span() for match in re.finditer(rb"[^\r\n]*(?:\r\n|\r|\n|$)", raw)
        if match.start() != match.end()
    ]


def _mapped_backtick(state, silent):
    """Record spans only when the unmodified CommonMark rule emits code."""

    start, count = state.pos, len(state.tokens)
    matched = _commonmark_backtick(state, silent)
    if not silent and len(state.tokens) > count and state.tokens[-1].type == "code_inline":
        token = state.tokens[-1]
        width = len(token.markup)
        token.meta["native_code_span"] = (start + width, state.pos - width)
    return matched


def _mapped_image(state, silent):
    """The parser creates image children from the source after its ``![``."""

    start, count = state.pos, len(state.tokens)
    matched = _commonmark_image(state, silent)
    if not silent and len(state.tokens) > count and state.tokens[-1].type == "image":
        state.tokens[-1].meta["native_child_start"] = start + 2
    return matched


def _mapped_inline_block(rule):
    """Retain lines removed by the parser's own container handling and strip.

    Paragraphs and setext headings call getLines(...).strip(). Reuse getLines
    while the parser's container state is still active, so even whitespace-only
    source lines stripped from a lazy blockquote continuation map correctly.
    """

    def mapped(state, start_line, end_line, silent):
        count = len(state.tokens)
        matched = rule(state, start_line, end_line, silent)
        for token in state.tokens[count:]:
            if token.type == "inline" and token.map is not None:
                start, end = token.map
                source = state.getLines(start, end, state.blkIndent, False)
                if source.strip() != token.content:
                    raise EventLogContractError("native markdown inline source")
                stripped = source[:len(source) - len(source.lstrip())]
                token.meta["native_line_start"] = start + stripped.count("\n")
        return matched

    return mapped


_COMMONMARK.inline.ruler.at("backticks", _mapped_backtick)
_COMMONMARK.inline.ruler.at("image", _mapped_image)
_COMMONMARK.block.ruler.at("paragraph", _mapped_inline_block(_commonmark_paragraph))
_COMMONMARK.block.ruler.at("lheading", _mapped_inline_block(_commonmark_lheading))


def _inline_code_spans(tokens: list[Token], source_offset: int = 0) -> list[tuple[int, int]]:
    """Collect parser-issued code content spans, including nested image labels."""

    spans = []
    for token in tokens:
        if token.type == "code_inline":
            start, end = token.meta["native_code_span"]
            spans.append((source_offset + start, source_offset + end))
        elif token.type == "image":
            spans.extend(_inline_code_spans(
                token.children or [], source_offset + token.meta["native_child_start"],
            ))
    return spans


def _native_markdown_code_coverage(text: str, raw: bytes) -> set[int]:
    """Return physical lines whose entire body is parser-recognized code.

    Parse the original source: substituting a marker can change reference-link
    resolution and invent a code span. Inline offsets are characters in the
    parser's container-stripped content; only its line map crosses back to the
    original bytes, after an exact normalized line-body check.
    """

    lines = _native_text_line_spans(raw)
    try:
        tokens = _COMMONMARK.parse(text)
    except Exception as error:  # pragma: no cover - markdown-it handles text inputs.
        raise EventLogContractError("native markdown parse") from error
    code_lines: set[int] = set()
    for token in tokens:
        if token.map is None:
            continue
        start, end = token.map
        if start < 0 or end < start or end > len(lines):
            raise EventLogContractError("native markdown source map")
        if token.type in {"fence", "code_block"}:
            code_lines.update(range(start, end))
        elif token.type == "inline":
            spans = _inline_code_spans(token.children or [])
            first_line = token.meta.get("native_line_start", start)
            offset = 0
            for index, body in enumerate(token.content.split("\n"), first_line):
                body_end = offset + len(body)
                if body.startswith("EVENT:"):
                    if index >= end:
                        raise EventLogContractError("native markdown inline line map")
                    left, right = lines[index]
                    original = raw[left:right].rstrip(b"\r\n").decode("utf-8").replace("\0", "\ufffd")
                    if original == body and any(offset >= left and body_end <= right for left, right in spans):
                        code_lines.add(index)
                offset = body_end + 1
    return code_lines


def _audit_native_event_markers(c, log, scenario, executions) -> None:
    """Join every standalone native marker to exactly one indexed event row.

    Marker lines are only transcript routing tokens.  This audit never treats
    them as action evidence; the ordinary event-record and corroboration joins
    remain the sole proof for the named action.
    """

    expected_by_execution: dict[str, list[tuple[int, str, int, int, str]]] = {
        execution_id: [] for execution_id in executions
    }
    logged_by_execution: dict[str, list[str]] = {
        execution_id: [] for execution_id in executions
    }
    for event in log["events"]:
        execution_id = event["execution_id"]
        logged_by_execution[execution_id].append(event["name"])
        marker = c.obj(event["transcript_marker_id"], "marker")
        expected_by_execution[execution_id].append((
            marker["native_record_start"], marker["text_pointer"],
            marker["byte_start"], marker["byte_end"], event["name"],
        ))
    for execution_id, execution in executions.items():
        try:
            required = required_events_for_phase(scenario, execution["phase"])
        except ValueError as error:
            raise EventLogContractError("required phase event markers") from error
        if tuple(logged_by_execution[execution_id]) != required:
            raise EventLogContractError("required phase event marker sequence")

    actual_by_execution: dict[str, list[tuple[int, str, int, int, str]]] = {
        execution_id: [] for execution_id in executions
    }

    for execution_id in executions:
        for native_start, native in c.native_records[execution_id].items():
            for text_pointer, text in native["texts"].items():
                raw = text.encode("utf-8")
                code_lines = _native_markdown_code_coverage(text, raw)
                for index, (start, end) in enumerate(_native_text_line_spans(raw)):
                    if index in code_lines:
                        continue
                    line = raw[start:end]
                    if marker := _NATIVE_EVENT_LINE.fullmatch(line):
                        actual_by_execution[execution_id].append((
                            native_start, text_pointer, start, end, marker.group(1).decode("ascii"),
                        ))
                    elif line.startswith(b"EVENT:"):
                        raise EventLogContractError("ambiguous native event marker boundary")
    if actual_by_execution != expected_by_execution:
        raise EventLogContractError("native event marker join")


def _event_order(c, log, executions):
    phase_indices = {execution_id: index for index, execution_id in enumerate(executions)}
    previous_position, previous_trace = None, None
    spans, commanded = {}, set()
    for event in log["events"]:
        marker = c.obj(event["transcript_marker_id"], "marker")
        execution_id = event["execution_id"]
        native = c.native_records[execution_id][marker["native_record_start"]]
        field_index = int(marker["text_pointer"].split("/")[3])
        position = (phase_indices[execution_id], marker["native_record_start"], field_index, marker["byte_start"])
        if previous_position is not None and position <= previous_position:
            raise EventLogContractError("native transcript marker ordering")
        key = (execution_id, marker["transcript_id"], marker["native_record_start"], marker["text_pointer"])
        if marker["byte_start"] < spans.get(key, 0):
            raise EventLogContractError("overlapping transcript marker spans")
        spans[key] = marker["byte_end"]
        trace_position = (phase_indices[execution_id], native["sequence"])
        record = c.obj(event["event_record_id"], "event_record")
        ids = []
        if "cursor_proof_id" in record:
            ids.extend(c.obj(record["cursor_proof_id"], "cursor_proof")["command_ids"])
        if "command_id" in record and record["command_id"] not in ids:
            ids.append(record["command_id"])
        for cid in ids:
            observation = c.obj(cid, "command_observation")
            if observation["execution_id"] != execution_id or observation["end_sequence"] >= native["sequence"]:
                raise EventLogContractError("marker precedes its completed command")
            if cid not in commanded:
                start_position = (phase_indices[execution_id], observation["start_sequence"])
                if previous_trace is not None and start_position <= previous_trace:
                    raise EventLogContractError("command before prerequisite marker ordering")
                commanded.add(cid)
        previous_position, previous_trace = position, trace_position


def _sync_stream(c, artifact_id, reference, command):
    raw = c.read(artifact_id, "sync_stream")
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise EventLogContractError("immutable stream digest")
    lines = raw.splitlines(keepends=True)
    if len(lines) < 2 or any(not line.endswith(b"\n") for line in lines):
        raise EventLogContractError("incomplete immutable stream")
    records = [_json(line, "sync stream line") for line in lines]
    header, trailer = records[0], records[-1]
    _require_keys(header, {"type", "schema_version", "command", "generated_at"}, "stream header")
    if header["type"] != "header" or type(header["schema_version"]) is not int or header["schema_version"] != 1 or header["command"] != command:
        raise EventLogContractError("stream header identity")
    _timestamp(header["generated_at"])
    counts = Counter()
    effect_digest = hashlib.sha256()
    effects = []
    for sequence, record in enumerate(records[1:-1], 1):
        _require_keys(record, {"type", "sequence", "kind", "data"}, "stream event")
        if (record["type"] != "event" or type(record["sequence"]) is not int or record["sequence"] != sequence
                or type(record["kind"]) is not str or record["kind"] not in reference.event_counts
                or type(record["data"]) is not dict):
            raise EventLogContractError("stream event identity")
        counts[record["kind"]] += 1
        effect = {"kind": record["kind"], "data": record["data"]}
        _semantic(semantic.parse_sync_effect, record["kind"], record["data"])
        effect_digest.update(json.dumps(effect, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        effects.append(effect)
    exact_counts = {kind: counts[kind] for kind in reference.event_counts}
    _require_keys(trailer, {"type", "event_count", "event_counts", "events_sha256", "corpus_revision"}, "stream trailer")
    if (trailer["type"] != "trailer" or type(trailer["event_count"]) is not int
            or trailer["event_count"] != len(effects) or type(trailer["event_counts"]) is not dict
            or any(type(value) is not int for value in trailer["event_counts"].values())
            or trailer["event_counts"] != exact_counts or exact_counts != dict(reference.event_counts)
            or trailer["events_sha256"] != hashlib.sha256(b"".join(lines[1:-1])).hexdigest()
            or trailer["corpus_revision"] != reference.corpus_revision):
        raise EventLogContractError("stream trailer identity")
    return effects, effect_digest.hexdigest()


def _validate_initial_sync_receipt_from_sealed_artifacts(
    c,
    *,
    execution_id: str,
    stage_boundaries: tuple[InitialSyncReceiptStageBoundary, ...],
    selection: InitialSyncReceiptSelection,
) -> InitialSyncReceiptFacts:
    """Read one complete phase-one initial-sync receipt without emitting data.

    This deliberately narrow decoder is for the pre-phase-two authority
    boundary.  It accepts only a read-capable sealed artifact view, four
    parser-bound lifecycle positions, and a closed six-ID selection.  It does
    not accept or construct a writer, event log, semantic plan, marker/event
    identity, artifact discovery hint, or private overlay.

    The full event-log validator still owns public-event validation.  This
    helper only proves that the exact receipt lifecycle was already completed
    in the shared trace before a web capability can become available.
    """

    if type(execution_id) is not str or _ID.fullmatch(execution_id) is None:
        raise EventLogContractError("initial sync receipt execution")
    if type(stage_boundaries) is not tuple or len(stage_boundaries) != len(_INITIAL_SYNC_RECEIPT_STAGES):
        raise EventLogContractError("initial sync receipt lifecycle boundaries")
    if type(selection) is not InitialSyncReceiptSelection:
        raise EventLogContractError("initial sync receipt selection")

    boundaries = tuple(stage_boundaries)
    if (any(type(boundary) is not InitialSyncReceiptStageBoundary for boundary in boundaries)
            or tuple(boundary.stage for boundary in boundaries) != _INITIAL_SYNC_RECEIPT_STAGES
            or tuple(boundary.trace_sequence for boundary in boundaries)
            != tuple(sorted(boundary.trace_sequence for boundary in boundaries))
            or len({boundary.trace_sequence for boundary in boundaries}) != len(boundaries)):
        raise EventLogContractError("initial sync receipt lifecycle order")

    entries = getattr(c, "entries", None)
    if not isinstance(entries, Mapping):
        raise EventLogContractError("sealed receipt artifact index")
    selected_ids = selection.command_observation_ids
    for artifact_id, artifact_type in (
        *((command_id, "command_observation") for command_id in selected_ids),
        (selection.stream_artifact_id, "sync_stream"),
        (selection.durable_artifact_id, "consumption_receipt"),
    ):
        entry = entries.get(artifact_id)
        if not isinstance(entry, Mapping) or entry.get("type") != artifact_type:
            raise EventLogContractError("initial sync receipt selected artifact type")

    # Every command observed in this phase must be one of the explicitly
    # selected lifecycle rows.  Do not let an extra invocation disappear when
    # the phase gate validates only a helpful subset.
    observed_phase_commands: set[str] = set()
    for artifact_id, entry in entries.items():
        if not isinstance(entry, Mapping) or entry.get("type") != "command_observation":
            continue
        observation = c.obj(artifact_id, "command_observation")
        observed_execution = observation.get("execution_id")
        if type(observed_execution) is not str:
            raise EventLogContractError("initial sync receipt command execution")
        if observed_execution == execution_id:
            observed_phase_commands.add(artifact_id)
    if observed_phase_commands != set(selected_ids):
        raise EventLogContractError("initial sync receipt extra or missing command")

    commands = []
    previous_boundary = 0
    for stage, boundary, command_id in zip(_INITIAL_SYNC_RECEIPT_STAGES, boundaries, selected_ids):
        argv, result = _result(c, command_id, execution_id)
        observation = c.obj(command_id, "command_observation")
        if not (
            previous_boundary < observation["start_sequence"] < observation["end_sequence"]
            < boundary.trace_sequence
        ):
            raise EventLogContractError("initial sync receipt command boundary order")
        _event_command("initial_sync_receipt_" + stage, argv, result)
        commands.append((argv, result, observation))
        previous_boundary = boundary.trace_sequence

    verify, consume, durable, acknowledge = (command[1] for command in commands)
    result_id, revision, counts, digest = _receipt_identity(verify, consume, acknowledge)
    _receipt_identity(verify, durable, acknowledge)
    consume_argv, durable_argv, acknowledge_argv = (commands[index][0] for index in (1, 2, 3))
    if (
        consume_argv != ["./brain", "--json", "source", "consume-sync-result", "--result-id", result_id]
        or durable_argv != consume_argv
        or acknowledge_argv != ["./brain", "--json", "source", "acknowledge-sync-result", "--result-id", result_id]
        or durable["data"]["status"] != "already_consumed"
        or {key: value for key, value in durable["data"].items() if key != "status"}
        != {key: value for key, value in consume["data"].items() if key != "status"}
    ):
        raise EventLogContractError("initial sync receipt replay or command form")

    # The phase-one web authority has no extraction handoff channel.  A
    # delivery or handoff effect requires the full later receipt/delivery
    # projection; it cannot be silently ignored at this narrow phase gate.
    if (consume["data"]["handoff_delivery"] is not None
            or durable["data"]["handoff_delivery"] is not None
            or any(isinstance(entry, Mapping) and entry.get("type") == "delivery"
                   for entry in entries.values())):
        raise EventLogContractError("initial sync receipt delivery is unavailable")

    reference = SyncResultReference.from_dict(_manifest(verify))
    publication_state = _source_state(c, selected_ids[0], result_id=result_id, revision=revision)
    authority = {
        "result_id": result_id,
        "corpus_revision": revision,
        "source_state": publication_state,
    }
    source_state_ids: set[str] = set()
    for role, command_id, observation in zip(
        _INITIAL_SYNC_RECEIPT_STAGES, selected_ids, (command[2] for command in commands),
    ):
        state_id = observation["source_state_id"]
        if role in {"verified", "consumed"} and state_id is None:
            raise EventLogContractError("initial sync receipt source state missing")
        if state_id is None:
            continue
        if state_id in source_state_ids:
            raise EventLogContractError("initial sync receipt source state reused")
        source_state_ids.add(state_id)
        if role == "verified":
            continue
        _receipt_source_state(c, command_id, authority, role)

    effects, actual_digest = _sync_stream(
        c, selection.stream_artifact_id, reference, verify["command"],
    )
    if actual_digest != digest:
        raise EventLogContractError("initial sync receipt durable effect digest")
    if any(effect["kind"] == "handoff_source_id" for effect in effects):
        raise EventLogContractError("initial sync receipt handoff is unavailable")
    _semantic(semantic.validate_stream_effects, effects, publication_state)
    parsed = _semantic(
        semantic.parse_manifest_response, verify,
        observed_exit_code=0 if verify["ok"] else 1,
    )
    stream_header = _json(
        c.read(selection.stream_artifact_id, "sync_stream").splitlines()[0],
        "stream header",
    )
    _semantic(
        semantic.validate_response_effects, parsed, effects, publication_state,
        generated_at=stream_header["generated_at"],
    )

    durable_raw = c.obj(selection.durable_artifact_id, "consumption_receipt")
    _require_keys(
        durable_raw,
        {"schema_version", "result_id", "reference", "event_counts", "effect_digest", "handoff_delivery"},
        "durable consumption",
    )
    try:
        durable_reference = SyncResultReference.from_dict(durable_raw["reference"])
    except (TypeError, ValueError) as error:
        raise EventLogContractError("durable consumption reference types") from error
    expected_durable = {
        "schema_version": 2,
        "result_id": result_id,
        "reference": reference.to_dict(),
        "event_counts": counts,
        "effect_digest": digest,
        "handoff_delivery": None,
    }
    if (
        durable_reference != reference
        or type(durable_raw["schema_version"]) is not int
        or durable_raw != expected_durable
        or type(durable_raw["event_counts"]) is not dict
        or any(type(value) is not int for value in durable_raw["event_counts"].values())
    ):
        raise EventLogContractError("initial sync receipt durable consumption identity")

    return InitialSyncReceiptFacts(
        result_id=result_id,
        corpus_revision=revision,
        event_counts=MappingProxyType(dict(sorted(counts.items()))),
        effect_digest=digest,
        selection=selection,
    )


def validate_initial_sync_receipt_from_sealed_artifacts(
    c,
    *,
    execution_id: str,
    stage_boundaries: tuple[InitialSyncReceiptStageBoundary, ...],
    selection: InitialSyncReceiptSelection,
) -> InitialSyncReceiptFacts:
    """Validate one selected initial-sync lifecycle from sealed reads only.

    Expected reader/data faults are translated into the contract's public
    failure type.  Assertion, attribute, and other programming failures stay
    visible: callers must not mistake an implementation defect for malformed
    evidence.
    """

    try:
        return _validate_initial_sync_receipt_from_sealed_artifacts(
            c,
            execution_id=execution_id,
            stage_boundaries=stage_boundaries,
            selection=selection,
        )
    except EventLogContractError:
        raise
    except (KeyError, IndexError, OSError, TypeError, UnicodeError, ValueError) as error:
        raise EventLogContractError("initial sync receipt sealed evidence") from error


def _snapshot_product(result):
    if result.get("command") == "eval mock-web-capture":
        _require_keys(result["data"], {"fixture_id", "product_result"}, "initial fixture result")
        if result["data"]["fixture_id"] != "web-approval-and-capture.initial":
            raise EventLogContractError("initial fixture result identity")
        result = result["data"]["product_result"]
    _envelope(result, "source snapshot-url")
    return result


def _validate_receipts(c: _Control, log: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    names = [event["name"] for event in events]
    deliveries = {delivery["id"]: delivery for delivery in log["deliveries"]}
    if len(deliveries) != len(log["deliveries"]):
        raise EventLogContractError("duplicate delivery")
    verified = {}
    used_deliveries = set()
    all_commands = set()
    for receipt in log["receipts"]:
        if receipt["id"] in verified:
            raise EventLogContractError("duplicate receipt")
        operation = receipt["operation"]
        stages = ("verified", "consumed", "durable", "acknowledged")
        try:
            positions = [names.index(f"{operation}_receipt_{stage}") for stage in stages]
        except ValueError as error:
            raise EventLogContractError("missing receipt lifecycle") from error
        if positions != sorted(positions):
            raise EventLogContractError("receipt lifecycle order")
        command_ids = [receipt[field] for field in ("verify_command_id", "consume_command_id", "durable_command_id", "acknowledge_command_id")]
        if len(set(command_ids)) != 4 or all_commands.intersection(command_ids):
            raise EventLogContractError("receipt command observation reused")
        all_commands.update(command_ids)
        commands = [_result(c, cid, events[pos]["execution_id"]) for cid, pos in zip(command_ids, positions)]
        for stage, (argv, result) in zip(stages, commands):
            _event_command(f"{operation}_receipt_{stage}", argv, result)
        verify = _snapshot_product(commands[0][1]) if operation in {"initial_snapshot", "rendered_snapshot"} else commands[0][1]
        consume, durable, ack = [command[1] for command in commands[1:]]
        result_id, revision, counts, digest = _receipt_identity(verify, consume, ack)
        _receipt_identity(verify, durable, ack)
        if (commands[1][0] != ["./brain", "--json", "source", "consume-sync-result", "--result-id", result_id]
                or commands[2][0] != commands[1][0]
                or commands[3][0] != ["./brain", "--json", "source", "acknowledge-sync-result", "--result-id", result_id]
                or durable["data"]["status"] != "already_consumed"
                or {key: value for key, value in durable["data"].items() if key != "status"}
                != {key: value for key, value in consume["data"].items() if key != "status"}):
            raise EventLogContractError("receipt replay or command form")
        for pos, cid in zip(positions, command_ids):
            if events[pos]["data"] != {"receipt_id": receipt["id"]} or events[pos]["corroboration_ids"] != [cid]:
                raise EventLogContractError("receipt event binding")
        reference = SyncResultReference.from_dict(_manifest(verify))
        publication_state = _source_state(c, command_ids[0], result_id=result_id, revision=revision)
        effects, actual_digest = _sync_stream(c, receipt["stream_artifact_id"], reference, verify["command"])
        _semantic(semantic.validate_stream_effects, effects, publication_state)
        parsed = _semantic(semantic.parse_manifest_response, verify, observed_exit_code=0 if verify["ok"] else 1)
        stream_header = _json(c.read(receipt["stream_artifact_id"], "sync_stream").splitlines()[0], "stream header")
        _semantic(semantic.validate_response_effects, parsed, effects, publication_state, generated_at=stream_header["generated_at"])
        authority = {"result_id": result_id, "corpus_revision": revision, "source_state": publication_state}
        consumption_state = _receipt_source_state(c, command_ids[1], authority, "consume")
        if actual_digest != digest:
            raise EventLogContractError("durable effect digest")
        durable_raw = c.obj(receipt["durable_artifact_id"], "consumption_receipt")
        _require_keys(durable_raw, {"schema_version", "result_id", "reference", "event_counts", "effect_digest", "handoff_delivery"}, "durable consumption")
        try:
            if SyncResultReference.from_dict(durable_raw["reference"]) != reference:
                raise ValueError("different durable reference")
            if durable_raw["handoff_delivery"] is not None:
                HandoffDeliveryReference.from_dict(durable_raw["handoff_delivery"])
        except (ValueError, TypeError) as error:
            raise EventLogContractError("durable consumption reference types") from error
        expected_durable = {
            "schema_version": 2, "result_id": result_id, "reference": reference.to_dict(),
            "event_counts": counts, "effect_digest": digest, "handoff_delivery": consume["data"]["handoff_delivery"],
        }
        if (type(durable_raw["schema_version"]) is not int or durable_raw != expected_durable
                or type(durable_raw["event_counts"]) is not dict
                or any(type(value) is not int for value in durable_raw["event_counts"].values())):
            raise EventLogContractError("durable consumption identity")
        sources = []
        for effect in effects:
            if effect["kind"] == "handoff_source_id":
                item = effect["data"]
                _require_keys(item, {"source_id", "record_sha256"}, "handoff source effect")
                if (type(item["source_id"]) is not str or not re.fullmatch(r"src_[0-9a-f]{64}", item["source_id"])
                        or not _sha(item["record_sha256"]) or item["source_id"] in sources):
                    raise EventLogContractError("duplicate or invalid handoff source effect")
                sources.append(item["source_id"])
        delivery_id = receipt["delivery_id"]
        required_delivery = f"{operation}_handoff_delivery_verified" in log.get("_scenario_required_events", names)
        consume_delivery = consume["data"]["handoff_delivery"]
        if delivery_id is None:
            if consume_delivery is not None or required_delivery or sources:
                raise EventLogContractError("missing or unexpected delivery")
        else:
            if not required_delivery or delivery_id in used_deliveries:
                raise EventLogContractError("unexpected or reused delivery")
            used_deliveries.add(delivery_id)
            delivery = deliveries.get(delivery_id)
            if delivery is None or delivery["receipt_id"] != receipt["id"]:
                raise EventLogContractError("delivery receipt binding")
            delivery_ref = HandoffDeliveryReference.from_dict(consume_delivery)
            raw = c.read(delivery["delivery_artifact_id"], "delivery")
            if hashlib.sha256(raw).hexdigest() != delivery_ref.sha256:
                raise EventLogContractError("immutable delivery digest")
            actual = _json(raw, "delivery")
            _require_keys(actual, {"schema_version", "result_id", "reference", "items"}, "delivery")
            try:
                if SyncResultReference.from_dict(actual["reference"]) != reference:
                    raise ValueError("different delivery reference")
            except (ValueError, TypeError) as error:
                raise EventLogContractError("delivery reference types") from error
            if (type(actual["schema_version"]) is not int or actual["schema_version"] != 1
                    or actual["result_id"] != result_id or actual["reference"] != reference.to_dict()
                    or type(actual["items"]) is not list or len(actual["items"]) != delivery_ref.item_count):
                raise EventLogContractError("immutable delivery identity")
            projected = []
            decoded = []
            try:
                for item in actual["items"]:
                    typed = _decode_item(item)
                    _validate_item(typed)
                    decoded.append(handoff_to_dict(typed))
                    projected.append({"item_id": typed.handoff_id, "kind": typed.kind, "handoff_id": typed.handoff_id,
                                      "handoff_source_id": typed.source_id, "payload_sha256": _canon(item)})
            except (ValueError, TypeError, AttributeError) as error:
                raise EventLogContractError("typed immutable delivery item") from error
            if (projected != delivery["items"] or [item["source_id"] for item in decoded] != sources
                    or len({item["handoff_id"] for item in decoded}) != len(decoded)):
                raise EventLogContractError("delivery does not match ordered durable effects")
            _semantic(semantic.validate_delivery_sources, effects, decoded, consumption_state)
            index = names.index(f"{operation}_handoff_delivery_verified")
            expected_mappings = [{"item_id": item["item_id"], "handoff_source_id": item["handoff_source_id"]} for item in projected]
            if (not positions[2] < index < positions[3]
                    or events[index]["data"] != {"delivery_id": delivery_id, "item_mappings": expected_mappings}
                    or events[index]["corroboration_ids"]):
                raise EventLogContractError("delivery mapping order")
        verified[receipt["id"]] = {
            "result_id": result_id, "corpus_revision": revision, "counts": counts, "effect_digest": digest,
            "delivery_id": delivery_id, "effects": effects, "ack_index": positions[3],
            "items": [] if delivery_id is None else decoded,
            "source_state": publication_state,
        }
    if used_deliveries != deliveries.keys():
        raise EventLogContractError("orphan delivery")
    return verified


def _check_reuse(names):
    if len(names) <= 1:
        return
    allowed = [
        ["public_web_access", "snapshot_used_source", "initial_snapshot_receipt_verified"],
        ["rendered_snapshot_url", "rendered_snapshot_receipt_verified", "verify_snapshot_active_representation"],
        ["register_extraction_handoff", "verify_registration_active_representation"],
        ["freshness_search_batched", "freshness_search_drained"],
        *[[f"source_pass_{part}", f"source_pass_{part}_drained"] for part in ("discovery", "expansion", "verification")],
    ]
    if names not in allowed:
        raise EventLogContractError("command reuse is not an explicit event alias")


def _audit_commands(c, log, executions, verified_receipts=None):
    """Every indexed invocation/result has one validated workflow purpose."""
    uses = set()
    manifests = set()
    support_targets, support_used = {}, set()
    receipt_roles = {}
    verified_receipts = {} if verified_receipts is None else verified_receipts
    for receipt in log.get("receipts", []):
        manifests.add(receipt["verify_command_id"])
        for role in ("verify", "consume", "durable", "acknowledge"):
            cid = receipt[role + "_command_id"]
            if cid in receipt_roles:
                raise EventLogContractError("unaccounted duplicate receipt command")
            receipt_roles[cid] = (receipt["id"], role)
    for event in log.get("events", []):
        record = c.obj(event["event_record_id"], "event_record")
        if "command_id" in record:
            uses.add(record["command_id"])
        if "cursor_proof_id" in record:
            uses.update(c.obj(record["cursor_proof_id"], "cursor_proof")["command_ids"])
        if "manifest_id" in record and event["name"] in {"wiki_apply", "persist_claim"}:
            _, manifest = _wiki_manifest(c, record["manifest_id"])
            support_targets.update({(item["staging_path"], item["sha256"]): record["command_id"] for item in manifest["changes"] if item["operation"] == "write"})
    result_owners = set()
    state_owners, pending_owners = set(), set()
    observed = {}
    for cid, entry in c.entries.items():
        if entry["type"] != "command_observation":
            continue
        observation = c.obj(cid, "command_observation")
        execution_id = observation.get("execution_id")
        if type(execution_id) is not str or execution_id not in executions:
            raise EventLogContractError("command has no execution")
        argv, result = _result(c, cid, execution_id)
        if observation["result_id"] in result_owners:
            raise EventLogContractError("command result has multiple invocation owners")
        result_owners.add(observation["result_id"])
        state_id = observation["source_state_id"]
        if state_id is not None:
            if state_id in state_owners:
                raise EventLogContractError("source state has multiple invocation owners")
            state_owners.add(state_id)
            state = c.obj(state_id, "source_state")
            if cid in receipt_roles:
                receipt_id, role = receipt_roles[cid]
                if receipt_id not in verified_receipts:
                    raise EventLogContractError("source-state has no validated receipt authority")
                _receipt_source_state(c, cid, verified_receipts[receipt_id], role)
            else:
                if argv[2:4] == ["source", "register-extraction"]:
                    revision = result["data"]["registration"]["corpus_revision"]
                elif argv[2:4] == ["wiki", "apply"]:
                    revision = result["data"]["corpus_revision"]
                else:
                    raise EventLogContractError("source-state has no command checkpoint role")
                checkpoint = _source_state(c, cid, result_id=None, revision=revision)
                if checkpoint.pending_checkpoint is not None:
                    raise EventLogContractError("pending checkpoint outside receipt lifecycle")
            pending_id = state["pending_result_id"]
            if pending_id is not None:
                if pending_id in pending_owners:
                    raise EventLogContractError("pending result capture has multiple state owners")
                pending_owners.add(pending_id)
        observed[cid] = observation
        args = argv[2:]
        origin = args[0] in {"sync", "init"} or args[:2] == ["source", "snapshot-url"] or args[:4] == ["eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.initial"]
        if origin != (cid in manifests):
            raise EventLogContractError("unaccounted manifest-producing command")
        if args[:2] == ["source", "snapshot-url"]:
            _rendered_argv(argv)
        if args[0] != "eval":
            if cid not in uses:
                raise EventLogContractError("unreferenced product command")
            continue
        if (log["scenario_id"] != "web-approval-and-capture" or log["network_mode"] != "mock_only"
                or executions[execution_id]["phase"] != "approved_capture"):
            raise EventLogContractError("fixture eval outside authorized phase")
        if args[:2] == ["eval", "mock-web-capture"]:
            rendered = len(args) > 3 and args[3] == "web-approval-and-capture.rendered"
            _fixture_argv(argv, rendered=rendered)
            if cid not in uses:
                raise EventLogContractError("unreferenced capture command")
        elif args[:2] == ["eval", "sha256"]:
            if len(args) != 4 or args[2] != "--path" or not re.fullmatch(
                    r"\.brain/wiki-staging/wstg_[0-9a-f]{32}/files/wiki/questions/[a-z0-9-]+\.md", args[3]):
                raise EventLogContractError("fixture sha256 scope")
            _require_keys(result["data"], {"path", "sha256"}, "fixture sha256 result")
            if result["data"]["path"] != args[3] or not _sha(result["data"]["sha256"]):
                raise EventLogContractError("fixture sha256 identity")
            target = (args[3], result["data"]["sha256"])
            if target not in support_targets or target in support_used:
                raise EventLogContractError("unreferenced fixture sha256 support")
            support_used.add(target)
            publication = c.obj(support_targets[target], "command_observation")
            if publication["execution_id"] != execution_id or observation["end_sequence"] >= publication["start_sequence"]:
                raise EventLogContractError("fixture sha256 support ordering after publication")
        else:
            raise EventLogContractError("unknown fixture eval command")
    if result_owners != {key for key, value in c.entries.items() if value["type"] == "command_result"}:
        raise EventLogContractError("unaccounted command result artifact")
    if state_owners != {key for key, value in c.entries.items() if value["type"] == "source_state"}:
        raise EventLogContractError("unaccounted source state artifact")
    if pending_owners != {key for key, value in c.entries.items() if value["type"] == "pending_sync_result"}:
        raise EventLogContractError("unaccounted pending result artifact")
    if not uses <= observed.keys() or not manifests <= observed.keys():
        raise EventLogContractError("unaccounted command reference")
    # The independently recorded trace supplies the order, not event array order.
    if observed:
        phases = {key: index for index, key in enumerate(executions)}
        pending, next_role = None, None
        last_timestamp = None
        for cid in sorted(observed, key=lambda key: (phases[observed[key]["execution_id"]], observed[key]["start_sequence"])):
            observation = observed[cid]
            timestamp = datetime.fromisoformat(observation["timestamp"].replace("Z", "+00:00"))
            if last_timestamp is not None and timestamp < last_timestamp:
                raise EventLogContractError("command timestamp ordering")
            last_timestamp = timestamp
            role = receipt_roles.get(cid)
            if pending is not None:
                if role != (pending, next_role):
                    raise EventLogContractError("command before pending receipt completed")
                next_role = {"consume": "durable", "durable": "acknowledge", "acknowledge": None}[next_role]
                if next_role is None:
                    pending = None
            elif role is not None:
                if role[1] != "verify":
                    raise EventLogContractError("receipt invocation ordering")
                pending, next_role = role[0], "consume"
        if pending is not None:
            raise EventLogContractError("unconsumed pending receipt at run completion")


def _approval(c, approval_id, event, log):
    value = c.obj(approval_id, "approval")
    _require_keys(value, {"run_id", "execution_id", "event_id", "scope", "note", "decision", "fixture_id", "capability_id", "manifest_id"}, "approval")
    for field in ("event_id", "scope", "note", "decision"):
        _text(value[field], field)
    if (value["run_id"] != log["run_id"] or value["execution_id"] != event["execution_id"]
            or {field: value[field] for field in ("event_id", "scope", "note", "decision")} != log["_manifest"]["approval"]):
        raise EventLogContractError("approval binding")
    if event["name"] == "ask_web_approval":
        if (log["scenario_id"] != "web-approval-and-capture" or value["decision"] != "approved"
                or value["fixture_id"] != "web-approval-and-capture.initial"
                or value["capability_id"] != log["_manifest"]["fixture_capability_id"]
                or value["manifest_id"] is not None):
            raise EventLogContractError("web approval scope")
    elif (log["scenario_id"] != "contradictory-evidence" or value["decision"] not in {"approved", "denied", "withheld"}
          or value["fixture_id"] is not None or value["capability_id"] is not None
          or type(value["manifest_id"]) is not str):
        raise EventLogContractError("interpretation approval scope")
    return value


def _capability(c, capability_id, event, log, approval):
    manifest = log["_manifest"]
    capability = c.obj(capability_id, "fixture_capability")
    _require_keys(capability, {"run_id", "execution_id", "phase", "fixture_id", "fixture_sha256", "capability_id",
                               "approval_event_id", "scope", "note", "enabled_fixture_ids", "descriptor_id"}, "fixture capability")
    if (log["network_mode"] != "mock_only" or log["scenario_id"] != "web-approval-and-capture"
            or capability_id != manifest["fixture_capability_id"]
            or capability["capability_id"] != capability_id or capability["run_id"] != log["run_id"]
            or capability["execution_id"] != event["execution_id"] or capability["phase"] != "approved_capture"
            or capability["fixture_id"] != "web-approval-and-capture"
            or capability["fixture_sha256"] != log["fixture_sha256"]
            or capability["approval_event_id"] != approval["event_id"]
            or capability["scope"] != approval["scope"] or capability["note"] != approval["note"]
            or capability["enabled_fixture_ids"] != ["web-approval-and-capture.initial", "web-approval-and-capture.rendered"]):
        raise EventLogContractError("fixture capability binding")
    descriptor = c.obj(capability["descriptor_id"], "fixture_descriptor")
    _require_keys(descriptor, {"fixture_id", "fixture_sha256", "static_sha256", "rendered_sha256", "unused_sha256",
                              "final_url", "media_type", "retrieved_at", "redirect_urls"}, "fixture descriptor")
    if descriptor["fixture_id"] != capability["fixture_id"] or descriptor["fixture_sha256"] != log["fixture_sha256"]:
        raise EventLogContractError("fixture descriptor identity")
    fixture_root = Path(__file__).parent / "fixtures/web-approval-and-capture"
    for key, filename in (("static_sha256", "static-shell.html"), ("rendered_sha256", "rendered-dom.html"), ("unused_sha256", "unused-candidate.html")):
        if descriptor[key] != hashlib.sha256((fixture_root / filename).read_bytes()).hexdigest():
            raise EventLogContractError("fixture descriptor bytes")
    _text(descriptor["final_url"])
    _text(descriptor["media_type"])
    _timestamp(descriptor["retrieved_at"])
    if type(descriptor["redirect_urls"]) is not list or any(type(url) is not str for url in descriptor["redirect_urls"]):
        raise EventLogContractError("fixture redirects")
    return descriptor


def _wiki_manifest(c, artifact_id):
    from brainlib.wiki_transaction import WikiManifest
    capture = c.obj(artifact_id, "wiki_manifest")
    _require_keys(capture, {"path", "content_id", "sha256"}, "wiki manifest capture")
    _relative_path(capture["path"])
    raw = c.read(capture["content_id"], "file_capture")
    if hashlib.sha256(raw).hexdigest() != capture["sha256"]:
        raise EventLogContractError("captured manifest digest")
    try:
        parsed = WikiManifest.from_json(raw.decode())
    except (ValueError, UnicodeError, TypeError) as error:
        raise EventLogContractError("production wiki manifest") from error
    return capture, parsed.to_dict()


def _packet(c, packet_id, event, cursors):
    from brainlib.citations import parse_citation_definitions
    from brainlib.contracts import SourceRecord, compute_corpus_revision
    from brainlib.evidence import CitationRef
    value = c.obj(packet_id, "evidence_packet")
    packet_keys = {"run_id", "execution_id", "kind", "corpus_revision", "cursor_proof_ids", "ledger_ids",
                   "citations", "complete", "current", "reason", "snapshot_id"}
    if set(value) != packet_keys:
        if set(value) == packet_keys - {"snapshot_id"}:
            raise EventLogContractError("evidence packet workspace snapshot binding")
        raise EventLogContractError("evidence packet shape")
    if (value["run_id"] != c.run_id or value["execution_id"] != event["execution_id"]
            or value["kind"] not in {"wiki", "source", "insufficiency"}
            or type(value["complete"]) is not bool or type(value["current"]) is not bool
            or not _sha(value["corpus_revision"]) or type(value["cursor_proof_ids"]) is not list
            or not value["cursor_proof_ids"] or len(set(value["cursor_proof_ids"])) != len(value["cursor_proof_ids"])
            or any(proof_id not in cursors for proof_id in value["cursor_proof_ids"])
            or type(value["citations"]) is not list or type(value["ledger_ids"]) is not list):
        raise EventLogContractError("evidence packet bindings")
    marker_snapshot_id, marker_snapshot = _marker_workspace_snapshot(c, event)
    if value["snapshot_id"] != marker_snapshot_id:
        raise EventLogContractError("evidence packet workspace snapshot binding")
    inventory = {entry["path"]: entry for entry in marker_snapshot["inventory"]}
    proofs = [cursors[proof_id] for proof_id in value["cursor_proof_ids"]]
    if any(proof["first"]["corpus_revision"] != value["corpus_revision"] for proof in proofs):
        raise EventLogContractError("packet search revision")
    records = []
    for artifact_id in value["ledger_ids"]:
        raw = c.read(artifact_id, "source_record")
        try:
            record = SourceRecord.from_dict(_json(raw, "packet ledger record"))
        except (ValueError, TypeError, KeyError) as error:
            raise EventLogContractError("packet ledger codec") from error
        captured = inventory.get(f"sources/ledger/{record.source_id}.json")
        if captured is None or raw != c.read(captured["content_id"], "file_capture"):
            raise EventLogContractError("evidence packet workspace ledger binding")
        records.append(record)
    if len({record.source_id for record in records}) != len(records) or compute_corpus_revision(records) != value["corpus_revision"]:
        raise EventLogContractError("packet complete ledger revision")
    sources = {record.source_id: record for record in records}
    seen = set()
    for entry in value["citations"]:
        _require_keys(entry, {"document_path", "citation_id", "document_id"}, "packet citation")
        try:
            ref = CitationRef(PurePosixPath(entry["document_path"]), entry["citation_id"])
        except (ValueError, TypeError, KeyError) as error:
            raise EventLogContractError("revalidated citation identity") from error
        captured = inventory.get(ref.document_path.as_posix())
        if captured is None or captured["content_id"] != entry["document_id"]:
            raise EventLogContractError("evidence packet workspace citation binding")
        try:
            document = c.read(entry["document_id"], "file_capture").decode()
            citations = parse_citation_definitions(document, path=Path(entry["document_path"]))
            citation = next(item for item in citations if item.citation_id == ref.citation_id)
            source = sources[citation.source_id]
            derivation = source.derivations[citation.derivation_id]
            if (source.active_content_sha256 != citation.content_sha256 or source.active_derivation_id != citation.derivation_id
                    or derivation.source_sha256 != citation.content_sha256 or citation.anchor not in derivation.anchors):
                raise ValueError("citation no longer current")
        except (ValueError, TypeError, KeyError, StopIteration, UnicodeError) as error:
            raise EventLogContractError("revalidated citation identity") from error
        identity = (ref.document_path.as_posix(), ref.citation_id)
        if identity in seen:
            raise EventLogContractError("duplicate packet citation")
        seen.add(identity)
        if not any(match["path"] == ref.document_path.as_posix() for proof in proofs for match in proof["records"]):
            raise EventLogContractError("citation not from completed wiki search")
    name = event["name"]
    if name in {"build_wiki_evidence_packet", "judge_sufficient"}:
        if value["kind"] != "wiki" or not value["citations"] or value["complete"] is not True or value["current"] is not True or value["reason"] is not None:
            raise EventLogContractError("wiki packet is not complete/current/nonempty")
    elif name == "revalidate_underlying_citations" and not value["citations"]:
        raise EventLogContractError("revalidation has no citations")
    elif name == "judge_insufficient" and value["reason"] not in {"no_supported_evidence", "incomplete", "stale", "contradiction"}:
        raise EventLogContractError("insufficiency reason")
    return value


def _validate_special_events(c, log, events, executions, receipts):
    names = [event["name"] for event in events]
    cursors, starts, staged, approvals, branches, registrations = {}, {}, {}, {}, {}, {}
    command_users = {}
    last_revision = None
    accumulated_ids, rewrites = set(), []
    for index, event in enumerate(events):
        name = event["name"]
        record = c.obj(event["event_record_id"], "event_record")
        cid = record.get("command_id")
        argv, result = (None, None) if cid is None else _result(c, cid, event["execution_id"])
        if cid is not None:
            command_users.setdefault(cid, []).append(name)
        phase = executions[event["execution_id"]]["phase"]
        if log["network_mode"] == "mock_only":
            approval_events = set(required_events_for_phase(log["_scenario"], "approval"))
            if phase != ("approval" if name in approval_events else "approved_capture"):
                raise EventLogContractError("event phase")
        if name.endswith("_receipt_acknowledged"):
            receipt = receipts[event["data"]["receipt_id"]]
            last_revision = receipt["corpus_revision"]
            for effect in receipt["effects"]:
                if effect["kind"] == "new_active_representation":
                    accumulated_ids.add(effect["data"]["source_id"])
                elif effect["kind"] == "citation_rewrite":
                    rewrites.append(effect["data"])
        if name.startswith("branch_"):
            delivery_id = event["data"]["delivery_id"]
            preceding = [receipt for receipt in receipts.values() if receipt["ack_index"] < index]
            if not preceding:
                raise EventLogContractError("branch before acknowledgement")
            receipt = max(preceding, key=lambda receipt: receipt["ack_index"])
            if receipt["delivery_id"] != delivery_id or index != receipt["ack_index"] + 1:
                raise EventLogContractError("branch must use immediately prior acknowledged delivery")
            kind = "extraction" if name == "branch_extraction_handoff" else "rendered_web_capture"
            item = next((item for item in receipt["items"] if item["handoff_id"] == event["data"]["item_id"]), None)
            if (item is None or item["kind"] != kind or event["data"]["handoff_id"] != item["handoff_id"]
                    or event["data"]["handoff_source_id"] != item["source_id"]):
                raise EventLogContractError("branch typed handoff identity")
            branches[kind] = item
        if name in {"freshness_search_batched", "source_pass_discovery", "source_pass_expansion", "source_pass_verification"}:
            if name == "freshness_search_batched" and (not accumulated_ids or result["data"]["searched_source_ids"] != sorted(accumulated_ids)):
                raise EventLogContractError("freshness newly-active identity")
            previous = {"source_pass_expansion": "source_pass_discovery_drained", "source_pass_verification": "source_pass_expansion_drained"}.get(name)
            if previous is not None and previous not in names[:index]:
                raise EventLogContractError("source pass before preceding drain")
            if last_revision is not None and result["data"]["corpus_revision"] != last_revision:
                raise EventLogContractError("search uses unacknowledged revision")
            starts[name] = cid
        if name.endswith("_drained"):
            family = name.removesuffix("_drained")
            if family == "freshness_search":
                family = "freshness_search_batched"
            proof = _cursor(c, record["cursor_proof_id"], event["execution_id"], family, cid)
            if family in starts and proof["command_ids"][0] != starts[family]:
                raise EventLogContractError("drain does not match preceding start")
            if last_revision is not None and proof["first"]["corpus_revision"] != last_revision:
                raise EventLogContractError("cursor uses unacknowledged revision")
            cursors[record["cursor_proof_id"]] = proof
        if name == "wiki_no_supported_evidence":
            proof = cursors.get(record["cursor_proof_id"])
            if proof is None or proof["family"] != "wiki_search" or proof["records"]:
                raise EventLogContractError("no-supported-evidence requires completed empty wiki search")
        if "packet_id" in record:
            _packet(c, record["packet_id"], event, cursors)
        if name in {"ask_web_approval", "ask_interpretation_approval"}:
            approvals[record["approval_id"]] = _approval(c, record["approval_id"], event, log)
        if name == "report_local_evidence_gap":
            marker = c.obj(event["transcript_marker_id"], "marker")
            text = c.native_records[event["execution_id"]][marker["native_record_start"]]["texts"][marker["text_pointer"]]
            if "prior standard" not in text or phase != "approval":
                raise EventLogContractError("documented local evidence gap")
        if name in {"public_web_access", "snapshot_used_source", "stage_faithful_browser_capture", "select_used_source"}:
            approval = next(iter(approvals.values()), None)
            if approval is None or approval["decision"] != "approved" or approval["execution_id"] == event["execution_id"]:
                raise EventLogContractError("web requires prior phase approval")
            descriptor = _capability(c, record["capability_id"], event, log, approval)
            if name != "select_used_source" and record["approval_id"] not in approvals:
                raise EventLogContractError("capture approval reference")
            if name in {"public_web_access", "snapshot_used_source"}:
                values = _fixture_argv(argv, rendered=False)
                if any(values["--approval-" + key] != approval["event_id" if key == "event-id" else key] for key in ("event-id", "scope", "note")):
                    raise EventLogContractError("capture approval values")
                snapshot = _snapshot_product(result)["data"]["snapshot"]
                if snapshot["content_sha256"] != descriptor["static_sha256"]:
                    raise EventLogContractError("static capture fixture bytes")
            elif name == "stage_faithful_browser_capture":
                item = branches.get("rendered_web_capture")
                if item is None or result["data"]["handoff_id"] != item["handoff_id"] or result["data"]["sha256"] != descriptor["rendered_sha256"]:
                    raise EventLogContractError("rendered staging handoff/fixture")
                staged["rendered"] = result["data"]
        if "diff_id" in record:
            diff = _event_diff(c, record["diff_id"], event)
            changed = diff["changed_paths"]
            if name in {"classify_repository_development", "use_software_workflow"}:
                allowed = log["_manifest"]["software_paths"]
                if any(path.startswith(("wiki/", "sources/", ".brain/")) or path not in allowed for path in changed):
                    raise EventLogContractError("nonarchival development paths")
            elif name == "stage_handoff_scoped_extraction":
                item = branches.get("extraction")
                if item is None or len(changed) != 1:
                    raise EventLogContractError("extraction staging delivery")
                _scoped_path(changed[0], ".brain/agent-staging/" + item["handoff_id"] + "/")
                staged["extraction"] = next(entry for entry in diff["after"] if entry["path"] == changed[0])
            elif name in {"write_wiki_question", "stage_existing_question_update", "preserve_both_claims", "cite_both_sides"}:
                if not changed or any(not re.fullmatch(r"\.brain/wiki-staging/[A-Za-z0-9][A-Za-z0-9_.-]*/files/wiki/(questions|pages)/[a-z0-9-]+\.md", path) for path in changed):
                    raise EventLogContractError("wiki writes must be staged")
                if name in {"write_wiki_question", "stage_existing_question_update"} and any("/files/wiki/questions/" not in path for path in changed):
                    raise EventLogContractError("question staging target")
                for entry in diff["after"]:
                    if entry["path"] in changed:
                        staged[entry["path"]] = entry
                if name in {"preserve_both_claims", "cite_both_sides"}:
                    _both_claims(c, diff)
            elif name == "select_used_source":
                for entry in diff["after"]:
                    if entry["path"] in changed and entry["sha256"] == descriptor["unused_sha256"]:
                        raise EventLogContractError("unused candidate persisted")
                used = [entry for entry in diff["after"] if entry["path"].startswith("sources/raw/_web/") and entry["sha256"] == descriptor["static_sha256"]]
                if len(used) != 1:
                    raise EventLogContractError("selected source is not the used fixture capture")
        if name == "register_extraction_handoff":
            item = branches.get("extraction")
            preparation = staged.get("extraction")
            registration = result["data"]["registration"]
            if (item is None or preparation is None or argv[5] != item["handoff_id"] or argv[7] != preparation["path"]
                    or registration["source_id"] != item["source_id"] or registration["content_sha256"] != item["content_sha256"]
                    or registration["active_representation"]["output_sha256"] != preparation["sha256"]):
                raise EventLogContractError("registration delivered staging identity")
            registrations[cid] = registration
            state = _source_state(c, cid, result_id=None, revision=registration["corpus_revision"])
            _semantic(semantic.require_active_representation, registration["active_representation"], state)
        if name == "verify_registration_active_representation":
            if index == 0 or events[index - 1]["name"] != "register_extraction_handoff" or cid not in registrations:
                raise EventLogContractError("registration activation observation")
            # The next sync must retain the activated representation/corpus.
            last_revision = registrations[cid]["corpus_revision"]
            accumulated_ids.add(registrations[cid]["source_id"])
        if name == "post_registration_sync_receipt_verified" and registrations:
            if _manifest(result)["corpus_revision"] != last_revision:
                raise EventLogContractError("post-registration sync activation revision")
        if name == "rendered_snapshot_url":
            item = branches.get("rendered_web_capture")
            preparation = staged.get("rendered")
            approval = next(iter(approvals.values()), None)
            if item is None or preparation is None or approval is None:
                raise EventLogContractError("rendered capture has no delivered staging")
            descriptor = _capability(c, log["_manifest"]["fixture_capability_id"], event, log, approval)
            values = _rendered_argv(argv)
            expected = {"--source-id": item["source_id"], "--handoff-id": item["handoff_id"], "--rendered-staging-path": preparation["path"],
                        "--retrieved-at": descriptor["retrieved_at"], "--final-url": descriptor["final_url"], "--detected-media-type": descriptor["media_type"],
                        "--approval-event-id": approval["event_id"], "--approval-scope": approval["scope"], "--approval-note": approval["note"],
                        "redirect_urls": descriptor["redirect_urls"]}
            if values != expected or result["data"]["snapshot"]["content_sha256"] != descriptor["rendered_sha256"]:
                raise EventLogContractError("rendered snapshot fixture selectors")
        if name == "verify_snapshot_active_representation":
            log["_rendered_activation"] = result["data"]["snapshot"]["active_representation"]
        if name in {"wiki_apply", "persist_claim", "wiki_reconcile_citations"}:
            capture, manifest = _wiki_manifest(c, record["manifest_id"])
            if argv[-1] != capture["path"] or manifest["expected_corpus_revision"] != last_revision or result["data"]["corpus_revision"] != last_revision:
                raise EventLogContractError("wiki manifest revision/path")
            if name == "wiki_reconcile_citations":
                expected_rewrites = sorted(rewrites, key=lambda item: (item["source_id"], item["content_sha256"], item["raw_path"]))
                if (manifest["change_intent"] != "routine" or manifest["approval_event_id"] is not None
                        or manifest["changes"] or manifest["link_candidate_runs"] or manifest["citation_rewrites"] != expected_rewrites):
                    raise EventLogContractError("citation reconciliation manifest")
            else:
                _applied_wiki(c, record, manifest, result, staged, cursors, approvals, log)
    for users in command_users.values():
        _check_reuse(users)


def _both_claims(c, diff):
    from brainlib.citations import parse_citation_definitions
    from brainlib.markdown import scan_markdown
    from tests.evals.generate_scenarios import SCENARIOS

    policy = next(item["interpretation_policy"] for item in SCENARIOS if item["id"] == "contradictory-evidence")
    required = {tuple(item[key] for key in ("source_id", "content_sha256", "derivation_id")) for item in policy["claim_identities"]}
    found = set()
    for entry in diff["after"]:
        if entry["path"] not in diff["changed_paths"]:
            continue
        body = c.read(entry["content_id"], "file_capture").decode()
        scan = scan_markdown(body)
        if scan.diagnostics:
            raise EventLogContractError("claim citation Markdown")
        citations = parse_citation_definitions(body, path=Path(entry["path"]))
        definition_lines = {citation.definition_line for citation in citations}
        used = {marker.citation_id for marker in scan.citation_markers if marker.line not in definition_lines}
        for citation in citations:
            if citation.citation_id not in used:
                raise EventLogContractError("claim citation is not used")
            found.add((citation.source_id, citation.content_sha256, citation.derivation_id))
    if not required <= found:
        raise EventLogContractError("both dated claims/citations must be retained")


def _interpretation_artifact_id(c, scenario):
    from tests.evals.generate_scenarios import validate_interpretation_policy

    try:
        validate_interpretation_policy(scenario)
    except ScenarioContractError as error:
        raise EventLogContractError("interpretation policy") from error
    ids = [name for name, entry in c.entries.items() if entry["type"] == "interpretation_decision"]
    expected_count = 1 if scenario["id"] == "contradictory-evidence" else 0
    if len(ids) != expected_count:
        raise EventLogContractError("interpretation_decision artifact cardinality")
    return ids[0] if ids else None


def _interpretation_claims(c, snapshot, path, body, question, state, checkpoint):
    """Resolve terminal usages against the final apply's source checkpoint."""
    from brainlib.citations import encode_markdown_path, parse_citation_definitions
    from brainlib.extractors.adapters import validate_markdown
    from brainlib.ledger import representation_for
    from brainlib.markdown import scan_markdown
    from brainlib.wiki_interpretations import validate_interpretation_document

    logical_root = Path("/__eval_logical_root__")
    document = logical_root / path
    if validate_interpretation_document(document, body, question):
        raise EventLogContractError("terminal interpretation document semantics")
    scan = scan_markdown(body)
    citations = parse_citation_definitions(body, path=document)
    definition_lines = {citation.definition_line for citation in citations}
    used = {marker.citation_id for marker in scan.citation_markers if marker.line not in definition_lines}
    if (scan.diagnostics or len({citation.citation_id for citation in citations}) != len(citations)
            or used != {citation.citation_id for citation in citations}):
        raise EventLogContractError("terminal interpretation citation usages")
    inventory = {entry["path"]: entry for entry in snapshot["inventory"]}
    ledger_paths = {path for path in inventory if path.startswith("sources/ledger/src_") and path.endswith(".json")}
    if ledger_paths != {entry["path"] for entry in checkpoint["records"]}:
        raise EventLogContractError("terminal interpretation apply checkpoint ledger identity")
    for entry in checkpoint["records"]:
        if c.read(inventory[entry["path"]]["content_id"], "file_capture") != c.read(entry["artifact_id"], "source_record"):
            raise EventLogContractError("terminal interpretation ledger differs from final apply checkpoint")
    for source_path, raw in state.files.items():
        if source_path == "config/extractors.toml":
            continue
        if (source_path not in inventory
                or c.read(inventory[source_path]["content_id"], "file_capture") != raw):
            raise EventLogContractError("terminal interpretation source bytes differ from final apply checkpoint")
    if state.corpus_revision != question.corpus_revision:
        raise EventLogContractError("terminal interpretation corpus revision")
    identities = set()
    for citation in citations:
        record = state.records[citation.source_id]
        representation = representation_for(record, citation.content_sha256, citation.derivation_id)
        if (representation is None or citation.anchor not in representation.anchors
                or record.active_content_sha256 != citation.content_sha256
                or record.active_derivation_id != citation.derivation_id):
            raise EventLogContractError("terminal interpretation resolving citation")
        raw_path = "sources/raw/" + representation.raw_path.as_posix()
        extracted_path = representation.extracted_path.as_posix()
        if (citation.original_destination != encode_markdown_path(document, logical_root / raw_path)
                or citation.extracted_destination != encode_markdown_path(document, logical_root / extracted_path)
                + f"#{citation.anchor.kind}:{citation.anchor.value}"):
            raise EventLogContractError("terminal interpretation citation destinations")
        version = record.versions[citation.content_sha256]
        derivation = record.derivations[citation.derivation_id]
        for target, sha, size in ((raw_path, citation.content_sha256, version.byte_size),
                                  (extracted_path, derivation.output_sha256, derivation.output_byte_size)):
            if target not in state.files:
                raise EventLogContractError("terminal interpretation final apply checkpoint missing source capture")
            entry = inventory[target]
            if entry["sha256"] != sha or entry["bytes"] != size:
                raise EventLogContractError("terminal interpretation retained source bytes")
        validate_markdown(c.read(inventory[extracted_path]["content_id"], "file_capture"), representation.anchors,
                          expected_anchors=tuple(sorted({anchor.kind for anchor in representation.anchors})),
                          max_output_bytes=derivation.output_byte_size)
        identities.add((citation.source_id, citation.content_sha256, citation.derivation_id))
    return [dict(zip(("source_id", "content_sha256", "derivation_id"), identity)) for identity in sorted(identities)]


def _require_interpretation_policy_state(question, policy, approval, manifest):
    """The assertion predicate consumes parsed v2 state, never answer prose."""
    decision = question.interpretation
    if decision.decision != policy["expected_decision"] or approval["decision"] != policy["expected_approval_decision"]:
        raise EventLogContractError("terminal interpretation policy decision")
    if decision.decision == "unresolved":
        if (decision.preference_citation_id is not None or decision.approval_event_id is not None
                or approval["decision"] != "withheld" or manifest["change_intent"] != "routine"
                or manifest["approval_event_id"] is not None):
            raise EventLogContractError("withheld terminal interpretation state")
    elif decision.decision == "preferred":
        if (approval["decision"] != "approved" or manifest["change_intent"] != "resolve_contradiction"
                or not decision.preference_citation_id
                or decision.approval_event_id != approval["event_id"]
                or manifest["approval_event_id"] != approval["event_id"]):
            raise EventLogContractError("approved terminal interpretation transaction")
    else:
        raise EventLogContractError("unsupported terminal interpretation decision")


def _validate_interpretation_decision(c, log, scenario, artifact_id):
    from brainlib.wiki_models import parse_question
    from brainlib.wiki_interpretations import validate_interpretation_transitions
    from tests.evals.generate_scenarios import interpretation_policy_sha256

    if artifact_id is None:
        return
    value = c.obj(artifact_id, "interpretation_decision")
    fields = {"schema_version", "run_id", "execution_id", "scenario_id", "scenario_sha256", "interpretation_policy_sha256",
              "approval_event_record_id", "approval_marker_id", "approval_id", "approval_event_id", "approval_decision",
              "validate_event_record_id", "validate_marker_id", "terminal_snapshot_id", "wiki_apply_event_record_id",
              "wiki_apply_command_id", "wiki_manifest_id", "question_path", "question_capture_id", "question_sha256",
              "question_id", "interpretation", "claim_identities"}
    _require_keys(value, fields, "interpretation_decision")
    _require_keys(value["interpretation"], {"decision", "preference_citation_id", "approval_event_id"}, "interpretation state")
    events = {}
    for name in ("ask_interpretation_approval", "write_wiki_question", "wiki_apply", "validate"):
        matches = [event for event in log["events"] if event["name"] == name]
        if len(matches) != 1:
            raise EventLogContractError("interpretation exact event cardinality")
        events[name] = matches[0]
    approval_event, write, apply, terminal = events.values()
    if len({event["execution_id"] for event in events.values()}) != 1:
        raise EventLogContractError("interpretation cross-writer event")
    approval_record = c.obj(approval_event["event_record_id"], "event_record")
    if approval_record["interpretation_decision_id"] != artifact_id:
        raise EventLogContractError("interpretation approval decision join")
    approval = _approval(c, approval_record["approval_id"], approval_event, log)
    approval_ids = {name for name, entry in c.entries.items() if entry["type"] == "approval"}
    if approval_ids != {approval_record["approval_id"]}:
        raise EventLogContractError("interpretation approval artifact cardinality")
    apply_record = c.obj(apply["event_record_id"], "event_record")
    _, manifest = _wiki_manifest(c, apply_record["manifest_id"])
    snapshot_id, snapshot = _marker_workspace_snapshot(c, terminal)
    policy = scenario["interpretation_policy"]
    path = policy["question_path"]
    capture = next((entry for entry in snapshot["inventory"] if entry["path"] == path), None)
    if capture is None or path in snapshot["staged_paths"]:
        raise EventLogContractError("terminal interpretation question capture")
    expected = {"run_id": c.run_id, "execution_id": terminal["execution_id"], "scenario_id": scenario["id"],
                "scenario_sha256": scenario_sha256(scenario), "interpretation_policy_sha256": interpretation_policy_sha256(scenario),
                "approval_event_record_id": approval_event["event_record_id"], "approval_marker_id": approval_event["transcript_marker_id"],
                "approval_id": approval_record["approval_id"], "approval_event_id": approval["event_id"], "approval_decision": approval["decision"],
                "validate_event_record_id": terminal["event_record_id"], "validate_marker_id": terminal["transcript_marker_id"],
                "terminal_snapshot_id": snapshot_id, "wiki_apply_event_record_id": apply["event_record_id"],
                "wiki_apply_command_id": apply_record["command_id"], "wiki_manifest_id": apply_record["manifest_id"],
                "question_path": path, "question_capture_id": capture["content_id"], "question_sha256": capture["sha256"],
                "question_id": policy["question_id"]}
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or any(value[key] != expected[key] for key in expected):
        raise EventLogContractError("interpretation_decision exact evidence join")
    positions = [_marker_workspace_snapshot(c, event)[1]["trace_sequence"] for event in events.values()]
    command = c.obj(apply_record["command_id"], "command_observation")
    _, apply_result = _result(c, apply_record["command_id"], apply["execution_id"])
    state = _source_state(c, apply_record["command_id"], result_id=None, revision=apply_result["data"]["corpus_revision"])
    checkpoint = c.obj(command["source_state_id"], "source_state")
    execution = next(item for item in log["executions"] if item["id"] == apply["execution_id"])
    process = c.obj(execution["process_id"], "process")
    trace = c.obj(process["trace_id"], "execution_trace")
    checkpoints = [entry["sequence"] for entry in trace["records"] if entry["kind"] == "source_state"
                   and entry["command_id"] == apply_record["command_id"] and entry["source_state_id"] == command["source_state_id"]]
    if len(checkpoints) != 1:
        raise EventLogContractError("interpretation final apply checkpoint trace cardinality")
    validate_record = c.obj(terminal["event_record_id"], "event_record")
    validate_command = c.obj(validate_record["command_id"], "command_observation")
    if not (positions[0] < positions[1] < command["start_sequence"] < checkpoints[0] < command["end_sequence"] < positions[2]
            < validate_command["start_sequence"] < validate_command["end_sequence"] < positions[3]):
        raise EventLogContractError("interpretation approval/write/apply/validate trace ordering")
    diff = _event_diff(c, apply_record["diff_id"], apply)
    promoted = next((entry for entry in diff["after"] if entry["path"] == path), None)
    changes = [change for change in manifest["changes"] if change["path"] == path]
    if (promoted != capture or path not in diff["changed_paths"] or len(changes) != 1
            or changes[0]["operation"] != "write" or changes[0]["sha256"] != capture["sha256"]
            or approval["manifest_id"] != apply_record["manifest_id"]):
        raise EventLogContractError("terminal interpretation apply/manifest capture join")
    body = c.read(capture["content_id"], "file_capture").decode("utf-8", "strict")
    question = parse_question(Path(path), text=body)
    actual = {"decision": question.interpretation.decision, "preference_citation_id": question.interpretation.preference_citation_id,
              "approval_event_id": question.interpretation.approval_event_id}
    if question.question_id != policy["question_id"] or value["interpretation"] != actual:
        raise EventLogContractError("terminal interpretation parsed v2 state")
    claims = _interpretation_claims(c, snapshot, path, body, question, state, checkpoint)
    if value["claim_identities"] != claims or claims != policy["claim_identities"]:
        raise EventLogContractError("terminal interpretation exact claim identities")
    before = next((entry for entry in diff["before"] if entry["path"] == path), None)
    validate_interpretation_transitions(
        before={} if before is None else {Path(path): c.read(before["content_id"], "file_capture").decode()},
        after={Path(path): body}, changed_paths=[Path(path)], change_intent=manifest["change_intent"],
        approval_event_id=manifest["approval_event_id"],
    )
    _require_interpretation_policy_state(question, policy, approval, manifest)


def _applied_wiki(c, record, manifest, result, staged, cursors, approvals, log):
    diff = _diff(c, record["diff_id"])
    reported = result["data"]["changed_paths"]
    if sorted(path for path in diff["changed_paths"] if path.startswith("wiki/")) != reported:
        raise EventLogContractError("wiki apply promoted paths differ from diff")
    # The transaction, never an agent manifest target, regenerates this index.
    changed = [path for path in reported if path != "wiki/index.md"]
    if {item["path"] for item in manifest["changes"]} != set(changed):
        raise EventLogContractError("wiki apply manifest targets")
    for change in manifest["changes"]:
        if change["operation"] != "write":
            raise EventLogContractError("eval scenario cannot delete wiki")
        preparation = staged.get(change["staging_path"])
        if preparation is None:
            # The web flow does not have a write_wiki_question event: capture its
            # staged bytes in the apply diff's before inventory.
            preparation = next((item for item in diff["before"] if item["path"] == change["staging_path"]), None)
        after = next((item for item in diff["after"] if item["path"] == change["path"]), None)
        if preparation is None or after is None or preparation["sha256"] != change["sha256"] or after["sha256"] != change["sha256"]:
            raise EventLogContractError("wiki apply staging/promoted digest")
    expected = []
    for proof in cursors.values():
        if proof["family"] == "link_candidates":
            first = proof["first"]
            expected.append({key: first[key] for key in ("run_id", "corpus_revision", "page_path", "terms", "candidate_manifest_sha256", "candidate_count")} | {"page_count": proof["page_count"]})
    if manifest["link_candidate_runs"] != sorted(expected, key=lambda item: item["page_path"]):
        raise EventLogContractError("applied manifest link-candidate proof")
    for approval in approvals.values():
        if approval["fixture_id"] is None:
            if approval["manifest_id"] != record["manifest_id"]:
                raise EventLogContractError("interpretation decision manifest binding")
            if approval["decision"] == "approved":
                if manifest["approval_event_id"] != approval["event_id"]:
                    raise EventLogContractError("interpretation approval not consumed")
            elif manifest["approval_event_id"] is not None or manifest["change_intent"] != "routine":
                raise EventLogContractError("withheld interpretation cannot authorize change")
    if log["scenario_id"] == "contradictory-evidence":
        _both_claims(c, diff)
    if log["scenario_id"] == "web-approval-and-capture":
        descriptor = c.obj(c.obj(log["_manifest"]["fixture_capability_id"], "fixture_capability")["descriptor_id"], "fixture_descriptor")
        if any(item["path"] in diff["changed_paths"] and item["sha256"] == descriptor["unused_sha256"] for item in diff["after"]):
            raise EventLogContractError("unused web candidate persisted")
        activation = log.get("_rendered_activation")
        if activation is None:
            raise EventLogContractError("rendered activation missing for web citation")
        state = _source_state(c, record["command_id"], result_id=None, revision=result["data"]["corpus_revision"])
        if activation["content_sha256"] != descriptor["rendered_sha256"]:
            raise EventLogContractError("rendered citation does not match authorized fixture")
        for item in diff["after"]:
            if item["path"] in changed:
                body = c.read(item["content_id"], "file_capture").decode()
                _semantic(semantic.validate_promoted_web_citations, body, item["path"], state, activation)


def _validate_event_log(log: dict[str, Any], schema: dict[str, Any], scenario: dict[str, Any], fixture_sha256: str, context: TrustedRunContext) -> None:
    """Accept only a completely corroborated pass from an external control root."""
    try:
        validate_against_schema(log, schema)
    except ScenarioContractError as error:
        raise EventLogContractError(str(error)) from error
    if any(name not in EVENT_RULES for name in scenario["required_events"]):
        raise EventLogContractError("scenario required event has no map rule")
    if (not _SHA.fullmatch(log["run_id"]) or log["scenario_id"] != scenario["id"]
            or log["fixture_sha256"] != fixture_sha256 or log["network_mode"] != scenario["network_mode"]
            or log["result"] != "pass" or log["incomplete_reasons"]):
        raise EventLogContractError("log identity")
    control = _Control(context, log)
    try:
        interpretation_id = _interpretation_artifact_id(control, scenario)
        manifest = control.fixed("run-manifest.json")
        _require_keys(manifest, {"schema_version", "run_id", "client", "scenario_id", "fixture_id", "fixture_sha256", "workspace", "phases", "policy_profiles", "fixture_capability_id", "approval", "software_paths", "scenario_sha256", "phase_prompt_protocol", "phase_prompts"}, "run manifest")
        if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["run_id"] != log["run_id"]
                or manifest["client"] != log["client"] or manifest["scenario_id"] != scenario["id"]
                or manifest["fixture_id"] != scenario["fixture_id"] or manifest["fixture_sha256"] != fixture_sha256 or manifest["workspace"] != control.workspace_path):
            raise EventLogContractError("run manifest identity")
        if type(manifest["software_paths"]) is not list or manifest["software_paths"] != sorted(set(manifest["software_paths"])):
            raise EventLogContractError("software path scope")
        for path in manifest["software_paths"]:
            _relative_path(path)
            if path.startswith(("wiki/", "sources/", ".brain/")):
                raise EventLogContractError("software path contains archive")
        if scenario["id"] != "repository-development-not-archived" and manifest["software_paths"]:
            raise EventLogContractError("unexpected software scope")
        if manifest["approval"] is not None:
            _require_keys(manifest["approval"], {"event_id", "scope", "note", "decision"}, "run approval")
            for value in manifest["approval"].values():
                _text(value)
        if (log["network_mode"] == "mock_only") != (type(manifest["fixture_capability_id"]) is str):
            raise EventLogContractError("run fixture capability")
        attestation = control.attestation()
        attestation_sha = attestation.pop("_sha256")
        unsigned = dict(log)
        unsigned.pop("log_attestation_sha256")
        if type(attestation.get("schema_version")) is not int or attestation_sha != log["log_attestation_sha256"] or attestation != {"schema_version": 1, "run_id": log["run_id"], "client": log["client"], "scenario_id": scenario["id"], "fixture_sha256": fixture_sha256, "evidence_index_sha256": log["evidence_index_sha256"], "log_sha256": _canon(unsigned)}:
            raise EventLogContractError("final attestation")
        required_phases = ["approval", "approved_capture"] if log["network_mode"] == "mock_only" else ["main"]
        if [execution["phase"] for execution in log["executions"]] != required_phases:
            raise EventLogContractError("execution phases")
        if manifest["phases"] != required_phases or type(manifest["policy_profiles"]) is not list:
            raise EventLogContractError("run manifest phases")
        executions = {execution["id"]: execution for execution in log["executions"]}
        if len(executions) != len(log["executions"]):
            raise EventLogContractError("duplicate execution")
        try:
            expected_scenario_sha256 = scenario_sha256(scenario)
        except ValueError as error:
            raise EventLogContractError("run manifest phase prompt scenario") from error
        if (manifest["scenario_sha256"] != expected_scenario_sha256
                or manifest["phase_prompt_protocol"] != PROMPT_PROTOCOL
                or type(manifest["phase_prompts"]) is not list
                or len(manifest["phase_prompts"]) != len(log["executions"])):
            raise EventLogContractError("run manifest phase prompt")
        phase_prompts: dict[str, dict[str, Any]] = {}
        seen_prompt_ids: set[str] = set()
        for expected_execution, prompt in zip(log["executions"], manifest["phase_prompts"]):
            if type(prompt) is not dict:
                raise EventLogContractError("run manifest phase prompt")
            _require_keys(prompt, {"execution_id", "phase", "phase_prompt_id", "phase_prompt_sha256", "phase_prompt_transport"}, "run manifest phase prompt")
            if (prompt["execution_id"] != expected_execution["id"]
                    or prompt["phase"] != expected_execution["phase"]
                    or type(prompt["phase_prompt_id"]) is not str
                    or prompt["phase_prompt_id"] in seen_prompt_ids
                    or prompt["execution_id"] in phase_prompts
                    or not _sha(prompt["phase_prompt_sha256"])):
                raise EventLogContractError("run manifest phase prompt")
            seen_prompt_ids.add(prompt["phase_prompt_id"])
            phase_prompts[prompt["execution_id"]] = prompt
        seen_executable_artifacts: set[str] = set()
        seen_probe_artifacts: set[str] = set()
        seen_phase_prompts: set[str] = set()
        for execution in log["executions"]:
            _validate_execution(
                control, execution, {**log, "_manifest": manifest}, context,
                scenario, phase_prompts[execution["id"]],
                seen_executable_artifacts=seen_executable_artifacts,
                seen_probe_artifacts=seen_probe_artifacts,
                seen_phase_prompts=seen_phase_prompts,
            )
        if seen_phase_prompts != {
            artifact_id for artifact_id, entry in control.entries.items()
            if entry["type"] == "phase_prompt"
        }:
            raise EventLogContractError("unreferenced phase prompt")
        _audit_workspace_snapshots(control, executions)
        if manifest["policy_profiles"] != [
            control.obj(execution["policy_id"], "policy")["profile"]
            for execution in log["executions"]
        ]:
            raise EventLogContractError("run manifest policy profiles")
        names: list[str] = []
        marker_ids: set[str] = set()
        for event in log["events"]:
            execution = executions.get(event["execution_id"])
            if execution is None:
                raise EventLogContractError("event execution")
            _validate_marker(control, event, execution, log["run_id"])
            if event["transcript_marker_id"] in marker_ids:
                raise EventLogContractError("transcript marker reused")
            marker_ids.add(event["transcript_marker_id"])
            _validate_event_record(control, event, log["run_id"])
            names.append(event["name"])
        if [event["sequence"] for event in log["events"]] != list(range(1, len(log["events"]) + 1)) or len(names) != len(set(names)):
            raise EventLogContractError("event sequence")
        _audit_native_event_markers(control, log, scenario, executions)
        _event_order(control, log, executions)
        cursor = 0
        try:
            for name in scenario["required_events"]:
                cursor = names.index(name, cursor) + 1
        except ValueError as error:
            raise EventLogContractError("required event missing") from error
        if set(names) & set(scenario["forbidden_events"]):
            raise EventLogContractError("forbidden event")
        operations = [receipt["operation"] for receipt in log["receipts"]]
        if operations != scenario["receipt_operations"] or len(set(operations)) != len(operations):
            raise EventLogContractError("required receipt operations")
        receipt_log = dict(log)
        receipt_log["_scenario_required_events"] = scenario["required_events"]
        verified_receipts = _validate_receipts(control, receipt_log, log["events"])
        special_log = dict(log)
        special_log["_manifest"] = manifest
        special_log["_scenario"] = scenario
        _validate_special_events(control, special_log, log["events"], executions, verified_receipts)
        _audit_commands(control, log, executions, verified_receipts)
        _validate_interpretation_decision(control, special_log, scenario, interpretation_id)
        if [assertion["text"] for assertion in log["repository_assertions"]] != scenario["repository_assertions"] or not all(assertion["passed"] for assertion in log["repository_assertions"]):
            raise EventLogContractError("scenario assertions")
        terminal_name = _ASSERTION_TERMINAL_EVENTS.get(scenario["id"])
        terminal = next((event for event in log["events"] if event["name"] == terminal_name), None)
        if terminal is None:
            raise EventLogContractError("assertion terminal event")
        terminal_snapshot_id, _ = _marker_workspace_snapshot(control, terminal)
        for index, assertion in enumerate(log["repository_assertions"]):
            proof = control.obj(assertion["assertion_id"], "assertion")
            diff = _diff(control, assertion["diff_id"], require_snapshot_bindings=True)
            proof_keys = {"run_id", "text", "passed", "diff_id", "marker_id", "snapshot_id"}
            if interpretation_id is not None and index == 2:
                proof_keys.add("interpretation_decision_id")
                if proof.get("interpretation_decision_id") != interpretation_id:
                    raise EventLogContractError("canonical interpretation assertion decision join")
            if (set(proof) != proof_keys
                    or proof["run_id"] != log["run_id"]
                    or proof["text"] != assertion["text"] or proof["passed"] is not True
                    or proof["diff_id"] != assertion["diff_id"]
                    or proof["marker_id"] != terminal["transcript_marker_id"]
                    or proof["snapshot_id"] != terminal_snapshot_id
                    or proof["snapshot_id"] != diff["after_snapshot_id"]
                    or type(diff.get("changed_paths")) is not list):
                raise EventLogContractError("assertion/diff proof")
        control._validate_edges()
    finally:
        control.close()


def validate_event_log(log, schema, scenario, fixture_sha256, context):
    """Validate indexed host evidence; malformed raw scalars always fail closed."""
    try:
        _validate_event_log(log, schema, scenario, fixture_sha256, context)
    except EventLogContractError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, IndexError, OSError) as error:
        raise EventLogContractError("malformed or unavailable evidence: " + str(error)) from error
