"""Closed, host-independent services for Task 7 evaluation fixtures.

These services are deliberately test-only.  They never probe a host converter,
start a process, or construct a public transport.  The shim supplies the only
allowed in-memory/static web capture explicitly for the approved fixture.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from brainlib.commands import CommandServices
from brainlib.extractors.processor import CommandExecution, DeterministicSourceProcessor
from brainlib.registry import ExtractorSpec, ResolvedConverter
from brainlib.sources.web import NetworkCapture, WebTransport


class FixtureServiceError(ValueError):
    """The controlled fixture boundary was asked to do something unapproved."""


def unavailable_prerequisite_digest(extractor: ExtractorSpec) -> str:
    """Return the production unavailable identity without probing the host."""

    return hashlib.sha256(
        f"converter-v1\0unavailable\0{extractor.extractor_id}".encode("utf-8")
    ).hexdigest()


def fixture_resolve(extractor: ExtractorSpec) -> ResolvedConverter | None:
    """Resolve only committed built-ins, deliberately excluding every PDF tool."""

    if extractor.extractor_id == "pdf":
        return None
    for converter in (extractor.preferred, *extractor.fallbacks):
        if not converter.converter_id.startswith("builtin."):
            continue
        version = f"builtin:{converter.converter_id}:{extractor.extractor_version}"
        digest = hashlib.sha256(
            f"converter-v1\0{converter.converter_id}\0{version}".encode("utf-8")
        ).hexdigest()
        return ResolvedConverter(converter, version, digest)
    return None


def fixture_prerequisite_digest(extractor: ExtractorSpec) -> str:
    resolved = fixture_resolve(extractor)
    return (
        unavailable_prerequisite_digest(extractor)
        if resolved is None
        else resolved.prerequisite_digest
    )


def _forbid_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> CommandExecution:
    del cwd, timeout_seconds, max_output_bytes, pass_fds
    raise AssertionError(f"fixture service attempted to run a command: {argv!r}")


def _forbid_transport() -> WebTransport:
    raise AssertionError("fixture service attempted to construct a web transport")


def controlled_services() -> CommandServices:
    """Build services suitable for ordinary fixture commands.

    A caller must replace ``web_transport_factory`` only for the static initial
    fixture capture.  Rendered imports intentionally keep the failing factory,
    which makes an accidental transport construction observable.
    """

    return CommandServices(
        processor_factory=lambda: DeterministicSourceProcessor(
            resolve=fixture_resolve,
            run=_forbid_command,
        ),
        prerequisite_digest=fixture_prerequisite_digest,
        web_transport_factory=_forbid_transport,
    )


@dataclass(frozen=True)
class StaticFixtureTransport:
    """A one-shot local-byte transport for the approved static shell only."""

    expected_url: str
    body: bytes
    retrieved_at: datetime
    detected_media_type: str
    filename: str = "fixture-static.html"

    @staticmethod
    def _write_staging(root: Path, filename: str, body: bytes) -> Path:
        """Create the fixed staging leaf without following workspace links."""

        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            root_fd = os.open(root, directory_flags)
        except OSError as error:
            raise FixtureServiceError("fixture workspace root is unsafe") from error
        current_fd = root_fd
        leaf_fd = -1
        try:
            for component in (".brain", "web-staging"):
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except FileNotFoundError:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                try:
                    if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                        raise FixtureServiceError("fixture staging directory is unsafe")
                except BaseException:
                    os.close(next_fd)
                    raise
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            leaf_fd = os.open(
                filename,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=current_fd,
            )
            offset = 0
            while offset < len(body):
                offset += os.write(leaf_fd, body[offset:])
            os.fsync(leaf_fd)
        except OSError as error:
            raise FixtureServiceError("fixture static staging path is unsafe") from error
        finally:
            if leaf_fd >= 0:
                os.close(leaf_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)
        return root / ".brain" / "web-staging" / filename

    def capture(
        self,
        requested_url: str,
        *,
        paths,
        timeout_seconds: int,
        max_output_bytes: int,
        now: datetime,
    ) -> NetworkCapture:
        del timeout_seconds, now
        if requested_url != self.expected_url:
            raise FixtureServiceError("static fixture URL does not match descriptor")
        if len(self.body) > max_output_bytes:
            raise FixtureServiceError("static fixture exceeds product capture bound")
        staging = self._write_staging(paths.root, self.filename, self.body)
        return NetworkCapture(
            staging,
            self.retrieved_at,
            self.expected_url,
            (),
            self.detected_media_type,
            "snapshot.html",
        )
