---
name: brain-answer
description: Use when the user asks a substantive knowledge question that should be answered from and, when evidence permits, persisted in the Second Brain.
---

# Answer from the brain

Read `BRAIN.md`, `docs/brain/workflows/answer.md`, and the citation, wiki, source-handling, approval, and web policies before acting.

## Classify

If this is a repository-development request, acknowledgement, or incidental conversational question, answer normally. **Do not create a question record** and do not invoke this knowledge workflow further.

## Synchronize

Do not skip synchronization or search for urgency. Run `./brain --json sync`; retain its bounded result ID, revision, counts, and samples. If a `sync` response carries `result_manifest`, run `./brain --json source consume-sync-result --result-id "$result_id"`; require its `result_id`, immutable `manifest_path`, corpus revision, exact event counts, effect digest, and `handoff_delivery` to match the retained response. This CLI verifies and drains the full immutable stream and records a durable deduplicated receipt without acknowledging, dispatching, mutating sources/wiki, installing, or using network. Read `manifest_path` only as needed for exact rewrites, new-active IDs, and gaps. Read a non-null immutable `handoff_delivery` only after matching it to the consumed result, then branch solely from its typed items; never route from response summaries. Durably apply/deduplicate those effects by `result_id`, then run `./brain --json source acknowledge-sync-result --result-id "$result_id"`. Acknowledgement requires that matching consumed receipt and is separately idempotent; never acknowledge a partial, stale, expired, or tampered result. Do this before any mutation, registration, or handoff dispatch. `source register-extraction` itself has no result manifest; validate its registration response instead. Drain deterministic or `needs_agent` ingestion using the initialization workflow, rerunning sync after every ingestion mutation until stable.

For each exact typed item in the consumed `handoff_delivery`, branch explicitly. If `handoff.kind == "extraction"`, write only in `.brain/agent-staging/<handoff-id>/...` and run `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. Require `data.registration.active_representation` to match the registration identity; it, not the immutable pre-registration `SnapshotResult.active_representation`, proves this extraction became active. If `handoff.kind == "rendered_web_capture"`, use approved faithful browser bytes in `.brain/web-staging/<handoff-id>/...` and run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`, appending observed redirect flags. Never send a `rendered_web_capture` handoff to `register-extraction`. After a `source snapshot-url` response carrying `result_manifest`, repeat the same consume command, matching-result checks, immutable `manifest_path` non-handoff effect handling, receipt-bound `handoff_delivery`, durable deduplication, and separate acknowledgement before processing its handoffs or making another mutation. A direct deterministic capture requires non-null `SnapshotResult.active_representation`; an extraction handoff may correctly have a null immutable pre-registration snapshot and completes only through the extraction branch.

Accumulate the sorted union of exact streamed `citation_rewrite` events or approved-adoption rewrites plus exact streamed newly activated source IDs across the entire loop, retain unresolved exact coverage-gap events, and use the final verified corpus revision. Put that union in the sorted `citation_rewrites` manifest array (including an empty array) before searching or judging the wiki, in a version-1 manifest with `link_candidate_runs: []` and `changes: []`, stage it at `.brain/wiki-staging/<run-id>/manifest.json`, and run `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`. This ledger-driven reconciliation recovers stale destinations even when a prior response was lost; stop on failure.

If any source became active, start one logical freshness run with `./brain --json search --scope sources --freshness --source-id "$source_id" --term "$term"`, repeating `--source-id` for every sorted new ID and `--term` for every initial term; do not create one run per source/term pair. Drain all pages with `./brain --json search --cursor "$next_cursor"` until `complete: true`, checking the run ID, revision, and contiguous indexes. This is a freshness probe, not a research pass. A relevant hit makes the old wiki answer stale; an incomplete run blocks sufficiency.

## Search the wiki

Generate literal exact terms, entities, aliases, dates, acronyms, spelling variants, and phrases. Start one logical `./brain --json search --scope wiki --term "$term"` with repeated terms, then drain it using `./brain --json search --cursor "$next_cursor"` until `complete: true`. Read relevant page and question sections only after the run is complete.

## Judge sufficiency

An incomplete, stale, expired, tampered, or corrupt wiki run is a coverage gap: stop with partial/unanswered status. If the fully drained wiki result has no match or no supported record, do not call `build_wiki_evidence_packet(...)`; proceed directly to exactly the three source passes. Otherwise revalidate every underlying citation against the current ledger by calling `build_wiki_evidence_packet(...)` with the fully drained wiki pages, matched paths, and exact supporting/counterevidence `CitationRef(document_path, citation_id)` tuples. Accept its canonical `WikiEvidencePacket` only if it directly covers every material part, every factual passage maps to a unique `RevalidatedCitation`, it reflects the current relevant corpus revision, contradictions/coverage gaps are represented, and `complete` is true. If sufficient, skip the three source passes, but do not answer or stop yet: pass this validated packet to the curator so it creates or updates the one evolving topic Q&A record, then complete graph and repository validation.

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

For an empty corpus, explain that there is no local evidence and invite files
in `sources/raw/`; do not invent a supported answer or an empty evidence packet.
After meaningful persisted changes, follow the save-and-return guidance in
`docs/brain/workflows/onboarding.md`. A save reminder is not permission to commit.

Run `./brain --json links check` and `./brain --json validate --full`. Repair deterministic failures through another staged wiki-apply transaction and request an independent `brain-auditor` review for material knowledge changes. Answer with unresolved conflict and coverage gaps disclosed; use partial/unanswered status rather than an unsupported answer.

If local evidence is insufficient, offer `brain-web-research`; do not browse before explicit approval. After approved captures are active, rerun Pass 1, Pass 2, and Pass 3 before changing the persisted synthesis.

Use v2 typed interpretation state: retain conflicts as `unresolved` with two
distinct exact citations in `## Contradictory evidence`. Before `preferred`,
ask for and record approval, store the selected local citation and matching
approval ID, cite it in both required sections, and use
`resolve_contradiction`. Preference prose has no durable effect.
