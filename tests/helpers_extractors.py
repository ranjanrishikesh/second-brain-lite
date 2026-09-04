from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import tempfile
import threading
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import TYPE_CHECKING

from brainlib.cli import main
from brainlib.commands import CommandServices
from brainlib.contracts import (
    Anchor,
    ContentVersion,
    Derivation,
    FileFingerprint,
    ProcessingAttempt,
    SourceRecord,
    SourceState,
    compute_sha256,
    derivation_id,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic, JSONValue
from brainlib.inventory import InventoryItem
from brainlib.layout import RepoPaths
from brainlib.ledger import derive_extraction_path
from brainlib.registry import (
    ExtractorRegistry,
    ExtractorSpec,
    ResolvedConverter,
    detect_converter_version,
    effective_extractor_version,
    is_committed_builtin_converter_id,
)
from brainlib.sync import ProcessResult, ProcessingContext
from tests.helpers import make_source_record

if TYPE_CHECKING:
    from brainlib.extractors.handoff import HandoffItem, HandoffKind
    from brainlib.extractors.processor import CommandExecution, Job, StagedInput
    from brainlib.sources.web import HTTPHop, IPAddress, VettedEndpoint

FIXED_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
STAGED_TEST_INPUTS: list[StagedInput] = []
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


ROUTES = {
    "/static": (
        200,
        {"Content-Type": "text/html"},
        b"<html><body><h1>Fact</h1></body></html>",
    ),
    "/redirect": (302, {"Location": "/report.pdf"}, b""),
    "/report.pdf": (
        200,
        {"Content-Type": "application/pdf"},
        b"%PDF-1.4\nfixture\n%%EOF\n",
    ),
    "/same": (200, {"Content-Type": "text/plain"}, b"same bytes\n"),
    "/changed-v1": (200, {"Content-Type": "text/plain"}, b"version one\n"),
    "/changed-v2": (200, {"Content-Type": "text/plain"}, b"version two\n"),
    "/render-shell": (
        200,
        {"Content-Type": "text/html"},
        b"<html><div id='app'></div><script>render()</script></html>",
    ),
}


class LocalWebServer:
    def __enter__(self) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = ROUTES.get(self.path, (404, {}, b"missing"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *args):
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join()


class StaticHostResolver:
    def __init__(self, answers: Mapping[tuple[str, int], tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.answers[(hostname, port)]


class ScriptedPinnedRequester:
    def __init__(self, hops: Mapping[str, Sequence[HTTPHop]]) -> None:
        self.hops = {url: list(values) for url, values in hops.items()}
        self.calls: list[tuple[VettedEndpoint, IPAddress, str]] = []

    def __call__(
        self,
        endpoint: VettedEndpoint,
        address: IPAddress,
        *,
        target: str,
        timeout_seconds: int,
    ) -> HTTPHop:
        self.calls.append((endpoint, address, target))
        return self.hops[endpoint.url].pop(0)


def hop(
    status: int,
    body: bytes = b"",
    *,
    peer: str = "93.184.216.34",
    headers: Mapping[str, str] | None = None,
) -> HTTPHop:
    from brainlib.sources.web import HTTPHop

    return HTTPHop(status, dict(headers or {}), io.BytesIO(body), peer)


class AllowLoopbackTestPolicy:
    """Tests only; never wire this into production CommandServices."""

    def vet_url(self, url: str) -> VettedEndpoint:
        from brainlib.sources.web import UnsafeNetworkTarget, VettedEndpoint

        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise UnsafeNetworkTarget(url)
        port = parsed.port or 80
        return VettedEndpoint(
            url,
            "http",
            "127.0.0.1",
            port,
            f"127.0.0.1:{port}",
            (ipaddress.ip_address("127.0.0.1"),),
        )

    def verify_connected_peer(
        self,
        endpoint: VettedEndpoint,
        peer_address: str,
    ) -> None:
        from brainlib.sources.web import NetworkPeerMismatch

        if ipaddress.ip_address(peer_address) not in endpoint.addresses:
            raise NetworkPeerMismatch(peer_address)


def web_test_resolve(extractor):
    for converter in (extractor.preferred, *extractor.fallbacks):
        if converter.converter_id.startswith("builtin."):
            return resolve_test_converter(replace(extractor, preferred=converter))
    return None


def web_test_digest(extractor):
    resolved = web_test_resolve(extractor)
    return "a" * 64 if resolved is None else resolved.prerequisite_digest


def web_test_processor():
    from brainlib.extractors.processor import DeterministicSourceProcessor

    return DeterministicSourceProcessor(resolve=web_test_resolve, run=FailIfCalled())


def web_test_services(transport_factory=None):
    from brainlib.sources.web import PublicHTTPTransport

    return CommandServices(
        web_test_processor,
        web_test_digest,
        web_transport_factory=transport_factory
        or (lambda: PublicHTTPTransport(policy=AllowLoopbackTestPolicy())),
    )


def make_descriptor_record(paths, url="https://example.test/report"):
    from brainlib.inventory import (
        MediaDetector,
        inventory_raw_sources,
        source_id_for_url_descriptor,
    )
    from brainlib.ledger import LedgerStore
    from brainlib.sync import _new_record

    relative = PurePosixPath("urls/report.url.md")
    target = paths.raw / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nkind: url\nurl: "
        + json.dumps(url)
        + '\ndescription: "fixture"\nadded: 2026-09-04\n---\n',
        encoding="utf-8",
    )
    item = next(
        item
        for item in inventory_raw_sources(paths, MediaDetector()).items
        if item.fingerprint.path == relative
    )
    record = _new_record(
        item,
        source_id=source_id_for_url_descriptor(item.url_descriptor),
        checksum=None,
        now=FIXED_NOW,
    )
    LedgerStore(paths).save(record)
    return record


def mock_web_transport(
    paths, body=b"same bytes\n", media_type="text/plain", filename="snapshot.txt"
):
    from unittest.mock import Mock
    from brainlib.sources.web import NetworkCapture, WebTransport

    staging = paths.root / ".brain/web-staging" / filename
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(body)
    transport = Mock(spec=WebTransport)
    transport.capture.return_value = NetworkCapture(
        staging, FIXED_NOW, "https://example.test/report", (), media_type, filename
    )
    return transport


def run_snapshot_fixture(
    record,
    *,
    approval,
    paths,
    transport,
    processor=None,
    records=None,
    checkpoint=None,
    rendered=None,
):
    from brainlib.ledger import LedgerStore
    from brainlib.sources.web import SnapshotRequest, snapshot_url

    store = LedgerStore(paths)
    store.save(record)
    return snapshot_url(
        SnapshotRequest(record.source_id, approval, rendered),
        descriptor=record.url_descriptor,
        record=record,
        paths=paths,
        registry=ExtractorRegistry.load(paths.registry),
        processor=web_test_processor() if processor is None else processor,
        prerequisite_digest=web_test_digest,
        records=store.load_all() if records is None else records,
        checkpoint=store.save if checkpoint is None else checkpoint,
        transport=transport,
        now=FIXED_NOW,
    )


def capture_fixture(record, url, *, event_id, paths):
    from brainlib.ledger import LedgerStore
    from brainlib.sources.web import ApprovalClaim, PublicHTTPTransport

    record = replace(record, url_descriptor=replace(record.url_descriptor, url=url))
    result = run_snapshot_fixture(
        record,
        approval=ApprovalClaim(event_id, "one fixture URL", "user approved"),
        paths=paths,
        transport=PublicHTTPTransport(policy=AllowLoopbackTestPolicy()),
    )
    return SimpleNamespace(
        record=LedgerStore(paths).load(record.source_id), result=result
    )


def snapshot_cli(repo_root, url, *, event_id, acknowledge_result=False):
    payload = run_brain_json(
        repo_root,
        "source",
        "snapshot-url",
        "--url",
        url,
        "--description",
        "fixture",
        "--approval-event-id",
        event_id,
        "--approval-scope",
        "one fixture URL",
        "--approval-note",
        "user approved",
        services=web_test_services(),
    )
    assert payload["ok"], payload
    if acknowledge_result:
        acknowledgement = run_brain_json(
            repo_root,
            "source",
            "acknowledge-sync-result",
            "--result-id",
            payload["data"]["result_manifest"]["result_id"],
            services=web_test_services(),
        )
        assert acknowledgement["ok"], acknowledgement
    return payload["data"]["snapshot"]


def render_handoff_id(record):
    from brainlib.extractors.handoff import collect_durable_handoffs

    paths = _WEB_HANDOFF_PATHS[record.source_id]
    return collect_durable_handoffs(
        {record.source_id: record}, ExtractorRegistry.load(paths.registry)
    )[0].handoff_id


_WEB_HANDOFF_PATHS = {}


def handoff_for_job(
    job: Job,
    *,
    kind: HandoffKind,
    reason: str,
    diagnostics: tuple[Diagnostic, ...],
) -> HandoffItem:
    from brainlib.extractors.handoff import HandoffItem, handoff_id_for

    assert job.extractor.agent_fallback
    assert job.extractor.agent_revision is not None
    fields: dict[str, JSONValue] = {
        "kind": kind,
        "source_id": job.record.source_id,
        "content_sha256": job.context.input_sha256,
        "raw_path": job.item.fingerprint.path.as_posix(),
        "media_type": job.item.media_type,
        "reason": reason,
        "required_anchor_kinds": list(job.extractor.expected_anchors),
        "extractor_id": job.extractor.extractor_id,
        "extractor_version": job.context.extractor_version,
        "config_sha256": job.extractor.config_sha256,
        "prerequisite_digest": job.context.prerequisite_digest,
        "agent_revision": job.extractor.agent_revision,
        "diagnostics": [
            {
                "code": d.code,
                "message": d.message,
                "path": None if d.path is None else d.path.as_posix(),
                "details": dict(d.details),
            }
            for d in diagnostics
        ],
    }
    return HandoffItem(
        handoff_id=handoff_id_for(fields),
        kind=kind,
        source_id=job.record.source_id,
        content_sha256=job.context.input_sha256,
        raw_path=job.item.fingerprint.path,
        media_type=job.item.media_type,
        reason=reason,
        required_anchor_kinds=job.extractor.expected_anchors,
        extractor_id=job.extractor.extractor_id,
        extractor_version=job.context.extractor_version,
        config_sha256=job.extractor.config_sha256,
        prerequisite_digest=job.context.prerequisite_digest,
        agent_revision=job.extractor.agent_revision,
        diagnostics=diagnostics,
    )


def record_for_handoff(handoff: HandoffItem, paths: RepoPaths) -> SourceRecord:
    metadata = (paths.raw / handoff.raw_path).stat()
    version = ContentVersion(
        handoff.content_sha256,
        handoff.raw_path,
        metadata.st_size,
        FileFingerprint(handoff.raw_path, metadata.st_size, metadata.st_mtime_ns),
        FIXED_NOW,
        (),
    )
    attempt = ProcessingAttempt(
        handoff.content_sha256,
        handoff.extractor_id,
        handoff.extractor_version,
        handoff.config_sha256,
        handoff.prerequisite_digest,
        SourceState.NEEDS_AGENT,
        FIXED_NOW,
        tuple(item.code for item in handoff.diagnostics),
    )
    return replace(
        make_source_record(source_id=handoff.source_id),
        current_raw_path=handoff.raw_path,
        media_type=handoff.media_type,
        byte_size=metadata.st_size,
        state=SourceState.NEEDS_AGENT,
        versions={handoff.content_sha256: version},
        active_content_sha256=handoff.content_sha256,
        derivations={},
        active_derivation_id=None,
        last_attempt=attempt,
        diagnostics=handoff.diagnostics,
    )


def resolve_test_converter(extractor: ExtractorSpec) -> ResolvedConverter:
    """Bind fixtures to their selected converter without probing optional tools."""

    converter = extractor.preferred
    version = (
        detect_converter_version(
            converter, extractor_version=extractor.extractor_version
        )
        if is_committed_builtin_converter_id(converter.converter_id)
        else "fixture-1"
    )
    digest = hashlib.sha256(
        f"converter-v1\0{converter.converter_id}\0{version}".encode("utf-8")
    ).hexdigest()
    return ResolvedConverter(converter, version, digest)


def select_test_converter(job: Job, converter_id: str) -> tuple[Job, ResolvedConverter]:
    """Choose a configured fallback while preserving Plan 2 prerequisite identity."""

    converter = next(
        item
        for item in (job.extractor.preferred, *job.extractor.fallbacks)
        if item.converter_id == converter_id
    )
    resolved = resolve_test_converter(replace(job.extractor, preferred=converter))
    return replace(
        job,
        context=replace(
            job.context,
            prerequisite_digest=resolved.prerequisite_digest,
            extractor_version=effective_extractor_version(
                job.extractor, resolved.prerequisite_digest
            ),
        ),
    ), resolved


def stage_test_input_from_verified_bytes(
    paths: RepoPaths,
    logical_path: PurePosixPath,
    body: bytes,
) -> StagedInput:
    from brainlib.extractors.processor import StagedInput

    directory = Path(tempfile.mkdtemp(prefix=".brain-test-stage-", dir=paths.root))
    os.chmod(directory, 0o700)
    checksum = hashlib.sha256(body).hexdigest()
    temporary = directory / "copy"
    with temporary.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    destination = directory / checksum
    os.chmod(temporary, 0o400)
    os.replace(temporary, destination)
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    descriptor_path = Path("/dev/fd") / str(descriptor)
    staged = StagedInput(
        logical_path,
        checksum,
        len(body),
        descriptor,
        descriptor_path,
        _stage_path=destination,
    )
    STAGED_TEST_INPUTS.append(staged)
    staged.revalidate()
    return staged


def make_job(
    paths: RepoPaths,
    relative: str,
    body: bytes,
    media_type: str,
    *,
    prerequisite: str | None = None,
) -> Job:
    from brainlib.extractors.processor import Job

    relative_path = PurePosixPath(relative)
    absolute = paths.raw / relative_path
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(body)
    stat = absolute.stat()
    sha256 = compute_sha256(absolute)
    fingerprint = FileFingerprint(relative_path, stat.st_size, stat.st_mtime_ns)
    item = InventoryItem(fingerprint, media_type, absolute.suffix.lower(), sha256)
    extractor = ExtractorRegistry.load(paths.registry).select(media_type, relative_path)
    assert extractor is not None
    if prerequisite is None:
        prerequisite = resolve_test_converter(extractor).prerequisite_digest
    version = ContentVersion(
        sha256, relative_path, stat.st_size, fingerprint, FIXED_NOW, ()
    )
    record = replace(
        make_source_record(source_id=source_id_for_first_seen(relative_path, sha256)),
        current_raw_path=relative_path,
        previous_raw_paths=(),
        media_type=media_type,
        byte_size=stat.st_size,
        state=SourceState.PENDING,
        versions={sha256: version},
        active_content_sha256=sha256,
        derivations={},
        active_derivation_id=None,
        last_attempt=None,
        diagnostics=(),
        url_descriptor=None,
    )
    staged_input = stage_test_input_from_verified_bytes(paths, relative_path, body)
    context = ProcessingContext(
        sha256,
        effective_extractor_version(extractor, prerequisite),
        extractor.config_sha256,
        prerequisite,
        FIXED_NOW,
        staged_input.descriptor_path,
        staged_input.descriptor,
    )
    return Job(record, item, extractor, context, staged_input)


def make_jobs(paths: RepoPaths, count: int = 6) -> list[Job]:
    return [
        make_job(paths, f"notes/{index}.txt", f"item {index}\n".encode(), "text/plain")
        for index in range(count)
    ]


def successful_process_result(job: Job, *, paths: RepoPaths) -> ProcessResult:
    resolved = resolve_test_converter(job.extractor)
    identifier = derivation_id(
        source_sha256=job.context.input_sha256,
        extractor_id=job.extractor.extractor_id,
        extractor_version=job.context.extractor_version,
        config_sha256=job.extractor.config_sha256,
    )
    relative = derive_extraction_path(
        job.item.fingerprint.path,
        job.context.input_sha256,
        identifier,
    )
    output = paths.extracted / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('<a id="line:1"></a>\nfixture\n', encoding="utf-8")
    stat = output.stat()
    derivation = Derivation(
        identifier,
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        paths.repo_relative(output),
        compute_sha256(output),
        stat.st_size,
        stat.st_mtime_ns,
        "ok",
        (Anchor("line", "1"),),
        job.context.attempted_at,
        method="deterministic",
        method_metadata={
            "converter_id": resolved.converter.converter_id,
            "converter_version": resolved.detected_version,
        },
    )
    attempt = ProcessingAttempt(
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        job.context.prerequisite_digest,
        SourceState.OK,
        job.context.attempted_at,
        (),
    )
    return ProcessResult(SourceState.OK, derivation, attempt, ())


@dataclass(frozen=True)
class InProcessResult:
    returncode: int
    stdout: str
    stderr: str


def run_brain(
    repo_root: Path,
    *argv: str,
    services: CommandServices | None = None,
) -> InProcessResult:
    stdout, stderr = io.StringIO(), io.StringIO()
    returncode = main(
        list(argv),
        cwd=repo_root,
        stdout=stdout,
        stderr=stderr,
        services=services,
    )
    return InProcessResult(returncode, stdout.getvalue(), stderr.getvalue())


def run_brain_json(
    repo_root: Path,
    *argv: str,
    services: CommandServices | None = None,
    expected_returncodes: frozenset[int] = frozenset({0, 1}),
) -> dict[str, JSONValue]:
    result = run_brain(repo_root, "--json", *argv, services=services)
    assert result.returncode in expected_returncodes, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"command", "ok", "data", "warnings", "errors"}
    return payload


class FailIfCalled:
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
        max_output_bytes: int,
        pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution:
        del pass_fds
        raise AssertionError(f"unexpected command: {argv!r}")


class RecordingRun:
    def __init__(self, *, markdown: str, returncode: int = 0) -> None:
        self.markdown = markdown
        self.returncode = returncode
        self.argv: tuple[str, ...] | None = None
        self.cwd: Path | None = None
        self.output_path: Path | None = None
        self.max_output_bytes: int | None = None

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
        max_output_bytes: int,
        pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution:
        from brainlib.extractors.processor import CommandExecution

        assert pass_fds
        self.argv = argv
        self.cwd = cwd
        self.max_output_bytes = max_output_bytes
        self.output_path = Path(argv[-1])
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.returncode == 0:
            self.output_path.write_text(self.markdown, encoding="utf-8")
        return CommandExecution(self.returncode, b"", b"")
