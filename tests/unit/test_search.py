import importlib
import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers_knowledge import (
    ContentAwareRgRecorder,
    FakeLedger,
    FakeRgProcess,
    make_active_ledger,
    make_source_representation,
)


@pytest.fixture
def search():
    assert importlib.util.find_spec("brainlib.search") is not None, (
        "safe search contract is absent"
    )
    return importlib.import_module("brainlib.search")


def test_literal_argument_arrays(search, tmp_path):
    request = search.SearchRequest("sources", "discovery", ("C++", "[draft]", "a|b"), 3)
    assert search.build_rg_argv(
        request, phase="filenames", pattern_file=tmp_path / "terms"
    ) == [
        "rg",
        "--no-config",
        "--sort",
        "path",
        "--fixed-strings",
        "--files-with-matches",
        "--null",
        "--file",
        str(tmp_path / "terms"),
        "--",
    ]
    assert search.build_rg_argv(
        request, phase="context", pattern_file=tmp_path / "terms"
    ) == [
        "rg",
        "--no-config",
        "--sort",
        "path",
        "--json",
        "--fixed-strings",
        "--line-number",
        "--context",
        "3",
        "--file",
        str(tmp_path / "terms"),
        "--",
    ]


@pytest.mark.parametrize(
    "updates",
    [
        {"terms": ()},
        {"terms": ("",)},
        {"terms": (" ",)},
        {"terms": ("a", "a")},
        {"terms": ("a\nb",)},
        {"terms": ("a\rb",)},
        {"terms": ("a\0b",)},
        {"scope": "raw"},
        {"pass_name": None},
        {"pass_name": "other"},
        {"context_lines": -1},
        {"context_lines": 21},
        {"context_lines": True},
        {"page_size": 0},
        {"page_size": 501},
        {"max_run_bytes": 255},
        {"freshness_source_ids": ("bad",)},
        {"scope": "wiki"},
    ],
)
def test_invalid_requests_fail_before_io(search, updates):
    args = dict(
        scope="sources", pass_name="discovery", terms=("Alpha",), context_lines=2
    )
    args.update(updates)
    with pytest.raises(ValueError):
        search.SearchRequest(**args)


def test_batches_account_for_encoded_bytes_and_every_path(search):
    operands = tuple(Path(f"/tmp/{number:04d}-résumé") for number in range(600))
    batches = search.batch_paths(operands, fixed_argv=("rg", "--"))
    assert [len(batch) for batch in batches] == [256, 256, 88]
    assert tuple(path for batch in batches for path in batch) == operands
    fixed = ("a" * (search.MAX_RG_ARGV_BYTES - 8),)
    assert search.batch_paths((Path("/é"), Path("/ê")), fixed_argv=fixed) == (
        (Path("/é"),),
        (Path("/ê"),),
    )
    with pytest.raises(search.SearchArgumentLimitError):
        search.batch_paths((Path("/long-operand"),), fixed_argv=fixed)


def test_every_filename_batch_precedes_context_and_late_hit(
    search, repo_paths, monkeypatch
):
    ledger = make_active_ledger(repo_paths, 600, matching_numbers={599})
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    first = search.search_active_sources(
        repo_paths,
        ledger,
        search.SearchRequest("sources", "discovery", ("Alpha",), 2, page_size=1),
    )
    assert len(recorder.calls) == 4
    assert all("--files-with-matches" in argv for argv in recorder.calls[:3])
    assert sum(len(argv[argv.index("--") + 1 :]) for argv in recorder.calls[:3]) == 600
    assert first.complete and first.candidate_count == 1
    assert (
        first.matches[0].path.as_posix().startswith("sources/extracted/search/599.txt/")
    )
    assert not recorder.overlap
    assert all(not path.exists() for path in recorder.pattern_files)


@pytest.mark.parametrize(
    "raw_name", ["résumé (final).md", "line\nbreak.txt", "-leading.txt"]
)
def test_unusual_names_round_trip(search, repo_paths, monkeypatch, raw_name):
    relative = PurePosixPath(
        "sources/extracted/unusual", raw_name, "0" * 64, "drv_" + "0" * 64 + ".md"
    )
    item = make_source_representation(repo_paths, 7, extracted_path=relative)
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    result = search.search_active_sources(
        repo_paths,
        FakeLedger((item,)),
        search.SearchRequest("sources", "discovery", ("Alpha",), 1),
    )
    assert result.matches[0].path == relative


@pytest.mark.parametrize(
    "unsafe",
    ["missing", "directory", "symlink", "parent_symlink", "outside", "traversal"],
)
def test_unsafe_operands_block_before_rg(search, repo_paths, monkeypatch, unsafe):
    item = make_source_representation(repo_paths, 1)
    target = repo_paths.root / item.extracted_path
    if unsafe == "missing":
        target.unlink()
    elif unsafe == "directory":
        target.unlink()
        target.mkdir()
    elif unsafe == "symlink":
        target.unlink()
        target.symlink_to(repo_paths.root / "AGENTS.md")
    elif unsafe == "parent_symlink":
        directory = target.parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    elif unsafe == "outside":
        item = replace(item, extracted_path=PurePosixPath("AGENTS.md"))
    else:
        item = replace(
            item, extracted_path=PurePosixPath("sources/extracted/../../AGENTS.md")
        )
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    with pytest.raises(search.SearchOperandError):
        search.search_active_sources(
            repo_paths,
            FakeLedger((item,)),
            search.SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
    assert recorder.calls == []


def test_freshness_exact_ids_and_empty_corpus(search, repo_paths, monkeypatch):
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    ledger = make_active_ledger(repo_paths, 2, matching_numbers={0, 1})
    source_id = "src_" + "0" * 64
    result = search.search_active_sources(
        repo_paths,
        ledger,
        search.SearchRequest(
            "sources", None, ("Alpha",), 2, freshness_source_ids=(source_id,)
        ),
    )
    assert result.mode == "freshness" and result.searched_source_ids == (source_id,)
    assert result.candidate_count == 1
    recorder.calls.clear()
    result = search.search_active_sources(
        repo_paths,
        FakeLedger(()),
        search.SearchRequest("sources", "verification", ("Alpha",), 1),
    )
    assert result.complete and not result.matches and result.candidate_count == 0
    assert recorder.calls == []


def test_wiki_scope_direct_regular_markdown_only(search, repo_paths, monkeypatch):
    (repo_paths.wiki_pages / "a.md").write_text("Alpha")
    (repo_paths.wiki_questions / "b.md").write_text("Alpha")
    (repo_paths.wiki_pages / "no.txt").write_text("Alpha")
    (repo_paths.wiki_pages / "nested").mkdir()
    (repo_paths.wiki_pages / "nested/c.md").write_text("Alpha")
    (repo_paths.wiki_pages / "link.md").symlink_to(repo_paths.wiki_questions / "b.md")
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    result = search.search_wiki(
        repo_paths, FakeLedger(()), search.SearchRequest("wiki", None, ("Alpha",), 1)
    )
    assert [match.path.as_posix() for match in result.matches] == [
        "wiki/pages/a.md",
        "wiki/questions/b.md",
    ]
    assert result.searched_source_ids == ()


@pytest.mark.parametrize(
    "stdout",
    [b"x" * 65537, b"/not-an-operand\0", b"unterminated"],
    ids=["oversize", "foreign", "unterminated"],
)
def test_filename_stream_validation(search, repo_paths, monkeypatch, stdout):
    ledger = make_active_ledger(repo_paths, 1, matching_numbers={0})
    process = FakeRgProcess(stdout)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: process)
    with pytest.raises(search.SearchError):
        search.search_active_sources(
            repo_paths,
            ledger,
            search.SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
    assert process.terminated


@pytest.mark.parametrize(
    "variant",
    [
        "text",
        "bytes",
        "bad_base64",
        "noncanonical_base64",
        "invalid_utf8",
        "utf16_event",
        "foreign_path",
        "malformed",
        "unknown",
        "bad_line",
        "oversize",
        "binary",
        "summary",
    ],
)
def test_context_stream_bytes_validation_and_blocked_state(
    search, repo_paths, monkeypatch, variant
):
    ledger = make_active_ledger(repo_paths, 1, matching_numbers={0})
    recorder = ContentAwareRgRecorder()
    target = repo_paths.root / ledger.active_representations()[0].extracted_path
    data = {
        "path": {"text": str(target)},
        "lines": {"text": "Alpha\n"},
        "line_number": 1,
        "submatches": [],
    }
    event = {"type": "match", "data": data}
    if variant == "bytes":
        data["path"] = {"bytes": base64.b64encode(str(target).encode()).decode()}
        data["lines"] = {"bytes": base64.b64encode("Alpha résumé\n".encode()).decode()}
    elif variant == "bad_base64":
        data["lines"] = {"bytes": "%%%"}
    elif variant == "noncanonical_base64":
        data["lines"] = {"bytes": "QR=="}
    elif variant == "invalid_utf8":
        data["lines"] = {"bytes": "/w=="}
    elif variant == "foreign_path":
        data["path"] = {"text": str(repo_paths.root / "AGENTS.md")}
    elif variant == "malformed":
        del data["lines"]
    elif variant == "unknown":
        event["type"] = "oops"
    elif variant == "bad_line":
        data["line_number"] = True
    elif variant == "oversize":
        data["lines"] = {"text": "A" * 65536}
    elif variant == "binary":
        event = {
            "type": "end",
            "data": {"path": {"text": str(target)}, "binary_offset": 5, "stats": {}},
        }
    elif variant == "summary":
        event = {"type": "summary", "data": {}}
    payload = (
        json.dumps(event).encode("utf-16-le" if variant == "utf16_event" else "utf-8")
        + b"\n"
    )
    process = FakeRgProcess(payload)

    def call(argv, **kwargs):
        if "--files-with-matches" in argv:
            return recorder(argv, **kwargs)
        return process

    monkeypatch.setattr(subprocess, "Popen", call)
    if variant in {"text", "bytes"}:
        result = search.search_active_sources(
            repo_paths,
            ledger,
            search.SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
        assert result.complete and result.matches[0].text.startswith("Alpha")
    else:
        with pytest.raises(search.SearchError):
            search.search_active_sources(
                repo_paths,
                ledger,
                search.SearchRequest("sources", "discovery", ("Alpha",), 1),
            )
        metadata = next(
            (repo_paths.root / ".brain/search-runs").glob("*/metadata.json")
        )
        assert json.loads(metadata.read_text())["metadata"]["blocked"] is not None
        assert process.terminated
    assert all(not path.exists() for path in recorder.pattern_files)


def test_rg_failed_exit_bounds_stderr_and_waits(search, repo_paths, monkeypatch):
    ledger = make_active_ledger(repo_paths, 1, matching_numbers={0})
    waited = []
    process = FakeRgProcess(b"", on_wait=lambda: waited.append(True), returncode=2)

    def fail(argv, **kwargs):
        kwargs["stderr"].write(b"e" * 50000)
        return process

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(search.SearchExecutionError) as error:
        search.search_active_sources(
            repo_paths,
            ledger,
            search.SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
    assert waited == [True]
    assert len(str(error.value)) < 16500


@pytest.mark.parametrize(
    "ids",
    [
        ("src_" + "0" * 64, "src_" + "0" * 64),
        ("src_" + "1" * 64, "src_" + "0" * 64),
        ("src_short",),
    ],
)
def test_freshness_rejects_duplicate_unsorted_or_short_ids(search, ids):
    with pytest.raises(ValueError):
        search.SearchRequest("sources", None, ("Alpha",), 1, freshness_source_ids=ids)


def test_unknown_freshness_id_is_not_a_successful_empty_probe(
    search, repo_paths, monkeypatch
):
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    with pytest.raises(search.SearchOperandError):
        search.search_active_sources(
            repo_paths,
            FakeLedger(()),
            search.SearchRequest(
                "sources",
                None,
                ("Alpha",),
                1,
                freshness_source_ids=("src_" + "f" * 64,),
            ),
        )
    assert recorder.calls == []


def test_source_and_wiki_locks_cover_processes_and_verification(
    search, repo_paths, monkeypatch
):
    (repo_paths.wiki_pages / "alpha.md").write_text("Alpha")
    recorder = ContentAwareRgRecorder()

    def locked(argv, **kwargs):
        assert repo_paths.lock.is_file()
        assert (repo_paths.root / ".brain/wiki-write.lock").is_file()
        return recorder(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", locked)
    first = search.search_wiki(
        repo_paths, FakeLedger(()), search.SearchRequest("wiki", None, ("Alpha",), 1)
    )
    assert (
        not repo_paths.lock.exists()
        and not (repo_paths.root / ".brain/wiki-write.lock").exists()
    )
    search.verify_search_run_proof(
        repo_paths, FakeLedger(()), search.complete_search_run((first,))
    )
    with pytest.raises(ValueError):
        search.completed_search_pass((first,))


def test_deeply_nested_rg_event_is_a_blocked_execution_error(
    search, repo_paths, monkeypatch, recursive_json_decoder
):
    ledger = make_active_ledger(repo_paths, 1, matching_numbers={0})
    recorder = ContentAwareRgRecorder()

    def process(argv, **kwargs):
        if "--files-with-matches" in argv:
            return recorder(argv, **kwargs)
        return FakeRgProcess(b"[" * 1100 + b"0" + b"]" * 1100 + b"\n")

    monkeypatch.setattr(subprocess, "Popen", process)
    with pytest.raises(search.SearchExecutionError):
        search.search_active_sources(
            repo_paths,
            ledger,
            search.SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
