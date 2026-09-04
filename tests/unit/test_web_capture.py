from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import socketserver
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest

from brainlib.contracts import SourceRecord, compute_corpus_revision
from brainlib.inventory import parse_url_descriptor
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore
from brainlib.sources.web import (
    ApprovalClaim,
    ApprovalEventConflict,
    ApprovalRequired,
    HTTPHop,
    NetworkPeerMismatch,
    PublicAddressNetworkPolicy,
    PublicHTTPTransport,
    RenderedCapture,
    TooManyRedirects,
    UnsafeNetworkTarget,
    WebCaptureError,
    approval_recorded_at_for,
    canonicalize_url,
    publish_url_descriptor,
)
from tests.helpers_extractors import (
    FIXED_NOW,
    ScriptedPinnedRequester,
    StaticHostResolver,
    capture_fixture,
    hop,
    run_brain_json as _run_brain_json,
    run_snapshot_fixture,
    snapshot_cli,
    web_test_services,
)


def run_brain_json(repo_root, *argv, **kwargs):
    kwargs.setdefault("services", web_test_services())
    return _run_brain_json(repo_root, *argv, **kwargs)


@pytest.mark.parametrize("phase", ("connect", "tls", "headers", "body"))
def test_capture_elapsed_deadline_interrupts_each_blocking_socket_phase(
    repo_paths, monkeypatch, phase
):
    import brainlib.sources.web as web

    released = threading.Event()
    closed = []

    def block(at):
        if phase == at:
            released.wait(3)

    class Socket:
        def settimeout(self, value):
            pass

        def connect(self, address):
            block("connect")

        def getpeername(self):
            return ("8.8.8.8", 443)

        def do_handshake(self):
            block("tls")

        def shutdown(self, how):
            released.set()

        def close(self):
            closed.append("socket")

    stream = Socket()

    class Context:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect=True):
            if do_handshake_on_connect:
                block("tls")
            return stream

    class Response(io.BytesIO):
        status = 200

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, size=-1):
            block("body")
            return super().read(size)

        def close(self):
            closed.append("response")
            super().close()

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            block("headers")
            return Response(b"slow text")

        def close(self):
            stream.close()

    monkeypatch.setattr(web.socket, "socket", lambda *args: stream)
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(web.http.client, "HTTPConnection", Connection)
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
        )
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        transport.capture(
            "https://public.test/",
            paths=repo_paths,
            timeout_seconds=1,
            max_output_bytes=100,
            now=FIXED_NOW,
        )
    assert time.monotonic() - started < 2
    assert released.is_set()
    assert "socket" in closed
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


def test_capture_uses_one_deadline_across_dns_redirects_and_final_eof(
    repo_paths, monkeypatch
):
    import brainlib.sources.web as web

    elapsed = [0.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: elapsed[0])

    def resolve(hostname, port):
        elapsed[0] += 2
        return ("8.8.8.8",)

    class SlowEOF(io.BytesIO):
        def read(self, size=-1):
            value = super().read(size)
            elapsed[0] += 2
            return value

    body = SlowEOF(b"ok")
    first = hop(302, headers={"Location": "/final"}, peer="8.8.8.8")
    hops = iter((first, HTTPHop(200, {"Content-Type": "text/plain"}, body, "8.8.8.8")))
    remaining = []

    def request(endpoint, address, *, target, timeout_seconds):
        remaining.append(timeout_seconds)
        elapsed[0] += 2
        return next(hops)

    with pytest.raises(TimeoutError):
        PublicHTTPTransport(
            policy=PublicAddressNetworkPolicy(resolve=resolve), request_hop=request
        ).capture(
            "https://public.test/",
            paths=repo_paths,
            timeout_seconds=11,
            max_output_bytes=100,
            now=FIXED_NOW,
        )
    assert remaining == [9, 5]
    assert first.body.closed and body.closed
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


def test_capture_deadline_cancels_and_reaps_stalled_dns_without_network(
    repo_paths, monkeypatch
):
    import brainlib.sources.web as web

    processes = []
    popen = subprocess.Popen

    def stalled_resolver(*args, **kwargs):
        # Characterize the old blocking resolver without making any DNS query.
        time.sleep(3)
        return [(2, 1, 6, "", ("8.8.8.8", 443))]

    def stalled_process(argv, **kwargs):
        # Exercise the real supervisor/kill/reap path using a local child.
        process = popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(3)"], **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(web.socket, "getaddrinfo", stalled_resolver)
    monkeypatch.setattr(subprocess, "Popen", stalled_process)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        PublicHTTPTransport(
            request_hop=Mock(side_effect=AssertionError("no request"))
        ).capture(
            "https://public.test/",
            paths=repo_paths,
            timeout_seconds=1,
            max_output_bytes=100,
            now=FIXED_NOW,
        )
    assert time.monotonic() - started < 2
    assert processes and all(process.poll() is not None for process in processes)
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


@pytest.mark.parametrize("phase", ("headers", "body"))
def test_slow_loopback_response_times_out_and_releases_command_lock(repo_paths, phase):
    from brainlib import commands
    from brainlib.sources.web import PublicHTTPTransport
    from tests.helpers_extractors import AllowLoopbackTestPolicy

    stopped = threading.Event()
    started_sending = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            header = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\n\r\n"
            try:
                if stopped.is_set():
                    self.request.sendall(header + b"x" * 100)
                    return
                if phase == "body":
                    self.request.sendall(header)
                else:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                started_sending.set()
                # Each byte arrives inside the old one-second inactivity limit.
                # The server has a finite independent deadline for RED runs.
                for _ in range(35):
                    self.request.sendall(b"x")
                    if stopped.wait(0.1):
                        break
            except OSError:
                pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    recipe = repo_paths.registry.read_text()
    repo_paths.registry.write_text(
        recipe.replace(
            'id = "webpage"\nversion = "1"\ntimeout_seconds = 120',
            'id = "webpage"\nversion = "1"\ntimeout_seconds = 1',
        )
    )
    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:

            def capture():
                return commands.snapshot_source_url(
                    repo_paths.root,
                    source_id=None,
                    url=f"http://127.0.0.1:{server.server_address[1]}/slow",
                    description="slow fixture",
                    approval=ApprovalClaim(
                        "evt_slow", "local test server", "approved fixture"
                    ),
                    services=web_test_services(
                        lambda: PublicHTTPTransport(policy=AllowLoopbackTestPolicy())
                    ),
                )

            result = capture()
            assert started_sending.is_set()
            assert time.monotonic() - started < 2
            assert not result.ok
            assert "deadline" in result.errors[0].message
            assert not repo_paths.lock.exists()
            assert not list((repo_paths.root / ".brain/web-staging").glob("*"))
            records = LedgerStore(repo_paths).load_all()
            assert len(records) == 1
            assert all(not record.versions for record in records.values())
            stopped.set()
            recovered = capture()
            assert recovered.ok, recovered
            assert not repo_paths.lock.exists()
        finally:
            stopped.set()
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("HTTPS://EXAMPLE.TEST:443#section", "https://example.test/"),
        (
            "http://BÜCHER.test:80/a%2Fb?q=%23x#frag",
            "http://xn--bcher-kva.test/a%2Fb?q=%23x",
        ),
        (
            "https://[2606:4700:4700::1111]:8443/a",
            "https://[2606:4700:4700::1111]:8443/a",
        ),
    ),
)
def test_url_canonicalization_preserves_path_and_query(url, expected):
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "https://name:secret@example.test/",
        "https://example.test:0/",
        "https://example.test:65536/",
        "https://example.test:/",
        "https://example.test/\nprivate",
        "https://example.test/\x7f",
        "https:///missing",
        "https://[fe80::1%25en0]/",
    ),
)
def test_invalid_url_rejected_before_resolving(url):
    resolver = StaticHostResolver({})
    with pytest.raises(ValueError):
        PublicAddressNetworkPolicy(resolve=resolver).vet_url(url)
    assert resolver.calls == []


@pytest.mark.parametrize("change", ("scope", "note", "timestamp"))
def test_reused_approval_inconsistency_precedes_transport_and_staging(
    records_with_approval_event, repo_paths, change
):
    record = next(iter(records_with_approval_event.values()))
    claim = ApprovalClaim("evt_existing", "one URL", "user approved")
    records = dict(records_with_approval_event)
    if change == "timestamp":
        version = record.versions[record.active_content_sha256]
        event = replace(
            version.retrieval_events[0],
            approval_recorded_at=FIXED_NOW + timedelta(seconds=1),
        )
        version = replace(version, retrieval_events=version.retrieval_events + (event,))
        record = replace(record, versions={version.sha256: version})
        records[record.source_id] = record
    else:
        claim = replace(claim, **{change: "different"})
    transport = Mock()
    with pytest.raises(ApprovalEventConflict):
        run_snapshot_fixture(
            record,
            approval=claim,
            paths=repo_paths,
            records=records,
            transport=transport,
            rendered=RenderedCapture(
                repo_paths.root / "absent",
                FIXED_NOW,
                "https://example.test/final",
                (),
                "text/html",
            ),
        )
    transport.capture.assert_not_called()


@pytest.mark.parametrize("alter", ("bytes", "mtime", "symlink"))
def test_retained_web_version_is_revalidated_before_reuse(
    captured_ok_descriptor_record, recording_transport, repo_paths, alter
):
    record = captured_ok_descriptor_record
    version = record.versions[record.active_content_sha256]
    target = repo_paths.raw / version.raw_path
    if alter == "bytes":
        target.write_bytes(b"changed!!!\n")
        os.utime(
            target, ns=(version.fingerprint.mtime_ns, version.fingerprint.mtime_ns)
        )
    elif alter == "mtime":
        os.utime(target, ns=(1, 1))
    else:
        body = target.read_bytes()
        target.unlink()
        outside = repo_paths.root / "replacement"
        outside.write_bytes(body)
        target.symlink_to(outside)
    with pytest.raises(WebCaptureError, match="web_snapshot_integrity_error"):
        run_snapshot_fixture(
            record,
            approval=ApprovalClaim("evt_integrity", "one URL", "approved"),
            paths=repo_paths,
            transport=recording_transport,
        )
    assert (
        len(
            LedgerStore(repo_paths)
            .load(record.source_id)
            .versions[version.sha256]
            .retrieval_events
        )
        == 1
    )


def test_reusable_derivation_does_not_trust_missing_or_changed_output(
    captured_ok_descriptor_record, recording_transport, repo_paths
):
    record = captured_ok_descriptor_record
    output = (
        repo_paths.root / record.derivations[record.active_derivation_id].output_path
    )
    output.write_bytes(b"corruption")
    result = run_snapshot_fixture(
        record,
        approval=ApprovalClaim("evt_corrupt_output", "one URL", "approved"),
        paths=repo_paths,
        transport=recording_transport,
    )
    assert result.extraction_result is not None
    assert result.active_representation is None
    assert LedgerStore(repo_paths).load(record.source_id).active_derivation_id is None


def test_reuse_checkpoints_once_and_returns_persisted_corpus(
    captured_ok_descriptor_record, recording_transport, repo_paths
):
    record = captured_ok_descriptor_record
    checkpoints = []
    store = LedgerStore(repo_paths)

    def save(updated):
        store.save(updated)
        checkpoints.append(updated)

    result = run_snapshot_fixture(
        record,
        approval=ApprovalClaim("evt_once", "one URL", "approved"),
        paths=repo_paths,
        transport=recording_transport,
        checkpoint=save,
    )
    assert len(checkpoints) == 1
    assert result.corpus_revision == compute_corpus_revision(store.load_all().values())
    assert (
        result.source_version
        == store.load(record.source_id).versions[result.content_sha256]
    )


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    (
        (302, {}, b""),
        (302, {"Location": "/"}, b""),
        (500, {}, b"failure"),
        (200, {"Content-Length": "12"}, b"short"),
        (200, {}, b"too large"),
    ),
)
def test_transport_failure_closes_response_and_removes_partial_staging(
    repo_paths, status, headers, body
):
    url = "https://public.test/"
    response = hop(status, body, peer="8.8.8.8", headers=headers)
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
        ),
        request_hop=ScriptedPinnedRequester({url: (response,)}),
    )
    with pytest.raises((ValueError, OSError)):
        transport.capture(
            url, paths=repo_paths, timeout_seconds=10, max_output_bytes=8, now=FIXED_NOW
        )
    assert response.body.closed
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


def test_filename_uses_disposition_without_path_or_control_characters(repo_paths):
    url = "https://public.test/no-suffix"
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
        ),
        request_hop=ScriptedPinnedRequester(
            {
                url: (
                    hop(
                        200,
                        b"document",
                        peer="8.8.8.8",
                        headers={
                            "Content-Type": "application/pdf",
                            "Content-Disposition": 'attachment; filename="../../unsafe\\\\report"',
                        },
                    ),
                )
            }
        ),
    )
    capture = transport.capture(
        url, paths=repo_paths, timeout_seconds=10, max_output_bytes=100, now=FIXED_NOW
    )
    assert "/" not in capture.safe_filename and "\\" not in capture.safe_filename
    assert capture.safe_filename.endswith(".pdf")


def test_descriptor_collision_never_overwrites_existing_bytes(repo_paths):
    first = publish_url_descriptor(
        repo_paths,
        canonical_url="https://example.test/",
        description="first",
        added=FIXED_NOW.date(),
    )
    before = (repo_paths.raw / first.path).read_bytes()
    with pytest.raises(WebCaptureError, match="descriptor_path_collision"):
        publish_url_descriptor(
            repo_paths,
            canonical_url="https://example.test/",
            description="changed",
            added=FIXED_NOW.date(),
        )
    assert (repo_paths.raw / first.path).read_bytes() == before


@pytest.mark.parametrize(
    ("media", "filename", "body"),
    (
        ("text/markdown", "summary.md", b"# A summary"),
        ("text/html", "summary.html", b"# A summary"),
        ("text/html", "summary.md", b"<html><body>Summary</body></html>"),
        ("application/pdf", "report.pdf", b"not PDF"),
        ("image/png", "image.png", b"not PNG"),
        ("text/html", "empty.html", b""),
    ),
)
def test_rendered_capture_rejects_nonfaithful_payloads_before_version_creation(
    descriptor_record, repo_paths, media, filename, body
):
    staging = repo_paths.root / ".brain/web-staging" / filename
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(body)
    with pytest.raises(WebCaptureError):
        run_snapshot_fixture(
            descriptor_record,
            approval=ApprovalClaim("evt_render", "one URL", "approved"),
            paths=repo_paths,
            transport=Mock(),
            rendered=RenderedCapture(
                staging, FIXED_NOW, "https://example.test/final", (), media
            ),
        )
    assert not LedgerStore(repo_paths).load(descriptor_record.source_id).versions


def test_rendered_path_symlinks_and_outside_paths_are_rejected(
    descriptor_record, rendered_staging_file, repo_paths
):
    outside = repo_paths.root / "outside.html"
    outside.write_bytes(rendered_staging_file.read_bytes())
    link = rendered_staging_file.parent / "linked.html"
    link.symlink_to(outside)
    for target in (outside, link):
        with pytest.raises(WebCaptureError):
            run_snapshot_fixture(
                descriptor_record,
                approval=ApprovalClaim("evt_path", "one URL", "approved"),
                paths=repo_paths,
                transport=Mock(),
                rendered=RenderedCapture(
                    target, FIXED_NOW, "https://example.test/final", (), "text/html"
                ),
            )


def test_web_staging_requires_explicit_namespace_and_only_reads_descriptor(repo_paths):
    from brainlib.extractors.processor import stage_pinned_input
    from brainlib.inventory import SnapshotNamespace
    from tests.helpers_extractors import make_job

    job = make_job(repo_paths, "notes/pinned.txt", b"pinned bytes\n", "text/plain")
    logical = PurePosixPath(
        "_web", job.record.source_id, job.context.input_sha256, "fact.txt"
    )
    assert not (repo_paths.raw / logical).exists()
    with pytest.raises(ValueError):
        stage_pinned_input(
            job.context, paths=repo_paths, logical_path=logical, expected_byte_size=13
        )
    with stage_pinned_input(
        job.context,
        paths=repo_paths,
        logical_path=logical,
        expected_byte_size=13,
        namespace=SnapshotNamespace.RAW_WEB,
    ) as staged:
        assert staged.logical_path == logical
        assert os.pread(staged.descriptor, 13, 0) == b"pinned bytes\n"


@pytest.mark.parametrize(
    "namespace,path",
    (
        ("raw_web", "_web/bad/bad/a.txt"),
        ("raw_user", "_web/bad/bad/a.txt"),
        ("extracted", "sources/extracted/x"),
        ("raw_version", "_versions/bad/bad/a.txt"),
    ),
)
def test_staging_rejects_invalid_or_nonraw_namespaces(repo_paths, namespace, path):
    from brainlib.extractors.processor import stage_pinned_input
    from brainlib.inventory import SnapshotNamespace
    from tests.helpers_extractors import make_job

    job = make_job(repo_paths, "notes/pinned.txt", b"bytes", "text/plain")
    with pytest.raises(ValueError):
        stage_pinned_input(
            job.context,
            paths=repo_paths,
            logical_path=PurePosixPath(path),
            expected_byte_size=5,
            namespace=SnapshotNamespace(namespace),
        )


def test_borrowed_stage_restores_nested_context_and_revalidates_on_exception(
    repo_paths, monkeypatch
):
    from brainlib.extractors.processor import borrowed_staged_input, _worker_stage
    from tests.helpers_extractors import make_job

    first = make_job(repo_paths, "first.txt", b"first", "text/plain").staged_input
    second = make_job(repo_paths, "second.txt", b"second", "text/plain").staged_input
    events = []
    original = second.revalidate

    def revalidate():
        events.append("proof")
        original()

    monkeypatch.setattr(second, "revalidate", revalidate)
    assert _worker_stage.get() is None
    with borrowed_staged_input(first):
        assert _worker_stage.get() is first
        with pytest.raises(RuntimeError, match="fixture interruption"):
            with borrowed_staged_input(second):
                assert _worker_stage.get() is second
                raise RuntimeError("fixture interruption")
        assert _worker_stage.get() is first
        assert os.pread(second.descriptor, 6, 0) == b"second"
    assert _worker_stage.get() is None
    assert events == ["proof", "proof"]


@pytest.mark.parametrize("invalid", (None, object()))
def test_borrowed_stage_rejects_non_stage_objects_without_leaking_context(invalid):
    from brainlib.extractors.processor import borrowed_staged_input, _worker_stage

    with pytest.raises(ValueError):
        with borrowed_staged_input(invalid):
            pytest.fail("non-stage object was borrowed")
    assert _worker_stage.get() is None


def test_public_requester_pins_address_verifies_tls_and_preserves_authority(
    repo_paths, monkeypatch
):
    import brainlib.sources.web as web

    events = []

    class Socket:
        def settimeout(self, value):
            pass

        def connect(self, address):
            events.append(("connect", address))

        def do_handshake(self):
            pass

        def getpeername(self):
            events.append(("peer",))
            return ("8.8.8.8", 443)

        def close(self):
            events.append(("socket_close",))

    class Context:
        def wrap_socket(self, stream, *, server_hostname, do_handshake_on_connect):
            assert not do_handshake_on_connect
            events.append(("tls", server_hostname))
            return stream

    class Response(io.BytesIO):
        status = 200

        def getheaders(self):
            return [("Content-Type", "text/plain")]

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, target, *, headers):
            events.append(("request", method, target, headers["Host"]))

        def getresponse(self):
            events.append(("response",))
            return Response(b"verified")

        def close(self):
            events.append(("connection_close",))

    monkeypatch.setattr(web.socket, "socket", lambda *args: Socket())
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(web.http.client, "HTTPConnection", Connection)
    result = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
        )
    ).capture(
        "https://public.test/a?b=1",
        paths=repo_paths,
        timeout_seconds=10,
        max_output_bytes=100,
        now=FIXED_NOW,
    )
    assert result.staging_path.read_bytes() == b"verified"
    assert events[:6] == [
        ("connect", ("8.8.8.8", 443)),
        ("peer",),
        ("tls", "public.test"),
        ("peer",),
        ("peer",),
        ("request", "GET", "/a?b=1", "public.test"),
    ]
    assert events[-1] == ("connection_close",)


@pytest.mark.parametrize("mismatch", ("tcp", "tls"))
def test_production_requester_rejects_peer_before_request_or_response(
    repo_paths, monkeypatch, mismatch
):
    import brainlib.sources.web as web

    events = []

    class Socket:
        peers = iter(("10.0.0.1",) if mismatch == "tcp" else ("8.8.8.8", "10.0.0.1"))

        def settimeout(self, value):
            pass

        def connect(self, address):
            pass

        def do_handshake(self):
            pass

        def getpeername(self):
            return (next(self.peers), 443)

        def close(self):
            events.append("close")

    class Context:
        def wrap_socket(self, stream, *, server_hostname, do_handshake_on_connect):
            assert not do_handshake_on_connect
            events.append("tls")
            return stream

    monkeypatch.setattr(web.socket, "socket", lambda *args: Socket())
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: Context())

    def forbidden(*args, **kwargs):
        pytest.fail("request/response opened before peer validation")

    monkeypatch.setattr(web.http.client, "HTTPConnection", forbidden)
    with pytest.raises(NetworkPeerMismatch):
        PublicHTTPTransport(
            policy=PublicAddressNetworkPolicy(
                resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
            )
        ).capture(
            "https://public.test/",
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=100,
            now=FIXED_NOW,
        )
    assert events == (["close"] if mismatch == "tcp" else ["tls", "close"])


def test_response_close_failure_removes_staging(repo_paths):
    class FailedClose(io.BytesIO):
        def close(self):
            super().close()
            raise OSError("close failed")

    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
        ),
        request_hop=ScriptedPinnedRequester(
            {
                "https://public.test/": (
                    HTTPHop(200, {}, FailedClose(b"body"), "8.8.8.8"),
                )
            }
        ),
    )
    with pytest.raises(OSError, match="close failed"):
        transport.capture(
            "https://public.test/",
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=100,
            now=FIXED_NOW,
        )
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


def test_html_rendered_capture_accepts_browser_doctype_and_comment(
    descriptor_record, repo_paths
):
    body = b'<!-- Browser-exported document --><!DOCTYPE html>\n<html lang="en"><body>Fact</body></html>'
    target = repo_paths.root / ".brain/web-staging/commented.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    result = run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_dom", "one URL", "approved"),
        paths=repo_paths,
        transport=Mock(),
        rendered=RenderedCapture(
            target, FIXED_NOW, "https://example.test/final", (), "text/html"
        ),
    )
    assert (repo_paths.raw / result.raw_path).read_bytes() == body


def test_missing_approval_rejects_before_network(
    descriptor_record: SourceRecord,
    repo_paths: RepoPaths,
) -> None:
    resolver = StaticHostResolver({})
    requester = ScriptedPinnedRequester({})
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    )
    with pytest.raises(ApprovalRequired):
        run_snapshot_fixture(
            descriptor_record,
            approval=ApprovalClaim("", "", ""),
            paths=repo_paths,
            transport=transport,
        )
    assert resolver.calls == []
    assert requester.calls == []


def test_active_representation_rejects_noncanonical_markdown_link(
    captured_ok_descriptor_record,
):
    from brainlib.sources.web import active_representation_for

    record = captured_ok_descriptor_record
    identifier = record.active_derivation_id
    derivation = record.derivations[identifier]
    invalid = replace(
        derivation, output_path=derivation.output_path.with_suffix(".txt")
    )
    record = replace(record, derivations={identifier: invalid})
    assert active_representation_for(record) is None


def test_same_bytes_reuse_version_and_append_retrieval_event(
    captured_descriptor_record: SourceRecord,
    local_web_server: str,
    repo_paths: RepoPaths,
) -> None:
    first = capture_fixture(
        captured_descriptor_record,
        f"{local_web_server}/same",
        event_id="evt_bounded_1",
        paths=repo_paths,
    )
    second = capture_fixture(
        first.record,
        f"{local_web_server}/same",
        event_id="evt_bounded_2",
        paths=repo_paths,
    )
    assert set(second.record.versions) == {first.result.content_sha256}
    version = second.record.versions[first.result.content_sha256]
    assert [event.approval_event_id for event in version.retrieval_events] == [
        "evt_bounded_1",
        "evt_bounded_2",
    ]
    assert second.result.source_version == version


def test_same_bytes_with_valid_derivation_does_not_run_processor(
    captured_ok_descriptor_record: SourceRecord,
    recording_transport: Mock,
    processor_spy: Mock,
    repo_paths: RepoPaths,
) -> None:
    refreshed = run_snapshot_fixture(
        captured_ok_descriptor_record,
        approval=ApprovalClaim("evt_reuse", "refresh one URL", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
        processor=processor_spy,
    )
    processor_spy.process.assert_not_called()
    assert refreshed.extraction_result is None
    assert refreshed.active_representation is not None
    assert refreshed.active_representation.content_sha256 == refreshed.content_sha256


def test_changed_bytes_create_new_immutable_version(
    captured_descriptor_record: SourceRecord,
    local_web_server: str,
    repo_paths: RepoPaths,
) -> None:
    first = capture_fixture(
        captured_descriptor_record,
        f"{local_web_server}/changed-v1",
        event_id="evt_change_1",
        paths=repo_paths,
    )
    second = capture_fixture(
        first.record,
        f"{local_web_server}/changed-v2",
        event_id="evt_change_2",
        paths=repo_paths,
    )
    assert len(second.record.versions) == 2
    assert first.result.content_sha256 != second.result.content_sha256
    assert all(
        (repo_paths.raw / version.raw_path).exists()
        for version in second.record.versions.values()
    )


def test_ok_descriptor_can_be_refreshed(
    captured_ok_descriptor_record: SourceRecord,
    recording_transport: Mock,
    repo_paths: RepoPaths,
) -> None:
    result = run_snapshot_fixture(
        captured_ok_descriptor_record,
        approval=ApprovalClaim("evt_refresh", "refresh one URL", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
    )
    assert result.source_id == captured_ok_descriptor_record.source_id


def test_one_bounded_event_id_can_cover_multiple_urls(
    repo_root: Path,
    local_web_server: str,
) -> None:
    first = snapshot_cli(
        repo_root,
        f"{local_web_server}/static",
        event_id="evt_question_7",
        acknowledge_result=True,
    )
    second = snapshot_cli(
        repo_root,
        f"{local_web_server}/report.pdf",
        event_id="evt_question_7",
    )
    assert first["retrieval"]["approval_event_id"] == "evt_question_7"
    assert second["retrieval"]["approval_event_id"] == "evt_question_7"
    assert (
        first["retrieval"]["approval_recorded_at"]
        == second["retrieval"]["approval_recorded_at"]
    )


def test_reused_event_id_rejects_changed_scope_before_network(
    records_with_approval_event: Mapping[str, SourceRecord],
    recording_transport: Mock,
) -> None:
    with pytest.raises(ApprovalEventConflict):
        approval_recorded_at_for(
            records_with_approval_event.values(),
            ApprovalClaim("evt_existing", "expanded scope", "user approved"),
            now=FIXED_NOW,
        )
    recording_transport.capture.assert_not_called()


def test_public_transport_captures_direct_url_with_original_authority(
    repo_paths: RepoPaths,
) -> None:
    url = "https://public.test/fact?q=1"
    resolver = StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
    requester = ScriptedPinnedRequester(
        {
            url: (
                hop(
                    200,
                    b"fact\n",
                    peer="8.8.8.8",
                    headers={"Content-Type": "text/plain"},
                ),
            ),
        }
    )
    capture = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    ).capture(
        url, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW
    )
    assert capture.staging_path.read_bytes() == b"fact\n"
    endpoint, address, target = requester.calls[0]
    assert (endpoint.hostname, endpoint.host_header, str(address), target) == (
        "public.test",
        "public.test",
        "8.8.8.8",
        "/fact?q=1",
    )


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.1.1/",
        "http://224.0.0.1/",
        "http://192.0.2.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
    ),
)
def test_non_public_literal_is_rejected_without_request(
    url: str,
    repo_paths: RepoPaths,
) -> None:
    requester = ScriptedPinnedRequester({})
    with pytest.raises(UnsafeNetworkTarget):
        PublicHTTPTransport(request_hop=requester).capture(
            url,
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=1024,
            now=FIXED_NOW,
        )
    assert requester.calls == []


def test_public_transport_vets_every_redirect(
    repo_paths: RepoPaths,
) -> None:
    start, final = "https://public.test/start", "https://next.test/final"
    resolver = StaticHostResolver(
        {
            ("public.test", 443): ("8.8.8.8",),
            ("next.test", 443): ("1.1.1.1",),
        }
    )
    requester = ScriptedPinnedRequester(
        {
            start: (hop(302, peer="8.8.8.8", headers={"Location": final}),),
            final: (
                hop(
                    200, b"done", peer="1.1.1.1", headers={"Content-Type": "text/plain"}
                ),
            ),
        }
    )
    capture = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    ).capture(
        start,
        paths=repo_paths,
        timeout_seconds=10,
        max_output_bytes=1024,
        now=FIXED_NOW,
    )
    assert capture.final_url == final
    assert capture.redirects == (final,)
    assert [call[0].hostname for call in requester.calls] == [
        "public.test",
        "next.test",
    ]


def test_redirect_to_private_literal_is_rejected_before_second_request(
    repo_paths: RepoPaths,
) -> None:
    start = "https://public.test/start"
    requester = ScriptedPinnedRequester(
        {
            start: (
                hop(
                    302,
                    peer="8.8.8.8",
                    headers={"Location": "http://127.0.0.1/private"},
                ),
            ),
        }
    )
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)}),
        ),
        request_hop=requester,
    )
    with pytest.raises(UnsafeNetworkTarget):
        transport.capture(
            start,
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=1024,
            now=FIXED_NOW,
        )
    assert len(requester.calls) == 1


def test_mixed_public_private_dns_answer_is_rejected_without_request(
    repo_paths: RepoPaths,
) -> None:
    requester = ScriptedPinnedRequester({})
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver(
                {
                    ("mixed.test", 443): ("8.8.8.8", "10.0.0.8"),
                }
            )
        ),
        request_hop=requester,
    )
    with pytest.raises(UnsafeNetworkTarget, match="non-public DNS answer"):
        transport.capture(
            "https://mixed.test/",
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=1024,
            now=FIXED_NOW,
        )
    assert requester.calls == []


def test_connected_peer_must_match_vetted_address_set(
    repo_paths: RepoPaths,
) -> None:
    url = "https://rebind.test/"
    requester = ScriptedPinnedRequester(
        {
            url: (
                hop(
                    200,
                    b"secret",
                    peer="10.0.0.9",
                    headers={"Content-Type": "text/plain"},
                ),
            ),
        }
    )
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver(
                {
                    ("rebind.test", 443): ("8.8.8.8",),
                }
            )
        ),
        request_hop=requester,
    )
    with pytest.raises(NetworkPeerMismatch):
        transport.capture(
            url,
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=1024,
            now=FIXED_NOW,
        )
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))


def test_redirect_limit_is_bounded(repo_paths: RepoPaths) -> None:
    first = "https://public.test/0"
    requester = ScriptedPinnedRequester(
        {
            f"https://public.test/{index}": (
                hop(302, peer="8.8.8.8", headers={"Location": f"/{index + 1}"}),
            )
            for index in range(3)
        }
    )
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver(
                {
                    ("public.test", 443): ("8.8.8.8",),
                }
            )
        ),
        request_hop=requester,
        max_redirects=2,
    )
    with pytest.raises(TooManyRedirects):
        transport.capture(
            first,
            paths=repo_paths,
            timeout_seconds=10,
            max_output_bytes=1024,
            now=FIXED_NOW,
        )
    assert len(requester.calls) == 3


@pytest.mark.parametrize(
    "description",
    (
        "colon: value",
        "hash # value",
        'quote " value',
        "brackets [value]",
        "Unicode 雪 café",
    ),
)
def test_ad_hoc_descriptor_scalars_round_trip_before_publish(
    description: str,
    repo_paths: RepoPaths,
) -> None:
    canonical_url = "https://example.test/a:b?q=%5Bvalue%5D%23quoted"
    descriptor = publish_url_descriptor(
        repo_paths,
        canonical_url=canonical_url,
        description=description,
        added=FIXED_NOW.date(),
    )
    absolute = repo_paths.raw / descriptor.path
    assert parse_url_descriptor(absolute, descriptor.path) == descriptor
    assert json.dumps(description, ensure_ascii=False) in absolute.read_text(
        encoding="utf-8",
    )


def test_invalid_descriptor_is_never_atomically_published(
    repo_paths: RepoPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "brainlib.sources.web.parse_url_descriptor",
        lambda *_: (_ for _ in ()).throw(ValueError("invalid fixture")),
    )
    with pytest.raises(ValueError, match="invalid fixture"):
        publish_url_descriptor(
            repo_paths,
            canonical_url="https://example.test/new",
            description="valid",
            added=FIXED_NOW.date(),
        )
    assert not list((repo_paths.raw / "urls").glob("*.url.md"))
    assert not list((repo_paths.raw / "urls").glob(".brain-tmp-*"))
