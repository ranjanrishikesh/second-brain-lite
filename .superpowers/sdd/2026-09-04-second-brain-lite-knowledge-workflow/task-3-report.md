# Task 3 report — immutable claim-level citations

Task 3 is implemented and verified. Starting content HEAD was
`4fbe41d493e2acb9415baf8b53aa60583cd6f4e9`. The implementation-only amendment is
`7e99ab1f33d677dd937699e202884db84e9fee94`; this report is included through a
report-only amendment of the same session commit. The final agent handoff supplies
the resulting HEAD because a tracked report cannot contain its own commit hash.

## Scope and fixture support

The implementation changes only `brainlib/citations.py`, the citation schema,
`tests/unit/test_citations.py`, Task 3 additions in `tests/helpers_knowledge.py`,
and the six declared citation scenario directories. No Task 2 search file or
shared scenario-loader contract changed. Before the report, the scoped diff was
50 files: four code/schema/helper/test files and 46 citation fixture files.

The brief enumerated citation page fixtures but `scenario_repo` requires complete
committed overlays, and the scenario README requires consistent raw, extracted,
ledger, summary, and wiki content. The parent explicitly authorized the minimal
support files inside those same six scenario directories and recorded the ruling.
The overlays therefore contain actual source bytes, matching extracted bytes and
anchors, retained versions/derivations, ledger shards and summaries, and wiki
collection markers. This adds no new fixture surface. The adoption fixture also
retains the requested `prior-original.txt` and another historical version of the
same source.

## Public behavior

- `Citation` uses the existing canonical `Anchor`; `MarkdownDocuments` is a
  mapping of absolute logical repository `Path` keys to authoritative staged text.
  `read_markdown_documents` normalizes path iterables into that view. Unpublished
  documents validate from their supplied text, and parent traversal in staged keys
  is rejected.
- `parse_citation_definitions` consumes the shared Markdown scanner to exclude
  frontmatter and fenced/indented code. Identity fields and evidence links must
  occur exactly once. Full lowercase source/version/derivation identities and
  canonical anchors are required. Validation distinguishes missing, duplicate,
  out-of-section, unused, and malformed definitions; references are local to their
  document. Inline code does not turn examples into claim references, escaped
  backticks do not hide claims, and code spans cannot cross block boundaries.
- `encode_markdown_path` produces minimal relative POSIX paths with uppercase
  UTF-8 percent escapes. `resolve_markdown_path` decodes exactly once, rejects
  ambiguous/nonminimal destinations and traversal, proves repository containment,
  and requires the exact canonical spelling. Original fragments are forbidden;
  extracted fragments must equal the serialized cited anchor.
- `validate_citations` resolves all three IDs through
  `LedgerStore.find_representation`, checks anchor membership, and demands the
  exact original and derivation targets. Existing same-byte substitutes remain
  target mismatches. Retained namespace ownership and embedded derivation identity
  are checked, including approved `_web` snapshots and archived `_versions`
  originals. Stale pre-adoption live links report `citation_original_stale` as well
  as the underlying target mismatch.
- Every evidence observation uses `ChecksumCache.observe` with its namespace and
  logical path. Normal validation compares recorded size/mtime without hashing;
  full validation also proves both checksums. A supplied cache is never reset.
  Repeated claim references each recheck identity, while hashes are reused for
  unchanged files. Even when canonical path resolution detects a temporary unsafe
  alias, the authoritative logical cache key is observed so its failure remains
  poisoned after the original directory is restored.
- `rewrite_historical_original_links` requires a sorted unique tuple of exact
  source/version rewrites and edits only the matching actual original-link span.
  Link-shaped text inside identity fields is preserved. The ledger-driven
  `canonicalize_citation_destinations` repairs both actual destinations, is
  idempotent, preserves unresolved identities for precise validation errors, and
  supplies the lost-report durability fallback.
- Reports use the existing `ValidationReport` with `checks=("citations",)`, exact
  error codes, document-qualified line/label details, and a corpus revision
  calculated once from the loaded records.

The schema explains adjacent claim markers, the final Sources section, canonical
paths and identities, all emitted codes, namespace-aware cache ownership, exact
sync-event draining/acknowledgment, and durable canonicalization. It explicitly
states that deterministic citation validity does not itself determine which prose
is factual or whether a passage semantically supports the claim.

## Causal RED evidence

The initial citation test failed because the module did not exist. A temporary
explicit availability test produced `1 failed in 0.87s`; it was removed once the
public behavior tests replaced that scaffolding. The initial attempted full
missing-module run and its setup-error run are not GREEN evidence.

Additional behavior tests were written and observed failing before each fix:

- `python3 -m pytest tests/unit/test_citations.py -q -k 'escaped_backticks or link_shaped_identity or namespace_owner'`
  — `3 failed, 84 deselected in 0.33s`: escaped backticks hid claims, rewrite matched
  identity text, and same-byte evidence under another namespace owner was accepted.
- `python3 -m pytest tests/unit/test_citations.py -q -k 'unapproved_web or extracted_namespace or invalid_definition or web_snapshot'`
  — `3 failed, 17 passed, 71 deselected in 1.57s`: later subordinate headings,
  a local source moved into `_web`, and a wrong embedded derivation ID were accepted;
  the genuine approved-web control passed.
- `python3 -m pytest tests/unit/test_citations.py -q -k repeated_claim_references`
  — `1 failed, 91 deselected in 0.24s`: two claim references observed each evidence
  file only once instead of revalidating each reference.
- `python3 -m pytest tests/unit/test_citations.py -q -k 'inline_code_does_not_cross or unsafe_namespace_observation'`
  — `3 failed, 91 deselected in 0.39s`: unmatched ticks crossed paragraph/heading
  boundaries, and a temporarily unsafe directory escaped transaction poisoning
  after restoration.
- `python3 -m pytest tests/unit/test_citations.py -q -k staged_document_key`
  — `1 failed, 94 deselected in 0.36s`: a staged logical key containing parent
  traversal was accepted.

All these regressions pass in the final focused and full runs. The TDD skill
supplied the RED/GREEN process, and verification-before-completion supplied the
captured-output completion gate.

## Final verification

- `python3 -m pytest tests/unit/test_citations.py -v` — exit 0,
  **95 passed in 9.80s**. This includes staged authority, strict path rejection and
  UTF-8/control-character round trips, code handling, precise identity/anchor/file
  diagnostics, normal/full checksum behavior, real adoption, namespace ownership,
  and canonical historical rewrites.
- `python3 -m pytest -q` — final run exit 0,
  **1533 passed in 215.99s (0:03:35)**. Earlier full runs also passed with 1525 and
  1532 tests while additional regression cases were being added; the 1533-test run
  supersedes those for the final checkpoint.
- `python3 -m pytest tests/unit/test_citations.py::test_real_adoption_rewrite_changes_only_the_exact_version -q`
  — exit 0, **1 passed in 0.24s**, after local helper formatting.
- `python3 -m ruff check brainlib/citations.py tests/unit/test_citations.py` —
  exit 0, **All checks passed!** The already-installed Ruff formatter was used;
  no package was installed.
- `git diff --check`, `git diff --cached --check`, and `git diff --quiet` passed
  before the implementation amendment. Explicit checks showed no staged Task 2
  search changes.
- `git commit --amend --no-edit` amended the existing session commit;
  `git rev-list --count origin/main..HEAD` returned **1**, and `git status --short`
  was empty. This requested report is force-added individually, matching the
  existing Task 1/2 report convention, and amended into the same commit.

Verification used the available local Python 3.14.2 environment and existing test
fixtures. No public network, package installation, optional real converter, or
external service was used. No known failing Task 3 contract remains. Combined
source/wiki validation, graph transactions, and CLI integration remain with their
later plan owners; they must pass the staged view and the caller-owned cache
through these APIs rather than adding a separate citation or checksum path.

## Fix round 1 — reviewed semantic boundaries and cache poisoning

Starting checkpoint: `add73bba8e02a0e75cfdf2871833ba002eb5b60c`. Its
1,533 passing tests did not cover four independently reviewed false-successes.
This section supersedes the initial final-verification checkpoint above.

The receiving-code-review skill required reproducing the findings before repair;
the TDD and verification-before-completion skills supplied the causal RED/GREEN
and fresh completion gates. The fix remained within the authorized citation,
shared scanner/cache, schema, and test files. No Task 2 search file or fixture
loader contract changed.

### Repairs and compatibility scope

- Inline-code masking now belongs to the existing Markdown scanner and is
  limited to semantic blocks. Frontmatter, fenced/indented code, headings,
  thematic breaks, setext underlines, and container starts cannot pair an
  unrelated backtick with later prose. Valid multiline inline code still masks
  its examples. The competing citation-wide mask was removed.
- Only actual definition positions are excluded from claim references. An
  additive `MarkdownScan.citation_definitions` field preserves those positions;
  colon-followed prose references remain visible, and marker/link escaping uses
  odd/even backslash parity. Citation parsing consumes this shared semantic
  evidence instead of constructing a second synthetic Markdown document.
- The scanner recognizes one-to-three-space indented ATX headings and setext
  headings, preserving original line positions. Later headings therefore make
  Sources non-final, while headings in excluded code/frontmatter stay excluded.
  A shared scanner repair was necessary because the previous scanner discarded
  the heading/marker information before citation validation could use it.
- The narrowly scoped public `ChecksumCache.poison(namespace, logical_path)`
  API retains a caller-detected authority failure for the current transaction.
  Citation namespace-owner and embedded derivation-owner mismatches poison their
  authoritative logical key before returning a target mismatch. No private cache
  state is accessed, supplied caches are never reset, unrelated keys remain
  usable, and only a genuinely new transaction clears the poison. Existing
  observation/hash behavior is unchanged.

The regressions cover normal/full modes, real source and retained-version ledger
records, valid A → wrong-owner B → valid A with a supplied begun cache, extracted
owner restoration, transaction reset, and unaffected keys. Valid definitions,
citations, code examples, escaped markers, and genuine multiline code are positive
controls. Direct scanner compatibility tests cover original link/marker positions
and definition positions as well as the expanded heading forms.

### Causal RED and GREEN

- Before production edits:
  `python3 -m pytest tests/unit/test_citations.py -q --tb=short -k review`
  — exit 1, **34 failed, 8 passed, 95 deselected in 2.76s**. Fourteen
  excluded-block masking cases, six colon/parity cases, ten later-heading cases,
  and four shared-cache owner cases failed their expected diagnostic assertions.
  The eight passing cases were existing-safe excluded-block and valid controls.
- After the minimal repairs, the same command returned exit 0,
  **42 passed, 95 deselected in 2.39s**.
- Additional scanner and extracted-owner compatibility controls were then added.
  One intermediate test-editing mistake misplaced the tail of the raw-owner test
  under the extracted-owner test, causing two `NameError` failures. Correcting
  that test placement required no production change; it is not causal RED
  evidence. The final focused run below includes every added control.

### Final fix-round verification

- `python3 -m pytest tests/unit/test_citations.py tests/unit/test_markdown.py -q --tb=short`
  — exit 0, **151 passed in 7.52s** (139 citation, 12 scanner).
- `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_wiki_models.py tests/unit/test_frontmatter.py tests/unit/test_validation.py tests/integration/test_validate_ledger.py -q --tb=short`
  — exit 0, **151 passed in 16.60s**.
- `python3 -m pytest -q --tb=short` — exit 0,
  **1586 passed in 182.63s (0:03:02)**.
- `python3 -m ruff check brainlib/citations.py brainlib/markdown.py brainlib/validation.py tests/unit/test_citations.py tests/unit/test_markdown.py`
  — exit 0, **All checks passed!**
- `git diff --check` and the staged diff check passed. Only the six scoped
  implementation/schema/test files and this tracked report enter the amendment.
  The delivery handoff records the amended HEAD, one-commit count, and clean
  status after `git commit --amend --no-edit`; a report cannot embed the hash of
  the commit containing itself.

No public network, package installation, optional converter, or external service
was used. All four reviewed findings have causal regressions and passing controls.

## Fix round 2 — list paragraph spans and original link tokens

Starting checkpoint: `2fd87f6e2b3502392d3a02d6a4a75eb093b8ae72`. Fresh
re-review found two shared-scanner regressions despite the prior 1,586 passing
tests. This round changes only `brainlib/markdown.py`, its scanner/citation unit
tests, and this report. It preserves the citation grammar, path/cache authority,
and all fix-round-1 tests. No Task 2 or fixture files changed.

The receiving-code-review and systematic-debugging skills guided reproduction and
root-cause checks: the list opener was scanned separately from its continuation,
and link values were read from the code-masked buffer. TDD required observing both
failures before production edits; verification-before-completion requires the
fresh focused, compatibility, and full-suite gates below.

### Narrow repair and controls

- A list opener now remains in its paragraph until a semantic boundary. Its
  continuation indentation is removed only in the boundary-detection view, so
  bullet, ordered, indented-list, and lazy paragraph continuations can share a
  valid multiline inline-code span. Original source lines remain authoritative
  for token bytes and line numbers. New items, nested containers, quotes, blank
  lines, headings, thematic/setext boundaries, fences, and excluded indented code
  still flush the paragraph. Fence state retains the container indentation needed
  to identify its real closing line.
- Link recognition and suppression still use the semantic masked view. The
  resulting match spans now select label/destination values from the original
  source line. Literal backticks survive unchanged in either value, while links
  wholly inside inline code remain suppressed.
- Scanner controls assert both that example markers/links disappear and that a
  real marker/link on the continuation remains visible at its original line.
  Citation controls combine list examples with a genuine retained citation in
  normal and full modes; distinct-block malformed ticks still produce exactly
  `citation_definition_missing`. Existing frontmatter, excluded-code, heading,
  escape-parity, definition, path, checksum, and poisoning tests remain in place.

### Causal RED and targeted GREEN

- Before production edits:
  `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_citations.py -q --tb=short -k review2`
  — exit 1, **14 failed, 31 passed, 151 deselected in 1.32s**. Seven scanner
  list cases exposed examples or dropped real continuation tokens, three link
  cases returned masked bytes, and four normal/full citation cases falsely
  reported a missing definition for code examples. The passing cases establish
  existing-safe controls. An earlier test assembly attempt misplaced the tail of
  an existing test and produced 14 additional `NameError` failures; that placement
  was corrected before this causal RED, without production edits.
- Link repair in isolation:
  `python3 -m pytest tests/unit/test_markdown.py -q --tb=short -k 'review2 and (tokens or token)'`
  — exit 0, **6 passed, 29 deselected in 0.10s**.
- After the list repair, the full targeted RED command returned exit 0,
  **45 passed, 151 deselected in 1.26s**.

### Final fix-round-2 verification

- `python3 -m pytest tests/unit/test_citations.py tests/unit/test_markdown.py -q --tb=short`
  — exit 0, **196 passed in 8.55s** (161 citation, 35 scanner).
- `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_wiki_models.py tests/unit/test_frontmatter.py tests/unit/test_validation.py tests/integration/test_validate_ledger.py -q --tb=short`
  — exit 0, **174 passed in 8.58s**.
- `python3 -m ruff check brainlib/markdown.py tests/unit/test_citations.py tests/unit/test_markdown.py`
  — exit 0, **All checks passed!** The existing formatter was used only for the
  three edited Python files; no package was installed.
- `python3 -m pytest -q --tb=short` — exit 0,
  **1631 passed in 169.50s (0:02:49)**.
- `git diff --check`, `git diff --cached --check`, and `git diff --quiet`
  passed before the amendment. The exact four-file staged scope was inspected;
  no prior tests or unrelated changes were removed. The sole session commit is
  amended with `git commit --amend --no-edit`; the delivery handoff records the
  resulting HEAD, `origin/main..HEAD` count of one, and clean worktree status.

Both re-review findings have causal regressions and passing controls. No public
network, package installation, optional converter, or external service was used.

## Fix round 3 — ending a list-scoped fence at its container boundary

Starting checkpoint: `bd6053ab9f535598a6f2a65be13c220bdd9e20d6`. Independent
re-review established that a list-scoped fence retained global fence state after
its container ended. The smallest causal scanner test confirmed the specified
single hypothesis before any production edits: the first outside line was lost.
The review/debugging skills supplied that root-cause gate, TDD supplied the
observed RED/GREEN cycle, and verification-before-completion supplied the fresh
regression gates.

The production change is exactly nine added lines in `brainlib/markdown.py`.
Before existing fence handling, a nonblank line lacking the stored nonzero list
indentation clears only the fence state. The same original line then flows
through normal Markdown classification; it is not skipped or consumed as a
closing fence. Blank/container-indented lines retain the fence, and top-level
fences still require an explicit matching close. No inline-span, link-token,
heading, marker, cache, citation-production, or fixture code changed.

Tests cover first outside prose, sibling bullet and ordered items,
nested-to-parent siblings, and blockquote transitions with both fence markers.
Scanner checks assert original line numbers and both real links and markers;
normal/full citation checks demand exactly `citation_definition_missing` with
the original line/label details. Positive controls cover properly closed list
and nested-list fences, explicit and longer matching closes, malformed closing
markers, retained blank lines, ordinary top-level fences, and list multiline
inline-code behavior. The normal/full controls retain a genuine valid citation.

### Causal RED and GREEN

- `python3 -m pytest tests/unit/test_markdown.py::test_review3_list_fence_reprocesses_first_outside_line -q --tb=short`
  — exit 1, **1 failed in 0.12s**: the expected outside marker at line 4 was
  absent. This was the smallest pre-production hypothesis test.
- `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_citations.py -q --tb=short -k review3`
  — before production edits, exit 1, **29 failed, 19 passed, 196 deselected in
  1.98s**. Nine scanner and twenty normal/full citation transition cases failed
  for absent tokens/diagnostics; nineteen exclusion and genuine-citation controls
  passed. After the single state-transition change, the same command returned
  exit 0, **48 passed, 196 deselected in 1.76s**.

### Final fix-round-3 verification

- `python3 -m pytest tests/unit/test_citations.py tests/unit/test_markdown.py -q --tb=short`
  — exit 0, **244 passed in 14.75s** (193 citation, 51 scanner).
- `python3 -m pytest tests/unit/test_markdown.py tests/unit/test_wiki_models.py tests/unit/test_frontmatter.py tests/unit/test_validation.py tests/integration/test_validate_ledger.py -q --tb=short`
  — exit 0, **190 passed in 9.42s**.
- `python3 -m ruff check brainlib/markdown.py tests/unit/test_citations.py tests/unit/test_markdown.py`
  — exit 0, **All checks passed!** The existing formatter changed only the new
  scanner-test formatting; no package was installed.
- `python3 -m pytest -q --tb=short` — exit 0,
  **1679 passed in 179.37s (0:02:59)**.
- `git diff --check`, `git diff --cached --check`, and `git diff --quiet`
  passed before the amendment. The staged diff contains only the scanner guard,
  added scanner/citation tests, and this report. The sole session commit is
  amended with `git commit --amend --no-edit`; the handoff records the resulting
  HEAD, one-commit count above `origin/main`, and clean worktree status.

The container-fence finding has causal RED/GREEN evidence and passing controls;
all prior regressions remain in the full suite. No public network, package
installation, optional converter, or external service was used.
