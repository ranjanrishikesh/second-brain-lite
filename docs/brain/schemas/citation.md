# Citation grammar

Every factual claim MUST use one or more adjacent citation markers that follow
this citation-definition grammar. Define each marker exactly once in that
document, under its final `## Sources` heading.
A source list alone does not make uncited factual prose valid. The deterministic
validator checks marker resolution and retained evidence; assessing which prose
is factual and whether evidence supports it remains the curator's responsibility.

For a direct child of `wiki/pages/` or `wiki/questions/`:

```text
A factual claim.[^cite-alpha-page-2]

## Sources

[^cite-alpha-page-2]: source_id: `src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`; content_sha256: `cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`; derivation_id: `drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`; anchor: `page:2`; [original](../../sources/raw/notes/a.pdf); [extracted](../../sources/extracted/notes/a.pdf/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.md#page:2)
```

Definitions occupy one line. Citation labels contain ASCII letters, digits,
underscores, and hyphens and start with a letter or digit. The four backtick-quoted
identity fields and two evidence links are semicolon-delimited and must each
appear exactly once. Field order does not affect identity. Source IDs are `src_`
plus 64 lowercase hexadecimal characters; derivation IDs are `drv_` plus 64;
content checksums are exactly 64 lowercase hexadecimal characters.

Anchors use `<kind>:<value>`, with a nonempty value and one of `line`, `page`,
`slide`, `sheet`, `section`, `row`, or `block`. The exact canonical `Anchor` must
occur in the retained derivation's anchors. The extracted link's fragment is
exactly that serialization, such as `#page:2`. Original links have no fragment.
Frontmatter and fenced or indented code do not contain citation definitions or
claim references. Inline code and escaped markers do not create claim references.

Local CommonMark parsing with `markdown-it-py` defines semantic blocks, container
membership, code exclusion, and headings. Repository code extracts custom citation
syntax and link spans only from parser-approved inline blocks, preserving their
original bytes and one-based physical CR/LF source lines. Unicode line and
paragraph separators remain content. Citation markers, one-line definitions, and
the final `## Sources` requirement remain repository syntax above CommonMark.

## Exact evidence and paths

All three identity fields select one retained `SourceRepresentation`; the current
active representation is never substituted for missing history. Its `raw_path`
is relative to `sources/raw`; its `extracted_path` is repository-relative.
Links must name those exact targets. Another existing file with identical bytes
is still a target mismatch. A materialized historical original uses
`sources/raw/_versions/<source-id>/<content-sha256>/<original-name>`. Approved web
evidence uses its immutable `_web` snapshot.

Destinations use the minimal relative POSIX path from the document's parent.
Encode each UTF-8 component using uppercase percent escapes; `/` separators and
unreserved ASCII letters, digits, `-`, `.`, `_`, and `~` remain literal. For example:

```text
notes/Résumé (100% #1).txt
notes/R%C3%A9sum%C3%A9%20%28100%25%20%231%29.txt
```

Decode exactly once. Reject schemes, authorities, absolute paths, query strings,
backslashes, NUL, malformed or lowercase escapes, unnecessary escapes, encoded
separators, encoded dot components, literal `.` components, and embedded or
canceling `..` traversal. Only the minimal necessary leading `..` components are
allowed. Resolve below the repository root and require the re-encoded destination
to equal the supplied bytes. Fragments are handled separately. A symlink spelling
that differs from the canonical resolved path is not a canonical citation target.

## Validation and staged documents

`MarkdownDocuments` maps absolute logical repository `Path` keys to authoritative
text. Staged text wins over live disk contents and may name unpublished documents.
An iterable of paths is read once into that same view. Definitions and references
are scoped to their document; a definition elsewhere cannot resolve a marker.

Normal validation checks recorded file size and nanosecond modification time
without calculating new hashes. Full validation differs only by additionally
comparing SHA-256 for original and extracted bytes. All evidence observations use `ChecksumCache`
with the exact logical path and `RAW_USER`, `RAW_VERSION`, `RAW_WEB`, or `EXTRACTED`
namespace. Each reference rechecks file identity; repeated full checks reuse the
checksum only for the same stable namespace/path identity. A replaced or unsafe
file fails closed. A standalone validator starts its cache transaction; a supplied
cache belongs to the caller and is never reset.
When a caller detects a namespace-owner mismatch before observing the file, it
uses `ChecksumCache.poison(namespace, logical_path)` to retain that authority
failure. Later references to the same key fail until `begin_transaction()` starts
a new transaction; unrelated evidence keys remain usable.

The result is `ValidationReport(checks=("citations",), issues=..., corpus_revision=...)`.
All citation issues are errors. Codes distinguish:

- `citation_markdown_ambiguous` when parser structure and raw source spans cannot
  be reconciled. Each scanner diagnostic retains its message, original source
  `line`, and `markdown_code`, in scanner order before ordinary document issues.
- `citation_definition_missing`, `citation_definition_duplicate`,
  `citation_definition_out_of_section`, `citation_definition_unused`, and
  `citation_definition_invalid`.
- `citation_source_id_invalid`, `citation_content_sha256_invalid`,
  `citation_derivation_id_invalid`, and `citation_anchor_invalid`.
- `citation_source_missing`, `citation_version_missing`,
  `citation_derivation_missing`, and `citation_anchor_missing`.
- `citation_destination_noncanonical`, `citation_target_escape`,
  `citation_original_target_mismatch`, `citation_extracted_target_mismatch`,
  `citation_extracted_fragment_mismatch`, and `citation_original_stale`.
- `citation_original_missing` and `citation_extracted_missing` for missing or
  unsafe file observations, including cache identity changes.
- `citation_original_fingerprint_mismatch` and
  `citation_extracted_fingerprint_mismatch` for metadata drift;
  `citation_original_checksum_mismatch` and `citation_extracted_checksum_mismatch`
  for full-mode byte mismatches.

## Durable rewrites

An ambiguous scan fails validation. Direct citation parsing, canonicalization,
and historical rewriting raise `CitationParseError`; no ambiguous Markdown is
rewritten. This local failure invokes neither web activity nor an LLM.

`rewrite_historical_original_links` accepts a sorted tuple of `CitationRewrite`
objects with unique `(source_id, content_sha256)` keys. It changes only the matching
definition's original destination. It preserves all other versions, sources,
identity fields, claim markers, prose, code, and extracted links.

The exact rewrite set comes from `VersionAdoption.citation_rewrites` or all verified
`citation_rewrite` events streamed from `SyncResultStore.iter_events(reference)`.
`SyncReport.citation_rewrites` is only a bounded display sample. Consumers drain
the verified exact events, durably deduplicate and apply `result_id`, then explicitly
acknowledge the result.

Every wiki transaction also runs `canonicalize_citation_destinations` against the
ledger. This idempotent durability fallback restores both evidence destinations
from each exact retained representation if a process lost the sync/adoption report
after its ledger checkpoint. Unresolved identities remain unchanged for precise
validation errors. Canonicalization preserves identity fields and all text outside
the two actual definition-link destinations.
