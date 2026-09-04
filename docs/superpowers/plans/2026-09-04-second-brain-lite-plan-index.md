# Second Brain Lite Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the approved Second Brain Lite design through five ordered, independently testable plans without contract drift, hidden corpus gaps, or extra session commits.

**Architecture:** Five plans build one repository in dependency order: foundation contracts, source reconciliation, extraction/web capture, knowledge/search/graph mechanics, and cross-client agent workflows. Each interface has one owning plan; later plans consume it verbatim. Focused tests gate every task, while the final combined gate runs the whole suite and full integrity validation once.

**Tech Stack:** Python 3.11+, Python standard library, pytest, ripgrep (`rg`), Markdown, TOML, JSON/JSON Schema, allowlisted optional document converters, Git, Codex project skills/agents, Claude Code project skills/subagents.

**Spec:** [`docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`](../specs/2026-09-04-second-brain-lite-design.md)

## Global Constraints

- Execute the plans in the listed order. A later plan must adapt to an earlier plan's published interface; it must not create a competing ledger, registry, source, validation, or citation model.
- Use `python3` and require Python 3.11+. Runtime coordination remains standard-library-first; optional extraction libraries and executables stay capability-detected and approval-gated.
- Originals, extracted representations, ledger records/summary, and wiki content are ordinary Git-tracked files. The template uses neither Git LFS nor a database, vector index, daemon, or hosted service.
- Generic discovery excludes sentinels, temporary files, escaping symlinks, `sources/raw/_versions/**`, and `sources/raw/_web/**`; dedicated provenance commands alone register historical and web bytes.
- Deterministic code owns inventory, hashes, state, extraction execution, search execution, and validation. Agents own terms, evidence judgment, synthesis, page qualification, relationship meaning, and approval conversations.
- Public-web access requires a new explicit user approval for the stated research event. Every used source becomes an immutable local version and active extraction before three renewed source passes and synthesis.
- Public-web capture accepts only vetted public HTTP(S) endpoints, applies the same policy to every redirect, and verifies the connected peer against the vetted resolution.
- Search and link discovery use bounded resumable pages, not silent top-N truncation. Every logical run must finish against one corpus/graph revision before its evidence can support synthesis or publication.
- Run normal validation during workflows and `./brain --json validate --full` before completed handoff or PR readiness.
- Never auto-commit or infer that a conversation ended. When executing through this index, skip every subplan's standalone optional commit block; this index owns the sole possible final commit for any selected prefix. Make at most one logical commit at the final requested boundary, and only when the user or host workflow asks for it.

---

## Ordered plan set

| Order | Plan | Owns | Starts when | Completion gate |
|---:|---|---|---|---|
| 1 | [`Core foundation`](2026-09-04-second-brain-lite-core-foundation.md) | template layout, canonical data/diagnostic contracts, CLI envelope, structural validation | approved spec is present | focused foundation tests pass |
| 2 | [`Source ledger and synchronization`](2026-09-04-second-brain-lite-source-ledger-sync.md) | extractor registry contract, inventory, sharded ledger, source versions, locking, adoption, sync/status/full source validation | Plan 1 passes | unchanged sync is fast and all source states are honest |
| 3 | [`Extractors and web evidence`](2026-09-04-second-brain-lite-extractors-web.md) | deterministic/agent extraction, processor integration, optional converters, approval-recorded web capture, immutable snapshots | Plan 2 passes | initialized supported corpus is searchable or has explicit gaps |
| 4 | [`Knowledge workflow`](2026-09-04-second-brain-lite-knowledge-workflow.md) | strict wiki/Q&A records, safe `rg`, evidence packets, immutable citations, link candidates, graph/index validation | Plan 3 passes | search/citation/graph tests and workflow contract pass |
| 5 | [`Agent integration and documentation`](2026-09-04-second-brain-lite-agent-integration-docs.md) | policies, shared skills/briefs, Codex/Claude adapters, README, workflow evaluations, final combined gate | Plan 4 passes | full test suite and full validation pass |

## Interface authority

When prose or an example appears ambiguous, use this ownership table instead of guessing:

| Contract | Sole owner | Required downstream vocabulary |
|---|---|---|
| diagnostics and validation reports | Plan 1, `brainlib/diagnostics.py` | `Diagnostic`, `ValidationIssue`, `ValidationReport(checks, issues, corpus_revision)` |
| source/content/derivation identity | Plan 1, `brainlib/contracts.py` | `src_<sha256>`, content SHA-256, content-bound `drv_<sha256>`, canonical anchors serialized as `<kind>:<value>`, immutable deterministic/agent method provenance |
| command protocol | Plan 1, `brainlib/cli.py` and `brainlib/output.py` | `./brain [--json] <command>` and `{command, ok, data, warnings, errors}` |
| converter allowlist | Plan 2, `config/extractors.toml` and `brainlib/registry.py` | one loaded registry; Plan 3 extends execution only |
| inventory and URL descriptors | Plan 2, `brainlib/inventory.py` | descriptors are local control metadata and never remote evidence |
| ledger and representations | Plan 2, `brainlib/ledger.py` | immutable mappings, active/historical `SourceRepresentation` lookup, atomic `LedgerStore.save()` |
| synchronization | Plan 2, `brainlib/sync.py` | `SyncReport`, pluggable `SourceProcessor`, durable retry inputs, no network |
| extraction and web capture | Plan 3, `brainlib/extractors/` and `brainlib/sources/web.py` | canonical derivation paths, staged immutable publication, method metadata, recorded approval/retrieval data, public-address-only transport |
| Markdown/evidence/search | Plan 4, `brainlib/frontmatter.py`, `evidence.py`, `markdown.py`, `search.py`, `search_runs.py` | exactly three completed logical pass records; bounded authenticated continuation pages; literal-pattern `rg` invocation; validated wiki fast-path packet |
| citations and graph | Plan 4, `brainlib/citations.py`, `graph.py`, `validators/wiki.py` | claim marker to exact source/version/derivation/anchor; complete paged candidate proof; relative reciprocal links |
| agent behavior | Plan 5, `.agents/skills/` and `docs/brain/agent-briefs/` | one canonical workflow layer; thin client adapters only |

## Acceptance-criteria coverage

| Spec criterion | Implemented and proven by |
|---:|---|
| 1. Empty template, no personal content, valid structure | Plan 1 Tasks 1/4; Plan 5 Tasks 6/8 |
| 2. Sentinels never ingested | Plan 1 exclusion predicate; Plan 2 inventory tests |
| 3. Populated approved extractor registry and recipes | Plan 2 registry task; Plan 3 capability/adapter tests |
| 4. Complete resumable initialization including agent handoff | Plan 2 reconciliation; Plan 3 processor/handoff/integration; Plan 5 `brain-initialize` |
| 5. Incremental sync without unchanged reconversion or retry loops | Plan 2 retry/scale tests; Plan 3 processor integration |
| 6. URL descriptors cause zero unapproved network access | Plan 2 inventory/sync tests; Plan 3 approval/capture tests |
| 7. Same canonical workflows in Codex and Claude | Plan 5 topology/adapter tests plus required normalized event logs from actual isolated runs in both clients |
| 8. Empty-wiki first question runs three passes and persists knowledge | Plan 4 workflow fixture; Plan 5 first-question evaluation |
| 9. Current sufficient wiki uses the fast path | Plan 4 validated wiki-evidence packet; Plan 5 current-wiki evaluation proves Q&A persistence without source passes |
| 10. Insufficient evidence asks before web access | Plan 3 capture boundary; Plan 5 web skill/evaluation |
| 11. Used web evidence is localized before renewed research | Plan 3 snapshot flow; Plan 4 packet rule; Plan 5 web evaluation |
| 12. Page and Q&A changes leave reciprocal relationships | Plan 4 graph/index tests and maintenance transaction tests |
| 13. Citations resolve exact original bytes and derivation | Plans 1/2 provenance contracts; Plan 3 immutable output; Plan 4 citation tests |
| 14. Every hybrid approval gate is respected | Plans 1/2 policy/adoption; Plan 3 web/install/allowlist boundaries; Plan 5 skills/evaluations |
| 15. Failures cannot appear as complete coverage | Plan 2 lifecycle/reporting; Plan 3 quality/failure fixtures; Plan 5 validation skill |
| 16. Deterministic tests, evaluations, and full validation pass | every plan's final gate; Plan 5 Task 8 and this index's combined gate |

---

### Task 1: Establish the execution baseline

**Files:**

- Read: `docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`
- Read: all five plans in the ordered plan set
- Verify: current repository status and target base `origin/main`

**Interfaces:**

- Consumes: approved design and plan set.
- Produces: one recorded execution scope and a clean understanding of pre-existing user changes; no repository mutation.

- [ ] **Step 1: Read the approved spec and all plan global constraints**

Do not begin implementation from this index alone. Read each selected plan completely before its first task.

- [ ] **Step 2: Inspect, but do not clean, the existing worktree**

Run:

```bash
git status --short
git diff --check
git diff --stat origin/main
```

Expected: all pre-existing edits are identified. Preserve unrelated user changes; never reset or overwrite them.

- [ ] **Step 3: Record the requested scope and commit boundary**

Record the selected ordered prefix and that every selected subplan skips its standalone optional commit block. Task 3 Step 5 of this index owns the sole possible final commit whether the selected scope ends after Plan 1 or Plan 5.

- [ ] **Step 4: Record a non-committing baseline checkpoint**

Run: `git status --short`

Expected: no implementation change was made by this task.

---

### Task 2: Execute and gate the five plans in dependency order

**Files:**

- Modify: only the exact files assigned by the currently executing subplan task
- Verify: prerequisite plan tests before proceeding to the next plan

**Interfaces:**

- Consumes: the plan order and interface-authority table above.
- Produces: one integrated Second Brain Lite implementation with no duplicate contract owner.

- [ ] **Step 1: Execute Core Foundation**

Follow every checkbox in [`2026-09-04-second-brain-lite-core-foundation.md`](2026-09-04-second-brain-lite-core-foundation.md). Run its final gate and skip its standalone optional commit.

- [ ] **Step 2: Execute Source Ledger and Synchronization**

Follow every checkbox in [`2026-09-04-second-brain-lite-source-ledger-sync.md`](2026-09-04-second-brain-lite-source-ledger-sync.md). Confirm it imports Plan 1 contracts rather than redefining them. Run its final gate and skip its standalone optional commit.

- [ ] **Step 3: Execute Extractors and Web Evidence**

Follow every checkbox in [`2026-09-04-second-brain-lite-extractors-web.md`](2026-09-04-second-brain-lite-extractors-web.md). Confirm `./brain init` and `./brain sync` now use the deterministic processor and that no network path is reachable without a current recorded approval. Run its final gate and skip its standalone optional commit.

- [ ] **Step 4: Execute the Knowledge Workflow**

Follow every checkbox in [`2026-09-04-second-brain-lite-knowledge-workflow.md`](2026-09-04-second-brain-lite-knowledge-workflow.md). Confirm it consumes the active/historical representation resolver and the one validation-report type. Run its final gate and skip its standalone optional commit.

- [ ] **Step 5: Execute Agent Integration and Documentation**

Follow every checkbox in [`2026-09-04-second-brain-lite-agent-integration-docs.md`](2026-09-04-second-brain-lite-agent-integration-docs.md). Run its final gate and skip its standalone optional commit; the index performs combined certification and owns the possible commit.

- [ ] **Step 6: Stop on interface drift instead of adding compatibility guesses**

If a later task cannot call an earlier public signature exactly, return to the owning plan/task, update that sole contract and all consumers, rerun both focused suites, and continue only after they agree. Do not add a second type with equivalent meaning.

---

### Task 3: Certify the combined implementation once

**Files:**

- Verify only: the complete repository

**Interfaces:**

- Consumes: all selected plan outputs.
- Produces: one evidence-backed completion report and, only at an authorized final boundary, at most one logical commit.

- [ ] **Step 1: Run the complete deterministic test suite**

Run: `python3 -m pytest -v`

Expected: PASS. Core contract tests do not skip; optional real-converter tests skip only with their explicit capability reason.

- [ ] **Step 2: Run status and full integrity validation**

Run:

```bash
./brain --json status
./brain --json validate --full
```

Expected: parseable command envelopes. Completion requires no unexplained source, extraction, integrity, citation, graph, instruction-topology, or adapter failure.

- [ ] **Step 3: Check the complete diff and template privacy**

Run:

```bash
git diff --check
git diff --stat origin/main
git status --short
```

Confirm no personal source, generated personal wiki, credential, transcript, or public-web artifact used only for implementation testing entered the distributed template.

- [ ] **Step 4: Review all sixteen acceptance criteria**

Use the coverage table above and cite the actual passing test/validation evidence for each criterion. A coverage gap is a blocker or an explicitly reported incomplete outcome, never an implicit pass.

- [ ] **Step 5: Respect the single final commit boundary**

Do not commit automatically. If the user or host explicitly requests the completed scope to be committed, review `git status --short`, stage only the already reviewed paths produced by the selected plan prefix, run `git diff --cached --check`, and create exactly one logical commit. Do not run any subplan commit block. Otherwise hand off the verified working tree without a commit.

## Plan self-review

- Spec coverage: the acceptance matrix maps every criterion in Section 23 to a concrete owning plan and test/evaluation gate; global safety, privacy, failure, and commit rules are carried across all milestones.
- Placeholder scan: this index delegates implementation only to exact linked plans and contains complete verification commands; it has no unnamed future module or unspecified test action.
- Type consistency: the interface-authority table establishes one owner and vocabulary for every cross-plan concept. Later plans must consume rather than reinterpret those contracts.
- Dependency consistency: the only permitted sequence is Foundation → Ledger/Sync → Extractors/Web → Knowledge → Agent Integration/Docs → combined certification.

## Execution handoff

After the plan set is approved, choose one execution mode for the requested scope:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`, dispatch a fresh implementation worker per task, and perform specification and code-quality review between tasks.
2. **Inline Execution:** use `superpowers:executing-plans`, execute in ordered batches, and stop at the plan checkpoints for review.

Use one mode for the plan set; individual subplans do not ask again.
