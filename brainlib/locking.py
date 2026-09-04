from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import stat
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Protocol, Self, cast

from .ledger import (
    UnsafeFilesystemError,
    _PinnedDirectory,
    _fsync_directory,
    _require_safe_filesystem,
    _same_file_observation,
)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised in an isolated interpreter
    _fcntl = None


MAX_LOCK_BYTES = 4096
DEFAULT_STALE_AFTER = timedelta(hours=1)
_MAX_REPLACEMENT_RETRIES = 8
_DARWIN_RENAME_EXCL = 0x00000004
_LINUX_RENAME_NOREPLACE = 0x00000001


class _NativeRenameAt(Protocol):
    def __call__(
        self,
        source_fd: int,
        source: bytes,
        target_fd: int,
        target: bytes,
        flags: int,
        /,
    ) -> int: ...


def _load_native_rename_no_replace() -> tuple[_NativeRenameAt, int] | None:
    if sys.platform == "darwin":
        name = "renameatx_np"
        flag = _DARWIN_RENAME_EXCL
    elif sys.platform.startswith("linux"):
        name = "renameat2"
        flag = _LINUX_RENAME_NOREPLACE
    else:
        return None
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, name, None)
    if function is None:
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return cast(_NativeRenameAt, function), flag


_NATIVE_RENAME_NO_REPLACE = _load_native_rename_no_replace()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LockMetadata:
    pid: int
    hostname: str
    started_at: datetime

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("lock pid must be a positive integer")
        if type(self.hostname) is not str or not self.hostname.strip():
            raise ValueError("lock hostname must be a nonempty string")
        if type(self.started_at) is not datetime or self.started_at.tzinfo is None:
            raise ValueError("lock started_at must be an aware UTC datetime")
        try:
            offset = self.started_at.utcoffset()
        except (OverflowError, ValueError) as error:
            raise ValueError("lock started_at must be an aware UTC datetime") from error
        if offset != timedelta(0):
            raise ValueError("lock started_at must be an aware UTC datetime")
        object.__setattr__(
            self,
            "started_at",
            self.started_at.astimezone(timezone.utc),
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": _format_timestamp(self.started_at),
        }

    def to_bytes(self) -> bytes:
        payload = (
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_LOCK_BYTES:
            raise ValueError("lock metadata is too large")
        return payload

    @classmethod
    def from_bytes(cls, payload: bytes) -> Self:
        if type(payload) is not bytes:
            raise ValueError("lock metadata must be bytes")
        if len(payload) > MAX_LOCK_BYTES:
            raise ValueError("lock metadata is too large")
        if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
            raise ValueError("lock metadata must end with exactly one newline")
        try:
            text = payload.decode("utf-8")
            document = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("lock metadata must be canonical JSON") from error
        if type(document) is not dict or set(document) != {
            "pid",
            "hostname",
            "started_at",
        }:
            raise ValueError("lock metadata must contain exactly the required keys")
        started_at = document["started_at"]
        if type(started_at) is not str or not started_at.endswith("Z"):
            raise ValueError("lock started_at must be a canonical UTC timestamp")
        try:
            parsed_started_at = datetime.fromisoformat(started_at[:-1] + "+00:00")
        except ValueError as error:
            raise ValueError(
                "lock started_at must be a canonical UTC timestamp"
            ) from error
        metadata = cls(document["pid"], document["hostname"], parsed_started_at)
        if metadata.to_bytes() != payload:
            raise ValueError("lock metadata must use canonical JSON and UTC timestamp")
        return metadata


class LockHeldError(RuntimeError):
    def __init__(
        self,
        lock_path: Path,
        owner: LockMetadata | None,
        *,
        detail: str | None = None,
    ) -> None:
        self.lock_path = lock_path
        self.owner = owner
        self.owner_metadata = owner
        if owner is None:
            message = f"source write lock is held at {str(lock_path)!r}"
            if detail:
                message += f" ({detail})"
        else:
            message = (
                "source write lock is held by "
                f"pid={owner.pid} hostname={owner.hostname!r} "
                f"started_at={_format_timestamp(owner.started_at)}"
            )
        super().__init__(message)


class LockCleanupError(RuntimeError):
    """The context body completed but lock release was not proven."""


def safe_lock_backend_available() -> bool:
    if _fcntl is None or _NATIVE_RENAME_NO_REPLACE is None:
        return False
    try:
        _require_safe_filesystem()
    except UnsafeFilesystemError:
        return False
    return True


@dataclass(frozen=True)
class _EntryIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> Self:
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )


@dataclass(frozen=True)
class _EntryOwnership:
    device: int
    inode: int
    uid: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> Self:
        return cls(metadata.st_dev, metadata.st_ino, metadata.st_uid)


@dataclass(frozen=True)
class _ObservedLock:
    payload: bytes
    identity: _EntryIdentity


class SourceWriteLock:
    def __init__(
        self,
        lock_path: Path,
        metadata: LockMetadata,
        entry_identity: _EntryIdentity,
        parent_identities: tuple[object, ...],
    ) -> None:
        self.lock_path = lock_path
        self.metadata = metadata
        self._entry_identity = entry_identity
        self._parent_identities = parent_identities
        self._released = False

    @classmethod
    def acquire(
        cls,
        lock_path: Path,
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        now: Callable[[], datetime] = utc_now,
    ) -> Self:
        _validate_lock_path(lock_path)
        if type(stale_after) is not timedelta or stale_after <= timedelta(0):
            raise ValueError("stale_after must be a positive timedelta")
        current_time = _canonical_utc(now(), "now")
        hostname = socket.gethostname()
        metadata = LockMetadata(os.getpid(), hostname, current_time)

        with _open_lock_parent(lock_path, create=True) as parent:
            _lock_directory(parent.descriptor)
            parent.validate()
            replacements = 0
            while True:
                try:
                    identity = _create_lock(
                        parent,
                        lock_path.name,
                        metadata.to_bytes(),
                    )
                except FileExistsError:
                    observed = _read_lock_at(parent.descriptor, lock_path.name)
                    try:
                        owner = LockMetadata.from_bytes(observed.payload)
                    except ValueError:
                        raise LockHeldError(
                            lock_path,
                            None,
                            detail="owner metadata is unavailable",
                        ) from None
                    if not _may_recover(
                        owner,
                        current_time=current_time,
                        stale_after=stale_after,
                        local_hostname=hostname,
                    ):
                        raise LockHeldError(lock_path, owner)
                    parent.validate()
                    current = _read_lock_at(parent.descriptor, lock_path.name)
                    if current != observed:
                        replacements += 1
                        if replacements >= _MAX_REPLACEMENT_RETRIES:
                            raise UnsafeFilesystemError(
                                "lock entry kept changing during stale recovery"
                            )
                        continue
                    if not _claim_and_remove(
                        parent,
                        lock_path.name,
                        expected=current,
                    ):
                        replacements += 1
                        if replacements >= _MAX_REPLACEMENT_RETRIES:
                            raise UnsafeFilesystemError(
                                "lock entry kept changing during stale recovery"
                            )
                    continue
                parent.validate()
                return cls(
                    lock_path,
                    metadata,
                    identity,
                    tuple(parent.identities),
                )

    def release(self) -> bool:
        if self._released:
            return False
        try:
            parent = _open_lock_parent(self.lock_path, create=False)
        except FileNotFoundError:
            return False
        with parent:
            _lock_directory(parent.descriptor)
            parent.validate()
            if tuple(parent.identities) != self._parent_identities:
                raise UnsafeFilesystemError(
                    "lock parent changed since this lock was acquired"
                )
            try:
                observed = _read_lock_at(parent.descriptor, self.lock_path.name)
            except FileNotFoundError:
                return False
            try:
                metadata = LockMetadata.from_bytes(observed.payload)
            except ValueError:
                return False
            if metadata != self.metadata or observed.identity != self._entry_identity:
                return False
            parent.validate()
            current = _read_lock_at(parent.descriptor, self.lock_path.name)
            if current != observed:
                return False
            try:
                removed = _claim_and_remove(
                    parent,
                    self.lock_path.name,
                    expected=current,
                )
            except Exception as error:
                if getattr(error, "_source_lock_entry_removed", False):
                    self._released = True
                raise
            if not removed:
                return False
            self._released = True
            return True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> bool:
        if self._released:
            return False
        try:
            released = self.release()
        except Exception as error:
            if exc_type is None:
                raise
            assert _exc is not None
            _exc.add_note(
                "Source write lock cleanup failed: "
                f"{type(error).__name__}: {safe_exception_text(error)}"
            )
            return False
        if not released:
            message = "Source write lock cleanup could not prove release."
            if exc_type is None:
                raise LockCleanupError(message)
            assert _exc is not None
            _exc.add_note(message)
        return False


def process_is_alive(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        return None
    return True


def _create_lock(
    parent: _PinnedDirectory,
    name: str,
    payload: bytes,
) -> _EntryIdentity:
    descriptor: int | None = None
    ownership_descriptor: int | None = None
    ownership: _EntryOwnership | None = None
    try:
        descriptor = _exclusive_create(parent.descriptor, name)
        opened = os.fstat(descriptor)
        ownership = _EntryOwnership.from_stat(opened)
        ownership_descriptor = os.dup(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeFilesystemError("new lock entry is not a regular file")
        _fchmod_lock(descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            _write_stream(stream, payload.decode("utf-8"))
            _fsync_file(stream.fileno())
            written = os.fstat(stream.fileno())
        parent.validate()
        named = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        if not _same_file_observation(written, named):
            raise UnsafeFilesystemError("new lock entry changed while it was written")
        identity = _EntryIdentity.from_stat(named)
        _fsync_directory(parent.descriptor)
        parent.validate()
        return identity
    except BaseException as error:
        if ownership is not None:
            try:
                _remove_owned_partial(parent, name, ownership)
            except Exception as cleanup_error:
                error.add_note(
                    "Partial source lock cleanup failed: "
                    f"{type(cleanup_error).__name__}: "
                    f"{safe_exception_text(cleanup_error)}"
                )
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if ownership_descriptor is not None:
            os.close(ownership_descriptor)


def _exclusive_create(directory_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )


def _fchmod_lock(descriptor: int) -> None:
    os.fchmod(descriptor, 0o600)


def _write_stream(stream: IO[str], payload: str) -> None:
    stream.write(payload)
    stream.flush()


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _remove_owned_partial(
    parent: _PinnedDirectory,
    name: str,
    ownership: _EntryOwnership,
) -> None:
    try:
        _claim_and_remove(parent, name, expected=ownership)
    except FileNotFoundError:
        return


def _claim_and_remove(
    parent: _PinnedDirectory,
    name: str,
    *,
    expected: _ObservedLock | _EntryOwnership,
) -> bool:
    quarantine = f".{name}.quarantine-{os.getpid()}-{uuid.uuid4().hex}"
    parent.validate()
    _rename_no_replace_at(parent.descriptor, name, quarantine)
    pending_sync_error: OSError | None = None
    try:
        _fsync_directory(parent.descriptor)
    except OSError as error:
        pending_sync_error = error
    try:
        parent.validate()
    except BaseException as error:
        _attach_pending_sync(error, pending_sync_error)
        raise
    try:
        claimed = _read_lock_at(parent.descriptor, quarantine)
    except BaseException as error:
        restored = False
        try:
            _restore_claimed_entry(parent, quarantine, name)
            restored = True
        except Exception as restore_error:
            error.add_note(
                "Claimed lock restoration failed: "
                f"{type(restore_error).__name__}: "
                f"{safe_exception_text(restore_error)}"
            )
        if not restored:
            _attach_pending_sync(error, pending_sync_error)
        raise
    if isinstance(expected, _ObservedLock):
        matches = _same_claimed_lock(claimed, expected)
    else:
        matches = (
            _EntryOwnership(
                claimed.identity.device,
                claimed.identity.inode,
                claimed.identity.uid,
            )
            == expected
        )
    if not matches:
        try:
            _restore_claimed_entry(parent, quarantine, name)
        except BaseException as error:
            _attach_pending_sync(error, pending_sync_error)
            raise
        return False
    try:
        parent.validate()
        os.unlink(quarantine, dir_fd=parent.descriptor)
    except BaseException as error:
        _attach_pending_sync(error, pending_sync_error)
        raise
    try:
        _fsync_directory(parent.descriptor)
        pending_sync_error = None
        parent.validate()
    except Exception as error:
        _attach_pending_sync(error, pending_sync_error)
        try:
            setattr(error, "_source_lock_entry_removed", True)
        except Exception:
            pass
        raise
    return True


def _same_claimed_lock(claimed: _ObservedLock, expected: _ObservedLock) -> bool:
    left = claimed.identity
    right = expected.identity
    return claimed.payload == expected.payload and (
        left.device,
        left.inode,
        left.mode,
        left.uid,
        left.size,
        left.mtime_ns,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.uid,
        right.size,
        right.mtime_ns,
    )


def _restore_claimed_entry(
    parent: _PinnedDirectory,
    quarantine: str,
    canonical: str,
) -> None:
    try:
        os.link(
            quarantine,
            canonical,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as error:
        raise UnsafeFilesystemError(
            "canonical lock name was concurrently repopulated; claimed entry "
            f"retained as {quarantine!r}"
        ) from error
    except OSError as error:
        raise UnsafeFilesystemError(
            f"claimed lock entry retained as {quarantine!r}; safe no-overwrite "
            "restoration failed"
        ) from error
    _fsync_directory(parent.descriptor)
    parent.validate()
    claimed = os.stat(
        quarantine,
        dir_fd=parent.descriptor,
        follow_symlinks=False,
    )
    restored = os.stat(
        canonical,
        dir_fd=parent.descriptor,
        follow_symlinks=False,
    )
    if not (
        stat.S_ISREG(claimed.st_mode) and _same_file_observation(claimed, restored)
    ):
        raise UnsafeFilesystemError(
            f"claimed lock entry retained as {quarantine!r}; restored entry "
            "could not be verified"
        )
    # The quarantine hard link is deliberately retained. Removing it after a
    # separate canonical-name check could delete the captured successor's last
    # link if an uncooperative writer replaced the canonical name in between.


def _rename_no_replace_at(directory_fd: int, source: str, target: str) -> None:
    native = _NATIVE_RENAME_NO_REPLACE
    if native is None:
        raise UnsafeFilesystemError("atomic no-replace lock quarantine is unavailable")
    if (
        not source
        or not target
        or "/" in source
        or "/" in target
        or "\0" in source
        or "\0" in target
    ):
        raise ValueError("lock entry names must be canonical path components")
    function, flag = native
    ctypes.set_errno(0)
    result = function(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(target),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        error = OSError(error_number, os.strerror(error_number), source)
        error.filename2 = target
        raise error


def _read_lock_at(directory_fd: int, name: str) -> _ObservedLock:
    descriptor: int | None = None
    try:
        named_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(named_before.st_mode):
            raise ValueError(f"{name} must be a regular lock file")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{name} must be a regular lock file")
        chunks: list[bytes] = []
        remaining = MAX_LOCK_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not (
            _same_file_observation(before, after)
            and _same_file_observation(after, named_after)
        ):
            raise UnsafeFilesystemError("lock file changed while it was being read")
        return _ObservedLock(payload, _EntryIdentity.from_stat(after))
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"{name} must be a regular lock file") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _may_recover(
    owner: LockMetadata,
    *,
    current_time: datetime,
    stale_after: timedelta,
    local_hostname: str,
) -> bool:
    age = current_time - owner.started_at
    if age < timedelta(0) or age < stale_after:
        return False
    if owner.hostname != local_hostname:
        return True
    try:
        alive = process_is_alive(owner.pid)
    except PermissionError:
        alive = True
    except ProcessLookupError:
        alive = False
    except Exception:
        alive = None
    return alive is not True


def _open_lock_parent(lock_path: Path, *, create: bool) -> _PinnedDirectory:
    _require_safe_filesystem()
    if _NATIVE_RENAME_NO_REPLACE is None:
        raise UnsafeFilesystemError("atomic no-replace lock quarantine is unavailable")
    try:
        return _PinnedDirectory.open(lock_path.parent)
    except FileNotFoundError:
        if not create:
            raise
    parent = lock_path.parent
    pinned = _PinnedDirectory.open(parent.parent)
    try:
        _lock_directory(pinned.descriptor)
        try:
            os.mkdir(parent.name, mode=0o700, dir_fd=pinned.descriptor)
            _fsync_directory(pinned.descriptor)
        except FileExistsError:
            pass
        pinned.open_child(pinned.descriptor, parent.name)
        pinned.validate()
        return pinned
    except BaseException:
        pinned.close()
        raise


def _lock_directory(descriptor: int) -> None:
    if _fcntl is None:
        raise UnsafeFilesystemError("safe source lock backend is unavailable")
    try:
        _fcntl.flock(descriptor, _fcntl.LOCK_EX)
    except OSError as error:
        raise UnsafeFilesystemError(
            "cannot serialize source lock operations on the parent directory"
        ) from error


def _validate_lock_path(lock_path: Path) -> None:
    if not isinstance(lock_path, Path) or not lock_path.is_absolute():
        raise ValueError("lock path must be an absolute path")
    if (
        not lock_path.name
        or lock_path.name in {".", ".."}
        or "\0" in str(lock_path)
        or any(part in {"", ".", ".."} for part in lock_path.parts[1:])
    ):
        raise ValueError("lock path must be canonical")


def _canonical_utc(value: datetime, label: str) -> datetime:
    try:
        return LockMetadata(1, "validation", value).started_at
    except ValueError as error:
        raise ValueError(f"{label} must be an aware UTC datetime") from error


def _format_timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _unique_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate lock metadata key: {key!r}")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def safe_exception_text(error: BaseException) -> str:
    """Return bounded ASCII-safe exception text for diagnostics and notes."""

    try:
        text = str(error)
    except Exception:
        text = "<unprintable>"
    escaped = ascii(text)
    if len(escaped) > 258:
        escaped = escaped[:255] + "...'"
    return escaped


def _attach_pending_sync(
    error: BaseException,
    pending_sync_error: OSError | None,
) -> None:
    if pending_sync_error is None:
        return
    error.add_note(
        "Initial quarantine rename durability failed: "
        f"{type(pending_sync_error).__name__}: "
        f"{safe_exception_text(pending_sync_error)}"
    )
