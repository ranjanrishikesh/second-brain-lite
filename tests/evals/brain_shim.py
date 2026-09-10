#!/usr/bin/env python3
"""Test-only controlled replacement for a copied fixture workspace's ``brain``.

The real launcher is preserved by the future runner outside the workspace.
This shim only injects deterministic fixture services into the normal library
CLI and writes its evidence captures to a runner-owned control directory.
It is intentionally not a product command and is never installed in a user
brain.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, Sequence, TextIO

from brainlib.cli import main as product_main
from brainlib.commands import CommandServices
from brainlib.diagnostics import Diagnostic
from brainlib.extractors import handoff as agent_handoff
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore
from brainlib.output import CommandResult, render_json
from brainlib.sync_results import (
    HandoffDeliveryReference,
    SyncResultReference,
    SyncResultStore,
)

from .fixture_services import (
    FixtureServiceError,
    StaticFixtureTransport,
    controlled_services,
)


CONTROL_ENV = "SECOND_BRAIN_EVAL_CONTROL"
_CONFIG_NAME = "brain-shim.json"
_COMMAND_LOG_NAME = "brain-shim-command-log.jsonl"
_CAPTURE_INDEX_NAME = "brain-shim-capture-index.jsonl"
_TRACE_NAME = "brain-shim-trace.jsonl"
_LOCK_NAME = ".brain-shim.lock"
_CAPTURE_DIRECTORY = "brain-shim-captures"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HANDOFF = re.compile(r"hnd_[0-9a-f]{64}\Z")
_WIKI_STAGE = re.compile(r"wstg_[0-9a-f]{32}\Z")
_QUESTION_STAGE_LEAF = re.compile(r"[a-z0-9-]+\.md\Z")
_KNOWN_SCENARIOS = frozenset(
    {
        "empty-wiki-first-question",
        "current-wiki-fast-path",
        "new-binary-before-question",
        "web-approval-and-capture",
        "contradictory-evidence",
        "repository-development-not-archived",
    }
)
_ALLOWED_PHASES = frozenset({"main", "approval", "approved_capture"})


class ShimError(ValueError):
    def __init__(self, message: str, *, code: str = "eval_shim_denied") -> None:
        self.code = code
        super().__init__(message)


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ShimError(f"invalid {label}", code="eval_control_invalid") from error
    if type(value) is not dict:
        raise ShimError(f"invalid {label}", code="eval_control_invalid")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ShimError(f"invalid {label}", code="eval_control_invalid")
    return value


def _relative_control_name(value: object, label: str) -> str:
    text = _nonempty_text(value, label)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or path.as_posix() != text
        or len(path.parts) != 1
        or path.parts[0] in {".", ".."}
    ):
        raise ShimError(f"invalid {label}", code="eval_control_invalid")
    return text


def _regular_control_file(root: Path, name: str, label: str) -> Path:
    path = root / name
    try:
        info = path.lstat()
    except OSError as error:
        raise ShimError(f"missing {label}", code="eval_control_invalid") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ShimError(f"unsafe {label}", code="eval_control_invalid")
    return path


def _read_control_file(root: Path, name: str, label: str) -> bytes:
    path = _regular_control_file(root, name, label)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ShimError(f"unsafe {label}", code="eval_control_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise ShimError(f"{label} exceeds control read bound", code="eval_control_invalid")
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ShimError(f"cannot read {label}", code="eval_control_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
    ):
        raise ShimError(f"{label} changed while reading", code="eval_control_invalid")
    return raw


def _parse_timestamp(value: object) -> datetime:
    text = _nonempty_text(value, "fixture retrieved_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ShimError("invalid fixture retrieved_at", code="eval_control_invalid") from error
    if parsed.utcoffset() is None:
        raise ShimError("fixture retrieved_at must be timezone-aware", code="eval_control_invalid")
    return parsed


@dataclass(frozen=True)
class FixtureDescriptor:
    fixture_sha256: str
    static_sha256: str
    rendered_sha256: str
    final_url: str
    media_type: str
    retrieved_at: datetime


@dataclass(frozen=True)
class ControlConfig:
    root: Path
    workspace: Path
    scenario_id: str
    phase: str
    approval: tuple[str, str, str] | None
    fixture_names: tuple[str, str, str] | None


_RawFileIdentity = tuple[int, int, int, int, int, int, int, int]


def _raw_file_identity(info: os.stat_result) -> _RawFileIdentity:
    """Return the complete regular-file identity retained by receipt sinks."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _valid_raw_file_identity(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 8
        and all(type(item) is int for item in value)
    )


def _valid_raw_receipt_path(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


@dataclass(frozen=True)
class _RawWriteReceipt:
    """Private, metadata-only receipt for one complete raw-file write.

    The raw bytes remain only in the runner-owned capture file.  The hook
    exposes enough descriptor-time facts for a pinned parent to reject a
    same-byte pathname replacement without copying potentially sensitive
    capture data into another payload.
    """

    relative_path: str
    sha256: str
    byte_count: int
    identity: _RawFileIdentity

    def __post_init__(self) -> None:
        if (
            not _valid_raw_receipt_path(self.relative_path)
            or type(self.sha256) is not str
            or _SHA.fullmatch(self.sha256) is None
            or type(self.byte_count) is not int
            or self.byte_count < 0
            or not _valid_raw_file_identity(self.identity)
            or self.identity[4] != 1
            or self.identity[5] != self.byte_count
        ):
            raise ShimError("invalid raw write receipt", code="eval_capture_invalid")


@dataclass(frozen=True)
class _RawAppendReceipt:
    """Private digest chain for one append-only control journal row.

    ``prior_*`` and ``completed_*`` describe the whole file immediately
    before and after the append while its descriptor is still open.  The
    implementation independently streams the prefix and tail to establish
    the exact prefix relationship before this metadata-only receipt reaches a
    sink.
    """

    relative_path: str
    prior_sha256: str
    prior_bytes: int
    prior_identity: _RawFileIdentity
    appended_sha256: str
    appended_bytes: int
    completed_sha256: str
    completed_bytes: int
    completed_identity: _RawFileIdentity
    prior_rows: int
    completed_rows: int

    def __post_init__(self) -> None:
        digest_values = (
            self.prior_sha256,
            self.appended_sha256,
            self.completed_sha256,
        )
        count_values = (
            self.prior_bytes,
            self.appended_bytes,
            self.completed_bytes,
            self.prior_rows,
            self.completed_rows,
        )
        if (
            not _valid_raw_receipt_path(self.relative_path)
            or any(type(value) is not str or _SHA.fullmatch(value) is None for value in digest_values)
            or any(type(value) is not int or value < 0 for value in count_values)
            or not _valid_raw_file_identity(self.prior_identity)
            or not _valid_raw_file_identity(self.completed_identity)
            or self.prior_identity[4] != 1
            or self.completed_identity[4] != 1
            or self.prior_identity[5] != self.prior_bytes
            or self.completed_identity[5] != self.completed_bytes
            or self.prior_identity[:5] != self.completed_identity[:5]
            or self.appended_bytes == 0
            or self.completed_bytes != self.prior_bytes + self.appended_bytes
            or self.completed_rows != self.prior_rows + 1
        ):
            raise ShimError("invalid raw append receipt", code="eval_capture_invalid")


_RawWriteReceiptEvent = _RawWriteReceipt | _RawAppendReceipt
_RawWriteReceiptSink = Callable[[_RawWriteReceiptEvent], None]


@dataclass(frozen=True)
class _RawAppendPreflight:
    """Private expected predecessor for one armed raw journal append.

    This deliberately contains only bounded metadata.  A runner which knew a
    journal was absent before launching a child can require the child's first
    append to create it exclusively; a runner which knew it existed can bind
    the descriptor-time predecessor to that retained snapshot.  No caller
    without this opt-in hook observes a different open path.
    """

    create_exclusive: bool
    prior_sha256: str
    prior_bytes: int
    prior_identity: _RawFileIdentity | None
    prior_rows: int

    def __post_init__(self) -> None:
        if (
            type(self.create_exclusive) is not bool
            or type(self.prior_sha256) is not str
            or _SHA.fullmatch(self.prior_sha256) is None
            or type(self.prior_bytes) is not int
            or self.prior_bytes < 0
            or type(self.prior_rows) is not int
            or self.prior_rows < 0
        ):
            raise ShimError("invalid raw append preflight", code="eval_capture_invalid")
        if self.create_exclusive:
            if (
                self.prior_sha256 != hashlib.sha256(b"").hexdigest()
                or self.prior_bytes != 0
                or self.prior_identity is not None
                or self.prior_rows != 0
            ):
                raise ShimError("invalid raw append preflight", code="eval_capture_invalid")
            return
        if (
            not _valid_raw_file_identity(self.prior_identity)
            or self.prior_identity[4] != 1
            or self.prior_identity[5] != self.prior_bytes
        ):
            raise ShimError("invalid raw append preflight", code="eval_capture_invalid")

    def verify_predecessor(
        self,
        *,
        sha256: str,
        byte_count: int,
        identity: _RawFileIdentity,
        rows: int,
    ) -> None:
        """Fail closed unless the live descriptor has the retained prefix."""

        if self.create_exclusive:
            valid = (
                sha256 == hashlib.sha256(b"").hexdigest()
                and byte_count == 0
                and rows == 0
            )
        else:
            valid = (
                sha256 == self.prior_sha256
                and byte_count == self.prior_bytes
                and identity == self.prior_identity
                and rows == self.prior_rows
            )
        if not valid:
            raise ShimError("raw append predecessor changed", code="eval_capture_invalid")


_RawAppendPreflightHook = Callable[[str], _RawAppendPreflight]


class HostRecorder(Protocol):
    """The runner-injectable command-bound capture interface."""

    def start(self) -> str: ...

    def finish(
        self,
        *,
        argv: list[str],
        code: int,
        raw_result: bytes,
        result: dict[str, object],
        command: tuple[str, ...],
    ) -> None: ...


RecorderFactory = Callable[[ControlConfig], HostRecorder]


def _fixture_descriptor(root: Path, fixture: dict[str, object]) -> tuple[FixtureDescriptor, bytes, bytes]:
    if set(fixture) != {"descriptor_path", "static_shell_path", "rendered_dom_path"}:
        raise ShimError("invalid fixture control fields", code="eval_control_invalid")
    descriptor_name = _relative_control_name(fixture["descriptor_path"], "descriptor path")
    static_name = _relative_control_name(fixture["static_shell_path"], "static shell path")
    rendered_name = _relative_control_name(fixture["rendered_dom_path"], "rendered DOM path")
    document = _json_object(_read_control_file(root, descriptor_name, "fixture descriptor"), "fixture descriptor")
    expected = {
        "fixture_id",
        "fixture_sha256",
        "static_sha256",
        "rendered_sha256",
        "unused_sha256",
        "final_url",
        "media_type",
        "retrieved_at",
        "redirect_urls",
    }
    if set(document) != expected or document["fixture_id"] != "web-approval-and-capture":
        raise ShimError("invalid fixture descriptor", code="eval_control_invalid")
    for key in ("fixture_sha256", "static_sha256", "rendered_sha256", "unused_sha256"):
        if type(document[key]) is not str or _SHA.fullmatch(document[key]) is None:
            raise ShimError("invalid fixture descriptor digest", code="eval_control_invalid")
    if (
        document["final_url"] != "https://example.test/standard"
        or document["media_type"] != "text/html"
        or document["redirect_urls"] != []
    ):
        raise ShimError("fixture descriptor is not the single approved fixture", code="eval_control_invalid")
    static_shell = _read_control_file(root, static_name, "static fixture shell")
    rendered_dom = _read_control_file(root, rendered_name, "rendered fixture DOM")
    if (
        hashlib.sha256(static_shell).hexdigest() != document["static_sha256"]
        or hashlib.sha256(rendered_dom).hexdigest() != document["rendered_sha256"]
    ):
        raise ShimError("fixture asset digest mismatch", code="eval_control_invalid")
    if b"<script" not in static_shell.lower() or b"<html" not in rendered_dom.lower():
        raise ShimError("fixture capture bytes are not the approved shell/DOM", code="eval_control_invalid")
    return (
        FixtureDescriptor(
            fixture_sha256=document["fixture_sha256"],
            static_sha256=document["static_sha256"],
            rendered_sha256=document["rendered_sha256"],
            final_url=document["final_url"],
            media_type=document["media_type"],
            retrieved_at=_parse_timestamp(document["retrieved_at"]),
        ),
        static_shell,
        rendered_dom,
    )


def _fixture_names(fixture: dict[str, object]) -> tuple[str, str, str]:
    if set(fixture) != {"descriptor_path", "static_shell_path", "rendered_dom_path"}:
        raise ShimError("invalid fixture control fields", code="eval_control_invalid")
    return (
        _relative_control_name(fixture["descriptor_path"], "descriptor path"),
        _relative_control_name(fixture["static_shell_path"], "static shell path"),
        _relative_control_name(fixture["rendered_dom_path"], "rendered DOM path"),
    )


def _load_fixture_assets(config: ControlConfig) -> tuple[FixtureDescriptor, bytes, bytes]:
    if config.fixture_names is None:
        raise ShimError("web fixture assets are unavailable", code="eval_control_invalid")
    descriptor_name, static_name, rendered_name = config.fixture_names
    return _fixture_descriptor(
        config.root,
        {
            "descriptor_path": descriptor_name,
            "static_shell_path": static_name,
            "rendered_dom_path": rendered_name,
        },
    )


def load_control(cwd: Path, environ: Mapping[str, str]) -> ControlConfig:
    raw_root = environ.get(CONTROL_ENV)
    if raw_root is None:
        raise ShimError(f"{CONTROL_ENV} is required", code="eval_control_missing")
    try:
        root = Path(raw_root).resolve(strict=True)
        info = root.lstat()
    except OSError as error:
        raise ShimError("control root is unavailable", code="eval_control_invalid") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ShimError("control root is unsafe", code="eval_control_invalid")
    workspace = cwd.resolve()
    if root.is_relative_to(workspace) or workspace.is_relative_to(root):
        raise ShimError("control root must be outside the fixture workspace", code="eval_control_invalid")
    document = _json_object(_read_control_file(root, _CONFIG_NAME, "shim config"), "shim config")
    expected = {"schema_version", "workspace", "scenario_id", "phase", "approval", "fixture"}
    if set(document) != expected or document["schema_version"] != 1:
        raise ShimError("invalid shim config fields", code="eval_control_invalid")
    configured_workspace = _nonempty_text(document["workspace"], "workspace")
    if not Path(configured_workspace).is_absolute() or Path(configured_workspace).resolve() != workspace:
        raise ShimError("shim config workspace does not match cwd", code="eval_control_invalid")
    scenario_id = _nonempty_text(document["scenario_id"], "scenario")
    phase = _nonempty_text(document["phase"], "phase")
    if scenario_id not in _KNOWN_SCENARIOS or phase not in _ALLOWED_PHASES:
        raise ShimError("unrecognized fixture scenario or phase", code="eval_control_invalid")
    approval = document["approval"]
    fixture = document["fixture"]
    if scenario_id == "web-approval-and-capture":
        if type(approval) is not dict or set(approval) != {"event_id", "scope", "note"}:
            raise ShimError("web fixture approval is invalid", code="eval_control_invalid")
        approved = tuple(_nonempty_text(approval[key], f"approval {key}") for key in ("event_id", "scope", "note"))
        if type(fixture) is not dict:
            raise ShimError("web fixture assets are invalid", code="eval_control_invalid")
        return ControlConfig(root, workspace, scenario_id, phase, approved, _fixture_names(fixture))
    if approval is not None or fixture is not None:
        raise ShimError("non-web fixtures may not configure web capability", code="eval_control_invalid")
    return ControlConfig(root, workspace, scenario_id, phase, None, None)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_json_line(value: object) -> bytes:
    return _canonical_json(value) + b"\n"


def _read_workspace_regular_file(
    workspace: Path,
    logical: PurePosixPath,
    *,
    label: str,
    max_bytes: int = 64 * 1024 * 1024,
) -> bytes:
    """Read one retained workspace file through pinned, no-follow directories."""

    if (
        logical.is_absolute()
        or not logical.parts
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise ShimError(f"invalid {label} path", code="eval_capture_invalid")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    root_fd = -1
    current_fd = -1
    leaf_fd = -1
    try:
        root_fd = os.open(workspace, directory_flags)
        current_fd = root_fd
        for part in logical.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise ShimError(f"unsafe {label} directory", code="eval_capture_invalid")
            except BaseException:
                os.close(next_fd)
                raise
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        leaf_fd = os.open(
            logical.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        before = os.fstat(leaf_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ShimError(f"unsafe {label}", code="eval_capture_invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(leaf_fd, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ShimError(f"{label} exceeds capture read bound", code="eval_capture_invalid")
            chunks.append(chunk)
        after = os.fstat(leaf_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ):
            raise ShimError(f"{label} changed while reading", code="eval_capture_invalid")
        return b"".join(chunks)
    except OSError as error:
        raise ShimError(f"cannot read {label}", code="eval_capture_invalid") from error
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if current_fd >= 0 and current_fd != root_fd:
            os.close(current_fd)
        if root_fd >= 0:
            os.close(root_fd)


@contextmanager
def _locked_control(root: Path):
    """Serialize one shim command and its post-command capture as one record."""

    import fcntl

    lock = root / _LOCK_NAME
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class Recorder:
    """Host-side raw capture producer; Task 7c indexes these immutable bytes."""

    def __init__(
        self,
        config: ControlConfig,
        *,
        now: Callable[[], datetime] | None = None,
        raw_write_receipt_sink: _RawWriteReceiptSink | None = None,
        raw_append_preflight: _RawAppendPreflightHook | None = None,
        raw_write_root_fd: int | None = None,
    ) -> None:
        if raw_write_receipt_sink is not None and not callable(raw_write_receipt_sink):
            raise TypeError("raw write receipt sink must be callable")
        if raw_append_preflight is not None and not callable(raw_append_preflight):
            raise TypeError("raw append preflight must be callable")
        if raw_write_root_fd is not None:
            if (type(raw_write_root_fd) is not int or raw_write_root_fd < 0
                    or raw_write_receipt_sink is None or raw_append_preflight is None):
                raise TypeError("raw write root descriptor requires paired private hooks")
            try:
                if not stat.S_ISDIR(os.fstat(raw_write_root_fd).st_mode):
                    raise TypeError("raw write root descriptor must be a directory")
            except OSError as error:
                raise TypeError("raw write root descriptor is unavailable") from error
        self.config = config
        self._now = now if now is not None else lambda: datetime.now(timezone.utc)
        # This is deliberately an internal, default-off parent hook.  It is
        # never represented in the shim's command log, capture index, result,
        # or any other evidence payload.
        self._raw_write_receipt_sink = raw_write_receipt_sink
        # This companion hook is also private and default-off.  It is only
        # meaningful to a parent that has pinned a pre-launch raw-journal
        # snapshot; without it, `_append` retains its historical O_CREAT path.
        self._raw_append_preflight = raw_append_preflight
        # The paired phase-two route also supplies the child pin's retained
        # control-root descriptor.  One-shot captures then walk and create
        # every parent through this fd, so a dynamic capture-directory symlink
        # cannot redirect raw bytes outside the private control root.
        self._raw_write_root_fd = raw_write_root_fd
        self.command_id: str | None = None
        self.start_sequence: int | None = None

    def _receipt_relative_path(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.config.root).as_posix()
        except ValueError as error:
            raise ShimError("raw receipt path escaped control root", code="eval_capture_invalid") from error
        if not _valid_raw_receipt_path(relative):
            raise ShimError("raw receipt path is unsafe", code="eval_capture_invalid")
        return relative

    @staticmethod
    def _write_all(descriptor: int, body: bytes) -> None:
        offset = 0
        while offset < len(body):
            written = os.write(descriptor, body[offset:])
            if written <= 0:
                raise ShimError("raw producer write did not advance", code="eval_capture_invalid")
            offset += written

    @staticmethod
    def _receipt_identity_from_named(
        descriptor: int,
        named: os.stat_result,
    ) -> _RawFileIdentity:
        """Check the open descriptor and already-selected name are one file."""

        opened = os.fstat(descriptor)
        opened_identity = _raw_file_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or opened_identity != _raw_file_identity(named)
        ):
            raise ShimError("raw producer path changed while writing", code="eval_capture_invalid")
        return opened_identity

    @staticmethod
    def _receipt_identity(path: Path, descriptor: int) -> _RawFileIdentity:
        """Check the open descriptor and selected pathname are one file."""

        return Recorder._receipt_identity_from_named(
            descriptor,
            path.stat(follow_symlinks=False),
        )

    def _rooted_raw_write_parent(
        self,
        relative_path: str,
        *,
        create: bool,
    ) -> tuple[int, str]:
        """Open a one-shot capture parent through the paired pinned root.

        The normal recorder deliberately uses path operations.  Only the
        private phase-two pairing supplies a retained root descriptor; it
        creates missing capture parents and walks every existing parent with
        ``O_NOFOLLOW`` so a post-launch symlink cannot redirect raw bytes.
        The returned descriptor is owned by the caller.
        """

        root_fd = self._raw_write_root_fd
        if root_fd is None or not _valid_raw_receipt_path(relative_path):
            raise ShimError("raw producer capture parent is invalid", code="eval_capture_invalid")
        parts = PurePosixPath(relative_path).parts
        if not parts:
            raise ShimError("raw producer capture parent is invalid", code="eval_capture_invalid")
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory = -1
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise ShimError("raw producer capture parent is invalid", code="eval_capture_invalid")
            directory = os.dup(root_fd)
            for part in parts[:-1]:
                try:
                    before = os.stat(part, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    if not create:
                        raise ShimError(
                            "raw producer capture parent disappeared",
                            code="eval_capture_invalid",
                        )
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory)
                    except FileExistsError:
                        # A concurrent creator is still safe only if the
                        # subsequent no-follow walk proves this exact name is
                        # a directory under the retained root.
                        pass
                    before = os.stat(part, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                    raise ShimError("raw producer capture parent is unsafe", code="eval_capture_invalid")
                child = os.open(part, directory_flags, dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    named = os.stat(part, dir_fd=directory, follow_symlinks=False)
                    expected = (before.st_dev, before.st_ino, before.st_mode, before.st_uid)
                    if (
                        (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid) != expected
                        or (named.st_dev, named.st_ino, named.st_mode, named.st_uid) != expected
                    ):
                        raise ShimError(
                            "raw producer capture parent changed while opened",
                            code="eval_capture_invalid",
                        )
                except BaseException:
                    os.close(child)
                    raise
                os.close(directory)
                directory = child
            return directory, parts[-1]
        except ShimError:
            if directory >= 0:
                os.close(directory)
            raise
        except OSError as error:
            if directory >= 0:
                os.close(directory)
            raise ShimError("raw producer capture parent is unavailable", code="eval_capture_invalid") from error

    def _rooted_receipt_identity(
        self,
        relative_path: str,
        descriptor: int,
    ) -> _RawFileIdentity:
        """Rewalk the retained root before issuing an armed write receipt."""

        parent_fd = -1
        try:
            parent_fd, leaf = self._rooted_raw_write_parent(relative_path, create=False)
            return self._receipt_identity_from_named(
                descriptor,
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False),
            )
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    @staticmethod
    def _descriptor_digest(
        descriptor: int,
        *,
        start: int,
        byte_count: int,
    ) -> tuple[str, int, bool]:
        """Hash an exact descriptor range without retaining its raw bytes."""

        if start < 0 or byte_count < 0:
            raise ShimError("invalid raw receipt range", code="eval_capture_invalid")
        digest = hashlib.sha256()
        offset = start
        remaining = byte_count
        rows = 0
        last_is_newline = byte_count == 0
        while remaining:
            chunk = os.pread(descriptor, min(65_536, remaining), offset)
            if not chunk:
                raise ShimError("raw producer file shortened while reading", code="eval_capture_invalid")
            digest.update(chunk)
            rows += chunk.count(b"\n")
            last_is_newline = chunk.endswith(b"\n")
            offset += len(chunk)
            remaining -= len(chunk)
        return digest.hexdigest(), rows, last_is_newline

    def _emit_raw_write_receipt(self, receipt: _RawWriteReceiptEvent) -> None:
        sink = self._raw_write_receipt_sink
        if sink is None:
            return
        try:
            # This call intentionally occurs before the producer descriptor is
            # closed.  A sink may persist only the bounded private metadata;
            # it is not given a writable descriptor or raw capture bytes.
            sink(receipt)
        except Exception as error:
            raise ShimError("raw write receipt sink failed", code="eval_capture_invalid") from error

    def _write_bytes(
        self,
        path: Path,
        body: bytes,
        *,
        exclusive: bool = False,
        mode: int = 0o666,
    ) -> None:
        """Write one raw control file, optionally retaining an FD-time receipt.

        ``RunnerHostRecorder`` may use this private primitive for its own
        result and preapply producers.  The no-sink branch deliberately
        retains the historical ``Path.write_bytes`` / exclusive-capture
        behavior.
        """

        if self._raw_write_receipt_sink is None:
            if not exclusive:
                path.write_bytes(body)
                return
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
            try:
                offset = 0
                while offset < len(body):
                    offset += os.write(descriptor, body[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        if type(body) is not bytes:
            raise ShimError("raw producer body is invalid", code="eval_capture_invalid")
        self._write_bytes_with_receipt(path, body, exclusive=exclusive, mode=mode)

    def _write_bytes_with_receipt(
        self,
        path: Path,
        body: bytes,
        *,
        exclusive: bool,
        mode: int,
    ) -> None:
        relative_path = self._receipt_relative_path(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        # A fully armed phase-two recorder proves the identity of the
        # descriptor it wrote, but must not adopt a one-shot pathname an actor
        # created after the parent established its phase-two boundary.  All
        # current phase-two callers produce fresh command-scoped captures, so
        # exclusive create is both the ownership proof and the safest failure
        # mode.  A standalone receipt sink remains compatible with its
        # existing overwrite behavior; only the paired predecessor preflight
        # marks this stricter private phase-two route.
        flags |= os.O_EXCL if exclusive or self._raw_append_preflight is not None else os.O_TRUNC
        descriptor = -1
        parent_fd = -1
        expected_sha256 = hashlib.sha256(body).hexdigest()
        try:
            if self._raw_write_root_fd is None:
                descriptor = os.open(path, flags, mode)
            else:
                parent_fd, leaf = self._rooted_raw_write_parent(relative_path, create=True)
                descriptor = os.open(leaf, flags, mode, dir_fd=parent_fd)
            self._write_all(descriptor, body)
            os.fsync(descriptor)
            identity = (
                self._receipt_identity(path, descriptor)
                if self._raw_write_root_fd is None
                else self._rooted_receipt_identity(relative_path, descriptor)
            )
            if identity[5] != len(body):
                raise ShimError("raw producer byte count changed", code="eval_capture_invalid")
            observed_sha256, _rows, _terminated = self._descriptor_digest(
                descriptor, start=0, byte_count=len(body)
            )
            if observed_sha256 != expected_sha256:
                raise ShimError("raw producer bytes changed while writing", code="eval_capture_invalid")
            current_identity = (
                self._receipt_identity(path, descriptor)
                if self._raw_write_root_fd is None
                else self._rooted_receipt_identity(relative_path, descriptor)
            )
            if current_identity != identity:
                raise ShimError("raw producer path changed while writing", code="eval_capture_invalid")
            self._emit_raw_write_receipt(
                _RawWriteReceipt(relative_path, observed_sha256, len(body), identity)
            )
        except ShimError:
            raise
        except OSError as error:
            raise ShimError("raw producer write is unavailable", code="eval_capture_invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if parent_fd >= 0:
                os.close(parent_fd)

    def _append_with_receipt(self, path: Path, body: bytes) -> None:
        """Append one canonical journal row and retain its full digest chain."""

        if body.count(b"\n") != 1 or not body.endswith(b"\n"):
            raise ShimError("raw append row is invalid", code="eval_capture_invalid")
        relative_path = self._receipt_relative_path(path)
        appended_sha256 = hashlib.sha256(body).hexdigest()
        preflight: _RawAppendPreflight | None = None
        if self._raw_append_preflight is not None:
            try:
                preflight = self._raw_append_preflight(relative_path)
            except ShimError:
                raise
            except Exception as error:
                raise ShimError("raw append preflight failed", code="eval_capture_invalid") from error
            if type(preflight) is not _RawAppendPreflight:
                raise ShimError("raw append preflight is invalid", code="eval_capture_invalid")
        descriptor = -1
        try:
            flags = os.O_APPEND | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            if preflight is None:
                flags |= os.O_CREAT
            elif preflight.create_exclusive:
                # Parent-observed absence is a security fact, not merely an
                # empty prefix.  O_EXCL prevents a same-byte empty journal
                # planted between its pre-launch snapshot and this append.
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(
                path,
                flags,
                0o600,
            )
            prior_identity = self._receipt_identity(path, descriptor)
            prior_bytes = prior_identity[5]
            prior_sha256, prior_rows, prior_terminated = self._descriptor_digest(
                descriptor, start=0, byte_count=prior_bytes
            )
            if not prior_terminated or self._receipt_identity(path, descriptor) != prior_identity:
                raise ShimError("raw append prefix changed while reading", code="eval_capture_invalid")
            if preflight is not None:
                preflight.verify_predecessor(
                    sha256=prior_sha256,
                    byte_count=prior_bytes,
                    identity=prior_identity,
                    rows=prior_rows,
                )
            self._write_all(descriptor, body)
            os.fsync(descriptor)
            completed_identity = self._receipt_identity(path, descriptor)
            completed_bytes = completed_identity[5]
            if (
                completed_identity[:5] != prior_identity[:5]
                or completed_bytes != prior_bytes + len(body)
            ):
                raise ShimError("raw append target changed while writing", code="eval_capture_invalid")
            prefix_sha256, prefix_rows, prefix_terminated = self._descriptor_digest(
                descriptor, start=0, byte_count=prior_bytes
            )
            tail_sha256, tail_rows, tail_terminated = self._descriptor_digest(
                descriptor, start=prior_bytes, byte_count=len(body)
            )
            completed_sha256, completed_rows, completed_terminated = self._descriptor_digest(
                descriptor, start=0, byte_count=completed_bytes
            )
            if (
                prefix_sha256 != prior_sha256
                or prefix_rows != prior_rows
                or prefix_terminated != prior_terminated
                or tail_sha256 != appended_sha256
                or tail_rows != 1
                or not tail_terminated
                or completed_rows != prior_rows + 1
                or not completed_terminated
                or self._receipt_identity(path, descriptor) != completed_identity
            ):
                raise ShimError("raw append chain changed while writing", code="eval_capture_invalid")
            self._emit_raw_write_receipt(
                _RawAppendReceipt(
                    relative_path,
                    prior_sha256,
                    prior_bytes,
                    prior_identity,
                    appended_sha256,
                    len(body),
                    completed_sha256,
                    completed_bytes,
                    completed_identity,
                    prior_rows,
                    completed_rows,
                )
            )
        except ShimError:
            raise
        except OSError as error:
            raise ShimError("raw append is unavailable", code="eval_capture_invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _trace_rows(self) -> list[dict[str, object]]:
        path = self.config.root / _TRACE_NAME
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise ShimError("unsafe shim trace", code="eval_control_invalid")
        rows: list[dict[str, object]] = []
        for line in path.read_bytes().splitlines():
            row = _json_object(line, "shim trace")
            if type(row.get("sequence")) is not int:
                raise ShimError("invalid shim trace sequence", code="eval_control_invalid")
            rows.append(row)
        if [row["sequence"] for row in rows] != list(range(1, len(rows) + 1)):
            raise ShimError("noncontiguous shim trace", code="eval_control_invalid")
        return rows

    def _previous_timestamp(self) -> datetime | None:
        path = self.config.root / _CAPTURE_INDEX_NAME
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ShimError("unsafe shim capture index", code="eval_control_invalid")
        previous: datetime | None = None
        for line in _read_control_file(self.config.root, _CAPTURE_INDEX_NAME, "shim capture index").splitlines():
            row = _json_object(line, "shim capture index")
            value = row.get("timestamp")
            if type(value) is not str:
                raise ShimError("invalid shim capture timestamp", code="eval_control_invalid")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ShimError("invalid shim capture timestamp", code="eval_control_invalid") from error
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ShimError("invalid shim capture timestamp", code="eval_control_invalid")
            if previous is not None and parsed < previous:
                raise ShimError("decreasing shim capture timestamp", code="eval_control_invalid")
            previous = parsed
        return previous

    def _timestamp(self) -> str:
        observed = self._now()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ShimError("host recorder timestamp must be timezone-aware", code="eval_control_invalid")
        previous = self._previous_timestamp()
        if previous is not None and observed < previous:
            observed = previous
        return observed.isoformat()

    def _append(self, name: str, value: object) -> None:
        path = self.config.root / name
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ShimError("unsafe control log", code="eval_control_invalid")
        if self._raw_write_receipt_sink is not None:
            self._append_with_receipt(path, _canonical_json_line(value))
            return
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            data = _canonical_json_line(value)
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def start(self) -> str:
        rows = self._trace_rows()
        sequence = len(rows) + 1
        command_id = f"shim-command-{sequence:08d}"
        self.command_id = command_id
        self.start_sequence = sequence
        self._append(_TRACE_NAME, {"sequence": sequence, "kind": "command_start", "command_id": command_id})
        return command_id

    def _capture_bytes(self, directory: Path, logical_path: str, body: bytes) -> dict[str, object]:
        digest = hashlib.sha256(body).hexdigest()
        target = directory / (hashlib.sha256(logical_path.encode("utf-8")).hexdigest() + ".bin")
        self._write_bytes(target, body, exclusive=True, mode=0o600)
        return {"path": logical_path, "capture_path": target.relative_to(self.config.root).as_posix(), "sha256": digest, "bytes": len(body)}

    def _capture_state(self, result: dict[str, object], command: tuple[str, ...]) -> str | None:
        if not _requires_source_state(command):
            return None
        assert self.command_id is not None
        product = _product_result(result)
        data = product.get("data") if type(product) is dict else None
        if type(data) is not dict:
            return None
        result_id, revision = _result_identity(data)
        paths = RepoPaths.discover(self.config.workspace)
        capture_root = self.config.root / _CAPTURE_DIRECTORY
        directory = capture_root / self.command_id
        if self._raw_write_root_fd is None:
            capture_root.mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700)
        records: list[dict[str, object]] = []
        files: list[dict[str, object]] = []
        for record in sorted(paths.ledger_dir.glob("src_*.json"), key=lambda item: item.name):
            if record.is_symlink() or not record.is_file():
                raise ShimError("unsafe source ledger record", code="eval_capture_invalid")
            logical = PurePosixPath(record.relative_to(paths.root).as_posix())
            records.append(
                self._capture_bytes(
                    directory,
                    logical.as_posix(),
                    _read_workspace_regular_file(
                        paths.root, logical, label="source ledger record"
                    ),
                )
            )
        for root in (paths.raw, paths.extracted):
            if root.is_symlink() or not root.is_dir():
                raise ShimError("unsafe retained source root", code="eval_capture_invalid")
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
                if path.is_symlink():
                    raise ShimError("unsafe retained source file", code="eval_capture_invalid")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ShimError("unsafe retained source file", code="eval_capture_invalid")
                logical = PurePosixPath(path.relative_to(paths.root).as_posix())
                files.append(
                    self._capture_bytes(
                        directory,
                        logical.as_posix(),
                        _read_workspace_regular_file(
                            paths.root, logical, label="retained source file"
                        ),
                    )
                )
        registry = paths.registry
        if registry.is_symlink() or not registry.is_file():
            raise ShimError("unsafe extractor registry", code="eval_capture_invalid")
        registry_logical = PurePosixPath("config", "extractors.toml")
        files.append(
            self._capture_bytes(
                directory,
                registry_logical.as_posix(),
                _read_workspace_regular_file(
                    paths.root, registry_logical, label="extractor registry"
                ),
            )
        )
        files.sort(key=lambda entry: str(entry["path"]))
        pending = paths.root / ".brain" / "sync-results" / "pending.json"
        pending_entry: dict[str, object] | None = None
        if pending.exists():
            if pending.is_symlink() or not pending.is_file():
                raise ShimError("unsafe pending sync checkpoint", code="eval_capture_invalid")
            pending_logical = PurePosixPath(".brain", "sync-results", "pending.json")
            pending_bytes = _read_workspace_regular_file(
                paths.root, pending_logical, label="pending sync checkpoint"
            )
            # Snapshot continuation uses the same filename but a different,
            # non-SyncResult checkpoint shape.  Task 7a deliberately grants
            # it no pending-sync authority; only init/sync receipts may be
            # retained as a pending_sync_result artifact.
            try:
                pending_value = _json_object(pending_bytes, "pending checkpoint")
            except ShimError:
                raise ShimError("invalid pending sync checkpoint", code="eval_capture_invalid")
            if pending_value.get("command") in {"init", "sync"}:
                pending_entry = self._capture_bytes(
                    directory, pending_logical.as_posix(), pending_bytes
                )
        if command in {("sync",), ("init",)} and pending_entry is None:
            raise ShimError("sync origin did not retain pending checkpoint", code="eval_capture_invalid")
        if command[:2] == ("source", "snapshot-url") and pending_entry is not None:
            raise ShimError("snapshot capture unexpectedly retained pending sync checkpoint", code="eval_capture_invalid")
        manifest = {
            "schema_version": 1,
            "command_id": self.command_id,
            "command": list(command),
            "capture_point": "after_command_before_return",
            "result_id": result_id,
            "corpus_revision": revision,
            "records": records,
            "files": files,
            "pending": pending_entry,
        }
        state = directory / "source-state.json"
        self._write_bytes(state, _canonical_json_line(manifest))
        return state.relative_to(self.config.root).as_posix()

    def _capture_receipt_artifacts(
        self, result: dict[str, object], command: tuple[str, ...]
    ) -> str | None:
        """Copy immutable receipt bytes while this command boundary is still owned."""

        assert self.command_id is not None
        product = _product_result(result)
        data = product.get("data") if type(product) is dict else None
        if type(data) is not dict:
            return None
        reference: SyncResultReference | None = None
        durable_entry: dict[str, object] | None = None
        delivery_entry: dict[str, object] | None = None
        origin = command in {("sync",), ("init",)} or command[:2] == (
            "source",
            "snapshot-url",
        )
        consume = command[:2] == ("source", "consume-sync-result")
        if not origin and not consume:
            return None
        directory = self.config.root / _CAPTURE_DIRECTORY / self.command_id
        if not directory.is_dir() or directory.is_symlink():
            raise ShimError("missing source capture directory", code="eval_capture_invalid")
        if origin:
            raw_reference = data.get("result_manifest")
            if raw_reference is None:
                return None
            try:
                reference = SyncResultReference.from_dict(raw_reference)
            except (TypeError, ValueError) as error:
                raise ShimError("invalid manifest reference", code="eval_capture_invalid") from error
        elif consume:
            if product.get("ok") is not True:
                return None
            result_id = data.get("result_id")
            if type(result_id) is not str:
                raise ShimError("invalid consume result identity", code="eval_capture_invalid")
            durable_logical = PurePosixPath(
                ".brain", "sync-results", f"consumed_{result_id}.json"
            )
            durable_bytes = _read_workspace_regular_file(
                self.config.workspace,
                durable_logical,
                label="durable consumption receipt",
            )
            try:
                durable_value = _json_object(durable_bytes, "durable consumption receipt")
                reference = SyncResultReference.from_dict(durable_value.get("reference"))
            except (TypeError, ValueError, ShimError) as error:
                raise ShimError("invalid durable consumption receipt", code="eval_capture_invalid") from error
            if (
                reference.result_id != result_id
                or data.get("manifest_path") != reference.path.as_posix()
                or data.get("corpus_revision") != reference.corpus_revision
            ):
                raise ShimError("consume result does not bind durable receipt", code="eval_capture_invalid")
            durable_entry = self._capture_bytes(
                directory, durable_logical.as_posix(), durable_bytes
            )
            raw_delivery = durable_value.get("handoff_delivery")
            if raw_delivery is not None:
                try:
                    delivery = HandoffDeliveryReference.from_dict(raw_delivery)
                except (TypeError, ValueError) as error:
                    raise ShimError("invalid durable handoff delivery", code="eval_capture_invalid") from error
                delivery_bytes = _read_workspace_regular_file(
                    self.config.workspace, delivery.path, label="handoff delivery"
                )
                if hashlib.sha256(delivery_bytes).hexdigest() != delivery.sha256:
                    raise ShimError("handoff delivery digest mismatch", code="eval_capture_invalid")
                delivery_entry = self._capture_bytes(
                    directory, delivery.path.as_posix(), delivery_bytes
                )
        assert reference is not None
        stream_bytes = _read_workspace_regular_file(
            self.config.workspace, reference.path, label="sync result stream"
        )
        if hashlib.sha256(stream_bytes).hexdigest() != reference.sha256:
            raise ShimError("sync result stream digest mismatch", code="eval_capture_invalid")
        stream_entry = self._capture_bytes(directory, reference.path.as_posix(), stream_bytes)
        manifest = {
            "schema_version": 1,
            "command_id": self.command_id,
            "capture_point": "after_command_before_return",
            "result_id": reference.result_id,
            "stream": stream_entry,
            "durable": durable_entry,
            "delivery": delivery_entry,
        }
        receipt = directory / "receipt-artifacts.json"
        self._write_bytes(receipt, _canonical_json_line(manifest))
        return receipt.relative_to(self.config.root).as_posix()

    def finish(self, *, argv: list[str], code: int, raw_result: bytes, result: dict[str, object], command: tuple[str, ...]) -> None:
        if self.command_id is None or self.start_sequence is None:
            raise RuntimeError("recorder start was not called")
        result_sha = hashlib.sha256(raw_result).hexdigest()
        state_path = self._capture_state(result, command)
        receipt_artifacts_path = self._capture_receipt_artifacts(result, command)
        if state_path is not None:
            sequence = self.start_sequence + 1
            self._append(_TRACE_NAME, {"sequence": sequence, "kind": "source_state", "command_id": self.command_id, "capture_path": state_path})
            end_sequence = sequence + 1
        else:
            end_sequence = self.start_sequence + 1
        result_directory = self.config.root / _CAPTURE_DIRECTORY / self.command_id
        if self._raw_write_root_fd is None:
            result_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        result_path = result_directory / "result.json"
        self._write_bytes(result_path, raw_result)
        self._append(_TRACE_NAME, {"sequence": end_sequence, "kind": "command_end", "command_id": self.command_id})
        timestamp = self._timestamp()
        self._append(
            _CAPTURE_INDEX_NAME,
            {
                "command_id": self.command_id,
                "argv": argv,
                "argv_sha256": hashlib.sha256(_canonical_json(argv)).hexdigest(),
                "exit_code": code,
                "result_path": result_path.relative_to(self.config.root).as_posix(),
                "result_sha256": result_sha,
                "source_state_path": state_path,
                "receipt_artifacts_path": receipt_artifacts_path,
                "start_sequence": self.start_sequence,
                "end_sequence": end_sequence,
                "phase": self.config.phase,
                "timestamp": timestamp,
            },
        )
        # The public shim log is deliberately limited to this canonical tuple.
        self._append(
            _COMMAND_LOG_NAME,
            {"argv": argv, "exit_code": code, "result_sha256": result_sha, "timestamp": timestamp},
        )


def _product_result(result: dict[str, object]) -> dict[str, object]:
    if result.get("command") == "eval mock-web-capture":
        data = result.get("data")
        if type(data) is dict and type(data.get("product_result")) is dict:
            return data["product_result"]
    return result


def _result_identity(data: dict[str, object]) -> tuple[str | None, str | None]:
    reference = data.get("result_manifest")
    if type(reference) is dict:
        result_id = reference.get("result_id")
        revision = reference.get("corpus_revision")
        if type(result_id) is str and type(revision) is str:
            return result_id, revision
    registration = data.get("registration")
    if type(registration) is dict and type(registration.get("corpus_revision")) is str:
        return None, registration["corpus_revision"]
    result_id = data.get("result_id")
    revision = data.get("corpus_revision")
    if type(result_id) is str and type(revision) is str:
        return result_id, revision
    if type(data.get("corpus_revision")) is str:
        return None, data["corpus_revision"]
    return (result_id if type(result_id) is str else None, revision if type(revision) is str else None)


def _requires_source_state(command: tuple[str, ...]) -> bool:
    return command in {("sync",), ("init",)} or command[:2] in {
        ("source", "snapshot-url"),
        ("source", "consume-sync-result"),
        ("source", "register-extraction"),
        ("wiki", "apply"),
    }


def _command_name(args: Sequence[str]) -> str:
    if len(args) >= 3 and args[1:3] == ["eval", "mock-web-capture"]:
        return "eval mock-web-capture"
    if len(args) >= 3 and args[1:3] == ["eval", "sha256"]:
        return "eval sha256"
    if len(args) >= 3 and args[1] in {"source", "wiki", "links"}:
        return " ".join(args[1:3])
    return args[1] if len(args) > 1 else "brain"


def _failure(args: Sequence[str], message: str, code: str) -> tuple[int, bytes, dict[str, object]]:
    stream = io.StringIO()
    render_json(CommandResult(_command_name(args), False, {}, errors=(Diagnostic(code, message),)), stream)
    raw = stream.getvalue().encode("utf-8")
    return 2, raw, json.loads(raw)


def _reject_direct_transport(args: Sequence[str]) -> None:
    if len(args) >= 3 and args[1:3] == ["source", "snapshot-url"]:
        # The static URL is constructed only inside the authorized initial
        # fixture path.  An external caller may use this command solely for
        # product's rendered-import form, which never constructs a transport.
        has_raw_url = any(value == "--url" or value.startswith("--url=") for value in args)
        has_rendered_staging = any(
            value == "--rendered-staging-path" or value.startswith("--rendered-staging-path=")
            for value in args
        )
        if has_raw_url or not has_rendered_staging:
            raise ShimError(
                "raw source snapshots are unavailable in fixture evaluation",
                code="eval_raw_url_denied",
            )


def _transport_like(value: str) -> bool:
    return bool(re.search(r"(?i)(https?://|file:|socket:|javascript:)", value))


def _parse_exact_flags(args: Sequence[str], prefix: Sequence[str], flags: Sequence[str]) -> dict[str, str]:
    if list(args[: len(prefix)]) != list(prefix) or len(args) != len(prefix) + 2 * len(flags):
        raise ShimError("fixture eval arguments are not exact", code="eval_fixture_denied")
    tail = args[len(prefix) :]
    if list(tail[::2]) != list(flags) or any(_transport_like(value) for value in tail[1::2]):
        raise ShimError("fixture eval contains unsupported transport-like input", code="eval_fixture_denied")
    return dict(zip(tail[::2], tail[1::2]))


def _run_product(args: Sequence[str], cwd: Path, services: CommandServices) -> tuple[int, bytes, dict[str, object], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = product_main(list(args), cwd=cwd, stdout=stdout, stderr=stderr, services=services)
    raw = stdout.getvalue().encode("utf-8")
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ShimError("normal CLI did not produce a JSON result", code="eval_product_failed") from error
    if type(result) is not dict:
        raise ShimError("normal CLI result is not an object", code="eval_product_failed")
    return code, raw, result, stderr.getvalue()


def _render_eval_result(command: str, data: dict[str, object]) -> tuple[int, bytes, dict[str, object]]:
    stream = io.StringIO()
    render_json(CommandResult(command, True, data), stream)
    raw = stream.getvalue().encode("utf-8")
    return 0, raw, json.loads(raw)


def _initial_fixture(config: ControlConfig, args: Sequence[str]) -> tuple[int, bytes, dict[str, object]]:
    if config.phase != "approved_capture" or config.approval is None:
        raise ShimError("initial web capture is not authorized in this phase", code="eval_phase_denied")
    values = _parse_exact_flags(
        args,
        ("--json", "eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.initial"),
        ("--approval-event-id", "--approval-scope", "--approval-note"),
    )
    if tuple(values[flag] for flag in ("--approval-event-id", "--approval-scope", "--approval-note")) != config.approval:
        raise ShimError("initial web capture approval does not match the runner approval", code="eval_approval_denied")
    descriptor, static_shell, _rendered_dom = _load_fixture_assets(config)
    transport = StaticFixtureTransport(
        expected_url=descriptor.final_url,
        body=static_shell,
        retrieved_at=descriptor.retrieved_at,
        detected_media_type=descriptor.media_type,
    )
    services = replace(controlled_services(), web_transport_factory=lambda: transport)
    inner = [
        "--json",
        "source",
        "snapshot-url",
        "--url",
        descriptor.final_url,
        "--description",
        "controlled standard fixture",
        "--approval-event-id",
        config.approval[0],
        "--approval-scope",
        config.approval[1],
        "--approval-note",
        config.approval[2],
    ]
    code, _raw, product, _stderr = _run_product(inner, config.workspace, services)
    if code != 0 or product.get("ok") is not True:
        raise ShimError("controlled static snapshot did not complete", code="eval_product_failed")
    return _render_eval_result(
        "eval mock-web-capture",
        {"fixture_id": "web-approval-and-capture.initial", "product_result": product},
    )


def _safe_web_staging_path(workspace: Path, handoff_id: str) -> tuple[Path, str]:
    relative = PurePosixPath(".brain") / "web-staging" / handoff_id / "rendered.html"
    current = workspace
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ShimError("unsafe rendered staging directory", code="eval_fixture_denied")
        else:
            current.mkdir(mode=0o700)
    return workspace / relative, relative.as_posix()


def _write_rendered_staging(workspace: Path, handoff_id: str, body: bytes) -> None:
    """Write the fixed leaf through pinned, no-follow workspace directories."""

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    root_fd = -1
    current_fd = -1
    leaf_fd = -1
    try:
        root_fd = os.open(workspace, flags)
        current_fd = root_fd
        for component in (".brain", "web-staging", handoff_id):
            next_fd = os.open(component, flags, dir_fd=current_fd)
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise ShimError("unsafe rendered staging directory", code="eval_fixture_denied")
            except BaseException:
                os.close(next_fd)
                raise
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        leaf_fd = os.open(
            "rendered.html",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=current_fd,
        )
        offset = 0
        while offset < len(body):
            offset += os.write(leaf_fd, body[offset:])
        os.fsync(leaf_fd)
    except OSError as error:
        raise ShimError("rendered staging path is unsafe", code="eval_fixture_denied") from error
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if current_fd >= 0 and current_fd != root_fd:
            os.close(current_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _is_consumed_rendered_delivery(paths: RepoPaths, handoff) -> bool:
    """Accept only an item in a production-validated immutable delivery."""

    store = SyncResultStore(paths)
    directory = paths.root / ".brain" / "sync-results"
    try:
        directory_info = directory.lstat()
    except OSError as error:
        raise ShimError("missing consumption receipt directory", code="eval_fixture_denied") from error
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        raise ShimError("unsafe consumption receipt directory", code="eval_fixture_denied")
    for receipt_path in sorted(directory.glob("consumed_sync_*.json"), key=lambda item: item.name):
        if re.fullmatch(r"consumed_sync_[0-9a-f]{64}\.json", receipt_path.name) is None:
            raise ShimError("unsafe consumption receipt", code="eval_fixture_denied")
        receipt_logical = PurePosixPath(".brain", "sync-results", receipt_path.name)
        try:
            receipt_bytes = _read_workspace_regular_file(
                paths.root, receipt_logical, label="consumption receipt"
            )
            receipt_value = _json_object(receipt_bytes, "consumption receipt")
            reference = SyncResultReference.from_dict(receipt_value.get("reference"))
            receipt = store.load_consumption_receipt(reference)
        except (OSError, TypeError, ValueError, ShimError) as error:
            raise ShimError("invalid consumption receipt", code="eval_fixture_denied") from error
        if receipt is None or receipt.handoff_delivery is None:
            continue
        if receipt_bytes != _canonical_json(receipt.receipt_payload()):
            raise ShimError("consumption receipt changed during authorization", code="eval_fixture_denied")
        try:
            delivery_bytes = _read_workspace_regular_file(
                paths.root, receipt.handoff_delivery.path, label="handoff delivery"
            )
            if hashlib.sha256(delivery_bytes).hexdigest() != receipt.handoff_delivery.sha256:
                raise ValueError("delivery digest")
            delivery = _json_object(delivery_bytes, "handoff delivery")
            items = delivery.get("items")
            if type(items) is not list:
                raise ValueError("delivery items")
            parsed = [agent_handoff._decode_item(item) for item in items]
        except (OSError, TypeError, ValueError) as error:
            raise ShimError("invalid handoff delivery", code="eval_fixture_denied") from error
        if any(item == handoff for item in parsed):
            return True
    return False


def _rendered_fixture(config: ControlConfig, args: Sequence[str]) -> tuple[int, bytes, dict[str, object]]:
    if config.phase != "approved_capture":
        raise ShimError("rendered web capture is not authorized in this phase", code="eval_phase_denied")
    values = _parse_exact_flags(
        args,
        ("--json", "eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.rendered"),
        ("--handoff-id",),
    )
    handoff_id = values["--handoff-id"]
    if _HANDOFF.fullmatch(handoff_id) is None:
        raise ShimError("rendered capture handoff ID is invalid", code="eval_fixture_denied")
    paths = RepoPaths.discover(config.workspace)
    try:
        handoff = agent_handoff.load_handoff_item(paths, handoff_id)
        record = LedgerStore(paths).load_all().get(handoff.source_id)
    except (OSError, ValueError) as error:
        raise ShimError("rendered capture handoff is unavailable", code="eval_fixture_denied") from error
    if (
        handoff.kind != "rendered_web_capture"
        or record is None
        or record.state.value != "needs_agent"
        or not _is_consumed_rendered_delivery(paths, handoff)
    ):
        raise ShimError("rendered capture handoff is not pending", code="eval_fixture_denied")
    _descriptor, _static_shell, rendered_dom = _load_fixture_assets(config)
    _target, logical = _safe_web_staging_path(config.workspace, handoff_id)
    _write_rendered_staging(config.workspace, handoff_id, rendered_dom)
    return _render_eval_result(
        "eval mock-web-capture",
        {
            "fixture_id": "web-approval-and-capture.rendered",
            "handoff_id": handoff_id,
            "path": logical,
            "sha256": hashlib.sha256(rendered_dom).hexdigest(),
            "bytes": len(rendered_dom),
        },
    )


@dataclass(frozen=True)
class _PathObservation:
    dev: int
    ino: int
    mode: int
    size: int
    mtime_ns: int
    nlink: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> _PathObservation:
        return cls(
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_nlink,
        )


def _safe_sha256_path(workspace: Path, value: str) -> tuple[Path, str, tuple[_PathObservation, ...]]:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) != 7
        or path.parts[:2] != (".brain", "wiki-staging")
        or _WIKI_STAGE.fullmatch(path.parts[2]) is None
        or path.parts[3:5] != ("files", "wiki")
        or path.parts[5] != "questions"
        or _QUESTION_STAGE_LEAF.fullmatch(path.name) is None
    ):
        raise ShimError("sha256 path is outside exact wiki staging", code="eval_fixture_denied")
    current = workspace
    observations: list[_PathObservation] = []
    for part in path.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise ShimError("sha256 target is unavailable", code="eval_fixture_denied") from error
        if stat.S_ISLNK(info.st_mode):
            raise ShimError("sha256 target may not traverse a symlink", code="eval_fixture_denied")
        observations.append(_PathObservation.from_stat(info))
    if not stat.S_ISREG(observations[-1].mode) or observations[-1].nlink != 1:
        raise ShimError("sha256 target is not a regular file", code="eval_fixture_denied")
    return current, path.as_posix(), tuple(observations)


def _same_observation(first: _PathObservation, second: os.stat_result, *, directory: bool = False) -> bool:
    if (first.dev, first.ino, first.mode) != (second.st_dev, second.st_ino, second.st_mode):
        return False
    if directory:
        return stat.S_ISDIR(second.st_mode)
    return (
        stat.S_ISREG(second.st_mode)
        and first.size == second.st_size
        and first.mtime_ns == second.st_mtime_ns
        and first.nlink == second.st_nlink == 1
    )


def _read_stable_sha256_target(
    workspace: Path,
    logical: PurePosixPath,
    observations: tuple[_PathObservation, ...],
) -> bytes:
    """Open each checked component descriptor-relatively and retain the leaf fd."""

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    root_fd = -1
    current_fd = -1
    leaf_fd = -1
    try:
        root_fd = os.open(workspace, directory_flags)
        current_fd = root_fd
        for index, part in enumerate(logical.parts[:-1]):
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            try:
                info = os.fstat(next_fd)
                if not _same_observation(observations[index], info, directory=True):
                    raise ShimError("sha256 staging ancestor changed", code="eval_fixture_denied")
            except BaseException:
                os.close(next_fd)
                raise
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        leaf_fd = os.open(
            logical.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        before = os.fstat(leaf_fd)
        if not _same_observation(observations[-1], before):
            raise ShimError("sha256 target changed before open", code="eval_fixture_denied")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(leaf_fd, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                raise ShimError("sha256 target exceeds fixture read bound", code="eval_fixture_denied")
            chunks.append(chunk)
        after = os.fstat(leaf_fd)
        if not _same_observation(observations[-1], after):
            raise ShimError("sha256 target changed while reading", code="eval_fixture_denied")
        return b"".join(chunks)
    except OSError as error:
        raise ShimError("sha256 target cannot be safely opened", code="eval_fixture_denied") from error
    finally:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        if current_fd >= 0 and current_fd != root_fd:
            os.close(current_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _sha256_fixture(config: ControlConfig, args: Sequence[str]) -> tuple[int, bytes, dict[str, object]]:
    if config.scenario_id != "web-approval-and-capture" or config.phase != "approved_capture":
        raise ShimError("sha256 fixture helper is unavailable in this phase", code="eval_phase_denied")
    values = _parse_exact_flags(args, ("--json", "eval", "sha256"), ("--path",))
    _target, normalized, observations = _safe_sha256_path(config.workspace, values["--path"])
    logical = PurePosixPath(normalized)
    body = _read_stable_sha256_target(config.workspace, logical, observations)
    return _render_eval_result("eval sha256", {"path": normalized, "sha256": hashlib.sha256(body).hexdigest()})


def _dispatch(config: ControlConfig, args: Sequence[str]) -> tuple[int, bytes, dict[str, object], tuple[str, ...]]:
    _reject_direct_transport(args)
    if len(args) >= 2 and args[1] == "eval":
        if config.scenario_id != "web-approval-and-capture":
            raise ShimError("fixture eval is unavailable outside the web scenario", code="eval_phase_denied")
        if args[2:3] == ["mock-web-capture"]:
            fixture_id = args[args.index("--fixture-id") + 1] if "--fixture-id" in args and args.index("--fixture-id") + 1 < len(args) else ""
            if fixture_id == "web-approval-and-capture.initial":
                code, raw, result = _initial_fixture(config, args)
                return code, raw, result, ("source", "snapshot-url")
            if fixture_id == "web-approval-and-capture.rendered":
                code, raw, result = _rendered_fixture(config, args)
                return code, raw, result, ("eval", "mock-web-capture")
            raise ShimError("unknown fixture ID", code="eval_fixture_denied")
        if args[2:3] == ["sha256"]:
            code, raw, result = _sha256_fixture(config, args)
            return code, raw, result, ("eval", "sha256")
        raise ShimError("unknown eval command", code="eval_fixture_denied")
    code, raw, result, _stderr = _run_product(args, config.workspace, controlled_services())
    return code, raw, result, tuple(args[1:])


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    recorder_factory: RecorderFactory | None = None,
) -> int:
    """Run one fixture command and atomically record its host-side evidence."""

    args = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    environment = os.environ if environ is None else environ
    workdir = Path.cwd() if cwd is None else cwd
    has_exact_json = bool(args) and args[0] == "--json" and args.count("--json") == 1
    try:
        config = load_control(workdir, environment)
    except ShimError as error:
        if not has_exact_json:
            errors.write("brain evaluation shim requires exactly one leading --json\n")
            return 2
        code, raw, _result = _failure(args, str(error), error.code)
        output.write(raw.decode("utf-8"))
        output.flush()
        return code
    if not has_exact_json:
        errors.write("brain evaluation shim requires exactly one leading --json\n")
        code, raw, result = _failure(
            args,
            "brain evaluation shim requires exactly one leading --json",
            "eval_json_required",
        )
        with _locked_control(config.root):
            recorder = Recorder(config) if recorder_factory is None else recorder_factory(config)
            recorder.start()
            recorder.finish(
                argv=["./brain", *args],
                code=code,
                raw_result=raw,
                result=result,
                command=(),
            )
        output.write(raw.decode("utf-8"))
        output.flush()
        return code
    argv_record = ["./brain", *args]
    with _locked_control(config.root):
        recorder = Recorder(config) if recorder_factory is None else recorder_factory(config)
        recorder.start()
        try:
            code, raw, result, command = _dispatch(config, args)
        except (FixtureServiceError, ShimError) as error:
            code, raw, result = _failure(args, str(error), getattr(error, "code", "eval_fixture_denied"))
            command = tuple(args[1:])
        recorder.finish(argv=argv_record, code=code, raw_result=raw, result=result, command=command)
    output.write(raw.decode("utf-8"))
    output.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
