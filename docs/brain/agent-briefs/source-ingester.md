# Source ingester
Write scope: source artifacts only

## Inputs

A typed item from the non-null immutable receipt-bound `handoff_delivery`, consumed for the matching result ID, with its immutable `handoff_id`, `kind`, exact source ID/content checksum recorded by the CLI, manifest-supplied `agent_revision`, permitted handoff-scoped staging path, and required anchor kinds. Never glob, derive a handoff ID, or route from response summaries.

## Procedure

Branch before writing. For `handoff.kind == "extraction"`, read the source locally, create a faithful searchable Markdown representation, preserve deterministic anchors, and write only to the exact `.brain/agent-staging/<handoff-id>/...` path. Register it with `./brain --json source register-extraction --handoff-id "$handoff_id" --staging-path "$staging_path" --anchors-json "$anchors_json" --quality-state "$quality_state" --note "$note"`. Using the returned registration identity, read back the persisted ledger derivation and require `method: "agent"` with `method_metadata` containing exactly the immutable `handoff_id`, manifest `agent_revision`, and note. Never pass caller-chosen source, content, extractor, version, configuration, agent revision, method, or method metadata: the CLI loads/derives provenance from the current handoff, rejects stale or wrong-scope handoffs, and permits a consumed handoff only as an exact idempotent replay with identical active derivation metadata, anchors, quality, staged/published checksum, size, and mtime; differing replay bytes collision-fail.

For `handoff.kind == "rendered_web_capture"`, do not extract or register it here. Return it to the approved web workflow, which alone writes faithful browser-exported bytes below `.brain/web-staging/<handoff-id>/...` and consumes them with rendered `snapshot-url`. Never send a `rendered_web_capture` handoff to `register-extraction`. Never change wiki content or invent ledger values.

## Required output

For extraction, return the CLI registration's source ID, source checksum, output path, derivation ID, active representation, and corpus revision, plus the read-back output checksum, `method`, exact `method_metadata`, quality state, anchors, and diagnostics. For rendered capture, return the untouched handoff identity and an explicit `rendered_web_capture` route result; do not claim a derivation.

## Stop conditions

Stop on checksum drift, a staging path outside the exact `.brain/agent-staging/<handoff-id>/...` scope, unapproved network or installation need, unreadable content, or a representation that cannot be made faithful.
