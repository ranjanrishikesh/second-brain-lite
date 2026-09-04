import importlib
import base64
import hashlib
import hmac
import json
import os
import stat
import subprocess
import tracemalloc
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers_knowledge import (
    FIXED_NOW,
    ContentAwareRgRecorder,
    FakeLedger,
    FakeRgProcess,
    make_active_ledger,
    make_source_representation,
    drain_search,
)


@pytest.fixture
def search(monkeypatch):
    assert importlib.util.find_spec("brainlib.search") is not None, (
        "authenticated search contract is absent"
    )
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    return importlib.import_module("brainlib.search")


def start(search, paths, *, count=5, page_size=2, **kwargs):
    ledger = make_active_ledger(paths, count, matching_numbers=set(range(count)))
    result = search.search_active_sources(
        paths,
        ledger,
        search.SearchRequest(
            "sources", "verification", ("Alpha",), 1, page_size=page_size
        ),
        **kwargs,
    )
    return ledger, result


def test_drain_one_logical_pass_and_verify_retained_proof(search, repo_paths):
    ledger, first = start(search, repo_paths)
    pages = drain_search(repo_paths, ledger, first)
    assert [page.page_index for page in pages] == [0, 1, 2]
    assert [len(page.matches) for page in pages] == [2, 2, 1]
    assert [page.complete for page in pages] == [False, False, True]
    assert pages[-1].next_cursor is None
    record = search.completed_search_pass(pages)
    assert record.name == "verification" and record.complete
    assert len(record.pages) == 3
    proof = search.complete_search_run(pages)
    assert proof.page_count == 3 and proof.match_count == 5
    search.verify_search_run_proof(repo_paths, ledger, proof)


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "path",
        "line_number",
        "kind",
        "reorder_pages",
        "reorder_matches",
        "omit_and_duplicate",
        "repartition",
    ],
)
def test_rehashed_returned_pages_cannot_forge_retained_proof(
    search, repo_paths, change
):
    ledger, first = start(search, repo_paths)
    pages = list(drain_search(repo_paths, ledger, first))
    retained = repo_paths.root / ".brain/search-runs" / first.run_id
    before = {path.name: path.read_bytes() for path in retained.iterdir()}
    genuine = search.complete_search_run(pages)
    search.verify_search_run_proof(repo_paths, ledger, genuine)
    assert search.completed_search_pass(pages).complete

    matches = [list(page.matches) for page in pages]
    if change in {"text", "path", "line_number", "kind"}:
        values = {
            "text": "fabricated evidence\n",
            "path": matches[1][0].path,
            "line_number": matches[0][0].line_number + 1,
            "kind": "context",
        }
        matches[0][0] = replace(matches[0][0], **{change: values[change]})
    elif change == "reorder_pages":
        matches[0], matches[1] = matches[1], matches[0]
    elif change == "reorder_matches":
        matches[0].reverse()
    elif change == "omit_and_duplicate":
        matches[0][0] = matches[1][0]
    else:
        # Keep exactly the same ordered records, but move a page boundary.
        matches[1].insert(0, matches[0].pop())
    forged = tuple(
        replace(
            page,
            matches=tuple(records),
            result_sha256=hashlib.sha256(
                b"".join(search.canonical(search.match_record(m)) for m in records)
            ).hexdigest(),
        )
        for page, records in zip(pages, matches, strict=True)
    )
    assert all(page.result_sha256 for page in forged)
    forged_proof = search.complete_search_run(forged)
    assert forged_proof.match_count == genuine.match_count
    assert forged_proof.page_count == genuine.page_count
    with pytest.raises(search.SearchRunBlocked, match="retained evidence"):
        search.verify_search_run_proof(repo_paths, ledger, forged_proof)
    assert {path.name: path.read_bytes() for path in retained.iterdir()} == before
    search.verify_search_run_proof(repo_paths, ledger, genuine)


@pytest.mark.parametrize("count", [0, 1, 5])
def test_proof_page_index_binding_is_canonical_and_round_trips(
    search, repo_paths, count
):
    ledger, first = start(search, repo_paths, count=count)
    pages = drain_search(repo_paths, ledger, first)
    proof = search.complete_search_run(pages)
    retained_index = (
        repo_paths.root / ".brain/search-runs" / first.run_id / "page-index.jsonl"
    ).read_bytes()
    assert proof.page_index_sha256 == hashlib.sha256(retained_index).hexdigest()
    payload = search.canonical(asdict(proof))
    decoded = json.loads(payload)
    decoded["terms"] = tuple(decoded["terms"])
    restored = search.SearchRunProof(**decoded)
    assert restored == proof
    assert search.canonical(asdict(restored)) == payload
    search.verify_search_run_proof(repo_paths, ledger, restored)


@pytest.mark.parametrize(
    "change",
    [
        "drop_first",
        "drop_middle",
        "undrained",
        "checksum",
        "matches",
        "binding",
        "cursor",
        "premature_complete",
    ],
)
def test_incomplete_or_forged_pages_cannot_become_evidence(search, repo_paths, change):
    ledger, first = start(search, repo_paths)
    pages = list(drain_search(repo_paths, ledger, first))
    if change == "drop_first":
        pages.pop(0)
    elif change == "drop_middle":
        pages.pop(1)
    elif change == "undrained":
        pages.pop()
    elif change == "checksum":
        pages[1] = replace(pages[1], result_sha256="0" * 64)
    elif change == "matches":
        pages[1] = replace(pages[1], matches=())
    elif change == "binding":
        pages[1] = replace(pages[1], terms=("other",))
    elif change == "cursor":
        pages[1] = replace(pages[1], request_cursor="bad")
    else:
        pages[0] = replace(pages[0], complete=True)
    with pytest.raises(ValueError):
        search.complete_search_run(pages)


def test_cursor_tamper_and_changed_revision_fail_closed(search, repo_paths):
    ledger, first = start(search, repo_paths)
    cursor = first.next_cursor
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(search.InvalidSearchCursor):
        search.resume_search(repo_paths, ledger, tampered)
    changed = FakeLedger(
        ledger.active_representations() + (make_source_representation(repo_paths, 9),)
    )
    with pytest.raises(search.SearchRunStale) as error:
        search.resume_search(repo_paths, changed, cursor)
    assert error.value.diagnostic.code == "search_run_stale"


def test_expiry_is_a_blocking_gap(search, repo_paths):
    ledger, first = start(search, repo_paths, now=FIXED_NOW)
    with pytest.raises(search.SearchRunExpired) as error:
        search.resume_search(
            repo_paths,
            ledger,
            first.next_cursor,
            now=FIXED_NOW + search.SEARCH_RUN_TTL + timedelta(seconds=1),
        )
    assert error.value.diagnostic.code == "search_run_expired"
    with pytest.raises(ValueError, match="complete"):
        search.completed_search_pass((first,))


@pytest.mark.parametrize(
    "filename",
    ["metadata.json", "candidates.jsonl", "results.jsonl", "page-index.jsonl"],
)
def test_retained_manifest_tamper_blocks_resume(search, repo_paths, filename):
    ledger, first = start(search, repo_paths)
    file = repo_paths.root / ".brain/search-runs" / first.run_id / filename
    with file.open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(search.SearchRunStale):
        search.resume_search(repo_paths, ledger, first.next_cursor)


def test_private_files_spool_limit_and_cleanup_horizon(search, repo_paths):
    ledger, first = start(search, repo_paths, now=FIXED_NOW)
    directory = repo_paths.root / ".brain/search-runs" / first.run_id
    assert directory.stat().st_mode & 0o777 == 0o700
    assert {file.name for file in directory.iterdir()} == {
        "metadata.json",
        "secret",
        "candidates.jsonl",
        "results.jsonl",
        "page-index.jsonl",
    }
    assert all(
        stat.S_ISREG(file.lstat().st_mode) and file.stat().st_mode & 0o777 == 0o600
        for file in directory.iterdir()
    )
    blocked = search.search_active_sources(
        repo_paths,
        ledger,
        search.SearchRequest("sources", "discovery", ("Alpha",), 1, max_run_bytes=256),
        now=FIXED_NOW,
    )
    assert not blocked.complete and blocked.next_cursor is None
    assert [gap.code for gap in blocked.coverage_gaps] == ["search_spool_limit"]
    assert (
        sum(
            (directory.parent / blocked.run_id / name).stat().st_size
            for name in ("candidates.jsonl", "results.jsonl", "page-index.jsonl")
        )
        <= 256
    )
    with pytest.raises(ValueError, match="complete"):
        search.complete_search_run((blocked,))
    complete = search.search_active_sources(
        repo_paths,
        ledger,
        search.SearchRequest("sources", "verification", ("absent",), 1),
        now=FIXED_NOW,
    )
    assert complete.complete and complete.matches == ()
    assert search.completed_search_pass((complete,)).pages[0].match_count == 0
    unrelated = directory.parent / "not-a-search-run"
    unrelated.mkdir()
    removed = search.cleanup_search_runs(
        repo_paths, now=FIXED_NOW + search.SEARCH_RUN_TTL + timedelta(seconds=1)
    )
    assert PurePosixPath(".brain/search-runs") / complete.run_id in removed
    assert directory.is_dir() and unrelated.is_dir()
    with pytest.raises(search.SearchRunExpired):
        search.resume_search(
            repo_paths,
            ledger,
            first.next_cursor,
            now=FIXED_NOW + search.SEARCH_RUN_TTL + timedelta(seconds=1),
        )
    removed = search.cleanup_search_runs(
        repo_paths, now=FIXED_NOW + 2 * search.SEARCH_RUN_TTL + timedelta(seconds=2)
    )
    assert PurePosixPath(".brain/search-runs") / first.run_id in removed
    assert unrelated.is_dir()


def test_ten_thousand_matches_have_bounded_peak_and_page(search, repo_paths):
    ledger = make_active_ledger(repo_paths, 10000, matching_numbers=set(range(10000)))
    tracemalloc.start()
    try:
        first = search.search_active_sources(
            repo_paths,
            ledger,
            search.SearchRequest("sources", "discovery", ("Alpha",), 1, page_size=25),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    assert len(first.matches) == 25 and first.candidate_count == 10000
    assert sum("--files-with-matches" in argv for argv in subprocess.Popen.calls) == 40
    assert sum("--json" in argv for argv in subprocess.Popen.calls) == 40


def test_expiry_discovered_by_resume_retains_explicit_gap(search, repo_paths):
    ledger, first = start(search, repo_paths, now=FIXED_NOW)
    later = FIXED_NOW + search.SEARCH_RUN_TTL + timedelta(seconds=1)
    with pytest.raises(search.SearchRunExpired):
        search.resume_search(repo_paths, ledger, first.next_cursor, now=later)
    search.cleanup_search_runs(repo_paths, now=later + timedelta(seconds=1))
    with pytest.raises(search.SearchRunExpired):
        search.resume_search(
            repo_paths, ledger, first.next_cursor, now=later + timedelta(seconds=2)
        )


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "index_zero",
        "index_oob",
        "index_bool",
        "expiry",
        "unknown",
        "bad_mac",
        "padding",
        "garbage",
    ],
)
def test_malformed_or_out_of_range_cursors(search, repo_paths, change):
    ledger, first = start(search, repo_paths)
    token = first.next_cursor
    value = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    if change == "version":
        value["version"] = 2
    elif change == "index_zero":
        value["page_index"] = 0
    elif change == "index_oob":
        value["page_index"] = 999
    elif change == "index_bool":
        value["page_index"] = True
    elif change == "expiry":
        value["expires_at"] = "2100-01-01T00:00:00+00:00"
    elif change == "unknown":
        value["run_id"] = "srch_" + "0" * 32
    elif change == "bad_mac":
        value["mac"] = "0" * 64
    value.pop("mac", None)
    secret = (
        repo_paths.root / ".brain/search-runs" / first.run_id / "secret"
    ).read_bytes()

    def canonical(obj):
        return (
            json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()

    value["mac"] = (
        "0" * 64
        if change == "bad_mac"
        else hmac.new(secret, canonical(value), "sha256").hexdigest()
    )
    token = base64.urlsafe_b64encode(canonical(value)).decode().rstrip("=")
    if change == "padding":
        token += "="
    elif change == "garbage":
        token = "../outside"
    with pytest.raises(search.InvalidSearchCursor):
        search.resume_search(repo_paths, ledger, token)


@pytest.mark.parametrize(
    "change",
    [
        "source_identity",
        "wiki_content",
        "wiki_corpus",
        "repository",
        "manifest_symlink",
    ],
)
def test_operand_and_repository_binding_is_rechecked(search, repo_paths, change):
    if change.startswith("wiki"):
        (repo_paths.wiki_pages / "alpha.md").write_text("Alpha")
        (repo_paths.wiki_pages / "beta.md").write_text("Alpha")
        ledger = FakeLedger(())
        first = search.search_wiki(
            repo_paths,
            ledger,
            search.SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        )
        if change == "wiki_content":
            (repo_paths.wiki_pages / "alpha.md").write_text("Changed")
        else:
            ledger = FakeLedger((make_source_representation(repo_paths, 99),))
    else:
        ledger, first = start(search, repo_paths)
        if change == "source_identity":
            items = ledger.active_representations()
            ledger = FakeLedger((replace(items[0], output_sha256="f" * 64), *items[1:]))
        elif change == "manifest_symlink":
            manifest = repo_paths.root / first.candidate_manifest
            manifest.unlink()
            manifest.symlink_to(repo_paths.root / "AGENTS.md")
        else:
            moved = repo_paths.root.with_name("moved-repository")
            repo_paths.root.rename(moved)
            from brainlib.layout import RepoPaths

            repo_paths = RepoPaths.discover(moved)
    with pytest.raises(search.SearchRunStale):
        search.resume_search(repo_paths, ledger, first.next_cursor)


def test_unserved_or_forged_proof_is_rejected(search, repo_paths):
    ledger, first = start(search, repo_paths)
    forged = search.SearchRunProof(
        first.run_id,
        first.corpus_revision,
        first.scope,
        first.mode,
        first.pass_name,
        first.terms,
        first.candidate_count,
        first.candidate_manifest_sha256,
        3,
        5,
        hashlib.sha256(
            (
                repo_paths.root
                / ".brain/search-runs"
                / first.run_id
                / "page-index.jsonl"
            ).read_bytes()
        ).hexdigest(),
    )
    with pytest.raises(search.SearchRunBlocked):
        search.verify_search_run_proof(repo_paths, ledger, forged)
    pages = drain_search(repo_paths, ledger, first)
    proof = search.complete_search_run(pages)
    with pytest.raises(search.SearchRunBlocked):
        search.verify_search_run_proof(
            repo_paths, ledger, replace(proof, terms=("fabricated",))
        )
    with pytest.raises(search.SearchRunExpired):
        search.verify_search_run_proof(
            repo_paths, ledger, proof, now=FIXED_NOW + 100 * search.SEARCH_RUN_TTL
        )


def test_cleanup_never_follows_run_or_state_symlinks(search, repo_paths, tmp_path):
    _, first = start(search, repo_paths, count=1, now=FIXED_NOW)
    parent = repo_paths.root / ".brain/search-runs"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_text("keep")
    link = parent / ("srch_" + "f" * 32)
    link.symlink_to(external, target_is_directory=True)
    target = parent / first.run_id / "results.jsonl"
    target.unlink()
    target.symlink_to(external / "keep")
    removed = search.cleanup_search_runs(
        repo_paths, now=FIXED_NOW + 3 * search.SEARCH_RUN_TTL
    )
    assert (
        removed == ()
        and link.is_symlink()
        and (external / "keep").read_text() == "keep"
    )


@pytest.mark.parametrize("terms", [("",), (" ",), ("Alpha\nBeta",), ("Alpha", "Alpha")])
def test_completed_page_terms_must_still_be_valid(search, repo_paths, terms):
    ledger, first = start(search, repo_paths, count=1)
    with pytest.raises(ValueError):
        search.complete_search_run((replace(first, terms=terms),))


def test_deeply_nested_cursor_is_a_canonical_invalid_cursor(
    search, repo_paths, recursive_json_decoder
):
    payload = b"[" * 1100 + b"0" + b"]" * 1100
    cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    with pytest.raises(search.InvalidSearchCursor):
        search.resume_search(repo_paths, FakeLedger(()), cursor)


def test_deeply_nested_metadata_is_stale(search, repo_paths, recursive_json_decoder):
    ledger, first = start(search, repo_paths)
    metadata = repo_paths.root / ".brain/search-runs" / first.run_id / "metadata.json"
    metadata.write_bytes(b"[" * 1100 + b"0" + b"]" * 1100)
    with pytest.raises(search.SearchRunStale):
        search.resume_search(repo_paths, ledger, first.next_cursor)


class CandidateRgRecorder:
    """Small JSON-event recorder with byte-accurate candidate submatches."""

    def __init__(self, *, malformed: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.malformed = malformed

    def __call__(self, argv: list[str], **kwargs: object) -> FakeRgProcess:
        assert kwargs.get("shell") is False
        assert kwargs["stdout"] == subprocess.PIPE
        self.calls.append(argv)
        pattern = Path(argv[argv.index("--file") + 1])
        terms = tuple(pattern.read_text(encoding="utf-8").splitlines())
        events: list[dict[str, object]] = []
        for operand in argv[argv.index("--") + 1 :]:
            path = Path(operand)
            for number, text in enumerate(
                path.read_text(encoding="utf-8").splitlines(keepends=True), start=1
            ):
                raw = text.encode("utf-8")
                submatches = []
                for term in reversed(terms):
                    match = term.encode("utf-8")
                    start = raw.find(match)
                    if start >= 0:
                        submatches.append(
                            {
                                "match": {"text": term},
                                "start": start,
                                "end": start + len(match),
                            }
                        )
                if not submatches:
                    continue
                if self.malformed == "empty":
                    submatches = []
                elif self.malformed == "wrong_bytes":
                    submatches[0]["match"] = {"text": "fabricated"}
                elif self.malformed == "bad_bounds":
                    submatches[0]["end"] = len(raw) + 1
                events.append(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": operand},
                            "lines": {"text": text},
                            "line_number": number,
                            "absolute_offset": 0,
                            "submatches": submatches,
                        },
                    }
                )
                break
        return FakeRgProcess(
            "".join(json.dumps(event) + "\n" for event in events),
            returncode=0 if events else 1,
        )


def _candidate_runs():
    return importlib.import_module("brainlib.search_runs")


def _start_candidates(search, paths, ledger, **kwargs):
    return _candidate_runs()._start_link_candidate_run(paths, ledger, **kwargs)


def _drain_candidates(search, paths, ledger, first):
    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(
            _candidate_runs()._resume_link_candidate_run(
                paths, ledger, pages[-1].next_cursor
            )
        )
    return tuple(pages)


def _write_candidate_pages(paths, values: dict[str, str]) -> None:
    for relative, text in values.items():
        destination = paths.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")


def test_candidate_run_binds_absent_target_and_excludes_live_self(
    search, repo_paths, monkeypatch
):
    recorder = CandidateRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "Alpha self\n",
            "wiki/pages/beta.md": "Alpha Beta\n",
            "wiki/questions/what-is-alpha.md": "A. Example\n",
        },
    )

    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "new-topic.md",
        terms=("Alpha", "A. Example"),
        page_size=1,
    )
    pages = _drain_candidates(search, repo_paths, FakeLedger(()), first)
    assert first.page_path == PurePosixPath("wiki/pages/new-topic.md")
    assert [candidate.path.as_posix() for page in pages for candidate in page.candidates] == [
        "wiki/pages/alpha.md",
        "wiki/pages/beta.md",
        "wiki/questions/what-is-alpha.md",
    ]
    assert pages[0].candidates[0].matched_term == "Alpha"
    assert pages[-1].complete and pages[-1].next_cursor is None
    proof = _candidate_runs()._complete_link_candidate_run(pages)
    assert proof.candidate_count == 3 and proof.page_count == 3

    self_first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=10,
    )
    assert {
        candidate.path.as_posix() for candidate in self_first.candidates
    } == {"wiki/pages/beta.md"}
    assert all("alpha.md" not in argv for argv in recorder.calls[-1:])


@pytest.mark.parametrize(
    "page_path",
    [
        "wiki/pages/nested/alpha.md",
        "wiki/pages/../questions/alpha.md",
        "wiki/pages/alpha\\beta.md",
        "wiki/pages/c1\u0085.md",
        "wiki/pages/format\u200b.md",
        "wiki/pages/line\u2028.md",
        "wiki/pages/paragraph\u2029.md",
        "wiki/index.md",
    ],
)
def test_candidate_target_requires_exact_direct_logical_path(
    search, repo_paths, monkeypatch, page_path
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    with pytest.raises(ValueError, match="logical wiki"):
        _start_candidates(
            search,
            repo_paths,
            FakeLedger(()),
            page_path=page_path,
            terms=("Alpha",),
        )


def test_candidate_run_fences_normal_resume_and_legacy_normal_metadata(
    search, repo_paths, monkeypatch
):
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "Alpha\n",
            "wiki/pages/beta.md": "Alpha\n",
            "wiki/pages/gamma.md": "Alpha\n",
        },
    )
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    candidate = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )
    assert candidate.next_cursor is not None
    with pytest.raises(search.InvalidSearchCursor, match="candidate"):
        search.resume_search(repo_paths, FakeLedger(()), candidate.next_cursor)

    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    ledger, normal = start(search, repo_paths, count=3, page_size=1)
    metadata_path = repo_paths.root / ".brain/search-runs" / normal.run_id / "metadata.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata = document["metadata"]
    metadata.pop("purpose")
    metadata.pop("excluded_page_path")
    secret = (metadata_path.parent / "secret").read_bytes()
    metadata_path.write_bytes(
        search.canonical(
            {
                "metadata": metadata,
                "mac": hmac.new(secret, search.canonical(metadata), "sha256").hexdigest(),
            }
        )
    )
    assert search.resume_search(repo_paths, ledger, normal.next_cursor).page_index == 1
    with pytest.raises(search.InvalidSearchCursor, match="normal"):
        _candidate_runs()._resume_link_candidate_run(
            repo_paths, ledger, normal.next_cursor
        )


@pytest.mark.parametrize("changed", ["target", "candidate"])
def test_candidate_run_stales_when_target_or_other_live_record_changes(
    search, repo_paths, monkeypatch, changed
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "Alpha target\n",
            "wiki/pages/beta.md": "Alpha candidate\n",
            "wiki/pages/gamma.md": "Alpha candidate\n",
        },
    )
    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )
    destination = repo_paths.wiki_pages / ("alpha.md" if changed == "target" else "beta.md")
    destination.write_text("changed\n", encoding="utf-8")
    assert first.next_cursor is not None
    with pytest.raises(search.SearchRunStale):
        _candidate_runs()._resume_link_candidate_run(
            repo_paths, FakeLedger(()), first.next_cursor
        )


def test_candidate_pages_are_one_per_document_zero_safe_and_late_paginated(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(repo_paths, {"wiki/pages/alpha.md": "target\n"})
    empty = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
    )
    assert empty.complete and empty.candidate_count == 0 and empty.candidates == ()

    values = {"wiki/pages/alpha.md": "Alpha target\n"}
    values.update(
        {
            f"wiki/pages/candidate-{number:03}.md": "Alpha once\nAlpha twice\n"
            for number in range(301)
        }
    )
    _write_candidate_pages(repo_paths, values)
    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=100,
    )
    pages = _drain_candidates(search, repo_paths, FakeLedger(()), first)
    candidates = tuple(candidate for page in pages for candidate in page.candidates)
    assert [len(page.candidates) for page in pages] == [100, 100, 100, 1]
    assert len(candidates) == 301
    assert candidates[-1].path == PurePosixPath("wiki/pages/candidate-300.md")
    assert all(candidate.line == 1 for candidate in candidates)


@pytest.mark.parametrize("malformed", ["empty", "wrong_bytes", "bad_bounds"])
def test_candidate_run_requires_validated_rg_byte_submatches(
    search, repo_paths, monkeypatch, malformed
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder(malformed=malformed))
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
        },
    )
    with pytest.raises(search.SearchExecutionError, match="submatch"):
        _start_candidates(
            search,
            repo_paths,
            FakeLedger(()),
            page_path=repo_paths.wiki_pages / "alpha.md",
            terms=("Alpha",),
        )


def test_candidate_proof_rejects_undrained_reordered_and_tampered_pages(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
            "wiki/pages/gamma.md": "Alpha\n",
            "wiki/pages/delta.md": "Alpha\n",
        },
    )
    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )
    with pytest.raises(ValueError, match="complete"):
        _candidate_runs()._complete_link_candidate_run((first,))
    pages = _drain_candidates(search, repo_paths, FakeLedger(()), first)
    with pytest.raises(ValueError, match="contiguous"):
        _candidate_runs()._complete_link_candidate_run(tuple(reversed(pages)))
    tampered = replace(pages[0], result_sha256="0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        _candidate_runs()._complete_link_candidate_run((tampered, *pages[1:]))


def test_candidate_proof_rejects_rehashed_altered_page_payload(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
            "wiki/pages/gamma.md": "Alpha\n",
        },
    )
    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )
    pages = _drain_candidates(search, repo_paths, FakeLedger(()), first)
    altered_candidate = replace(pages[0].candidates[0], context="fabricated\n")
    altered_payload = search.canonical(
        {
            "path": altered_candidate.path.as_posix(),
            "line": altered_candidate.line,
            "matched_term": altered_candidate.matched_term,
            "context": altered_candidate.context,
            "kind": altered_candidate.kind,
        }
    )
    altered_page = replace(
        pages[0],
        candidates=(altered_candidate,),
        result_sha256=hashlib.sha256(altered_payload).hexdigest(),
    )
    with pytest.raises(ValueError, match="candidate manifest"):
        _candidate_runs()._complete_link_candidate_run(
            (altered_page, *pages[1:])
        )


def test_candidate_metadata_unsafe_target_fails_closed_as_stale(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
            "wiki/pages/gamma.md": "Alpha\n",
        },
    )
    first = _start_candidates(
        search,
        repo_paths,
        FakeLedger(()),
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )
    target = repo_paths.wiki_pages / "alpha.md"
    target.unlink()
    target.symlink_to(repo_paths.wiki_pages / "beta.md")
    with pytest.raises(search.SearchRunStale, match="metadata"):
        _candidate_runs()._resume_link_candidate_run(
            repo_paths, FakeLedger(()), first.next_cursor
        )


def test_normal_proof_verifier_rejects_completed_candidate_purpose_run(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
        },
    )
    ledger = FakeLedger(())
    first = _start_candidates(
        search,
        repo_paths,
        ledger,
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
    )
    candidate_proof = _candidate_runs()._complete_link_candidate_run((first,))
    forged_normal = search.SearchRunProof(
        candidate_proof.run_id,
        candidate_proof.corpus_revision,
        "wiki",
        "research",
        None,
        candidate_proof.terms,
        candidate_proof.candidate_count,
        candidate_proof.candidate_manifest_sha256,
        candidate_proof.page_count,
        candidate_proof.candidate_count,
        "0" * 64,
    )
    with pytest.raises(search.SearchRunBlocked, match="candidate"):
        search.verify_search_run_proof(repo_paths, ledger, forged_normal)


@pytest.mark.parametrize(
    "name",
    [
        "a\\b.md",
        "line\nbreak.md",
        "unit\x1f.md",
        "c1\u0085.md",
        "format\u200b.md",
        "line\u2028.md",
        "paragraph\u2029.md",
    ],
)
def test_candidate_tree_rejects_unsafe_direct_filename_before_rg(
    search, repo_paths, monkeypatch, name
):
    recorder = CandidateRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            f"wiki/pages/{name}": "Alpha\n",
        },
    )
    with pytest.raises(search.SearchOperandError, match="non-logical"):
        _start_candidates(
            search,
            repo_paths,
            FakeLedger(()),
            page_path=repo_paths.wiki_pages / "alpha.md",
            terms=("Alpha",),
        )
    assert recorder.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "a\\b.md",
        "line\nbreak.md",
        "unit\x1f.md",
        "c1\u009f.md",
        "format\u200b.md",
        "line\u2028.md",
        "paragraph\u2029.md",
    ],
)
def test_candidate_record_rejects_unsafe_direct_filename(name):
    runs = _candidate_runs()
    candidate = runs._LinkCandidate(
        PurePosixPath("wiki", "pages", name), 1, "Alpha", "Alpha\n", "page"
    )
    with pytest.raises(ValueError, match="candidate path"):
        runs._candidate_record(candidate)


def test_candidate_filename_gate_allows_visible_unicode_and_spaces(search):
    assert search.is_canonical_wiki_record_name("béta topic.md")


def test_candidate_tree_rejects_surrogateescaped_filename_before_hashing_or_rg(
    search, repo_paths, monkeypatch
):
    recorder = CandidateRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    _write_candidate_pages(repo_paths, {"wiki/pages/alpha.md": "target\n"})

    # APFS refuses invalid raw UTF-8 bytes, but POSIX `os.listdir()` can expose
    # this exact surrogateescaped value on filesystems that permit them.
    unsafe_name = "\udcff.md"
    original_listdir = os.listdir
    pages_stat = repo_paths.wiki_pages.stat()

    def listdir_with_surrogate(directory):
        names = original_listdir(directory)
        if isinstance(directory, int):
            observed = os.fstat(directory)
            if (observed.st_dev, observed.st_ino) == (
                pages_stat.st_dev,
                pages_stat.st_ino,
            ):
                return [*names, unsafe_name]
        return names

    original_stat = os.stat

    def stat_surrogate(name, *args, **kwargs):
        if name == unsafe_name and kwargs.get("dir_fd") is not None:
            return original_stat(
                "alpha.md",
                dir_fd=kwargs["dir_fd"],
                follow_symlinks=kwargs.get("follow_symlinks", True),
            )
        return original_stat(name, *args, **kwargs)

    identity_calls = []
    original_identity = search._regular_identity

    def record_identity(path, **kwargs):
        if path.name == unsafe_name:
            identity_calls.append(path)
            return 1, 1, "0" * 64
        return original_identity(path, **kwargs)

    monkeypatch.setattr(search.os, "listdir", listdir_with_surrogate)
    monkeypatch.setattr(search.os, "stat", stat_surrogate)
    monkeypatch.setattr(search, "_regular_identity", record_identity)
    with pytest.raises(search.SearchOperandError, match="non-logical"):
        _start_candidates(
            search,
            repo_paths,
            FakeLedger(()),
            page_path=repo_paths.wiki_pages / "alpha.md",
            terms=("Alpha",),
        )
    assert identity_calls == [] and recorder.calls == []


def test_locked_candidate_verifier_never_reenters_repository_locks(
    search, repo_paths, monkeypatch
):
    monkeypatch.setattr(subprocess, "Popen", CandidateRgRecorder())
    _write_candidate_pages(
        repo_paths,
        {
            "wiki/pages/alpha.md": "target\n",
            "wiki/pages/beta.md": "Alpha\n",
        },
    )
    ledger = FakeLedger(())
    first = _start_candidates(
        search,
        repo_paths,
        ledger,
        page_path=repo_paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
    )
    proof = _candidate_runs()._complete_link_candidate_run((first,))

    def no_lock(*_args, **_kwargs):
        raise AssertionError("locked verifier must not acquire a lock")

    monkeypatch.setattr(_candidate_runs().SourceWriteLock, "acquire", no_lock)
    _candidate_runs()._verify_link_candidate_run_locked(
        repo_paths,
        ledger,
        run_id=proof.run_id,
        corpus_revision=proof.corpus_revision,
        page_path=proof.page_path,
        terms=proof.terms,
        candidate_manifest_sha256=proof.candidate_manifest_sha256,
        page_count=proof.page_count,
        candidate_count=proof.candidate_count,
    )
