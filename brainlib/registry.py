from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, Self


_AGENT_REVISION_RE = re.compile(r"[1-9][0-9]*")
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_EXPECTED_ANCHORS = frozenset(
    {"line", "page", "slide", "sheet", "section", "row", "block"}
)
_ROOT_KEYS = frozenset({"schema_version", "extractors"})
_EXTRACTOR_KEYS = frozenset(
    {
        "id",
        "version",
        "timeout_seconds",
        "max_output_bytes",
        "mimes",
        "extensions",
        "mode",
        "output_suffix",
        "anchors",
        "agent_fallback",
        "agent_revision",
        "preferred",
        "fallbacks",
    }
)
_CONVERTER_KEYS = frozenset(
    {
        "id",
        "executable",
        "argv",
        "version_args",
        "python_distribution",
        "install",
    }
)
_INSTALL_KEYS = frozenset({"macos", "linux", "windows"})
_BUILTIN_CONVERTER_IDS = frozenset(
    {
        "builtin.text",
        "builtin.tabular",
        "builtin.json",
        "builtin.html",
        "builtin.http-capture",
    }
)
_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"})
_SHELL_FLAGS = frozenset({"-c", "/c", "-command", "/command"})
_SHELL_FRAGMENTS = ("|", ">", "<", ";", "&", "`", "$(")
_VERSION_TIMEOUT_SECONDS = 5
_VERSION_MAX_OUTPUT_BYTES = 16_384
_VERSION_CLEANUP_RESERVE_SECONDS = 0.25
_VERSION_WAIT_POLL_SECONDS = 0.01
_WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_LIMIT_KILL_ON_CLOSE = 0x00002000
_WINDOWS_CREATE_SUSPENDED = 0x00000004


def is_normalized_identifier(value: object) -> bool:
    """Return whether value uses the invariant registry identifier grammar."""

    return type(value) is str and _IDENTIFIER_RE.fullmatch(value) is not None


def is_committed_builtin_converter_id(value: object) -> bool:
    """Return whether value names a permanent built-in converter producer."""

    return type(value) is str and value in _BUILTIN_CONVERTER_IDS


class ExecutionMode(StrEnum):
    PYTHON = "python"
    COMMAND = "command"
    WEB_CAPTURE = "web_capture"


class RunVersionProbe(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> str: ...


class _WindowsJobApi(Protocol):
    def create_job(self) -> int: ...

    def configure_kill_on_close(self, job_handle: int) -> None: ...

    def assign_process(self, job_handle: int, process_handle: int) -> None: ...

    def resume_process(self, process_handle: int) -> None: ...

    def close_handle(self, job_handle: int) -> None: ...


class _ProbeTree(Protocol):
    popen_options: Mapping[str, object]
    preserve_leader_identity: bool

    def attach(self, process: subprocess.Popen[bytes]) -> None: ...

    def terminate_tree(self) -> None: ...


class _PosixProbeTree:
    popen_options: Mapping[str, object] = MappingProxyType({"start_new_session": True})
    preserve_leader_identity = True

    def __init__(self) -> None:
        self._process_group_id: int | None = None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self._process_group_id = process.pid

    def terminate_tree(self) -> None:
        process_group_id = self._process_group_id
        self._process_group_id = None
        if process_group_id is None:
            return
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except OSError:
            pass


class _DirectProbeTree:
    popen_options: Mapping[str, object] = MappingProxyType({})
    preserve_leader_identity = False

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        del process

    def terminate_tree(self) -> None:
        return


class _WindowsProbeTree:
    preserve_leader_identity = False

    def __init__(
        self,
        api: _WindowsJobApi,
        job_handle: int,
        *,
        creation_flags: int,
    ) -> None:
        self._api = api
        self._job_handle: int | None = job_handle
        self.popen_options: Mapping[str, object] = MappingProxyType(
            {"creationflags": creation_flags}
        )

    @classmethod
    def create(
        cls,
        api: _WindowsJobApi,
        *,
        creation_flags: int,
    ) -> _WindowsProbeTree:
        job_handle = api.create_job()
        try:
            api.configure_kill_on_close(job_handle)
        except BaseException:
            try:
                api.close_handle(job_handle)
            except BaseException:
                pass
            raise
        return cls(api, job_handle, creation_flags=creation_flags)

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise OSError("Windows probe process handle is unavailable")
        assert self._job_handle is not None
        self._api.assign_process(self._job_handle, int(process_handle))
        self._api.resume_process(int(process_handle))

    def terminate_tree(self) -> None:
        job_handle = self._job_handle
        self._job_handle = None
        if job_handle is not None:
            self._api.close_handle(job_handle)


class _CtypesWindowsJobApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll")
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self._ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self._ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    def create_job(self) -> int:
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            self._raise_last_error()
        return int(handle)

    def configure_kill_on_close(self, job_handle: int) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_LIMIT_KILL_ON_CLOSE
        if not self._kernel32.SetInformationJobObject(
            job_handle,
            _WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self._raise_last_error()

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
            self._raise_last_error()

    def resume_process(self, process_handle: int) -> None:
        status = self._ntdll.NtResumeProcess(process_handle)
        if status < 0:
            error_code = self._ntdll.RtlNtStatusToDosError(status)
            raise self._ctypes.WinError(error_code)

    def close_handle(self, job_handle: int) -> None:
        if not self._kernel32.CloseHandle(job_handle):
            self._raise_last_error()

    def _raise_last_error(self) -> None:
        raise self._ctypes.WinError(self._ctypes.get_last_error())


@dataclass(frozen=True)
class ConverterSpec:
    converter_id: str
    executable: str | None
    argv_template: tuple[str, ...]
    version_args: tuple[str, ...]
    python_distribution: str | None
    install_recipes: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv_template", tuple(self.argv_template))
        object.__setattr__(self, "version_args", tuple(self.version_args))
        object.__setattr__(
            self,
            "install_recipes",
            MappingProxyType(
                {
                    platform: tuple(arguments)
                    for platform, arguments in self.install_recipes.items()
                }
            ),
        )
        _validate_converter(self)


@dataclass(frozen=True)
class ResolvedConverter:
    converter: ConverterSpec
    detected_version: str
    prerequisite_digest: str


@dataclass(frozen=True)
class ExtractorSpec:
    extractor_id: str
    extractor_version: str
    timeout_seconds: int
    max_output_bytes: int
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    mode: ExecutionMode
    output_suffix: str
    expected_anchors: tuple[str, ...]
    agent_fallback: bool
    agent_revision: str | None
    preferred: ConverterSpec
    fallbacks: tuple[ConverterSpec, ...]
    config_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "media_types", tuple(self.media_types))
        object.__setattr__(self, "extensions", tuple(self.extensions))
        object.__setattr__(self, "expected_anchors", tuple(self.expected_anchors))
        object.__setattr__(self, "fallbacks", tuple(self.fallbacks))


@dataclass(frozen=True)
class ExtractorRegistry:
    schema_version: int
    extractors: tuple[ExtractorSpec, ...]
    config_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "extractors", tuple(self.extractors))

    @classmethod
    def load(cls, path: Path) -> Self:
        with path.open("rb") as source:
            document = tomllib.load(source)
        _reject_unknown_keys(document, _ROOT_KEYS, "registry")

        raw_extractors = _array(document.get("extractors"), "extractors")
        extractors = tuple(
            _parse_extractor(raw, index=index)
            for index, raw in enumerate(raw_extractors)
        )
        schema_version = _integer(document.get("schema_version"), "schema_version")
        if schema_version != 1:
            raise ValueError("schema_version must be 1")
        _validate_unique_registry_entries(extractors)
        return cls(
            schema_version=schema_version,
            extractors=extractors,
            config_sha256=_canonical_sha256(document),
        )

    def select(
        self,
        detected_media_type: str | None,
        path: str | Path,
    ) -> ExtractorSpec | None:
        if detected_media_type is not None:
            for extractor in self.extractors:
                if detected_media_type in extractor.media_types:
                    return extractor

        name = str(path).lower()
        candidates = (
            (len(extension), index, extractor)
            for index, extractor in enumerate(self.extractors)
            for extension in extractor.extensions
            if name.endswith(extension)
        )
        return next(
            (
                extractor
                for _length, _index, extractor in sorted(
                    candidates, key=lambda item: (-item[0], item[1])
                )
            ),
            None,
        )


def build_converter_argv(
    converter: ConverterSpec,
    *,
    input_path: Path,
    output_path: Path,
) -> tuple[str, ...]:
    substitutions = {"{input}": str(input_path), "{output}": str(output_path)}
    rendered = tuple(
        substitutions.get(token, token) for token in converter.argv_template
    )
    if converter.executable is None:
        return rendered
    return (converter.executable, *rendered)


def detect_converter_version(
    converter: ConverterSpec,
    *,
    extractor_version: str,
    run: RunVersionProbe | None = None,
) -> str:
    if is_committed_builtin_converter_id(converter.converter_id):
        return f"builtin:{converter.converter_id}:{extractor_version}"
    if converter.executable is not None:
        runner = _run_version_probe if run is None else run
        output = runner(
            (converter.executable, *converter.version_args),
            timeout_seconds=_VERSION_TIMEOUT_SECONDS,
            max_output_bytes=_VERSION_MAX_OUTPUT_BYTES,
        )
        return _normalize_detected_version(output)
    assert converter.python_distribution is not None
    return _normalize_detected_version(metadata.version(converter.python_distribution))


def resolve_converter(
    extractor: ExtractorSpec,
    *,
    run: RunVersionProbe | None = None,
) -> ResolvedConverter | None:
    for converter in (extractor.preferred, *extractor.fallbacks):
        try:
            detected_version = detect_converter_version(
                converter,
                extractor_version=extractor.extractor_version,
                run=run,
            )
        except (
            _ConverterUnavailable,
            metadata.PackageNotFoundError,
            OSError,
            subprocess.SubprocessError,
        ):
            continue
        digest = hashlib.sha256(
            (
                "converter-v1\0" + converter.converter_id + "\0" + detected_version
            ).encode("utf-8")
        ).hexdigest()
        return ResolvedConverter(converter, detected_version, digest)
    return None


def prerequisite_digest(
    extractor: ExtractorSpec,
    *,
    run: RunVersionProbe | None = None,
) -> str:
    resolved = resolve_converter(extractor, run=run)
    if resolved is not None:
        return resolved.prerequisite_digest
    return hashlib.sha256(
        f"converter-v1\0unavailable\0{extractor.extractor_id}".encode("utf-8")
    ).hexdigest()


def effective_extractor_version(extractor: ExtractorSpec, digest: str) -> str:
    return f"{extractor.extractor_version}+{digest}"


class _ConverterUnavailable(RuntimeError):
    pass


def _run_version_probe(
    argv: tuple[str, ...],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    # The reader itself stops after max_output_bytes + 1, so the queue can never
    # retain more than that bounded payload. Leaving it unbackpressured also
    # lets the reader terminate while the main thread tears down the probe tree.
    events: queue.Queue[bytes | BaseException | None] = queue.Queue()
    cancelled = threading.Event()
    output = bytearray()
    reader: threading.Thread | None = None
    leader_exited = False
    process_reaped = False
    return_code: int | None = None
    stdout = None
    probe_tree = _create_probe_tree()
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            **probe_tree.popen_options,
        )
    except BaseException:
        probe_tree.terminate_tree()
        raise
    try:
        stdout = process.stdout
        if stdout is None:
            raise _ConverterUnavailable("converter version output pipe is missing")
        probe_tree.attach(process)
        reader = threading.Thread(
            target=_read_bounded_probe_output,
            args=(
                stdout.fileno(),
                max_output_bytes + 1,
                events,
                cancelled,
            ),
            daemon=True,
        )
        reader.start()

        while True:
            remaining = _remaining_probe_work_time(deadline)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
            try:
                event = events.get(timeout=remaining)
            except queue.Empty as error:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds) from error
            if event is None:
                break
            if isinstance(event, BaseException):
                raise _ConverterUnavailable(
                    "converter version output read failed"
                ) from event
            output.extend(event)
            if len(output) > max_output_bytes:
                raise _ConverterUnavailable("converter version output exceeded limit")

        remaining = _remaining_probe_work_time(deadline)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        if probe_tree.preserve_leader_identity:
            return_code = _wait_for_posix_leader_without_reaping(
                process,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        else:
            return_code = process.wait(timeout=remaining)
            process_reaped = True
        leader_exited = True
    finally:
        cancelled.set()
        try:
            probe_tree.terminate_tree()
        finally:
            try:
                if not leader_exited and not process_reaped:
                    try:
                        process.kill()
                    except OSError:
                        pass
            finally:
                try:
                    if stdout is not None:
                        stdout.close()
                finally:
                    try:
                        if not process_reaped:
                            reaped_return_code = _bounded_wait_or_reap_later(
                                process,
                                deadline=deadline,
                            )
                            if return_code is None:
                                return_code = reaped_return_code
                    finally:
                        if reader is not None and reader.ident is not None:
                            reader.join(timeout=max(0.0, deadline - time.monotonic()))

    assert return_code is not None
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code, list(argv), output=bytes(output)
        )
    return bytes(output).decode("utf-8", errors="replace")


def _read_bounded_probe_output(
    file_descriptor: int,
    byte_limit: int,
    events: queue.Queue[bytes | BaseException | None],
    cancelled: threading.Event,
) -> None:
    remaining = byte_limit
    try:
        while remaining and not cancelled.is_set():
            try:
                chunk = os.read(file_descriptor, min(4096, remaining))
            except InterruptedError:
                continue
            if not chunk:
                break
            events.put(chunk)
            remaining -= len(chunk)
    except BaseException as error:
        events.put(error)
    finally:
        events.put(None)


def _remaining_probe_work_time(deadline: float) -> float:
    return deadline - time.monotonic() - _VERSION_CLEANUP_RESERVE_SECONDS


def _create_probe_tree() -> _ProbeTree:
    if os.name == "posix":
        return _PosixProbeTree()
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | _WINDOWS_CREATE_SUSPENDED
        )
        return _WindowsProbeTree.create(
            _CtypesWindowsJobApi(),
            creation_flags=creation_flags,
        )
    return _DirectProbeTree()


def _wait_for_posix_leader_without_reaping(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: int,
    deadline: float,
) -> int:
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        remaining = _remaining_probe_work_time(deadline)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
        try:
            result = os.waitid(os.P_PID, process.pid, flags)
        except InterruptedError:
            continue
        if result is not None:
            if result.si_code == os.CLD_EXITED:
                return result.si_status
            if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                return -result.si_status
            raise _ConverterUnavailable("converter process exit status is invalid")
        time.sleep(min(_VERSION_WAIT_POLL_SECONDS, remaining))


def _bounded_wait_or_reap_later(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> int | None:
    try:
        return process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        reaper = threading.Thread(
            target=_reap_probe_process,
            args=(process,),
            daemon=True,
            name="converter-version-probe-reaper",
        )
        reaper.start()
        return None


def _reap_probe_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except BaseException:
        return


def _normalize_detected_version(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise _ConverterUnavailable("converter version output is invalid")
    normalized = " ".join(value.split())
    if not normalized:
        raise _ConverterUnavailable("converter version output is empty")
    return normalized


def _parse_extractor(value: object, *, index: int) -> ExtractorSpec:
    raw = _object(value, f"extractors[{index}]")
    _reject_unknown_keys(raw, _EXTRACTOR_KEYS, f"extractors[{index}]")

    extractor_id = _identifier(raw.get("id"), f"extractors[{index}].id")
    extractor_version = _nonempty_string(
        raw.get("version"), f"extractor {extractor_id} version"
    )
    preferred = _parse_converter(
        raw.get("preferred"), context=f"extractor {extractor_id} preferred"
    )
    fallbacks = tuple(
        _parse_converter(
            item, context=f"extractor {extractor_id} fallback[{item_index}]"
        )
        for item_index, item in enumerate(
            _array(raw.get("fallbacks", []), f"extractor {extractor_id} fallbacks")
        )
    )

    agent_fallback = _boolean(
        raw.get("agent_fallback"), f"extractor {extractor_id} agent_fallback"
    )
    agent_revision_value = raw.get("agent_revision")
    agent_revision: str | None
    if agent_fallback:
        if (
            type(agent_revision_value) is not str
            or _AGENT_REVISION_RE.fullmatch(agent_revision_value) is None
        ):
            raise ValueError(
                f"extractor {extractor_id} agent_revision must match [1-9][0-9]* "
                "when agent_fallback is true"
            )
        agent_revision = agent_revision_value
    else:
        if agent_revision_value is not None:
            raise ValueError(
                f"extractor {extractor_id} agent_revision is forbidden when "
                "agent_fallback is false"
            )
        agent_revision = None

    timeout_seconds = _positive_integer(
        raw.get("timeout_seconds"), f"extractor {extractor_id} timeout_seconds"
    )
    max_output_bytes = _positive_integer(
        raw.get("max_output_bytes"), f"extractor {extractor_id} max_output_bytes"
    )
    media_types = _nonempty_string_tuple(
        raw.get("mimes"), f"extractor {extractor_id} mimes"
    )
    extensions = _nonempty_string_tuple(
        raw.get("extensions"), f"extractor {extractor_id} extensions"
    )
    expected_anchors = _nonempty_string_tuple(
        raw.get("anchors"), f"extractor {extractor_id} anchors"
    )
    output_suffix = _nonempty_string(
        raw.get("output_suffix"), f"extractor {extractor_id} output_suffix"
    )
    mode_value = _nonempty_string(raw.get("mode"), f"extractor {extractor_id} mode")
    try:
        mode = ExecutionMode(mode_value)
    except ValueError as error:
        raise ValueError(
            f"extractor {extractor_id} mode must be one of "
            f"{[item.value for item in ExecutionMode]}"
        ) from error

    if any(
        media_type != media_type.lower() or "/" not in media_type
        for media_type in media_types
    ):
        raise ValueError(
            f"extractor {extractor_id} mimes must be normalized MIME types"
        )
    if any(
        not extension.startswith(".")
        or extension != extension.lower()
        or "/" in extension
        or "\\" in extension
        for extension in extensions
    ):
        raise ValueError(
            f"extractor {extractor_id} extensions must be normalized suffixes"
        )
    if (
        not output_suffix.startswith(".")
        or "/" in output_suffix
        or "\\" in output_suffix
    ):
        raise ValueError(f"extractor {extractor_id} output_suffix must be a suffix")
    if any(anchor not in _EXPECTED_ANCHORS for anchor in expected_anchors):
        raise ValueError(f"extractor {extractor_id} anchors contain an unknown kind")
    _reject_duplicates(media_types, f"extractor {extractor_id} mimes")
    _reject_duplicates(extensions, f"extractor {extractor_id} extensions")
    _reject_duplicates(expected_anchors, f"extractor {extractor_id} anchors")

    return ExtractorSpec(
        extractor_id=extractor_id,
        extractor_version=extractor_version,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        media_types=media_types,
        extensions=extensions,
        mode=mode,
        output_suffix=output_suffix,
        expected_anchors=expected_anchors,
        agent_fallback=agent_fallback,
        agent_revision=agent_revision,
        preferred=preferred,
        fallbacks=fallbacks,
        config_sha256=_canonical_sha256(raw),
    )


def _parse_converter(value: object, *, context: str) -> ConverterSpec:
    raw = _object(value, context)
    _reject_unknown_keys(raw, _CONVERTER_KEYS, context)
    install_raw = _object(raw.get("install", {}), f"{context} install")
    _reject_unknown_keys(install_raw, _INSTALL_KEYS, f"{context} install")
    return ConverterSpec(
        converter_id=_identifier(raw.get("id"), f"{context} id"),
        executable=_optional_nonempty_string(
            raw.get("executable"), f"{context} executable"
        ),
        argv_template=_nonempty_string_tuple(raw.get("argv"), f"{context} argv"),
        version_args=_string_tuple(
            raw.get("version_args", []), f"{context} version_args"
        ),
        python_distribution=_optional_nonempty_string(
            raw.get("python_distribution"), f"{context} python_distribution"
        ),
        install_recipes={
            platform: _nonempty_string_tuple(arguments, f"{context} install.{platform}")
            for platform, arguments in install_raw.items()
        },
    )


def _validate_converter(converter: ConverterSpec) -> None:
    _identifier(converter.converter_id, "converter_id")
    forms = sum(
        item is not None
        for item in (converter.executable, converter.python_distribution)
    )
    builtin = is_committed_builtin_converter_id(converter.converter_id)
    if converter.converter_id.startswith("builtin.") and not builtin:
        raise ValueError(
            f"{converter.converter_id} is not a committed builtin converter"
        )
    if builtin:
        if forms != 0:
            raise ValueError(
                "builtin converters cannot declare an executable or distribution"
            )
        if converter.version_args:
            raise ValueError("builtin converters cannot declare version_args")
    elif forms != 1:
        raise ValueError(
            "non-builtin converters must declare exactly one executable or "
            "python_distribution"
        )
    if converter.executable is not None:
        _nonempty_string(converter.executable, "converter executable")
        if not converter.version_args:
            raise ValueError("executable converters must declare version_args")
    elif converter.version_args:
        raise ValueError("version_args require an executable converter")
    if converter.python_distribution is not None:
        _nonempty_string(converter.python_distribution, "python_distribution")

    _validate_safe_tokens(
        converter.argv_template,
        context="converter argv template",
        allow_placeholders=True,
        executable=converter.executable,
    )
    if "{input}" not in converter.argv_template:
        raise ValueError("converter argv template must contain {input}")
    _validate_safe_tokens(
        converter.version_args,
        context="converter version_args",
        allow_placeholders=False,
        executable=converter.executable,
    )
    for platform, recipe in converter.install_recipes.items():
        if platform not in _INSTALL_KEYS:
            raise ValueError(f"unknown install platform: {platform}")
        if not recipe:
            raise ValueError("install recipes must not be empty")
        for token in recipe:
            _nonempty_string(token, f"{platform} install token")


def _validate_safe_tokens(
    tokens: tuple[str, ...],
    *,
    context: str,
    allow_placeholders: bool,
    executable: str | None,
) -> None:
    executable_name = "" if executable is None else Path(executable).name.lower()
    executable_name = executable_name.removesuffix(".exe")
    if executable_name in _SHELL_PROGRAMS:
        raise ValueError(f"{context} cannot invoke a shell")
    for token in tokens:
        _nonempty_string(token, context)
        lower = token.lower()
        program = Path(lower).name.removesuffix(".exe")
        if program in _SHELL_PROGRAMS or lower in _SHELL_FLAGS:
            raise ValueError(f"{context} cannot contain shell invocation syntax")
        if any(fragment in token for fragment in _SHELL_FRAGMENTS):
            raise ValueError(f"{context} cannot contain shell syntax")
        placeholders = _PLACEHOLDER_RE.findall(token)
        if placeholders:
            if not allow_placeholders or token not in {"{input}", "{output}"}:
                raise ValueError(
                    f"{context} permits only exact {{input}} and {{output}} placeholders; "
                    "shell interpolation is forbidden"
                )
        elif "{" in token or "}" in token:
            raise ValueError(
                f"{context} permits only exact {{input}} and {{output}} placeholders; "
                "shell interpolation is forbidden"
            )


def _validate_unique_registry_entries(extractors: tuple[ExtractorSpec, ...]) -> None:
    _reject_duplicates(tuple(item.extractor_id for item in extractors), "extractor ids")
    media_owners: dict[str, str] = {}
    extension_owners: dict[str, str] = {}
    for extractor in extractors:
        for media_type in extractor.media_types:
            owner = media_owners.setdefault(media_type, extractor.extractor_id)
            if owner != extractor.extractor_id:
                raise ValueError(
                    f"MIME type {media_type} is assigned to both {owner} and "
                    f"{extractor.extractor_id}"
                )
        for extension in extractor.extensions:
            owner = extension_owners.setdefault(extension, extractor.extractor_id)
            if owner != extractor.extractor_id:
                raise ValueError(
                    f"extension {extension} is assigned to both {owner} and "
                    f"{extractor.extractor_id}"
                )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_unknown_keys(
    value: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{context} contains unknown keys: {unknown}")


def _reject_duplicates(values: tuple[str, ...], context: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{context} must not contain duplicates")


def _object(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{context} must be a table")
    return value


def _array(value: object, context: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{context} must be an array")
    return value


def _nonempty_string(value: object, context: str) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError(f"{context} must be a nonempty NUL-free string")
    return value


def _optional_nonempty_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, context)


def _identifier(value: object, context: str) -> str:
    identifier = _nonempty_string(value, context)
    if not is_normalized_identifier(identifier):
        raise ValueError(f"{context} must be a normalized identifier")
    return identifier


def _integer(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{context} must be an integer")
    return value


def _positive_integer(value: object, context: str) -> int:
    integer = _integer(value, context)
    if integer <= 0:
        raise ValueError(f"{context} must be positive")
    return integer


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    return tuple(_nonempty_string(item, context) for item in _array(value, context))


def _nonempty_string_tuple(value: object, context: str) -> tuple[str, ...]:
    items = _string_tuple(value, context)
    if not items:
        raise ValueError(f"{context} must not be empty")
    return items
