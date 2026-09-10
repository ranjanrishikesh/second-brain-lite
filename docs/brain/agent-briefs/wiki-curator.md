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

For v2 questions, write `unresolved` when preserving contradictory claims and
retain two distinct exact citations in `## Contradictory evidence`. Before
choosing `preferred`, ask and record approval; put the selected local citation
in both required sections, record its ID and matching approval event ID, and
use `resolve_contradiction`. Preference prose is never evidence of this state.
