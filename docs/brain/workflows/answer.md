# Answer workflow

Read `BRAIN.md`, `docs/brain/policies/source-handling.md`, `docs/brain/policies/citations.md`, `docs/brain/policies/wiki.md`, and `docs/brain/policies/web.md` before acting.

## 1. Classify the request

Repository development, acknowledgements, and incidental conversation stop here and are not archived. A substantive knowledge request continues.

## 2. Synchronize

Run `./brain --json sync` and treat `payload.data` as a bounded status/count/sample envelope. Retain its result ID, revision, and counts, then run `./brain --json source consume-sync-result --result-id "$result_id"`. Require its `result_id`, immutable `manifest_path`, corpus revision, exact event counts, effect digest, and `handoff_delivery` to match the envelope. This production CLI verifies and drains the full immutable stream and records a durable deduplicated receipt without acknowledgement, handoff dispatch, source/wiki mutation, installation, or network. Read `manifest_path` only as needed for exact rewrites, new-active IDs, and gaps; if non-null, read only the receipt-bound immutable `handoff_delivery` for typed handoffs. Apply/deduplicate those permanent `result_id` effects and durably record them. Only after those consumer-visible steps succeed, run `./brain --json source acknowledge-sync-result --result-id "$result_id"`; acknowledgement requires the matching receipt, while output or consumer failure before it deliberately leaves the exact result pending for replay. Drain deterministic and `needs_agent` work through the initialization rules, rerunning sync after every ingestion mutation until stable. Branch on receipt-bound typed handoff items: `handoff.kind == "extraction"` uses exact `.brain/agent-staging/<handoff-id>/...` output and `register-extraction`; `handoff.kind == "rendered_web_capture"` uses an approved faithful `.brain/web-staging/<handoff-id>/...` browser export and rendered `snapshot-url --rendered-staging-path`. Every snapshot response must follow the capture receipt boundary in `docs/brain/workflows/web-research.md`: consume its `result_manifest`, derive exact non-handoff effects from `manifest_path` and typed handoffs from `handoff_delivery`, durably deduplicate them, and acknowledge it before processing handoffs, registration, or another snapshot. Never send a `rendered_web_capture` handoff to `register-extraction`. For direct capture completion, require `SnapshotResult.active_representation`; after agent extraction, instead require `data.registration.active_representation` and `data.registration.corpus_revision` and revalidate the persisted derivation. The original `SnapshotResult` remains unchanged and may retain a null representation. Across that bounded loop, accumulate the sorted union of every exact `citation_rewrite` event (plus approved adoption rewrites) and every exact newly activated source ID; preserve exact coverage-gap events. Never infer completeness from response samples. Use only the final verified result's `corpus_revision` below.

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

First authenticate the fully drained wiki run with `complete_search_run(search_pages)` and `verify_search_run_proof(...)`. If it has no matches, or its well-formed matched records provide no supporting citations for this question, go directly to Section 5 without calling `build_wiki_evidence_packet(...)`. An empty wiki or a valid search with only unrelated records is insufficient evidence, not an invalid evidence packet.

Inspect any nonempty candidates before taking that branch: a malformed nonempty candidate, invalid citation definition or destination, citation revalidation failure, or incomplete, stale, or tampered search must fail closed. Never treat these failures as an empty result; report the blocker or partial state and repair it through the applicable workflow.

When supported matched records remain, the wiki is sufficient only when it addresses every material part, matches the relevant corpus revision, and has no unresolved material contradiction. Before making that judgment, re-resolve every underlying claim citation against the current ledger and call `build_wiki_evidence_packet(...)` with the fully drained wiki `search_pages`, matched paths, and exact supporting/counterevidence `CitationRef(document_path, citation_id)` tuples. Its `WikiEvidencePacket` fields—question ID, corpus revision, search run ID, matched records, `RevalidatedCitation` values, `supporting_citations`, `counterevidence_citations`, contradictions, coverage gaps, and `complete`—must validate, and `complete` must be true. If that packet is sufficient, skip Section 5 but continue through Sections 6 and 7; the fast path still updates the one evolving topic Q&A record and validates the graph before answering.

## 5. Search sources three times

When Section 4 finds no supported wiki evidence, or the validated `WikiEvidencePacket` is insufficient, start exactly these three **logical** source passes, once each and in this order; repeat `--term` inside the same initial invocation for every term in that pass:

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

Run `./brain --json links check` and `./brain --json validate --full`. Repair failures through a new `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"` transaction before answering. Disclose partial coverage and conflicts. Offer the approval-gated web workflow only when local evidence remains insufficient.

When claims materially conflict, persist `interpretation_decision: unresolved`
with two distinct exact citations in `## Contradictory evidence`. To set
`preferred`, first obtain approval, store its event ID and selected local
citation ID, cite it in both Contradictory evidence and Current answer, and
publish through a `resolve_contradiction` manifest. Do not infer it from prose.
