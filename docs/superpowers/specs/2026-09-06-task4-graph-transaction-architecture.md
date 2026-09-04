# Graph and Wiki Transaction Architecture Amendment

**Status:** Approved for implementation under the user's standing authorization
on 2026-09-06.

## Purpose

Task 4 turns proposed wiki edits into a deterministic, evidence-preserving
graph and the sole durable wiki publication path. The original task correctly
required a complete candidate search before every changed logical record, but
the accepted search and Markdown contracts expose a few necessary seams that
were not named in its initial file list. This amendment defines those seams
before code is written so an apparently complete graph cannot be forged,
partially scanned, or published through a lock re-entry.

No public-web request, package installation, LLM call, source conversion, or
direct live wiki write is part of this work.

## Compatibility boundaries

Normal research/freshness search remains unchanged. `SearchRequest`,
`SearchResult`, `SearchRunProof`, `SearchMatch`, the normal cursor schema, and
the existing research/freshness spool payloads retain their current public
meaning. A candidate run is a separate purpose of the existing authenticated
retained-run mechanism, not a second cursor or an unbounded in-memory search.

Existing Markdown public values retain equality and positional construction:
new source-span fields on `MarkdownLink` are keyword-only and excluded from
repr/equality, while `MarkdownScan.visible_text_spans` is appended after the
existing `diagnostics` field with an empty default. Existing callers therefore
continue to see the same links, headings, markers, definitions, diagnostics,
and equality behavior.

## Canonical logical wiki records

A logical record is exactly one direct regular Markdown file under either
`wiki/pages/<name>.md` or `wiki/questions/<name>.md`. It has no nested child
directory, dot component, backslash, NUL, symlink, or nonregular target. A
shared internal normalizer accepts an absent direct path only for a new staged
write; live enumeration treats an unsafe entry as a validation error rather
than following or silently skipping it.

The graph slug is the exact direct filename stem, with no ASCII folding,
Unicode normalization, or inferred title slug. Slugs and record IDs must be
globally unique across pages and questions. Titles, aliases, canonical
questions, and prior phrasings form exact display names only; a name resolving
to two different records is an ambiguity error, never an automatic target
choice. Relationship labels remain authorial presentation text in Task 4:
the current parsed model deliberately validates destination and description
but does not retain labels, so this task validates destinations and reciprocal
edges rather than claiming to validate label/title equivalence.

## Parser-proven visible text

The graph's first-meaningful-occurrence rule cannot be implemented with a raw
regex or line-only links: a plain occurrence before a link on the same line,
inline code, image alt text, and a link destination all have different meaning.
The scanner therefore gains only the following additive position API:

```python
@dataclass(frozen=True)
class MarkdownSourceSpan:
    # Python-string offsets into the exact scan input; end is exclusive.
    start: int
    end: int
    # One-based physical CR/LF line coordinates, inclusive.
    line: int
    end_line: int

@dataclass(frozen=True)
class MarkdownVisibleText:
    text: str
    source_span: MarkdownSourceSpan
```

Every scanner-created direct `MarkdownLink` exposes keyword-only
`source_span` and `label_span`; both are populated for reconciled links. A
clean scan exposes ordered `visible_text_spans`. They are exact contiguous raw
source chunks and consumers must never join adjacent chunks before matching.
They contain parser-approved visible prose and direct-link label text only.
They exclude frontmatter, fenced/indented code, inline code, image alt text and
destinations, direct-link syntax/destination/title, and any unreconciled
region. A valid image nested in a link label is excluded while other text in
the outer label remains visible. Literal malformed or escaped syntax remains
visible because the parser did not recognize it as hidden Markdown syntax.

Implementation must derive the chunks from the parser/raw reconciliation
already used by `scan_markdown`: project parser inline positions to raw
offsets, prove raw witnesses equal parser spans, subtract code and entire image
spans, retain only direct-link label interiors, split at exclusion, newline, or
raw-offset discontinuity, and emit original source slices. Any scan diagnostic
returns no visible spans along with the existing fail-closed result.

## Graph semantics

`validate_graph` receives either a complete authoritative logical
`Path -> str` mapping or a safe complete live record tree. A supplied mapping
is not merged with live files. This lets the transaction validate an exact
postimage consisting of live records overlaid by staged writes and deletions.
`wiki/index.md` is not a logical record.

Each full document is scanned once before parsing. Every scanner diagnostic is
an error containing its line and Markdown diagnostic code; that document then
contributes no inferred links, headings, relationships, or targets. A
frontmatter/model parse failure becomes `wiki_record_invalid` and likewise
does not produce guessed secondary graph errors.

All ordinary scanner links except those on an exact scanner-reported citation
definition line are graph links. Their destination grammar is strict:

1. Split at zero or one literal `#`; empty or multiple fragments fail.
2. Resolve the path portion exactly once with `resolve_markdown_path`, then
   require the resolved target to be a canonical logical record in the supplied
   mapping. Schemes, authorities, queries, backslashes, traversal, literal
   spaces, lowercase escapes, optional Markdown titles, external links, and
   noncanonical spellings fail.
3. If present, a fragment must be canonical UTF-8 percent encoding using
   `quote(decoded, safe="-._~")` with uppercase escapes. Its decoded text must
   exactly and uniquely equal a scanner-reported heading on the target. It is
   case-sensitive and Unicode-exact; no browser-specific slug algorithm is
   applied.
4. A path resolving to its source record is a self-link error even when it has
   a fragment. A relationship-list edge may not have a fragment.

Citation definition links are excluded only because they are evidence syntax;
ordinary links in Sources prose still obey the strict graph grammar.

Relationships are reciprocal record claims. Page-to-page requires a reverse
page-to-page edge. Page-to-question requires the question's reverse
question-to-page edge, and question-to-page requires the page's reverse
page-to-question edge. Wrong kind, missing target, self target, noncanonical
path, and missing reverse edge have deterministic separate graph diagnostics.

For every declared related target, each non-structural top-level H2 content
section is inspected independently. Pages inspect Summary and topical H2
sections; questions inspect Current answer, Supporting evidence, and
Contradictory evidence. Titles, headings, related lists, Sources,
frontmatter, citation-definition lines, code, images, and hidden link syntax do
not satisfy the rule. If any exact whole-token occurrence of an unambiguous
target display name appears in one such section, the earliest raw offset must
be inside a direct-link label resolving to that target. The tie order is raw
offset, then longest matching name, then lexical name. A whole-token boundary
is a non-alphanumeric/non-underscore boundary using Unicode character
semantics. A later correct link cannot cure an earlier plain, code-hidden, or
wrong-target occurrence.

The generator-owned index is exactly:

```text
# Second Brain Lite

## Pages

- [Title](pages/path.md)

## Questions

- [Question](questions/path.md)
```

It includes both headings even when a group is empty, has one final newline,
sorts each group by canonical logical relative path, uses
`encode_markdown_path(wiki/index.md, target)`, and escapes Markdown label
brackets/backslashes. `validate_graph` compares the complete candidate index
byte-for-byte and reports a stale generated index instead of rewriting it.

## Purpose-bound link candidate runs

The retained authenticated metadata gains only:

```python
purpose: Literal["search", "link_candidates"]
excluded_page_path: str | None
```

Both values are HMAC-authenticated. Legacy metadata lacking `purpose` is read
as `"search"`; the normal cursor JSON remains unchanged. Normal public resume
and proof verification reject candidate-purpose runs, and candidate endpoints
reject normal-purpose runs.

A link candidate run binds the ordered unique nonempty terms and a canonical
excluded logical page path. Its wiki operand identity hashes every safe live
wiki record, including an existing target, plus a canonical excluded-path
marker; `rg` receives every operand except that target. Thus an edit to the
target or any candidate makes the proof stale, while a not-yet-live staged
target remains bound by its intended path.

Candidate results are one deterministic record per matching other record, in
canonical path order. They use the first actual `rg` match event, not context,
and retain `path`, `line`, the first supplied ordered term proved by the event's
validated byte submatch, exact match-line context, and page/question kind.
Candidate records—not normal `SearchMatch` rows—are hashed into paged results.
Zero matches remain a single complete empty page. Completion proves a contiguous
cursor chain from page zero to the only final complete page, immutable run/path/
term bindings, every per-page checksum, aggregate counts, no coverage gaps,
and no unserved page.

`graph.py` owns the public `LinkCandidate`, `LinkCandidateResult`, and
`LinkCandidateRunProof` adapters. `search_runs.py` owns private candidate
spool/page machinery and a private
`_verify_link_candidate_run_locked(...)` seam. The seam validates retained
HMAC metadata, exact purpose/path/terms, corpus revision, recomputed live wiki
operand identity, manifest checksums, counts, page count, and final-page
service state. It must not acquire locks.

## Manifest-only publication and recovery

`WikiManifest` retains the documented JSON shape. A staging run is derived
from every write path, which must be exactly
`.brain/wiki-staging/wstg_<32-lowercase-hex>/files/wiki/(pages|questions)/<name>.md`.
All write paths share that run. A loaded manifest must itself be exactly
`.brain/wiki-staging/<same-run>/manifest.json`; delete-only and empty in-memory
manifests require no staging run. Staging files are regular, nonsymlink,
single-link, strict UTF-8 files and are rechecked for exact hash immediately
before publication. `wiki/index.md` is generator-owned and cannot be an
explicit change.

The transaction uses descriptor-pinned directory access and existing safe
ledger I/O helpers; production code does not trust convenience `Path` reads or
writes for staging/journal/live paths. An injected `replace_file` test seam is
used only after the relevant parent descriptors have been pinned and
revalidated. A live target must be absent or a safe direct regular file.

Public apply and recovery acquire source lock then wiki lock exactly once.
They delegate to no-lock internal helpers. In particular, apply never calls
the normal public search-proof verifier while it owns both locks, because that
would re-enter the source lock. The read-only transaction-state validator does
not repair, delete, or lock a journal.

Before a first live replacement/deletion, apply writes a bounded strict
canonical journal under `.brain/wiki-transaction.json`, fsyncs it and `.brain`,
then publishes sorted paths and fsyncs each parent. The journal has a fixed
schema, duplicate-key rejection, bounded size/target count/original-byte total,
canonical base64 original bytes, prior-existence metadata, and old/new hashes.
It is removed and `.brain` fsynced only after every publish succeeds. Recovery
validates all entries before mutation; each current target must be absent, old,
or new exactly as the entry permits. An unexpected third state fails closed.
Recovery restores every preimage (or prior absence) in sorted order and retains
the journal on any error, including a crash after all replacements but before
journal removal.

Under both locks, apply performs this order:

1. Recover an existing valid journal through `_recover_locked`.
2. Revalidate manifest, same-run staging files, and all path/hash bindings.
3. Compute and compare the canonical ledger corpus revision.
4. Verify one retained, complete, current link-candidate proof for every
   explicit changed page/question through the no-lock verifier.
5. Safely load the complete live logical mapping and overlay staged writes and
   deletes in memory.
6. Verify each supplied `CitationRewrite` against the retained ledger version,
   then canonicalize historical citation destinations across all postimage
   documents. This permits a later empty manifest to repair a lost adoption
   report without a candidate proof.
7. Run metadata-fast citation validation, complete graph validation, and exact
   generated-index validation on that one postimage/revision.
8. Compare parsed/scanned pre/post structure. `routine` and `rename` reject
   record deletion, citation marker/definition removal, and declared
   relationship removal. The specified destructive intents require a nonempty
   approval event ID.
9. Stage only byte changes plus the generated index, write the journal, and
   publish.

Any failure through step 8 leaves live wiki bytes untouched.

## Implementation slices

The top-level Task 4 is complete only after all four slices below pass fresh
review. Slices A and B may run concurrently because their file ownership is
disjoint; later slices are sequential.

1. **A — parser-proven visible spans.** Own `brainlib/markdown.py` and
   `tests/unit/test_markdown.py` only. Add the compatibility-safe span API and
   scanner regression matrix.
2. **B — authenticated candidate-run core.** Own `brainlib/search.py`,
   `brainlib/search_runs.py`, `tests/unit/test_search.py`, and
   `tests/unit/test_search_runs.py` only. Add purpose/path binding, candidate
   paging/spooling, endpoint fencing, and the no-lock retained verifier.
3. **C — graph candidates, validation, and generated index.** Own
   `brainlib/graph.py`, graph fixtures, `tests/unit/test_graph.py`, and the
   narrow helper additions required for graph scenarios. It depends on A and B.
4. **D — the only wiki write transaction.** Own `brainlib/wiki_transaction.py`,
   transaction fixtures, `tests/unit/test_wiki_transaction.py`, and the narrow
   helper additions required for staging/manifests. It depends on B and C.

Each slice follows test-driven development, runs its focused regression set,
is independently reviewed before acceptance, and is folded into the sole
session commit only by the root agent.
