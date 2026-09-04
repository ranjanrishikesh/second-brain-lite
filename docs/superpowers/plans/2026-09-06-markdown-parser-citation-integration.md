# Markdown Parser Citation Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the handwritten Markdown container classifier with a local CommonMark parser-backed, byte-preserving scanner so citation and wiki validation cannot silently hide semantic Markdown content.

**Architecture:** `markdown-it-py` owns block membership and heading structure; repository code operates only inside parser-approved inline source-line ranges to retain exact raw link/citation bytes and one-based locations. Reconciliation failures produce deterministic diagnostics, which citation validation and wiki parsing fail closed rather than guessing.

**Tech Stack:** Python 3.11+, `markdown-it-py>=4,<5`, pytest, existing `ValidationIssue` and `MarkdownScan` contracts.

**Spec:** `docs/superpowers/specs/2026-09-06-markdown-parser-citation-architecture.md`

## Global Constraints

- Declare exactly `markdown-it-py>=4,<5` as a runtime dependency; do not add a lockfile, optional parser plugin, package installation step, browser request, web request, LLM call, or external service.
- `MarkdownIt("commonmark")` is the sole authority for block/container membership, fenced and indented code exclusion, and heading recognition.
- Retain the full raw Markdown text for lexical extraction. `MarkdownLink.destination`, `MarkdownLink.text`, citation IDs, and line numbers must come from original source slices, never parser-normalized link tokens.
- Preserve public compatibility for `MarkdownLink`, `MarkdownHeading`, `CitationMarker`, and all existing `MarkdownScan` fields. An additive `diagnostics` field must default to an empty tuple.
- Preserve the current complete-leading-frontmatter contract: an initial `---` through a later exact `---` is excluded; an unmatched opener remains ordinary Markdown. Use one CR/LF-only physical-line table and original source substrings for parser maps, offsets, ranges, headings, and raw extraction; do not use broad `str.splitlines()` line accounting or rejoin source lines.
- Repository citation markers and one-line citation definitions remain application syntax and are recognized only in parser-approved inline blocks.
- A parser/raw reconciliation failure is deterministic and fail-closed: citation validation emits `citation_markdown_ambiguous`; wiki parsing raises a deterministic structural `ValueError`; rewriting must not modify an ambiguous document.
- Do not invent graph-validator or CLI changes: this checkout has citation and wiki-model consumers only. Later graph work must consume the diagnostic contract when such a validator exists.
- Follow TDD for every behavior change: write an independently failing test, observe the expected failure, then write the smallest implementation that makes it pass.
- This session has one reachable commit. Stage only owned files and use `git commit --amend --no-edit`; verify `git rev-list --count origin/main..HEAD` stays `1`.

---

## File Structure

| File | Responsibility after this plan |
| --- | --- |
| `pyproject.toml` | Declares the CommonMark structural-parser runtime dependency. |
| `brainlib/markdown.py` | Converts parser block maps into approved raw ranges, derives headings from parser tokens, performs exact byte-preserving inline extraction, and reports reconciliation diagnostics. |
| `brainlib/citations.py` | Converts structural diagnostics to citation errors and prevents parsing/canonicalization/historical rewriting from proceeding on ambiguous Markdown. |
| `brainlib/wiki_models.py` | Refuses to construct page or question sections when structural reconciliation is ambiguous. |
| `docs/brain/schemas/citation.md` | Documents CommonMark-backed structural semantics and the new citation diagnostic. |
| `tests/unit/test_contracts.py` | Pins the runtime/development dependency boundary. |
| `tests/unit/test_markdown.py` | Exercises parser-backed structure, raw-byte preservation, frontmatter, code exclusion, and malformed-map diagnostics. |
| `tests/unit/test_citations.py` | Exercises diagnostic conversion and rewrite refusal without weakening retained-evidence checks. |
| `tests/unit/test_wiki_models.py` | Exercises deterministic wiki-parser refusal on structural ambiguity. |

## Shared Interfaces

```python
@dataclass(frozen=True)
class MarkdownDiagnostic:
    code: str
    message: str
    line: int

@dataclass(frozen=True)
class MarkdownScan:
    links: tuple[MarkdownLink, ...]
    headings: tuple[MarkdownHeading, ...]
    citation_markers: tuple[CitationMarker, ...]
    citation_definitions: tuple[CitationMarker, ...] = ()
    diagnostics: tuple[MarkdownDiagnostic, ...] = ()

def scan_markdown(markdown: str) -> MarkdownScan: ...
```

`MarkdownDiagnostic.line` is one-based in the original document. The scanner uses `markdown_reconciliation_ambiguous` for invalid, out-of-order, overlapping, or otherwise unprovable parser/raw source-map reconciliation. A parser exception is represented by the same public safe failure class or a more specific parser-failure code, but citation consumers map either scanner diagnostic to `citation_markdown_ambiguous`.

### Task 1: Parser-backed, byte-preserving Markdown scanner

**Files:**

- Modify: `pyproject.toml`
- Modify: `brainlib/markdown.py`
- Modify: `tests/unit/test_contracts.py`
- Modify: `tests/unit/test_markdown.py`

**Interfaces:**

- Consumes: raw Markdown strings; `markdown_it.MarkdownIt("commonmark")` block tokens and their zero-based, half-open `map` ranges.
- Produces: the shared `MarkdownDiagnostic`, additive `MarkdownScan.diagnostics`, and unchanged `scan_markdown(markdown: str) -> MarkdownScan` token fields for Tasks 2 and 3.
- Internal testable seam: `_approved_inline_ranges(tokens: Iterable[object], *, body_line_count: int, line_offset: int) -> tuple[tuple[tuple[int, int], ...], tuple[MarkdownDiagnostic, ...]]`. Returned ranges are zero-based half-open raw-document line ranges; diagnostic lines are one-based raw-document lines.

- [ ] **Step 1: Write the dependency-contract RED test**

Change the existing dependency-contract test so it proves the parser is a runtime dependency and `jsonschema` remains development-only.

```python
assert config["project"]["dependencies"] == ["markdown-it-py>=4,<5"]
assert "jsonschema>=4,<5" in config["project"]["optional-dependencies"]["dev"]
```

- [ ] **Step 2: Run the dependency-contract test and observe the expected failure**

Run: `python3 -m pytest tests/unit/test_contracts.py::test_jsonschema_is_declared_as_a_development_only_dependency -q`

Expected: failure because `project.dependencies` is currently empty.

- [ ] **Step 3: Declare the bounded runtime dependency**

Update only the runtime dependency array in `pyproject.toml`.

```toml
[project]
dependencies = ["markdown-it-py>=4,<5"]
```

Do not install packages or create a lockfile. The local environment already supplies the parser for tests.

- [ ] **Step 4: Re-run the dependency-contract test**

Run: `python3 -m pytest tests/unit/test_contracts.py::test_jsonschema_is_declared_as_a_development_only_dependency -q`

Expected: PASS.

- [ ] **Step 5: Write the structural-scanner RED tests**

Add focused tests before replacing production logic. Keep existing regressions intact and add these concrete controls:

```python
def test_scan_markdown_uses_commonmark_ranges_for_tabbed_nested_fences() -> None:
    scan = scan_markdown(
        "- parent\n\t- ~~~\n\t  [Hidden](hidden.md)[^hidden]\n"
        "\t  ~~~\n\nParent prose [Real](real.md)[^real]\n"
    )
    assert [(link.text, link.destination, link.line) for link in scan.links] == [
        ("Real", "real.md", 6)
    ]
    assert [(marker.citation_id, marker.line) for marker in scan.citation_markers] == [
        ("real", 6)
    ]


def test_approved_inline_ranges_fail_closed_for_an_invalid_parser_map() -> None:
    ranges, diagnostics = _approved_inline_ranges(
        (SimpleNamespace(type="inline", map=(1, 7)),),
        body_line_count=3,
        line_offset=2,
    )
    assert ranges == ()
    assert diagnostics == (
        MarkdownDiagnostic(
            "markdown_reconciliation_ambiguous",
            "Markdown parser returned an invalid inline source range.",
            4,
        ),
    )
```

Also add a table-driven test covering a tab-indented list fence, a space-plus-tab list fence, a nested-list fence followed by a parent continuation, a sibling list item, a blockquote transition, and outside prose. In each case, parser-classified code must hide its link/marker and the first semantic outside construct must retain exact one-based line and raw label/destination. Add a heading test with ATX and multiline setext headings proving headings are selected by CommonMark token type plus their paired inline map rather than a hand-authored container state machine. Retain controls for complete and incomplete CRLF frontmatter, a Unicode line-separator inside one physical source line, parser-exception line offset after frontmatter, escaped syntax, inline code, literal backticks in links, and raw CRLF-safe line locations. Add exact-byte link controls for `[a\\]](x.md)`, `[a](foo(and)bar.md)`, and `[a](foo\\(and\\).md)` while retaining the escaped non-link control `\\[literal](x.md)`.

- [ ] **Step 6: Run the new scanner tests and observe the expected failures**

Run: `python3 -m pytest tests/unit/test_markdown.py -q`

Expected: the invalid-map test fails because `_approved_inline_ranges` does not exist; at least one tab/nested-container control fails or exposes the current handwritten structural logic.

- [ ] **Step 7: Replace only the structural classification layer**

Refactor `brainlib/markdown.py` so `MarkdownIt("commonmark")` decides membership. Use a module-level parser, preserve the current `TypeError` for non-string input, and preserve original document line numbers.

```python
from markdown_it import MarkdownIt

_PARSER = MarkdownIt("commonmark")


def scan_markdown(markdown: str) -> MarkdownScan:
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    body, line_offset = _without_complete_frontmatter(markdown)
    try:
        tokens = _PARSER.parse(body)
    except Exception as error:
        return MarkdownScan((), (), (), (), (
            MarkdownDiagnostic("markdown_reconciliation_ambiguous", str(error), 1),
        ))
    ranges, diagnostics = _approved_inline_ranges(
        tokens, body_line_count=len(body.splitlines()), line_offset=line_offset
    )
    headings, heading_diagnostics = _headings_from_tokens(tokens, markdown, line_offset)
    links, markers, definitions = _scan_raw_inline_ranges(markdown, ranges)
    return MarkdownScan(links, headings, markers, definitions, diagnostics + heading_diagnostics)
```

The exact helper names may differ only if their signatures and behavior remain as described. `_without_complete_frontmatter` must preserve the current leading-envelope semantics and return an unmodified original-source body substring plus its number of excluded physical CR/LF lines. `_approved_inline_ranges` must inspect only `inline` tokens, translate their parser maps by `line_offset`, and reject an absent, non-two-item, boolean, non-integer, empty, out-of-bounds, duplicate, overlapping, or unordered map with the stated deterministic diagnostic instead of scanning guessed ranges. Validate in token encounter order; do not sort or deduplicate before validation. A parse exception reports the first body source line (`line_offset + 1`). It must not recreate list, quote, tab, fence, or indented-code state.

`_headings_from_tokens` must select `heading_open` tokens/maps emitted by CommonMark and reconcile each with its paired `inline` token/map. (Setext heading maps contain their underline, while their inline map does not.) It may use raw source slices to preserve the existing display spelling, but it must not independently decide whether a raw line is a heading. A malformed heading pairing/reconciliation emits a scanner diagnostic rather than inventing a heading.

`_scan_raw_inline_ranges` processes every approved range independently so inline-code masking cannot cross a parser block boundary. Replace the old flat link regular expression with a small stateful raw link-span lexer that balances destination parentheses, recognizes escaped label/destination delimiters, observes escape parity, and slices raw label/destination text without parser normalization. It may retain the established marker/definition lexical rules, but it must skip all raw extraction if range reconciliation emitted a diagnostic.

`_scan_raw_inline_ranges` may reuse/adapt the current escape-parity, inline-code masking, link, marker, and definition lexical rules, but it receives only parser-approved ranges. It must slice labels/destinations from the original raw line, not from a masked buffer or parser token. Remove the handwritten fence/list/blockquote/container ownership loop once the parser-backed implementation makes it unused.

- [ ] **Step 8: Run focused scanner and compatibility tests**

Run: `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_contracts.py -q`

Expected: PASS. The scanner must preserve every pre-existing raw-byte regression control while adding the CommonMark container controls.

- [ ] **Step 9: Lint and inspect Task 1 scope**

Run:

```bash
python3 -m ruff check brainlib/markdown.py tests/unit/test_markdown.py tests/unit/test_contracts.py
git diff --check
```

Expected: both commands exit 0. Inspect the diff to confirm no citation/wiki consumer behavior changed in this task.

- [ ] **Step 10: Amend the single session commit**

Run:

```bash
git add pyproject.toml brainlib/markdown.py tests/unit/test_contracts.py tests/unit/test_markdown.py
git commit --amend --no-edit
git rev-list --count origin/main..HEAD
```

Expected: commit count is `1`.

### Task 2: Citation fail-closed integration and grammar documentation

**Files:**

- Modify: `brainlib/citations.py`
- Modify: `docs/brain/schemas/citation.md`
- Modify: `tests/unit/test_citations.py`

**Interfaces:**

- Consumes: `scan_markdown(markdown) -> MarkdownScan`, including `MarkdownScan.diagnostics` from Task 1 and its physical CR/LF one-based line contract.
- Produces: deterministic `ValidationIssue("error", "citation_markdown_ambiguous", ...)` records with `{"line": diagnostic.line, "markdown_code": diagnostic.code}` details; `CitationParseError` from direct parsing/canonicalization/rewriting when an ambiguity exists.
- Preserves: `validate_citations(...) -> ValidationReport`, `parse_citation_definitions(...)`, `canonicalize_citation_destinations(...)`, and `rewrite_historical_original_links(...)` signatures and retained-evidence behavior.

- [ ] **Step 1: Write the citation diagnostic RED tests**

Construct a real `MarkdownScan` with an additive diagnostic and monkeypatch only `brainlib.citations.scan_markdown` for the unit boundary. Prove each consumer refuses ambiguity instead of omitting a claim or changing text.

```python
diagnostic = MarkdownDiagnostic(
    "markdown_reconciliation_ambiguous",
    "Markdown parser returned an invalid inline source range.",
    7,
)
monkeypatch.setattr(
    citation_module,
    "scan_markdown",
    lambda _markdown: MarkdownScan((), (), (), (), (diagnostic,)),
)
report = citation_module.validate_citations(scenario.paths, scenario.ledger, {page: "Claim.[^missing]"})
assert {(issue.code, issue.details["line"], issue.details["markdown_code"]) for issue in report.issues} == {
    ("citation_markdown_ambiguous", 7, "markdown_reconciliation_ambiguous")
}
```

Add separate direct-parser and rewrite tests that assert `CitationParseError` and that the returned/observable Markdown remains unchanged when scanner diagnostics are present. Keep a normal and `full=True` retained-evidence control proving successful documents still validate and canonicalization remains byte-local. Add a Unicode line-separator-before-definition control proving definition parsing and destination replacement use physical CR/LF lines rather than broad `str.splitlines()` indexing; the exact original source bytes before and after a durable rewrite must remain aligned.

- [ ] **Step 2: Run the new citation tests and observe the expected failures**

Run: `python3 -m pytest tests/unit/test_citations.py -q`

Expected: failures because scanner diagnostics are currently ignored and rewriting can continue.

- [ ] **Step 3: Centralize scan reuse and diagnostic conversion**

Make one scan per document in each validation path and make all definition consumers reuse it.

```python
def _scan_diagnostic_issues(scan: MarkdownScan, path: Path | PurePosixPath) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            "error",
            "citation_markdown_ambiguous",
            diagnostic.message,
            PurePosixPath(path),
            {"line": diagnostic.line, "markdown_code": diagnostic.code},
        )
        for diagnostic in scan.diagnostics
    ]

def _definition_lines(markdown: str, scan: MarkdownScan) -> tuple[tuple[int, re.Match[str]], ...]:
    ...
```

Refactor `_parse_definitions` to accept the already-computed scan rather than calling `scan_markdown` again. In `validate_citations`, obtain the scan once, append `_scan_diagnostic_issues` before ordinary marker/definition issues, and reuse that scan for definitions, markers, and headings. In direct parsing, canonicalization, and historical rewrite paths, convert diagnostics into `CitationParseError` before accepting citations or applying replacements. Replace any line-number lookup or replacement helper that relies on broad `str.splitlines()` with a physical CR/LF line table compatible with Task 1; preserve every non-CR/LF Unicode source character and original terminator. Preserve existing issue order for documents without diagnostics and do not suppress ordinary parse issues merely because a diagnostic exists.

- [ ] **Step 4: Document the structural contract**

Amend `docs/brain/schemas/citation.md` with a concise clause that CommonMark parsing defines semantic blocks/headings while repository code preserves raw citation/link bytes in parser-approved inline blocks. Add `citation_markdown_ambiguous` to the all-error code list and state that an ambiguous scan blocks validation and durable rewrites; it does not invoke a web tool or an LLM.

- [ ] **Step 5: Run citation tests in normal and full modes**

Run: `python3 -m pytest tests/unit/test_citations.py -q`

Expected: PASS, including retained-version, checksum, raw-link rewrite, and new ambiguity controls.

- [ ] **Step 6: Lint, inspect, and amend the single session commit**

Run:

```bash
python3 -m ruff check brainlib/citations.py tests/unit/test_citations.py
git diff --check
git add brainlib/citations.py docs/brain/schemas/citation.md tests/unit/test_citations.py
git commit --amend --no-edit
git rev-list --count origin/main..HEAD
```

Expected: lint/diff checks pass and commit count is `1`.

### Task 3: Wiki-model structural refusal

**Files:**

- Modify: `brainlib/wiki_models.py`
- Modify: `tests/unit/test_wiki_models.py`

**Interfaces:**

- Consumes: `MarkdownScan.diagnostics` from Task 1 through the existing `scan_markdown` call in `_sections(body: str, path: Path)`.
- Produces: the existing `parse_page` and `parse_question` APIs either return their typed models or raise `ValueError(f"{path}: markdown structure cannot be reconciled at line {line}: {message}")` before deriving section hierarchy.
- Preserves: existing heading hierarchy, duplicate-heading, title, required-section, and final-`Sources` checks when `diagnostics == ()`.

- [ ] **Step 1: Write the wiki ambiguity RED test**

Use the existing valid `_question_text` fixture and monkeypatch only the module-local scanner so the test exercises the public parsing entry point.

```python
def test_parse_question_rejects_ambiguous_markdown_structure(tmp_path, monkeypatch) -> None:
    record = tmp_path / "topic.md"
    record.write_text(_question_text("[Topic]", "[Alpha]", "[]"), encoding="utf-8")
    real_scan = wiki_models.scan_markdown(record.read_text(encoding="utf-8").split("---\n", 2)[2])
    monkeypatch.setattr(
        wiki_models,
        "scan_markdown",
        lambda _body: replace(
            real_scan,
            diagnostics=(MarkdownDiagnostic("markdown_reconciliation_ambiguous", "bad map", 4),),
        ),
    )
    with pytest.raises(ValueError, match=r"markdown structure cannot be reconciled at line 4: bad map"):
        wiki_models.parse_question(record)
```

Include a non-monkeypatched control proving the same valid record continues to parse and retains `Sources` final-section enforcement from parser-derived headings.

- [ ] **Step 2: Run the wiki test and observe the expected failure**

Run: `python3 -m pytest tests/unit/test_wiki_models.py -q`

Expected: the new ambiguity test fails because `_sections` currently ignores `scan.diagnostics`.

- [ ] **Step 3: Fail closed before hierarchy processing**

Update `_sections` immediately after `scan = scan_markdown(body)`.

```python
if scan.diagnostics:
    diagnostic = scan.diagnostics[0]
    raise ValueError(
        f"{path}: markdown structure cannot be reconciled at line "
        f"{diagnostic.line}: {diagnostic.message}"
    )
```

Use the first diagnostic because the parser's deterministic source-order diagnostic is sufficient to block an unsafe structural interpretation. Do not change relationship parsing, frontmatter parsing, or the pre-existing final-`Sources` policy.

- [ ] **Step 4: Run focused wiki and scanner compatibility tests**

Run: `python3 -m pytest tests/unit/test_wiki_models.py tests/unit/test_markdown.py -q`

Expected: PASS.

- [ ] **Step 5: Lint, inspect, and amend the single session commit**

Run:

```bash
python3 -m ruff check brainlib/wiki_models.py tests/unit/test_wiki_models.py
git diff --check
git add brainlib/wiki_models.py tests/unit/test_wiki_models.py
git commit --amend --no-edit
git rev-list --count origin/main..HEAD
```

Expected: lint/diff checks pass and commit count is `1`.

## Cross-task verification

After Tasks 1–3 have each passed review, run the following before broad final review:

```bash
python3 -m pytest tests/unit/test_contracts.py tests/unit/test_markdown.py tests/unit/test_citations.py tests/unit/test_wiki_models.py -q
python3 -m pytest tests/integration/test_validate_ledger.py -q
python3 -m ruff check brainlib/markdown.py brainlib/citations.py brainlib/wiki_models.py tests/unit/test_contracts.py tests/unit/test_markdown.py tests/unit/test_citations.py tests/unit/test_wiki_models.py
git diff --check
git status --short
git rev-list --count origin/main..HEAD
```

Expected: every test/lint/diff check passes, the working tree is clean after the final amendment, and the reachable commit count is `1`. Then use a fresh whole-branch review focused on parser block maps, raw-byte preservation, fail-closed consumer handling, and no unintended source/web/ledger behavior changes.
