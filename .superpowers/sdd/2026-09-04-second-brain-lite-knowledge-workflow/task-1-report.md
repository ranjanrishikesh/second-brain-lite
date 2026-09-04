# Task 1 — evidence and Markdown contracts

## RED

Added focused contract tests before implementation, then ran:

```text
python3 -m pytest tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_wiki_models.py -v
```

The required RED was observed: collection stopped with four expected
`ModuleNotFoundError` failures for `brainlib.evidence`,
`brainlib.frontmatter`, `brainlib.markdown`, and `brainlib.wiki_models`.

## GREEN

Implemented the strict evidence packet, frontmatter, Markdown lexical scan,
and page/question-record contracts, plus reusable knowledge test builders,
the one-overlay scenario fixture, valid Markdown fixtures, and the three
human-readable schema contracts.

The focused GREEN command passed **16 tests**. Full verification passed
**1312 tests** in 112 seconds:

```text
python3 -m compileall -q brainlib tests
python3 -m pytest -q
```

`git diff --check` also passed.

## Interfaces delivered

- `brainlib.evidence`: `SearchPassName`, page/pass records, source-versioned
  evidence items, strict `EvidencePacket`, `WikiEvidencePacket`,
  `RevalidatedCitation`, document-qualified `CitationRef`, and
  `CuratorEvidence`; both packet codecs reject duplicate JSON keys and emit
  canonical sorted-key JSON with a trailing newline.
- `brainlib.frontmatter`: strict schema-sized YAML subset parser with staged
  text override and duplicate-key rejection.
- `brainlib.markdown`: code/frontmatter-aware links, headings, and citation
  marker scanner.
- `brainlib.wiki_models`: `parse_page`/`parse_question` and their immutable
  record types, including strict frontmatter, dates, body sections, and
  relationship grammar.
- `tests.helpers_knowledge`: the shared IDs, deterministic completed-pass
  builder, scenario holder, and metadata restoration helper. `scenario_repo`
  is fixture-only in `tests/conftest.py`; it uses existing `RepoPaths` and
  `LedgerStore` rather than reimplementing repository plumbing.

## Ruling / concern

The supplied completed-pass pseudocode selected the leading 32 characters of
a 64-character zero-padded number. For the small test run numbers this makes
all `srch_` IDs identical, contradicting the required distinct-run-ID packet
invariant and preventing the supplied evidence round-trip test from creating
a valid packet. The shared builder therefore uses the low 32 characters,
which preserves the required `srch_<32-lowercase-hex>` form and yields
distinct deterministic IDs. No production contract was relaxed.

## Review round 1 — RED/GREEN strictness fixes

Added five causal regression groups before changing production code and ran
the Task 1 focused command. RED produced **9 failures / 16 passes**:

- a fence closer was accepted merely because it began with backticks, so
  ```` ```not-a-close ```` exposed hidden links and citation markers;
- section ranges ended at the next heading regardless of level, silently
  discarding nested relationship content, while title position and headings
  after Sources were not structurally checked;
- inline list values accepted mapping-shaped `a: b`, and quoted scalar parsing
  accepted interior quote characters such as `"a"b"`;
- persisted question search terms reused the nonempty source-pass rule and
  rejected an otherwise valid wiki-only fast path;
- wiki match IDs accepted arbitrary strings and uniqueness included matched
  terms, allowing one document/record identity to appear twice.

GREEN changes require a complete whitespace-only closing fence, derive section
content through the next same-or-higher heading, require the matching H1 first
and `## Sources` last overall, validate scalar/list grammar consistently,
allow empty persisted question term sets only, and enforce canonical/unique
record and document identities independently of term tuples. The direct codec
test verifies duplicate identity rejection after JSON decode as well as at the
constructor boundary.

Focused GREEN passed **24 tests**:

```text
python3 -m pytest tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_wiki_models.py -v
```

Complete verification after the review fixes passed **1320 tests** in 100.71
seconds:

```text
python3 -m compileall -q brainlib tests
python3 -m pytest -q
git diff --check
```

The project has no configured formatter or linter in `pyproject.toml`; no
package was installed merely to add one. The existing source style and the
compile/test/whitespace checks are clean.
