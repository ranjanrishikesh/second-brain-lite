from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brainlib.contracts import Anchor, SourceState
from brainlib.extractors.processor import DeterministicSourceProcessor
from brainlib.sync import _validate_process_result
from tests.helpers_extractors import (
    FailIfCalled,
    make_job,
    resolve_test_converter,
    run_brain_json,
    select_test_converter,
)


@pytest.fixture
def adapters():
    return importlib.import_module("brainlib.extractors.adapters")


def test_native_markdown_is_published_below_extracted(adapters, repo_paths):
    job = make_job(repo_paths, "notes/readme.md", b"# Heading\nFact\n", "text/markdown")
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.derivation is not None
    assert result.derivation.output_path.as_posix().startswith(
        "sources/extracted/notes/readme.md/"
    )
    assert result.derivation.output_path.suffix == ".md"
    assert result.derivation.anchors == (Anchor("line", "1"), Anchor("line", "2"))
    assert (repo_paths.root / result.derivation.output_path).read_text() == (
        '<a id="line:1"></a>\n<a id="line:2"></a>\n\n# Heading\nFact\n'
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<div id="page">Page body</div>\n', '<div id="page">Page body</div>'),
        (
            '<a id="page:999"></a> Quoted source marker\n',
            '&lt;a id="page:999"&gt;&lt;/a&gt; Quoted source marker',
        ),
    ],
)
def test_native_markdown_distinguishes_source_html_from_generated_anchors(
    adapters, repo_paths, body, expected
):
    job = make_job(repo_paths, "captured-page.md", body.encode(), "text/markdown")
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert expected in markdown
    assert result.derivation.anchors == (Anchor("line", "1"),)
    assert markdown.count('<a id="line:1"></a>') == 1


@pytest.mark.parametrize(
    ("relative", "media_type", "body", "anchors", "excerpt"),
    [
        (
            "notes.txt",
            "text/plain",
            b"First\r\nSecond\r\n",
            (Anchor("line", "1"), Anchor("line", "2")),
            "Second",
        ),
        (
            "table.csv",
            "text/csv",
            b'name,value\n"A|B",2\n',
            (Anchor("row", "1"), Anchor("row", "2")),
            "A\\|B",
        ),
        (
            "table.tsv",
            "text/tab-separated-values",
            b"name\tvalue\nA\t2\n",
            (Anchor("row", "1"), Anchor("row", "2")),
            "| A | 2 |",
        ),
        (
            "data.json",
            "application/json",
            b'{"z": 2,"a": 1}',
            (Anchor("block", "1"),),
            '```json\n{\n  "a": 1,\n  "z": 2\n}\n```',
        ),
        (
            "data.jsonl",
            "application/x-ndjson",
            b'{"z":2}\n{"a":1}\n',
            (Anchor("block", "1"), Anchor("block", "2")),
            '"a": 1',
        ),
    ],
)
def test_native_formats_have_deterministic_markdown(
    adapters,
    repo_paths,
    relative,
    media_type,
    body,
    anchors,
    excerpt,
):
    job = make_job(repo_paths, relative, body, media_type)
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    assert result.derivation.anchors == anchors
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert excerpt in markdown
    for anchor in anchors:
        assert markdown.count(adapters.anchor_html_id(anchor)) == 1
    _validate_process_result(
        result,
        extractor=job.extractor,
        context=job.context,
        raw_path=job.item.fingerprint.path,
        paths=repo_paths,
    )


@pytest.mark.parametrize(
    ("relative", "media_type", "body", "code"),
    [
        ("data.json", "application/json", b"{bad}", "malformed_input"),
        ("data.json", "application/json", b'{"n":NaN}', "malformed_input"),
        ("text.txt", "text/plain", b"\xff", "invalid_utf8"),
        ("text.txt", "text/plain", b"  \n", "empty_extraction"),
        ("data.csv", "text/csv", b'a,b\n"unclosed\n', "malformed_input"),
    ],
)
def test_invalid_native_input_never_publishes(
    adapters, repo_paths, relative, media_type, body, code
):
    job = make_job(repo_paths, relative, body, media_type)
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == code
    assert not list(repo_paths.extracted.rglob("*.md"))


def test_html_fallback_retains_headings_and_text_without_active_content(
    adapters, repo_paths
):
    job = make_job(
        repo_paths,
        "page.html",
        b"<html><head><style>secret-css</style></head><body><h1>Title</h1>"
        b'<script>secret-script</script><p onclick="bad()">Hello &amp; goodbye</p></body></html>',
        "text/html",
    )
    job, resolved = select_test_converter(job, "builtin.html")
    result = adapters.run_job(
        job, paths=repo_paths, run=FailIfCalled(), resolved=resolved
    )
    assert result.state is SourceState.OK
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert "# Title" in markdown and "Hello & goodbye" in markdown
    assert (
        "secret" not in markdown
        and "onclick" not in markdown
        and "<script" not in markdown
    )
    assert result.derivation.anchors == (Anchor("section", "1"),)
    _validate_process_result(
        result,
        extractor=job.extractor,
        context=job.context,
        raw_path=job.item.fingerprint.path,
        paths=repo_paths,
    )


def test_raw_path_changes_do_not_affect_owned_input(adapters, repo_paths):
    job = make_job(repo_paths, "note.txt", b"Original evidence\n", "text/plain")
    (repo_paths.raw / job.item.fingerprint.path).write_text("changed raw")
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    assert (
        "Original evidence"
        in (repo_paths.root / result.derivation.output_path).read_text()
    )
    job.staged_input.revalidate()
    assert os.pread(job.staged_input.descriptor, 18, 0) == b"Original evidence\n"


def test_normal_processor_path_stages_descriptor_and_publishes(adapters, repo_paths):
    job = make_job(repo_paths, "note.txt", b"Pinned input\n", "text/plain")
    result = DeterministicSourceProcessor(
        run=FailIfCalled(),
        resolve=resolve_test_converter,
    ).process(
        job.record, job.item, job.extractor, paths=repo_paths, context=job.context
    )
    assert result.state is SourceState.OK
    job.staged_input.revalidate()
    assert not list(repo_paths.root.glob(".brain-tmp-*"))
    assert not list(repo_paths.root.glob(".brain-stage-*"))


def test_same_stem_formats_do_not_collide(adapters, repo_paths):
    outputs = []
    for name in ("note.txt", "note.md"):
        job = make_job(repo_paths, name, b"same evidence", "text/plain")
        result = adapters.run_job(
            job,
            paths=repo_paths,
            run=FailIfCalled(),
            resolved=resolve_test_converter(job.extractor),
        )
        assert result.state is SourceState.OK
        outputs.append(result.derivation.output_path)
    assert outputs[0] != outputs[1]
    assert outputs[0].parts[2] == "note.txt"
    assert outputs[1].parts[2] == "note.md"


def test_markdown_code_fences_retain_text_and_navigable_line_anchors(
    adapters, repo_paths
):
    job = make_job(
        repo_paths,
        "code.md",
        b"# Example\n```python\nprint(1)\n```\nAfter\n",
        "text/markdown",
    )
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    text = (repo_paths.root / result.derivation.output_path).read_text()
    assert "```python\nprint(1)\n```" in text
    assert len(result.derivation.anchors) == 5
    inside = False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
        if line.startswith('<a id="line:'):
            assert not inside


def test_batch_passes_the_owned_stage_directly_to_runner(
    adapters, pdf_job, pdf_resolved, repo_paths
):
    from brainlib.extractors.processor import CommandExecution

    def run(argv, *, pass_fds, **kwargs):
        assert pass_fds == (pdf_job.staged_input.descriptor,)
        assert argv[-2] == str(pdf_job.staged_input.descriptor_path)
        assert not list(repo_paths.root.glob(".brain-stage-*"))
        from pathlib import Path

        Path(argv[-1]).write_text("Page one")
        return CommandExecution(0, b"", b"")

    result = list(
        DeterministicSourceProcessor(
            run=run, resolve=lambda _: pdf_resolved
        ).iter_batch(
            [pdf_job],
            paths=repo_paths,
            max_workers=1,
        )
    )
    assert result[0][1].state is SourceState.OK
    with pytest.raises(OSError):
        os.fstat(pdf_job.staged_input.descriptor)


@pytest.mark.parametrize(
    "body",
    [
        "| Name | Value |\n| --- | --- |\n| One | 1 |\n| Two | 2 |\n",
        "Setext heading\n==============\n\nFollowing paragraph\n",
        "- First item\n  Continued first item\n\n  Another paragraph\n\n- Second item\n",
        "> Quoted paragraph\n> continued\n>\n> Another quoted paragraph\n",
        "Paragraph with `multiline\ninline code` and text\n",
        "    indented code\n    second code line\n",
        "Hard break  \nNext line\n\n\n",
        "# Windows heading\r\n\r\nParagraph\r\ncontinues without final newline",
    ],
)
@pytest.mark.parametrize(
    ("relative", "media_type"),
    [
        ("structure.md", "text/markdown"),
        ("structure.txt", "text/plain"),
    ],
)
def test_native_line_anchor_prelude_preserves_multiline_markdown_structures(
    adapters,
    repo_paths,
    body,
    relative,
    media_type,
):
    job = make_job(repo_paths, relative, body.encode(), media_type)
    result = adapters.run_job(
        job,
        paths=repo_paths,
        run=FailIfCalled(),
        resolved=resolve_test_converter(job.extractor),
    )
    assert result.state is SourceState.OK
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    anchors = tuple(
        Anchor("line", str(index))
        for index in range(1, len(normalized.splitlines()) + 1)
    )
    assert result.derivation.anchors == anchors
    markdown = (repo_paths.root / result.derivation.output_path).read_text()
    assert normalized in markdown
    prelude = "\n".join(adapters.anchor_html_id(anchor) for anchor in anchors)
    assert markdown == prelude + "\n\n" + normalized
    for anchor in anchors:
        assert markdown.count(adapters.anchor_html_id(anchor)) == 1
    assert (
        adapters.validate_markdown(
            markdown,
            anchors,
            expected_anchors=("line",),
            max_output_bytes=job.extractor.max_output_bytes,
        )
        == markdown.encode()
    )
    _validate_process_result(
        result,
        extractor=job.extractor,
        context=job.context,
        raw_path=job.item.fingerprint.path,
        paths=repo_paths,
    )


def test_fixture_generator_is_clean(repo_root: Path) -> None:
    result = subprocess.run(
        (sys.executable, "tests/fixtures/generate_extractors.py", "--check"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_generator_detects_only_owned_fixture_drift(repo_root: Path) -> None:
    fixture_root = repo_root / "tests/fixtures"
    (fixture_root / "extractors/core/sample.md").unlink()
    (fixture_root / "extractors/core/sample.txt").write_text(
        "drift\n", encoding="utf-8"
    )
    (fixture_root / "extractors/unexpected.bin").write_bytes(b"extra")
    (fixture_root / "inventory/not-generator-owned.txt").write_text(
        "unrelated\n", encoding="utf-8"
    )

    result = subprocess.run(
        (sys.executable, "tests/fixtures/generate_extractors.py", "--check"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "extra: extractors/unexpected.bin",
        "missing: extractors/core/sample.md",
        "nonmatching: extractors/core/sample.txt",
    ]
    assert "not-generator-owned" not in result.stdout

    restored = subprocess.run(
        (sys.executable, "tests/fixtures/generate_extractors.py", "--write"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stdout + restored.stderr
    default_check = subprocess.run(
        (sys.executable, "tests/fixtures/generate_extractors.py"),
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert default_check.returncode == 1
    assert default_check.stdout.splitlines() == ["extra: extractors/unexpected.bin"]


@pytest.mark.parametrize(
    ("relative", "state", "anchor_kind"),
    [
        ("core/sample.md", "ok", "line"),
        ("core/sample.txt", "ok", "line"),
        ("core/sample.csv", "ok", "row"),
        ("core/sample.tsv", "ok", "row"),
        ("core/sample.json", "ok", "block"),
        ("core/sample.html", "ok", "section"),
        ("core/sample.pdf", "ok", "page"),
        ("core/sample.docx", "ok", "section"),
        ("core/sample.pptx", "ok", "slide"),
        ("core/sample.xlsx", "ok", "sheet"),
        ("core/sample.png", "needs_agent", None),
        ("failures/empty.txt", "failed", None),
        ("failures/malformed.docx", "failed", None),
        ("failures/encrypted.pdf", "failed", None),
        ("failures/unsupported.bin", "unsupported", None),
    ],
)
def test_fixture_has_explicit_outcome(
    relative: str,
    state: str,
    anchor_kind: str | None,
    initialized_repo: Path,
    fixture_services,
) -> None:
    from conftest import active_representation, install_fixture, record_for_fixture

    install_fixture(initialized_repo, relative)
    payload = run_brain_json(initialized_repo, "sync", services=fixture_services)
    assert payload["command"] == "sync"
    assert payload["ok"] is (state == "ok")
    if state != "ok":
        status = run_brain_json(initialized_repo, "status", services=fixture_services)
        assert status["command"] == "status"
        assert not status["ok"]
    record = record_for_fixture(initialized_repo, Path(relative).name)
    assert record["state"] == state
    if anchor_kind is not None:
        representation = active_representation(record)
        assert any(
            anchor["kind"] == anchor_kind for anchor in representation["anchors"]
        )
        assert representation["extracted_path"].endswith(".md")
        if relative == "core/sample.xlsx":
            assert {anchor["kind"] for anchor in representation["anchors"]} == {
                "sheet",
                "row",
            }


@pytest.mark.parametrize(
    "relative",
    [
        "core/sample.png",
        "failures/empty.txt",
        "failures/unsupported.bin",
    ],
)
def test_full_validation_accepts_retained_coverage_gaps(
    relative: str, initialized_repo: Path, fixture_services
) -> None:
    from conftest import install_fixture

    install_fixture(initialized_repo, relative)
    sync = run_brain_json(initialized_repo, "sync", services=fixture_services)
    assert not sync["ok"]
    validation = run_brain_json(
        initialized_repo, "validate", "--full", services=fixture_services
    )
    assert validation["command"] == "validate"
    assert validation["ok"]
