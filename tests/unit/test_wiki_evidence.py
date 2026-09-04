from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path, PurePosixPath

import pytest

import brainlib.wiki_evidence as wiki_evidence
from brainlib.evidence import CitationRef
from brainlib.search import SearchRequest, search_wiki
from brainlib.wiki_evidence import WikiEvidenceError, build_wiki_evidence_packet
from tests.helpers_knowledge import KnowledgeScenario, drain_search


def test_build_wiki_evidence_packet_revalidates_exact_citations_and_complete_run(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    first = search_wiki(
        scenario.paths, scenario.ledger,
        SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
    )
    pages = drain_search(scenario.paths, scenario.ledger, first)
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-page-2")
    packet = build_wiki_evidence_packet(
        scenario.paths, scenario.ledger, question_id="question-alpha",
        search_pages=pages, matched_paths=(ref.document_path,),
        supporting_citations=(ref,), counterevidence_citations=(),
    )
    assert packet.complete is True
    assert packet.search_run_id == first.run_id
    assert packet.corpus_revision == first.corpus_revision
    assert packet.matched_records[0].path == ref.document_path
    assert packet.revalidated_citations[0].citation_id == ref.citation_id


def test_wiki_evidence_builder_rejects_undrained_search(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    first = search_wiki(
        scenario.paths, scenario.ledger,
        SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/alpha.md"), "unused")
    with pytest.raises(WikiEvidenceError) as undrained:
        build_wiki_evidence_packet(
            scenario.paths, scenario.ledger, question_id="question-alpha",
            search_pages=(first,), matched_paths=(ref.document_path,),
            supporting_citations=(ref,), counterevidence_citations=(),
        )
    assert undrained.value.diagnostic.code == "wiki_search_incomplete"


def test_wiki_evidence_builder_rejects_changed_exact_citation_target(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    pages = drain_search(
        scenario.paths, scenario.ledger,
        search_wiki(
            scenario.paths, scenario.ledger,
            SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        ),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-page-2")
    representation = scenario.ledger.active_representations()[0]
    raw = scenario.paths.raw / representation.raw_path
    raw.write_bytes(raw.read_bytes() + b"changed after search")
    with pytest.raises(WikiEvidenceError) as invalid:
        build_wiki_evidence_packet(
            scenario.paths, scenario.ledger, question_id="question-alpha",
            search_pages=pages, matched_paths=(ref.document_path,),
            supporting_citations=(ref,), counterevidence_citations=(),
        )
    assert invalid.value.diagnostic.code == "wiki_citation_invalid"


def test_wiki_evidence_builder_rejects_malformed_selected_page(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    page = scenario.paths.wiki_pages / "current.md"
    page.write_text(
        page.read_text(encoding="utf-8").split("---\n", 2)[-1], encoding="utf-8"
    )
    pages = drain_search(
        scenario.paths,
        scenario.ledger,
        search_wiki(
            scenario.paths,
            scenario.ledger,
            SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        ),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-page-2")

    with pytest.raises(WikiEvidenceError) as invalid:
        build_wiki_evidence_packet(
            scenario.paths,
            scenario.ledger,
            question_id="question-alpha",
            search_pages=pages,
            matched_paths=(ref.document_path,),
            supporting_citations=(ref,),
            counterevidence_citations=(),
        )

    assert invalid.value.diagnostic.code == "wiki_citation_invalid"


def test_wiki_evidence_rechecks_proof_after_pinned_document_capture(
    scenario_repo: Callable[[str], KnowledgeScenario],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = scenario_repo("citations/current")
    page = scenario.paths.wiki_pages / "current.md"
    outside = tmp_path / "outside.md"
    outside.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    pages = drain_search(
        scenario.paths,
        scenario.ledger,
        search_wiki(
            scenario.paths,
            scenario.ledger,
            SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        ),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-page-2")
    capture_documents = wiki_evidence._load_live_documents

    def capture_then_swap(paths):
        captured = capture_documents(paths)
        page.unlink()
        page.symlink_to(outside)
        return captured

    monkeypatch.setattr(
        wiki_evidence, "_load_live_documents", capture_then_swap, raising=False
    )
    with pytest.raises(WikiEvidenceError) as stale:
        build_wiki_evidence_packet(
            scenario.paths,
            scenario.ledger,
            question_id="question-alpha",
            search_pages=pages,
            matched_paths=(ref.document_path,),
            supporting_citations=(ref,),
            counterevidence_citations=(),
        )

    assert stale.value.diagnostic.code == "wiki_search_incomplete"


def test_wiki_evidence_rejects_capture_restore_content_alternation(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("citations/current")
    page = scenario.paths.wiki_pages / "current.md"
    original = page.read_text(encoding="utf-8")
    original_stat = page.stat()
    altered = original.replace("id: current", "id: altered")
    assert altered != original and len(altered.encode()) == len(original.encode())
    pages = drain_search(
        scenario.paths,
        scenario.ledger,
        search_wiki(
            scenario.paths,
            scenario.ledger,
            SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        ),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-page-2")
    capture_documents = wiki_evidence._load_live_documents

    def capture_altered_then_restore(paths):
        page.write_text(altered, encoding="utf-8")
        captured = capture_documents(paths)
        page.write_text(original, encoding="utf-8")
        os.utime(page, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return captured

    monkeypatch.setattr(
        wiki_evidence, "_load_live_documents", capture_altered_then_restore
    )
    with pytest.raises(WikiEvidenceError) as stale:
        build_wiki_evidence_packet(
            scenario.paths,
            scenario.ledger,
            question_id="question-alpha",
            search_pages=pages,
            matched_paths=(ref.document_path,),
            supporting_citations=(ref,),
            counterevidence_citations=(),
        )

    assert stale.value.diagnostic.code == "wiki_search_incomplete"
