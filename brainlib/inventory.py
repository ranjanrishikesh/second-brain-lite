from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import stat
import struct
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeVar
from urllib.parse import urlsplit

from .contracts import FileFingerprint, compute_sha256, source_id_for_first_seen
from .diagnostics import Diagnostic
from .layout import RepoPaths, is_ignored_source_path


_URL_DESCRIPTOR_MEDIA_TYPE = "application/x.second-brain-url-descriptor"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PPTX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SNIFF_BYTES = 65_536
_MAX_DESCRIPTOR_BYTES = 65_536
_MAX_ZIP_TAIL_BYTES = 65_557
_MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 131_072
_MAX_ZIP_LOCAL_METADATA_BYTES = 131_072
_MAX_ZIP_MEMBERS = 4_096
_MAX_ZIP_MEMBER_NAME_BYTES = 32_768
_SUPPORTED_ZIP_FLAGS = 0x0800
_SUPPORTED_ZIP_METHODS = frozenset({0, 8})
_DESCRIPTOR_KEYS = frozenset({"kind", "url", "description", "added"})
_FIELD_LINE_RE = re.compile(r"^([a-z][a-z0-9_-]*): (.*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{64}$")
_DERIVATION_FILE_RE = re.compile(r"^drv_[0-9a-f]{64}\..+$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_TEXT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".ndjson": "application/x-ndjson",
    ".tsv": "text/tab-separated-values",
    ".xhtml": "application/xhtml+xml",
}
_EXTENSION_MEDIA_TYPES = {
    **_TEXT_MEDIA_TYPES,
    ".docx": _DOCX_MEDIA_TYPE,
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".pptx": _PPTX_MEDIA_TYPE,
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".xlsx": _XLSX_MEDIA_TYPE,
}
_KNOWN_EXTENSIONS = tuple(
    sorted((*_EXTENSION_MEDIA_TYPES, ".url.md"), key=len, reverse=True)
)
_OPEN_SUPPORT_MARKER = os.open
_STAT_SUPPORT_MARKER = os.stat
_READLINK_SUPPORT_MARKER = os.readlink
_LISTDIR_SUPPORT_MARKER = os.listdir


@dataclass(frozen=True)
class UrlDescriptor:
    path: PurePosixPath
    url: str
    description: str
    added: date


@dataclass(frozen=True)
class InventoryItem:
    fingerprint: FileFingerprint
    media_type: str
    extension: str
    sha256: str | None
    url_descriptor: UrlDescriptor | None = None


@dataclass(frozen=True)
class InventoryReport:
    items: tuple[InventoryItem, ...]
    skipped: tuple[Diagnostic, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "skipped", tuple(self.skipped))


class MediaDetector:
    def detect(self, path: Path) -> str:
        descriptor, observed = _open_direct_regular_file(path)
        try:
            media_type = self.detect_open_file(descriptor, path.name)
            if not _same_stat(os.fstat(descriptor), observed):
                raise OSError("file changed during media detection")
            return media_type
        finally:
            os.close(descriptor)

    def detect_open_file(self, descriptor: int, logical_name: str) -> str:
        sample, complete = _read_bounded_descriptor(descriptor, _SNIFF_BYTES)
        detected = _signature_media_type(sample)
        if detected is not None:
            return detected
        if sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            detected = _ooxml_media_type(descriptor)
            if detected is not None:
                return detected

        extension = _extension_for(logical_name)
        if extension == ".url.md":
            return _URL_DESCRIPTOR_MEDIA_TYPE
        if _is_utf8_text(sample, complete=complete):
            return _TEXT_MEDIA_TYPES.get(extension, "text/plain")
        return _EXTENSION_MEDIA_TYPES.get(extension, "application/octet-stream")


@dataclass(frozen=True)
class _LinkObservation:
    parent_descriptor: int
    name: str
    observed_stat: os.stat_result


@dataclass(frozen=True)
class _DirectoryEdge:
    parent_descriptor: int
    parent_stat: os.stat_result
    name: str
    entry_stat: os.stat_result
    child_descriptor: int
    child_stat: os.stat_result


@dataclass(frozen=True)
class _FinalFileEdge:
    parent_descriptor: int
    name: str
    observed_stat: os.stat_result


@dataclass(frozen=True)
class _ResolvedContent:
    descriptor: int
    content_stat: os.stat_result
    links: tuple[_LinkObservation, ...]
    directory_edges: tuple[_DirectoryEdge, ...]
    owned_directory_edges: tuple[_DirectoryEdge, ...]
    final_file_edge: _FinalFileEdge | None


@dataclass(frozen=True)
class _ZipMember:
    name: str
    version_needed: int
    flags: int
    compression_method: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int


class _UnsafeSourceError(OSError):
    pass


class InventoryAccessError(OSError):
    """A live source no longer matches its safe inventory observation."""


class SnapshotNamespace(StrEnum):
    """Logical file namespaces accepted by stable ledger validation."""

    RAW_USER = "raw_user"
    RAW_VERSION = "raw_version"
    RAW_WEB = "raw_web"
    EXTRACTED = "extracted"


@dataclass(frozen=True, repr=False)
class _StableFileIdentity:
    content: tuple[int, int, int, int, int, int]
    source_entry: tuple[int, int, int, int, int, int]
    directories: tuple[tuple[int, int], ...]
    links: tuple[tuple[int, int, int, int, int, int], ...]


@dataclass(frozen=True)
class StableFileSnapshot:
    """A same-observation file snapshot whose identity is intentionally opaque."""

    namespace: SnapshotNamespace
    logical_path: PurePosixPath
    identity: object
    byte_size: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True)
class PinnedFile:
    """A stable file authority that is valid only during its callback."""

    snapshot: StableFileSnapshot
    descriptor: int
    descriptor_path: Path


_PinnedResult = TypeVar("_PinnedResult")


def validate_inventory_item(item: object) -> InventoryItem:
    """Validate the complete public inventory-item boundary without doing I/O."""

    if not isinstance(item, InventoryItem):
        raise ValueError("inventory items must be InventoryItem objects")
    fingerprint = item.fingerprint
    if not isinstance(fingerprint, FileFingerprint):
        raise ValueError("inventory fingerprint must be a FileFingerprint")
    validate_inventory_path(fingerprint.path, "inventory fingerprint path")
    if type(fingerprint.byte_size) is not int or fingerprint.byte_size < 0:
        raise ValueError(
            "inventory fingerprint byte_size must be a nonnegative integer"
        )
    if type(fingerprint.mtime_ns) is not int or fingerprint.mtime_ns < 0:
        raise ValueError("inventory fingerprint mtime_ns must be a nonnegative integer")
    if (
        type(item.media_type) is not str
        or not item.media_type
        or item.media_type != item.media_type.lower()
        or "/" not in item.media_type
        or any(
            character.isspace() or character == "\0" for character in item.media_type
        )
    ):
        raise ValueError("inventory media_type must be a normalized MIME type")
    if (
        type(item.extension) is not str
        or item.extension != item.extension.lower()
        or item.extension != _extension_for(fingerprint.path.name)
    ):
        raise ValueError("inventory extension must match the normalized source suffix")
    if item.sha256 is not None and (
        type(item.sha256) is not str or _SHA256_RE.fullmatch(item.sha256) is None
    ):
        raise ValueError(
            "inventory sha256 must be 64 lower-case hexadecimal characters"
        )

    descriptor = item.url_descriptor
    if descriptor is None:
        if item.media_type == _URL_DESCRIPTOR_MEDIA_TYPE:
            raise ValueError("inventory URL media type requires descriptor metadata")
        return item
    if not isinstance(descriptor, UrlDescriptor):
        raise ValueError("inventory url_descriptor must be a UrlDescriptor")
    validate_inventory_path(descriptor.path, "inventory URL descriptor path")
    if descriptor.path != fingerprint.path:
        raise ValueError("inventory URL descriptor path must match its fingerprint")
    if item.media_type != _URL_DESCRIPTOR_MEDIA_TYPE or item.extension != ".url.md":
        raise ValueError("inventory URL descriptor type and suffix must be canonical")
    if item.sha256 is not None:
        raise ValueError("inventory URL descriptor must not carry a byte checksum")
    if type(descriptor.url) is not str:
        raise ValueError("inventory URL must be a string")
    _validate_http_url(descriptor.url)
    if (
        type(descriptor.description) is not str
        or not descriptor.description
        or descriptor.description != descriptor.description.strip()
    ):
        raise ValueError("inventory URL description must be a nonempty trimmed string")
    _validate_single_line_text(descriptor.description, "description")
    if type(descriptor.added) is not date:
        raise ValueError("inventory URL added must be a date")
    return item


def hash_inventory_item(
    paths: RepoPaths,
    item: InventoryItem,
    *,
    hash_file: Callable[[Path], str] = compute_sha256,
) -> str:
    """Hash one inventory item through pinned, descriptor-relative source access."""

    validate_inventory_item(item)
    if item.url_descriptor is not None:
        raise ValueError("URL descriptor bytes must not be hashed")
    snapshot = stable_file_snapshot(
        paths,
        SnapshotNamespace.RAW_USER,
        item.fingerprint.path,
        include_sha256=True,
        hash_file=hash_file,
        expected_fingerprint=item.fingerprint,
    )
    assert snapshot.sha256 is not None
    return snapshot.sha256


def stable_file_snapshot(
    paths: RepoPaths,
    namespace: SnapshotNamespace,
    logical_path: PurePosixPath,
    *,
    include_sha256: bool = False,
    hash_file: Callable[[Path], str] = compute_sha256,
    expected_fingerprint: FileFingerprint | None = None,
) -> StableFileSnapshot:
    """Observe one ledgered file without ever reopening its pathname.

    RAW_USER permits the same safe, in-root aliases as source inventory. Evidence
    namespaces require real directory entries throughout their paths.
    """

    return use_stable_file(
        paths,
        namespace,
        logical_path,
        lambda pinned: pinned.snapshot,
        include_sha256=include_sha256,
        hash_file=hash_file,
        expected_fingerprint=expected_fingerprint,
    )


def use_stable_file(
    paths: RepoPaths,
    namespace: SnapshotNamespace,
    logical_path: PurePosixPath,
    callback: Callable[[PinnedFile], _PinnedResult],
    *,
    include_sha256: bool = False,
    hash_file: Callable[[Path], str] = compute_sha256,
    expected_fingerprint: FileFingerprint | None = None,
) -> _PinnedResult:
    """Run ``callback`` while one exact, revalidated file descriptor stays pinned."""

    components, allow_alias = _validate_snapshot_request(namespace, logical_path)
    if not callable(callback):
        raise ValueError("stable file callback must be callable")
    if expected_fingerprint is not None:
        if not isinstance(expected_fingerprint, FileFingerprint):
            raise ValueError("expected_fingerprint must be a FileFingerprint")
        if expected_fingerprint.path != logical_path:
            raise ValueError("expected fingerprint path must match logical path")
        if (
            type(expected_fingerprint.byte_size) is not int
            or expected_fingerprint.byte_size < 0
            or type(expected_fingerprint.mtime_ns) is not int
            or expected_fingerprint.mtime_ns < 0
        ):
            raise ValueError(
                "expected fingerprint metadata must be nonnegative integers"
            )
    if type(include_sha256) is not bool:
        raise ValueError("include_sha256 must be a boolean")
    if not _safe_fd_primitives_available():
        raise InventoryAccessError("safe descriptor-relative access is unavailable")

    root_descriptor: int | None = None
    anchor_edges: tuple[_DirectoryEdge, ...] = ()
    directory_descriptors: list[int] = []
    path_edges: list[_DirectoryEdge] = []
    resolved: _ResolvedContent | None = None
    callback_error = False
    try:
        root_descriptor, anchor_edges = _open_snapshot_root(paths, namespace)
        directory_descriptors.append(root_descriptor)
        for component in components[:-1]:
            parent = directory_descriptors[-1]
            observed = os.stat(component, dir_fd=parent, follow_symlinks=False)
            child = _open_directory_entry(parent, component, observed)
            try:
                edge = _capture_directory_edge(parent, component, observed, child)
            except BaseException:
                _close_noexcept(child)
                raise
            try:
                directory_descriptors.append(child)
            except BaseException:
                _close_noexcept(child)
                _close_edges_noexcept((edge,))
                raise
            try:
                path_edges.append(edge)
            except BaseException:
                directory_descriptors.pop()
                _close_noexcept(child)
                _close_edges_noexcept((edge,))
                raise

        source_parent = directory_descriptors[-1]
        source_name = components[-1]
        source_stat = os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
        source_edges = (*anchor_edges, *path_edges)
        if stat.S_ISREG(source_stat.st_mode):
            content_descriptor = _open_regular_entry(
                source_parent, source_name, source_stat
            )
            try:
                resolved = _ResolvedContent(
                    content_descriptor,
                    source_stat,
                    (),
                    source_edges,
                    (),
                    None,
                )
            except BaseException:
                _close_noexcept(content_descriptor)
                raise
        elif allow_alias and stat.S_ISLNK(source_stat.st_mode):
            resolved = _open_symlink_target(
                source_parent,
                components[:-1],
                tuple(directory_descriptors),
                source_name,
                source_stat,
                paths.raw.absolute(),
                source_edges,
            )
        else:
            raise _UnsafeSourceError("ledgered path is not a permitted regular file")

        if expected_fingerprint is not None and not _stat_matches_fingerprint(
            resolved.content_stat, expected_fingerprint
        ):
            raise _UnsafeSourceError("ledgered fingerprint changed before observation")
        if not _open_content_is_unchanged(
            resolved.descriptor,
            resolved.content_stat,
            resolved.links,
            resolved.directory_edges,
            source_parent,
            source_name,
            source_stat,
            resolved.final_file_edge,
        ):
            raise _UnsafeSourceError("ledgered file changed before observation")

        checksum = None
        if include_sha256:
            checksum = hash_file(_descriptor_path(resolved.descriptor))
            if type(checksum) is not str or _SHA256_RE.fullmatch(checksum) is None:
                raise ValueError(
                    "hash callback must return 64 lower-case hexadecimal characters"
                )

        before_callback = os.fstat(resolved.descriptor)
        if not _same_stat(
            before_callback, resolved.content_stat
        ) or not _open_content_is_unchanged(
            resolved.descriptor,
            resolved.content_stat,
            resolved.links,
            resolved.directory_edges,
            source_parent,
            source_name,
            source_stat,
            resolved.final_file_edge,
        ):
            raise _UnsafeSourceError("ledgered file changed during observation")
        if expected_fingerprint is not None and not _stat_matches_fingerprint(
            before_callback, expected_fingerprint
        ):
            raise _UnsafeSourceError("ledgered fingerprint changed during observation")
        identity = _StableFileIdentity(
            _stat_identity(before_callback),
            _stat_identity(source_stat),
            (
                (
                    resolved.directory_edges[0].parent_stat.st_dev,
                    resolved.directory_edges[0].parent_stat.st_ino,
                ),
                *(
                    (edge.child_stat.st_dev, edge.child_stat.st_ino)
                    for edge in resolved.directory_edges
                ),
            ),
            tuple(_stat_identity(link.observed_stat) for link in resolved.links),
        )
        os.lseek(resolved.descriptor, 0, os.SEEK_SET)
        snapshot = StableFileSnapshot(
            namespace,
            logical_path,
            identity,
            before_callback.st_size,
            before_callback.st_mtime_ns,
            checksum,
        )
        try:
            result = callback(
                PinnedFile(
                    snapshot,
                    resolved.descriptor,
                    _descriptor_path(resolved.descriptor),
                )
            )
        except BaseException:
            callback_error = True
            raise

        after_callback = os.fstat(resolved.descriptor)
        if not _same_stat(
            after_callback, resolved.content_stat
        ) or not _open_content_is_unchanged(
            resolved.descriptor,
            resolved.content_stat,
            resolved.links,
            resolved.directory_edges,
            source_parent,
            source_name,
            source_stat,
            resolved.final_file_edge,
        ):
            raise _UnsafeSourceError("ledgered file changed during stable use")
        if expected_fingerprint is not None and not _stat_matches_fingerprint(
            after_callback, expected_fingerprint
        ):
            raise _UnsafeSourceError("ledgered fingerprint changed during stable use")
        return result
    except InventoryAccessError:
        raise
    except (OSError, ValueError) as error:
        if callback_error:
            raise
        raise InventoryAccessError(
            "ledgered file could not be observed safely"
        ) from error
    finally:
        if resolved is not None:
            _close_noexcept(resolved.descriptor)
            for link in resolved.links:
                _close_noexcept(link.parent_descriptor)
            if resolved.final_file_edge is not None:
                _close_noexcept(resolved.final_file_edge.parent_descriptor)
            _close_edges_noexcept(resolved.owned_directory_edges)
        for index in range(len(directory_descriptors) - 1, 0, -1):
            _close_noexcept(directory_descriptors[index])
        _close_edges_noexcept(path_edges)
        if root_descriptor is not None:
            _close_noexcept(root_descriptor)
        _close_edges_noexcept(anchor_edges)


def validate_snapshot_path(
    namespace: SnapshotNamespace, logical_path: PurePosixPath
) -> None:
    """Validate a namespace's exact path grammar without any filesystem access."""
    _validate_snapshot_request(namespace, logical_path)


def _validate_snapshot_request(
    namespace: SnapshotNamespace, logical_path: PurePosixPath
) -> tuple[tuple[str, ...], bool]:
    if not isinstance(namespace, SnapshotNamespace):
        raise ValueError("namespace must be a SnapshotNamespace")
    if not isinstance(logical_path, PurePosixPath) or logical_path.is_absolute():
        raise ValueError("logical path must be a repository-relative POSIX path")
    text = logical_path.as_posix()
    if (
        not text
        or text == "."
        or _WINDOWS_DRIVE_RE.match(text)
        or any(part in {"", ".", ".."} for part in logical_path.parts)
        or "\\" in text
        or "\0" in text
    ):
        raise ValueError("logical path must be canonical")

    if namespace is SnapshotNamespace.RAW_USER:
        validate_inventory_path(logical_path, "raw user path")
        return logical_path.parts, True
    if namespace is SnapshotNamespace.RAW_VERSION:
        _validate_evidence_path(logical_path, "_versions", "raw version")
        return logical_path.parts, False
    if namespace is SnapshotNamespace.RAW_WEB:
        _validate_evidence_path(logical_path, "_web", "raw web")
        return logical_path.parts, False
    if (
        logical_path.parts[:2] != ("sources", "extracted")
        or len(logical_path.parts) < 5
        or _SHA256_RE.fullmatch(logical_path.parts[-2]) is None
        or _DERIVATION_FILE_RE.fullmatch(logical_path.name) is None
    ):
        raise ValueError("extracted path must have canonical evidence structure")
    embedded_raw_path = PurePosixPath(*logical_path.parts[2:-2])
    if embedded_raw_path.parts[:1] == ("_web",):
        _validate_evidence_path(embedded_raw_path, "_web", "embedded raw web")
    else:
        validate_inventory_path(embedded_raw_path, "embedded raw user path")
    return logical_path.parts[2:], False


def _validate_evidence_path(
    logical_path: PurePosixPath, prefix: str, label: str
) -> None:
    parts = logical_path.parts
    if (
        len(parts) != 4
        or parts[0] != prefix
        or _SOURCE_ID_RE.fullmatch(parts[1]) is None
        or _SHA256_RE.fullmatch(parts[2]) is None
    ):
        raise ValueError(
            f"{label} path must be {prefix}/<source-id>/<sha256>/<filename>"
        )


def _open_snapshot_root(
    paths: RepoPaths, namespace: SnapshotNamespace
) -> tuple[int, tuple[_DirectoryEdge, ...]]:
    leaf = "extracted" if namespace is SnapshotNamespace.EXTRACTED else "raw"
    return _open_repo_directory_root(paths, ("sources", leaf))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _close_noexcept(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _close_edges_noexcept(
    edges: tuple[_DirectoryEdge, ...] | list[_DirectoryEdge],
) -> None:
    for edge in reversed(edges):
        _close_noexcept(edge.child_descriptor)
        _close_noexcept(edge.parent_descriptor)


def validate_inventory_path(path: object, name: str = "inventory path") -> None:
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
        or path.parts[:2] == ("sources", "raw")
        or is_ignored_source_path(path)
    ):
        raise ValueError(f"{name} must be a canonical raw-source-relative path")


def is_url_descriptor_path(path: object) -> bool:
    """Return whether a logical path has the case-normalized descriptor suffix."""

    return isinstance(path, PurePosixPath) and _extension_for(path.name) == ".url.md"


def _stat_matches_fingerprint(
    observed: os.stat_result,
    fingerprint: FileFingerprint,
) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_size == fingerprint.byte_size
        and observed.st_mtime_ns == fingerprint.mtime_ns
    )


def _descriptor_path(descriptor: int) -> Path:
    for root in (Path("/dev/fd"), Path("/proc/self/fd")):
        if root.is_dir():
            return root / str(descriptor)
    raise InventoryAccessError("a safe path for the pinned descriptor is unavailable")


def inventory_raw_sources(paths: RepoPaths, detector: MediaDetector) -> InventoryReport:
    if not _safe_fd_primitives_available():
        return InventoryReport(
            (),
            (
                Diagnostic(
                    "source_inventory_unsupported",
                    "Safe descriptor-relative source inventory is unavailable.",
                ),
            ),
        )

    items: list[InventoryItem] = []
    skipped: list[Diagnostic] = []
    try:
        raw_descriptor, anchor_edges = _open_raw_root(paths)
    except OSError:
        return InventoryReport(
            (),
            (
                Diagnostic(
                    "source_inventory_error",
                    "The sources/raw directory could not be inspected safely.",
                ),
            ),
        )
    try:
        _walk_raw_directory(
            raw_descriptor,
            (),
            (raw_descriptor,),
            anchor_edges,
            paths.raw.absolute(),
            detector,
            items,
            skipped,
        )
    finally:
        os.close(raw_descriptor)
        _close_directory_edges(anchor_edges)

    items.sort(key=lambda item: item.fingerprint.path.as_posix())
    skipped.sort(
        key=lambda item: (
            "" if item.path is None else item.path.as_posix(),
            item.code,
            item.message,
        )
    )
    return InventoryReport(tuple(items), tuple(skipped))


def parse_url_descriptor(path: Path, relative_path: PurePosixPath) -> UrlDescriptor:
    descriptor, observed = _open_direct_regular_file(path)
    try:
        result = _parse_url_descriptor_from_open_file(descriptor, relative_path)
        if not _same_stat(os.fstat(descriptor), observed):
            raise ValueError("descriptor changed while being parsed")
        return result
    finally:
        os.close(descriptor)


def _parse_url_descriptor_from_open_file(
    descriptor: int, relative_path: PurePosixPath
) -> UrlDescriptor:
    raw, complete = _read_bounded_descriptor(descriptor, _MAX_DESCRIPTOR_BYTES)
    if not complete:
        raise ValueError("descriptor exceeds the byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("descriptor is not valid UTF-8") from error

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("descriptor must start with frontmatter")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("descriptor frontmatter is not closed") from error

    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        match = _FIELD_LINE_RE.fullmatch(line)
        if match is None:
            raise ValueError("frontmatter values must be single-line scalars")
        key, raw_value = match.groups()
        if key in fields:
            raise ValueError(f"duplicate descriptor key: {key}")
        fields[key] = raw_value

    actual_keys = frozenset(fields)
    if actual_keys != _DESCRIPTOR_KEYS:
        unknown = sorted(actual_keys - _DESCRIPTOR_KEYS)
        missing = sorted(_DESCRIPTOR_KEYS - actual_keys)
        if unknown:
            raise ValueError(f"unknown descriptor keys: {', '.join(unknown)}")
        raise ValueError(f"missing descriptor keys: {', '.join(missing)}")

    if fields["kind"] != "url":
        raise ValueError("descriptor kind must be url")
    url = _parse_descriptor_string(fields["url"], "url")
    description = _parse_descriptor_string(fields["description"], "description")
    if not description:
        raise ValueError("descriptor description must not be empty")
    if description != description.strip():
        raise ValueError("descriptor description must not have outer whitespace")
    _validate_single_line_text(description, "description")
    _validate_http_url(url)

    added_text = fields["added"]
    try:
        added = date.fromisoformat(added_text)
    except ValueError as error:
        raise ValueError("descriptor added date is invalid") from error
    if added.isoformat() != added_text:
        raise ValueError("descriptor added date must use YYYY-MM-DD")
    return UrlDescriptor(relative_path, url, description, added)


def source_id_for_url_descriptor(descriptor: UrlDescriptor) -> str:
    descriptor_sha256 = hashlib.sha256(
        b"url-descriptor-v1\0" + descriptor.url.encode("utf-8")
    ).hexdigest()
    return source_id_for_first_seen(descriptor.path, descriptor_sha256)


def _walk_raw_directory(
    directory_descriptor: int,
    directory_parts: tuple[str, ...],
    ancestor_descriptors: tuple[int, ...],
    directory_edges: tuple[_DirectoryEdge, ...],
    raw_absolute: Path,
    detector: MediaDetector,
    items: list[InventoryItem],
    skipped: list[Diagnostic],
) -> None:
    if not _directory_edges_are_unchanged(directory_edges):
        skipped.append(
            _source_changed(
                PurePosixPath(*directory_parts) if directory_parts else None
            )
        )
        return
    try:
        entries = sorted(os.listdir(directory_descriptor))
    except OSError:
        skipped.append(
            Diagnostic(
                "source_inventory_error",
                "Source directory could not be inspected safely.",
                PurePosixPath(*directory_parts) if directory_parts else None,
            )
        )
        return

    for name in entries:
        relative_path = PurePosixPath(*directory_parts, name)
        if is_ignored_source_path(relative_path):
            continue
        try:
            source_stat = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            skipped.append(_inspection_error(relative_path))
            continue

        if stat.S_ISDIR(source_stat.st_mode):
            child_descriptor: int | None = None
            try:
                child_descriptor = _open_directory_entry(
                    directory_descriptor,
                    name,
                    source_stat,
                )
                child_edge = _capture_directory_edge(
                    directory_descriptor,
                    name,
                    source_stat,
                    child_descriptor,
                )
            except OSError:
                if child_descriptor is not None:
                    os.close(child_descriptor)
                skipped.append(_inspection_error(relative_path))
                continue
            try:
                _walk_raw_directory(
                    child_descriptor,
                    (*directory_parts, name),
                    (*ancestor_descriptors, child_descriptor),
                    (*directory_edges, child_edge),
                    raw_absolute,
                    detector,
                    items,
                    skipped,
                )
            finally:
                _close_directory_edge(child_edge)
                os.close(child_descriptor)
            continue
        if stat.S_ISREG(source_stat.st_mode):
            try:
                content_descriptor = _open_regular_entry(
                    directory_descriptor,
                    name,
                    source_stat,
                )
            except OSError:
                skipped.append(_inspection_error(relative_path))
                continue
            _inventory_open_content(
                content_descriptor,
                source_stat,
                (),
                directory_descriptor,
                name,
                source_stat,
                relative_path,
                name,
                directory_edges,
                (),
                None,
                detector,
                items,
                skipped,
            )
            continue
        if stat.S_ISLNK(source_stat.st_mode):
            try:
                resolved = _open_symlink_target(
                    directory_descriptor,
                    directory_parts,
                    ancestor_descriptors,
                    name,
                    source_stat,
                    raw_absolute,
                    directory_edges,
                )
            except OSError:
                skipped.append(_unsafe_symlink(relative_path))
                continue
            _inventory_open_content(
                resolved.descriptor,
                resolved.content_stat,
                resolved.links,
                directory_descriptor,
                name,
                source_stat,
                relative_path,
                relative_path.name,
                resolved.directory_edges,
                resolved.owned_directory_edges,
                resolved.final_file_edge,
                detector,
                items,
                skipped,
            )
            continue
        skipped.append(
            Diagnostic(
                "unsupported_source_type",
                "Only regular files and safe file symlinks can be inventoried.",
                relative_path,
            )
        )


def _inventory_open_content(
    content_descriptor: int,
    content_stat: os.stat_result,
    links: tuple[_LinkObservation, ...],
    source_parent_descriptor: int,
    source_name: str,
    source_stat: os.stat_result,
    relative_path: PurePosixPath,
    detection_name: str,
    directory_edges: tuple[_DirectoryEdge, ...],
    owned_directory_edges: tuple[_DirectoryEdge, ...],
    final_file_edge: _FinalFileEdge | None,
    detector: MediaDetector,
    items: list[InventoryItem],
    skipped: list[Diagnostic],
) -> None:
    try:
        if not _open_content_is_unchanged(
            content_descriptor,
            content_stat,
            links,
            directory_edges,
            source_parent_descriptor,
            source_name,
            source_stat,
            final_file_edge,
        ):
            skipped.append(_source_changed(relative_path))
            return
        try:
            media_type = detector.detect_open_file(content_descriptor, detection_name)
        except (OSError, UnicodeError, ValueError):
            skipped.append(_inspection_error(relative_path))
            return

        descriptor_value: UrlDescriptor | None = None
        if media_type == _URL_DESCRIPTOR_MEDIA_TYPE:
            try:
                descriptor_value = _parse_url_descriptor_from_open_file(
                    content_descriptor,
                    relative_path,
                )
            except (OSError, UnicodeError, ValueError) as error:
                skipped.append(
                    Diagnostic(
                        "invalid_url_descriptor",
                        "URL descriptor is invalid.",
                        relative_path,
                        {"reason": str(error)},
                    )
                )
                return

        if not _open_content_is_unchanged(
            content_descriptor,
            content_stat,
            links,
            directory_edges,
            source_parent_descriptor,
            source_name,
            source_stat,
            final_file_edge,
        ):
            skipped.append(_source_changed(relative_path))
            return

        items.append(
            InventoryItem(
                FileFingerprint(
                    relative_path,
                    content_stat.st_size,
                    content_stat.st_mtime_ns,
                ),
                media_type,
                _extension_for(relative_path.name),
                None,
                descriptor_value,
            )
        )
    finally:
        os.close(content_descriptor)
        for link in links:
            os.close(link.parent_descriptor)
        if final_file_edge is not None:
            os.close(final_file_edge.parent_descriptor)
        _close_directory_edges(owned_directory_edges)


def _open_content_is_unchanged(
    content_descriptor: int,
    content_stat: os.stat_result,
    links: tuple[_LinkObservation, ...],
    directory_edges: tuple[_DirectoryEdge, ...],
    source_parent_descriptor: int,
    source_name: str,
    source_stat: os.stat_result,
    final_file_edge: _FinalFileEdge | None = None,
) -> bool:
    try:
        if not _directory_edges_are_unchanged(directory_edges):
            return False
        current_source = os.stat(
            source_name,
            dir_fd=source_parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_stat(current_source, source_stat):
            return False
        if not _same_stat(os.fstat(content_descriptor), content_stat):
            return False
        if not _final_file_edge_is_unchanged(
            final_file_edge,
            content_descriptor,
            content_stat,
        ):
            return False
        return all(
            _same_stat(
                os.stat(
                    link.name,
                    dir_fd=link.parent_descriptor,
                    follow_symlinks=False,
                ),
                link.observed_stat,
            )
            for link in links
        )
    except OSError:
        return False


def _final_file_edge_is_unchanged(
    edge: _FinalFileEdge | None,
    content_descriptor: int,
    content_stat: os.stat_result,
) -> bool:
    if edge is None:
        return True
    current_entry = os.stat(
        edge.name,
        dir_fd=edge.parent_descriptor,
        follow_symlinks=False,
    )
    pinned_content = os.fstat(content_descriptor)
    return (
        stat.S_ISREG(current_entry.st_mode)
        and stat.S_ISREG(edge.observed_stat.st_mode)
        and stat.S_ISREG(pinned_content.st_mode)
        and _same_stat(current_entry, edge.observed_stat)
        and _same_stat(pinned_content, content_stat)
        and _same_stat(current_entry, pinned_content)
    )


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
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


def _capture_directory_edge(
    parent_descriptor: int,
    name: str,
    entry_stat: os.stat_result,
    child_descriptor: int,
) -> _DirectoryEdge:
    parent_stat = os.fstat(parent_descriptor)
    child_stat = os.fstat(child_descriptor)
    if not stat.S_ISDIR(parent_stat.st_mode) or not _same_directory_identity(
        entry_stat, child_stat
    ):
        raise _UnsafeSourceError("directory edge changed before observation")
    owned_parent = os.dup(parent_descriptor)
    try:
        owned_child = os.dup(child_descriptor)
    except BaseException:
        _close_noexcept(owned_parent)
        raise
    try:
        return _DirectoryEdge(
            owned_parent,
            parent_stat,
            name,
            entry_stat,
            owned_child,
            child_stat,
        )
    except BaseException:
        _close_noexcept(owned_child)
        _close_noexcept(owned_parent)
        raise


def _directory_edges_are_unchanged(edges: tuple[_DirectoryEdge, ...]) -> bool:
    try:
        for edge in edges:
            parent_now = os.fstat(edge.parent_descriptor)
            child_now = os.fstat(edge.child_descriptor)
            entry_now = os.stat(
                edge.name,
                dir_fd=edge.parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_directory_identity(parent_now, edge.parent_stat)
                or not _same_directory_identity(child_now, edge.child_stat)
                or not _same_directory_identity(entry_now, edge.entry_stat)
                or not _same_directory_identity(entry_now, child_now)
            ):
                return False
    except OSError:
        return False
    return True


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


def _close_directory_edge(edge: _DirectoryEdge) -> None:
    _close_noexcept(edge.child_descriptor)
    _close_noexcept(edge.parent_descriptor)


def _close_directory_edges(
    edges: tuple[_DirectoryEdge, ...] | list[_DirectoryEdge],
) -> None:
    for edge in reversed(edges):
        _close_directory_edge(edge)


def _safe_fd_primitives_available() -> bool:
    return (
        all(
            hasattr(os, name)
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "dup", "fstat")
        )
        and _OPEN_SUPPORT_MARKER in os.supports_dir_fd
        and _STAT_SUPPORT_MARKER in os.supports_dir_fd
        and _READLINK_SUPPORT_MARKER in os.supports_dir_fd
        and _STAT_SUPPORT_MARKER in os.supports_follow_symlinks
        and _LISTDIR_SUPPORT_MARKER in os.supports_fd
    )


def _open_raw_root(paths: RepoPaths) -> tuple[int, tuple[_DirectoryEdge, ...]]:
    return _open_repo_directory_root(paths, ("sources", "raw"))


def _open_repo_directory_root(
    paths: RepoPaths, components: tuple[str, ...]
) -> tuple[int, tuple[_DirectoryEdge, ...]]:
    anchor_edges: list[_DirectoryEdge] = []
    root_descriptor = _open_directory_path(paths.root)
    opened_descriptors: list[int] = []
    try:
        parent = root_descriptor
        for component in components:
            observed = os.stat(component, dir_fd=parent, follow_symlinks=False)
            child = _open_directory_entry(parent, component, observed)
            try:
                edge = _capture_directory_edge(parent, component, observed, child)
            except BaseException:
                _close_noexcept(child)
                raise
            try:
                opened_descriptors.append(child)
            except BaseException:
                _close_noexcept(child)
                _close_edges_noexcept((edge,))
                raise
            try:
                anchor_edges.append(edge)
            except BaseException:
                opened_descriptors.pop()
                _close_noexcept(child)
                _close_edges_noexcept((edge,))
                raise
            parent = child
        edges = tuple(anchor_edges)
        result = opened_descriptors[-1]
        return_value = (result, edges)
        opened_descriptors.pop()
        return return_value
    except BaseException:
        _close_edges_noexcept(anchor_edges)
        raise
    finally:
        for index in range(len(opened_descriptors) - 1, -1, -1):
            _close_noexcept(opened_descriptors[index])
        _close_noexcept(root_descriptor)


def _open_directory_path(path: Path) -> int:
    descriptor = os.open(path, _directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _UnsafeSourceError("opened path is not a directory")
        return descriptor
    except BaseException:
        _close_noexcept(descriptor)
        raise


def _open_directory_entry(
    parent_descriptor: int,
    name: str,
    observed: os.stat_result,
) -> int:
    if not stat.S_ISDIR(observed.st_mode):
        raise _UnsafeSourceError("observed entry is not a directory")
    descriptor = os.open(
        name,
        _directory_open_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_stat(opened, observed):
            raise _UnsafeSourceError("directory changed before open")
        return descriptor
    except BaseException:
        _close_noexcept(descriptor)
        raise


def _open_regular_entry(
    parent_descriptor: int,
    name: str,
    observed: os.stat_result,
) -> int:
    if not stat.S_ISREG(observed.st_mode):
        raise _UnsafeSourceError("observed entry is not a regular file")
    descriptor = os.open(
        name,
        _content_open_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_stat(opened, observed):
            raise _UnsafeSourceError("file changed before open")
        return descriptor
    except BaseException:
        _close_noexcept(descriptor)
        raise


def _open_direct_regular_file(path: Path) -> tuple[int, os.stat_result]:
    if not _safe_fd_primitives_available():
        raise OSError("safe descriptor-relative file access is unavailable")
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode):
        raise OSError("path is not a regular file")
    descriptor = os.open(path, _content_open_flags())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_stat(opened, observed):
            raise OSError("file changed before open")
        return descriptor, opened
    except BaseException:
        _close_noexcept(descriptor)
        raise


def _open_symlink_target(
    source_parent_descriptor: int,
    parent_parts: tuple[str, ...],
    ancestor_descriptors: tuple[int, ...],
    source_name: str,
    source_stat: os.stat_result,
    raw_absolute: Path,
    source_directory_edges: tuple[_DirectoryEdge, ...],
) -> _ResolvedContent:
    links: list[_LinkObservation] = []
    owned_directory_edges: list[_DirectoryEdge] = []
    descriptor_stack: list[int] = []
    resolved_parts = list(parent_parts)
    try:
        for value in ancestor_descriptors:
            duplicate = os.dup(value)
            try:
                descriptor_stack.append(duplicate)
            except BaseException:
                _close_noexcept(duplicate)
                raise
        target, observation = _read_stable_link(
            source_parent_descriptor,
            source_name,
            source_stat,
        )
        try:
            links.append(observation)
        except BaseException:
            _close_noexcept(observation.parent_descriptor)
            raise
        pending = _link_target_components(
            target,
            (),
            raw_absolute,
            descriptor_stack,
            resolved_parts,
        )
        followed_links = 1

        while pending:
            component = pending.pop(0)
            if component in {"", "."}:
                continue
            if component == "..":
                if len(descriptor_stack) == 1:
                    raise _UnsafeSourceError("symlink escapes sources/raw")
                _close_noexcept(descriptor_stack.pop())
                resolved_parts.pop()
                continue

            candidate_path = PurePosixPath(*resolved_parts, component)
            if is_ignored_source_path(candidate_path):
                raise _UnsafeSourceError("symlink target is ignored")
            current_descriptor = descriptor_stack[-1]
            observed = os.stat(
                component,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )

            if stat.S_ISLNK(observed.st_mode):
                followed_links += 1
                if followed_links > 40:
                    raise _UnsafeSourceError("too many symlink components")
                nested_target, nested_observation = _read_stable_link(
                    current_descriptor,
                    component,
                    observed,
                )
                try:
                    links.append(nested_observation)
                except BaseException:
                    _close_noexcept(nested_observation.parent_descriptor)
                    raise
                pending = _link_target_components(
                    nested_target,
                    tuple(pending),
                    raw_absolute,
                    descriptor_stack,
                    resolved_parts,
                )
                continue

            if pending:
                child_descriptor = _open_directory_entry(
                    current_descriptor,
                    component,
                    observed,
                )
                try:
                    edge = _capture_directory_edge(
                        current_descriptor,
                        component,
                        observed,
                        child_descriptor,
                    )
                except BaseException:
                    _close_noexcept(child_descriptor)
                    raise
                try:
                    owned_directory_edges.append(edge)
                except BaseException:
                    _close_edges_noexcept((edge,))
                    _close_noexcept(child_descriptor)
                    raise
                try:
                    descriptor_stack.append(child_descriptor)
                except BaseException:
                    owned_directory_edges.pop()
                    _close_edges_noexcept((edge,))
                    _close_noexcept(child_descriptor)
                    raise
                resolved_parts.append(component)
                continue

            content_descriptor: int | None = None
            final_parent_descriptor: int | None = None
            try:
                content_descriptor = _open_regular_entry(
                    current_descriptor,
                    component,
                    observed,
                )
                target_path = PurePosixPath(*resolved_parts, component)
                if is_ignored_source_path(target_path):
                    raise _UnsafeSourceError("symlink target is ignored")
                final_parent_descriptor = os.dup(current_descriptor)
                result = _ResolvedContent(
                    content_descriptor,
                    observed,
                    tuple(links),
                    (*source_directory_edges, *owned_directory_edges),
                    tuple(owned_directory_edges),
                    _FinalFileEdge(
                        final_parent_descriptor,
                        component,
                        observed,
                    ),
                )
            except BaseException:
                if final_parent_descriptor is not None:
                    _close_noexcept(final_parent_descriptor)
                if content_descriptor is not None:
                    _close_noexcept(content_descriptor)
                raise
            return result
        raise _UnsafeSourceError("symlink target is not a regular file")
    except BaseException:
        for link in links:
            _close_noexcept(link.parent_descriptor)
        _close_directory_edges(owned_directory_edges)
        raise
    finally:
        for index in range(len(descriptor_stack) - 1, -1, -1):
            _close_noexcept(descriptor_stack[index])


def _read_stable_link(
    parent_descriptor: int,
    name: str,
    observed: os.stat_result,
) -> tuple[str, _LinkObservation]:
    target = os.readlink(name, dir_fd=parent_descriptor)
    after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISLNK(after.st_mode) or not _same_stat(after, observed):
        raise _UnsafeSourceError("symlink changed while resolving")
    duplicate = os.dup(parent_descriptor)
    try:
        observation = _LinkObservation(duplicate, name, observed)
    except BaseException:
        _close_noexcept(duplicate)
        raise
    return target, observation


def _link_target_components(
    target: str,
    remaining: tuple[str, ...],
    raw_absolute: Path,
    descriptor_stack: list[int],
    resolved_parts: list[str],
) -> list[str]:
    target_path = Path(target)
    if target_path.is_absolute():
        normalized = Path(os.path.normpath(target))
        try:
            relative = normalized.relative_to(raw_absolute)
        except ValueError as error:
            raise _UnsafeSourceError(
                "absolute symlink target escapes sources/raw"
            ) from error
        while len(descriptor_stack) > 1:
            os.close(descriptor_stack.pop())
        resolved_parts.clear()
        components = list(relative.parts)
    else:
        components = list(target_path.parts)
    return [*components, *remaining]


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _content_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _read_bounded_descriptor(descriptor: int, limit: int) -> tuple[bytes, bool]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    byte_budget = limit + 1
    chunks: list[bytes] = []
    observed_bytes = 0
    reached_eof = False
    while observed_bytes < byte_budget:
        remaining = byte_budget - observed_bytes
        try:
            chunk = os.read(descriptor, remaining)
        except InterruptedError:
            continue
        if not chunk:
            reached_eof = True
            break
        if len(chunk) > remaining:
            raise OSError("read exceeded the requested byte budget")
        chunks.append(chunk)
        observed_bytes += len(chunk)
    value = b"".join(chunks)
    return value[:limit], reached_eof


def _signature_media_type(sample: bytes) -> str | None:
    if b"%PDF-" in sample[:1_024]:
        return "application/pdf"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ooxml_media_type(descriptor: int) -> str | None:
    file_size = os.fstat(descriptor).st_size
    tail_size = min(file_size, _MAX_ZIP_TAIL_BYTES)
    tail_offset = file_size - tail_size
    tail = _read_exact_at(descriptor, tail_offset, tail_size)
    eocd = _find_zip_eocd(tail)
    if eocd is None:
        return None

    eocd_offset, fields = eocd
    (
        _signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        _comment_size,
    ) = fields
    absolute_eocd = tail_offset + eocd_offset
    if (
        disk_number != 0
        or central_disk != 0
        or disk_entries != total_entries
        or total_entries > _MAX_ZIP_MEMBERS
        or total_entries == 0xFFFF
        or central_size > _MAX_ZIP_CENTRAL_DIRECTORY_BYTES
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or central_offset + central_size != absolute_eocd
    ):
        return None

    central = _read_exact_at(descriptor, central_offset, central_size)
    if len(central) != central_size:
        return None
    members = _parse_zip_central_directory(central, total_entries)
    if members is None:
        return None

    name_counts: dict[str, int] = {}
    for member in members:
        name_counts[member.name] = name_counts.get(member.name, 0) + 1
    if any(
        name_counts.get(required_name) != 1
        for required_name in ("[Content_Types].xml", "_rels/.rels")
    ):
        return None

    families = tuple(
        media_type
        for member_name, media_type in (
            ("word/document.xml", _DOCX_MEDIA_TYPE),
            ("ppt/presentation.xml", _PPTX_MEDIA_TYPE),
            ("xl/workbook.xml", _XLSX_MEDIA_TYPE),
        )
        if name_counts.get(member_name) == 1
    )
    if len(families) != 1 or any(
        name_counts.get(member_name, 0) > 1
        for member_name in (
            "word/document.xml",
            "ppt/presentation.xml",
            "xl/workbook.xml",
        )
    ):
        return None
    if not _validate_zip_local_headers(descriptor, members, central_offset):
        return None
    return families[0]


def _find_zip_eocd(
    tail: bytes,
) -> tuple[int, tuple[bytes, int, int, int, int, int, int, int]] | None:
    signature = b"PK\x05\x06"
    offset = tail.rfind(signature)
    while offset >= 0:
        if len(tail) - offset >= 22:
            fields = struct.unpack_from("<4s4H2LH", tail, offset)
            if offset + 22 + fields[-1] == len(tail):
                return offset, fields
        offset = tail.rfind(signature, 0, offset)
    return None


def _parse_zip_central_directory(
    central: bytes,
    expected_members: int,
) -> tuple[_ZipMember, ...] | None:
    members: list[_ZipMember] = []
    cursor = 0
    cumulative_name_bytes = 0
    for _index in range(expected_members):
        if cursor + 46 > len(central) or central[cursor : cursor + 4] != b"PK\x01\x02":
            return None
        (
            _signature,
            _version_made_by,
            version_needed,
            flags,
            compression_method,
            _modified_time,
            _modified_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
            comment_size,
            disk_start,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = struct.unpack_from("<4s6H3L5H2L", central, cursor)
        if (
            not _supported_zip_version(version_needed, compression_method)
            or flags & ~_SUPPORTED_ZIP_FLAGS
            or compression_method not in _SUPPORTED_ZIP_METHODS
            or compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
            or disk_start != 0
            or (compression_method == 0 and compressed_size != uncompressed_size)
        ):
            return None
        record_size = 46 + name_size + extra_size + comment_size
        if cursor + record_size > len(central):
            return None
        cumulative_name_bytes += name_size
        if cumulative_name_bytes > _MAX_ZIP_MEMBER_NAME_BYTES:
            return None
        raw_name = central[cursor + 46 : cursor + 46 + name_size]
        name = _decode_zip_name(raw_name, flags)
        if name is None or not _valid_zip_member_name(name):
            return None
        extra_start = cursor + 46 + name_size
        extra = central[extra_start : extra_start + extra_size]
        if not _valid_zip_extra_fields(extra):
            return None
        members.append(
            _ZipMember(
                name,
                version_needed,
                flags,
                compression_method,
                crc32,
                compressed_size,
                uncompressed_size,
                local_header_offset,
            )
        )
        cursor += record_size
    return tuple(members) if cursor == len(central) else None


def _validate_zip_local_headers(
    descriptor: int,
    members: tuple[_ZipMember, ...],
    central_offset: int,
) -> bool:
    if not hasattr(os, "pread"):
        return False
    metadata_bytes = 0
    occupied_ranges: list[tuple[int, int]] = []
    for member in members:
        if member.local_header_offset + 30 > central_offset:
            return False
        metadata_bytes += 30
        if metadata_bytes > _MAX_ZIP_LOCAL_METADATA_BYTES:
            return False
        fixed = _pread_exact(descriptor, member.local_header_offset, 30)
        if len(fixed) != 30 or fixed[:4] != b"PK\x03\x04":
            return False
        (
            _signature,
            version_needed,
            flags,
            compression_method,
            _modified_time,
            _modified_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_size,
            extra_size,
        ) = struct.unpack("<4s5H3L2H", fixed)
        variable_size = name_size + extra_size
        metadata_bytes += variable_size
        if (
            metadata_bytes > _MAX_ZIP_LOCAL_METADATA_BYTES
            or member.local_header_offset + 30 + variable_size > central_offset
        ):
            return False
        variable = _pread_exact(
            descriptor,
            member.local_header_offset + 30,
            variable_size,
        )
        if len(variable) != variable_size:
            return False
        raw_name = variable[:name_size]
        local_name = _decode_zip_name(raw_name, flags)
        extra = variable[name_size:]
        if (
            version_needed != member.version_needed
            or flags != member.flags
            or flags & ~_SUPPORTED_ZIP_FLAGS
            or compression_method != member.compression_method
            or crc32 != member.crc32
            or compressed_size != member.compressed_size
            or uncompressed_size != member.uncompressed_size
            or compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_name != member.name
            or not _valid_zip_extra_fields(extra)
        ):
            return False
        data_start = member.local_header_offset + 30 + variable_size
        data_end = data_start + member.compressed_size
        if data_end > central_offset:
            return False
        occupied_ranges.append((member.local_header_offset, data_end))

    occupied_ranges.sort()
    return all(
        current_start >= previous_end
        for (_previous_start, previous_end), (current_start, _current_end) in zip(
            occupied_ranges,
            occupied_ranges[1:],
            strict=False,
        )
    )


def _supported_zip_version(version_needed: int, compression_method: int) -> bool:
    minimum = 10 if compression_method == 0 else 20
    return minimum <= version_needed <= 20


def _decode_zip_name(raw_name: bytes, flags: int) -> str | None:
    try:
        return raw_name.decode("utf-8" if flags & 0x0800 else "cp437")
    except UnicodeDecodeError:
        return None


def _valid_zip_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and "\x00" not in name
        and ".." not in path.parts
        and path.as_posix() == name.rstrip("/")
    )


def _valid_zip_extra_fields(extra: bytes) -> bool:
    cursor = 0
    while cursor < len(extra):
        if cursor + 4 > len(extra):
            return False
        field_id, field_size = struct.unpack_from("<2H", extra, cursor)
        cursor += 4
        if field_id == 0x0001 or cursor + field_size > len(extra):
            return False
        cursor += field_size
    return True


def _pread_exact(descriptor: int, offset: int, size: int) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    while observed < size:
        remaining = size - observed
        try:
            chunk = os.pread(descriptor, remaining, offset + observed)
        except InterruptedError:
            continue
        if not chunk:
            break
        if len(chunk) > remaining:
            raise OSError("pread exceeded the requested metadata budget")
        chunks.append(chunk)
        observed += len(chunk)
    return b"".join(chunks)


def _read_exact_at(descriptor: int, offset: int, size: int) -> bytes:
    os.lseek(descriptor, offset, os.SEEK_SET)
    chunks: list[bytes] = []
    observed = 0
    while observed < size:
        remaining = size - observed
        try:
            chunk = os.read(descriptor, remaining)
        except InterruptedError:
            continue
        if not chunk:
            break
        if len(chunk) > remaining:
            raise OSError("read exceeded the requested metadata budget")
        chunks.append(chunk)
        observed += len(chunk)
    return b"".join(chunks)


def _is_utf8_text(sample: bytes, *, complete: bool) -> bool:
    try:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        decoded = decoder.decode(sample, final=complete)
    except UnicodeDecodeError:
        return False
    return "\x00" not in decoded


def _extension_for(name: str) -> str:
    lowered = name.lower()
    return next(
        (extension for extension in _KNOWN_EXTENSIONS if lowered.endswith(extension)),
        Path(lowered).suffix,
    )


def _parse_descriptor_string(raw_value: str, field: str) -> str:
    if raw_value.startswith('"'):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise ValueError(f"descriptor {field} has invalid JSON") from error
        if not isinstance(value, str):
            raise ValueError(f"descriptor {field} must be a string")
        _validate_single_line_text(value, field)
        return value

    if not raw_value or raw_value != raw_value.strip():
        raise ValueError(f"descriptor {field} must not be blank or padded")
    if (
        raw_value[0] in "'[{|>"
        or ": " in raw_value
        or any(character in raw_value for character in '#[]{}"')
    ):
        raise ValueError(f"descriptor {field} uses an unsupported scalar form")
    if raw_value.startswith(("&", "*", "!", "- ", "? ")):
        raise ValueError(f"descriptor {field} uses an unsupported scalar form")
    _validate_single_line_text(raw_value, field)
    return raw_value


def _validate_single_line_text(value: str, field: str) -> None:
    if any(
        character in "\r\n" or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ValueError(f"descriptor {field} must be one line without controls")


def _validate_http_url(url: str) -> None:
    _validate_single_line_text(url, "url")
    if any(character.isspace() for character in url):
        raise ValueError("descriptor URL must not contain whitespace")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as error:
        raise ValueError("descriptor URL is invalid") from error
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("descriptor URL must use http or https")


def _inspection_error(relative_path: PurePosixPath) -> Diagnostic:
    return Diagnostic(
        "source_inventory_error",
        "Source could not be inspected safely.",
        relative_path,
    )


def _source_changed(relative_path: PurePosixPath | None) -> Diagnostic:
    return Diagnostic(
        "source_changed_during_inventory",
        "Source changed while it was being inventoried.",
        relative_path,
    )


def _unsafe_symlink(relative_path: PurePosixPath | None) -> Diagnostic:
    return Diagnostic(
        "unsafe_source_symlink",
        "Symlink does not resolve to an allowed regular file below sources/raw.",
        relative_path,
    )
