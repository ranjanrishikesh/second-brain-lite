# Second Brain Lite Agent Integration and Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the finished source, search, citation, and graph machinery reliably discoverable and usable from both Codex and Claude Code through one canonical set of policies, skills, agent briefs, evaluations, and user documentation.

**Architecture:** Canonical behavior lives in repository-owned Markdown: `AGENTS.md` routes, `.agents/skills/**/SKILL.md` orchestrates workflows, and `docs/brain/agent-briefs/` defines semantic roles. Claude skill directories are relative symlinks to the canonical skills; Codex TOML and Claude Markdown agents are thin adapters to the same briefs. Deterministic validators prevent client drift. Six generated synthetic-corpus scenarios plus normalized, schema-checked event logs from actual isolated Codex and Claude executions make cross-client judgment behavior a required acceptance gate rather than a prose-only claim.

**Tech Stack:** Python 3.11+, Python standard library, pytest, Markdown, TOML, JSON Schema documents, Codex project skills/agents, Claude Code project skills/subagents, Git symlinks.

**Spec:** [`docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`](../specs/2026-09-04-second-brain-lite-design.md)

## Global Constraints

- Complete the core-foundation, source-ledger-sync, extractors-web, and knowledge-workflow plans first; this plan packages their stable CLI contracts rather than reimplementing them.
- `AGENTS.md` is the concise canonical root instruction file; `CLAUDE.md` must remain a relative symlink to it.
- Canonical skills live only under `.agents/skills/`; every `.claude/skills/<name>` entry is a relative symlink to the corresponding canonical directory.
- Native Git symlink checkout is required. On Windows, document Developer Mode/`core.symlinks=true` or WSL rather than silently accepting copied link text.
- Canonical semantic role text lives only under `docs/brain/agent-briefs/`; tool-specific agent files are thin adapters and do not copy policy.
- Do not pin model names. Adapters inherit the user's available/default model.
- The coordinator runtime remains standard-library-first on Python 3.11+; pytest is a development dependency.
- Public-web access always requires explicit user approval for a stated research event. Every external source actually used must be captured, extracted, ledgered, and locally cited before synthesis, followed by exactly three renewed source passes.
- A logical search may span continuation pages. Start each freshness, wiki, discovery, expansion, verification, or link-candidate run once, follow only its opaque cursors until `complete: true`, and verify invariant run/revision/terms, the exact request/next-cursor chain, contiguous page indexes, candidate-manifest identity, each `result_sha256`, aggregate counts, and coverage gaps before producing a `SearchRunProof`, `SearchPassRecord`, or `LinkCandidateRunProof`. Fail closed on a stale, expired, tampered, or incomplete run. Continuation pages never count as additional logical passes.
- Repository-development questions are never persisted as brain Q&A records.
- The repository never auto-commits. Preserve the approved one-logical-commit-per-conversation-or-PR rule; task checkpoints below do not create commits.
- When this plan is executed through the plan index, it also skips any standalone commit; the index alone owns the one possible final commit for the selected prefix.
- Run `./brain --json validate --full` before a completed handoff or PR-readiness claim.

---

## Plan dependencies

| Required plan | Contracts consumed here |
|---|---|
| [`core-foundation`](2026-09-04-second-brain-lite-core-foundation.md) | `RepoPaths`, CLI result envelope, scoped validation, template layout |
| [`source-ledger-sync`](2026-09-04-second-brain-lite-source-ledger-sync.md) | `init`, `sync`, `status`, source states, corpus revision, coverage diagnostics |
| [`extractors-web`](2026-09-04-second-brain-lite-extractors-web.md) | immutable typed handoffs, `ExtractorSpec.agent_revision`, `Derivation.method`/`method_metadata`, `register-extraction`, rendered `snapshot-url`, approval-event claim fields, extraction validation |
| [`knowledge-workflow`](2026-09-04-second-brain-lite-knowledge-workflow.md) | resumable logical search and link-candidate runs, `EvidencePacket`, `WikiEvidencePacket`, wiki/Q&A contracts, citations, graph validation |

### Task 1: Freeze the shared operating policies and role briefs

**Files:**

- Create: `docs/brain/policies/citations.md`
- Create: `docs/brain/policies/wiki.md`
- Create: `docs/brain/policies/web.md`
- Create: `docs/brain/workflows/answer.md`
- Create: `docs/brain/workflows/web-research.md`
- Create: `docs/brain/workflows/wiki-maintenance.md`
- Create: `docs/brain/workflows/validate.md`
- Create: `docs/brain/agent-briefs/source-researcher.md`
- Create: `docs/brain/agent-briefs/source-ingester.md`
- Create: `docs/brain/agent-briefs/wiki-curator.md`
- Create: `docs/brain/agent-briefs/brain-auditor.md`
- Create: `tests/integration/test_operating_docs.py`

**Interfaces:**

- Consumes: CLI commands and JSON fields defined by the four prerequisite plans.
- Produces: canonical policy anchors and four role briefs referenced verbatim by every skill and client adapter.

All wiki instructions in this plan consume the knowledge-workflow plan's version-1 staging contract verbatim:

```text
./brain --json wiki apply --manifest .brain/wiki-staging/<run-id>/manifest.json
./brain --json wiki recover
```

The manifest is a JSON object with exactly `schema_version`, `expected_corpus_revision`, `change_intent`, `approval_event_id`, `citation_rewrites`, `link_candidate_runs`, and `changes`. `schema_version` is `1`; `expected_corpus_revision` is the 64-hex revision observed before staging; `change_intent` is one of `routine`, `rename`, `delete`, `merge`, `split`, `remove_claim`, `remove_relationship`, `resolve_contradiction`, `major_uncertain_rewrite`, or `ambiguous_rename`; and `approval_event_id` is a nonempty string for every destructive/uncertain intent and `null` otherwise. `citation_rewrites` is the sorted, unmodified array of `{source_id, content_sha256, raw_path}` objects obtained either from an adoption result or by consuming every exact `citation_rewrite` event in a digest-verified sync-result manifest; the bounded sync response array is display-only. `link_candidate_runs` is the sorted array containing one canonical `LinkCandidateRunProof` from a fully drained, current run for each explicit write or delete; it is `[]` for a reconciliation-only apply. A page write is `{operation: "write", path: "wiki/pages/<slug>.md", staging_path: ".brain/wiki-staging/<run-id>/files/wiki/pages/<slug>.md", sha256: "<64hex>"}`; a question write has the same fields with both `pages` segments replaced by `questions`. Its staging path mirrors the complete logical target beneath the run's `files/` directory. A delete is `{operation: "delete", path: "wiki/pages/<slug>.md"}` or the analogous direct `wiki/questions/<slug>.md` child. Paths are repository-relative POSIX paths. On every apply—even `changes: []` with empty rewrite and link-proof arrays—the CLI canonicalizes every parsed original and extracted citation destination through `LedgerStore.find_representation`; manifest rewrites are an optimization/assertion, not the sole recovery source. It then validates checksums/revision and complete link-run proofs, preflights the complete candidate tree, regenerates the index, journals, and publishes atomically. Missing, stale, or incomplete proofs block with `link_candidate_search_incomplete`.

- [ ] **Step 1: Write the failing operating-document contract tests**

```python
# tests/integration/test_operating_docs.py
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

POLICY_ASSERTIONS = {
    "docs/brain/policies/citations.md": (
        "## Immutable citation identity",
        "source_id",
        "content_sha256",
        "derivation_id",
        "## Sources",
    ),
    "docs/brain/policies/wiki.md": (
        "## Page qualification",
        "first meaningful occurrence",
        "reciprocal",
        "approval",
    ),
    "docs/brain/policies/web.md": (
        "## Approval boundary",
        "sources/raw/_web/",
        "unused search results",
        "three source passes",
    ),
}

BRIEF_WRITE_SCOPES = {
    "source-researcher.md": "Write scope: none",
    "source-ingester.md": "Write scope: source artifacts only",
    "wiki-curator.md": "Write scope: wiki artifacts only",
    "brain-auditor.md": "Write scope: none",
}


def test_policies_state_the_non_negotiable_rules() -> None:
    for relative, phrases in POLICY_ASSERTIONS.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in text, f"{relative} must contain {phrase!r}"


def test_agent_briefs_declare_non_overlapping_write_scopes() -> None:
    brief_dir = ROOT / "docs/brain/agent-briefs"
    for filename, declaration in BRIEF_WRITE_SCOPES.items():
        text = (brief_dir / filename).read_text(encoding="utf-8")
        assert declaration in text
        assert "## Required output" in text
        assert "## Stop conditions" in text


def test_answer_workflow_has_one_ordered_pipeline() -> None:
    text = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    headings = [
        "## 1. Classify the request",
        "## 2. Synchronize",
        "## 3. Search the wiki",
        "## 4. Judge sufficiency",
        "## 5. Search sources three times",
        "## 6. Synthesize and persist",
        "## 7. Validate and answer",
    ]
    offsets = [text.index(heading) for heading in headings]
    assert offsets == sorted(offsets)


def test_answer_workflow_uses_exact_cli_pipeline_and_applies_sync_rewrites_first() -> None:
    text = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    commands = (
        "./brain --json sync",
        "./brain --json wiki apply --manifest",
        "./brain --json search --scope sources --freshness",
        "./brain --json search --scope wiki",
        "./brain --json search --scope sources --pass discovery",
        "./brain --json search --scope sources --pass expansion",
        "./brain --json search --scope sources --pass verification",
    )
    for command in commands:
        assert command in text
    offsets = [text.index(command) for command in commands[:4]]
    assert offsets == sorted(offsets)
    assert text.index("citation_rewrites") < text.index("## 4. Judge sufficiency")
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert "./brain --json search --cursor \"$next_cursor\"" in text
    assert "WikiEvidencePacket" in text
    assert "one evolving topic Q&A record" in text


def test_workflows_route_typed_handoffs_without_crossing_registration_paths() -> None:
    answer = (ROOT / "docs/brain/workflows/answer.md").read_text(encoding="utf-8")
    web = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    for text in (answer, web):
        assert 'handoff.kind == "extraction"' in text
        assert 'handoff.kind == "rendered_web_capture"' in text
        assert "--rendered-staging-path" in text
        assert "SnapshotResult.active_representation" in text
        assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text


def test_role_briefs_never_allow_direct_live_wiki_edits() -> None:
    text = (ROOT / "docs/brain/agent-briefs/wiki-curator.md").read_text(encoding="utf-8")
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "Never write directly to `wiki/`" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "link_candidate_runs" in text


def test_web_workflow_has_separate_capture_forms_and_exact_renewed_passes() -> None:
    text = (ROOT / "docs/brain/workflows/web-research.md").read_text(encoding="utf-8")
    assert "snapshot-url --source-id" in text
    assert "snapshot-url --url" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert "./brain --json search --cursor \"$next_cursor\"" in text


def test_wiki_workflow_stages_and_applies_instead_of_editing_live_files() -> None:
    text = (ROOT / "docs/brain/workflows/wiki-maintenance.md").read_text(encoding="utf-8")
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "never edit live `wiki/` files directly" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "link_candidate_runs" in text
```

- [ ] **Step 2: Run the contract tests and confirm that the missing documents fail**

Run: `python3 -m pytest tests/integration/test_operating_docs.py -v`

Expected: FAIL because the policy, workflow, and brief files do not exist.

- [ ] **Step 3: Write the three canonical policies**

Use these exact headings and rules; prose may clarify them but may not weaken them:

```markdown
<!-- docs/brain/policies/citations.md -->
# Citation policy

## Immutable citation identity
Every factual claim maps to `source_id`, `content_sha256`, `derivation_id`, and a useful anchor. The original link resolves to bytes whose SHA-256 is `content_sha256`; the extraction link resolves to the recorded derivation.

## Placement
Put the citation marker directly after the smallest supported factual passage. A page-level bibliography alone is not claim-level provenance.

## Sources
Define every marker once in the final `## Sources` section using the grammar enforced by `./brain --json validate`. Link both the immutable original version and exact extracted representation.

## Unavailable evidence
Never cite a search-result snippet, an uncaptured webpage, an unresolved anchor, or a moving raw path whose bytes no longer match. Report the coverage gap instead.
```

```markdown
<!-- docs/brain/policies/wiki.md -->
# Wiki policy

## Page qualification
Create a page for a distinct reusable subject once at least one useful sourced statement exists. Small evidence-backed pages are valid; empty stubs, alias-only pages, and generic dictionary words are not.

## Linking
Link the first meaningful occurrence of a related topic in each section. Never perform global string replacement, create a self-link, or guess an ambiguous alias.

## Reciprocal relationships
Every declared reciprocal page-to-question relationship must resolve in both directions. Search all pages and questions for inbound candidates whenever either side changes.

## Hybrid approval
Routine grounded additions are automatic. Ask for approval before deleting, merging, or materially splitting pages; removing sourced claims; choosing between materially contradictory interpretations; or making an uncertain major rewrite.

## Contradictions and removal
Retain dated, cited evidence on both sides of a contradiction. After approved removal, search and reconcile every affected page, question, link, and citation before validation.
```

```markdown
<!-- docs/brain/policies/web.md -->
# Public-web policy

## Approval boundary
Before any public-web access, state the local evidence gap, proposed scope, and intended source kinds, then obtain explicit user approval for that research event. Expanded scope needs fresh approval.

## Durable evidence
Every used external document or page must be saved immutably beneath `sources/raw/_web/`, extracted, ledgered, and cited locally. Record requested and final URLs, redirects, retrieval time, media type, and checksum. Do not persist unused search results or merely viewed pages.

## Return to local research
After capture succeeds, run all three source passes over the updated active corpus. Do not synthesize directly from browser content.

## Failure
If a page cannot be captured faithfully, its claims cannot support durable repository knowledge. Report the limitation without laundering the browser observation into the wiki.
```

- [ ] **Step 4: Write the four ordered workflow documents**

`answer.md` must contain the seven tested headings and these exact decision rules:

````markdown
# Answer workflow

Read `BRAIN.md`, `docs/brain/policies/source-handling.md`, `docs/brain/policies/citations.md`, `docs/brain/policies/wiki.md`, and `docs/brain/policies/web.md` before acting.

## 1. Classify the request
Repository development, acknowledgements, and incidental conversation stop here and are not archived. A substantive knowledge request continues.

## 2. Synchronize
Run `./brain --json sync` and treat `payload.data` as a bounded status/count/sample envelope. Parse `result_manifest` with `SyncResultReference.from_dict`, require its corpus revision and exact event counts to match the envelope, and consume it only through `SyncResultStore.verify`/`iter_events`; verification completes before the first event is yielded. Stream the iterator to exhaustion without materializing the corpus-sized set, apply/deduplicate the permanent `result_id`, and durably record that seen ID. Only after those consumer-visible steps succeed, run `./brain --json source acknowledge-sync-result --result-id "$result_id"`; output or consumer failure before acknowledgement deliberately leaves the exact result pending for replay, while a repeated acknowledgement is idempotent. Drain deterministic and `needs_agent` work through the initialization rules, rerunning sync after every ingestion mutation until stable. Branch on exact handoff events: `handoff.kind == "extraction"` uses exact `.brain/agent-staging/<handoff-id>/...` output and `register-extraction`; `handoff.kind == "rendered_web_capture"` uses an approved faithful `.brain/web-staging/<handoff-id>/...` browser export and rendered `snapshot-url --rendered-staging-path`. Never send a `rendered_web_capture` handoff to `register-extraction`; require the resulting `SnapshotResult.active_representation` before treating the capture as evidence. Across that bounded loop, accumulate the sorted union of every exact `citation_rewrite` event (plus approved adoption rewrites) and every exact newly activated source ID; preserve exact coverage-gap events. Never infer completeness from response samples. Use only the final verified result's `corpus_revision` below.

Before any wiki search or sufficiency judgment, always run an empty-change reconciliation apply. Write a version-1 wiki-apply manifest beneath `.brain/wiki-staging/<run-id>/manifest.json` with the final report's `corpus_revision` as `expected_corpus_revision`, `change_intent: "routine"`, `approval_event_id: null`, the exact sorted accumulated `citation_rewrites` array (including `[]`), `link_candidate_runs: []`, and `changes: []`; then run:

```bash
./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"
```

Stop if the transaction fails. This ledger-driven reconciliation also repairs stale destinations when a prior sync report was lost. If the accumulated new-active source-ID set is nonempty, start exactly one logical freshness run. Put every sorted source ID and every deduplicated initial literal question term into that one command by repeating `--source-id` and `--term`; do not create an ID-by-term matrix of runs:

```bash
./brain --json search --scope sources --freshness --source-id "$source_id" --term "$term"
```

Read the returned `SearchResult`. While `complete` is false, require a non-null `next_cursor` and request the next page only with `./brain --json search --cursor "$next_cursor"`; require the same `run_id` and `corpus_revision`, contiguous `page_index` values, and a final `complete: true`. The freshness probe is one logical sync-evidence run, never a fourth research pass. A relevant hit makes an older wiki answer stale. If any page is unavailable, stale, expired, tampered, or incomplete, stop with a partial result; do not judge sufficiency.

## 3. Search the wiki
Build one deduplicated term list from every exact term, entity, alias, date, acronym, spelling variant, or literal phrase, then start one logical wiki run by repeating `--term` in:

```bash
./brain --json search --scope wiki --term "$term"
```

Drain every continuation using only `./brain --json search --cursor "$next_cursor"` until the wiki `SearchResult.complete` is true. Reject a changed run ID/revision, a noncontiguous page, or an incomplete run.

## 4. Judge sufficiency
The wiki is sufficient only when it addresses every material part, matches the relevant corpus revision, and has no unresolved material contradiction. Before making that judgment, re-resolve every underlying claim citation against the current ledger and call `build_wiki_evidence_packet(...)` with the fully drained wiki `search_pages`, matched paths, and exact supporting/counterevidence `CitationRef(document_path, citation_id)` tuples. Its `WikiEvidencePacket` fields—question ID, corpus revision, search run ID, matched records, `RevalidatedCitation` values, `supporting_citations`, `counterevidence_citations`, contradictions, coverage gaps, and `complete`—must validate, and `complete` must be true. If that packet is sufficient, skip Section 5 but continue through Sections 6 and 7; the fast path still updates the one evolving topic Q&A record and validates the graph before answering.

## 5. Search sources three times
When the validated `WikiEvidencePacket` is insufficient, start exactly these three **logical** source passes, once each and in this order; repeat `--term` inside the same initial invocation for every term in that pass:

```bash
./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3
./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3
./brain --json search --scope sources --pass verification --term "$verification_term" --context 3
```

After each initial invocation, consume its `SearchResult`; while `complete` is false, call only `./brain --json search --cursor "$next_cursor"`. Require a stable run ID/revision, contiguous pages, and final `complete: true`, then aggregate all pages into that pass's `SearchPassRecord`. Generate expansion literals only after discovery is fully drained. Generate verification literals for decisive support, dates, exceptions, conflicts, temporal qualifiers, and counterexamples only after expansion is fully drained. Never substitute extra logical searches for one of these passes. If any pass cannot finish, persist/report only a partial or unanswered state; never construct a completed `EvidencePacket` or proceed as if the missing pages were empty.

## 6. Synthesize and persist
Choose exactly one validated `CuratorEvidence`: the complete `WikiEvidencePacket` on the sufficient fast path, or a completed three-pass `EvidencePacket` with support, counterevidence, temporal qualifiers, immutable citation identities, and uncertainty on the source path. The curator always creates or updates one evolving topic Q&A record, including a meaningful new phrasing/refinement and the current corpus revision; it may also stage qualifying pages. It writes only beneath `.brain/wiki-staging/<run-id>/files/`, mirroring each full `wiki/pages/...` or `wiki/questions/...` target path, and never edits live `wiki/` files directly.

For every proposed write or delete, start `./brain --json links candidates "$changed_page" --term "$term"` with all relevant repeated terms, then drain it with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`. Put the sorted fully drained `LinkCandidateRunProof` values in manifest `link_candidate_runs`; stop rather than apply if any proof is absent, stale, or incomplete. Reconcile reciprocal links in the staged write set, write the exact version-1 manifest defined by the knowledge-workflow plan, and publish only with `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`.

## 7. Validate and answer
Run `./brain --json links check` and `./brain --json validate`. Repair failures through a new `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"` transaction before answering. Disclose partial coverage and conflicts. Offer the approval-gated web workflow only when local evidence remains insufficient.
````

`web-research.md`, `wiki-maintenance.md`, and `validate.md` must be executable checklists that cite their canonical policy file and name exact commands. In particular, include:

```markdown
# Web-research workflow
Read `docs/brain/policies/web.md` and `docs/brain/policies/approvals.md` before acting.

1. Stop before opening any public URL.
2. Explain the local evidence gap and ask for explicit approval for a bounded research event.
3. After approval, research candidates; distinguish viewed candidates from evidence actually used.
4. For a used source with an existing descriptor, run `./brain --json source snapshot-url --source-id "$source_id" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`.
5. For a used ad-hoc URL with no descriptor, separately run `./brain --json source snapshot-url --url "$url" --description "$description" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`.
6. Read each `SnapshotResult` plus its current `payload.data.handoffs[]`. If `handoff.kind == "extraction"`, create the representation only at the exact `.brain/agent-staging/<handoff-id>/...` scope and register it with `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. If `handoff.kind == "rendered_web_capture"`, use the already approved browser event to export faithful serialized DOM, print-to-PDF, or downloaded claim-bearing bytes below `.brain/web-staging/<handoff-id>/...`, then run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`; append one `--redirect-url "$redirect_url"` per observed redirect. Never send a `rendered_web_capture` handoff to `register-extraction`.
7. Verify `SnapshotResult.active_representation` is non-null for every used capture, points to that result's source/content identity, and belongs to the latest returned corpus revision. Process any new typed handoff by the same branch. If a faithful local active representation cannot be reached, stop and do not cite the browser material. Never persist unused candidates.
8. Against the new corpus revision, start exactly these three logical runs once each and in order: `./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3`; `./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3`; `./brain --json search --scope sources --pass verification --term "$verification_term" --context 3`. Repeat `--term` within the initial invocation, and drain each result with `./brain --json search --cursor "$next_cursor"` until `complete: true` before deriving the next pass. Stop with partial/unanswered status if any run is incomplete.
9. Persist only locally resolving citations through a staged wiki-apply manifest after fully draining link-candidate runs for every write/delete.
```

```markdown
# Wiki-maintenance workflow
Read `docs/brain/policies/wiki.md` and `docs/brain/policies/citations.md` before acting.

1. Parse, do not regex-rewrite, Markdown structure.
2. Enforce evidence-backed page qualification and claim-level citations.
3. For every proposed write/delete, run `./brain --json links candidates "$changed_page" --term "$term"` for all titles, aliases, entities, dates, and important phrases, repeating `--term` in that one initial command.
4. Drain every result with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`; stop if a run is stale, expired, tampered, or incomplete. Let the curator accept only genuine, unambiguous relationships.
5. Write proposed pages/questions only beneath `.brain/wiki-staging/<run-id>/files/wiki/pages/` or `files/wiki/questions/`, mirroring each full logical target, update relationships reciprocally in that staged set, and write the version-1 manifest with checksums, the current expected corpus revision, and sorted `link_candidate_runs` proofs covering every write/delete.
6. Publish only with `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`; never edit live `wiki/` files directly.
7. Run `./brain --json links check` and `./brain --json validate`.
8. Stop for approval at every destructive or contradiction gate in the wiki policy and put the approval event ID in the manifest.
```

```markdown
# Validation workflow
Read `BRAIN.md` and the policies/workflows relevant to the changed paths.

1. Run focused tests for changed behavior.
2. Run `python3 -m pytest -v`.
3. Run `./brain --json validate --full`.
4. Inspect state counts, coverage gaps, citation failures, and graph failures rather than relying only on exit status.
5. Do not claim completion while any unexplained pending, `needs_agent`, integrity, citation, or graph failure remains.
```

- [ ] **Step 5: Write the canonical agent briefs with strict input/output contracts**

Each brief starts with its write-scope declaration and contains the following role-specific contract:

```markdown
<!-- source-researcher.md -->
# Source researcher
Write scope: none

## Inputs
Question, corpus revision, active search roots, known coverage gaps, and any prior evidence packet.

## Procedure
Start exactly one `./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3`, exactly one `./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3`, and exactly one `./brain --json search --scope sources --pass verification --term "$verification_term" --context 3`, in that order. Repeat `--term` within each initial invocation when a pass has multiple literals. For each logical pass, call only `./brain --json search --cursor "$next_cursor"` until its `SearchResult.complete` is true; validate stable run/revision and contiguous pages, then aggregate those pages into one `SearchPassRecord`. Generate discovery terms from the question, expansion terms only after the complete discovery run, and verification terms only after complete expansion context for decisive claims, dates, exceptions, conflicts, and counterexamples. Use only those three logical searches, their continuation pages, and read operations. Never browse or mutate repository files.

## Required output
Return a structured evidence packet containing the question, corpus revision, all three pass records, supporting passages, counterevidence, source/version/derivation/anchor identities, unanswered points, and coverage gaps.

## Stop conditions
Stop and report partial/unanswered status if active sources are unavailable, a cursor is stale/expired/tampered, any run remains incomplete, a requested citation does not resolve, public-web access would be needed, or the corpus revision changes during research.
```

```markdown
<!-- source-ingester.md -->
# Source ingester
Write scope: source artifacts only

## Inputs
A current entry from `payload.data.handoffs[]` with its immutable `handoff_id`, `kind`, exact source ID/content checksum recorded by the CLI, manifest-supplied `agent_revision`, permitted handoff-scoped staging path, and required anchor kinds. Never glob or derive a handoff ID or provenance value.

## Procedure
Branch before writing. For `handoff.kind == "extraction"`, read the source locally, create a faithful searchable Markdown representation, preserve deterministic anchors, and write only to the exact `.brain/agent-staging/<handoff-id>/...` path. Register it with `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. Using the returned registration identity, read back the persisted ledger derivation and require `method: "agent"` with `method_metadata` containing exactly the immutable `handoff_id`, manifest `agent_revision`, and note. Never pass caller-chosen source, content, extractor, version, configuration, agent revision, method, or method metadata: the CLI loads/derives provenance from the current handoff, rejects stale or wrong-scope handoffs, and permits a consumed handoff only as an exact idempotent replay with identical active derivation metadata, anchors, quality, staged/published checksum, size, and mtime; differing replay bytes collision-fail.

For `handoff.kind == "rendered_web_capture"`, do not extract or register it here. Return it to the approved web workflow, which alone writes faithful browser-exported bytes below `.brain/web-staging/<handoff-id>/...` and consumes them with rendered `snapshot-url`. Never send a `rendered_web_capture` handoff to `register-extraction`. Never change wiki content or invent ledger values.

## Required output
For extraction, return the CLI registration's source ID, source checksum, output path, derivation ID, active representation, and corpus revision, plus the read-back output checksum, `method`, exact `method_metadata`, quality state, anchors, and diagnostics. For rendered capture, return the untouched handoff identity and an explicit `rendered_web_capture` route result; do not claim a derivation.

## Stop conditions
Stop on checksum drift, a staging path outside the exact `.brain/agent-staging/<handoff-id>/...` scope, unapproved network or installation need, unreadable content, or a representation that cannot be made faithful.
```

```markdown
<!-- wiki-curator.md -->
# Wiki curator
Write scope: wiki artifacts only

## Inputs
A validated `CuratorEvidence` (`WikiEvidencePacket | EvidencePacket`), current page/question files, fully drained candidate-link contexts/proofs, corpus revision, and approval decisions when required.

## Procedure
Create proposed content only beneath `.brain/wiki-staging/<run-id>/files/wiki/pages/` or `files/wiki/questions/`, mirroring each full logical target. Never write directly to `wiki/`. Whether the input is a wiki-sufficient packet or a three-pass packet, create or update exactly one evolving topic question, preserve its meaningful question history, and record the current revision. Create/update qualifying atomic pages, attach immutable claim citations, and preserve contradictions. For every proposed write/delete, start one batched `./brain --json links candidates "$changed_page" --term "$term"` run, drain it only with `./brain --json links candidates --cursor "$next_cursor"`, and reconcile genuine reciprocal relationships only after `complete: true`. Write the version-1 manifest with the expected corpus revision, exact staged-file SHA-256 values, change intent, approval event ID when required, any `citation_rewrites`, and sorted `link_candidate_runs` proofs; invoke `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`. Do not edit sources or ledger data.

## Required output
Return changed paths, created/updated IDs, claim-to-citation mappings, reciprocal relationships, unresolved ambiguity, and validation result.

## Stop conditions
Stop when evidence is incomplete/insufficient for a factual claim, a `WikiEvidencePacket` or `EvidencePacket` is invalid, citation identity is incomplete, any link-candidate run is not fully drained/current, a destructive/contradictory decision lacks approval, or another writer owns the same wiki write set.
```

```markdown
<!-- brain-auditor.md -->
# Brain auditor
Write scope: none

## Inputs
Proposed changes, source status, evidence packet, wiki records, and deterministic validation reports.

## Procedure
Independently check corpus coverage, claim support, counterevidence, immutable citations, source/version consistency, reciprocal links, routing, and approval evidence. Never repair findings itself.

## Required output
Return ordered findings with severity, exact path/claim, violated rule, evidence, and a clear pass only when no blocking finding remains.

## Stop conditions
Stop and fail closed when required artifacts, approvals, source versions, or validation reports are missing.
```

- [ ] **Step 6: Run the operating-document tests**

Run: `python3 -m pytest tests/integration/test_operating_docs.py -v`

Expected: PASS.

- [ ] **Step 7: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only Task 1 files and earlier planned work are listed. Do not commit.

---

### Task 2: Add a deterministic instruction-architecture validator

**Files:**

- Create: `brainlib/instructions.py`
- Create: `brainlib/validators/__init__.py`
- Create: `brainlib/validators/instructions.py`
- Create: `tests/unit/test_instructions.py`
- Modify: `brainlib/validation.py`
- Modify: `tests/integration/test_wiki_validation.py`

**Interfaces:**

- Consumes: `RepoPaths`, `ValidationIssue`, `ValidationReport`, and the plan-wide skill/brief names.
- Produces:

```python
@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    body: str
    path: PurePosixPath

def parse_scalar_frontmatter(path: Path) -> tuple[dict[str, str], str]: ...
def load_skill(root: Path, path: Path) -> SkillDocument: ...
def validate_instruction_architecture(paths: RepoPaths) -> ValidationReport: ...
```

- [ ] **Step 1: Write failing parser and topology tests**

```python
# tests/unit/test_instructions.py
import io
import json
from pathlib import Path

import pytest

from brainlib.cli import main
from brainlib.instructions import FrontmatterError, load_skill, parse_scalar_frontmatter
from brainlib.layout import RepoPaths
from brainlib.validators import validate_instruction_architecture


def break_instruction_topology(repo_root: Path) -> RepoPaths:
    canonical = repo_root / ".agents/skills/brain-answer/SKILL.md"
    if canonical.is_file():
        canonical.unlink()
    claude_link = repo_root / ".claude/skills/brain-answer"
    if claude_link.is_symlink() or claude_link.exists():
        claude_link.unlink()
    return RepoPaths.discover(repo_root)


def test_parse_scalar_frontmatter_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: one\nname: two\n---\nBody\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="duplicate key: name"):
        parse_scalar_frontmatter(path)


def test_load_skill_requires_matching_directory_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "brain-answer"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text("---\nname: wrong\ndescription: x\n---\nBody\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="must match directory"):
        load_skill(tmp_path, path)


def test_validator_requires_canonical_skills_and_relative_claude_links(repo_root: Path) -> None:
    report = validate_instruction_architecture(break_instruction_topology(repo_root))
    assert {issue.code for issue in report.issues} >= {
        "missing_canonical_skill",
        "invalid_claude_skill_link",
    }


def test_validate_command_appends_instruction_architecture_report(repo_root: Path) -> None:
    break_instruction_topology(repo_root)
    stdout = io.StringIO()
    returncode = main(["--json", "validate"], cwd=repo_root, stdout=stdout, stderr=io.StringIO())
    payload = json.loads(stdout.getvalue())
    report = next(item for item in payload["data"]["reports"] if item["checks"] == ["instruction-architecture"])
    assert returncode == 1
    assert {item["code"] for item in report["issues"]} >= {"missing_canonical_skill", "invalid_claude_skill_link"}


def test_validator_rejects_model_pins_and_cross_client_description_drift(repo_root: Path) -> None:
    codex = repo_root / ".codex/agents/source-researcher.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(
        'name = "source-researcher"\n'
        'description = "Codex description"\n'
        'model = "pinned"\n'
        'developer_instructions = "Read docs/brain/agent-briefs/source-researcher.md"\n',
        encoding="utf-8",
    )
    claude = repo_root / ".claude/agents/source-researcher.md"
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(
        "---\nname: source-researcher\ndescription: Claude description\n---\n"
        "Read `docs/brain/agent-briefs/source-researcher.md`.\n",
        encoding="utf-8",
    )
    report = validate_instruction_architecture(RepoPaths.discover(repo_root))
    assert "invalid_agent_adapter" in {issue.code for issue in report.issues}
```

Append this integration assertion to the Plan 4-owned validation tests so composition is checked at the function boundary rather than only through the renderer:

```python
# tests/integration/test_wiki_validation.py
from brainlib.validation import validate_repository


def test_validate_repository_composes_instruction_architecture_report(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("citations/current")
    reports = validate_repository(
        scenario.paths, scenario.ledger, full=False,
    )
    instruction_reports = [
        report for report in reports
        if report.checks == ("instruction-architecture",)
    ]
    assert len(instruction_reports) == 1
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `python3 -m pytest tests/unit/test_instructions.py -v`

Expected: FAIL with `ModuleNotFoundError: brainlib.instructions`.

- [ ] **Step 3: Implement the strict scalar-frontmatter parser**

```python
# brainlib/instructions.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class FrontmatterError(ValueError):
    pass


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    body: str
    path: PurePosixPath


def parse_scalar_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise FrontmatterError(f"{path}: missing opening frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise FrontmatterError(f"{path}: missing closing frontmatter delimiter") from exc
    values: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise FrontmatterError(f"{path}:{number}: expected scalar key: value")
        key, raw_value = line.split(":", 1)
        key, value = key.strip(), raw_value.strip()
        if key in values:
            raise FrontmatterError(f"{path}:{number}: duplicate key: {key}")
        if not key or not value or value[0] in "[{|>":
            raise FrontmatterError(f"{path}:{number}: only nonempty scalar values are allowed")
        values[key] = value.strip("\"'")
    return values, "\n".join(lines[end + 1 :]).strip() + "\n"


def load_skill(root: Path, path: Path) -> SkillDocument:
    metadata, body = parse_scalar_frontmatter(path)
    missing = {"name", "description"} - metadata.keys()
    if missing:
        raise FrontmatterError(f"{path}: missing {', '.join(sorted(missing))}")
    if metadata["name"] != path.parent.name:
        raise FrontmatterError(f"{path}: skill name must match directory {path.parent.name}")
    return SkillDocument(
        name=metadata["name"],
        description=metadata["description"],
        body=body,
        path=PurePosixPath(path.relative_to(root).as_posix()),
    )
```

- [ ] **Step 4: Implement topology validation and merge it into `./brain --json validate`**

```python
# brainlib/validators/__init__.py
"""Repository validation entry points."""

from brainlib.validators.instructions import validate_instruction_architecture

__all__ = ("validate_instruction_architecture",)
```

```python
# brainlib/validators/instructions.py
from pathlib import Path
import tomllib

from brainlib.diagnostics import ValidationIssue, ValidationReport
from brainlib.instructions import FrontmatterError, load_skill, parse_scalar_frontmatter
from brainlib.layout import RepoPaths

SKILLS = (
    "brain-initialize",
    "brain-answer",
    "brain-web-research",
    "brain-wiki-maintenance",
    "brain-validate",
)
AGENTS = ("source-researcher", "source-ingester", "wiki-curator", "brain-auditor")


def validate_instruction_architecture(paths: RepoPaths) -> ValidationReport:
    issues: list[ValidationIssue] = []
    for name in SKILLS:
        canonical = paths.root / ".agents" / "skills" / name / "SKILL.md"
        if not canonical.is_file():
            issues.append(ValidationIssue("error", "missing_canonical_skill", f"missing {canonical.relative_to(paths.root)}"))
        else:
            try:
                load_skill(paths.root, canonical)
            except FrontmatterError as exc:
                issues.append(ValidationIssue("error", "invalid_skill", str(exc)))
        claude_link = paths.root / ".claude" / "skills" / name
        expected = Path("..") / ".." / ".agents" / "skills" / name
        if not claude_link.is_symlink() or Path(claude_link.readlink()) != expected:
            issues.append(ValidationIssue("error", "invalid_claude_skill_link", f"{claude_link.relative_to(paths.root)} must link to {expected}"))
    for name in AGENTS:
        brief = paths.root / "docs" / "brain" / "agent-briefs" / f"{name}.md"
        if not brief.is_file():
            issues.append(ValidationIssue("error", "missing_agent_brief", f"missing {brief.relative_to(paths.root)}"))
        brief_reference = f"docs/brain/agent-briefs/{name}.md"
        codex_relative = Path(".codex/agents") / f"{name}.toml"
        codex = paths.root / codex_relative
        claude_relative = Path(".claude/agents") / f"{name}.md"
        claude = paths.root / claude_relative
        codex_value: dict[str, object] | None = None
        if not codex.is_file():
            issues.append(ValidationIssue("error", "missing_agent_adapter", f"missing {codex_relative}"))
        else:
            try:
                codex_value = tomllib.loads(codex.read_text(encoding="utf-8"))
                allowed = {"name", "description", "sandbox_mode", "developer_instructions"}
                if codex_value.keys() - allowed:
                    raise ValueError("unknown or model-specific key")
                if codex_value.get("name") != name or not isinstance(codex_value.get("description"), str):
                    raise ValueError("name/description mismatch")
                instructions = codex_value.get("developer_instructions")
                if "model" in codex_value or not isinstance(instructions, str) or brief_reference not in instructions:
                    raise ValueError("model pin or shared-brief route violation")
                if len(codex.read_text(encoding="utf-8").splitlines()) > 12:
                    raise ValueError("adapter exceeds 12 lines")
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as exc:
                issues.append(ValidationIssue("error", "invalid_agent_adapter", f"{codex_relative}: {exc}"))
        if not claude.is_file():
            issues.append(ValidationIssue("error", "missing_agent_adapter", f"missing {claude_relative}"))
        else:
            try:
                metadata, body = parse_scalar_frontmatter(claude)
                if set(metadata) != {"name", "description"} or metadata.get("name") != name:
                    raise ValueError("frontmatter must contain only matching name and description")
                if brief_reference not in body:
                    raise ValueError("body does not route to shared brief")
                if codex_value is not None and codex_value.get("description") != metadata["description"]:
                    raise ValueError("Codex/Claude descriptions differ")
                if len(claude.read_text(encoding="utf-8").splitlines()) > 12:
                    raise ValueError("adapter exceeds 12 lines")
            except (OSError, UnicodeError, FrontmatterError, ValueError) as exc:
                issues.append(ValidationIssue("error", "invalid_agent_adapter", f"{claude_relative}: {exc}"))
    return ValidationReport(checks=("instruction-architecture",), issues=tuple(issues), corpus_revision=None)
```

Keep the canonical Plan 1 `RepoPaths` unchanged and derive instruction-only paths from `paths.root`, as the implementation above does. Modify the Plan 4-owned `brainlib.validation.validate_repository()` itself: while it holds its existing coherent source-then-wiki lock scope, append exactly one `validate_instruction_architecture(paths)` result to the tuple it already returns, after its layout/source/transaction/wiki reports. Do this for both normal and `full=True` calls without changing the function signature or checksum-cache behavior. The CLI validate handler remains only a renderer of that returned tuple; do not import the instruction validator there, assemble another report list, or create a competing top-level validator. The report therefore appears once in `payload["data"]["reports"]` with `checks == ["instruction-architecture"]`. During this task, its missing-skill/adapter issues are expected until Tasks 3–5 finish; any malformed object that already exists remains a real error.

- [ ] **Step 5: Run the focused tests**

Run: `python3 -m pytest tests/unit/test_instructions.py tests/integration/test_wiki_validation.py::test_validate_repository_composes_instruction_architecture_report -v`

Expected: PASS for parser behavior and expected missing-topology diagnostics.

- [ ] **Step 6: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors. Do not commit.

---

### Task 3: Implement the initialization and answer skills

**Required sub-skill:** Read and follow `superpowers:writing-skills` before authoring or changing either `SKILL.md` file.

**Files:**

- Create: `.agents/skills/brain-initialize/SKILL.md`
- Create: `.agents/skills/brain-answer/SKILL.md`
- Create: `tests/integration/test_core_skills.py`

**Interfaces:**

- Consumes: `./brain --json doctor`, `./brain --json init`, `./brain --json sync`, `./brain --json status`, `./brain --json search`, `./brain --json source register-extraction`, link, wiki-apply, and validation commands; canonical workflows/briefs.
- Produces: two client-independent orchestration entrypoints with explicit state and stop rules.

- [ ] **Step 1: Write failing behavioral-content tests**

```python
# tests/integration/test_core_skills.py
from pathlib import Path

from brainlib.instructions import load_skill

ROOT = Path(__file__).resolve().parents[2]


def skill(name: str) -> str:
    return load_skill(ROOT, ROOT / ".agents/skills" / name / "SKILL.md").body


def ordered(text: str, phrases: tuple[str, ...]) -> bool:
    offsets = [text.index(phrase) for phrase in phrases]
    return offsets == sorted(offsets)


def test_initialize_skill_is_resumable_and_drains_agent_handoffs() -> None:
    text = skill("brain-initialize")
    assert ordered(text, ("./brain --json doctor", "./brain --json init", "needs_agent", "./brain --json status", "./brain --json validate --full"))
    assert "Ask before installing" in text
    assert "Ask before every public-web research event" in text
    assert "complete_with_gaps" in text
    assert "payload.data.handoffs[]" in text
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert "python3 -m pytest" not in text


def test_answer_skill_enforces_sync_wiki_first_and_three_passes() -> None:
    text = skill("brain-answer")
    assert ordered(text, ("./brain --json sync", "citation_rewrites", "./brain --json wiki apply --manifest", "--freshness", "Search the wiki", "Judge sufficiency", "--pass discovery", "--pass expansion", "--pass verification", "Persist", "./brain --json validate"))
    assert "repository-development request" in text
    assert "Do not create a question record" in text
    assert "corpus revision changes" in text
    assert "after every ingestion mutation until stable" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert ".brain/wiki-staging/" in text
    assert "Never write directly to `wiki/`" in text
    assert "WikiEvidencePacket" in text
    assert "one evolving topic Q&A record" in text
    assert "./brain --json search --cursor \"$next_cursor\"" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "If sufficient, skip the three source passes" in text


def test_answer_skill_batches_freshness_and_routes_typed_handoffs() -> None:
    text = skill("brain-answer")
    assert "one logical freshness run" in text
    assert "do not create one run per source/term pair" in text
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
```

- [ ] **Step 2: Run the tests and confirm missing-skill failures**

Run: `python3 -m pytest tests/integration/test_core_skills.py -v`

Expected: FAIL because both canonical skill files are absent.

- [ ] **Step 3: Write `brain-initialize` as an explicit resumable state machine**

```markdown
---
name: brain-initialize
description: Use when the user asks to initialize a Second Brain, ingest all initial sources, or resume an interrupted initialization.
---

# Initialize the brain

Read `BRAIN.md`, `docs/brain/workflows/initialize.md`, `docs/brain/policies/source-handling.md`, `docs/brain/policies/approvals.md`, and `docs/brain/policies/web.md` before acting.

1. Run `./brain --json doctor`. Treat its JSON as data; do not copy checksum or state values by hand.
2. If an approved converter is missing, present the exact reported recipe and affected formats. **Ask before installing** any system or repository-local dependency. Never substitute an unlisted tool.
3. Run `./brain --json init`. It is resumable: preserve successful checkpoints and retry only work the ledger marks eligible.
4. If URL descriptors are `awaiting_approval`, explain the bounded initial fetch set. **Ask before every public-web research event**; after approval, create a unique event ID, bounded scope, and short note. For each existing descriptor run `./brain --json source snapshot-url --source-id "$source_id" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`. The CLI records this agent-supplied claim on each captured content version but cannot independently prove a chat response occurred. Do not use the ad-hoc `--url` form for a descriptor. Retain the returned `SnapshotResult`, handoff summaries, event ID, scope, and note.
5. For each exact entry in `payload.data.handoffs[]`, branch on its immutable kind; never glob or derive an ID and never parallelize two writes to one source.
   - If `handoff.kind == "extraction"`, dispatch `source-ingester`, require its output below the exact `.brain/agent-staging/<handoff-id>/...` scope, and run `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. Do not supply source/content/extractor/version/configuration/agent-revision/method flags. Use the returned registration identity to read back the ledger and verify the persisted derivation has `method: "agent"` and exact `method_metadata` `{handoff_id, agent_revision, note}` derived by the CLI.
   - If `handoff.kind == "rendered_web_capture"`, confirm the prior approval still covers browser rendering; otherwise ask again before access. Save a faithful serialized DOM, print-to-PDF, or downloaded claim-bearing artifact below the exact `.brain/web-staging/<handoff-id>/...` scope. Run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`, appending one `--redirect-url "$redirect_url"` per observed redirect. Never send a `rendered_web_capture` handoff to `register-extraction`. Verify the returned `SnapshotResult.active_representation` is non-null and matches its source/content identity; route any newly returned typed handoff through this same branch.
6. Rerun `./brain --json init` until every eligible item is processed and the handoff manifest is empty. Stop rather than claim completion if a rendered export or active representation cannot be made faithful.
7. For normal user initialization, run `./brain --json status` and `./brain --json validate --full`. Pytest is an implementation/PR gate in `brain-validate`, not an initialization step.

A `complete_with_gaps` result is not complete initialization. Report every pending, failed, unsupported, warning, integrity, approval, or agent gap. Never create wiki pages during initialization. Never install, browse, mutate raw originals, or change the extractor allowlist without its separate approval.
```

- [ ] **Step 4: Write `brain-answer` as the sole substantive-question pipeline**

````markdown
---
name: brain-answer
description: Use when the user asks a substantive knowledge question that should be answered from and, when evidence permits, persisted in the Second Brain.
---

# Answer from the brain

Read `BRAIN.md`, `docs/brain/workflows/answer.md`, and the citation, wiki, source-handling, approval, and web policies before acting.

## Classify
If this is a repository-development request, acknowledgement, or incidental conversational question, answer normally. **Do not create a question record** and do not invoke this knowledge workflow further.

## Synchronize
Run `./brain --json sync`; retain its bounded status/count/sample fields, parse the `result_manifest` reference, verify the whole manifest and matching revision/counts, then stream exact events to exhaustion. Deduplicate/apply by `result_id`, durably record that ID, and only then run `./brain --json source acknowledge-sync-result --result-id "$result_id"`; never acknowledge a partial stream or an ID not durably recorded. Drain deterministic or `needs_agent` ingestion using the initialization workflow, rerunning sync after every ingestion mutation until stable. For every exact handoff event, branch explicitly. If `handoff.kind == "extraction"`, write only in `.brain/agent-staging/<handoff-id>/...` and run `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. If `handoff.kind == "rendered_web_capture"`, use approved faithful browser bytes in `.brain/web-staging/<handoff-id>/...` and run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`, appending observed redirect flags. Never send a `rendered_web_capture` handoff to `register-extraction`, and require `SnapshotResult.active_representation` before treating its evidence as active.

Accumulate the sorted union of exact streamed `citation_rewrite` events or approved-adoption rewrites plus exact streamed newly activated source IDs across the entire loop, retain unresolved exact coverage-gap events, and use the final verified corpus revision. Before searching or judging the wiki, always put that exact rewrite union (including an empty array) in a version-1 manifest with `link_candidate_runs: []` and `changes: []`, stage it at `.brain/wiki-staging/<run-id>/manifest.json`, and run `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`. This ledger-driven reconciliation recovers stale destinations even when a prior response was lost; stop on failure.

If any source became active, start one logical freshness run with `./brain --json search --scope sources --freshness --source-id "$source_id" --term "$term"`, repeating `--source-id` for every sorted new ID and `--term` for every initial term; do not create one run per source/term pair. Drain all pages with `./brain --json search --cursor "$next_cursor"` until `complete: true`, checking the run ID, revision, and contiguous indexes. This is a freshness probe, not a research pass. A relevant hit makes the old wiki answer stale; an incomplete run blocks sufficiency.

## Search the wiki
Generate literal exact terms, entities, aliases, dates, acronyms, spelling variants, and phrases. Start one logical `./brain --json search --scope wiki --term "$term"` with repeated terms, then drain it using `./brain --json search --cursor "$next_cursor"` until `complete: true`. Read relevant page and question sections only after the run is complete.

## Judge sufficiency
Revalidate every underlying citation against the current ledger by calling `build_wiki_evidence_packet(...)` with the fully drained wiki pages, matched paths, and exact supporting/counterevidence `CitationRef(document_path, citation_id)` tuples. Accept its canonical `WikiEvidencePacket` only if it directly covers every material part, every factual passage maps to a unique `RevalidatedCitation`, it reflects the current relevant corpus revision, contradictions/coverage gaps are represented, and `complete` is true. If sufficient, skip the three source passes, but do not answer or stop yet: pass this validated packet to the curator so it creates or updates the one evolving topic Q&A record, then complete graph and repository validation.

## Search sources
Dispatch one read-only `source-researcher`. It must make exactly these three invocations, once each and in order, repeating `--term` inside an invocation for additional pass literals:

```bash
./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3
./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3
./brain --json search --scope sources --pass verification --term "$verification_term" --context 3
```

Each initial command starts one logical pass. Drain every returned page using only `./brain --json search --cursor "$next_cursor"` until `complete: true`; validate stable run/revision and contiguous pages and aggregate them into one `SearchPassRecord`. Derive expansion only after discovery is complete and verification only after expansion is complete. Read relevant sections after bounded contexts. If a cursor/run is stale, expired, tampered, or incomplete, report partial/unanswered status and do not construct a completed packet. If the corpus revision changes, discard the stale evidence packet and rerun exactly those three logical passes.

## Persist
Give exactly one validated `CuratorEvidence` to one `wiki-curator`: the `WikiEvidencePacket` on the sufficient fast path or the completed three-pass `EvidencePacket` otherwise. On both paths it updates the existing topic Q&A or creates one if none exists, preserves meaningful prior phrasings/refinements, records the current corpus revision, and stages qualifying evidence-backed pages beneath `.brain/wiki-staging/<run-id>/files/`, mirroring each full logical target. Never write directly to `wiki/`.

For every write/delete, start a batched `./brain --json links candidates "$changed_page" --term "$term"` and drain it with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`. The curator reconciles reciprocal links, includes the sorted `LinkCandidateRunProof` values in `link_candidate_runs`, writes the version-1 manifest, and invokes `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"` atomically. It must stop if any required candidate run is incomplete.

## Validate and answer
Run `./brain --json links check` and `./brain --json validate`. Repair deterministic failures through another staged wiki-apply transaction and request an independent `brain-auditor` review for material knowledge changes. Answer with unresolved conflict and coverage gaps disclosed.

If local evidence is insufficient, offer `brain-web-research`; do not browse before explicit approval. After approved captures are active, rerun Pass 1, Pass 2, and Pass 3 before changing the persisted synthesis.
````

- [ ] **Step 5: Run the core-skill tests**

Run: `python3 -m pytest tests/integration/test_core_skills.py -v`

Expected: PASS.

- [ ] **Step 6: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors. Do not commit.

---

### Task 4: Implement the web, wiki-maintenance, and validation skills

**Required sub-skill:** Read and follow `superpowers:writing-skills` before authoring or changing any `SKILL.md` file.

**Files:**

- Create: `.agents/skills/brain-web-research/SKILL.md`
- Create: `.agents/skills/brain-wiki-maintenance/SKILL.md`
- Create: `.agents/skills/brain-validate/SKILL.md`
- Create: `tests/integration/test_guardrail_skills.py`

**Interfaces:**

- Consumes: approval-event claim fields and immutable snapshot contracts, wiki mutation/validation contracts, and canonical policies.
- Produces: focused reusable workflows invoked by `brain-answer`, manual maintenance, and handoff routing.

- [ ] **Step 1: Write failing guardrail tests**

```python
# tests/integration/test_guardrail_skills.py
from pathlib import Path

from brainlib.instructions import load_skill

ROOT = Path(__file__).resolve().parents[2]


def body(name: str) -> str:
    return load_skill(ROOT, ROOT / ".agents/skills" / name / "SKILL.md").body


def test_web_skill_asks_before_access_and_localizes_used_evidence() -> None:
    text = body("brain-web-research")
    assert text.index("Ask the user") < text.index("Access public-web tools")
    for required in ("approval event ID", "snapshot-url", "Unused candidates", "Pass 1", "Pass 2", "Pass 3"):
        assert required in text
    assert "snapshot-url --source-id" in text
    assert "snapshot-url --url" in text
    assert text.count("--pass discovery") == 1
    assert text.count("--pass expansion") == 1
    assert text.count("--pass verification") == 1
    assert 'handoff.kind == "extraction"' in text
    assert 'handoff.kind == "rendered_web_capture"' in text
    assert "--rendered-staging-path" in text
    assert "SnapshotResult.active_representation" in text
    assert "Never send a `rendered_web_capture` handoff to `register-extraction`" in text
    assert "./brain --json search --cursor \"$next_cursor\"" in text


def test_wiki_skill_names_every_approval_gate() -> None:
    text = body("brain-wiki-maintenance")
    for required in ("deleting", "merging", "materially splitting", "Removing a sourced claim", "contradictory", "ambiguous rename"):
        assert required in text
    assert "global string replacement" in text
    assert "reciprocal" in text
    assert ".brain/wiki-staging/" in text
    assert "./brain --json wiki apply --manifest" in text
    assert "Never write directly to `wiki/`" in text
    assert "./brain --json links candidates --cursor \"$next_cursor\"" in text
    assert "link_candidate_runs" in text


def test_validate_skill_requires_full_validation_at_handoff() -> None:
    text = body("brain-validate")
    assert "python3 -m pytest -v" in text
    assert "./brain --json validate --full" in text
    assert "exit code alone" in text
```

- [ ] **Step 2: Run the tests and confirm missing-skill failures**

Run: `python3 -m pytest tests/integration/test_guardrail_skills.py -v`

Expected: FAIL because the three skill files do not exist.

- [ ] **Step 3: Write the public-web skill**

````markdown
---
name: brain-web-research
description: Use when local brain evidence is insufficient and the user is considering an explicitly approved, bounded public-web research event.
---

# Research the public web safely

Read `docs/brain/policies/web.md`, `docs/brain/policies/approvals.md`, and `docs/brain/workflows/web-research.md`.

1. Show the unresolved local evidence gap, proposed research question, scope, and expected source kinds. **Ask the user** for explicit approval. Stop if approval is absent.
2. Create a unique approval event ID, bounded scope, and short approval note for this question. The CLI records those claim fields plus its own UTC recording time on each captured content version, but relies on the calling agent to represent the conversation honestly.
3. **Access public-web tools** only now. Track candidates separately from evidence chosen for use.
4. For a used source that already has a descriptor, run exactly `./brain --json source snapshot-url --source-id "$source_id" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`.
5. For a used ad-hoc URL with no descriptor, run the separate command `./brain --json source snapshot-url --url "$url" --description "$description" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`. Do not combine `--source-id` and `--url`. Each successful command saves immutable bytes beneath `_web` and returns its exact `SnapshotResult` plus current typed handoffs.
6. Branch on every returned handoff. If `handoff.kind == "extraction"`, create Markdown only beneath `.brain/agent-staging/<handoff-id>/...` and run `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. If `handoff.kind == "rendered_web_capture"`, use only this approved event to save a faithful serialized DOM, print-to-PDF, or downloaded claim-bearing artifact beneath `.brain/web-staging/<handoff-id>/...`, then run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`, appending one `--redirect-url "$redirect_url"` per observed redirect. Never send a `rendered_web_capture` handoff to `register-extraction`.
7. Do not cite browser text. Verify each final `SnapshotResult.active_representation` is non-null, matches the result's source/content identity and current corpus revision, and has resolving original, extraction, and anchors. Route any newly emitted typed handoff through the same branch. Unused candidates and search-result snippets are not persisted.
8. Return to local research and start exactly these three logical runs once each and in order, repeating `--term` within an initial call when needed:

```bash
./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3
./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3
./brain --json search --scope sources --pass verification --term "$verification_term" --context 3
```

These are **Pass 1**, **Pass 2**, and **Pass 3** against the new corpus revision. For each, call `./brain --json search --cursor "$next_cursor"` until `complete: true` before deriving or starting the next pass. Stop partial/unanswered if any continuation fails.
9. Persist and cite only the new local evidence through the curator's staged wiki-apply transaction after all link-candidate runs are fully drained. Report capture failures and do not use their claims.

Ask again before a materially expanded scope, a later research event, software installation, or an allowlist change.
````

- [ ] **Step 4: Write the wiki-maintenance and validation skills**

```markdown
---
name: brain-wiki-maintenance
description: Use when the user asks to create, update, link, rename, reconcile, merge, split, or remove wiki pages or question records.
---

# Maintain the wiki

Read `docs/brain/policies/wiki.md`, `docs/brain/policies/citations.md`, and `docs/brain/workflows/wiki-maintenance.md`.

- Require at least one useful sourced statement before creating a page. Do not create empty or alias-only stubs.
- Parse Markdown structure. Never use raw global string replacement. Link only the first meaningful occurrence of a topic in each section; reject self-links and ambiguous aliases.
- For every proposed write/delete, start one `./brain --json links candidates "$changed_page" --term "$term"` with every relevant term repeated in that command. Drain its pages only with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`; stop on stale, expired, tampered, or incomplete output. Read every matching context, choose only genuine relationships, and update every declared page/question relationship reciprocally in the staged files.
- Preserve meaningful question refinements and both sides of sourced contradictions.
- Ask before deleting, merging, or materially splitting pages; **Removing a sourced claim**; choosing a materially contradictory interpretation; a major uncertain rewrite; or an ambiguous rename.
- Write proposed pages/questions only beneath `.brain/wiki-staging/<run-id>/files/`, mirroring each full `wiki/pages/...` or `wiki/questions/...` logical target. Never write directly to `wiki/`. Calculate each staged write's SHA-256, create the exact version-1 manifest with the current expected corpus revision, intent, approval event ID when required, any pending citation rewrites, sorted `link_candidate_runs` proofs covering every write/delete, and the complete write/delete set, then invoke `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`.
- After an approved removal or rename, search and reconcile every affected page/question in the same staged transaction, then run `./brain --json links check` and `./brain --json validate`.
```

````markdown
---
name: brain-validate
description: Use before claiming a Second Brain handoff, completed conversation, or pull request is ready.
---

# Validate the brain

Read `docs/brain/workflows/validate.md` and run focused tests first. Then run:

```bash
python3 -m pytest -v
./brain --json validate --full
```

Inspect the structured report, not exit code alone. Completion requires no unexplained source gap, stale extraction, integrity error, unresolved citation or anchor, broken/one-way relationship, duplicate ID/slug, invalid skill link, or adapter drift. Warnings are acceptable only when their limitation is explicitly carried into the answer or handoff. This skill reports and verifies; it does not infer approval for destructive repair and never commits automatically.
````

- [ ] **Step 5: Run all canonical-skill tests**

Run: `python3 -m pytest tests/integration/test_core_skills.py tests/integration/test_guardrail_skills.py -v`

Expected: PASS.

- [ ] **Step 6: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors. Do not commit.

---

### Task 5: Wire Claude skill links and thin Codex/Claude agent adapters

**Files:**

- Create symlink: `.claude/skills/brain-initialize` -> `../../.agents/skills/brain-initialize`
- Create symlink: `.claude/skills/brain-answer` -> `../../.agents/skills/brain-answer`
- Create symlink: `.claude/skills/brain-web-research` -> `../../.agents/skills/brain-web-research`
- Create symlink: `.claude/skills/brain-wiki-maintenance` -> `../../.agents/skills/brain-wiki-maintenance`
- Create symlink: `.claude/skills/brain-validate` -> `../../.agents/skills/brain-validate`
- Create: `.codex/agents/source-researcher.toml`
- Create: `.codex/agents/source-ingester.toml`
- Create: `.codex/agents/wiki-curator.toml`
- Create: `.codex/agents/brain-auditor.toml`
- Create: `.claude/agents/source-researcher.md`
- Create: `.claude/agents/source-ingester.md`
- Create: `.claude/agents/wiki-curator.md`
- Create: `.claude/agents/brain-auditor.md`
- Create: `tests/integration/test_client_adapters.py`

**Interfaces:**

- Consumes: canonical skill names and role brief paths.
- Produces: native discovery entries for Codex and Claude Code with no duplicated policy or model pinning.

- [ ] **Step 1: Write failing cross-client topology and thinness tests**

```python
# tests/integration/test_client_adapters.py
from pathlib import Path
import tomllib

from brainlib.instructions import parse_scalar_frontmatter

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ("brain-initialize", "brain-answer", "brain-web-research", "brain-wiki-maintenance", "brain-validate")
AGENTS = ("source-researcher", "source-ingester", "wiki-curator", "brain-auditor")


def test_claude_skill_links_are_relative_and_resolve_to_canonical_skills() -> None:
    for name in SKILLS:
        link = ROOT / ".claude/skills" / name
        assert link.is_symlink()
        assert link.readlink() == Path("../../.agents/skills") / name
        assert link.resolve() == (ROOT / ".agents/skills" / name).resolve()


def test_codex_agents_only_route_to_shared_briefs() -> None:
    for name in AGENTS:
        path = ROOT / ".codex/agents" / f"{name}.toml"
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        assert value["name"] == name
        assert value["description"] == CLAUDE_DESCRIPTIONS[name]
        assert f"docs/brain/agent-briefs/{name}.md" in value["developer_instructions"]
        assert "model" not in value
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 12


def test_claude_agents_only_route_to_shared_briefs() -> None:
    for name in AGENTS:
        path = ROOT / ".claude/agents" / f"{name}.md"
        metadata, body = parse_scalar_frontmatter(path)
        text = path.read_text(encoding="utf-8")
        assert metadata == {"name": name, "description": CLAUDE_DESCRIPTIONS[name]}
        assert f"docs/brain/agent-briefs/{name}.md" in text
        assert "model:" not in text
        assert body.strip()
        assert len(text.splitlines()) <= 12


CLAUDE_DESCRIPTIONS = {
    "source-researcher": "Searches the active local corpus in three passes and returns a cited evidence packet without writing files.",
    "source-ingester": "Creates and registers faithful searchable representations for exact source-ingestion handoffs.",
    "wiki-curator": "Stages and atomically applies grounded Q&A and wiki changes with immutable citations and reciprocal links.",
    "brain-auditor": "Independently audits coverage, evidence, citations, graph integrity, routing, and approvals without writing files.",
}
```

- [ ] **Step 2: Run the tests and confirm missing-link/adapter failures**

Run: `python3 -m pytest tests/integration/test_client_adapters.py -v`

Expected: FAIL because the links and adapters do not exist.

- [ ] **Step 3: Create the five relative skill symlinks**

Use `ln -s` only for creating these new links; do not replace an existing non-symlink without stopping for review:

```bash
ln -s ../../.agents/skills/brain-initialize .claude/skills/brain-initialize
ln -s ../../.agents/skills/brain-answer .claude/skills/brain-answer
ln -s ../../.agents/skills/brain-web-research .claude/skills/brain-web-research
ln -s ../../.agents/skills/brain-wiki-maintenance .claude/skills/brain-wiki-maintenance
ln -s ../../.agents/skills/brain-validate .claude/skills/brain-validate
```

- [ ] **Step 4: Create the four Codex adapters**

Write all four exact files below. Read-only roles explicitly request read-only sandboxing, while writers inherit the workspace policy so the host remains authoritative:

```toml
# .codex/agents/source-researcher.toml
name = "source-researcher"
description = "Searches the active local corpus in three passes and returns a cited evidence packet without writing files."
sandbox_mode = "read-only"
developer_instructions = """
Read and follow docs/brain/agent-briefs/source-researcher.md completely. Treat it as canonical. Return its required output contract and stop at its stop conditions.
"""
```

```toml
# .codex/agents/source-ingester.toml
name = "source-ingester"
description = "Creates and registers faithful searchable representations for exact source-ingestion handoffs."
developer_instructions = """
Read and follow docs/brain/agent-briefs/source-ingester.md completely. Treat it as canonical. Do not write outside the brief's source-artifact scope.
"""
```

```toml
# .codex/agents/wiki-curator.toml
name = "wiki-curator"
description = "Stages and atomically applies grounded Q&A and wiki changes with immutable citations and reciprocal links."
developer_instructions = """
Read and follow docs/brain/agent-briefs/wiki-curator.md completely. Treat it as canonical. Stage proposals and use ./brain --json wiki apply --manifest .brain/wiki-staging/RUN_ID/manifest.json; never write live wiki files directly.
"""
```

```toml
# .codex/agents/brain-auditor.toml
name = "brain-auditor"
description = "Independently audits coverage, evidence, citations, graph integrity, routing, and approvals without writing files."
sandbox_mode = "read-only"
developer_instructions = """
Read and follow docs/brain/agent-briefs/brain-auditor.md completely. Treat it as canonical. Return findings rather than repairing them.
"""
```

- [ ] **Step 5: Create all four complete Claude adapters**

Write each file exactly as shown; do not replace the frontmatter or body with a shared-template note.

```markdown
---
name: source-researcher
description: Searches the active local corpus in three passes and returns a cited evidence packet without writing files.
---
Read and follow `docs/brain/agent-briefs/source-researcher.md` completely. It is canonical. Return its required output and stop at its stop conditions.
```

```markdown
---
name: source-ingester
description: Creates and registers faithful searchable representations for exact source-ingestion handoffs.
---
Read and follow `docs/brain/agent-briefs/source-ingester.md` completely. It is canonical. Do not write outside its source-artifact scope.
```

```markdown
---
name: wiki-curator
description: Stages and atomically applies grounded Q&A and wiki changes with immutable citations and reciprocal links.
---
Read and follow `docs/brain/agent-briefs/wiki-curator.md` completely. It is canonical. Do not write outside its wiki-artifact scope.
```

```markdown
---
name: brain-auditor
description: Independently audits coverage, evidence, citations, graph integrity, routing, and approvals without writing files.
---
Read and follow `docs/brain/agent-briefs/brain-auditor.md` completely. It is canonical. Return findings rather than repairing them.
```

- [ ] **Step 6: Run adapter and instruction-topology tests**

Run: `python3 -m pytest tests/integration/test_client_adapters.py tests/unit/test_instructions.py -v`

Expected: PASS, and `validate_instruction_architecture()` has no topology issue once all adapters exist.

- [ ] **Step 7: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: symlinks appear with mode `120000`; no copied `SKILL.md` exists beneath `.claude/skills/`. Do not commit.

---

### Task 6: Finalize root routing, the operating manual, and the template README

**Files:**

- Modify: `AGENTS.md`
- Modify: `BRAIN.md`
- Create: `README.md`
- Modify: `docs/brain/workflows/initialize.md`
- Modify: `docs/brain/workflows/synchronize.md`
- Create: `tests/integration/test_user_journey_docs.py`

**Interfaces:**

- Consumes: all finished commands, skills, policies, workflows, and adapters.
- Produces: concise routing for agents and an end-to-end setup path for a user cloning the empty template.

- [ ] **Step 1: Write failing routing and user-journey tests**

```python
# tests/integration/test_user_journey_docs.py
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_agents_routes_six_request_classes_without_copying_policies() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in (
        "initialize this brain", "substantive knowledge question", "public-web research",
        "wiki maintenance", "handoff or PR", "repository development",
    ):
        assert phrase in text
    assert len(text.splitlines()) <= 90
    assert "CLAUDE.md" in text


def test_readme_documents_complete_empty_template_journey() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    ordered = (
        "Create a private repository",
        "sources/raw/",
        "initialize this brain",
        "dependency",
        "sources/ledger.md",
        "Ask a question",
        "Add new sources",
        "validate --full",
    )
    offsets = [text.index(item) for item in ordered]
    assert offsets == sorted(offsets)
    for warning in ("ordinary Git objects", "file-size limits", "Git history", "public web", "one logical commit"):
        assert warning in text
    assert "does not reconvert unchanged sources" in text
    assert "Commit `sources/raw/`, `sources/extracted/`, `sources/ledger/`, `sources/ledger.md`, and `wiki/`" in text
    for command in (
        "./brain --json doctor", "./brain --json init", "./brain --json sync",
        "./brain --json status", "./brain --json search --scope wiki --term",
        "./brain --json search --scope sources --pass discovery --term",
        "./brain --json search --cursor",
        "./brain --json source snapshot-url --source-id",
        "./brain --json source snapshot-url --url",
        "--rendered-staging-path",
        "./brain --json source adopt-version", "./brain --json source register-extraction",
        "./brain --json links candidates", "./brain --json links candidates --cursor", "./brain --json links check",
        "./brain --json wiki apply --manifest", "./brain --json validate --full",
    ):
        assert command in text
    assert "Show `doctor`" not in text


def test_readme_documents_the_committed_extractor_allowlist_without_drift() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    registry = tomllib.loads((ROOT / "config/extractors.toml").read_text(encoding="utf-8"))
    for extractor in registry["extractors"]:
        assert f"`{extractor['id']}`" in readme
        for extension in extractor["extensions"]:
            assert f"`{extension}`" in readme
    assert "`config/extractors.toml` is authoritative" in readme
    assert "Never edit the allowlist without explicit approval" in readme
    for strategy in (
        "Built-in UTF-8 Markdown/text normalization",
        "Built-in deterministic Markdown tables",
        "Built-in sorted JSON Markdown representation",
        "Pandoc → standard-library HTML parser",
        "`pdftotext -layout` → PyMuPDF",
        "Pandoc → python-docx",
        "python-pptx → isolated headless LibreOffice",
        "openpyxl → isolated headless LibreOffice",
        "Tesseract for text-focused images → approved agent handoff for complex images",
        "Explicitly approved immutable web capture → normal extraction",
    ):
        assert strategy in readme


def test_brain_manual_links_every_canonical_document() -> None:
    text = (ROOT / "BRAIN.md").read_text(encoding="utf-8")
    for relative in (
        "docs/brain/policies/source-handling.md",
        "docs/brain/policies/approvals.md",
        "docs/brain/policies/citations.md",
        "docs/brain/policies/wiki.md",
        "docs/brain/policies/web.md",
        "docs/brain/workflows/initialize.md",
        "docs/brain/workflows/synchronize.md",
        "docs/brain/workflows/answer.md",
        "docs/brain/workflows/web-research.md",
        "docs/brain/workflows/wiki-maintenance.md",
        "docs/brain/workflows/validate.md",
        "docs/brain/schemas/README.md",
        "docs/brain/schemas/source-record.v1.schema.json",
        "docs/brain/schemas/page-frontmatter.v1.schema.json",
        "docs/brain/schemas/question-frontmatter.v1.schema.json",
        "docs/brain/schemas/citation.md",
        "docs/brain/schemas/evidence-packet.md",
        "docs/brain/schemas/wiki-page.md",
        "docs/brain/schemas/question-record.md",
        "docs/brain/agent-briefs/source-researcher.md",
        "docs/brain/agent-briefs/source-ingester.md",
        "docs/brain/agent-briefs/wiki-curator.md",
        "docs/brain/agent-briefs/brain-auditor.md",
    ):
        assert relative in text
    for heading in ("## Authority", "## Directory roles", "## Evidence lifecycle", "## Question lifecycle", "## Approval boundary", "## Canonical document index"):
        assert heading in text


def test_initialize_and_sync_docs_state_the_agent_and_network_boundaries() -> None:
    initialize = (ROOT / "docs/brain/workflows/initialize.md").read_text(encoding="utf-8")
    synchronize = (ROOT / "docs/brain/workflows/synchronize.md").read_text(encoding="utf-8")
    assert "## Agent completion boundary" in initialize
    assert "./brain --json init" in initialize
    assert "`needs_agent`" in initialize
    assert "## Network and citation-rewrite boundary" in synchronize
    assert "./brain --json sync" in synchronize
    assert "never performs network access" in synchronize
    assert "citation_rewrites" in synchronize
```

- [ ] **Step 2: Run the tests and confirm they fail against the thin foundation docs**

Run: `python3 -m pytest tests/integration/test_user_journey_docs.py -v`

Expected: FAIL for missing routes, journey text, or document links.

- [ ] **Step 3: Replace `AGENTS.md` with concise deterministic routing**

Its operative body must be exactly this policy shape; retain only a short project introduction above it:

```markdown
## Route every request

- “initialize this brain,” first-time ingestion, or resume: load and follow the `brain-initialize` skill.
- A substantive knowledge question: load and follow the `brain-answer` skill. Synchronization is part of that skill.
- For public-web research after local insufficiency, load and follow the `brain-web-research` skill; it must ask before access.
- For wiki maintenance—manual page, link, rename, contradiction, merge, split, or removal work—load and follow the `brain-wiki-maintenance` skill.
- Completed handoff or PR readiness: load and follow the `brain-validate` skill and require `./brain --json validate --full`.
- A repository development request—CLI, tests, docs, schemas, skills, or architecture—uses normal software-development instructions and must not become a knowledge Q&A record.

## Authority

`AGENTS.md` is canonical and `CLAUDE.md` must be its symlink. `BRAIN.md` indexes detailed rules. Deterministic CLI output owns checksums, IDs, ledger state, and validation; agents own semantic judgment. Never bypass an approval gate, invent provenance, auto-commit, or hide a coverage gap.
```

- [ ] **Step 4: Write the complete README user journey and safety notes**

Write `README.md` exactly from the following complete starting content; project-specific clarification may be added, but no heading, command, safety rule, or allowlist row may be omitted:

````markdown
# Second Brain Lite

Second Brain Lite is a Git-native, source-grounded personal wiki operated through Codex or Claude Code. It uses ordinary files and `rg`; it has no database, vector service, or daemon.

## Start a brain

1. Create a private repository from this template.
2. Put originals beneath `sources/raw/` without changing the reserved `_versions/` or `_web/` directories.
3. Tell Codex or Claude Code: “initialize this brain.”
4. Review any dependency installation or initial URL approval request.
5. Inspect `sources/ledger.md`; full initialization has no unexplained pending or agent work.

## Ask a question

Ask normally. The agent synchronizes new sources, searches the wiki, runs three widening `rg` passes when needed, persists one evolving topic record and qualifying pages, and validates citations and links.

## Add new sources

Copy them into `sources/raw/`. The next substantive question runs incremental synchronization: it inventories against the maintained ledger, processes eligible new or changed inputs, and does not reconvert unchanged sources. Use `./brain --json status` at any time.

## Approved extraction allowlist

`config/extractors.toml` is authoritative. Never edit the allowlist without explicit approval. `./brain --json doctor` reports which selected implementation is installed and prints the repository-defined installation recipes; it never installs anything.

| Registry ID | Extensions | Preferred → fallback behavior |
|---|---|---|
| `text` | `.txt`, `.md`, `.markdown` | Built-in UTF-8 Markdown/text normalization |
| `tabular` | `.csv`, `.tsv` | Built-in deterministic Markdown tables |
| `json` | `.json`, `.jsonl`, `.ndjson` | Built-in sorted JSON Markdown representation |
| `html` | `.html`, `.htm`, `.xhtml` | Pandoc → standard-library HTML parser |
| `pdf` | `.pdf` | `pdftotext -layout` → PyMuPDF |
| `docx` | `.docx` | Pandoc → python-docx |
| `pptx` | `.pptx` | python-pptx → isolated headless LibreOffice |
| `xlsx` | `.xlsx` | openpyxl → isolated headless LibreOffice |
| `image` | `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.webp` | Tesseract for text-focused images → approved agent handoff for complex images |
| `webpage` | `.url.md` | Explicitly approved immutable web capture → normal extraction |

## Direct CLI use

Run commands from the repository root:

```bash
./brain --json doctor
./brain --json init
./brain --json sync
./brain --json status
./brain --json search --scope wiki --term "Alpha"
./brain --json search --scope sources --pass discovery --term "Alpha" --context 3
./brain --json search --scope sources --pass expansion --term "Beta relationship" --context 3
./brain --json search --scope sources --pass verification --term "Alpha exception" --context 3
./brain --json search --cursor OPAQUE_TOKEN
./brain --json source snapshot-url --source-id src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source snapshot-url --url https://example.com/report --description "Example report" --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source snapshot-url --source-id src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --rendered-staging-path .brain/web-staging/hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/page.html --handoff-id hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --retrieved-at 2026-09-04T12:00:00Z --final-url https://example.com/report --detected-media-type text/html --approval-event-id evt_20260904_q1 --approval-scope q1 --approval-note "user approved this bounded capture"
./brain --json source adopt-version src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --candidate-sha256 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --approval-note "user approved this source replacement"
./brain --json source register-extraction --handoff-id hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --staging-path .brain/agent-staging/hnd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/output.md --anchors-json '[{"kind":"page","value":"1"}]' --quality-state ok --note "faithful agent extraction"
./brain --json links candidates wiki/pages/alpha.md --term "Alpha"
./brain --json links candidates --cursor OPAQUE_TOKEN
./brain --json links check
./brain --json wiki apply --manifest .brain/wiki-staging/example/manifest.json
./brain --json wiki recover
./brain --json validate --full
```

Direct `./brain --json init` cannot drain `needs_agent` work; use “initialize this brain” for the full agent-assisted workflow. A handoff is typed: extraction uses `register-extraction`, while rendered web capture uses the rendered `snapshot-url` form and must yield a non-null `SnapshotResult.active_representation`. The three source research commands start exactly one discovery, one expansion, and one verification logical run; repeat `--term` within the initial invocation and drain its opaque cursor before starting the next pass.

## Privacy, Git, symlinks, and size

Raw, extracted, ledger, and wiki files are ordinary Git objects; this template does not use Git LFS. Commit `sources/raw/`, `sources/extracted/`, `sources/ledger/`, `sources/ledger.md`, and `wiki/` with the brain so immutable evidence, searchable representations, machine/human ledger state, and synthesized knowledge travel together. Recommend a private remote, warn about provider file-size limits, and explain that deleting current files does not erase private data from Git history. State that Windows checkout needs Developer Mode with Git symlink support or WSL; `./brain --json validate` rejects a text-file copy of `CLAUDE.md` or a copied `.claude/skills` directory.

## Approval boundaries

Ask before public web access, dependency installation, extractor allowlist changes, source mutation/adoption, destructive wiki edits, sourced-claim removal, and materially contradictory interpretations. Every used public source is saved, extracted, ledgered, and cited locally; public web browsing never occurs silently. Unused candidates are not persisted.

## Development and handoff

The repository does not infer sessions or auto-commit. Keep one logical commit for a completed conversation or PR through the chosen host/squash workflow. Run `python3 -m pytest -v` and `./brain --json validate --full` before handoff.
````

- [ ] **Step 5: Complete `BRAIN.md` and reconcile initialization/sync wording**

Replace `BRAIN.md` with this complete manual (the links, lifecycle, boundaries, and command order are normative):

```markdown
# Second Brain operating manual

## Authority

`AGENTS.md` is the concise routing authority and `CLAUDE.md` is a relative symlink to it. This manual indexes the detailed repository-owned policies, workflows, schemas, and shared agent briefs. Deterministic `./brain` output owns IDs, checksums, corpus revision, ledger state, staged publication, and validation. Agents own term generation, evidence interpretation, page qualification, relationship judgment, and asking for approvals. A client adapter may route to a shared brief but may not redefine it.

## Directory roles

- `sources/raw/` contains authoritative user originals, immutable `_versions/`, and approved `_web/` snapshots.
- `sources/extracted/` contains committed, versioned Markdown representations that `rg` can search.
- `sources/ledger/` and `sources/ledger.md` contain canonical machine records and their generated human summary.
- `wiki/questions/` contains one evolving record per stable topic; `wiki/pages/` contains evidence-backed atomic pages; `wiki/index.md` is generated.
- `.brain/agent-staging/` and `.brain/wiki-staging/` contain temporary agent proposals. Only the CLI may promote them to canonical source or wiki paths.
- `.agents/skills/` contains canonical client-independent workflows. `.claude/skills/` contains only relative symlinks to them.
- `docs/brain/agent-briefs/` contains canonical semantic roles. `.codex/agents/` and `.claude/agents/` are thin discovery adapters.

## Evidence lifecycle

`sources/raw` authoritative bytes
→ one stable logical source ID
→ SHA-256 content version
→ versioned searchable derivation with `method` and immutable `method_metadata`
→ validated wiki packet or completed three-pass source packet
→ claim-level citation
→ evolving Q&A and atomic wiki pages

The ledger records mechanics; the wiki records supported interpretation. Deterministic derivations have `method: deterministic` and exact converter ID/version metadata. Agent derivations have `method: agent` and exact `{handoff_id, agent_revision, note}` metadata derived from the immutable handoff; callers never choose provenance fields. A citation is valid only while both the exact original version and exact derivation resolve.

## Question lifecycle

For a substantive knowledge question, run `./brain --json sync`; verify and fully stream its durable result manifest, durably deduplicate/apply its `result_id`, then explicitly acknowledge that ID; complete eligible typed handoffs and rerun sync after every ingestion mutation until stable; accumulate exact rewrites and newly active source IDs across the loop; reconcile citations through `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`; run one batched freshness probe for those IDs; and drain one wiki search. Revalidate its underlying citations into a `WikiEvidencePacket` before judging revision, completeness, and contradiction sufficiency. If insufficient, run exactly one discovery, one expansion, and one verification logical source pass. Drain every opaque cursor before starting the next pass; incomplete runs produce partial/unanswered status. Give only the validated wiki packet or completed three-pass packet to the curator. Both paths create/update one evolving topic Q&A. The curator writes only `.brain/wiki-staging/`, drains link-candidate runs for every mutation, publishes through `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`, and then runs `./brain --json links check` and `./brain --json validate`. Repository-development and incidental conversational requests are not archived.

## Approval boundary

Routine grounded additions may proceed. Ask first for public-web access, dependency installation, extractor-allowlist changes, source-byte adoption, delete/merge/material split, sourced-claim or relationship removal, materially contradictory interpretation, major uncertain rewrite, or ambiguous rename. Put the approval event ID in the command or wiki manifest that consumes it. Approval is bounded to the stated event and scope. The repository never installs, browses, changes authoritative source versions, performs destructive wiki work, or commits automatically.

## Canonical document index

Policies:

- [Source handling](docs/brain/policies/source-handling.md)
- [Approvals](docs/brain/policies/approvals.md)
- [Citations](docs/brain/policies/citations.md)
- [Wiki](docs/brain/policies/wiki.md)
- [Public web](docs/brain/policies/web.md)

Workflows:

- [Initialize](docs/brain/workflows/initialize.md)
- [Synchronize](docs/brain/workflows/synchronize.md)
- [Answer](docs/brain/workflows/answer.md)
- [Web research](docs/brain/workflows/web-research.md)
- [Wiki maintenance](docs/brain/workflows/wiki-maintenance.md)
- [Validate](docs/brain/workflows/validate.md)

Schemas:

- [Schema index](docs/brain/schemas/README.md)
- [Source record v1](docs/brain/schemas/source-record.v1.schema.json)
- [Page frontmatter v1](docs/brain/schemas/page-frontmatter.v1.schema.json)
- [Question frontmatter v1](docs/brain/schemas/question-frontmatter.v1.schema.json)
- [Citation grammar](docs/brain/schemas/citation.md)
- [Evidence packet](docs/brain/schemas/evidence-packet.md)
- [Wiki page](docs/brain/schemas/wiki-page.md)
- [Question record](docs/brain/schemas/question-record.md)

Shared role briefs:

- [Source researcher](docs/brain/agent-briefs/source-researcher.md)
- [Source ingester](docs/brain/agent-briefs/source-ingester.md)
- [Wiki curator](docs/brain/agent-briefs/wiki-curator.md)
- [Brain auditor](docs/brain/agent-briefs/brain-auditor.md)
```

Append this exact section to `docs/brain/workflows/initialize.md`:

```markdown
## Agent completion boundary

`./brain --json init` inventories and completes only eligible deterministic work. If its structured report contains `needs_agent`, `awaiting_approval`, pending, failed, unsupported, warning, or integrity gaps, direct CLI initialization is not full initialization. The `brain-initialize` skill branches on every current handoff: `extraction` uses exact handoff-scoped agent staging and `register-extraction`; `rendered_web_capture` uses an approved faithful browser artifact and rendered `snapshot-url`, never registration. It asks at every approval boundary, requires every used `SnapshotResult.active_representation`, reruns `./brain --json init` until eligible work is exhausted, and finishes with `./brain --json status` plus `./brain --json validate --full`. Pytest belongs to implementation/PR validation, not normal initialization. Neither path may hide a remaining gap.
```

Append this exact section to `docs/brain/workflows/synchronize.md`:

```markdown
## Network and citation-rewrite boundary

`./brain --json sync` never performs network access. URL descriptors remain `awaiting_approval` until the separate approved snapshot command. When ingestion changes state, rerun sync until stable; for every response verify and fully stream the referenced manifest, durably deduplicate/apply its `result_id`, explicitly acknowledge it, and accumulate the sorted union of exact rewrite/new-active events. Before searching or judging wiki sufficiency, apply that rewrite union—even when empty—through `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`, then run one batched freshness search containing all accumulated new-active IDs and terms. Drain its opaque cursor until complete. Bounded response samples are never a completeness source. Sync does not itself perform semantic research or live wiki edits.
```

- [ ] **Step 6: Run the user-journey and full documentation tests**

Run: `python3 -m pytest tests/integration/test_user_journey_docs.py tests/integration/test_operating_docs.py -v`

Expected: PASS.

- [ ] **Step 7: Record a non-committing task checkpoint**

Run: `git diff --check && git status --short`

Expected: no whitespace errors. Do not commit.

---

### Task 7: Add versioned cross-client LLM workflow evaluation scenarios

**Files:**

- Create: `tests/evals/README.md`
- Create: `tests/evals/__init__.py`
- Create: `tests/evals/scenario.v1.schema.json`
- Create: `tests/evals/event-log.v1.schema.json`
- Create: `tests/evals/scenario_contract.py`
- Create: `tests/evals/event_log_contract.py`
- Create: `tests/evals/generate_scenarios.py`
- Create: `tests/evals/run_cross_client.py`
- Create: `tests/evals/scenarios/empty-wiki-first-question.json`
- Create: `tests/evals/scenarios/current-wiki-fast-path.json`
- Create: `tests/evals/scenarios/new-binary-before-question.json`
- Create: `tests/evals/scenarios/web-approval-and-capture.json`
- Create: `tests/evals/scenarios/contradictory-evidence.json`
- Create: `tests/evals/scenarios/repository-development-not-archived.json`
- Create: `tests/evals/test_scenario_contracts.py`
- Create: `tests/evals/test_cross_client_runs.py`
- Create (generated): `tests/evals/fixtures/<scenario-id>/fixture-manifest.json` and `repo/` for each of the six scenario IDs
- Create (actual-run artifacts): `tests/evals/runs/codex/empty-wiki-first-question.event-log.json`, `current-wiki-fast-path.event-log.json`, `new-binary-before-question.event-log.json`, `web-approval-and-capture.event-log.json`, `contradictory-evidence.event-log.json`, and `repository-development-not-archived.event-log.json`
- Create (actual-run artifacts): the same six filenames under `tests/evals/runs/claude/`

**Interfaces:**

- Consumes: canonical skill names, CLI trace vocabulary, expected repository mutations, and approval rules.
- Produces: six generated synthetic-corpus fixtures; schema-versioned scenario contracts; normalized event-log validation; an isolation/launch/normalization runner; and twelve required event logs from actual Codex and Claude executions.

- [ ] **Step 1: Write failing scenario-contract tests**

```python
# tests/evals/test_scenario_contracts.py
import json
from pathlib import Path

import pytest

from tests.evals.generate_scenarios import SCENARIOS as SCENARIO_DATA, rendered_scenarios
from tests.evals.scenario_contract import ScenarioContractError, validate_against_schema

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "tests/evals/scenarios"
REQUIRED_IDS = {
    "empty-wiki-first-question",
    "current-wiki-fast-path",
    "new-binary-before-question",
    "web-approval-and-capture",
    "contradictory-evidence",
    "repository-development-not-archived",
}


def load() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(SCENARIO_DIR.glob("*.json"))]


def test_all_required_scenarios_are_versioned_and_complete() -> None:
    schema = json.loads((ROOT / "tests/evals/scenario.v1.schema.json").read_text(encoding="utf-8"))
    scenarios = load()
    assert {item["id"] for item in scenarios} == REQUIRED_IDS
    for item in scenarios:
        validate_against_schema(item, schema)


def test_checked_in_scenarios_are_exact_generator_output() -> None:
    expected = rendered_scenarios()
    actual = {path.name: path.read_text(encoding="utf-8") for path in sorted(SCENARIO_DIR.glob("*.json"))}
    assert actual == expected
    assert {item["id"] for item in SCENARIO_DATA} == REQUIRED_IDS


def test_schema_rejects_extra_keys_bad_ids_and_duplicate_events() -> None:
    schema = json.loads((ROOT / "tests/evals/scenario.v1.schema.json").read_text(encoding="utf-8"))
    base = dict(load()[0])
    invalid_values = (
        {**base, "unexpected": True},
        {**base, "id": "Bad ID"},
        {**base, "required_events": ["sync", "sync"]},
    )
    for value in invalid_values:
        with pytest.raises(ScenarioContractError):
            validate_against_schema(value, schema)


def test_web_scenario_orders_approval_before_access_and_capture_before_claim() -> None:
    item = next(value for value in load() if value["id"] == "web-approval-and-capture")
    events = item["required_events"]
    assert events.index("ask_web_approval") < events.index("public_web_access")
    assert events.index("snapshot_used_source") < events.index("source_pass_discovery")
    assert events.index("source_pass_verification") < events.index("persist_claim")


def test_sync_scenarios_reconcile_citations_before_wiki_sufficiency() -> None:
    for item in load():
        if "sync" not in item["required_events"]:
            continue
        events = item["required_events"]
        assert events.index("sync") < events.index("wiki_reconcile_citations")
        assert events.index("wiki_reconcile_citations") < events.index("wiki_search_drained")


def test_repository_development_scenario_forbids_knowledge_mutation() -> None:
    item = next(value for value in load() if value["id"] == "repository-development-not-archived")
    assert "write_wiki_question" in item["forbidden_events"]
    assert "write_wiki_page" in item["forbidden_events"]


def test_current_wiki_fast_path_still_revalidates_and_updates_one_question() -> None:
    item = next(value for value in load() if value["id"] == "current-wiki-fast-path")
    events = item["required_events"]
    assert events.index("build_wiki_evidence_packet") < events.index("judge_sufficient")
    assert events.index("judge_sufficient") < events.index("stage_existing_question_update")
    assert events.index("stage_existing_question_update") < events.index("wiki_apply")
    assert "source_pass_discovery" in item["forbidden_events"]
    assert "public_web_access" in item["forbidden_events"]
    assert any("underlying citation" in value and "revalidated" in value for value in item["repository_assertions"])


def test_handoff_scenarios_require_kind_specific_routes() -> None:
    by_id = {item["id"]: item for item in load()}
    binary_events = by_id["new-binary-before-question"]["required_events"]
    web_events = by_id["web-approval-and-capture"]["required_events"]
    assert "branch_extraction_handoff" in binary_events
    assert "register_extraction_handoff" in binary_events
    assert "branch_rendered_web_capture_handoff" in web_events
    assert "rendered_snapshot_url" in web_events
    assert "verify_snapshot_active_representation" in web_events
    assert "register_rendered_handoff" in by_id["web-approval-and-capture"]["forbidden_events"]
```

- [ ] **Step 2: Run the test and confirm missing-scenario failure**

Run: `python3 -m pytest tests/evals/test_scenario_contracts.py -v`

Expected: FAIL because the scenario directory and files are absent.

- [ ] **Step 3: Define the strict version-1 scenario schema and its dependency-free validator**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://second-brain-lite.local/schemas/eval-scenario.v1.json",
  "title": "Second Brain Lite workflow evaluation scenario",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "id", "title", "skill", "prompt", "fixture_id", "network_mode", "approval_script", "fixture_setup", "required_events", "forbidden_events", "repository_assertions"],
  "properties": {
    "schema_version": {"const": 1},
    "id": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
    "title": {"type": "string", "minLength": 1},
    "skill": {"enum": ["brain-initialize", "brain-answer", "brain-web-research", "brain-wiki-maintenance", "brain-validate"]},
    "prompt": {"type": "string", "minLength": 1},
    "fixture_id": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
    "network_mode": {"enum": ["disabled", "mock_only"]},
    "approval_script": {"type": "array", "uniqueItems": true, "items": {"type": "string", "minLength": 1}},
    "fixture_setup": {"type": "array", "minItems": 3, "items": {"type": "string", "minLength": 1}},
    "required_events": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"type": "string", "minLength": 1}},
    "forbidden_events": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"type": "string", "minLength": 1}},
    "repository_assertions": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
  }
}
```

Create an empty `tests/evals/__init__.py`, then implement every keyword used by that exact schema rather than doing ad-hoc key checks:

```python
# tests/evals/scenario_contract.py
from __future__ import annotations

import argparse
import json
import re
from typing import Any


class ScenarioContractError(ValueError):
    pass


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]


def validate_against_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    if "type" in schema and not _is_type(value, schema["type"]):
        raise ScenarioContractError(f"{path}: expected {schema['type']}")
    if "const" in schema and value != schema["const"]:
        raise ScenarioContractError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ScenarioContractError(f"{path}: value is not in enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ScenarioContractError(f"{path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ScenarioContractError(f"{path}: string does not match pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ScenarioContractError(f"{path}: array has too few items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise ScenarioContractError(f"{path}: array items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_against_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        missing = required - value.keys()
        if missing:
            raise ScenarioContractError(f"{path}: missing {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = value.keys() - properties.keys()
            if extra:
                raise ScenarioContractError(f"{path}: unexpected {', '.join(sorted(extra))}")
        for key, child in value.items():
            if key in properties:
                validate_against_schema(child, properties[key], f"{path}.{key}")
```

- [ ] **Step 4: Add all six concrete evaluation scenarios**

Use one checked generator as the exact source for all six checked-in JSON files; do not hand-maintain a second summarized table:

```python
# tests/evals/generate_scenarios.py
from __future__ import annotations

import argparse
import json
from pathlib import Path


SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "schema_version": 1,
        "id": "empty-wiki-first-question",
        "title": "An empty wiki requires complete local research before its first answer",
        "skill": "brain-answer",
        "prompt": "What does the Alpha source say about the Beta relationship?",
        "fixture_id": "empty-wiki-first-question",
        "network_mode": "disabled",
        "approval_script": [],
        "fixture_setup": [
            "The ledger has one active cited text derivation about Alpha and Beta",
            "wiki/pages and wiki/questions contain only their committed sentinels",
            "The active corpus has no coverage gap and its revision is recorded",
        ],
        "required_events": [
            "sync", "wiki_reconcile_citations", "wiki_search_drained", "build_wiki_evidence_packet",
            "judge_insufficient", "source_pass_discovery", "source_pass_discovery_drained",
            "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification",
            "source_pass_verification_drained", "write_wiki_question", "write_qualifying_pages",
            "link_candidates_drained", "wiki_apply", "links_check", "validate",
        ],
        "forbidden_events": ["answer_without_citation", "skip_source_pass", "create_empty_stub"],
        "repository_assertions": [
            "Exactly one evolving Alpha question record exists",
            "Every factual answer passage has an exact resolving source-version-derivation citation",
            "Every qualifying Alpha or Beta page relationship is reciprocal",
        ],
    },
    {
        "schema_version": 1,
        "id": "current-wiki-fast-path",
        "title": "A current complete wiki answer uses the validated fast path",
        "skill": "brain-answer",
        "prompt": "Restate the documented Alpha decision for a risk review, preserving this new phrasing in the same topic record.",
        "fixture_id": "current-wiki-fast-path",
        "network_mode": "disabled",
        "approval_script": [],
        "fixture_setup": [
            "The Alpha question and page cover every material part of the prompt",
            "All claim citations resolve at the current corpus revision",
            "Sync returns no new active representations, rewrites, or coverage gaps",
        ],
        "required_events": [
            "sync", "wiki_reconcile_citations", "wiki_search_drained", "revalidate_underlying_citations",
            "build_wiki_evidence_packet", "judge_sufficient", "stage_existing_question_update",
            "link_candidates_drained", "wiki_apply", "links_check", "validate",
        ],
        "forbidden_events": [
            "source_pass_discovery", "source_pass_expansion", "source_pass_verification",
            "public_web_access", "duplicate_question_record", "direct_live_wiki_write",
        ],
        "repository_assertions": [
            "The existing question ID remains unique and its same file records the new risk-review phrasing",
            "The Q&A update is published by one wiki transaction while source and ledger content stay unchanged",
            "Every underlying citation is revalidated against the current ledger before WikiEvidencePacket sufficiency",
        ],
    },
    {
        "schema_version": 1,
        "id": "new-binary-before-question",
        "title": "A newly added binary is extracted before wiki sufficiency is judged",
        "skill": "brain-answer",
        "prompt": "Does the new quarterly PDF change the Alpha conclusion?",
        "fixture_id": "new-binary-before-question",
        "network_mode": "disabled",
        "approval_script": [],
        "fixture_setup": [
            "A valid allowlisted PDF exists beneath sources/raw but not in the ledger",
            "The existing wiki contains an older cited Alpha conclusion",
            "The selected deterministic PDF converter is represented by a controlled fixture",
        ],
        "required_events": [
            "sync", "detect_new_source", "branch_extraction_handoff", "stage_handoff_scoped_extraction",
            "register_extraction_handoff", "activate_representation", "wiki_reconcile_citations",
            "freshness_search_batched", "freshness_search_drained", "wiki_search_drained",
            "revalidate_underlying_citations", "judge_insufficient", "source_pass_discovery",
            "source_pass_discovery_drained", "source_pass_expansion", "source_pass_expansion_drained",
            "source_pass_verification", "source_pass_verification_drained", "write_wiki_question",
            "link_candidates_drained", "wiki_apply", "links_check", "validate",
        ],
        "forbidden_events": [
            "answer_before_ingestion", "hide_coverage_gap", "rehash_unchanged_corpus",
            "caller_controls_agent_revision", "rendered_handoff_registered_as_extraction",
        ],
        "repository_assertions": [
            "The new PDF has a ledgered content version and Markdown derivation before sufficiency judgment",
            "The freshness probe is scoped to the newly active source ID",
            "The old wiki answer is not accepted without considering the new representation",
        ],
    },
    {
        "schema_version": 1,
        "id": "web-approval-and-capture",
        "title": "Insufficient local evidence requires approval and durable capture",
        "skill": "brain-web-research",
        "prompt": "What changed in the external standard this year?",
        "fixture_id": "web-approval-and-capture",
        "network_mode": "mock_only",
        "approval_script": ["Approve only the mock standard URL after the agent states the local evidence gap and bounded scope."],
        "fixture_setup": [
            "Ledger and wiki contain only the prior standard",
            "Local three-pass research returns no current evidence",
            "A controllable web fixture exposes one used HTML page and one unused result",
        ],
        "required_events": [
            "report_local_evidence_gap", "ask_web_approval", "public_web_access",
            "select_used_source", "snapshot_used_source", "branch_rendered_web_capture_handoff",
            "stage_faithful_browser_capture", "rendered_snapshot_url", "verify_snapshot_active_representation",
            "source_pass_discovery", "source_pass_discovery_drained", "source_pass_expansion",
            "source_pass_expansion_drained", "source_pass_verification", "source_pass_verification_drained",
            "link_candidates_drained", "persist_claim",
        ],
        "forbidden_events": [
            "web_access_before_approval", "persist_unused_result", "cite_browser_text",
            "persist_claim_before_snapshot", "skip_renewed_source_pass", "register_rendered_handoff",
        ],
        "repository_assertions": [
            "Used page has an immutable sources/raw/_web version",
            "Unused result has no source record",
            "Claim citation resolves to the local source version and derivation",
            "Question record stores the post-capture corpus revision and three pass terms",
        ],
    },
    {
        "schema_version": 1,
        "id": "contradictory-evidence",
        "title": "Materially contradictory evidence is preserved and approval gated",
        "skill": "brain-answer",
        "prompt": "Which of the conflicting Alpha limits should I rely on?",
        "fixture_id": "contradictory-evidence",
        "network_mode": "disabled",
        "approval_script": ["Do not approve a preferred interpretation; require the answer to preserve both sourced limits."],
        "fixture_setup": [
            "Two active source versions state materially different dated Alpha limits",
            "Both derivations expose resolving anchors for their conflicting claims",
            "The existing Alpha question contains only the older limit",
        ],
        "required_events": [
            "sync", "wiki_reconcile_citations", "wiki_search_drained", "revalidate_underlying_citations",
            "judge_insufficient", "source_pass_discovery", "source_pass_discovery_drained",
            "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification",
            "source_pass_verification_drained", "preserve_both_claims", "cite_both_sides",
            "ask_interpretation_approval", "write_wiki_question", "link_candidates_drained",
            "wiki_apply", "links_check", "validate",
        ],
        "forbidden_events": ["silently_overwrite_claim", "choose_without_approval", "remove_old_citation"],
        "repository_assertions": [
            "The evolving question has conflicted status",
            "Both dated claims retain exact resolving citations",
            "No preferred interpretation is persisted without its approval event",
        ],
    },
    {
        "schema_version": 1,
        "id": "repository-development-not-archived",
        "title": "Repository development remains outside the knowledge archive",
        "skill": "brain-answer",
        "prompt": "Add a --dry-run flag to the sync command and test it.",
        "fixture_id": "repository-development-not-archived",
        "network_mode": "disabled",
        "approval_script": [],
        "fixture_setup": [
            "The repository has a valid initialized empty knowledge corpus",
            "The requested change concerns CLI source code and tests",
            "The wiki and question trees are clean before the development request",
        ],
        "required_events": ["classify_repository_development", "use_software_workflow"],
        "forbidden_events": ["invoke_brain_answer", "write_wiki_question", "write_wiki_page"],
        "repository_assertions": [
            "No wiki question file is added or changed",
            "No wiki page file is added or changed",
            "Only software-development paths requested by the task may differ",
        ],
    },
)


def rendered_scenarios() -> dict[str, str]:
    return {
        f"{item['id']}.json": json.dumps(item, indent=2, sort_keys=True) + "\n"
        for item in SCENARIOS
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    output_dir = Path(__file__).with_name("scenarios")
    expected = rendered_scenarios()
    actual = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(output_dir.glob("*.json"))
    }
    if arguments.check:
        if actual != expected:
            raise SystemExit("generated scenario files are stale; run with --write")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    unexpected = actual.keys() - expected.keys()
    if unexpected:
        raise SystemExit(f"refusing to remove unexpected scenarios: {sorted(unexpected)}")
    for filename, content in expected.items():
        (output_dir / filename).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
```

Generate the six versioned files, then let the equality test prevent drift:

```bash
python3 -m tests.evals.generate_scenarios --write
```

- [ ] **Step 5: Generate real synthetic corpus fixtures and define the normalized event-log contract**

Extend `generate_scenarios.py` so `--write` and `--check` are its only modes. It owns both the six scenario JSON files and six `tests/evals/fixtures/<scenario-id>/repo/` corpus overlays. Use fixed UTF-8 source bytes, a fixed `2026-09-04T12:00:00Z` timestamp, production identity functions, `SourceRecord.to_dict()`, `LedgerStore.write_summary()`, and `render_wiki_index()`; never copy illustrative IDs into a ledger. Each fixture manifest has exactly `schema_version`, `scenario_id`, `tree_sha256`, and sorted `paths`, where `tree_sha256` hashes the sequence `repo-relative POSIX path + NUL + file SHA-256 + NUL`.

The six builder profiles are exact: `empty-wiki-first-question` has one active Alpha/Beta text derivation and sentinel-only wiki; `current-wiki-fast-path` has one valid Alpha page and one current Q&A whose prior phrasings do not yet contain the risk-review wording; `new-binary-before-question` adds a fixed minimal valid quarterly PDF to `sources/raw/` without a ledger entry and uses the fixture's approved agent-fallback PDF registry revision; `web-approval-and-capture` has the prior standard locally, a mock initial static JavaScript shell that deterministically emits `rendered_web_capture`, a faithful mock browser-exported DOM containing the claim, and a separate unused candidate outside `repo/`; `contradictory-evidence` has two active, anchored, dated text derivations and an older one-sided Q&A; `repository-development-not-archived` has a valid empty corpus/wiki and clean software tree. The generator validates every finished overlay with production full source/wiki validation except the deliberate pre-sync unledgered PDF. `--check` generates in a temporary directory and compares the complete path set and bytes without modifying the worktree. There are no prose-only fixture steps and no current-time or public-network input.

Create `event-log.v1.schema.json` with this exact content:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://second-brain-lite.local/schemas/eval-event-log.v1.json",
  "title": "Second Brain Lite normalized cross-client event log",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "scenario_id", "client", "client_version", "fixture_sha256", "network_mode", "executions", "events", "repository_assertions", "result", "incomplete_reasons"],
  "properties": {
    "schema_version": {"const": 1},
    "scenario_id": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
    "client": {"enum": ["codex", "claude"]},
    "client_version": {"type": "string", "minLength": 1},
    "fixture_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "network_mode": {"enum": ["disabled", "mock_only"]},
    "executions": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["kind", "argv", "exit_code", "transcript_sha256"],
        "properties": {
          "kind": {"const": "actual_client_process"},
          "argv": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
          "exit_code": {"type": "integer"},
          "transcript_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        }
      }
    },
    "events": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["sequence", "name", "evidence_kind", "evidence"],
        "properties": {
          "sequence": {"type": "integer"},
          "name": {"type": "string", "minLength": 1},
          "evidence_kind": {"enum": ["command", "client_trace", "approval", "mock_web", "repository_diff", "validator"]},
          "evidence": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
        }
      }
    },
    "repository_assertions": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["text", "passed", "evidence"],
        "properties": {
          "text": {"type": "string", "minLength": 1},
          "passed": {"type": "boolean"},
          "evidence": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
        }
      }
    },
    "result": {"enum": ["pass", "fail", "incomplete"]},
    "incomplete_reasons": {"type": "array", "uniqueItems": true, "items": {"type": "string", "minLength": 1}}
  }
}
```

`event_log_contract.py` reuses `validate_against_schema()` and implements `validate_event_log(log, schema, scenario, fixture_sha256)`. It must additionally require: exact scenario/client/network values; sequences exactly `1..N`; an actual process with exit code zero; the generated fixture tree hash; required events as an ordered subsequence; no forbidden event; assertion texts exactly equal to the scenario list with every `passed: true`; approval before mock access; no `public_web_access` outside `mock_only`; command evidence for every `sync`, search, snapshot, registration, links, apply, and validate event; successful `wiki apply` changed-path evidence for any Q&A/page mutation; cursor evidence before any `*_drained` event; and `result: pass` with an empty incomplete list. Missing execution/transcript evidence is `incomplete`, never a synthesized pass.

Implement those semantics directly, not as comments:

```python
# tests/evals/event_log_contract.py
from __future__ import annotations

from typing import Any

from tests.evals.scenario_contract import validate_against_schema


class EventLogContractError(ValueError):
    pass


COMMAND_EVENTS = {
    "sync", "wiki_reconcile_citations", "snapshot_used_source",
    "register_extraction_handoff", "rendered_snapshot_url", "wiki_apply",
    "links_check", "validate", "source_pass_discovery", "source_pass_expansion",
    "source_pass_verification", "wiki_search_drained", "freshness_search_batched",
    "freshness_search_drained", "source_pass_discovery_drained",
    "source_pass_expansion_drained", "source_pass_verification_drained",
    "link_candidates_drained",
}


def validate_event_log(
    log: dict[str, Any], schema: dict[str, Any], scenario: dict[str, Any], fixture_sha256: str,
) -> None:
    validate_against_schema(log, schema)
    if log["scenario_id"] != scenario["id"] or log["fixture_sha256"] != fixture_sha256:
        raise EventLogContractError("scenario or fixture identity mismatch")
    if log["network_mode"] != scenario["network_mode"]:
        raise EventLogContractError("network mode mismatch")
    expected_processes = 2 if scenario["network_mode"] == "mock_only" else 1
    if len(log["executions"]) != expected_processes:
        raise EventLogContractError("wrong number of actual client processes")
    if any(item["exit_code"] != 0 for item in log["executions"]):
        raise EventLogContractError("client process failed")
    events = log["events"]
    if [item["sequence"] for item in events] != list(range(1, len(events) + 1)):
        raise EventLogContractError("event sequence is not contiguous")
    names = [item["name"] for item in events]
    position = 0
    for required in scenario["required_events"]:
        try:
            position = names.index(required, position) + 1
        except ValueError as exc:
            raise EventLogContractError(f"missing/out-of-order required event: {required}") from exc
    forbidden = set(names) & set(scenario["forbidden_events"])
    if forbidden:
        raise EventLogContractError(f"forbidden events: {sorted(forbidden)}")
    for logical_run in (
        "freshness_search_batched", "source_pass_discovery",
        "source_pass_expansion", "source_pass_verification",
    ):
        if logical_run in scenario["required_events"] and names.count(logical_run) != 1:
            raise EventLogContractError(f"{logical_run} must occur exactly once")
    for event in events:
        if event["name"] in COMMAND_EVENTS and event["evidence_kind"] != "command":
            raise EventLogContractError(f"{event['name']} lacks command evidence")
        if event["name"].endswith("_drained") and not any("complete=true" in value for value in event["evidence"]):
            raise EventLogContractError(f"{event['name']} lacks complete cursor-run evidence")
    if "public_web_access" in names:
        if scenario["network_mode"] != "mock_only" or names.index("ask_web_approval") > names.index("public_web_access"):
            raise EventLogContractError("web access was not mock-only and approval-first")
    mutations = {"stage_existing_question_update", "write_wiki_question", "write_qualifying_pages", "persist_claim"}
    if mutations & set(names):
        applies = [item for item in events if item["name"] == "wiki_apply"]
        if not applies or not any("changed_paths=wiki/questions/" in value for item in applies for value in item["evidence"]):
            raise EventLogContractError("wiki mutation lacks successful question changed-path evidence")
    assertions = log["repository_assertions"]
    if [item["text"] for item in assertions] != scenario["repository_assertions"]:
        raise EventLogContractError("repository assertion set/order mismatch")
    if not all(item["passed"] for item in assertions):
        raise EventLogContractError("repository assertion failed")
    if log["result"] != "pass" or log["incomplete_reasons"]:
        raise EventLogContractError("run is not a complete pass")
```

- [ ] **Step 6: Implement and run the isolated cross-client harness**

`run_cross_client.py` has one interface:

```text
python3 -m tests.evals.run_cross_client --client {codex,claude} --scenario {all,<scenario-id>} --client-command-json JSON --network-attestation {workspace-egress-denied,mock-only}
```

For each selected scenario it creates a new temporary copy of the repository, removes `.git`, `.brain`, live `sources`, live `wiki`, and prior eval runs, overlays the generated fixture, initializes a private Git repository for before/after diffs, and wraps that copy's `./brain` to record argv, canonical JSON result, exit code, and timestamps before executing the untouched real launcher. Raw native client streams go to `.brain/eval-transcripts/`; only their SHA-256 enters the committed normalized log. The prompt includes the scenario, canonical skill entrypoint, permitted event vocabulary, and an instruction to emit semantic events; the normalizer accepts objective command/mutation events only when corroborated by the wrapper log or Git diff.

The outer Codex or Claude process may reach only its model-provider control plane; that transport is not a research tool. Every command, browser, MCP, or other capability exposed inside the generated workspace must have outbound egress denied. For Codex, the runner requires the workspace-write sandbox with `sandbox_workspace_write.network_access=false`. For Claude, it requires `--permission-mode dontAsk`, an exact tool allowlist, only the `Bash(./brain *)`, `Bash(git status *)`, and `Bash(git diff *)` command patterns, and no WebFetch/WebSearch/browser tool. The runner inspects and records these effective policies before accepting `workspace-egress-denied`; an unrecognized client command or unverifiable policy produces an incomplete run rather than trusting a string attestation.

The web scenario runs in two phases: the first actual client process must ask and stop without access; the harness then supplies exactly the scripted approval and one injected local `mock_web_capture` capability that can resolve only fixture IDs, never a URL or socket, and runs the second actual client process against the same workspace. The second phase records `mock-only`; every other workspace capability remains egress-denied. The harness refuses a public URL, an unmocked request, or any tool-policy expansion. If isolation cannot be verified, either executable is absent, either phase fails, or normalization lacks evidence, the harness still writes `result: incomplete` with reasons so readiness visibly fails.

Run all twelve actual executions (the JSON arrays are passed without shell evaluation):

```bash
python3 -m tests.evals.run_cross_client --client codex --scenario all --client-command-json '["codex","exec","--json","--sandbox","workspace-write","-c","sandbox_workspace_write.network_access=false","-"]' --network-attestation workspace-egress-denied
python3 -m tests.evals.run_cross_client --client claude --scenario all --client-command-json '["claude","--print","--output-format","stream-json","--permission-mode","dontAsk","--tools","Read,Edit,Write,Glob,Grep,Bash","--allowedTools","Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)"]' --network-attestation workspace-egress-denied
```

For `web-approval-and-capture`, the runner internally switches only its injected fixture capability to `mock-only`; the command-line attestation still declares egress denial for the workspace and the other five scenarios. It writes exactly `tests/evals/runs/<client>/<scenario-id>.event-log.json`. Re-running replaces only that exact client/scenario log; it never writes a user brain or auto-commits.

Add the final acceptance test:

```python
# tests/evals/test_cross_client_runs.py
import json
from pathlib import Path

from tests.evals.event_log_contract import validate_event_log

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ("codex", "claude")


def test_every_scenario_has_passing_actual_codex_and_claude_runs() -> None:
    event_schema = json.loads((ROOT / "tests/evals/event-log.v1.schema.json").read_text(encoding="utf-8"))
    for scenario_path in sorted((ROOT / "tests/evals/scenarios").glob("*.json")):
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        fixture = json.loads(
            (ROOT / "tests/evals/fixtures" / scenario["fixture_id"] / "fixture-manifest.json").read_text(encoding="utf-8")
        )
        for client in CLIENTS:
            run_path = ROOT / "tests/evals/runs" / client / f"{scenario['id']}.event-log.json"
            assert run_path.is_file(), f"readiness incomplete: run {client}/{scenario['id']}"
            log = json.loads(run_path.read_text(encoding="utf-8"))
            assert log["client"] == client, f"wrong client identity: {run_path}"
            validate_event_log(log, event_schema, scenario, fixture["tree_sha256"])
            assert log["result"] == "pass", f"readiness incomplete: {run_path}: {log['incomplete_reasons']}"
```

- [ ] **Step 7: Document and verify the generated and actual artifacts**

`tests/evals/README.md` states the exact generator and runner commands above, the provider-control-plane/workspace-egress separation and verified client tool policies, two-phase mocked approval, normalized log paths/schema, objective-evidence rules, and that twelve missing/failing/incomplete logs block release. It explicitly forbids hand-authoring a passing log or copying a log between clients/scenarios.

Run:

```bash
python3 -m tests.evals.generate_scenarios --check
python3 -m pytest tests/evals/test_scenario_contracts.py tests/evals/test_cross_client_runs.py -v
git diff --check
git status --short
```

Expected: generator check passes; all six scenarios and fixtures are byte-stable; all twelve logs validate as actual passing executions. A missing client, missing isolation, or incomplete scenario makes this gate fail with `readiness incomplete`. Do not commit.

---

### Task 8: Run the complete acceptance gate and return to the plan index

**Files:**

- Verify only: `AGENTS.md`, `CLAUDE.md`, `BRAIN.md`, `README.md`, `.agents/`, `.claude/`, `.codex/`, `brain`, `brainlib/`, `config/`, `docs/`, `sources/`, `wiki/`, `tests/`, `pyproject.toml`, `.gitignore`

**Interfaces:**

- Consumes: all five implementation plans.
- Produces: a clean test, integrity, packaging, and documentation handoff to the plan index without staging or committing.

- [ ] **Step 1: Run focused Plan 5 tests**

Run:

```bash
python3 -m pytest \
  tests/unit/test_instructions.py \
  tests/integration/test_operating_docs.py \
  tests/integration/test_core_skills.py \
  tests/integration/test_guardrail_skills.py \
  tests/integration/test_client_adapters.py \
  tests/integration/test_user_journey_docs.py \
  tests/evals/test_scenario_contracts.py \
  tests/evals/test_cross_client_runs.py -v
```

Expected: PASS, including six actual isolated scenarios for each client. Missing or incomplete normalized run artifacts are a release blocker, not a skip.

- [ ] **Step 2: Run the whole deterministic suite**

Run: `python3 -m pytest -v`

Expected: PASS with no skipped core contract or cross-client acceptance test. Optional real-converter tests may skip only with an explicit missing-approved-tool reason; an unavailable Codex/Claude executable or missing network-isolation attestation must leave a failing `incomplete` evaluation, never a skip.

- [ ] **Step 3: Run CLI and full-integrity acceptance checks**

Run:

```bash
./brain --json doctor
./brain --json status
./brain --json validate --full
```

Expected: `doctor` reports rather than installs; status JSON is parseable; full validation reports `ok: true` for the empty template fixture/current valid repository.

- [ ] **Step 4: Verify working-tree symlinks, sentinels, and template emptiness before staging**

Run:

```bash
test -L CLAUDE.md
test "$(readlink CLAUDE.md)" = "AGENTS.md"
test -f CLAUDE.md
for name in brain-initialize brain-answer brain-web-research brain-wiki-maintenance brain-validate; do
  test -L ".claude/skills/$name"
  test "$(readlink ".claude/skills/$name")" = "../../.agents/skills/$name"
  test -f ".claude/skills/$name/SKILL.md"
done
find sources/raw sources/extracted sources/ledger wiki -type f \( -name .gitkeep -o -name .keep \) -print | sort
find sources/raw -type f ! -name .gitkeep ! -name .keep -print
```

Expected: the root and five skill entries are real resolving filesystem symlinks; reserved directories contain their sentinels; a distributed-template run prints no personal source file. Do not use `git ls-files` yet: newly created paths cannot be proven tracked until a review index or the authorized final index staging step includes them.

- [ ] **Step 5: Review the final diff against the approved spec**

First inventory all tracked and untracked paths in the real working tree, then use an isolated temporary Git index to review the complete selected-plan diff—including new files and symlink modes—without changing the user's real index:

```bash
git status --short --untracked-files=all
review_index="$(mktemp)"
rm -f "$review_index"
trap 'rm -f "$review_index"' EXIT
GIT_INDEX_FILE="$review_index" git read-tree origin/main
GIT_INDEX_FILE="$review_index" git add -A -- AGENTS.md CLAUDE.md BRAIN.md README.md .agents .claude .codex brain brainlib config docs sources wiki tests pyproject.toml .gitignore
GIT_INDEX_FILE="$review_index" git diff --cached --check origin/main
GIT_INDEX_FILE="$review_index" git diff --cached --stat origin/main
GIT_INDEX_FILE="$review_index" git diff --cached origin/main
for link in CLAUDE.md .claude/skills/brain-initialize .claude/skills/brain-answer .claude/skills/brain-web-research .claude/skills/brain-wiki-maintenance .claude/skills/brain-validate; do
  mode="$(GIT_INDEX_FILE="$review_index" git ls-files -s -- "$link" | awk '{print $1}')"
  test "$mode" = "120000"
done
GIT_INDEX_FILE="$review_index" git ls-files --error-unmatch -- sources/raw/_versions/.gitkeep sources/raw/_web/.gitkeep sources/extracted/.gitkeep sources/ledger/.gitkeep sources/ledger.md wiki/pages/.gitkeep wiki/questions/.gitkeep wiki/index.md
GIT_INDEX_FILE="$review_index" git ls-files -s CLAUDE.md .claude/skills sources/raw sources/extracted sources/ledger wiki
```

The temporary index must show mode `120000` for `CLAUDE.md` and the five Claude skill links and must contain the reserved-directory sentinels. Then verify explicitly: five canonical skills; five Claude links; four shared briefs; four Codex and four Claude adapters; no model pins; no duplicated policies; public-web approval and localization; exactly-three-pass restart after web; repository-development exclusion; full validation; no automatic commit behavior. The `EXIT` trap removes only the exact temporary index created above.

- [ ] **Step 6: Hand off without staging or committing**

Run `git status --short --untracked-files=all` against the real index once more and report the verified paths and any unrelated pre-existing changes. Do not stage the real index or run `git commit` in this plan; the temporary-index review above is the only permitted `git add`. Return to the plan index: its combined-certification Task 3 Step 5 alone may stage the already reviewed selected-prefix paths and create at most one logical commit, and only when the user or host explicitly requests it.

## Plan self-review

- Spec coverage: Tasks 1 and 6 establish one canonical policy/manual layer and the complete empty-template user journey; Tasks 3 and 4 encode typed-handoff initialization, wiki-first answering with mandatory fast-path persistence, resumable three-pass research, web approval/localization, maintenance, and full validation; Task 5 exposes the same briefs and skills to both clients; Task 7 provides six generated synthetic-corpus scenarios plus twelve schema-validated event logs from actual isolated Codex/Claude processes; Task 8 requires both deterministic and cross-client acceptance gates.
- Placeholder scan: operational command examples contain complete option names and values; interface declarations alone use Python stub ellipses. Task 8 is verify-only and routes failures back to the task that owns the exact file.
- Type consistency: this plan consumes the canonical `RepoPaths`, diagnostics/report, source handoff, approval-event claim fields, evidence packet, citation, and graph names from Plans 1-4 without defining competing data models.
- Commit consistency: no task stages or commits in the real index. The plan index owns the only possible final selected-prefix commit and still requires explicit user or host authorization.

## Plan-set handoff

Return to [`2026-09-04-second-brain-lite-plan-index.md`](2026-09-04-second-brain-lite-plan-index.md) for the single execution choice. Do not ask for a second per-plan execution choice here.
