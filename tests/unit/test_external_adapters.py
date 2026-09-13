from __future__ import annotations

import importlib
import io
import os
import signal
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from brainlib.contracts import Anchor, SourceState
from brainlib.extractors.processor import CommandExecution, DeterministicSourceProcessor
from tests.helpers_extractors import (
    FailIfCalled,
    RecordingRun,
    make_job,
    resolve_test_converter,
    select_test_converter,
)


@pytest.fixture
def adapters():
    return importlib.import_module("brainlib.extractors.adapters")


def test_pdf_uses_resolved_converter_and_page_anchors(
    adapters,
    pdf_job,
    pdf_resolved,
    recording_run,
    repo_paths,
) -> None:
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=recording_run,
        resolved=pdf_resolved,
    )
    assert recording_run.argv == (
        "pdftotext",
        "-layout",
        "-enc",
        "UTF-8",
        str(pdf_job.staged_input.descriptor_path),
        str(recording_run.output_path),
    )
    assert result.state is SourceState.OK
    assert result.derivation is not None
    assert result.derivation.method == "deterministic"
    assert result.derivation.method_metadata == {
        "converter_id": pdf_resolved.converter.converter_id,
        "converter_version": pdf_resolved.detected_version,
    }
    assert result.derivation.anchors == (Anchor("page", "1"),)


def test_empty_converter_output_requests_configured_agent(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown=""),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.NEEDS_AGENT
    assert result.derivation is None
    assert result.diagnostics[0].code == "empty_extraction"


def test_empty_converter_output_without_agent_fallback_is_failed(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    job = replace(
        pdf_job,
        extractor=replace(
            pdf_job.extractor, agent_fallback=False, agent_revision=None
        ),
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=RecordingRun(markdown=""),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.FAILED
    assert result.derivation is None
    assert result.diagnostics[0].code == "empty_extraction"


def test_pdf_literal_markers_cannot_replace_actual_multipage_boundaries(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(
            markdown='First page quotes <a id="page:999"></a>\f'
            'Second page quotes <a id="page:1"></a>\f'
        ),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("page", "1"), Anchor("page", "2"))
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert '&lt;a id="page:999"&gt;&lt;/a&gt;' in markdown
    assert '&lt;a id="page:1"&gt;&lt;/a&gt;' in markdown
    assert markdown.count('<a id="page:1"></a>') == 1
    assert markdown.count('<a id="page:2"></a>') == 1
    assert markdown.index("First page") < markdown.index('<a id="page:2"></a>')
    assert markdown.index('<a id="page:2"></a>') < markdown.index("Second page")


def test_pdf_plaintext_angle_brackets_cannot_hide_page_anchors(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown="State dimension <n\fSecond page\f"),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("page", "1"), Anchor("page", "2"))
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "State dimension &lt;n" in markdown
    assert markdown.count('<a id="page:1"></a>') == 1
    assert markdown.count('<a id="page:2"></a>') == 1


def test_python_driver_quotes_source_markers_before_creating_page_metadata(
    pdf_job, monkeypatch, adapters
):
    from brainlib.extractors.python_driver import extract_python_payload

    monkeypatch.setitem(
        sys.modules,
        "fitz",
        SimpleNamespace(
            open=lambda path: type(
                "Document",
                (),
                {
                    "is_encrypted": False,
                    "__iter__": lambda self: iter(
                        [
                            SimpleNamespace(
                                get_text=lambda kind: '<a id="page:999"></a> One <n'
                            ),
                            SimpleNamespace(get_text=lambda kind: "Two"),
                        ]
                    ),
                    "close": lambda self: None,
                },
            )()
        ),
    )
    payload = extract_python_payload(
        "python.pymupdf", pdf_job.staged_input.descriptor_path, max_output_bytes=4096
    )
    assert '&lt;a id="page:999"&gt;&lt;/a&gt;' in payload.markdown
    assert "One &lt;n" in payload.markdown
    assert payload.anchors == (Anchor("page", "1"), Anchor("page", "2"))
    adapters.validate_markdown(
        payload.markdown,
        payload.anchors,
        expected_anchors=("page",),
        max_output_bytes=4096,
    )


def test_processor_rejects_resolver_drift_before_execution(
    pdf_job,
    recording_run,
    repo_paths,
    pdf_resolved,
) -> None:
    drifted = replace(pdf_resolved, prerequisite_digest="b" * 64)
    processor = DeterministicSourceProcessor(
        run=recording_run, resolve=lambda _: drifted
    )
    result = processor.process(
        pdf_job.record,
        pdf_job.item,
        pdf_job.extractor,
        paths=repo_paths,
        context=pdf_job.context,
    )
    assert result.diagnostics[0].code == "prerequisite_changed"
    assert recording_run.argv is None


def test_every_ledger_anchor_has_one_navigable_html_id(
    adapters,
    pdf_job,
    pdf_resolved,
    recording_run,
    repo_paths,
) -> None:
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=recording_run,
        resolved=pdf_resolved,
    )
    assert result.derivation is not None
    markdown = (repo_paths.root / result.derivation.output_path).read_text(
        encoding="utf-8"
    )
    for anchor in result.derivation.anchors:
        assert markdown.count(adapters.anchor_html_id(anchor)) == 1


@pytest.mark.parametrize(
    "anchor",
    [
        Anchor("page", '2" onclick="alert(1)'),
        Anchor("page", ""),
        Anchor("page", "two words"),
        Anchor("page", "2>"),
        Anchor("unknown", "1"),
    ],
)
def test_anchor_value_cannot_inject_html(adapters, anchor) -> None:
    with pytest.raises(adapters.ExtractionQualityError, match="invalid anchor"):
        adapters.anchor_html_id(anchor)


@pytest.mark.parametrize(
    ("markdown", "anchors", "code"),
    [
        (
            '<a id="page:1"></a>\n<a id="page:1"></a>\nText',
            (Anchor("page", "1"),),
            "invalid_anchors",
        ),
        ("Text", (Anchor("page", "1"),), "invalid_anchors"),
        (
            '<a id="page:1"></a>\nText',
            (Anchor("page", "1"), Anchor("page", "1")),
            "invalid_anchors",
        ),
        ('<a id="section:1"></a>\nText', (Anchor("section", "1"),), "invalid_anchors"),
        (
            '<a id="page:1"></a>\n<a id="page:2"></a>\nText',
            (Anchor("page", "1"),),
            "invalid_anchors",
        ),
        (
            '<a id="page:1"></a>\n<a id="page:1" onclick="alert(1)"></a>\nText',
            (Anchor("page", "1"),),
            "invalid_anchors",
        ),
        (
            '```\n<a id="page:1"></a>\n```\nText',
            (Anchor("page", "1"),),
            "invalid_anchors",
        ),
        ('<a id="page:1"></a>\n ', (Anchor("page", "1"),), "empty_extraction"),
    ],
)
def test_shared_publisher_rejects_invalid_evidence(
    adapters,
    repo_paths,
    markdown,
    anchors,
    code,
) -> None:
    destination = repo_paths.extracted / "source.pdf" / "artifact.md"
    with pytest.raises(adapters.ExtractionQualityError) as caught:
        adapters.publish_markdown_artifact(
            markdown,
            anchors,
            paths=repo_paths,
            destination=destination,
            expected_anchors=("page",),
            max_output_bytes=4096,
        )
    assert caught.value.code == code
    assert not destination.exists()


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"\xff", "invalid_utf8"),
        (b'<a id="page:1"></a>\n' + b"x" * 100, "output_too_large"),
    ],
)
def test_shared_publisher_validates_bytes_before_creating_output(
    adapters,
    repo_paths,
    body,
    code,
) -> None:
    destination = repo_paths.extracted / "artifact.md"
    with pytest.raises(adapters.ExtractionQualityError) as caught:
        adapters.publish_markdown_artifact(
            body,
            (Anchor("page", "1"),),
            paths=repo_paths,
            destination=destination,
            expected_anchors=("page",),
            max_output_bytes=64,
        )
    assert caught.value.code == code
    assert not destination.exists()


def test_pdf_page_boundaries_become_stable_markers(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown="Page one\fPage two\f"),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("page", "1"), Anchor("page", "2"))
    assert (repo_paths.root / result.derivation.output_path).read_text() == (
        '<a id="page:1"></a>\n\nPage one\n\n<a id="page:2"></a>\n\nPage two\n'
    )


@pytest.mark.parametrize(
    ("execution", "body", "code"),
    [
        (CommandExecution(2, b"", b"encrypted"), b"text", "converter_failed"),
        (
            CommandExecution(0, b"x", b"", stdout_truncated=True),
            b"text",
            "converter_output_truncated",
        ),
        (
            CommandExecution(0, b"", b"x", stderr_truncated=True),
            b"text",
            "converter_output_truncated",
        ),
        (CommandExecution(0, b"", b""), b"\xff", "invalid_utf8"),
        (CommandExecution(0, b"", b""), b"x" * 129, "output_too_large"),
        (CommandExecution(0, b"fallback text", b""), None, "missing_converter_output"),
    ],
)
def test_converter_failures_do_not_publish(
    adapters,
    pdf_job,
    pdf_resolved,
    repo_paths,
    execution,
    body,
    code,
) -> None:
    job = replace(pdf_job, extractor=replace(pdf_job.extractor, max_output_bytes=128))

    def run(argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        assert max_output_bytes == 128
        assert pass_fds == (job.staged_input.descriptor,)
        if body is not None:
            Path(argv[-1]).write_bytes(body)
        return execution

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=pdf_resolved)
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == code
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_converter_timeout_is_structured_failure(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout_seconds"])

    result = adapters.run_job(pdf_job, paths=repo_paths, run=run, resolved=pdf_resolved)
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "converter_timeout"


def test_existing_output_is_reused_only_for_identical_bytes(
    adapters,
    pdf_job,
    pdf_resolved,
    recording_run,
    repo_paths,
) -> None:
    first = adapters.run_job(
        pdf_job, paths=repo_paths, run=recording_run, resolved=pdf_resolved
    )
    assert first.derivation is not None
    output = repo_paths.root / first.derivation.output_path
    original = output.stat()
    second = adapters.run_job(
        pdf_job, paths=repo_paths, run=recording_run, resolved=pdf_resolved
    )
    assert second.derivation == first.derivation
    assert output.stat().st_ino == original.st_ino
    failed = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown="changed"),
        resolved=pdf_resolved,
    )
    assert failed.state is SourceState.FAILED
    assert failed.diagnostics[0].code == "output_path_collision"
    assert output.read_text() == (
        '<a id="page:1"></a>\n\n&lt;a id="page:1"&gt;&lt;/a&gt;\nPage one\n'
    )


@pytest.mark.parametrize("where", ["destination", "parent"])
def test_publication_never_follows_output_symlinks(
    adapters, repo_paths, tmp_path, where
):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "artifact.md"
    victim.write_text("retain me")
    parent = repo_paths.extracted / "paper.pdf"
    if where == "parent":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.mkdir()
        (parent / "artifact.md").symlink_to(victim)
    with pytest.raises(adapters.ExtractionQualityError):
        adapters.publish_markdown_artifact(
            '<a id="page:1"></a>\nText',
            (Anchor("page", "1"),),
            paths=repo_paths,
            destination=parent / "artifact.md",
            expected_anchors=("page",),
            max_output_bytes=128,
        )
    assert victim.read_text() == "retain me"


def test_output_stage_cannot_be_a_symlink(
    adapters, pdf_job, pdf_resolved, repo_paths, tmp_path
):
    victim = tmp_path / "victim"
    victim.write_text("external data")

    def run(argv, **kwargs):
        Path(argv[-1]).symlink_to(victim)
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(pdf_job, paths=repo_paths, run=run, resolved=pdf_resolved)
    assert result.state is SourceState.FAILED
    assert not list(repo_paths.extracted.rglob("*.md"))
    assert victim.read_text() == "external data"


def test_processor_uses_one_authoritative_attempt_directory(
    adapters,
    pdf_job,
    pdf_resolved,
    recording_run,
    repo_paths,
) -> None:
    class InspectRun(RecordingRun):
        def __call__(self, argv, *, cwd, **kwargs):
            attempts = list(repo_paths.root.glob(".brain-tmp-*"))
            assert attempts == [cwd]
            assert Path(argv[-1]).is_relative_to(cwd)
            return super().__call__(argv, cwd=cwd, **kwargs)

    run = InspectRun(markdown="one")
    result = DeterministicSourceProcessor(
        run=run, resolve=lambda _: pdf_resolved
    ).process(
        pdf_job.record,
        pdf_job.item,
        pdf_job.extractor,
        paths=repo_paths,
        context=pdf_job.context,
    )
    assert result.state is SourceState.OK
    assert not list(repo_paths.root.glob(".brain-tmp-*"))
    pdf_job.staged_input.revalidate()


def test_posix_runner_uses_bounded_files_and_inherited_input(
    adapters, tmp_path, monkeypatch
):
    observed = {}

    class Process:
        pid = 123456

        def wait(self, timeout=None):
            observed["timeout"] = timeout
            return 0

    def popen(argv, **kwargs):
        observed.update(kwargs)
        assert argv == ["pdftotext", "input", "output"]
        kwargs["stdout"].write(b"s" * 65)
        kwargs["stderr"].write(b"e" * (16 * 1024 + 1))
        return Process()

    monkeypatch.setattr(adapters.subprocess, "Popen", popen)
    monkeypatch.setattr(adapters.os, "killpg", lambda *args: None)
    monkeypatch.setattr(
        adapters,
        "_wait_for_posix_leader_without_reaping",
        lambda process, **kwargs: process.wait(timeout=kwargs["timeout_seconds"]),
    )
    result = adapters.run_command(
        ("pdftotext", "input", "output"),
        cwd=tmp_path,
        timeout_seconds=7,
        max_output_bytes=64,
        pass_fds=(9,),
    )
    assert result == CommandExecution(0, b"s" * 64, b"e" * (16 * 1024), True, True)
    assert observed["shell"] is False
    assert observed["start_new_session"] is True
    assert observed["pass_fds"] == (9,)
    assert observed["cwd"] == tmp_path
    assert observed["timeout"] <= 7
    assert observed["stdout"] is not observed["stderr"]
    assert observed["stdout"].closed and observed["stderr"].closed


def test_posix_runner_accepts_permission_error_for_exited_group(
    adapters, tmp_path, monkeypatch
):
    class Process:
        pid = 123456

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        adapters,
        "_wait_for_posix_leader_without_reaping",
        lambda process, **kwargs: 0,
    )

    def vanished_group(*args):
        raise PermissionError("Darwin reports an already-gone process group as EPERM")

    monkeypatch.setattr(adapters.os, "killpg", vanished_group)
    result = adapters.run_command(
        ("pdftotext", "input", "output"),
        cwd=tmp_path,
        timeout_seconds=7,
        max_output_bytes=64,
        pass_fds=(9,),
    )
    assert result.returncode == 0


def test_posix_runner_keeps_permission_error_strict_during_timeout(
    adapters, tmp_path, monkeypatch
):
    class Process:
        pid = 123456

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: Process())

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture", 1)

    monkeypatch.setattr(adapters, "_wait_for_posix_leader_without_reaping", timeout)
    monkeypatch.setattr(
        adapters.os,
        "killpg",
        lambda *args: (_ for _ in ()).throw(PermissionError("not permitted")),
    )
    with pytest.raises(PermissionError, match="not permitted"):
        adapters.run_command(
            ("pdftotext",),
            cwd=tmp_path,
            timeout_seconds=1,
            max_output_bytes=64,
            pass_fds=(9,),
        )


def test_posix_runner_kills_process_group_on_timeout(adapters, tmp_path, monkeypatch):
    killed = []

    class Process:
        pid = 123456
        waited = 0

        def wait(self, timeout=None):
            self.waited += 1
            if self.waited == 1:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return -9

    process = Process()
    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        adapters.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(
        adapters,
        "_wait_for_posix_leader_without_reaping",
        lambda process, **kwargs: process.wait(timeout=kwargs["timeout_seconds"]),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        adapters.run_command(
            ("pdftotext",),
            cwd=tmp_path,
            timeout_seconds=1,
            max_output_bytes=64,
            pass_fds=(9,),
        )
    assert killed == [(123456, signal.SIGKILL)]
    assert process.waited == 2


def test_tesseract_stdout_is_the_only_stdout_output_recipe(adapters, repo_paths):
    job = make_job(repo_paths, "scan.png", b"fixture", "image/png")
    resolved = resolve_test_converter(job.extractor)

    def run(argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        assert argv == (
            "tesseract",
            str(job.staged_input.descriptor_path),
            "stdout",
            "-l",
            "eng",
        )
        assert pass_fds == (job.staged_input.descriptor,)
        return CommandExecution(
            0, b"A clearly readable document with useful text.\n", b""
        )

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("block", "1"),)
    assert (
        "clearly readable"
        in (repo_paths.root / result.derivation.output_path).read_text()
    )


@pytest.mark.parametrize("body", [b"", b"?? ## ~!", b"x"])
def test_low_quality_image_output_requests_agent(adapters, repo_paths, body):
    job = make_job(repo_paths, "scan.png", b"fixture", "image/png")
    result = adapters.run_job(
        job,
        paths=repo_paths,
        resolved=resolve_test_converter(job.extractor),
        run=lambda *args, **kwargs: CommandExecution(0, body, b""),
    )
    assert result.state is SourceState.NEEDS_AGENT
    assert result.derivation is None


def test_publication_keeps_source_and_ledger_inactive(
    adapters,
    pdf_job,
    pdf_resolved,
    recording_run,
    repo_paths,
    monkeypatch,
):
    monkeypatch.setattr("brainlib.ledger.LedgerStore", FailIfCalled())
    result = adapters.run_job(
        pdf_job, paths=repo_paths, run=recording_run, resolved=pdf_resolved
    )
    assert result.state is SourceState.OK
    assert pdf_job.record.active_derivation_id is None
    assert list(repo_paths.ledger_dir.iterdir()) == [repo_paths.ledger_dir / ".gitkeep"]


@pytest.mark.parametrize(
    "markdown",
    [
        '`<a id="page:1"></a>`\nText',
        '<script><a id="page:1"></a></script>\nText',
        '<div hidden><a id="page:1"></a></div>\nText',
        '<!-- <a id="page:1"></a> -->\nText',
    ],
)
def test_shared_publisher_rejects_hidden_or_inline_code_markers(
    adapters, repo_paths, markdown
):
    with pytest.raises(adapters.ExtractionQualityError, match="anchor"):
        adapters.publish_markdown_artifact(
            markdown,
            (Anchor("page", "1"),),
            paths=repo_paths,
            destination=repo_paths.extracted / "artifact.md",
            expected_anchors=("page",),
            max_output_bytes=1024,
        )


def test_concurrent_different_publications_cannot_overwrite(
    adapters, repo_paths, monkeypatch
):
    link = os.link
    barrier = threading.Barrier(2, timeout=5)
    destination = repo_paths.extracted / "artifact.md"

    def synchronized_link(*args, **kwargs):
        barrier.wait()
        return link(*args, **kwargs)

    monkeypatch.setattr(adapters.os, "link", synchronized_link)

    def publish(text):
        try:
            return adapters.publish_markdown_artifact(
                f'<a id="page:1"></a>\n{text}',
                (Anchor("page", "1"),),
                paths=repo_paths,
                destination=destination,
                expected_anchors=("page",),
                max_output_bytes=1024,
            )
        except adapters.ExtractionQualityError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ("First", "Second")))
    assert results.count("output_path_collision") == 1
    artifact = next(result for result in results if not isinstance(result, str))
    assert artifact.output_path == PurePosixPath("sources/extracted/artifact.md")
    assert destination.read_text() in {
        '<a id="page:1"></a>\nFirst',
        '<a id="page:1"></a>\nSecond',
    }
    assert not list(repo_paths.extracted.glob(".brain-tmp-*"))


def test_shared_publisher_reuses_identical_artifact(adapters, repo_paths):
    kwargs = dict(
        paths=repo_paths,
        destination=repo_paths.extracted / "artifact.md",
        expected_anchors=("page",),
        max_output_bytes=128,
    )
    body = '<a id="page:1"></a>\nEvidence'
    first = adapters.publish_markdown_artifact(body, (Anchor("page", "1"),), **kwargs)
    second = adapters.publish_markdown_artifact(
        body.encode(), (Anchor("page", "1"),), **kwargs
    )
    assert second == first


def test_replaced_attempt_directory_is_rejected(
    adapters, pdf_job, pdf_resolved, repo_paths, tmp_path
):
    stage = repo_paths.root / ".brain-tmp-owned"
    stage.mkdir(mode=0o700)
    moved = stage.with_name(stage.name + "-moved")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "converted.md").write_text("attacker evidence")

    def run(argv, **kwargs):
        stage.rename(moved)
        stage.symlink_to(outside, target_is_directory=True)
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(
        pdf_job, paths=repo_paths, run=run, resolved=pdf_resolved, staging_dir=stage
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "unsafe_converter_output"
    assert not list(repo_paths.extracted.rglob("*.md"))


class FixedPythonRun:
    """Run the fixed driver inside a test with fake modules, never a subprocess."""

    def __init__(self, job):
        self.job = job

    def __call__(self, argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        driver_path = (
            Path(__file__).resolve().parents[2] / "brainlib/extractors/python_driver.py"
        )
        assert argv[:3] == (sys.executable, "-I", str(driver_path))
        assert argv[4] == str(self.job.staged_input.descriptor_path)
        assert Path(argv[5]).is_relative_to(cwd)
        assert argv[6] == str(max_output_bytes)
        assert timeout_seconds == self.job.extractor.timeout_seconds
        assert pass_fds == (self.job.staged_input.descriptor,)
        assert importlib.util.find_spec("brainlib.extractors.python_driver") is not None
        driver = importlib.import_module("brainlib.extractors.python_driver")
        return CommandExecution(driver.main(list(argv[3:])), b"", b"")


def test_python_pdf_driver_preserves_pages_and_uses_only_resolved_module(
    adapters,
    pdf_job,
    repo_paths,
    monkeypatch,
):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")
    closed = []

    class Document:
        is_encrypted = False

        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(get_text=lambda kind: "One"),
                    SimpleNamespace(get_text=lambda kind: "Two"),
                ]
            )

        def close(self):
            closed.append(True)

    def open_document(path):
        assert path == str(job.staged_input.descriptor_path)
        return Document()

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=open_document))
    run = FixedPythonRun(job)
    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("page", "1"), Anchor("page", "2"))
    assert result.derivation.method_metadata["converter_id"] == "python.pymupdf"
    assert closed == [True]


def test_python_pdf_driver_rejects_encrypted_document(
    adapters, pdf_job, repo_paths, monkeypatch
):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")
    monkeypatch.setitem(
        sys.modules,
        "fitz",
        SimpleNamespace(
            open=lambda path: SimpleNamespace(is_encrypted=True, close=lambda: None)
        ),
    )
    result = adapters.run_job(
        job, paths=repo_paths, run=FixedPythonRun(job), resolved=resolved
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "converter_failed"
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_python_docx_driver_retains_headings_paragraphs_and_tables(
    adapters, repo_paths, monkeypatch
):
    job = make_job(
        repo_paths,
        "document.docx",
        b"fixture",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    job, resolved = select_test_converter(job, "python.python-docx")
    blocks = [
        SimpleNamespace(text="Overview", style=SimpleNamespace(name="Heading 1")),
        SimpleNamespace(
            text="Evidence paragraph", style=SimpleNamespace(name="Normal")
        ),
        SimpleNamespace(
            rows=[
                SimpleNamespace(
                    cells=[SimpleNamespace(text="Value"), SimpleNamespace(text="42")]
                )
            ]
        ),
    ]
    monkeypatch.setitem(
        sys.modules,
        "docx",
        SimpleNamespace(
            Document=lambda path: SimpleNamespace(
                iter_inner_content=lambda: iter(blocks)
            )
        ),
    )
    result = adapters.run_job(
        job, paths=repo_paths, run=FixedPythonRun(job), resolved=resolved
    )
    assert result.state is SourceState.OK
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert (
        "# Overview" in markdown
        and "Evidence paragraph" in markdown
        and "42" in markdown
    )
    assert result.derivation.anchors == (Anchor("section", "1"),)


def test_python_pptx_driver_preserves_slide_boundaries_and_table_text(
    adapters, repo_paths, monkeypatch
):
    job = make_job(
        repo_paths,
        "slides.pptx",
        b"fixture",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    slides = [
        SimpleNamespace(
            shapes=[
                SimpleNamespace(has_text_frame=True, text="Slide one", has_table=False)
            ]
        ),
        SimpleNamespace(
            shapes=[
                SimpleNamespace(
                    has_text_frame=False,
                    has_table=True,
                    table=SimpleNamespace(
                        rows=[SimpleNamespace(cells=[SimpleNamespace(text="42")])]
                    ),
                )
            ]
        ),
    ]
    monkeypatch.setitem(
        sys.modules,
        "pptx",
        SimpleNamespace(Presentation=lambda path: SimpleNamespace(slides=slides)),
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FixedPythonRun(job),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (Anchor("slide", "1"), Anchor("slide", "2"))
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "Slide one" in markdown and "42" in markdown


def test_python_xlsx_driver_preserves_all_sheets_and_sheet_scoped_rows(
    adapters, repo_paths, monkeypatch
):
    job = make_job(
        repo_paths,
        "book.xlsx",
        b"fixture",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    closed = []
    sheets = [
        SimpleNamespace(
            title='Unsafe" name',
            iter_rows=lambda **kwargs: iter([("A", 1), ("Total", "=B1")]),
        ),
        SimpleNamespace(title="Empty", iter_rows=lambda **kwargs: iter([])),
        SimpleNamespace(title="Later", iter_rows=lambda **kwargs: iter([("B", 2)])),
    ]

    def load_workbook(path, **kwargs):
        assert path.name == str(job.staged_input.descriptor_path)
        assert kwargs == {"read_only": True, "data_only": False}
        return SimpleNamespace(worksheets=sheets, close=lambda: closed.append(True))

    monkeypatch.setitem(
        sys.modules, "openpyxl", SimpleNamespace(load_workbook=load_workbook)
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FixedPythonRun(job),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (
        Anchor("sheet", "1"),
        Anchor("row", "1/1"),
        Anchor("row", "1/2"),
        Anchor("sheet", "2"),
        Anchor("sheet", "3"),
        Anchor("row", "3/1"),
    )
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "=B1" in markdown and "Empty" in markdown and "Later" in markdown
    assert closed == [True]


def _office_package(kind, names):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        if kind == "xlsx":
            xml = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets>'
            for index, name in enumerate(names, 1):
                xml += f'<sheet name="{name}" sheetId="{index}"/>'
            archive.writestr("xl/workbook.xml", xml + "</sheets></workbook>")
        else:
            for index, name in enumerate(names, 1):
                archive.writestr(f"ppt/slides/slide{index}.xml", "<slide/>")
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_libreoffice_uses_isolated_profile_and_descriptor(adapters, repo_paths, kind):
    media_type = "application/vnd.openxmlformats-officedocument." + (
        "spreadsheetml.sheet" if kind == "xlsx" else "presentationml.presentation"
    )
    job = make_job(
        repo_paths, f"file.{kind}", _office_package(kind, ["Sheet1"]), media_type
    )
    job, resolved = select_test_converter(job, "libreoffice")

    def run(argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        assert argv[:4] == (
            "soffice",
            "--headless",
            "--convert-to",
            "csv" if kind == "xlsx" else "txt",
        )
        assert argv[4] == "--outdir"
        assert argv[6] == str(job.staged_input.descriptor_path)
        assert (
            argv[7] == "-env:UserInstallation=" + (cwd / "libreoffice-profile").as_uri()
        )
        assert pass_fds == (job.staged_input.descriptor,)
        directory = Path(argv[5])
        assert directory.is_relative_to(cwd)
        directory.mkdir(exist_ok=True)
        (
            directory
            / (
                str(job.staged_input.descriptor)
                + (".csv" if kind == "xlsx" else ".txt")
            )
        ).write_text("A,42\n" if kind == "xlsx" else "Slide text")
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.OK
    assert set(anchor.kind for anchor in result.derivation.anchors) == (
        {"sheet", "row"} if kind == "xlsx" else {"slide"}
    )


def test_libreoffice_rejects_partial_multi_sheet_export(adapters, repo_paths):
    job = make_job(
        repo_paths,
        "book.xlsx",
        _office_package("xlsx", ["First", "Second"]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    job, resolved = select_test_converter(job, "libreoffice")

    def run(argv, **kwargs):
        Path(argv[5]).mkdir(exist_ok=True)
        (Path(argv[5]) / "book.csv").write_text("only first sheet\n")
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "incomplete_sheet_coverage"
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_libreoffice_preserves_each_identifiable_generated_sheet(adapters, repo_paths):
    job = make_job(
        repo_paths,
        "book.xlsx",
        _office_package("xlsx", ["First", "Second"]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    job, resolved = select_test_converter(job, "libreoffice")

    def run(argv, **kwargs):
        directory = Path(argv[5])
        directory.mkdir(exist_ok=True)
        (directory / "First.csv").write_text("A,1\n")
        (directory / "Second.csv").write_text("B,2\n")
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.OK
    assert result.derivation.anchors == (
        Anchor("sheet", "1"),
        Anchor("row", "1/1"),
        Anchor("sheet", "2"),
        Anchor("row", "2/1"),
    )


def test_image_preclassified_as_complex_skips_ocr(adapters, repo_paths):
    from brainlib.diagnostics import Diagnostic

    job = make_job(repo_paths, "diagram.png", b"fixture", "image/png")
    job = replace(
        job,
        record=replace(
            job.record,
            diagnostics=(
                Diagnostic("complex_image", "Requires visual interpretation"),
            ),
        ),
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.NEEDS_AGENT
    assert result.diagnostics[0].code == "complex_image"


def test_command_runner_terminates_descendants_before_reaping(
    adapters, tmp_path, monkeypatch
):
    observed = []

    class Process:
        pid = 123456

        def wait(self, timeout=None):
            observed.append("reap")
            return 0

    monkeypatch.setattr(adapters.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        adapters.os, "killpg", lambda *args: observed.append("kill group")
    )

    def exited(process, **kwargs):
        observed.append("exit without reaping")
        return 0

    monkeypatch.setattr(
        adapters, "_wait_for_posix_leader_without_reaping", exited, raising=False
    )
    adapters.run_command(
        ("pdftotext",),
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=64,
        pass_fds=(9,),
    )
    assert observed == ["exit without reaping", "kill group", "reap"]


def test_stage_context_mismatch_fails_before_conversion(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    from brainlib.inventory import InventoryAccessError

    job = replace(pdf_job, context=replace(pdf_job.context, input_sha256="0" * 64))
    with pytest.raises(InventoryAccessError):
        adapters.run_job(
            job, paths=repo_paths, run=FailIfCalled(), resolved=pdf_resolved
        )
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_direct_publication_returns_structured_collision(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    first = adapters.ExtractedPayload(
        '<a id="page:1"></a>\nFirst', (Anchor("page", "1"),), None
    )
    second = adapters.ExtractedPayload(
        '<a id="page:1"></a>\nSecond', (Anchor("page", "1"),), None
    )
    assert (
        adapters.publish_payload(
            pdf_job, first, paths=repo_paths, resolved=pdf_resolved
        ).state
        is SourceState.OK
    )
    result = adapters.publish_payload(
        pdf_job, second, paths=repo_paths, resolved=pdf_resolved
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "output_path_collision"


def test_transient_output_directory_swap_cannot_supply_evidence(
    adapters,
    pdf_job,
    pdf_resolved,
    repo_paths,
    tmp_path,
    monkeypatch,
):
    stage = repo_paths.root / ".brain-tmp-owned"
    stage.mkdir(mode=0o700)
    moved = stage.with_name(stage.name + "-moved")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "converted.md").write_text("attacker evidence")
    read_output = adapters._read_output

    def replace_while_opening(path, limit, **kwargs):
        stage.rename(moved)
        stage.symlink_to(outside, target_is_directory=True)
        try:
            return read_output(path, limit, **kwargs)
        finally:
            stage.unlink()
            moved.rename(stage)

    monkeypatch.setattr(adapters, "_read_output", replace_while_opening)
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown="Trusted staged output"),
        resolved=pdf_resolved,
        staging_dir=stage,
    )
    assert result.state is SourceState.OK
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "Trusted staged output" in markdown
    assert "attacker" not in markdown


@pytest.mark.parametrize("mismatch", ["distribution", "template", "identifier"])
def test_python_adapter_rejects_unregistered_driver_inputs_before_spawn(
    adapters,
    pdf_job,
    repo_paths,
    mismatch,
):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")
    values = {
        "distribution": {"python_distribution": "OtherPackage"},
        "template": {"argv_template": ("{input}", "--unexpected", "{output}")},
        "identifier": {"converter_id": "python.unregistered"},
    }[mismatch]
    converter = replace(resolved.converter, **values)
    job = replace(
        job, extractor=replace(job.extractor, preferred=converter, fallbacks=())
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=replace(resolved, converter=converter),
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "unsupported_converter"


def test_python_adapter_timeout_removes_attempt_output(adapters, pdf_job, repo_paths):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")

    def run(argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        assert pass_fds == (job.staged_input.descriptor,)
        assert timeout_seconds == job.extractor.timeout_seconds
        Path(argv[5]).write_text("partial extraction")
        raise subprocess.TimeoutExpired(argv, timeout_seconds)

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "converter_timeout"
    assert not list(repo_paths.root.glob(".brain-tmp-*"))
    assert not list(repo_paths.extracted.rglob("*.md"))
    job.staged_input.revalidate()


def test_python_driver_handler_bounds_output_without_real_optional_modules(
    adapters,
    pdf_job,
    monkeypatch,
):
    assert importlib.util.find_spec("brainlib.extractors.python_driver") is not None
    driver = importlib.import_module("brainlib.extractors.python_driver")
    monkeypatch.setitem(
        sys.modules,
        "fitz",
        SimpleNamespace(
            open=lambda path: SimpleNamespace(
                is_encrypted=False,
                close=lambda: None,
                __iter__=lambda: iter(()),
            )
        ),
    )

    class Document:
        is_encrypted = False

        def __iter__(self):
            return iter([SimpleNamespace(get_text=lambda kind: "x" * 256)])

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules, "fitz", SimpleNamespace(open=lambda path: Document())
    )
    with pytest.raises(adapters.ExtractionQualityError) as caught:
        driver.extract_python_payload(
            "python.pymupdf", pdf_job.staged_input.descriptor_path, max_output_bytes=128
        )
    assert caught.value.code == "output_too_large"


def test_python_driver_rejects_mutable_input_path_without_imports(
    adapters, repo_paths, monkeypatch
):
    assert importlib.util.find_spec("brainlib.extractors.python_driver") is not None
    driver = importlib.import_module("brainlib.extractors.python_driver")
    monkeypatch.setattr(
        driver.importlib,
        "import_module",
        lambda *args: pytest.fail("must reject input before import"),
    )
    with pytest.raises(adapters.ExtractionQualityError):
        driver.extract_python_payload(
            "python.pymupdf", repo_paths.raw / "paper.pdf", max_output_bytes=128
        )


def test_pandoc_html_output_cannot_retain_active_content(adapters, repo_paths):
    job = make_job(repo_paths, "page.html", b"<h1>Title</h1>", "text/html")
    result = adapters.run_job(
        job,
        paths=repo_paths,
        resolved=resolve_test_converter(job.extractor),
        run=RecordingRun(
            markdown='<a id="section:1"></a>\n# Title\n<script>active()</script>\n<style>hide</style>\nFact'
        ),
    )
    assert result.state is SourceState.OK
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "# Title" in markdown and "Fact" in markdown
    assert (
        "script" not in markdown
        and "active()" not in markdown
        and "hide" not in markdown
    )


def test_short_converter_read_never_publishes_partial_evidence(
    adapters, pdf_job, pdf_resolved, repo_paths, monkeypatch
):
    pread = os.pread

    def short_read(descriptor, size, offset):
        body = pread(descriptor, size, offset)
        if body == b"Full converted evidence":
            return b"Full"
        return body

    monkeypatch.setattr(adapters.os, "pread", short_read)
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=RecordingRun(markdown="Full converted evidence"),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.FAILED
    assert not list(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize("version", [None, 42, "   "])
def test_invalid_resolved_version_cannot_be_persisted(
    adapters, pdf_job, pdf_resolved, repo_paths, version
):
    result = adapters.run_job(
        pdf_job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=replace(pdf_resolved, detected_version=version),
    )
    assert result.state is SourceState.PENDING
    assert result.diagnostics[0].code == "prerequisite_changed"


def test_image_timeout_requests_configured_agent(adapters, repo_paths):
    job = make_job(repo_paths, "scan.png", b"fixture", "image/png")

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout_seconds"])

    result = adapters.run_job(
        job, paths=repo_paths, run=run, resolved=resolve_test_converter(job.extractor)
    )
    assert result.state is SourceState.NEEDS_AGENT
    assert result.diagnostics[0].code == "converter_timeout"


@pytest.mark.parametrize("kind", ["native", "tesseract"])
def test_unrecognized_builtin_or_stdout_recipe_is_rejected_before_dispatch(
    adapters, repo_paths, kind
):
    job = make_job(
        repo_paths,
        "note.txt" if kind == "native" else "scan.png",
        b"Evidence",
        "text/plain" if kind == "native" else "image/png",
    )
    converter = replace(
        job.extractor.preferred, argv_template=("{input}", "unexpected", "{output}")
    )
    job = replace(job, extractor=replace(job.extractor, preferred=converter))
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "unsupported_converter"


def test_native_short_read_cannot_publish_partial_text(
    adapters, repo_paths, monkeypatch
):
    job = make_job(repo_paths, "note.txt", b"Full original evidence\n", "text/plain")
    pread = os.pread

    def short_read(descriptor, size, offset):
        body = pread(descriptor, size, offset)
        if body == b"Full original evidence\n":
            return b"Full"
        return body

    monkeypatch.setattr(adapters.os, "pread", short_read)
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    assert (
        "Full original evidence"
        in (repo_paths.root / result.derivation.output_path).read_text()
    )


@pytest.mark.parametrize(
    ("relative", "media_type", "converter_id"),
    [
        ("file.pdf", "application/pdf", "poppler.pdftotext"),
        ("file.html", "text/html", "pandoc"),
        (
            "file.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pandoc",
        ),
        (
            "file.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "libreoffice",
        ),
        (
            "file.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "libreoffice",
        ),
        ("file.png", "image/png", "tesseract"),
    ],
)
@pytest.mark.parametrize("mismatch", ["executable", "template", "distribution"])
def test_command_identity_and_recipe_are_statically_allowlisted_before_dispatch(
    adapters,
    repo_paths,
    relative,
    media_type,
    converter_id,
    mismatch,
):
    job = make_job(repo_paths, relative, b"fixture", media_type)
    job, resolved = select_test_converter(job, converter_id)
    values = {
        "executable": {"executable": "/tmp/custom-converter"},
        "template": {
            "argv_template": (
                *resolved.converter.argv_template,
                "--unregistered-option",
            )
        },
        "distribution": {
            "executable": None,
            "python_distribution": "OtherPackage",
            "version_args": (),
        },
    }
    converter = replace(resolved.converter, **values[mismatch])
    job = replace(
        job, extractor=replace(job.extractor, preferred=converter, fallbacks=())
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=replace(resolved, converter=converter),
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "unsupported_converter"
    assert not list(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize(
    ("relative", "media_type", "converter_id", "old", "new"),
    [
        ("file.html", "text/html", "pandoc", "--from=html", "--from=docx"),
        (
            "file.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pandoc",
            "--from=docx",
            "--from=html",
        ),
        (
            "file.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "libreoffice",
            "txt",
            "csv",
        ),
        (
            "file.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "libreoffice",
            "csv",
            "txt",
        ),
    ],
)
def test_command_recipes_cannot_cross_extractor_formats(
    adapters,
    repo_paths,
    relative,
    media_type,
    converter_id,
    old,
    new,
):
    job = make_job(repo_paths, relative, b"fixture", media_type)
    job, resolved = select_test_converter(job, converter_id)
    converter = replace(
        resolved.converter,
        argv_template=tuple(
            new if argument == old else argument
            for argument in resolved.converter.argv_template
        ),
    )
    job = replace(
        job, extractor=replace(job.extractor, preferred=converter, fallbacks=())
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=replace(resolved, converter=converter),
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "unsupported_converter"
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_python_adapter_preserves_current_virtual_environment_interpreter(
    adapters,
    pdf_job,
    repo_paths,
    monkeypatch,
    tmp_path,
):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")
    interpreter = tmp_path / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(adapters.sys, "executable", str(interpreter))

    def run(argv, *, cwd, timeout_seconds, max_output_bytes, pass_fds=()):
        assert argv[0] == str(interpreter)
        assert argv[1] == "-I"
        assert argv[4] == str(job.staged_input.descriptor_path)
        assert pass_fds == (job.staged_input.descriptor,)
        Path(argv[5]).write_text('<a id="page:1"></a>\n\nVirtual environment text.\n')
        return CommandExecution(0, b"", b"")

    result = adapters.run_job(job, paths=repo_paths, run=run, resolved=resolved)
    assert result.state is SourceState.OK
    assert result.derivation.method_metadata["converter_id"] == "python.pymupdf"


@pytest.mark.parametrize("invalid", ["relative", "directory", "nonexecutable"])
def test_python_adapter_rejects_invalid_current_interpreter_before_spawn(
    adapters,
    pdf_job,
    repo_paths,
    monkeypatch,
    tmp_path,
    invalid,
):
    job, resolved = select_test_converter(pdf_job, "python.pymupdf")
    interpreter = tmp_path / "python"
    if invalid == "relative":
        interpreter.symlink_to(sys.executable)
        monkeypatch.chdir(tmp_path)
        value = "python"
    elif invalid == "directory":
        interpreter.mkdir()
        value = str(interpreter)
    else:
        interpreter.write_bytes(b"not an executable")
        interpreter.chmod(0o600)
        value = str(interpreter)
    monkeypatch.setattr(adapters.sys, "executable", value)
    result = adapters.run_job(
        job, paths=repo_paths, run=FailIfCalled(), resolved=resolved
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "converter_unavailable"
    assert not list(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize(
    ("markdown", "anchors"),
    [
        ('\\<a id="page:1"></a>\nText', (Anchor("page", "1"),)),
        ('\\\\\\<a id="page:1"></a>\nText', (Anchor("page", "1"),)),
        ('`literal\n<a id="page:1"></a>\nend`\nText', (Anchor("page", "1"),)),
        ('``literal\n<a id="page:1"></a>\nend``\nText', (Anchor("page", "1"),)),
        (
            '<a id="page:1"></a>\nText\n\n\\<a id="page:2"></a>\nMore text',
            (Anchor("page", "1"), Anchor("page", "2")),
        ),
    ],
)
@pytest.mark.parametrize("entrypoint", ["validator", "publisher"])
def test_shared_validation_rejects_markdown_invisible_anchors(
    adapters,
    repo_paths,
    markdown,
    anchors,
    entrypoint,
):
    with pytest.raises(adapters.ExtractionQualityError, match="anchor"):
        if entrypoint == "validator":
            adapters.validate_markdown(
                markdown,
                anchors,
                expected_anchors=("page",),
                max_output_bytes=1024,
            )
        else:
            adapters.publish_markdown_artifact(
                markdown,
                anchors,
                paths=repo_paths,
                destination=repo_paths.extracted / "artifact.md",
                expected_anchors=("page",),
                max_output_bytes=1024,
            )
    assert not list(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize(
    "markdown",
    [
        '`literal\nmore literal`\n\n<a id="page:1"></a>\nText',
        '\\\\<a id="page:1"></a>\nText',
        '\\`literal\n\n<a id="page:1"></a>\nText',
        '`unclosed\n\n<a id="page:1"></a>\nText',
        '``literal ` different run``\n\n<a id="page:1"></a>\nText',
    ],
)
def test_shared_validation_retains_visible_anchors_after_markdown_code(
    adapters,
    repo_paths,
    markdown,
):
    assert (
        adapters.validate_markdown(
            markdown,
            (Anchor("page", "1"),),
            expected_anchors=("page",),
            max_output_bytes=1024,
        )
        == markdown.encode()
    )
    artifact = adapters.publish_markdown_artifact(
        markdown,
        (Anchor("page", "1"),),
        paths=repo_paths,
        destination=repo_paths.extracted / "artifact.md",
        expected_anchors=("page",),
        max_output_bytes=1024,
    )
    assert (repo_paths.root / artifact.output_path).read_text() == markdown
