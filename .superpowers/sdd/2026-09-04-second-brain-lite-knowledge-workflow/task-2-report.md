# Task 2 implementation report

## Scope and design

Implemented the Task 2 safe search and retained-run contracts in `brainlib/search.py` and `brainlib/search_runs.py`, with the existing `brain` entry point and `CommandResult` CLI envelope. `SearchPassName`, `SearchPageRecord`, and `SearchPassRecord` come from Task 1's `brainlib.evidence`. Corpus revision and active operands use `compute_corpus_revision`, `LedgerStore.load_all()` and `active_representations()`. Production never reads ledger JSON directly and introduces no alternate source/cursor model.

Search uses Python `subprocess.Popen(argv, shell=False)`, exact literal pattern-file argument arrays, 256-path / 131,072-byte operand batches, sequential children, streamed NUL filenames and bounded JSON events, and private temporary stderr/pattern files. Filename discovery finishes every operand batch before context work. Candidates and normalized results are streamed to disk; pages use byte-range indexes and canonical payload hashes. Freshness probes bind exactly their requested source IDs; wiki search admits only direct nonsymlink Markdown files and binds source revision plus wiki content identity.

Run directories/files are created privately, opened with descriptor-relative no-follow checks, and metadata is published atomically with fsync and authenticated using the run's 256-bit secret. Cursors carry only version, run ID, next page, expiry, and MAC. Source locking wraps starts/resumes/proof verification/cleanup; wiki operations acquire source then wiki locks. Revision/operand/repository changes and manifest tampering block evidence. A completed pass requires all contiguous returned pages, cursor chaining, matching immutable bindings and checksums, and a complete final page; retained proof verification additionally checks live state and highest page served.

The 1 GiB default spool budget counts candidates, results, and indexes. Exceeding it leaves an explicit incomplete run with `search_spool_limit`, no cursor, and CLI code 1. Zero hits produce one authenticated complete empty page. Expired undrained runs retain an explicit expiry diagnostic for a further retention window, including when resume first discovers expiry. Cleanup skips unknown directories, symlinks, unexpected children, and unsafe files.

## RED / GREEN evidence

1. Before production search existed, the initial focused suite reported **8 failed, 52 errors**. The CLI assertions exposed the missing search JSON contract; library setup assertions explicitly reported the absent search module. These setup errors were not treated as causal boundary coverage.
2. With the request dataclass shape present but no validation, **18 request-boundary tests failed with `DID NOT RAISE ValueError`**: empty/duplicate/control-character terms, invalid scopes/passes, freshness conflicts, invalid integer bounds and booleans. Implemented validation, exact argv construction and byte batching: **20 passed**.
3. Initial source/search-run implementation: **51 passed**, covering complete filename batching, unusual UTF-8/newline/leading-dash names, missing/directory/symlink/traversal operands, wiki scoping, freshness scoping, cursor tampering/staleness/expiry, page-chain rejection, manifest tampering, private files, cleanup and spool gaps. Initial CLI checks: **8 passed**.
4. Further hostile-stream and retention tests produced **4 causal failures / 27 passes**: UTF-16 JSON was accepted; binary-skipped content and malformed summaries could finish as complete; a run first expired through resume was immediately removable by cleanup. Corrected strict UTF-8 event parsing, binary/summary validation, and retention marking.
5. The first completed 10,000-source memory check failed at **67,768,268 bytes**, above the required **33,554,432-byte** ceiling. Three allocation-profile runs identified Python 3.14 `Path` comparison caches and retained operand lists. The first correction still failed at **41,162,067 bytes**. String-key sorting in both full operands and batch hits, releasing discovery operands after spooling, and identity-only revalidation brought the test below the ceiling. All **40 filename batches and 40 context batches** are now asserted; the returned page remains bounded to 25 records.
6. Four forged-completion term cases failed with `DID NOT RAISE ValueError`; completion now reuses request validation to reject empty, whitespace, duplicate and control-character terms.
7. Expanded source fixture and CLI checks: **13 passed**, including real local `rg`, all continuations, unusual/late evidence, human continuation/completion, missing-rg JSON errors, spool code 1, obsolete positional-query code 2, and Git-ignore proof.
8. Focused search suite: **105 passed in 30.76s**. Existing CLI/evidence/wiki-model/template compatibility selection: **70 passed in 2.48s**. Repository regression suite before the final parser-fallback addition: **1,424 passed in 142.97s**.
9. Deeply nested JSON initially passed against the installed C decoder, even at Python's normal recursion limit. Exercising the real stdlib recursive fallback decoder exposed **3 causal `RecursionError` failures** for rg events, cursors and metadata. The library now maps those failures to canonical execution/cursor/stale errors. Targeted GREEN: **3 passed, 92 deselected in 0.22s**.

## Verification commands and counts

Commands were invoked with argument arrays through the local execution tool. No shell, package installation, public network request, or optional converter was invoked by Task 2. Node's child-process facility was only tool orchestration; no Node production/test code or dependency was added. Existing regression tests use their committed fakes and local loopback fixtures.

- Focused command: `python3 -m pytest tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py -q --tb=short`.
- Compatibility command: `python3 -m pytest tests/unit/test_cli.py tests/unit/test_evidence.py tests/unit/test_wiki_models.py tests/integration/test_template_contract.py -q --tb=short`.
- Full regression command: `python3 -m pytest -q --tb=short`.
- CLI smoke: `python3 brain --json search --scope wiki --term Alpha` returned exit 0, canonical JSON, zero candidates/matches, `complete=true`, and no cursor.
- `python3 -m ruff check` on the Task 2 code/test surfaces passed. Existing local Ruff formatted the same surfaces; no formatter was installed.
- `git diff --check` passed. `git rev-list --count 7dfb96e..HEAD` was **1** before amendment.
- Before the final full run: **22 pytest invocations**, including **3 allocation-profile invocations** and **2 orchestration attempts without captured completion**. The latter attempts received no verification credit; all relevant tests were rerun with captured exit codes. The final full run is invocation **23**.
- Concrete subprocess assertions: 600 operands require **3 filename children followed by 1 context child** to reach operand 599; 10,000 matching operands require **40 filename + 40 context children**. Empty scopes invoke **0 rg children**. Child overlap is rejected by the recorder, and every temporary pattern file is checked for removal.

## Fixture and compatibility decisions

The `search/valid` fixture contains three real active source records and extracted outputs: `early.txt`, `unusual/résumé (final).md`, and `zzz-late.txt`. Their hashes and byte sizes are deterministic. Minimum raw files, wiki directory markers/index, and a ledger summary were added because the existing `scenario_repo` fixture restores every advertised raw/derived mtime and requires a complete wiki overlay. The parent explicitly authorized that support surface.

Under the parent's recorded Ruling 6 authorization, only the obsolete `search renewable` milestone-4-unavailable tuple was removed from `tests/unit/test_cli.py`. Its replacement invalid-arguments/code-2 assertion lives in the new search integration suite. Other legacy parser behavior remains covered and unchanged.

## Concerns and handoff requirements

No known failing Task 2 contract remains. Later evidence handoff code must call `verify_search_run_proof` after `complete_search_run`; the pure page aggregation helper has no repository/time arguments and cannot itself establish current expiry or retained-state authenticity. Freshness/wiki runs must also be drained and verified before sufficiency judgments, and only named source research runs become `SearchPassRecord` entries. Cleanup after evidence handoff belongs to that later workflow.

The verification environment was Python 3.14 with installed local `rg`; recursive-decoder tests additionally exercise the standard-library fallback. No other Python interpreter or OS was available for a runtime matrix. Filesystem containment uses the repository's existing pinned-directory primitives and cooperative source/wiki locks.

The two early long-running tool attempts did not provide completion output and are explicitly excluded from GREEN claims. The later captured focused/full runs supersede them.

## Final checkpoint

Final full regression command completed with **`1427 passed in 142.50s (0:02:22)`**, exit 0 and empty stderr. This includes all **108 Task 2 search tests**, including the 10,000-source memory and 40+40 subprocess-count assertions, the final recursive-decoder cases, and the existing compatibility suite. Ruff format/check and the staged diff whitespace check passed. The exact 23 Task 2/authorized support files and this explicitly requested report form the checkpoint; the report is force-added individually because `.superpowers/` is otherwise ignored, matching the retained Task 1 report precedent.

The existing session commit was amended successfully. `git rev-list --count 7dfb96e..HEAD` returned **1**, and the implementation amendment left the worktree clean. A report-only amendment records this verification without creating another reachable session commit. The final agent handoff supplies the resulting HEAD (a tracked report cannot embed its own commit hash).

## Fix round 1 — bind returned evidence to authenticated retained pages

The fresh review identified a real gap in the original completion boundary: after a run was genuinely drained, a caller could alter a returned match, recompute the public page checksum, and receive a proof accepted by retained-state verification. The proof preserved aggregate counts and request metadata but discarded the ordered page/result identities. The starting checkpoint was `e5c2baec247f37a7c74d41d2e1d74c05cabdde5f`, with an empty worktree and exactly one commit in `origin/main..HEAD`.

The fix adds the required `SearchRunProof.page_index_sha256` field. Completion derives it from the exact ordered canonical page-index rows, including each page index, start/end byte offsets, record count, and SHA-256 of its ordered canonical match records. A shared private row-construction helper keeps proof creation and retained spooling on the existing codec. Match hashing is incremental; completion no longer joins a whole page payload into one additional allocation. Retained proof verification compares this field against the existing metadata-authenticated `page-index.jsonl` hash, after the existing repository, operand, expiry, manifest, and highest-page-served checks. No new retained file, metadata format, cursor model, CLI field, ledger access, lock order, dependency, or external service was introduced.

### Causal RED and GREEN

- Before production edits, `python3 -m pytest tests/unit/test_search_runs.py -q --tb=short -k 'rehashed_returned_pages or proof_page_index_binding or drain_one_logical'` exited 1 with **`11 failed, 1 passed, 39 deselected in 0.92s`**. Eight mutation cases failed specifically with **`DID NOT RAISE SearchRunBlocked`**: altered text, path, line number, record kind, swapped page payloads, reversed within-page results, omission compensated by duplication, and shifted page boundaries with the identical overall ordered results. All public checksums were recomputed and aggregate page/match counts preserved. The genuine control passed. Three additional canonical-binding tests failed because the new proof field was absent; these API-shape failures are separate from the eight causal security regressions.
- After the narrow fix, the same selection plus `or unserved_or_forged` exited 0 with **`13 passed, 38 deselected in 0.97s`**. Every mutation is rejected, retained files are byte-for-byte unchanged, and the genuine proof remains valid afterward. Zero-hit, single-page, and multi-page proofs match the actual retained index hash and round-trip through canonical JSON without losing the binding. The existing unserved-proof test now supplies the genuine retained index hash, preserving its original causal check that an undrained run is still rejected.

### Fix-round verification commands and counts

All commands used local argument arrays with `shell=False`; no public network, package installation, optional converter, shell command, subagent, or external service was used. The code-review skill informed verification of the finding against the implementation; TDD and verification-before-completion supplied the RED/GREEN and captured-output gates.

- **Five pytest invocations** this fix round: one causal RED, one targeted GREEN, one focused gate, one compatibility gate, and one full regression gate. Every invocation completed with captured exit status/stdout/stderr; there were no uncaptured or aborted test attempts.
- Focused: `python3 -m pytest tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py -q --tb=short` — exit 0, **`119 passed in 35.47s`**, empty stderr. This includes all 11 new cases and the existing containment, full-batch, cursor, spool, cleanup, CLI, and bounded-memory checks.
- Compatibility: `python3 -m pytest tests/unit/test_cli.py tests/unit/test_evidence.py tests/unit/test_wiki_models.py tests/integration/test_template_contract.py -q --tb=short` — exit 0, **`70 passed in 2.16s`**, empty stderr.
- Full regression: `python3 -m pytest -q --tb=short` — exit 0, **`1438 passed in 154.88s (0:02:34)`**, empty stderr.
- Lint: `python3 -m ruff check brainlib/search.py brainlib/search_runs.py brainlib/cli.py tests/helpers_knowledge.py tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py` — exit 0, **`All checks passed!`**, empty stderr.
- `git diff --check` passed after the implementation. The final staged whitespace check and amend/count/status outputs are supplied in the handoff checkpoint.
- Search subprocess counts are unchanged: the 600-operand test still asserts **3 filename + 1 context** children; the 10,000-match test still asserts **40 filename + 40 context** children, a 25-record returned page, and peak memory below **33,554,432 bytes**. Proof binding adds no `rg` invocation.

### Compatibility and concerns

Only `brainlib/search.py`, `brainlib/search_runs.py`, `tests/unit/test_search_runs.py`, and this report changed in fix round 1. Aggregate-only `SearchRunProof` constructors now require the new digest; the sole existing direct constructor was updated, and canonical JSON round-trip coverage was added. The existing Task 1 `SearchPageRecord`/`SearchPassRecord`, CLI envelopes, and retained run formats are unchanged.

No known failing fix-round contract remains. Completion and pass-record construction remain pure helpers, so evidence consumers must still verify the proof from the same pages with `verify_search_run_proof` before treating them as accepted evidence. This fix binds the exact payload/page identities at that existing authenticated boundary; it does not make an unverified public checksum authoritative. Runtime-matrix limitations and later workflow handoff responsibilities noted above remain unchanged. The final handoff reports the amended HEAD because this tracked report cannot embed its own commit hash.
