from __future__ import annotations

import errno
import json
import multiprocessing
import os
import socket
import stat
import tempfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brainlib.locking as locking_module
from brainlib.layout import RepoPaths
from brainlib.ledger import UnsafeFilesystemError
from brainlib.locking import (
    MAX_LOCK_BYTES,
    LockCleanupError,
    LockHeldError,
    LockMetadata,
    SourceWriteLock,
    safe_exception_text,
)
from tests.helpers import FIXED_NOW


FIXED_OLD_TIME = FIXED_NOW - timedelta(hours=2)


def test_public_exception_formatter_is_ascii_safe_and_bounded() -> None:
    rendered = safe_exception_text(RuntimeError("hostile\n\x1b[31m" + "x" * 1000))

    assert "\n" not in rendered
    assert "\x1b" not in rendered
    assert "\\n" in rendered
    assert "\\x1b" in rendered
    assert len(rendered) <= 259


def _process_lock_contender(path_text: str, connection: object) -> None:
    from pathlib import Path

    from brainlib.locking import LockHeldError, SourceWriteLock

    channel = connection
    try:
        channel.send(("ready", os.getpid()))  # type: ignore[attr-defined]
        if channel.recv() != "start":  # type: ignore[attr-defined]
            raise RuntimeError("parent did not start contender")
        try:
            lock = SourceWriteLock.acquire(Path(path_text))
        except LockHeldError as error:
            channel.send(  # type: ignore[attr-defined]
                ("held", error.owner.pid if error.owner is not None else None)
            )
            return
        channel.send(("acquired", lock.metadata.pid))  # type: ignore[attr-defined]
        if channel.recv() != "release":  # type: ignore[attr-defined]
            raise RuntimeError("parent did not release contender")
        channel.send(("released", lock.release()))  # type: ignore[attr-defined]
    except BaseException as error:
        channel.send(("error", type(error).__name__, ascii(str(error))))  # type: ignore[attr-defined]
    finally:
        channel.close()  # type: ignore[attr-defined]


def write_lock(
    path: Path,
    *,
    pid: int,
    started_at: datetime,
    hostname: str | None = None,
) -> bytes:
    payload = (
        json.dumps(
            {
                "pid": pid,
                "hostname": hostname or socket.gethostname(),
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def test_lock_metadata_is_frozen_and_has_one_canonical_encoding() -> None:
    metadata = LockMetadata(123, "test-host", FIXED_NOW)
    expected = (
        b'{"hostname":"test-host","pid":123,"started_at":"2026-09-04T00:00:00Z"}\n'
    )

    assert metadata.to_bytes() == expected
    assert LockMetadata.from_bytes(expected) == metadata
    with pytest.raises(FrozenInstanceError):
        metadata.pid = 456  # type: ignore[misc]


@pytest.mark.parametrize(
    ("pid", "hostname", "started_at"),
    [
        (0, "host", FIXED_NOW),
        (-1, "host", FIXED_NOW),
        (True, "host", FIXED_NOW),
        (1, "", FIXED_NOW),
        (1, "   ", FIXED_NOW),
        (1, "host", datetime(2026, 9, 4)),
        (1, "host", datetime(2026, 9, 4, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_lock_metadata_rejects_invalid_owner_fields(
    pid: object, hostname: object, started_at: object
) -> None:
    with pytest.raises(ValueError):
        LockMetadata(pid, hostname, started_at)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        b'{"hostname":"host","pid":1,"started_at":"2026-09-04T00:00:00Z","extra":1}\n',
        b'{"hostname":"host","pid":1}\n',
        b'{"hostname":"host","hostname":"other","pid":1,"started_at":"2026-09-04T00:00:00Z"}\n',
        b'{"hostname":"host","pid":1,"started_at":"2026-09-04T00:00:00+00:00"}\n',
        b'{"hostname":"host","pid":1,"started_at":"2026-09-04T00:00:00.000000Z"}\n',
        b'{"hostname":"host","pid":true,"started_at":"2026-09-04T00:00:00Z"}\n',
        b'{"hostname":"host","pid":NaN,"started_at":"2026-09-04T00:00:00Z"}\n',
        b'{"hostname":"host","pid":Infinity,"started_at":"2026-09-04T00:00:00Z"}\n',
        b'{"hostname":"host","pid":-Infinity,"started_at":"2026-09-04T00:00:00Z"}\n',
        b'{"hostname":"host","pid":1,"started_at":"2026-09-04T00:00:00Z"}',
        b"[]\n",
    ],
)
def test_lock_metadata_strictly_rejects_noncanonical_documents(payload: bytes) -> None:
    with pytest.raises(ValueError):
        LockMetadata.from_bytes(payload)


def test_lock_metadata_rejects_oversized_documents_before_parsing() -> None:
    with pytest.raises(ValueError, match="too large"):
        LockMetadata.from_bytes(b"{" + b" " * MAX_LOCK_BYTES + b"}\n")


def test_lock_metadata_refuses_to_serialize_an_oversized_document() -> None:
    metadata = LockMetadata(1, "h" * MAX_LOCK_BYTES, FIXED_NOW)

    with pytest.raises(ValueError, match="too large"):
        metadata.to_bytes()


def test_acquire_creates_mode_0600_canonical_durable_lock(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock

    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == lock.metadata.to_bytes()
    assert lock.metadata.started_at == FIXED_NOW
    assert lock.release() is True
    assert not path.exists()


def test_second_writer_receives_owner_metadata(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW) as owner:
        with pytest.raises(LockHeldError, match="pid") as raised:
            SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert raised.value.owner == owner.metadata
    assert raised.value.owner_metadata == owner.metadata


def test_spawned_processes_have_one_winner_and_release_all_descriptors(
    repo_root: Path,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    context = multiprocessing.get_context("spawn")

    def start_contender() -> tuple[object, multiprocessing.Process]:
        parent, child = context.Pipe()
        process = context.Process(
            target=_process_lock_contender,
            args=(str(path), child),
        )
        process.start()
        child.close()
        assert parent.poll(10)  # type: ignore[attr-defined]
        message = parent.recv()  # type: ignore[attr-defined]
        assert message[0] == "ready"
        return parent, process

    channels: list[object] = []
    processes: list[multiprocessing.Process] = []
    try:
        first_channel, first_process = start_contender()
        second_channel, second_process = start_contender()
        channels.extend((first_channel, second_channel))
        processes.extend((first_process, second_process))
        for channel in channels:
            channel.send("start")  # type: ignore[attr-defined]
        results = []
        for channel in channels:
            assert channel.poll(10)  # type: ignore[attr-defined]
            results.append(channel.recv())  # type: ignore[attr-defined]
        assert sorted(result[0] for result in results) == ["acquired", "held"]
        acquired_pid = next(item[1] for item in results if item[0] == "acquired")
        held_owner_pid = next(item[1] for item in results if item[0] == "held")
        assert held_owner_pid == acquired_pid
        winner = results.index(next(item for item in results if item[0] == "acquired"))
        channels[winner].send("release")  # type: ignore[attr-defined]
        assert channels[winner].poll(10)  # type: ignore[attr-defined]
        assert channels[winner].recv() == ("released", True)  # type: ignore[attr-defined]
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive()
            assert process.exitcode == 0

        third_channel, third_process = start_contender()
        channels.append(third_channel)
        processes.append(third_process)
        third_channel.send("start")  # type: ignore[attr-defined]
        assert third_channel.poll(10)  # type: ignore[attr-defined]
        assert third_channel.recv()[0] == "acquired"  # type: ignore[attr-defined]
        third_channel.send("release")  # type: ignore[attr-defined]
        assert third_channel.poll(10)  # type: ignore[attr-defined]
        assert third_channel.recv() == ("released", True)  # type: ignore[attr-defined]
        third_process.join(timeout=10)
        assert not third_process.is_alive()
        assert third_process.exitcode == 0
    finally:
        for channel in channels:
            try:
                channel.send("release")  # type: ignore[attr-defined]
            except (BrokenPipeError, EOFError, OSError):
                pass
            channel.close()  # type: ignore[attr-defined]
        for process in processes:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_dead_old_lock_is_recovered(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=999_999_999, started_at=FIXED_OLD_TIME)

    with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW) as lock:
        assert lock.metadata.pid == os.getpid()


def test_live_lock_is_never_recovered_only_for_age(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    monkeypatch.setattr(locking_module, "process_is_alive", lambda _pid: True)
    original = write_lock(path, pid=123, started_at=FIXED_OLD_TIME)

    with pytest.raises(LockHeldError):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("age", "hostname", "alive", "recovered"),
    [
        (timedelta(hours=1) - timedelta(microseconds=1), None, False, False),
        (timedelta(hours=1), None, False, True),
        (timedelta(hours=2), None, True, False),
        (timedelta(hours=-1), None, False, False),
        (timedelta(minutes=30), "remote-host.invalid", None, False),
        (timedelta(hours=2), "remote-host.invalid", None, True),
    ],
)
def test_stale_recovery_obeys_age_host_and_liveness_boundaries(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    age: timedelta,
    hostname: str | None,
    alive: bool | None,
    recovered: bool,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, hostname=hostname, started_at=FIXED_NOW - age)
    checks: list[int] = []

    def check(pid: int) -> bool | None:
        checks.append(pid)
        return alive

    monkeypatch.setattr(locking_module, "process_is_alive", check)
    if recovered:
        with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW):
            pass
    else:
        with pytest.raises(LockHeldError):
            SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert checks == (
        [] if hostname == "remote-host.invalid" or age < timedelta(hours=1) else [123]
    )


@pytest.mark.parametrize(
    ("error", "recovered"),
    [
        (PermissionError("not permitted"), False),
        (ProcessLookupError("gone"), True),
        (RuntimeError("uncheckable"), True),
    ],
)
def test_process_check_errors_distinguish_existence_from_uncheckable(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    recovered: bool,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)

    def fail(_pid: int) -> bool:
        raise error

    monkeypatch.setattr(locking_module, "process_is_alive", fail)
    if recovered:
        with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW):
            pass
    else:
        with pytest.raises(LockHeldError):
            SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_acquire_never_follows_or_removes_nonregular_lock_entries(
    repo_root: Path, kind: str
) -> None:
    path = RepoPaths.discover(repo_root).lock
    path.parent.mkdir()
    if kind == "symlink":
        target = repo_root / "outside-lock"
        target.write_text("outside", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    with pytest.raises((ValueError, UnsafeFilesystemError)):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert path.is_symlink() if kind == "symlink" else path.exists()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX unavailable")
def test_acquire_rejects_unix_socket_entry_without_blocking() -> None:
    with tempfile.TemporaryDirectory(
        prefix="sbl-lock-", dir=Path("/tmp").resolve()
    ) as temporary:
        path = Path(temporary) / "lock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            with pytest.raises((ValueError, UnsafeFilesystemError)):
                SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

        assert path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"{\n",
        b'{"hostname":"host","hostname":"again","pid":1,"started_at":"2026-09-04T00:00:00Z"}\n',
        b"x" * (MAX_LOCK_BYTES + 1),
    ],
)
def test_malformed_or_oversized_existing_lock_fails_closed(
    repo_root: Path, payload: bytes
) -> None:
    path = RepoPaths.discover(repo_root).lock
    path.parent.mkdir()
    path.write_bytes(payload)

    with pytest.raises(LockHeldError) as raised:
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert raised.value.owner is None
    assert path.read_bytes() == payload


def test_symlinked_parent_is_rejected_without_touching_target(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    outside = repo_root / "outside"
    outside.mkdir()
    path.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises((ValueError, UnsafeFilesystemError)):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert not (outside / path.name).exists()


@pytest.mark.parametrize(
    "failure_point", ["create", "fchmod", "write", "file_fsync", "dir_fsync"]
)
def test_creation_failures_remove_only_this_attempts_partial_lock(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    path.parent.mkdir()
    descriptors_before = len(os.listdir("/dev/fd"))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"{failure_point} failed")

    monkeypatch.setattr(
        locking_module,
        {
            "create": "_exclusive_create",
            "fchmod": "_fchmod_lock",
            "write": "_write_stream",
            "file_fsync": "_fsync_file",
            "dir_fsync": "_fsync_directory",
        }[failure_point],
        fail,
    )

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert not path.exists()
    monkeypatch.undo()
    with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW):
        pass
    assert len(os.listdir("/dev/fd")) == descriptors_before


def _replace_at_final_claim(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    successor: Path,
) -> os.stat_result:
    successor_identity = successor.stat()
    real_rename = locking_module._rename_no_replace_at
    armed = True

    def replace_then_claim(directory_fd: int, source: str, target: str) -> None:
        nonlocal armed
        if armed and source == path.name:
            armed = False
            os.replace(successor, path)
        real_rename(directory_fd, source, target)

    monkeypatch.setattr(
        locking_module,
        "_rename_no_replace_at",
        replace_then_claim,
    )
    return successor_identity


def _replace_after_restoration_verification(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    later_successor: Path,
) -> None:
    real_link = locking_module.os.link
    real_stat = locking_module.os.stat
    restoration_linked = False
    replaced = False

    def track_link(*args: object, **kwargs: object) -> None:
        nonlocal restoration_linked
        real_link(*args, **kwargs)
        restoration_linked = True

    def replace_after_stat(*args: object, **kwargs: object) -> os.stat_result:
        nonlocal replaced
        result = real_stat(*args, **kwargs)
        if (
            restoration_linked
            and not replaced
            and args
            and args[0] == path.name
            and kwargs.get("dir_fd") is not None
        ):
            replaced = True
            os.replace(later_successor, path)
        return result

    monkeypatch.setattr(locking_module.os, "link", track_link)
    monkeypatch.setattr(locking_module.os, "stat", replace_after_stat)


def _assert_open_inode_remains_named(path: Path, descriptor: int) -> None:
    held = os.fstat(descriptor)
    assert held.st_nlink > 0
    candidates = [path, *path.parent.glob(".source-write.lock.quarantine-*")]
    assert any(
        candidate.exists()
        and (candidate.stat().st_dev, candidate.stat().st_ino)
        == (held.st_dev, held.st_ino)
        for candidate in candidates
    )


def test_stale_recovery_final_claim_never_deletes_fresh_successor(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    successor = path.with_name("successor")
    successor.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    successor_identity = _replace_at_final_claim(monkeypatch, path, successor)

    with pytest.raises(LockHeldError):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert (path.stat().st_dev, path.stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )
    quarantines = list(path.parent.glob(".source-write.lock.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0].stat().st_dev, quarantines[0].stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )


def test_release_final_claim_restores_identical_successor(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    successor = path.with_name("successor")
    successor.write_bytes(lock.metadata.to_bytes())
    successor_identity = _replace_at_final_claim(monkeypatch, path, successor)

    assert lock.release() is False
    assert (path.stat().st_dev, path.stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )
    quarantines = list(path.parent.glob(".source-write.lock.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0].stat().st_dev, quarantines[0].stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )


def test_stale_recovery_retains_captured_successor_after_restored_name_changes(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    captured = path.with_name("captured")
    captured.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    later = path.with_name("later")
    later.write_bytes(LockMetadata(os.getpid(), "later-host", FIXED_NOW).to_bytes())
    descriptor = os.open(captured, os.O_RDONLY)
    try:
        _replace_at_final_claim(monkeypatch, path, captured)
        _replace_after_restoration_verification(monkeypatch, path, later)

        with pytest.raises(LockHeldError):
            SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

        _assert_open_inode_remains_named(path, descriptor)
    finally:
        os.close(descriptor)


def test_release_retains_captured_successor_after_restored_name_changes(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    captured = path.with_name("captured")
    captured.write_bytes(lock.metadata.to_bytes())
    later = path.with_name("later")
    later.write_bytes(lock.metadata.to_bytes())
    descriptor = os.open(captured, os.O_RDONLY)
    try:
        _replace_at_final_claim(monkeypatch, path, captured)
        _replace_after_restoration_verification(monkeypatch, path, later)

        assert lock.release() is False

        _assert_open_inode_remains_named(path, descriptor)
    finally:
        os.close(descriptor)


def test_partial_cleanup_retains_successor_after_restored_name_changes(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    path.parent.mkdir()
    captured = path.with_name("captured")
    captured.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    later = path.with_name("later")
    later.write_bytes(LockMetadata(os.getpid(), "later-host", FIXED_NOW).to_bytes())
    descriptor = os.open(captured, os.O_RDONLY)
    try:
        _replace_at_final_claim(monkeypatch, path, captured)
        _replace_after_restoration_verification(monkeypatch, path, later)

        def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError("write failed")

        monkeypatch.setattr(locking_module, "_write_stream", fail_write)
        with pytest.raises(OSError, match="write failed"):
            SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

        _assert_open_inode_remains_named(path, descriptor)
    finally:
        os.close(descriptor)


def test_initial_quarantine_claim_never_overwrites_existing_entry(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    original = path.stat()

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(locking_module.uuid, "uuid4", lambda: FixedUuid())
    quarantine = path.with_name(
        f".{path.name}.quarantine-{os.getpid()}-{FixedUuid.hex}"
    )
    quarantine.write_bytes(b"existing quarantine")
    existing = quarantine.stat()

    with pytest.raises(FileExistsError):
        lock.release()

    assert (path.stat().st_dev, path.stat().st_ino) == (
        original.st_dev,
        original.st_ino,
    )
    assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == (
        existing.st_dev,
        existing.st_ino,
    )
    assert quarantine.read_bytes() == b"existing quarantine"


def test_locking_fails_closed_without_native_no_replace_claim(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    monkeypatch.setattr(locking_module, "_NATIVE_RENAME_NO_REPLACE", None)

    with pytest.raises(
        UnsafeFilesystemError,
        match="atomic no-replace lock quarantine is unavailable",
    ):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert not path.exists()


def test_mismatched_claim_is_retained_if_canonical_name_is_repopulated(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    claimed_successor = path.with_name("claimed-successor")
    claimed_successor.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    claimed_identity = claimed_successor.stat()
    canonical_successor = path.with_name("canonical-successor")
    canonical_successor.write_bytes(
        LockMetadata(os.getpid(), "other-host", FIXED_NOW).to_bytes()
    )
    canonical_identity = canonical_successor.stat()
    real_rename = locking_module._rename_no_replace_at
    armed = True

    def replace_claim_and_repopulate(
        directory_fd: int, source: str, target: str
    ) -> None:
        nonlocal armed
        if armed and source == path.name:
            armed = False
            os.replace(claimed_successor, path)
            real_rename(directory_fd, source, target)
            os.replace(canonical_successor, path)
            return
        real_rename(directory_fd, source, target)

    monkeypatch.setattr(
        locking_module,
        "_rename_no_replace_at",
        replace_claim_and_repopulate,
    )

    with pytest.raises(UnsafeFilesystemError, match="concurrently repopulated"):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert (path.stat().st_dev, path.stat().st_ino) == (
        canonical_identity.st_dev,
        canonical_identity.st_ino,
    )
    quarantines = list(path.parent.glob(".source-write.lock.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0].stat().st_dev, quarantines[0].stat().st_ino) == (
        claimed_identity.st_dev,
        claimed_identity.st_ino,
    )


@pytest.mark.parametrize("failure_point", ["fchmod", "write", "file_fsync"])
def test_failure_cleanup_final_claim_restores_successor(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    path.parent.mkdir()
    successor = path.with_name("successor")
    successor.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    successor_identity = _replace_at_final_claim(monkeypatch, path, successor)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"{failure_point} failed")

    monkeypatch.setattr(
        locking_module,
        {
            "fchmod": "_fchmod_lock",
            "write": "_write_stream",
            "file_fsync": "_fsync_file",
        }[failure_point],
        fail,
    )

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert (path.stat().st_dev, path.stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )
    with pytest.raises(LockHeldError):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    quarantines = list(path.parent.glob(".source-write.lock.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0].stat().st_dev, quarantines[0].stat().st_ino) == (
        successor_identity.st_dev,
        successor_identity.st_ino,
    )


def _exception_text(error: BaseException) -> str:
    return "\n".join((str(error), *getattr(error, "__notes__", ())))


def test_first_claim_fsync_failure_is_surfaced_on_immediate_validation_failure(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    first_sync_failed = False
    real_validate = locking_module._PinnedDirectory.validate

    def fail_first_sync(_descriptor: int) -> None:
        nonlocal first_sync_failed
        first_sync_failed = True
        raise OSError("claim rename fsync failed")

    def fail_after_sync(parent: object) -> None:
        if first_sync_failed:
            raise UnsafeFilesystemError("post-claim validation failed")
        real_validate(parent)  # type: ignore[arg-type]

    monkeypatch.setattr(locking_module, "_fsync_directory", fail_first_sync)
    monkeypatch.setattr(
        locking_module._PinnedDirectory,
        "validate",
        fail_after_sync,
    )

    with pytest.raises(UnsafeFilesystemError) as raised:
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert "claim rename fsync failed" in _exception_text(raised.value)
    assert list(path.parent.glob(".source-write.lock.quarantine-*"))


def test_first_claim_fsync_failure_is_surfaced_when_read_and_restore_sync_fail(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    real_read = locking_module._read_lock_at
    sync_calls = 0

    def fail_syncs(_descriptor: int) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise OSError("claim rename fsync failed")
        raise OSError("restoration fsync failed")

    def fail_claimed_read(directory_fd: int, name: str) -> object:
        if ".quarantine-" in name:
            raise OSError("claimed read failed")
        return real_read(directory_fd, name)

    monkeypatch.setattr(locking_module, "_fsync_directory", fail_syncs)
    monkeypatch.setattr(locking_module, "_read_lock_at", fail_claimed_read)

    with pytest.raises(OSError, match="claimed read failed") as raised:
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    text = _exception_text(raised.value)
    assert "claim rename fsync failed" in text
    assert "restoration fsync failed" in text
    quarantines = list(path.parent.glob(".source-write.lock.quarantine-*"))
    assert len(quarantines) == 1
    assert path.exists()
    assert (path.stat().st_dev, path.stat().st_ino) == (
        quarantines[0].stat().st_dev,
        quarantines[0].stat().st_ino,
    )


def test_first_claim_fsync_failure_is_surfaced_when_repopulation_blocks_restore(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    claimed = path.with_name("claimed")
    claimed.write_bytes(
        LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    )
    later = path.with_name("later")
    later.write_bytes(LockMetadata(os.getpid(), "later-host", FIXED_NOW).to_bytes())
    real_rename = locking_module._rename_no_replace_at
    armed = True

    def claim_and_repopulate(directory_fd: int, source: str, target: str) -> None:
        nonlocal armed
        if armed and source == path.name:
            armed = False
            os.replace(claimed, path)
            real_rename(directory_fd, source, target)
            os.replace(later, path)
            return
        real_rename(directory_fd, source, target)

    def fail_first_sync(_descriptor: int) -> None:
        raise OSError("claim rename fsync failed")

    monkeypatch.setattr(
        locking_module,
        "_rename_no_replace_at",
        claim_and_repopulate,
    )
    monkeypatch.setattr(locking_module, "_fsync_directory", fail_first_sync)

    with pytest.raises(
        UnsafeFilesystemError, match="concurrently repopulated"
    ) as raised:
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert "claim rename fsync failed" in _exception_text(raised.value)
    assert path.exists()
    assert list(path.parent.glob(".source-write.lock.quarantine-*"))


def test_recovery_revalidates_replaced_entry_before_removal(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    successor = LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()
    replaced = False

    def replace_before_recovery(_pid: int) -> bool:
        nonlocal replaced
        if not replaced:
            replacement = path.with_name("replacement")
            replacement.write_bytes(successor)
            replacement.replace(path)
            replaced = True
        return False

    monkeypatch.setattr(locking_module, "process_is_alive", replace_before_recovery)

    with pytest.raises(LockHeldError):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert path.read_bytes() == successor


def test_recovery_fails_closed_if_parent_anchor_is_swapped(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    write_lock(path, pid=123, started_at=FIXED_OLD_TIME)
    successor = LockMetadata(os.getpid(), socket.gethostname(), FIXED_NOW).to_bytes()

    def swap_parent(_pid: int) -> bool:
        path.parent.rename(repo_root / ".brain-old")
        path.parent.mkdir()
        path.write_bytes(successor)
        return False

    monkeypatch.setattr(locking_module, "process_is_alive", swap_parent)

    with pytest.raises(UnsafeFilesystemError):
        SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert path.read_bytes() == successor
    assert (repo_root / ".brain-old" / path.name).exists()


def test_release_refuses_tampered_metadata(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    tampered = LockMetadata(lock.metadata.pid, "other-host", FIXED_NOW).to_bytes()
    path.write_bytes(tampered)

    assert lock.release() is False
    assert path.read_bytes() == tampered


def test_release_refuses_replacement_even_with_equal_metadata(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    successor = path.with_name("successor")
    successor.write_bytes(lock.metadata.to_bytes())
    successor.replace(path)

    assert lock.release() is False
    assert path.read_bytes() == lock.metadata.to_bytes()


def test_release_is_idempotent_after_successful_removal(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    assert lock.release() is True
    assert lock.release() is False
    assert not path.exists()


@pytest.mark.parametrize("entry_change", ("missing", "tampered", "successor"))
def test_context_exit_treats_unproved_release_as_nonclean(
    repo_root: Path,
    entry_change: str,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    if entry_change == "missing":
        path.unlink()
    elif entry_change == "tampered":
        path.write_bytes(
            LockMetadata(lock.metadata.pid, "other-host", FIXED_NOW).to_bytes()
        )
    else:
        successor = path.with_name("successor")
        successor.write_bytes(lock.metadata.to_bytes())
        successor.replace(path)

    with pytest.raises(LockCleanupError, match="could not prove release"):
        with lock:
            pass


@pytest.mark.parametrize("entry_change", ("missing", "tampered", "successor"))
def test_context_exit_preserves_body_exception_when_release_is_unproved(
    repo_root: Path,
    entry_change: str,
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)
    body_error = RuntimeError("body failed")
    if entry_change == "missing":
        path.unlink()
    elif entry_change == "tampered":
        path.write_bytes(
            LockMetadata(lock.metadata.pid, "other-host", FIXED_NOW).to_bytes()
        )
    else:
        successor = path.with_name("successor")
        successor.write_bytes(lock.metadata.to_bytes())
        successor.replace(path)

    with pytest.raises(RuntimeError, match="body failed") as raised:
        with lock:
            raise body_error
    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "Source write lock cleanup could not prove release."
    ]


def test_context_exit_accepts_already_proven_manual_release(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock

    with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW) as lock:
        assert lock.release() is True

    assert not path.exists()


def test_context_exit_preserves_body_exception_when_release_cannot_validate_parent(
    repo_root: Path,
) -> None:
    path = RepoPaths.discover(repo_root).lock

    body_error = RuntimeError("body failed")
    with pytest.raises(RuntimeError, match="body failed") as raised:
        with SourceWriteLock.acquire(path, now=lambda: FIXED_NOW):
            path.parent.rename(repo_root / ".brain-old")
            path.parent.mkdir()
            raise body_error

    assert raised.value is body_error
    assert raised.value.__notes__ == [
        "Source write lock cleanup failed: UnsafeFilesystemError: "
        "'lock parent changed since this lock was acquired'"
    ]
    assert (repo_root / ".brain-old" / path.name).exists()


def test_context_exit_sanitizes_cleanup_failure_note(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    body_error = RuntimeError("body")
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    def fail_parent(*_args: object, **_kwargs: object) -> object:
        raise OSError("cleanup\nforged\x1b[31m")

    monkeypatch.setattr(locking_module, "_open_lock_parent", fail_parent)
    with pytest.raises(RuntimeError) as raised:
        with lock:
            raise body_error

    assert raised.value is body_error
    note = raised.value.__notes__[0]
    assert "cleanup\\nforged\\x1b[31m" in note
    assert "\n" not in note
    assert "\x1b" not in note


def test_context_exit_without_body_error_propagates_release_failure(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = RepoPaths.discover(repo_root).lock
    lock = SourceWriteLock.acquire(path, now=lambda: FIXED_NOW)

    def fail_parent(*_args: object, **_kwargs: object) -> object:
        raise OSError("cleanup failed")

    monkeypatch.setattr(locking_module, "_open_lock_parent", fail_parent)
    with pytest.raises(OSError, match="cleanup failed"):
        with lock:
            pass


def test_known_owner_error_escapes_hostile_control_characters() -> None:
    owner = LockMetadata(123, "host\nforged\x1b[31m", FIXED_NOW)

    message = str(LockHeldError(Path("/ignored"), owner))

    assert "host\\nforged\\x1b[31m" in message
    assert "\n" not in message
    assert "\x1b" not in message


@pytest.mark.parametrize(
    ("error_number", "expected"),
    [(errno.ESRCH, False), (errno.EPERM, True), (errno.EIO, None)],
)
def test_process_is_alive_maps_raw_os_kill_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    expected: bool | None,
) -> None:
    def fail(_pid: int, _signal: int) -> None:
        raise OSError(error_number, "process check failed")

    monkeypatch.setattr(locking_module.os, "kill", fail)

    assert locking_module.process_is_alive(123) is expected
