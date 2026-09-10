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
5. For a used ad-hoc URL with no descriptor, run the separate command `./brain --json source snapshot-url --url "$url" --description "$description" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`. Do not combine `--source-id` and `--url`. Each successful command saves immutable bytes beneath `_web` and returns its exact `SnapshotResult`.
6. Consume every `result_manifest` before another snapshot, registration, or source mutation: retain its result ID, revision, and counts from the bounded response; run `./brain --json source consume-sync-result --result-id "$result_id"`; require its `result_id`, immutable `manifest_path`, corpus revision, exact event counts, effect digest, and `handoff_delivery` to match that reference. This CLI verifies and drains the full immutable stream and records a durable deduplicated receipt without acknowledging, dispatching, mutating sources/wiki, installing, or using network. Read `manifest_path` only as needed for exact rewrites, new-active IDs, and gaps. When non-null, read only the immutable `handoff_delivery`, requiring every typed item to map to the consumed handoff effect; durably deduplicate/apply those effects by `result_id`, and then run `./brain --json source acknowledge-sync-result --result-id "$result_id"`. Acknowledgement requires the matching consumed receipt and remains idempotent. Do not acknowledge a partial, stale, tampered, or unrecorded result. This acknowledgement clears the pending snapshot receipt; without it, the next capture or registration is blocked.
7. Only after that receipt is consumed, branch on every typed `handoff_delivery` item. If `handoff.kind == "extraction"`, create Markdown only beneath `.brain/agent-staging/<handoff-id>/...` and run `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. Verify `registration.active_representation` from that registration before treating extraction evidence as active. If `handoff.kind == "rendered_web_capture"`, use only this approved event to save a faithful serialized DOM, print-to-PDF, or downloaded claim-bearing artifact beneath `.brain/web-staging/<handoff-id>/...`, then run `./brain --json source snapshot-url --source-id "$source_id" --rendered-staging-path "$rendered_staging_path" --handoff-id "$handoff_id" --retrieved-at "$retrieved_at" --final-url "$final_url" --detected-media-type "$detected_media_type" --approval-event-id "$event_id" --approval-scope "$scope" --approval-note "$approval_note"`, appending one `--redirect-url "$redirect_url"` per observed redirect. Never send a `rendered_web_capture` handoff to `register-extraction`.
8. Do not cite browser text. Use `SnapshotResult.active_representation` only for a direct capture that already returned an active representation; it must be non-null, match the result's source/content identity and current corpus revision, and have resolving original, extraction, and anchors. Route any newly emitted typed handoff through Steps 6 and 7. Unused candidates and search-result snippets are not persisted.
9. Return to local research and start exactly these three logical runs once each and in order, repeating `--term` within an initial call when needed:

```bash
./brain --json search --scope sources --pass discovery --term "$discovery_term" --context 3
./brain --json search --scope sources --pass expansion --term "$expansion_term" --context 3
./brain --json search --scope sources --pass verification --term "$verification_term" --context 3
```

These are **Pass 1**, **Pass 2**, and **Pass 3** against the new corpus revision. For each, call `./brain --json search --cursor "$next_cursor"` until `complete: true` before deriving or starting the next pass. Stop partial/unanswered if any continuation fails.
10. Persist and cite only the new local evidence through the curator's staged wiki-apply transaction after all link-candidate runs are fully drained. Report capture failures and do not use their claims.

Ask again before a materially expanded scope, a later research event, software installation, or an allowlist change.
