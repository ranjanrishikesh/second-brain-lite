# Markdown Parser Architecture Amendment

**Status:** Approved — user approved implementation on 2026-09-06

## Purpose

Second Brain Lite must answer from grounded evidence quickly, save approved
knowledge as validated wiki Markdown, and retrieve that durable wiki knowledge
quickly on later questions. Citation validation is part of that correctness
path: it must never silently treat an uncited factual claim as invisible merely
because the claim appears near complex Markdown containers.

The current handwritten lexical scanner has accumulated container-state repairs
for lists, tabs, nested lists, blockquotes, and fenced blocks. Fresh reviews
demonstrated that these repairs can hide real citation markers. This amendment
moves Markdown *block-structure* authority to a standards-oriented local parser
while retaining byte-exact repository behavior for citations and links.

## Decision

Declare `markdown-it-py>=4,<5` as a runtime dependency in `pyproject.toml`.
Use `MarkdownIt("commonmark")` as the authoritative local classifier for
Markdown block structure. No public web request, LLM call, package installation,
or external service is part of parsing or validation.

The project currently has no lockfile or package-manager-specific workflow. The
single dependency declaration is therefore the complete packaging change;
ordinary environment setup resolves its transitive dependencies.

## Why this is hybrid, not parser-only

`markdown-it-py` correctly classifies CommonMark lists, nested containers,
blockquotes, fences, indented code, tabs, and headings. Its source maps are
block-level line ranges, however, and inline child tokens do not preserve raw
source offsets. Its link tokens also normalize destination and label syntax.

The repository requires exact original link bytes and original line locations:
historical citation rewriting may edit only the intended link span, and a
literal backtick or percent escape must not be normalized before deterministic
validation. A parser-only replacement would violate those contracts.

The resulting architecture has two deliberately separate layers:

1. **Structural layer — `markdown-it-py`.** It establishes which source-line
   ranges are semantic inline blocks, which are fenced/indented code, where
   headings occur, and where container boundaries begin/end. It alone decides
   list, tab, nested-list, blockquote, and fence membership.
2. **Byte-preserving lexical layer — repository code.** It examines only raw
   lines belonging to parser-approved inline blocks. It finds repository custom
   citation markers/definitions and Markdown link spans, masks inline code only
   within that already-approved block range, and returns original bytes and
   one-based source lines. It never reimplements block/container membership.

This split lets the parser solve Markdown grammar while the repository retains
the precise spans needed for canonical citation identity and safe rewrites.

## Data flow

1. Preserve the full raw Markdown string and its original one-based line
   numbering.
2. If the document begins with a complete repository-supported YAML frontmatter
   envelope, exclude that envelope from parser input and retain its line offset.
   An incomplete opening delimiter retains current behavior and is parsed as
   ordinary Markdown.
3. Parse the remaining body with `MarkdownIt("commonmark")`.
4. Translate parser token maps back to original line ranges using the retained
   frontmatter offset. Only `inline` token ranges are eligible for custom
   marker/link extraction. Fenced and indented-code token ranges are ineligible.
5. Derive semantic headings from parser heading tokens and their maps. Raw text
   may supply display spelling only after parser token type and location establish
   that the line is a heading; a separate raw heading grammar is not authoritative.
6. Feed each approved inline range to the raw lexical layer. It returns:
   `MarkdownLink`, `CitationMarker`, citation-definition positions, and any
   explicit scanner diagnostics. Link labels/destinations are sliced from the
   original source, never parser-normalized token fields.
7. `validate_citations`, graph validation, historical-link rewriting, and other
   current consumers continue using `scan_markdown` data. They do not call an
   LLM or make a network request.

## Compatibility contract

`MarkdownLink`, `MarkdownHeading`, `CitationMarker`, and existing
`MarkdownScan` fields remain compatible for callers. `MarkdownScan` may gain an
additive diagnostics field with an empty default. This allows citation and graph
validation to fail closed when a parser/byte-lexer reconciliation cannot prove a
semantic result, while existing readers can continue to consume the established
token fields.

The following remain application-level syntax, not CommonMark extensions:

- `[^citation-id]` claim markers;
- one-line citation definitions;
- the final `## Sources` contract;
- exact citation identity fields and canonical destinations.

They are recognized only in parser-approved semantic inline blocks. A citation
definition or marker in frontmatter, a fence, indented code, or another
parser-classified non-inline block is not a claim reference or definition.

## Fail-closed behavior

The parser is authoritative for structural membership, but it does not produce
raw inline offsets. The byte-preserving layer must therefore reconcile every
reported raw link/marker/definition with a parser-approved inline source range.

If reconciliation is ambiguous or invalid, the scanner reports a deterministic
diagnostic. Citation validation converts a citation-relevant diagnostic to an
error (for example, `citation_markdown_ambiguous`) rather than omitting a marker
or certifying a document. Graph validation likewise reports a deterministic
structural error when an ambiguous result affects graph links/headings. This is
preferable to guessing, silently normalizing, or asking an LLM to decide syntax.

CommonMark is the documented structural baseline. Repository custom citation
syntax continues to be validated above it. Unsupported Markdown extensions may
remain plain CommonMark text, but no extension may cause a citation-relevant
construct to be silently skipped; ambiguity fails validation.

## Error and performance expectations

Parsing is local, deterministic, and linear in document size apart from the
parser's own documented behavior. It introduces no browser, web, `rg`, ledger,
or LLM work. It runs only at existing Markdown-validation/transaction boundaries
and improves answer reliability by preventing invalid pages from being committed
to the durable wiki.

The answer workflow remains unchanged in responsibility:

- LLM-guided roles judge evidence sufficiency and draft proposed knowledge.
- deterministic code validates citations/graph structure and atomically applies
  approved wiki changes;
- committed wiki pages become the fast local knowledge surface for later
  wiki-first questions.

## Implementation scope

Expected implementation surfaces:

- `pyproject.toml` and its dependency-contract test;
- `brainlib/markdown.py`, split into focused structural and raw-span helpers if
  that improves isolation;
- citation and graph consumers only where they need to surface scanner
  diagnostics;
- `docs/brain/schemas/citation.md` and relevant agent/developer instructions;
- scanner, citation, graph, transaction, and integration regression tests.

No source ledger schema, evidence checksum model, search-run format, web
approval policy, agent semantic role, raw-source data, or existing wiki content
migration changes are required.

## Verification requirements

Tests must prove all prior failures and controls, including:

- list, ordered-list, nested-list, blockquote, tab-indented, and fence
  transitions cannot hide a real marker/link;
- real code examples, inline code, literal backticks in labels/destinations,
  escaped syntax, and raw source line locations remain correct;
- frontmatter remains excluded under the existing repository contract;
- parser-approved headings correctly enforce final `Sources` locality;
- original byte-preserving citation rewrites alter only the intended definition
  span;
- ambiguous parser/raw reconciliation fails validation rather than succeeding;
- normal/full checksum semantics, namespace ownership poisoning, source search,
  wiki graph validation, and the full repository suite remain compatible.

## Non-goals

- Letting an LLM decide Markdown syntax or structural validity.
- Replacing semantic evidence judgment with parser output.
- Adding public-web access, automatic package installation, or optional
  converters.
- Supporting every Markdown extension as a new repository grammar.
