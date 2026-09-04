# Task 4 Graph and Transaction Integration Plan

**Goal:** Implement the approved Task 4 graph and manifest-only publication
workflow without weakening accepted search, Markdown, citation, or lock
contracts.

**Spec:** `docs/superpowers/specs/2026-09-06-task4-graph-transaction-architecture.md`

**Execution rule:** This plan refines Task 4 in
`docs/superpowers/plans/2026-09-04-second-brain-lite-knowledge-workflow.md`.
The original API names and required behavior remain authoritative where this
plan is silent. One reachable session commit is maintained by amending the
existing commit after each accepted slice; workers do not stage or commit.

## Slice A — Parser-proven visible spans

**Owns:** `brainlib/markdown.py`, `tests/unit/test_markdown.py`

1. Write causal RED tests for raw source spans and visibility: same-line plain
   before a link, inline code before a link, direct-link labels/destinations and
   titles, image alt/destination, nested images, CRLF/CR, Unicode U+2028,
   frontmatter, fences, malformed literal syntax, no chunk joining, and scanner
   diagnostics returning no spans.
2. Run `python3 -m pytest tests/unit/test_markdown.py -q` and observe the new
   failures.
3. Add `MarkdownSourceSpan`, `MarkdownVisibleText`, additive link spans, and
   additive visible chunks using only parser-reconciled raw positions. Preserve
   all pre-existing scanner values exactly.
4. Run focused scanner and wiki/citation compatibility tests:

   ```bash
   python3 -m pytest tests/unit/test_markdown.py tests/unit/test_wiki_models.py tests/unit/test_citations.py -q
   python3 -m ruff check brainlib/markdown.py tests/unit/test_markdown.py
   ```

5. Fresh review must probe source-offset/line invariants, parser equivalence,
   image/code suppression, and failure behavior before slice acceptance.

## Slice B — Authenticated candidate-run core

**Owns:** `brainlib/search.py`, `brainlib/search_runs.py`,
`tests/unit/test_search.py`, `tests/unit/test_search_runs.py`

1. Write RED tests for legacy regular run compatibility, purpose fencing,
   canonical excluded logical paths (including a not-yet-live target), target
   and nonself live-edit staleness, self exclusion, zero candidates, one
   candidate per document, >300 late candidates, submatch validation,
   tampering/undrained/reordered pages, and locked verifier non-reentrancy.
2. Run the focused tests and observe failures.
3. Add HMAC-bound purpose/path metadata, canonical wiki operand binding,
   candidate record spooling/pageing, strict candidate result decoding, public
   purpose fences, and a no-lock internal verifier. Do not modify normal public
   result/proof/cursor types or their serialized payloads.
4. Run:

   ```bash
   python3 -m pytest tests/unit/test_search.py tests/unit/test_search_runs.py -q
   python3 -m ruff check brainlib/search.py brainlib/search_runs.py tests/unit/test_search.py tests/unit/test_search_runs.py
   ```

5. Fresh review must independently inspect retained-state authentication,
   spool/page hashing, stale semantics, and lock ownership before acceptance.

## Slice C — Graph candidate adapter, validation, and index

**Owns:** `brainlib/graph.py`, `tests/unit/test_graph.py`, declared graph
fixtures, and narrowly necessary graph fixture/helper support.

1. Write RED tests for public candidate pagination/proof construction; unsafe
   input mapping; scanner/model failures; ID/slug/display-name ambiguity;
   canonical non-ASCII paths and fragments; strict bad-link grammar; citation
   definition exclusion; reciprocal relationship kinds; first meaningful
   occurrences; staged-only mapping; and exact deterministic empty/nonempty
   index output.
2. Run `python3 -m pytest tests/unit/test_graph.py -q` and observe failures.
3. Implement public graph candidate adapters over Slice B, safe complete
   mapping loading, graph diagnostics, reciprocal/canonical link validation,
   first-occurrence matching using Slice A spans, and index rendering.
4. Run focused graph/scanner/search/citation tests plus Ruff. A fresh reviewer
   must confirm that no raw-regex visibility approximation, live-file merge, or
   normal-search contract regression exists.

## Slice D — Manifest-only transaction and recovery

**Owns:** `brainlib/wiki_transaction.py`, `tests/unit/test_wiki_transaction.py`,
declared transaction/removal fixtures, and narrowly necessary test helpers.

1. Write RED tests for manifest codec/path/staging safety, approvals, current
   retained proof coverage, stale/tampered proof refusal, adoption repair,
   corpus staleness, all journal interruption/recovery points, invalid journal
   refusal, lock ordering/no re-entry, and zero live writes on every preflight
   failure.
2. Run `python3 -m pytest tests/unit/test_wiki_transaction.py -q` and observe
   failures.
3. Implement strict manifest codec/loading, staging writer, same-run checks,
   locked preflight sequence, descriptor-pinned journal/publish/recovery, and
   read-only pending-state validation. Use only internal locked verifier and
   recovery seams when locks are already held.
4. Run:

   ```bash
   python3 -m pytest tests/unit/test_wiki_transaction.py tests/unit/test_graph.py tests/unit/test_citations.py -q
   python3 -m ruff check brainlib/wiki_transaction.py tests/unit/test_wiki_transaction.py
   ```

5. Fresh review must attempt stage/journal path escapes, inode swaps, retained
   proof forgery, crash recovery, and lock re-entry before acceptance.

## Final Task 4 gate

After all slices are accepted, run the complete relevant suite, lint the
changed modules, inspect `git diff --check`, verify the working tree, and
verify `git rev-list --count origin/main..HEAD` remains `1`. Then arrange a
fresh cross-component review covering graph/postimage consistency, citation
rewrite repair, candidate-proof binding, journal recovery, and no direct wiki
publication path.
