from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import brainlib.graph as graph
import brainlib.wiki_models as wiki_models
from brainlib.graph import (
    LinkCandidateResult,
    complete_link_candidate_run,
    find_link_candidates,
    render_wiki_index,
    resume_link_candidates,
    validate_graph,
)


_REVISION = "a" * 64


def _page(
    identifier: str,
    title: str,
    *,
    aliases: tuple[str, ...] = (),
    summary: str = "A concise summary.",
    details: str = "A stable topic detail.",
    related_pages: str = "",
    related_questions: str = "",
    extra_sections: str = "",
    sources: str = "",
) -> str:
    alias_text = "[" + ",".join(aliases) + "]"
    return (
        "---\n"
        f"id: {identifier}\n"
        f"title: {title}\n"
        "description: A documented topic.\n"
        "type: concept\n"
        f"aliases: {alias_text}\n"
        "created: 2026-09-04\n"
        "updated: 2026-09-04\n"
        "---\n"
        f"# {title}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Details\n{details}\n\n"
        f"{extra_sections}"
        f"## Related pages\n{related_pages}\n\n"
        f"## Related questions\n{related_questions}\n\n"
        f"## Sources\n{sources}"
    )


def _question(
    identifier: str,
    title: str,
    *,
    canonical_question: str | None = None,
    prior_phrasings: tuple[str, ...] = (),
    current_answer: str = "A durable answer.",
    related_pages: str = "",
) -> str:
    prior_text = "[" + ",".join(prior_phrasings) + "]"
    return (
        "---\n"
        "schema_version: 2\n"
        f"id: {identifier}\n"
        f"title: {title}\n"
        "description: A documented question.\n"
        f"canonical_question: {canonical_question or title}\n"
        f"prior_phrasings: {prior_text}\n"
        "answer_status: answered\n"
        "interpretation_decision: not_applicable\n"
        f"corpus_revision: {_REVISION}\n"
        "last_researched: 2026-09-04\n"
        "discovery_terms: [topic]\n"
        "expansion_terms: [topic detail]\n"
        "verification_terms: [topic check]\n"
        "---\n"
        f"# {title}\n\n"
        f"## Current answer\n{current_answer}\n\n"
        "## Supporting evidence\nNone.\n\n"
        "## Contradictory evidence\nNone.\n\n"
        f"## Related pages\n{related_pages}\n\n"
        "## Sources\n"
    )


def _documents(paths, *records: tuple[str, str]) -> dict[Path, str]:
    result: dict[Path, str] = {}
    for logical, text in records:
        result[paths.root / logical] = text
    return result


def _generated_index(paths, documents: dict[Path, str]) -> str:
    return render_wiki_index(paths, documents)


def test_find_link_candidates_adapts_all_pages_and_questions_and_proves_the_chain(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/candidates")

    first = find_link_candidates(
        scenario.paths,
        scenario.ledger,
        page_path=scenario.paths.wiki_pages / "alpha.md",
        terms=("Alpha", "A. Example"),
        page_size=1,
    )

    assert isinstance(first, LinkCandidateResult)
    assert first.page_index == 0
    assert first.candidates and first.candidates[0].path != Path("alpha.md")
    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(
            resume_link_candidates(
                scenario.paths, scenario.ledger, pages[-1].next_cursor
            )
        )
    proof = complete_link_candidate_run(tuple(pages))
    candidates = tuple(item for page in pages for item in page.candidates)

    assert [candidate.path.as_posix() for candidate in candidates] == [
        "wiki/pages/beta.md",
        "wiki/pages/zzz-related.md",
        "wiki/questions/what-is-alpha.md",
    ]
    assert all(candidate.path.as_posix() != "wiki/pages/alpha.md" for candidate in candidates)
    assert proof.page_count == len(pages)
    assert proof.candidate_count == len(candidates)


def test_complete_link_candidate_run_rejects_an_undrained_public_chain(scenario_repo) -> None:
    scenario = scenario_repo("graph/candidates")
    first = find_link_candidates(
        scenario.paths,
        scenario.ledger,
        page_path=scenario.paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )

    with pytest.raises(ValueError, match="complete candidate search"):
        complete_link_candidate_run((first,))


def test_complete_link_candidate_run_rejects_a_forged_public_candidate(scenario_repo) -> None:
    scenario = scenario_repo("graph/candidates")
    first = find_link_candidates(
        scenario.paths,
        scenario.ledger,
        page_path=scenario.paths.wiki_pages / "alpha.md",
        terms=("Alpha",),
        page_size=1,
    )

    with pytest.raises(ValueError, match="candidate record"):
        complete_link_candidate_run((replace(first, candidates=(object(),)),))  # type: ignore[arg-type]


def test_validate_graph_requires_reciprocal_relationships(scenario_repo) -> None:
    scenario = scenario_repo("graph/nonreciprocal")

    report = validate_graph(scenario.paths)

    assert "relationship_not_reciprocal" in {issue.code for issue in report.issues}


def test_validate_graph_requires_the_first_visible_occurrence_to_be_linked(
    scenario_repo,
) -> None:
    scenario = scenario_repo("graph/unlinked")

    report = validate_graph(scenario.paths)

    assert "related_topic_first_occurrence_unlinked" in {
        issue.code for issue in report.issues
    }


def test_validate_graph_accepts_canonical_non_ascii_path_and_fragment(scenario_repo) -> None:
    scenario = scenario_repo("graph/encoded")

    report = validate_graph(scenario.paths)

    assert report.ok, report.issues


@pytest.mark.parametrize(
    ("scenario_name", "expected_codes"),
    [
        ("graph/candidates", set()),
        ("graph/valid", set()),
        ("graph/encoded", set()),
        ("graph/nonreciprocal", {"relationship_not_reciprocal"}),
        ("graph/unlinked", {"related_topic_first_occurrence_unlinked"}),
    ],
)
def test_graph_fixtures_have_the_declared_outcomes(
    scenario_repo, scenario_name: str, expected_codes: set[str]
) -> None:
    scenario = scenario_repo(scenario_name)

    report = validate_graph(scenario.paths)

    assert {issue.code for issue in report.issues} == expected_codes


def test_validate_graph_uses_only_the_supplied_postimage_mapping(repo_paths) -> None:
    alpha = repo_paths.wiki_pages / "alpha.md"
    documents = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", _page("alpha", "Alpha", details="[Beta](beta.md).")),
        ("wiki/pages/beta.md", _page("beta", "Beta")),
    )
    (repo_paths.wiki_pages / "beta.md").write_text("not a wiki record", encoding="utf-8")

    staged = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )
    omitted = validate_graph(
        repo_paths,
        documents={alpha: documents[alpha]},
        index_text="# Second Brain Lite\n",
    )

    assert staged.ok, staged.issues
    assert "graph_link_target_missing" in {issue.code for issue in omitted.issues}


def test_validate_graph_rejects_unsafe_document_mappings(repo_paths) -> None:
    report = validate_graph(
        repo_paths,
        documents={Path("relative.md"): _page("alpha", "Alpha")},
        index_text="# Second Brain Lite\n",
    )

    assert {issue.code for issue in report.issues} == {"wiki_documents_invalid"}


def test_validate_graph_reports_an_existing_symlink_mapping_as_unsafe(repo_paths) -> None:
    outside = repo_paths.root / "outside.md"
    outside.write_text(_page("outside", "Outside"), encoding="utf-8")
    unsafe = repo_paths.wiki_pages / "alpha.md"
    unsafe.symlink_to(outside)

    report = validate_graph(
        repo_paths,
        documents={unsafe: _page("alpha", "Alpha")},
        index_text="# Second Brain Lite\n",
    )

    assert {issue.code for issue in report.issues} == {"wiki_documents_invalid"}


def test_validate_graph_fails_closed_for_scanner_and_model_failures(repo_paths) -> None:
    scanner_bad = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", _page("alpha", "Alpha", summary="unsafe\0text")),
    )
    model_bad = _documents(
        repo_paths,
        ("wiki/pages/beta.md", "---\nid: beta\n---\n# Beta\n"),
    )

    scanner_report = validate_graph(
        repo_paths, documents=scanner_bad, index_text="# Second Brain Lite\n"
    )
    model_report = validate_graph(
        repo_paths, documents=model_bad, index_text="# Second Brain Lite\n"
    )

    scanner_issue = next(
        issue for issue in scanner_report.issues if issue.code == "graph_markdown_ambiguous"
    )
    assert scanner_issue.details["markdown_code"] == "markdown_reconciliation_ambiguous"
    assert "wiki_record_invalid" in {issue.code for issue in model_report.issues}


def test_validate_graph_scans_each_body_once_and_reuses_it_for_model_parse(
    repo_paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", _page("alpha", "Alpha", details="[Beta](beta.md).")),
        ("wiki/pages/beta.md", _page("beta", "Beta")),
    )
    index_text = _generated_index(repo_paths, documents)
    observed: list[str] = []
    real_scan = graph.scan_markdown

    def counting_scan(markdown: str):
        observed.append(markdown)
        return real_scan(markdown)

    def unexpected_model_scan(_markdown: str):
        raise AssertionError("graph validation must reuse its precomputed scan")

    monkeypatch.setattr(graph, "scan_markdown", counting_scan)
    monkeypatch.setattr(wiki_models, "scan_markdown", unexpected_model_scan)

    report = graph.validate_graph(repo_paths, documents=documents, index_text=index_text)

    assert report.ok, report.issues
    assert len(observed) == 2
    assert all(markdown.startswith("---\n") for markdown in observed)


def test_first_occurrence_ignores_every_heading_line(repo_paths) -> None:
    related = "- [Beta](beta.md): A reciprocal related topic."
    reverse = "- [Alpha](alpha.md): A reciprocal related topic."
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                details="### Beta\n[Beta](beta.md).",
                related_pages=related,
            ),
        ),
        ("wiki/pages/beta.md", _page("beta", "Beta", related_pages=reverse)),
    )

    report = graph.validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert report.ok, report.issues


def test_validate_graph_reports_id_slug_and_display_name_collisions(repo_paths) -> None:
    documents = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", _page("same", "Alpha", aliases=("Shared",))),
        ("wiki/pages/alpha-copy.md", _page("same", "Beta", aliases=("Shared",))),
        ("wiki/questions/alpha.md", _question("question-alpha", "Alpha question")),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text="# Second Brain Lite\n",
    )

    assert {
        "wiki_record_id_duplicate",
        "wiki_record_slug_duplicate",
        "display_name_ambiguous",
    } <= {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    ("destination", "code"),
    [
        ("https://example.invalid/topic.md", "graph_link_destination_invalid"),
        ("béta%20topic.md", "graph_link_destination_invalid"),
        ('b%C3%A9ta%20topic.md "optional title"', "graph_link_destination_invalid"),
        ("b%C3%A9ta%20topic.md#", "graph_link_fragment_invalid"),
        ("b%C3%A9ta%20topic.md#Details#Again", "graph_link_fragment_invalid"),
        ("b%C3%A9ta%20topic.md#details", "graph_link_fragment_invalid"),
    ],
)
def test_validate_graph_enforces_strict_path_and_fragment_grammar(
    repo_paths, destination: str, code: str
) -> None:
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page("alpha", "Alpha", details=f"[Béta topic]({destination})."),
        ),
        ("wiki/pages/béta topic.md", _page("beta", "Béta topic")),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert code in {issue.code for issue in report.issues}


def test_validate_graph_rejects_a_relationship_of_the_wrong_kind(repo_paths) -> None:
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                related_questions="- [Beta](beta.md): This wrongly names a page.",
            ),
        ),
        ("wiki/pages/beta.md", _page("beta", "Beta")),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert "relationship_target_kind_invalid" in {issue.code for issue in report.issues}


def test_graph_excludes_only_links_on_scanner_reported_citation_definition_lines(repo_paths) -> None:
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                sources=(
                    "[^source]: source_id: `src`; [original](https://example.invalid/a)"
                ),
            ),
        ),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert report.ok, report.issues


def test_first_occurrence_uses_visible_chunks_without_joining_or_code_or_images(repo_paths) -> None:
    related = "- [Beta](beta.md): A reciprocal related topic."
    reverse = "- [Alpha](alpha.md): A reciprocal related topic."
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                details="Al[pha](beta.md) and `Beta` plus ![Beta](cover.png).",
                related_pages=related,
            ),
        ),
        ("wiki/pages/beta.md", _page("beta", "Beta", related_pages=reverse)),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert report.ok, report.issues


def test_first_occurrence_uses_raw_offset_before_later_correct_link(repo_paths) -> None:
    related = "- [Beta](beta.md): A reciprocal related topic."
    reverse = "- [Alpha](alpha.md): A reciprocal related topic."
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                details="Beta appears before [Beta](beta.md).",
                related_pages=related,
            ),
        ),
        ("wiki/pages/beta.md", _page("beta", "Beta", related_pages=reverse)),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert "related_topic_first_occurrence_unlinked" in {
        issue.code for issue in report.issues
    }


def test_first_occurrence_ignores_citation_definition_lines(repo_paths) -> None:
    related = "- [Beta](beta.md): A reciprocal related topic."
    reverse = "- [Alpha](alpha.md): A reciprocal related topic."
    documents = _documents(
        repo_paths,
        (
            "wiki/pages/alpha.md",
            _page(
                "alpha",
                "Alpha",
                details="[^cite]: Beta is citation-definition syntax, not prose.",
                related_pages=related,
            ),
        ),
        ("wiki/pages/beta.md", _page("beta", "Beta", related_pages=reverse)),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text=_generated_index(repo_paths, documents),
    )

    assert report.ok, report.issues


def test_render_wiki_index_has_empty_groups_exactly_and_escapes_labels(repo_paths) -> None:
    empty = render_wiki_index(repo_paths, {})
    bracket_page = _page("alpha", "A bracket slash").replace(
        "title: A bracket slash", 'title: "A [bracket] slash"'
    ).replace("# A bracket slash", "# A [bracket] slash")
    documents = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", bracket_page),
        ("wiki/questions/what.md", _question("question-what", "What?")),
    )

    rendered = render_wiki_index(repo_paths, documents)

    assert empty == "# Second Brain Lite\n\n## Pages\n\n## Questions\n"
    assert rendered == (
        "# Second Brain Lite\n\n"
        "## Pages\n\n"
        "- [A \\[bracket\\] slash](pages/alpha.md)\n\n"
        "## Questions\n\n"
        "- [What?](questions/what.md)\n"
    )


def test_validate_graph_reports_a_stale_generated_index(repo_paths) -> None:
    documents = _documents(
        repo_paths,
        ("wiki/pages/alpha.md", _page("alpha", "Alpha")),
    )

    report = validate_graph(
        repo_paths,
        documents=documents,
        index_text="# Second Brain Lite\n",
    )

    assert "wiki_index_stale" in {issue.code for issue in report.issues}
