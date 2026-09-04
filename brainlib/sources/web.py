"""Explicitly approved HTTP capture and immutable, descriptor-backed snapshots."""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path, PurePath, PurePosixPath
from typing import BinaryIO, Literal, Protocol, TypeAlias

from ..contracts import (
    ContentVersion,
    FileFingerprint,
    RetrievalMetadata,
    SourceRecord,
    SourceRepresentation,
    SourceState,
    UrlDescriptorMetadata,
    compute_corpus_revision,
)
from ..diagnostics import Diagnostic, JSONValue
from ..extractors import handoff as agent_handoff
from ..inventory import (
    InventoryAccessError,
    InventoryItem,
    SnapshotNamespace,
    UrlDescriptor,
    parse_url_descriptor,
    stable_file_snapshot,
    use_stable_file,
)
from ..layout import RepoPaths
from ..ledger import (
    ActivationGuard,
    _PinnedDirectory,
    _atomic_write_archive,
    _canonical_record_payload,
    _fsync_directory,
    _read_regular_at,
    activate_derivation,
    derive_extraction_path,
    representation_for,
)
from ..registry import ExtractorRegistry, ExtractorSpec, effective_extractor_version
from ..sync import (
    ProcessResult,
    ProcessingContext,
    SourceProcessor,
    _representation_data,
    _extracting_record,
    _replace_attempt_diagnostics,
    _validate_pinned_process_artifact,
    _validate_process_result,
)


class WebCaptureError(ValueError):
    code = "web_capture_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = self.code if code is None else code
        super().__init__(f"{self.code}: {message}")


class ApprovalRequired(WebCaptureError):
    code = "approval_required"


class ApprovalEventConflict(WebCaptureError):
    code = "approval_event_conflict"


class UnsafeNetworkTarget(WebCaptureError):
    code = "unsafe_network_target"


class NetworkPeerMismatch(WebCaptureError):
    code = "network_peer_mismatch"


class TooManyRedirects(WebCaptureError):
    code = "too_many_redirects"


@dataclass(frozen=True)
class ApprovalClaim:
    event_id: str
    scope: str
    note: str


@dataclass(frozen=True)
class RenderedCapture:
    staging_path: Path
    retrieved_at: datetime
    final_url: str
    redirects: tuple[str, ...]
    detected_media_type: str
    handoff_id: str | None = None


@dataclass(frozen=True)
class SnapshotRequest:
    source_id: str
    approval: ApprovalClaim
    rendered: RenderedCapture | None = None


@dataclass(frozen=True)
class SnapshotResult:
    source_id: str
    raw_path: PurePosixPath
    content_sha256: str
    source_version: ContentVersion
    retrieval: RetrievalMetadata
    extraction_result: ProcessResult | None
    active_representation: SourceRepresentation | None
    corpus_revision: str


IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class VettedEndpoint:
    url: str
    scheme: Literal["http", "https"]
    hostname: str
    port: int
    host_header: str
    addresses: tuple[IPAddress, ...]


class ResolveHost(Protocol):
    def __call__(self, hostname: str, port: int) -> tuple[str, ...]: ...


class NetworkPolicy(Protocol):
    def vet_url(self, url: str) -> VettedEndpoint: ...
    def verify_connected_peer(
        self, endpoint: VettedEndpoint, peer_address: str
    ) -> None: ...


def _control(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def canonicalize_url(url: str) -> str:
    """Normalize authority only; path/query escapes retain their original bytes."""
    try:
        if (
            type(url) is not str
            or not url
            or _control(url)
            or any(c.isspace() for c in url)
        ):
            raise ValueError("URL must have no whitespace or control characters")
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must use HTTP(S) with a hostname")
        if (
            parsed.username is not None
            or parsed.password is not None
            or "\\" in parsed.netloc
        ):
            raise ValueError("credentials and backslashes are not allowed")
        hostname = parsed.hostname
        if "%" in hostname or parsed.netloc.endswith(":"):
            raise ValueError("invalid hostname or empty port")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii").lower()
            if len(hostname) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in hostname.rstrip(".").split(".")
            ):
                raise ValueError("invalid DNS hostname")
        else:
            hostname = str(address)
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if scheme == "https" else 80
        port = default_port if parsed.port is None else parsed.port
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        authority = host if port == default_port else f"{host}:{port}"
        return urllib.parse.urlunsplit(
            (scheme, authority, parsed.path or "/", parsed.query, "")
        )
    except (ValueError, UnicodeError) as error:
        raise UnsafeNetworkTarget(str(error)) from error


_active_deadline: ContextVar[_CaptureDeadline | None] = ContextVar(
    "web_capture_deadline", default=None
)


class _CaptureDeadline:
    """One elapsed budget, with cancellation inside blocking socket operations."""

    def __init__(self, seconds: float) -> None:
        self.ends_at = time.monotonic() + seconds
        self._lock = threading.Lock()
        self._stream = None
        self._expired = False
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def remaining(self) -> float:
        remaining = self.ends_at - time.monotonic()
        if self._expired or remaining <= 0:
            raise TimeoutError("capture deadline exceeded")
        return remaining

    def bind_socket(self, stream) -> None:
        with self._lock:
            self.remaining()
            self._stream = stream
            stream.settimeout(self.remaining())

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            if self._stream is not None:
                # shutdown wakes a pending buffered HTTP read even when a peer
                # keeps sending bytes often enough to reset socket inactivity.
                try:
                    self._stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._stream.close()

    def __enter__(self):
        self._token = _active_deadline.set(self)
        self._timer.start()
        return self

    def __exit__(self, exc_type, error, traceback):
        self._timer.cancel()
        self._timer.join()
        _active_deadline.reset(self._token)
        if error is not None and isinstance(error, Exception):
            self.remaining()


def _resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    # libc getaddrinfo has no cancellable elapsed timeout. Keep it in a fixed
    # short-lived child that can be killed and reaped, never a lingering thread.
    deadline = _active_deadline.get()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("dns_driver.py")),
                hostname,
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            try:
                process.wait(timeout=30 if deadline is None else deadline.remaining())
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("capture deadline exceeded during DNS") from error
            if process.returncode:
                raise OSError("host resolution failed")
            output.seek(0)
            body = output.read(65537)
            if len(body) > 65536:
                raise OSError("host resolution exceeded its byte limit")
            answers = json.loads(body)
            if type(answers) is not list or any(
                type(item) is not str for item in answers
            ):
                raise OSError("invalid host resolution result")
            return tuple(answers)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _public(address: IPAddress) -> bool:
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


class PublicAddressNetworkPolicy:
    def __init__(self, *, resolve: ResolveHost | None = None) -> None:
        self._resolve = _resolve_host if resolve is None else resolve

    def vet_url(self, url: str) -> VettedEndpoint:
        canonical = canonicalize_url(url)
        parsed = urllib.parse.urlsplit(canonical)
        hostname = parsed.hostname
        assert hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                answers = self._resolve(hostname, port)
                addresses = tuple(
                    sorted(
                        {ipaddress.ip_address(value) for value in answers},
                        key=lambda a: (a.version, int(a)),
                    )
                )
            except TimeoutError:
                raise
            except (OSError, ValueError) as error:
                raise UnsafeNetworkTarget("host resolution failed") from error
            if not addresses or any(not _public(address) for address in addresses):
                raise UnsafeNetworkTarget("empty or non-public DNS answer")
        else:
            if not _public(literal):
                raise UnsafeNetworkTarget("non-public IP literal")
            addresses = (literal,)
        return VettedEndpoint(
            canonical, parsed.scheme, hostname, port, parsed.netloc, addresses
        )

    def verify_connected_peer(
        self, endpoint: VettedEndpoint, peer_address: str
    ) -> None:
        try:
            peer = ipaddress.ip_address(peer_address)
        except ValueError as error:
            raise NetworkPeerMismatch("invalid peer address") from error
        if peer not in endpoint.addresses or not _public(peer):
            raise NetworkPeerMismatch(
                "connected peer is outside the vetted address set"
            )


@dataclass
class HTTPHop:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO
    peer_address: str


class PinnedRequester(Protocol):
    def __call__(
        self,
        endpoint: VettedEndpoint,
        address: IPAddress,
        *,
        target: str,
        timeout_seconds: float,
    ) -> HTTPHop: ...


@dataclass(frozen=True)
class NetworkCapture:
    staging_path: Path
    retrieved_at: datetime
    final_url: str
    redirects: tuple[str, ...]
    detected_media_type: str
    safe_filename: str


class WebTransport(Protocol):
    def capture(
        self,
        requested_url: str,
        *,
        paths: RepoPaths,
        timeout_seconds: int,
        max_output_bytes: int,
        now: datetime,
    ) -> NetworkCapture: ...


class _HTTPBody:
    def __init__(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection
    ) -> None:
        self.response, self.connection = response, connection

    def read(self, size: int) -> bytes:
        return self.response.read(size)

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()


_MIME_SUFFIX = {
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "text/csv": ".csv",
    "application/octet-stream": ".bin",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def _media_type(value: str) -> str:
    media = value.split(";", 1)[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media):
        raise WebCaptureError("invalid detected media type")
    return media


def _safe_filename(name: str, media: str) -> str:
    name = "".join(c for c in name if c not in "/\\" and not _control(c))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:120] or "snapshot"
    if not PurePosixPath(name).suffix:
        name += _MIME_SUFFIX.get(media, ".bin")
    return name


@contextmanager
def _staging_directory(paths: RepoPaths):
    with _PinnedDirectory.open(paths.root) as pinned:
        directory = pinned.descriptor
        for component in (".brain", "web-staging"):
            try:
                os.mkdir(component, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = pinned.open_child(directory, component)
            _fsync_directory(directory)
            directory = child
        pinned.validate()
        yield pinned, directory
        pinned.validate()


class PublicHTTPTransport:
    def __init__(
        self,
        *,
        policy: NetworkPolicy | None = None,
        request_hop: PinnedRequester | None = None,
        max_redirects: int = 10,
    ) -> None:
        if type(max_redirects) is not int or max_redirects < 0:
            raise ValueError("max_redirects must be a nonnegative integer")
        self.policy = PublicAddressNetworkPolicy() if policy is None else policy
        self._request_hop = self._request_pinned if request_hop is None else request_hop
        self.max_redirects = max_redirects

    def _request_pinned(
        self,
        endpoint: VettedEndpoint,
        address: IPAddress,
        *,
        target: str,
        timeout_seconds: float,
    ) -> HTTPHop:
        if address not in endpoint.addresses:
            raise UnsafeNetworkTarget("connection address was not vetted")
        stream = socket.socket(
            socket.AF_INET6 if address.version == 6 else socket.AF_INET,
            socket.SOCK_STREAM,
        )
        connection = None
        try:
            deadline = _active_deadline.get()
            if deadline is None:
                raise ValueError("pinned HTTP requests require a capture deadline")
            deadline.bind_socket(stream)
            # This socket receives a numeric address, never a hostname or proxy.
            stream.connect((str(address), endpoint.port))
            deadline.remaining()
            self.policy.verify_connected_peer(endpoint, stream.getpeername()[0])
            if endpoint.scheme == "https":
                stream = ssl.create_default_context().wrap_socket(
                    stream,
                    server_hostname=endpoint.hostname,
                    do_handshake_on_connect=False,
                )
                deadline.bind_socket(stream)
                stream.do_handshake()
                deadline.remaining()
                self.policy.verify_connected_peer(endpoint, stream.getpeername()[0])
            peer = stream.getpeername()[0]
            connection = http.client.HTTPConnection(
                endpoint.hostname, endpoint.port, timeout=timeout_seconds
            )
            connection.sock = stream
            stream.settimeout(deadline.remaining())
            connection.request(
                "GET",
                target,
                headers={
                    "Host": endpoint.host_header,
                    "Connection": "close",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Second-Brain-Lite/1",
                },
            )
            response = connection.getresponse()
            deadline.remaining()
            return HTTPHop(
                response.status,
                dict(response.getheaders()),
                _HTTPBody(response, connection),
                peer,
            )
        except BaseException:
            if connection is not None:
                connection.close()
            stream.close()
            raise

    def capture(
        self,
        requested_url: str,
        *,
        paths: RepoPaths,
        timeout_seconds: int,
        max_output_bytes: int,
        now: datetime,
    ) -> NetworkCapture:
        if (
            type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or type(max_output_bytes) is not int
            or max_output_bytes <= 0
        ):
            raise ValueError("capture limits must be positive integers")
        _utc(now)
        with _CaptureDeadline(timeout_seconds) as deadline:
            return self._capture(
                requested_url,
                paths=paths,
                max_output_bytes=max_output_bytes,
                now=now,
                deadline=deadline,
            )

    def _capture(
        self,
        requested_url: str,
        *,
        paths: RepoPaths,
        max_output_bytes: int,
        now: datetime,
        deadline: _CaptureDeadline,
    ) -> NetworkCapture:
        current = canonicalize_url(requested_url)
        redirects: list[str] = []
        visited: set[str] = set()
        while True:
            deadline.remaining()
            if current in visited:
                raise WebCaptureError("redirect cycle")
            visited.add(current)
            endpoint = self.policy.vet_url(current)
            parsed = urllib.parse.urlsplit(endpoint.url)
            target = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            response = self._request_hop(
                endpoint,
                endpoint.addresses[0],
                target=target,
                timeout_seconds=deadline.remaining(),
            )
            staged_name = None
            try:
                deadline.remaining()
                self.policy.verify_connected_peer(endpoint, response.peer_address)
                headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if response.status in {301, 302, 303, 307, 308}:
                    if len(redirects) >= self.max_redirects:
                        raise TooManyRedirects("redirect limit exceeded")
                    if not headers.get("location"):
                        raise WebCaptureError("redirect has no location")
                    location = headers["location"]
                    if _control(location):
                        raise UnsafeNetworkTarget(
                            "redirect contains control characters"
                        )
                    current = canonicalize_url(
                        urllib.parse.urljoin(endpoint.url, location)
                    )
                    redirects.append(current)
                    continue
                if not 200 <= response.status < 300:
                    raise WebCaptureError(f"HTTP status {response.status}")
                if headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise WebCaptureError("unexpected encoded response")
                media = _media_type(
                    headers.get("content-type", "application/octet-stream")
                )
                length = headers.get("content-length")
                expected_size = None
                if length is not None:
                    if not re.fullmatch(r"[0-9]+", length):
                        raise WebCaptureError("invalid Content-Length")
                    expected_size = int(length)
                    if expected_size > max_output_bytes:
                        raise WebCaptureError("response exceeds the byte limit")
                disposition = Message()
                disposition["Content-Disposition"] = headers.get(
                    "content-disposition", ""
                )
                name = (
                    disposition.get_filename()
                    or urllib.parse.unquote(PurePosixPath(parsed.path).name)
                    or "snapshot"
                )
                filename = _safe_filename(name, media)
                staged_name = uuid.uuid4().hex + "-" + filename
                with _staging_directory(paths) as (pinned, directory):
                    try:
                        fd = os.open(
                            staged_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=directory,
                        )
                        size = 0
                        with os.fdopen(fd, "wb") as output:
                            while True:
                                deadline.remaining()
                                chunk = response.body.read(
                                    min(1_048_576, max_output_bytes + 1 - size)
                                )
                                deadline.remaining()
                                if not chunk:
                                    break
                                size += len(chunk)
                                if size > max_output_bytes:
                                    raise WebCaptureError(
                                        "response exceeds the byte limit"
                                    )
                                output.write(chunk)
                            if expected_size is not None and size != expected_size:
                                raise WebCaptureError("truncated HTTP body")
                            output.flush()
                            os.fsync(output.fileno())
                        _fsync_directory(directory)
                        pinned.validate()
                    except BaseException:
                        try:
                            os.unlink(staged_name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                        raise
                return NetworkCapture(
                    paths.root / ".brain/web-staging" / staged_name,
                    _utc(now),
                    endpoint.url,
                    tuple(redirects),
                    media,
                    filename,
                )
            finally:
                try:
                    response.body.close()
                    deadline.remaining()
                except BaseException:
                    if staged_name is not None:
                        with _PinnedDirectory.open(
                            paths.root / ".brain/web-staging"
                        ) as pinned:
                            try:
                                os.unlink(staged_name, dir_fd=pinned.descriptor)
                                _fsync_directory(pinned.descriptor)
                            except FileNotFoundError:
                                pass
                    raise


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise WebCaptureError("capture timestamps must have a timezone")
    return value.astimezone(timezone.utc)


def approval_recorded_at_for(
    records: Iterable[SourceRecord], claim: ApprovalClaim, *, now: datetime
) -> datetime:
    if not isinstance(claim, ApprovalClaim):
        raise ApprovalRequired("an explicit approval claim is required")
    for name, limit in (("event_id", 128), ("scope", 4096), ("note", 8192)):
        value = getattr(claim, name)
        if (
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            or len(value) > limit
            or _control(value)
        ):
            raise ApprovalRequired(f"approval {name} must be nonempty bounded text")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", claim.event_id) is None:
        raise ApprovalRequired("invalid approval event ID")
    matches = {
        (event.approval_scope, event.approval_note, event.approval_recorded_at)
        for record in records
        for version in record.versions.values()
        for event in version.retrieval_events
        if event.approval_event_id == claim.event_id
    }
    if not matches:
        return _utc(now)
    if len(matches) != 1:
        raise ApprovalEventConflict("retained event metadata is inconsistent")
    scope, note, recorded_at = next(iter(matches))
    if (scope, note) != (claim.scope, claim.note):
        raise ApprovalEventConflict("event ID already binds a different scope or note")
    return recorded_at


def serialize_url_descriptor(
    *, canonical_url: str, description: str, added: date
) -> bytes:
    if canonicalize_url(canonical_url) != canonical_url:
        raise WebCaptureError("descriptor URL must be canonical")
    if (
        type(description) is not str
        or not description.strip()
        or _control(description)
        or any(c in description for c in "\u2028\u2029")
    ):
        raise WebCaptureError("description must be nonempty single-line text")
    if type(added) is not date:
        raise WebCaptureError("added must be a date")
    descriptor_bytes = (
        "---\n"
        "kind: url\n"
        f"url: {json.dumps(canonical_url, ensure_ascii=False)}\n"
        f"description: {json.dumps(description.strip(), ensure_ascii=False)}\n"
        f"added: {added.isoformat()}\n"
        "---\n"
    ).encode("utf-8")
    return descriptor_bytes


def publish_url_descriptor(
    paths: RepoPaths, *, canonical_url: str, description: str, added: date
) -> UrlDescriptor:
    body = serialize_url_descriptor(
        canonical_url=canonical_url, description=description, added=added
    )
    parsed = urllib.parse.urlsplit(canonical_url)
    slug = (
        re.sub(r"[^a-z0-9]+", "-", (parsed.hostname + parsed.path).lower()).strip("-")[
            :64
        ]
        or "url"
    )
    relative = PurePosixPath(
        "urls",
        f"{slug}-{hashlib.sha256(canonical_url.encode()).hexdigest()[:12]}.url.md",
    )
    with _PinnedDirectory.open(paths.raw) as pinned:
        try:
            os.mkdir("urls", mode=0o755, dir_fd=pinned.descriptor)
        except FileExistsError:
            pass
        directory = pinned.open_child(pinned.descriptor, "urls")
        try:
            prior, _ = _read_regular_at(
                directory, relative.name, label="URL descriptor", max_bytes=len(body)
            )
        except FileNotFoundError:
            prior = None
        except (OSError, ValueError) as error:
            raise WebCaptureError(
                "descriptor destination differs", code="descriptor_path_collision"
            ) from error
        if prior is not None:
            if prior != body:
                raise WebCaptureError(
                    "descriptor destination differs", code="descriptor_path_collision"
                )
            result = parse_url_descriptor(paths.raw / relative, relative)
            pinned.validate()
            return result
        temporary = ".brain-tmp-" + uuid.uuid4().hex
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            result = parse_url_descriptor(paths.raw / "urls" / temporary, relative)
            if (result.path, result.url, result.description, result.added) != (
                relative,
                canonical_url,
                description.strip(),
                added,
            ):
                raise ValueError("parsed descriptor does not match its inputs")
            pinned.validate()
            # The caller's outer SourceWriteLock serializes competing publishers.
            os.replace(
                temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory
            )
            _fsync_directory(directory)
            pinned.validate()
            return result
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def active_representation_for(record: SourceRecord) -> SourceRepresentation | None:
    try:
        if record.active_content_sha256 is None or record.active_derivation_id is None:
            return None
        version = record.versions.get(record.active_content_sha256)
        derivation = record.derivations.get(record.active_derivation_id)
        if (
            version is None
            or derivation is None
            or derivation.output_path
            != PurePosixPath("sources/extracted")
            / derive_extraction_path(
                version.raw_path, version.sha256, derivation.derivation_id
            )
        ):
            return None
        return representation_for(
            record, record.active_content_sha256, record.active_derivation_id
        )
    except (ValueError, TypeError, KeyError):
        return None


def snapshot_result_data(value):
    """Serialize the immutable snapshot contracts without a parallel model."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return value.as_posix()
    if is_dataclass(value):
        return {
            field.name: snapshot_result_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: snapshot_result_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [snapshot_result_data(item) for item in value]
    return value


def snapshot_request_identity(
    *,
    paths: RepoPaths,
    selector: Mapping[str, str],
    approval: ApprovalClaim,
    rendered: RenderedCapture | None,
) -> dict[str, JSONValue]:
    browser = None
    if rendered is not None:
        absolute = (
            rendered.staging_path
            if rendered.staging_path.is_absolute()
            else paths.root / rendered.staging_path
        )
        try:
            relative = absolute.relative_to(paths.root / ".brain/web-staging")
            if not relative.parts or ".." in absolute.parts:
                raise ValueError("noncanonical staging path")
        except ValueError as error:
            raise WebCaptureError(
                "rendered staging must be below .brain/web-staging",
                code="web_staging_invalid",
            ) from error
        browser = {
            "staging_path": (PurePosixPath(".brain/web-staging") / relative).as_posix(),
            "handoff_id": rendered.handoff_id,
            "retrieved_at": snapshot_result_data(_utc(rendered.retrieved_at)),
            "final_url": canonicalize_url(rendered.final_url),
            "redirects": [canonicalize_url(url) for url in rendered.redirects],
            "detected_media_type": _media_type(rendered.detected_media_type),
        }
    identity = {
        "selector": dict(selector),
        "approval": snapshot_result_data(approval),
        "rendered": browser,
    }
    validate_snapshot_request_identity(identity)
    return identity


def validate_snapshot_request_identity(identity: Mapping[str, JSONValue]) -> None:
    if set(identity) != {"selector", "approval", "rendered"}:
        raise ValueError("snapshot request identity fields are invalid")
    selector = identity["selector"]
    approval = identity["approval"]
    browser = identity["rendered"]
    if not isinstance(selector, dict) or selector.get("kind") not in {
        "source_id",
        "url",
    }:
        raise ValueError("snapshot selector is invalid")
    source_selector = selector["kind"] == "source_id"
    if set(selector) != {
        "kind",
        "url",
        "source_id" if source_selector else "description",
    }:
        raise ValueError("snapshot selector fields are invalid")
    if canonicalize_url(selector["url"]) != selector["url"]:
        raise ValueError("snapshot selector URL must be canonical")
    if source_selector:
        if (
            not isinstance(selector["source_id"], str)
            or re.fullmatch(r"src_[0-9a-f]{64}", selector["source_id"]) is None
        ):
            raise ValueError("snapshot source identity is invalid")
    else:
        serialize_url_descriptor(
            canonical_url=selector["url"],
            description=selector["description"],
            added=date(2000, 1, 1),
        )
        if selector["description"] != selector["description"].strip():
            raise ValueError("snapshot description must be canonical")
    if not isinstance(approval, dict) or set(approval) != {"event_id", "scope", "note"}:
        raise ValueError("snapshot approval identity is invalid")
    approval_recorded_at_for(
        (), ApprovalClaim(**approval), now=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )
    if browser is None:
        return
    if not isinstance(browser, dict) or set(browser) != {
        "staging_path",
        "handoff_id",
        "retrieved_at",
        "final_url",
        "redirects",
        "detected_media_type",
    }:
        raise ValueError("snapshot rendered identity fields are invalid")
    raw_path = browser["staging_path"]
    if not isinstance(raw_path, str):
        raise ValueError("snapshot staging identity is invalid")
    path = PurePosixPath(raw_path)
    if (
        path.as_posix() != raw_path
        or path.parts[:2] != (".brain", "web-staging")
        or len(path.parts) < 3
        or ".." in path.parts
    ):
        raise ValueError("snapshot staging identity is invalid")
    if browser["handoff_id"] is not None and (
        not isinstance(browser["handoff_id"], str)
        or re.fullmatch(r"hnd_[0-9a-f]{64}", browser["handoff_id"]) is None
    ):
        raise ValueError("snapshot rendered handoff identity is invalid")
    timestamp = browser["retrieved_at"]
    if (
        not isinstance(timestamp, str)
        or snapshot_result_data(
            _utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00")))
        )
        != timestamp
    ):
        raise ValueError("snapshot rendered timestamp is not canonical")
    if not isinstance(browser["redirects"], list):
        raise ValueError("snapshot rendered redirects are invalid")
    for url in [browser["final_url"], *browser["redirects"]]:
        if canonicalize_url(url) != url:
            raise ValueError("snapshot rendered URL is not canonical")
    if _media_type(browser["detected_media_type"]) != browser["detected_media_type"]:
        raise ValueError("snapshot rendered media type is not canonical")


def snapshot_url(
    request: SnapshotRequest,
    *,
    descriptor: UrlDescriptorMetadata,
    record: SourceRecord,
    paths: RepoPaths,
    registry: ExtractorRegistry,
    processor: SourceProcessor,
    prerequisite_digest: Callable[[ExtractorSpec], str],
    records: Mapping[str, SourceRecord],
    checkpoint: Callable[[SourceRecord], None],
    transport: WebTransport,
    now: datetime,
    event_sink: Callable[[str, Mapping[str, JSONValue]], None] | None = None,
    event_commit: Callable[[str, Mapping[str, JSONValue]], None] | None = None,
    prepare_continuation: Callable[[SnapshotResult, SourceRecord], None] | None = None,
) -> SnapshotResult:
    approval_at = approval_recorded_at_for(records.values(), request.approval, now=now)
    now = _utc(now)
    record.to_dict()
    if (
        request.source_id != record.source_id
        or descriptor is None
        or record.url_descriptor != descriptor
    ):
        raise WebCaptureError("snapshot requires the current descriptor-backed source")
    requested_url = canonicalize_url(descriptor.url)
    recipe = registry.select(
        "application/x.second-brain-url-descriptor", record.current_raw_path
    )
    if recipe is None:
        raise WebCaptureError("URL capture recipe is unavailable")
    rendered = request.rendered
    if rendered is not None:
        if rendered.handoff_id is not None:
            _require_render_handoff(
                paths, rendered.handoff_id, record, registry, prerequisite_digest
            )
        capture = NetworkCapture(
            rendered.staging_path,
            _utc(rendered.retrieved_at),
            canonicalize_url(rendered.final_url),
            tuple(canonicalize_url(url) for url in rendered.redirects),
            _media_type(rendered.detected_media_type),
            _safe_filename(
                rendered.staging_path.name, _media_type(rendered.detected_media_type)
            ),
        )
    else:
        capture = transport.capture(
            requested_url,
            paths=paths,
            timeout_seconds=recipe.timeout_seconds,
            max_output_bytes=recipe.max_output_bytes,
            now=now,
        )
    final_url = canonicalize_url(capture.final_url)
    redirects = tuple(canonicalize_url(url) for url in capture.redirects)
    media = _media_type(capture.detected_media_type)
    body = _read_capture_staging(
        paths, capture.staging_path, limit=recipe.max_output_bytes
    )
    if rendered is not None:
        _validate_rendered_bytes(body, media, capture.staging_path)
    checksum = hashlib.sha256(body).hexdigest()
    retrieval = RetrievalMetadata(
        requested_url,
        final_url,
        redirects,
        _utc(capture.retrieved_at),
        media,
        len(body),
        checksum,
        request.approval.event_id,
        approval_at,
        request.approval.scope,
        request.approval.note,
    )
    existing = record.versions.get(checksum)
    if existing is not None:
        _verify_web_version(paths, record.source_id, existing)
        if existing.byte_size != retrieval.byte_size:
            raise WebCaptureError(
                "retained version size differs", code="web_snapshot_integrity_error"
            )
        version = replace(
            existing, retrieval_events=existing.retrieval_events + (retrieval,)
        )
    else:
        relative = PurePosixPath(
            "_web",
            record.source_id,
            checksum,
            _safe_filename(capture.safe_filename, media),
        )
        try:
            _atomic_write_archive(paths.root, relative, body)
            observation = stable_file_snapshot(
                paths, SnapshotNamespace.RAW_WEB, relative, include_sha256=True
            )
            if observation.sha256 != checksum or observation.byte_size != len(body):
                raise ValueError("published snapshot changed")
        except (OSError, ValueError) as error:
            raise WebCaptureError(str(error), code="web_snapshot_collision") from error
        version = ContentVersion(
            checksum,
            relative,
            len(body),
            FileFingerprint(relative, len(body), observation.mtime_ns),
            now,
            (retrieval,),
        )
    updated = replace(
        record,
        versions={**record.versions, checksum: version},
        active_content_sha256=checksum,
        active_derivation_id=None,
        state=SourceState.PENDING,
        media_type=media,
        byte_size=len(body),
        inspected_at=now,
        updated_at=now,
    )
    reusable = _reusable_derivation(record, version, paths)
    result: ProcessResult | None = None

    def snapshot_result(final, extraction_result):
        updated_records = {**records, final.source_id: final}
        return SnapshotResult(
            final.source_id,
            version.raw_path,
            checksum,
            final.versions[checksum],
            retrieval,
            extraction_result,
            active_representation_for(final),
            compute_corpus_revision(updated_records.values()),
        )

    def prepare_result(final, extraction_result):
        if prepare_continuation is not None:
            prepare_continuation(snapshot_result(final, extraction_result), final)

    if reusable is not None:
        final = _reuse_derivation(
            record,
            updated,
            reusable,
            paths,
            checkpoint,
            now,
            prepare_result=prepare_result,
            event_sink=event_sink,
            event_commit=event_commit,
        )
    else:
        previous_codes = (
            set(record.last_attempt.diagnostic_codes) if record.last_attempt else set()
        )
        updated = replace(
            updated,
            diagnostics=tuple(
                d
                for d in record.diagnostics
                if d.code not in previous_codes | {"web_capture_awaiting_approval"}
            ),
        )
        _checkpoint(paths, checkpoint, updated)
        extractor = registry.select(media, version.raw_path)
        if extractor is None:
            final = replace(
                updated,
                state=SourceState.UNSUPPORTED,
                diagnostics=updated.diagnostics
                + (
                    Diagnostic(
                        "unsupported_media_type",
                        "No extractor supports this capture.",
                        version.raw_path,
                    ),
                ),
            )
            prepare_result(final, None)
            _checkpoint(paths, checkpoint, final)
        else:
            item = InventoryItem(
                version.fingerprint, media, version.raw_path.suffix.lower(), checksum
            )
            final, result = _process_web_record(
                updated,
                item,
                extractor,
                paths=paths,
                processor=processor,
                digest=prerequisite_digest(extractor),
                checkpoint=checkpoint,
                now=now,
                rendering_required=rendered is None
                and media in {"text/html", "application/xhtml+xml"}
                and _requires_rendering(body),
                prepare_result=prepare_result,
                event_sink=event_sink,
                event_commit=event_commit,
            )
    _prove_checkpoint(paths, final)
    return snapshot_result(final, result)


def _read_capture_staging(paths: RepoPaths, path: Path, *, limit: int) -> bytes:
    absolute = path if path.is_absolute() else paths.root / path
    try:
        relative = absolute.relative_to(paths.root / ".brain/web-staging")
        if not relative.parts or any(part in {".", ".."} for part in absolute.parts):
            raise ValueError("staging must be strictly below .brain/web-staging")
        with _PinnedDirectory.open(absolute.parent) as pinned:
            body, _ = _read_regular_at(
                pinned.descriptor, absolute.name, label="web staging", max_bytes=limit
            )
            pinned.validate()
            return body
    except (OSError, ValueError) as error:
        raise WebCaptureError(
            f"staging scope/read failed: {error}", code="web_staging_invalid"
        ) from error


class _HTMLFacts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.html = False
        self.script = False
        self.hidden = 0
        self.visible: list[str] = []
        self.prefix_text = False

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            self.html = True
        if tag == "script":
            self.script = True
        if tag in {"script", "style", "head", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.html and data.strip():
            self.prefix_text = True
        if not self.hidden:
            self.visible.append(data)


def _requires_rendering(body: bytes) -> bool:
    try:
        facts = _HTMLFacts()
        facts.feed(body.decode("utf-8"))
        return facts.script and not "".join(facts.visible).strip()
    except (UnicodeDecodeError, ValueError):
        return False


def _validate_rendered_bytes(body: bytes, media: str, path: Path) -> None:
    valid = False
    if not body or path.suffix.lower() in {".md", ".markdown"}:
        raise WebCaptureError(
            "rendered capture must contain faithful document bytes",
            code="rendered_capture_invalid",
        )
    if media in {"text/html", "application/xhtml+xml"}:
        try:
            text = body.decode("utf-8-sig")
            facts = _HTMLFacts()
            facts.feed(text)
            valid = facts.html and not facts.prefix_text
        except (UnicodeDecodeError, ValueError):
            pass
    elif media == "application/pdf":
        valid = body.startswith(b"%PDF-") and b"%%EOF" in body[-1024:]
    elif media == "image/png":
        valid = (
            body.startswith(b"\x89PNG\r\n\x1a\n")
            and body[12:16] == b"IHDR"
            and b"IEND" in body[-16:]
        )
    elif media == "image/jpeg":
        valid = body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9")
    elif media == "image/tiff":
        valid = len(body) >= 8 and body[:4] in {b"II*\0", b"MM\0*"}
    elif media == "image/webp":
        valid = (
            len(body) >= 16
            and body[:4] == b"RIFF"
            and body[8:12] == b"WEBP"
            and int.from_bytes(body[4:8], "little") + 8 == len(body)
        )
    elif media in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        expected = {
            ".docx": "word/document.xml",
            ".pptx": "ppt/presentation.xml",
            ".xlsx": "xl/workbook.xml",
        }[_MIME_SUFFIX[media]]
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                valid = body.startswith(b"PK\x03\x04") and {
                    "[Content_Types].xml",
                    expected,
                } <= set(archive.namelist())
        except (OSError, ValueError, zipfile.BadZipFile):
            pass
    if not valid:
        raise WebCaptureError(
            "rendered media/signature is not an allowlisted faithful document",
            code="rendered_capture_invalid",
        )


def _require_render_handoff(paths, handoff_id, record, registry, digest_for):
    handoff = agent_handoff.load_handoff_item(paths, handoff_id)
    version = record.versions.get(record.active_content_sha256)
    expected = agent_handoff.collect_durable_handoffs(
        {record.source_id: record}, registry
    )
    if (
        handoff.kind != "rendered_web_capture"
        or version is None
        or handoff.source_id != record.source_id
        or handoff.content_sha256 != record.active_content_sha256
        or handoff.raw_path != version.raw_path
        or handoff.media_type != record.media_type
    ):
        raise agent_handoff.AgentRegistrationError(
            "rendered handoff no longer matches the current web source",
            code="handoff_source_stale",
        )
    extractor = registry.select(record.media_type, version.raw_path)
    if (
        extractor is None
        or handoff not in expected
        or digest_for(extractor) != handoff.prerequisite_digest
    ):
        raise agent_handoff.AgentRegistrationError(
            "rendered capture recipe changed", code="handoff_recipe_stale"
        )
    _verify_web_version(paths, record.source_id, version)


def _verify_web_version(paths, source_id, version):
    try:
        if version.raw_path.parts[:3] != ("_web", source_id, version.sha256):
            raise ValueError("version does not name this source's web evidence")
        observed = stable_file_snapshot(
            paths,
            SnapshotNamespace.RAW_WEB,
            version.raw_path,
            include_sha256=True,
            expected_fingerprint=version.fingerprint,
        )
        if (
            observed.logical_path,
            observed.sha256,
            observed.byte_size,
            observed.mtime_ns,
        ) != (
            version.raw_path,
            version.sha256,
            version.byte_size,
            version.fingerprint.mtime_ns,
        ):
            raise ValueError("retained web bytes differ")
        return observed
    except (OSError, ValueError) as error:
        raise WebCaptureError(
            str(error), code="web_snapshot_integrity_error"
        ) from error


def _prove_checkpoint(paths: RepoPaths, record: SourceRecord) -> None:
    expected = _canonical_record_payload(record)
    with _PinnedDirectory.open(paths.ledger_dir) as pinned:
        observed, _ = _read_regular_at(
            pinned.descriptor,
            record.source_id + ".json",
            label="snapshot checkpoint",
            synchronize=True,
        )
        if observed != expected:
            raise WebCaptureError(
                "snapshot checkpoint does not match expected durable record"
            )
        pinned.validate()


def _checkpoint(paths, checkpoint, record):
    checkpoint(record)
    _prove_checkpoint(paths, record)


def _reusable_derivation(record, version, paths):
    candidates = sorted(
        (d for d in record.derivations.values() if d.source_sha256 == version.sha256),
        key=lambda d: (
            d.derivation_id == record.active_derivation_id,
            d.created_at,
            d.derivation_id,
        ),
        reverse=True,
    )
    for derivation in candidates:
        canonical = PurePosixPath("sources/extracted") / derive_extraction_path(
            version.raw_path, version.sha256, derivation.derivation_id
        )
        if derivation.output_path != canonical:
            continue
        try:
            observed = stable_file_snapshot(
                paths,
                SnapshotNamespace.EXTRACTED,
                derivation.output_path,
                include_sha256=True,
            )
            if (observed.sha256, observed.byte_size, observed.mtime_ns) == (
                derivation.output_sha256,
                derivation.output_byte_size,
                derivation.output_mtime_ns,
            ):
                return derivation, observed.identity
        except (OSError, ValueError):
            continue
    return None


def _compensate(guard, paths, checkpoint):
    try:
        checkpoint(guard.rollback)
    except Exception:
        checkpoint(guard.rollback)
    guard._clear_after_checkpoint(guard.rollback)


def _activation_event(record):
    representation = active_representation_for(record)
    if representation is None:
        raise ValueError("web activation did not create a canonical representation")
    return {
        **_representation_data(representation),
        "record_sha256": hashlib.sha256(_canonical_record_payload(record)).hexdigest(),
    }


def _reuse_derivation(
    predecessor,
    rollback,
    reusable,
    paths,
    checkpoint,
    now,
    *,
    prepare_result,
    event_sink,
    event_commit,
):
    derivation, identity = reusable
    version = rollback.versions[rollback.active_content_sha256]
    candidate = activate_derivation(rollback, derivation, now=now)
    guard = None
    event_data = None
    checkpoint_attempted = False

    def use_raw(raw):
        if raw.snapshot.sha256 != version.sha256:
            raise InventoryAccessError("retained web input changed")

        def use_output(output):
            nonlocal guard, event_data, checkpoint_attempted
            _validate_pinned_process_artifact(derivation, output)
            if output.snapshot.identity != identity:
                raise InventoryAccessError("retained output identity changed")
            if predecessor.active_derivation_id != derivation.derivation_id:
                event_data = _activation_event(candidate)
                if event_sink is not None:
                    event_sink("new_active_representation", event_data)
            prepare_result(candidate, None)
            if event_data is not None:
                guard = ActivationGuard.prepare_reactivation(
                    paths, predecessor, rollback, candidate
                )
            checkpoint_attempted = True
            _checkpoint(paths, checkpoint, candidate)

        use_stable_file(
            paths,
            SnapshotNamespace.EXTRACTED,
            derivation.output_path,
            use_output,
            include_sha256=True,
        )

    try:
        use_stable_file(
            paths,
            SnapshotNamespace.RAW_WEB,
            version.raw_path,
            use_raw,
            include_sha256=True,
            expected_fingerprint=version.fingerprint,
        )
        if guard is not None:
            guard._clear_after_checkpoint(candidate)
    except BaseException:
        if guard is not None:
            _compensate(guard, paths, checkpoint)
        elif checkpoint_attempted:
            # Existing-active reuse changes only retrieval metadata. On failed
            # evidence proof, remove its active pointer before returning the error.
            _checkpoint(paths, checkpoint, rollback)
        raise
    if event_data is not None and event_commit is not None:
        event_commit("new_active_representation", event_data)
    return candidate


def _process_web_record(
    record,
    item,
    extractor,
    *,
    paths,
    processor,
    digest,
    checkpoint,
    now,
    rendering_required,
    prepare_result,
    event_sink,
    event_commit,
):
    from ..extractors.processor import borrowed_staged_input, stage_pinned_input

    guard = None
    final = None
    result = None
    staged = None
    event_data = None

    def use_raw(pinned):
        nonlocal guard, final, result, staged
        if pinned.snapshot.sha256 != record.active_content_sha256:
            raise InventoryAccessError(
                "pinned web input differs from the retained version"
            )
        context = ProcessingContext(
            record.active_content_sha256,
            effective_extractor_version(extractor, digest),
            extractor.config_sha256,
            digest,
            now,
            pinned.descriptor_path,
            pinned.descriptor,
        )
        extracting = _extracting_record(record, extractor, context)
        _checkpoint(paths, checkpoint, extracting)
        if rendering_required:
            diagnostic = Diagnostic(
                "web_rendering_required",
                "The HTML shell requires a faithful rendered document capture.",
                item.fingerprint.path,
            )
            attempt = replace(
                extracting.last_attempt,
                outcome=SourceState.NEEDS_AGENT,
                diagnostic_codes=(diagnostic.code,),
            )
            result = ProcessResult(
                SourceState.NEEDS_AGENT, None, attempt, (diagnostic,)
            )
        else:
            staged = stage_pinned_input(
                context,
                paths=paths,
                logical_path=item.fingerprint.path,
                expected_byte_size=item.fingerprint.byte_size,
                namespace=SnapshotNamespace.RAW_WEB,
            )
            with borrowed_staged_input(staged):
                result = processor.process(
                    extracting, item, extractor, paths=paths, context=context
                )
        _validate_process_result(
            result,
            extractor=extractor,
            context=context,
            raw_path=item.fingerprint.path,
            paths=paths,
        )
        prepared = replace(
            extracting,
            last_attempt=result.attempt,
            diagnostics=_replace_attempt_diagnostics(
                record.diagnostics, record.last_attempt, result.diagnostics
            ),
        )
        if result.derivation is None:
            final = replace(prepared, state=result.state)
            return
        derivation = result.derivation

        def activate(artifact):
            nonlocal guard, final, event_data
            _validate_pinned_process_artifact(derivation, artifact)
            final = activate_derivation(prepared, derivation, now=now)
            event_data = _activation_event(final)
            if event_sink is not None:
                event_sink("new_active_representation", event_data)
            prepare_result(final, result)
            guard = ActivationGuard.prepare(paths, extracting, final)
            _checkpoint(paths, checkpoint, final)
            if staged is not None:
                staged.revalidate()

        use_stable_file(
            paths,
            SnapshotNamespace.EXTRACTED,
            derivation.output_path,
            activate,
            include_sha256=True,
        )

    try:
        use_stable_file(
            paths,
            SnapshotNamespace.RAW_WEB,
            item.fingerprint.path,
            use_raw,
            include_sha256=True,
            expected_fingerprint=item.fingerprint,
        )
        if staged is not None:
            staged.revalidate()
        if guard is not None:
            guard._clear_after_checkpoint(final)
    except BaseException:
        if guard is not None:
            _compensate(guard, paths, checkpoint)
        raise
    finally:
        if staged is not None:
            staged.close()
    assert final is not None and result is not None
    if result.derivation is None:
        prepare_result(final, result)
        _checkpoint(paths, checkpoint, final)
    elif event_commit is not None:
        event_commit("new_active_representation", event_data)
    return final, result
