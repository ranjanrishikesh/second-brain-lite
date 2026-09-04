from __future__ import annotations

import os
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from brainlib import citations as citation_module
from brainlib.contracts import Anchor, compute_corpus_revision, compute_sha256
from brainlib.diagnostics import ValidationIssue
from brainlib.inventory import SnapshotNamespace
from brainlib.ledger import CitationRewrite
from brainlib.markdown import MarkdownDiagnostic, MarkdownScan
from brainlib.validation import ChecksumCache
from tests.helpers_knowledge import adopt_changed_scenario_source


@pytest.fixture
def citations():
    return citation_module


@pytest.fixture
def current(scenario_repo):
    scenario = scenario_repo("citations/current")
    page = scenario.paths.wiki_pages / "current.md"
    return scenario, page, page.read_text(encoding="utf-8")


def codes(report):
    return {issue.code for issue in report.issues}


@pytest.fixture
def ambiguous_scan(monkeypatch):
    diagnostics = (
        MarkdownDiagnostic("markdown_reconciliation_ambiguous", "Bad inline map.", 7),
        MarkdownDiagnostic("markdown_parser_failure", "Parser could not finish.", 3),
    )
    calls = []

    def install(scan=None):
        result = replace(scan or MarkdownScan((), (), ()), diagnostics=diagnostics)

        def scan_document(markdown):
            calls.append(markdown)
            return result

        monkeypatch.setattr(citation_module, "scan_markdown", scan_document)
        return calls

    return install


def _ambiguity_issues(path):
    return (
        ValidationIssue(
            "error",
            "citation_markdown_ambiguous",
            "Bad inline map.",
            PurePosixPath(path),
            {"line": 7, "markdown_code": "markdown_reconciliation_ambiguous"},
        ),
        ValidationIssue(
            "error",
            "citation_markdown_ambiguous",
            "Parser could not finish.",
            PurePosixPath(path),
            {"line": 3, "markdown_code": "markdown_parser_failure"},
        ),
    )


def test_validation_reports_scanner_diagnostics_in_exact_order(current, ambiguous_scan):
    scenario, page, markdown = current
    calls = ambiguous_scan()
    report = citation_module.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}
    )
    assert not report.ok
    assert report.issues == _ambiguity_issues("wiki/pages/current.md")
    assert calls == [markdown]


def test_validation_keeps_ordinary_issues_after_scanner_diagnostics(
    current, ambiguous_scan
):
    scenario, page, _ = current
    markdown = "Claim.[^missing]\n\n## Sources\n\n[^broken]: source_id: `bad`\n"
    calls = ambiguous_scan(citation_module.scan_markdown(markdown))
    report = citation_module.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}
    )
    assert report.issues == _ambiguity_issues("wiki/pages/current.md") + (
        ValidationIssue(
            "error",
            "citation_definition_invalid",
            "Citation requires each identity field and evidence link exactly once.",
            PurePosixPath("wiki/pages/current.md"),
            {"line": 5, "citation_id": "broken"},
        ),
        ValidationIssue(
            "error",
            "citation_definition_missing",
            "Citation marker has no definition in this document.",
            PurePosixPath("wiki/pages/current.md"),
            {"line": 1, "citation_id": "missing"},
        ),
        ValidationIssue(
            "error",
            "citation_definition_unused",
            "A source definition needs an adjacent claim marker reference.",
            PurePosixPath("wiki/pages/current.md"),
            {"line": 5, "citation_id": "broken"},
        ),
    )
    assert calls == [markdown]


@pytest.mark.parametrize("with_invalid_definition", [False, True])
def test_direct_parser_refuses_scanner_diagnostics_and_retains_parse_issues(
    current, ambiguous_scan, with_invalid_definition
):
    _, page, markdown = current
    expected = _ambiguity_issues(page)
    if with_invalid_definition:
        markdown = "[^broken]: source_id: `bad`\n"
        expected += (
            ValidationIssue(
                "error",
                "citation_definition_invalid",
                "Citation requires each identity field and evidence link exactly once.",
                PurePosixPath(page),
                {"line": 1, "citation_id": "broken"},
            ),
        )
    calls = ambiguous_scan(citation_module.scan_markdown(markdown))
    before = page.read_bytes()
    with pytest.raises(citation_module.CitationParseError) as error:
        citation_module.parse_citation_definitions(markdown, path=page)
    assert error.value.issues == expected
    assert calls == [markdown]
    assert page.read_bytes() == before


@pytest.mark.parametrize("operation", ["canonicalize", "historical"])
def test_destination_rewrites_refuse_scanner_diagnostics_before_parsing(
    current, ambiguous_scan, operation
):
    scenario, page, markdown = current
    (parsed,) = citation_module.parse_citation_definitions(markdown, path=page)
    markdown = markdown.replace(parsed.original_destination, "wrong.txt")
    markdown += "\n[^broken]: source_id: `bad`\n"
    calls = ambiguous_scan(citation_module.scan_markdown(markdown))
    before = page.read_bytes()
    with pytest.raises(citation_module.CitationParseError) as error:
        if operation == "canonicalize":
            citation_module.canonicalize_citation_destinations(
                markdown, scenario.ledger, paths=scenario.paths, document_path=page
            )
        else:
            rewrite = CitationRewrite(
                parsed.source_id, parsed.content_sha256, PurePosixPath("notes/moved.txt")
            )
            citation_module.rewrite_historical_original_links(
                markdown, (rewrite,), paths=scenario.paths, document_path=page
            )
    assert error.value.issues == _ambiguity_issues(page)
    assert calls == [markdown]
    assert page.read_bytes() == before


@pytest.mark.parametrize("full", [False, True])
def test_validation_scans_each_document_once(current, monkeypatch, full):
    scenario, page, markdown = current
    scan = citation_module.scan_markdown
    calls = []

    def scan_document(text):
        calls.append(text)
        return scan(text)

    monkeypatch.setattr(citation_module, "scan_markdown", scan_document)
    another = markdown.replace("Alpha is supported.", "Another supported claim.")
    report = citation_module.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown, page.with_name("later.md"): another},
        full=full,
    )
    assert report.ok, report.issues
    assert calls == [markdown, another]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_definition_lookup_uses_physical_lines(current, separator, newline):
    scenario, page, markdown = current
    markdown = markdown.replace("## Sources\n\n", "## Sources\n\nProse" + separator + "\n")
    markdown = markdown.replace("\n", newline)
    (parsed,) = citation_module.parse_citation_definitions(markdown, path=page)
    assert parsed.definition_line == 25
    assert parsed.original_destination == "../../sources/raw/notes/a.txt"
    assert citation_module.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}, full=True
    ).ok


@pytest.mark.parametrize("operation", ["canonicalize", "historical"])
@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_destination_rewrites_preserve_physical_lines_and_source_bytes(
    current, operation, separator, newline
):
    scenario, page, markdown = current
    (parsed,) = citation_module.parse_citation_definitions(markdown, path=page)
    decoy = "Prose [original](wrong.txt)" + separator
    markdown = markdown.replace("## Sources\n\n", "## Sources\n\n" + decoy + "\n")
    markdown = markdown.replace("\n", newline).rstrip("\r\n")
    if operation == "canonicalize":
        broken = markdown.replace(parsed.original_destination, "wrong.txt").replace(
            parsed.extracted_destination, "wrong.md#page:99"
        )
        rewritten = citation_module.canonicalize_citation_destinations(
            broken, scenario.ledger, paths=scenario.paths, document_path=page
        )
        expected = markdown
    else:
        rewrite = CitationRewrite(
            parsed.source_id, parsed.content_sha256, PurePosixPath("notes/moved.txt")
        )
        rewritten = citation_module.rewrite_historical_original_links(
            markdown, (rewrite,), paths=scenario.paths, document_path=page
        )
        expected = markdown.replace(
            "[original](../../sources/raw/notes/a.txt)",
            "[original](../../sources/raw/notes/moved.txt)",
        )
    assert rewritten.encode("utf-8") == expected.encode("utf-8")


@pytest.mark.parametrize(
    "scenario_name,page_name",
    [("current", "current"), ("historical", "historical"), ("encoded-path", "encoded")],
)
def test_exact_retained_evidence_validates(
    citations, scenario_repo, scenario_name, page_name
):
    scenario = scenario_repo("citations/" + scenario_name)
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        (scenario.paths.wiki_pages / (page_name + ".md"),),
        full=True,
    )
    assert report.ok, report.issues
    assert report.checks == ("citations",)
    assert report.corpus_revision == compute_corpus_revision(
        scenario.ledger.load_all().values()
    )


def test_marker_without_definition_fails(citations, scenario_repo):
    scenario = scenario_repo("citations/missing-definition")
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, (scenario.paths.wiki_pages / "broken.md",)
    )
    assert codes(report) == {"citation_definition_missing"}


def test_code_example_is_not_a_citation(citations, scenario_repo):
    scenario = scenario_repo("citations/code-example")
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        (scenario.paths.wiki_pages / "code-example.md",),
    ).ok


def test_parser_uses_canonical_anchor_and_definition_line(citations, current):
    _, page, markdown = current
    (citation,) = citations.parse_citation_definitions(markdown, path=page)
    assert citation.citation_id == "cite-alpha-page-2"
    assert citation.anchor == Anchor("page", "2")
    assert citation.definition_line == 24


@pytest.mark.parametrize(
    "wrapper",
    [
        "```text\n{definition}\n```",
        "~~~\n{definition}\n~~~",
        "    {definition}",
        "\t{definition}",
        "---\n{definition}\n---",
    ],
)
def test_parser_ignores_definitions_in_nonsemantic_markdown(
    citations, current, wrapper
):
    _, page, markdown = current
    definition = markdown.splitlines()[-1]
    assert (
        citations.parse_citation_definitions(
            wrapper.format(definition=definition), path=page
        )
        == ()
    )


@pytest.mark.parametrize(
    "text",
    [
        "`[^example]`",
        "``[^example]``",
        "\\[^example]",
        "    [^example]",
        "```text\n```not-closing\n[^example]\n```",
    ],
)
def test_markers_in_code_or_escaped_text_are_ignored(citations, current, text):
    scenario, page, _ = current
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: text}
    ).ok


def test_mapping_values_are_authoritative_even_for_unpublished_documents(
    citations, current
):
    scenario, page, markdown = current
    page.write_text("Bad claim.[^missing]\n", encoding="utf-8")
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}
    ).ok
    staged = page.with_name("not-yet-published.md")
    assert not staged.exists()
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {staged: markdown}
    ).ok
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: "Bad claim.[^missing]"}
    )
    assert codes(report) == {"citation_definition_missing"}


def test_definitions_do_not_resolve_across_documents(citations, current):
    scenario, page, markdown = current
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {
            page: markdown,
            page.with_name("other.md"): "Another claim.[^cite-alpha-page-2]",
        },
    )
    assert codes(report) == {"citation_definition_missing"}
    assert report.issues[0].path == PurePosixPath("wiki/pages/other.md")


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda text: text + text.splitlines()[-1] + "\n",
            "citation_definition_duplicate",
        ),
        (
            lambda text: text.replace("## Sources", "## Evidence"),
            "citation_definition_out_of_section",
        ),
        (lambda text: text + "\n## Later\n", "citation_definition_out_of_section"),
        (lambda text: text + "\n### Later\n", "citation_definition_out_of_section"),
        (
            lambda text: text.replace("## Sources", "### Sources"),
            "citation_definition_out_of_section",
        ),
        (
            lambda text: text.replace(
                "Alpha is supported.[^cite-alpha-page-2]", "Alpha is supported."
            ),
            "citation_definition_unused",
        ),
        (
            lambda text: text.replace(
                "source_id:", "source_id: `src_" + "a" * 64 + "`; source_id:", 1
            ),
            "citation_definition_invalid",
        ),
        (
            lambda text: text.replace("; anchor: `page:2`", ""),
            "citation_definition_invalid",
        ),
        (
            lambda text: text.replace("source_id: `src_", "source_id: `src_X", 1),
            "citation_source_id_invalid",
        ),
        (
            lambda text: text.replace("content_sha256: `", "content_sha256: `X", 1),
            "citation_content_sha256_invalid",
        ),
        (
            lambda text: text.replace(
                "derivation_id: `drv_", "derivation_id: `drv_X", 1
            ),
            "citation_derivation_id_invalid",
        ),
        (
            lambda text: text.replace("anchor: `page:2`", "anchor: `unknown:2`"),
            "citation_anchor_invalid",
        ),
        (
            lambda text: text.replace("anchor: `page:2`", "anchor: `page:`"),
            "citation_anchor_invalid",
        ),
        (
            lambda text: text.replace("anchor: `page:2`", "anchor: `page:7`"),
            "citation_anchor_missing",
        ),
        (
            lambda text: text.replace("#page:2)", "#page:7)"),
            "citation_extracted_fragment_mismatch",
        ),
        (
            lambda text: text.replace("#page:2)", ")"),
            "citation_extracted_fragment_mismatch",
        ),
        (
            lambda text: text.replace(
                "[original](../../sources/raw/notes/a.txt)",
                "[original](../../sources/raw/notes/a.txt#page:2)",
            ),
            "citation_destination_noncanonical",
        ),
    ],
)
def test_invalid_definition_has_precise_diagnostic(
    citations, current, mutation, expected
):
    scenario, page, markdown = current
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: mutation(markdown)}
    )
    assert expected in codes(report), report.issues
    assert not report.ok
    assert all(
        issue.severity == "error"
        and issue.path == PurePosixPath("wiki/pages/current.md")
        for issue in report.issues
    )


@pytest.mark.parametrize(
    "field,prefix,expected",
    [
        ("source_id", "src_", "citation_source_missing"),
        ("content_sha256", "", "citation_version_missing"),
        ("derivation_id", "drv_", "citation_derivation_missing"),
    ],
)
def test_missing_identity_is_not_replaced_by_active_identity(
    citations, current, field, prefix, expected
):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    before = f"{field}: `{getattr(parsed, field)}`"
    after = f"{field}: `{prefix}{'f' * 64}`"
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown.replace(before, after)}
    )
    assert codes(report) == {expected}


@pytest.mark.parametrize(
    "destination",
    [
        "../../sources/raw/%2G.txt",
        "../../sources/raw/%2fetc.txt",
        "../../sources/raw/%2Fetc.txt",
        "../../sources/raw/%5Cetc.txt",
        "../../sources/raw/%00.txt",
        "../../sources/raw/%41.txt",
        "../../sources/raw/%2E/alias.txt",
        "../../sources/raw/./alias.txt",
        "../../sources/raw/folder/../alias.txt",
        "https://example.test/evidence.txt",
        "//example.test/evidence.txt",
        "/etc/passwd",
        "../../../../outside.txt",
        "../../sources/raw/a.txt?query",
        "../../sources/raw/a.txt#fragment",
        "../../sources/raw/a\\b.txt",
        "../../sources/raw/%FF.txt",
        "../../sources/raw/file with space.txt",
        "../../sources/raw/a%0a.txt",
        "../../sources/raw//a.txt",
        "../../sources/raw/a.txt/",
        "../../sources/raw/notes/../../../sources/raw/notes/a.txt",
    ],
)
def test_path_resolver_rejects_ambiguous_or_escaping_destination(
    citations, repo_paths, destination
):
    with pytest.raises(citations.CitationPathError):
        citations.resolve_markdown_path(
            repo_paths, repo_paths.wiki_pages / "a.md", destination
        )


@pytest.mark.parametrize(
    "name,encoded",
    [
        ("Résumé (100% #1).txt", "R%C3%A9sum%C3%A9%20%28100%25%20%231%29.txt"),
        ("a\n\t?.txt", "a%0A%09%3F.txt"),
        ("a%2520.txt", "a%252520.txt"),
        ("a;b:`x`.txt", "a%3Bb%3A%60x%60.txt"),
    ],
)
def test_utf8_destinations_round_trip_exactly_once(
    citations, repo_paths, name, encoded
):
    page = repo_paths.wiki_pages / "a.md"
    target = repo_paths.raw / name
    assert (
        citations.encode_markdown_path(page, target) == "../../sources/raw/" + encoded
    )
    assert (
        citations.resolve_markdown_path(
            repo_paths, page, "../../sources/raw/" + encoded
        )
        == target
    )


@pytest.mark.parametrize("link", ["original", "extracted"])
def test_wrong_existing_same_byte_target_is_rejected(citations, current, link):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    old_destination = getattr(parsed, link + "_destination")
    path_part, _, fragment = old_destination.partition("#")
    old_target = citations.resolve_markdown_path(scenario.paths, page, path_part)
    duplicate = old_target.with_name("duplicate" + old_target.suffix)
    duplicate.write_bytes(old_target.read_bytes())
    destination = citations.encode_markdown_path(page, duplicate) + (
        "#" + fragment if fragment else ""
    )
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown.replace(old_destination, destination)},
        full=True,
    )
    assert "citation_" + link + "_target_mismatch" in codes(report)


@pytest.mark.parametrize("link", ["original", "extracted"])
def test_missing_file_has_distinct_issue(citations, current, link):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    target = citations.resolve_markdown_path(
        scenario.paths, page, getattr(parsed, link + "_destination").split("#")[0]
    )
    target.unlink()
    assert "citation_" + link + "_missing" in codes(
        citations.validate_citations(scenario.paths, scenario.ledger, {page: markdown})
    )


@pytest.mark.parametrize("link", ["original", "extracted"])
def test_full_mode_rejects_same_metadata_changed_bytes(citations, current, link):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    target = citations.resolve_markdown_path(
        scenario.paths, page, getattr(parsed, link + "_destination").split("#")[0]
    )
    metadata = target.stat()
    target.write_bytes(b"x" * metadata.st_size)
    os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}
    ).ok
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}, full=True
    )
    assert "citation_" + link + "_checksum_mismatch" in codes(report)


def test_normal_mode_never_hashes_full_mode_reuses_namespace_cache(citations, current):
    scenario, page, markdown = current
    calls = []
    cache = ChecksumCache(
        hash_file=lambda path: calls.append(path.resolve()) or compute_sha256(path)
    )
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}, checksum_cache=cache
    ).ok
    assert calls == []
    cache.begin_transaction()
    documents = {page: markdown, page.with_name("again.md"): markdown}
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, documents, full=True, checksum_cache=cache
    ).ok
    assert len(calls) == 2


def test_supplied_cache_is_not_reset_and_replacement_poison_survives(
    citations, current
):
    scenario, page, markdown = current
    cache = ChecksumCache()
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=True,
        checksum_cache=cache,
    ).ok
    target = scenario.paths.raw / "notes/a.txt"
    metadata = target.stat()
    replacement = target.with_name("replacement.txt")
    replacement.write_bytes(target.read_bytes())
    os.utime(replacement, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    replacement.replace(target)
    for _ in range(2):
        report = citations.validate_citations(
            scenario.paths,
            scenario.ledger,
            {page: markdown},
            full=True,
            checksum_cache=cache,
        )
        assert "citation_original_missing" in codes(report)


def test_real_adoption_rewrite_changes_only_the_exact_version(citations, scenario_repo):
    scenario = scenario_repo("citations/adoption")
    page = scenario.paths.wiki_pages / "adoption.md"
    before = page.read_text(encoding="utf-8")
    other = before.splitlines()[-1]
    prior = (
        scenario.root
        / "tests/fixtures/wiki/scenarios/citations/adoption/prior-original.txt"
    ).read_bytes()
    adoption = adopt_changed_scenario_source(
        scenario, prior, approval_note="User approved replacement in this conversation"
    )
    assert "citation_original_stale" in codes(
        citations.validate_citations(scenario.paths, scenario.ledger, {page: before})
    )
    rewritten = citations.rewrite_historical_original_links(
        before, adoption.citation_rewrites, paths=scenario.paths, document_path=page
    )
    assert other in rewritten
    assert "A prose mention of sources/raw/notes/a.txt stays unchanged." in rewritten
    assert "```text\n[original](../../sources/raw/notes/a.txt)\n```" in rewritten
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: rewritten}, full=True
    ).ok
    assert (
        citations.rewrite_historical_original_links(
            rewritten,
            adoption.citation_rewrites,
            paths=scenario.paths,
            document_path=page,
        )
        == rewritten
    )
    canonical = citations.canonicalize_citation_destinations(
        before, scenario.ledger, paths=scenario.paths, document_path=page
    )
    assert canonical == rewritten
    assert (
        citations.canonicalize_citation_destinations(
            canonical, scenario.ledger, paths=scenario.paths, document_path=page
        )
        == canonical
    )


def test_canonicalizer_repairs_both_destinations_and_preserves_unresolved_identity(
    citations, current
):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    broken = markdown.replace(parsed.original_destination, "wrong.txt").replace(
        parsed.extracted_destination, "wrong.md#page:99"
    )
    assert (
        citations.canonicalize_citation_destinations(
            broken, scenario.ledger, paths=scenario.paths, document_path=page
        )
        == markdown
    )
    unknown = broken.replace(parsed.source_id, "src_" + "f" * 64)
    assert (
        citations.canonicalize_citation_destinations(
            unknown, scenario.ledger, paths=scenario.paths, document_path=page
        )
        == unknown
    )


@pytest.mark.parametrize("shape", ["list", "unsorted", "duplicate", "conflict"])
def test_rewrite_input_requires_sorted_unique_tuple(
    citations, repo_paths, shape, ambiguous_scan
):
    a = CitationRewrite("src_" + "a" * 64, "c" * 64, PurePosixPath("notes/a.txt"))
    b = CitationRewrite("src_" + "b" * 64, "c" * 64, PurePosixPath("notes/b.txt"))
    rewrites = {
        "list": [a],
        "unsorted": (b, a),
        "duplicate": (a, a),
        "conflict": (a, replace(a, raw_path=PurePosixPath("notes/b.txt"))),
    }[shape]
    calls = ambiguous_scan()
    with pytest.raises(ValueError, match="citation rewrites must") as error:
        citations.rewrite_historical_original_links(
            "No citations",
            rewrites,
            paths=repo_paths,
            document_path=repo_paths.wiki_pages / "a.md",
        )
    assert not isinstance(error.value, citations.CitationParseError)
    assert calls == []


def test_path_escape_is_distinct_from_noncanonical_destination(
    citations, current, tmp_path
):
    scenario, page, markdown = current
    before = "../../sources/raw/notes/a.txt"
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown.replace(before, "../../../../outside.txt")},
    )
    assert "citation_target_escape" in codes(report)
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown.replace(before, "../../sources/raw/%2G.txt")},
    )
    assert "citation_destination_noncanonical" in codes(report)


def test_original_namespace_is_not_bypassed_with_symlink_to_extracted(
    citations, current
):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    raw = scenario.paths.raw / "notes/a.txt"
    extracted = citations.resolve_markdown_path(
        scenario.paths, page, parsed.extracted_destination.split("#")[0]
    )
    raw.unlink()
    raw.symlink_to(extracted)
    assert not citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}, full=True
    ).ok


def test_escaped_backticks_do_not_hide_claim_markers(citations, current):
    scenario, page, _ = current
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: r"\`A real claim.[^missing]\`"}
    )
    assert codes(report) == {"citation_definition_missing"}


def test_rewrite_never_changes_link_shaped_identity_text(citations, current):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    markdown = markdown.replace(
        "anchor: `page:2`", "anchor: `section:[original](inside-identity)`"
    )
    rewrite = CitationRewrite(
        parsed.source_id, parsed.content_sha256, PurePosixPath("notes/moved.txt")
    )
    rewritten = citations.rewrite_historical_original_links(
        markdown, (rewrite,), paths=scenario.paths, document_path=page
    )
    assert "anchor: `section:[original](inside-identity)`" in rewritten
    assert "[original](../../sources/raw/notes/moved.txt)" in rewritten


def test_cache_is_consulted_for_every_reference_under_exact_namespace(
    citations, scenario_repo
):
    scenario = scenario_repo("citations/historical")
    page = scenario.paths.wiki_pages / "historical.md"
    markdown = page.read_text(encoding="utf-8")
    calls = []

    class ObservedCache(ChecksumCache):
        def observe(
            self, paths, namespace, logical_path, *, full, expected_fingerprint=None
        ):
            calls.append((namespace, logical_path))
            return super().observe(
                paths,
                namespace,
                logical_path,
                full=full,
                expected_fingerprint=expected_fingerprint,
            )

    cache = ObservedCache()
    cache.begin_transaction()
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown, page.with_name("another.md"): markdown},
        full=True,
        checksum_cache=cache,
    )
    assert report.ok
    assert len(calls) == 4
    assert [entry[0] for entry in calls].count(SnapshotNamespace.RAW_VERSION) == 2
    assert [entry[0] for entry in calls].count(SnapshotNamespace.EXTRACTED) == 2
    assert all(
        path.parts[0] == "_versions"
        for namespace, path in calls
        if namespace is SnapshotNamespace.RAW_VERSION
    )


def test_namespace_owner_mismatch_fails_even_with_matching_bytes(citations, current):
    scenario, page, markdown = current
    (record,) = scenario.ledger.load_all().values()
    old = record.versions[record.active_content_sha256]
    wrong = PurePosixPath("_versions", "src_" + "f" * 64, old.sha256, old.raw_path.name)
    target = scenario.paths.raw / wrong
    target.parent.mkdir(parents=True)
    target.write_bytes((scenario.paths.raw / old.raw_path).read_bytes())
    os.utime(target, ns=(1, 1))
    changed = replace(
        record,
        versions={
            old.sha256: replace(
                old, raw_path=wrong, fingerprint=replace(old.fingerprint, path=wrong)
            )
        },
    )
    scenario.ledger.save(changed)
    rewritten = markdown.replace(
        "../../sources/raw/notes/a.txt", "../../sources/raw/" + wrong.as_posix()
    )
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: rewritten}, full=True
    )
    assert not report.ok
    assert "citation_original_target_mismatch" in codes(report)


def test_encoded_fixture_malformed_escape_rejected(citations, scenario_repo):
    scenario = scenario_repo("citations/encoded-path")
    page = scenario.paths.wiki_pages / "encoded.md"
    markdown = page.read_text(encoding="utf-8")
    assert "R%C3%A9sum%C3%A9%20%28100%25%20%231%29.txt" in markdown
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown.replace("%28", "%2G", 1)}
    )
    assert "citation_destination_noncanonical" in codes(report)


@pytest.mark.parametrize("role", ["original", "extracted"])
def test_normal_mode_rejects_metadata_drift(citations, current, role):
    scenario, page, markdown = current
    (parsed,) = citations.parse_citation_definitions(markdown, path=page)
    path = citations.resolve_markdown_path(
        scenario.paths, page, getattr(parsed, role + "_destination").split("#")[0]
    )
    with path.open("ab") as stream:
        stream.write(b"changed\n")
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}
    )
    assert "citation_" + role + "_fingerprint_mismatch" in codes(report)


def test_unapproved_web_namespace_cannot_replace_user_original(citations, current):
    scenario, page, markdown = current
    (record,) = scenario.ledger.load_all().values()
    old = record.versions[record.active_content_sha256]
    wrong = PurePosixPath("_web", record.source_id, old.sha256, old.raw_path.name)
    target = scenario.paths.raw / wrong
    target.parent.mkdir(parents=True)
    target.write_bytes((scenario.paths.raw / old.raw_path).read_bytes())
    os.utime(target, ns=(1, 1))
    changed = replace(
        record,
        versions={
            old.sha256: replace(
                old, raw_path=wrong, fingerprint=replace(old.fingerprint, path=wrong)
            )
        },
    )
    scenario.ledger.save(changed)
    rewritten = markdown.replace(
        "../../sources/raw/notes/a.txt", "../../sources/raw/" + wrong.as_posix()
    )
    assert "citation_original_target_mismatch" in codes(
        citations.validate_citations(
            scenario.paths, scenario.ledger, {page: rewritten}, full=True
        )
    )


def test_extracted_namespace_must_bind_its_declared_derivation(citations, current):
    scenario, page, markdown = current
    (record,) = scenario.ledger.load_all().values()
    old = record.derivations[record.active_derivation_id]
    wrong = old.output_path.with_name("drv_" + "f" * 64 + ".md")
    target = scenario.paths.root / wrong
    target.write_bytes((scenario.paths.root / old.output_path).read_bytes())
    os.utime(target, ns=(1, 1))
    scenario.ledger.save(
        replace(
            record, derivations={old.derivation_id: replace(old, output_path=wrong)}
        )
    )
    rewritten = markdown.replace(
        "../../" + old.output_path.as_posix(), "../../" + wrong.as_posix()
    )
    assert "citation_extracted_target_mismatch" in codes(
        citations.validate_citations(
            scenario.paths, scenario.ledger, {page: rewritten}, full=True
        )
    )


def test_web_snapshot_uses_raw_web_namespace(
    citations, repo_paths, captured_ok_descriptor_record
):
    from brainlib.ledger import LedgerStore

    ledger = LedgerStore(repo_paths)
    record = captured_ok_descriptor_record
    representation = ledger.find_representation(
        record.source_id, record.active_content_sha256, record.active_derivation_id
    )
    anchor = representation.anchors[0]
    page = repo_paths.wiki_pages / "web.md"
    markdown = f"A captured fact.[^web]\n\n## Sources\n\n[^web]: source_id: `{record.source_id}`; content_sha256: `{record.active_content_sha256}`; derivation_id: `{record.active_derivation_id}`; anchor: `{anchor.kind}:{anchor.value}`; [original](../../sources/raw/{representation.raw_path}); [extracted](../../{representation.extracted_path}#{anchor.kind}:{anchor.value})\n"
    namespaces = []

    class WebCache(ChecksumCache):
        def observe(
            self, paths, namespace, logical_path, *, full, expected_fingerprint=None
        ):
            namespaces.append(namespace)
            return super().observe(
                paths,
                namespace,
                logical_path,
                full=full,
                expected_fingerprint=expected_fingerprint,
            )

    cache = WebCache()
    cache.begin_transaction()
    report = citations.validate_citations(
        repo_paths, ledger, {page: markdown}, full=True, checksum_cache=cache
    )
    assert report.ok, report.issues
    assert namespaces == [SnapshotNamespace.RAW_WEB, SnapshotNamespace.EXTRACTED]


def test_repeated_claim_references_each_recheck_file_identity(citations, current):
    scenario, page, markdown = current
    markdown = markdown.replace(
        "## Sources", "Another fact.[^cite-alpha-page-2]\n\n## Sources"
    )
    observations = []

    class ReferenceCache(ChecksumCache):
        def observe(
            self, paths, namespace, logical_path, *, full, expected_fingerprint=None
        ):
            observations.append(namespace)
            return super().observe(
                paths,
                namespace,
                logical_path,
                full=full,
                expected_fingerprint=expected_fingerprint,
            )

    cache = ReferenceCache()
    cache.begin_transaction()
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=True,
        checksum_cache=cache,
    )
    assert report.ok
    assert observations == [SnapshotNamespace.RAW_USER, SnapshotNamespace.EXTRACTED] * 2


@pytest.mark.parametrize("boundary", ["\n\n", "\n## Claim\n"])
def test_inline_code_does_not_cross_markdown_block_boundaries(
    citations, current, boundary
):
    scenario, page, _ = current
    markdown = "`unmatched" + boundary + "A factual claim.[^missing] `another tick`"
    assert "citation_definition_missing" in codes(
        citations.validate_citations(scenario.paths, scenario.ledger, {page: markdown})
    )


def test_unsafe_namespace_observation_poison_survives_restored_directory(
    citations, current
):
    scenario, page, markdown = current
    cache = ChecksumCache()
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=True,
        checksum_cache=cache,
    ).ok
    directory = scenario.paths.raw / "notes"
    parked = scenario.paths.raw / "parked"
    directory.rename(parked)
    directory.symlink_to(scenario.paths.extracted, target_is_directory=True)
    assert not citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=True,
        checksum_cache=cache,
    ).ok
    directory.unlink()
    parked.rename(directory)
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=True,
        checksum_cache=cache,
    )
    assert "citation_original_missing" in codes(report)


def test_staged_document_key_cannot_escape_through_parent_components(
    citations, current
):
    scenario, _, _ = current
    path = scenario.paths.root / "wiki/pages/../../../outside.md"
    with pytest.raises(ValueError, match="repository"):
        citations.validate_citations(
            scenario.paths, scenario.ledger, {path: "No citations"}
        )


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "prefix",
    [
        "    `indented example\n",
        "\t`tabbed example\n",
        "---\ntitle: `frontmatter\n---\n",
        "~~~text\n`fenced example\n~~~\n",
        "# `heading\n",
        "`earlier prose\n***\n",
        "`earlier prose\n---\n",
        "`earlier prose\nTitle\n===\n",
    ],
)
def test_review_excluded_block_tick_cannot_hide_prose_marker(
    citations, current, prefix, full
):
    scenario, page, _ = current
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: prefix + "A real claim.[^missing] `prose tick"},
        full=full,
    )
    assert "citation_definition_missing" in codes(report)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "claim",
    [
        "A claim.[^missing]: prose",
        r"A claim.\\[^missing]",
        r"A claim.\\\\[^missing]: prose",
    ],
)
def test_review_real_prose_marker_is_not_a_definition_or_escape(
    citations, current, claim, full
):
    scenario, page, _ = current
    assert "citation_definition_missing" in codes(
        citations.validate_citations(
            scenario.paths, scenario.ledger, {page: claim}, full=full
        )
    )


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "later_heading",
    [" ## Later", "   ## Later", "Later\n---", "Later\n===", "   Later\n   ---"],
)
def test_review_definition_under_later_semantic_heading_fails(
    citations, current, later_heading, full
):
    scenario, page, markdown = current
    markdown = markdown.replace(
        "## Sources\n\n", "## Sources\n\n" + later_heading + "\n\n"
    )
    assert "citation_definition_out_of_section" in codes(
        citations.validate_citations(
            scenario.paths, scenario.ledger, {page: markdown}, full=full
        )
    )


@pytest.mark.parametrize(
    "example",
    [
        r"\[^missing]",
        r"\\\[^missing]",
        "`[^missing]: text`",
        "`start\n[^missing]\nend`",
        "    ## Later\n    `[^missing]`",
        "~~~\nLater\n---\n[^missing]\n~~~",
    ],
)
def test_review_valid_example_exclusions_preserve_real_definition(
    citations, current, example
):
    scenario, page, markdown = current
    markdown = markdown.replace("## Sources", example + "\n\n## Sources")
    assert citations.validate_citations(
        scenario.paths, scenario.ledger, {page: markdown}, full=True
    ).ok


@pytest.mark.parametrize("scenario_name", ["current", "historical"])
@pytest.mark.parametrize("full", [False, True])
def test_review_wrong_owner_poison_survives_later_valid_citation(
    citations, scenario_repo, scenario_name, full
):
    from brainlib.contracts import source_id_for_first_seen

    scenario = scenario_repo("citations/" + scenario_name)
    page = scenario.paths.wiki_pages / (scenario_name + ".md")
    markdown = page.read_text(encoding="utf-8")
    (record,) = scenario.ledger.load_all().values()
    (citation,) = citations.parse_citation_definitions(markdown, path=page)
    raw_origin = PurePosixPath("notes/b.txt")
    alternate = replace(
        record,
        source_id=source_id_for_first_seen(raw_origin, citation.content_sha256),
        current_raw_path=raw_origin,
    )
    scenario.ledger.save(alternate)
    cache = ChecksumCache()
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=full,
        checksum_cache=cache,
    ).ok
    invalid = markdown.replace(record.source_id, alternate.source_id)
    assert "citation_original_target_mismatch" in codes(
        citations.validate_citations(
            scenario.paths,
            scenario.ledger,
            {page: invalid},
            full=full,
            checksum_cache=cache,
        )
    )
    assert "citation_original_missing" in codes(
        citations.validate_citations(
            scenario.paths,
            scenario.ledger,
            {page: markdown},
            full=full,
            checksum_cache=cache,
        )
    )
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=full,
        checksum_cache=cache,
    ).ok


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "example",
    [
        "- `start\n  [^example]\n  end`",
        "1. `start\n   [^example]\n   end`",
        "10. `start\n    [^example]\n    end`",
        "  - `start\n    [^example]\n    end`",
    ],
)
def test_review2_valid_list_code_preserves_genuine_citation(
    citations, current, example, full
):
    scenario, page, markdown = current
    staged = markdown.replace("## Sources", example + "\n\n## Sources")
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: staged}, full=full
    )
    assert report.ok, report.issues


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "\n",
        "- next item\n",
        "  ## Heading\n",
        "  ***\n",
        "  ---\n",
        "  ~~~\n  `code\n  ~~~\n",
        "      `code\n",
    ],
)
def test_review2_list_block_boundary_keeps_real_marker_visible(
    citations, current, boundary, full
):
    scenario, page, _ = current
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: "- `unfinished\n" + boundary + "  Claim.[^missing] `unmatched"},
        full=full,
    )
    assert codes(report) == {"citation_definition_missing"}


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("fence", ["~~~", "```"])
@pytest.mark.parametrize(
    "prefix,external,line",
    [
        ("- {fence}\n  ignored code\n\n", "Outside ", 4),
        ("- {fence}\n  ignored\n", "- ", 3),
        ("1. {fence}\n   ignored\n", "2. ", 3),
        ("- parent\n  - {fence}\n    ignored\n", "  - ", 4),
        ("- {fence}\n  ignored\n", "> ", 3),
    ],
)
def test_review3_external_claim_after_list_fence_requires_definition(
    citations, current, prefix, external, line, fence, full
):
    scenario, page, _ = current
    report = citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: prefix.format(fence=fence) + external + "[Real](real.md)[^missing]"},
        full=full,
    )
    assert codes(report) == {"citation_definition_missing"}
    assert report.issues[0].details == {"line": line, "citation_id": "missing"}


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "example",
    [
        "- ~~~\n  [^example]\n  ~~~",
        "~~~\n[^example]\n\n~~~",
        "- ~~~\n\n  [^example]\n \n\n  [^still]\n  ~~~",
        "- ~~~\n  ~~~not-close\n  [^example]\n  ~~~~",
        "- parent\n  - ```\n    [^example]\n    ```",
        "- `start\n  [^example]\n  end`",
    ],
)
def test_review3_fence_controls_preserve_genuine_citation(
    citations, current, example, full
):
    scenario, page, markdown = current
    staged = markdown.replace("## Sources", example + "\n\n## Sources")
    report = citations.validate_citations(
        scenario.paths, scenario.ledger, {page: staged}, full=full
    )
    assert report.ok, report.issues


@pytest.mark.parametrize("full", [False, True])
def test_review_wrong_extracted_owner_poison_survives_record_restoration(
    citations, current, full
):
    scenario, page, markdown = current
    (record,) = scenario.ledger.load_all().values()
    cache = ChecksumCache()
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=full,
        checksum_cache=cache,
    ).ok
    derivation = record.derivations[record.active_derivation_id]
    alternate_id = "drv_" + "f" * 64
    changed = replace(
        record,
        derivations={alternate_id: replace(derivation, derivation_id=alternate_id)},
        active_derivation_id=alternate_id,
    )
    scenario.ledger.save(changed)
    invalid = markdown.replace(
        f"derivation_id: `{derivation.derivation_id}`",
        f"derivation_id: `{alternate_id}`",
    )
    assert "citation_extracted_target_mismatch" in codes(
        citations.validate_citations(
            scenario.paths,
            scenario.ledger,
            {page: invalid},
            full=full,
            checksum_cache=cache,
        )
    )
    scenario.ledger.save(record)
    assert "citation_extracted_missing" in codes(
        citations.validate_citations(
            scenario.paths,
            scenario.ledger,
            {page: markdown},
            full=full,
            checksum_cache=cache,
        )
    )
    version = record.versions[record.active_content_sha256]
    assert (
        cache.observe(
            scenario.paths, SnapshotNamespace.RAW_USER, version.raw_path, full=full
        ).byte_size
        == version.byte_size
    )
    cache.begin_transaction()
    assert citations.validate_citations(
        scenario.paths,
        scenario.ledger,
        {page: markdown},
        full=full,
        checksum_cache=cache,
    ).ok
