from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Protocol, Self, TypeAlias, runtime_checkable

from ..contracts import SourceRecord, SourceState
from ..diagnostics import Diagnostic
from ..inventory import (
    InventoryAccessError,
    InventoryItem,
    SnapshotNamespace,
    validate_snapshot_path,
)
from ..layout import RepoPaths
from ..registry import (
    ExtractorSpec,
    ResolvedConverter,
    effective_extractor_version,
    resolve_converter,
)
from ..sync import (
    ProcessResult,
    ProcessingContext,
    SourceProcessor,
    UnavailableProcessor,
)


@dataclass(frozen=True)
class CommandExecution:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class RunCommand(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
        max_output_bytes: int,
        pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution: ...


ResolveConverter: TypeAlias = Callable[[ExtractorSpec], ResolvedConverter | None]


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _descriptor_sha256(descriptor: int, byte_size: int) -> str:
    checksum = hashlib.sha256()
    offset = 0
    while offset <= byte_size:
        chunk = os.pread(descriptor, min(1024 * 1024, byte_size + 1 - offset), offset)
        if not chunk:
            break
        checksum.update(chunk)
        offset += len(chunk)
    if offset != byte_size:
        raise InventoryAccessError("staged input byte size changed")
    return checksum.hexdigest()


@dataclass
class StagedInput(AbstractContextManager["StagedInput"]):
    """Owned, descriptor-pinned copy of the already verified Plan 2 input."""

    logical_path: PurePosixPath
    sha256: str
    byte_size: int
    descriptor: int
    descriptor_path: Path
    _stage_path: Path | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        self._closed = False
        self._identity = _identity(os.fstat(self.descriptor))
        self._directory_descriptor: int | None = None
        self._directory_identity: tuple[int, int] | None = None
        if self._stage_path is not None:
            self._directory_descriptor = os.open(
                self._stage_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            observed = os.fstat(self._directory_descriptor)
            self._directory_identity = (observed.st_dev, observed.st_ino)

    def revalidate(self) -> None:
        """Reject descriptor, published stage, or private-directory replacement."""

        try:
            self._validate_identity()
            if _descriptor_sha256(self.descriptor, self.byte_size) != self.sha256:
                raise InventoryAccessError("staged input checksum changed")
            self._validate_identity()
        except OSError as error:
            raise InventoryAccessError("staged input could not be verified") from error

    def _validate_identity(self) -> None:
        if self._closed:
            raise InventoryAccessError("staged input is closed")
        observed = os.fstat(self.descriptor)
        if not stat.S_ISREG(observed.st_mode) or _identity(observed) != self._identity:
            raise InventoryAccessError("staged input identity changed")
        if self._stage_path is not None:
            parent = self._stage_path.parent.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or (parent.st_dev, parent.st_ino) != self._directory_identity
                or parent.st_mode & 0o777 != 0o700
            ):
                raise InventoryAccessError("staged input directory changed")
            entry = os.stat(
                self._stage_path.name,
                dir_fd=self._directory_descriptor,
                follow_symlinks=False,
            )
            if _identity(entry) != self._identity:
                raise InventoryAccessError("staged input publication changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.descriptor)
        finally:
            if self._directory_descriptor is not None:
                try:
                    assert self._stage_path is not None
                    try:
                        os.unlink(
                            self._stage_path.name, dir_fd=self._directory_descriptor
                        )
                    except FileNotFoundError:
                        pass
                    try:
                        parent = self._stage_path.parent.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        if (parent.st_dev, parent.st_ino) == self._directory_identity:
                            self._stage_path.parent.rmdir()
                finally:
                    os.close(self._directory_descriptor)

    def __enter__(self) -> Self:
        self.revalidate()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def stage_pinned_input(
    context: ProcessingContext,
    *,
    paths: RepoPaths,
    logical_path: PurePosixPath,
    expected_byte_size: int,
    namespace: SnapshotNamespace = SnapshotNamespace.RAW_USER,
) -> StagedInput:
    """Copy only the caller's live descriptor; never reopen a logical raw path."""

    if namespace not in (SnapshotNamespace.RAW_USER, SnapshotNamespace.RAW_WEB):
        raise ValueError("staging namespace must be RAW_USER or RAW_WEB")
    validate_snapshot_path(namespace, logical_path)
    if (
        type(context.input_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", context.input_sha256) is None
    ):
        raise ValueError("input_sha256 must be a canonical sha256")
    if type(expected_byte_size) is not int or expected_byte_size < 0:
        raise ValueError("expected byte size must be a nonnegative integer")
    before = os.fstat(context.input_descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_size != expected_byte_size:
        raise InventoryAccessError("pinned input size does not match inventory")
    directory = Path(tempfile.mkdtemp(prefix=".brain-stage-", dir=paths.root))
    temporary = directory / "copy"
    published = directory / context.input_sha256
    descriptor: int | None = None
    staged: StagedInput | None = None
    try:
        os.chmod(directory, 0o700)
        checksum = hashlib.sha256()
        offset = 0
        with temporary.open("xb") as output:
            while offset <= expected_byte_size:
                chunk = os.pread(
                    context.input_descriptor,
                    min(1024 * 1024, expected_byte_size + 1 - offset),
                    offset,
                )
                if not chunk:
                    break
                output.write(chunk)
                checksum.update(chunk)
                offset += len(chunk)
            output.flush()
            os.fchmod(output.fileno(), 0o400)
            os.fsync(output.fileno())
        if (
            offset != expected_byte_size
            or checksum.hexdigest() != context.input_sha256
            or _identity(os.fstat(context.input_descriptor)) != _identity(before)
        ):
            raise InventoryAccessError("pinned input changed while staging")
        os.replace(temporary, published)
        directory_descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        descriptor = os.open(published, os.O_RDONLY | os.O_NOFOLLOW)
        staged = StagedInput(
            logical_path,
            context.input_sha256,
            expected_byte_size,
            descriptor,
            Path("/dev/fd") / str(descriptor),
            _stage_path=published,
        )
        staged.revalidate()
        return staged
    except BaseException:
        if staged is not None:
            staged.close()
        else:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            published.unlink(missing_ok=True)
            directory.rmdir()
        raise


@dataclass(frozen=True)
class Job:
    record: SourceRecord
    item: InventoryItem
    extractor: ExtractorSpec
    context: ProcessingContext
    staged_input: StagedInput


_worker_stage: ContextVar[StagedInput | None] = ContextVar(
    "extractor_worker_stage", default=None
)


@contextmanager
def borrowed_staged_input(staged: StagedInput):
    """Lend an owned stage to a processor without changing its lifetime."""
    if not isinstance(staged, StagedInput):
        raise ValueError("borrowed input must be a live StagedInput")
    staged.revalidate()
    token = _worker_stage.set(staged)
    try:
        yield
    finally:
        try:
            staged.revalidate()
        finally:
            _worker_stage.reset(token)


@runtime_checkable
class BatchSourceProcessor(SourceProcessor, Protocol):
    def iter_batch(
        self,
        jobs: Iterable[Job],
        *,
        paths: RepoPaths,
        max_workers: int | None = None,
    ) -> Iterator[tuple[str, ProcessResult]]: ...


def validate_max_workers(max_workers: int | None) -> int:
    if max_workers is None:
        return min(4, os.cpu_count() or 1)
    if type(max_workers) is not int or not 1 <= max_workers <= 16:
        raise ValueError("max_workers must be an integer in 1..16")
    return max_workers


class DeterministicSourceProcessor:
    def __init__(
        self,
        *,
        run: RunCommand | None = None,
        resolve: ResolveConverter | None = None,
    ) -> None:
        self._run = run
        self._resolve = resolve_converter if resolve is None else resolve

    def process(
        self,
        record: SourceRecord,
        item: InventoryItem,
        extractor: ExtractorSpec,
        *,
        paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        with tempfile.TemporaryDirectory(
            prefix=f".brain-tmp-{record.source_id[:12]}-",
            dir=paths.root,
        ) as directory:
            resolved = self._resolve(extractor)
            unavailable = UnavailableProcessor().process(
                record,
                item,
                extractor,
                paths=paths,
                context=context,
            )
            if resolved is None:
                if extractor.agent_fallback:
                    return replace(
                        unavailable,
                        state=SourceState.NEEDS_AGENT,
                        attempt=replace(
                            unavailable.attempt, outcome=SourceState.NEEDS_AGENT
                        ),
                    )
                return unavailable
            if (
                resolved.prerequisite_digest != context.prerequisite_digest
                or context.extractor_version
                != effective_extractor_version(extractor, resolved.prerequisite_digest)
                or context.config_sha256 != extractor.config_sha256
            ):
                diagnostic = Diagnostic(
                    "prerequisite_changed",
                    "The converter or recipe changed after reconciliation; retry with current prerequisites.",
                )
                return replace(
                    unavailable,
                    diagnostics=(diagnostic,),
                    attempt=replace(
                        unavailable.attempt, diagnostic_codes=(diagnostic.code,)
                    ),
                )
            from .adapters import run_command, run_job

            with ExitStack() as owned:
                staged = _worker_stage.get()
                if staged is None:
                    staged = owned.enter_context(
                        stage_pinned_input(
                            context,
                            paths=paths,
                            logical_path=item.fingerprint.path,
                            expected_byte_size=item.fingerprint.byte_size,
                        )
                    )
                staged.revalidate()
                staged_context = replace(
                    context,
                    input_descriptor=staged.descriptor,
                    input_path=staged.descriptor_path,
                )
                return run_job(
                    Job(record, item, extractor, staged_context, staged),
                    paths=paths,
                    run=run_command if self._run is None else self._run,
                    resolved=resolved,
                    staging_dir=Path(directory),
                )

    def _process_staged_job(self, job: Job, *, paths: RepoPaths) -> ProcessResult:
        with borrowed_staged_input(job.staged_input):
            return self.process(
                job.record,
                job.item,
                job.extractor,
                paths=paths,
                context=job.context,
            )

    def iter_batch(
        self,
        jobs: Iterable[Job],
        *,
        paths: RepoPaths,
        max_workers: int | None = None,
    ) -> Iterator[tuple[str, ProcessResult]]:
        worker_count = validate_max_workers(max_workers)
        source = iter(jobs)
        futures: dict[Future[ProcessResult], Job] = {}
        executor = ThreadPoolExecutor(max_workers=worker_count)
        exhausted = False
        try:
            while True:
                while not exhausted and len(futures) < worker_count:
                    try:
                        job = next(source)
                    except StopIteration:
                        exhausted = True
                        break
                    try:
                        job.staged_input.revalidate()
                        if (
                            job.context.input_descriptor != job.staged_input.descriptor
                            or job.context.input_path
                            != job.staged_input.descriptor_path
                            or job.context.input_sha256 != job.staged_input.sha256
                            or job.item.fingerprint.byte_size
                            != job.staged_input.byte_size
                            or job.item.fingerprint.path
                            != job.staged_input.logical_path
                        ):
                            raise InventoryAccessError(
                                "job does not own its staged input"
                            )
                        future = executor.submit(
                            self._process_staged_job,
                            job,
                            paths=paths,
                        )
                        futures[future] = job
                    except BaseException:
                        job.staged_input.close()
                        raise
                if not futures:
                    break
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in sorted(
                    completed, key=lambda value: futures[value].record.source_id
                ):
                    job = futures.pop(future)
                    try:
                        result = future.result()
                        job.staged_input.revalidate()
                        yield job.record.source_id, result
                    finally:
                        job.staged_input.close()
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            for job in futures.values():
                job.staged_input.close()
            close = getattr(source, "close", None)
            if close is not None:
                close()


def build_source_processor() -> SourceProcessor:
    return DeterministicSourceProcessor()
