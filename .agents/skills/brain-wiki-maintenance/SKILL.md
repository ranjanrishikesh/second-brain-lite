---
name: brain-wiki-maintenance
description: Use when the user asks to create, update, link, rename, reconcile, merge, split, or remove wiki pages or question records.
---

# Maintain the wiki

Read `docs/brain/policies/wiki.md`, `docs/brain/policies/citations.md`, and `docs/brain/workflows/wiki-maintenance.md`.

- Require at least one useful sourced statement before creating a page. Do not create empty or alias-only stubs.
- Parse Markdown structure. Never use raw global string replacement. Link only the first meaningful occurrence of a topic in each section; reject self-links and ambiguous aliases.
- For every proposed write/delete, start one `./brain --json links candidates "$changed_page" --term "$term"` with every relevant term repeated in that command. Drain its pages only with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`; a malformed, stale, expired, tampered, or incomplete result must fail closed. An empty, complete wiki or candidate-search result is normal insufficiency: do not invent a relationship or turn it into an error. Read every matching context, choose only genuine relationships, and update every declared page/question relationship reciprocally in the staged files.
- Preserve meaningful question refinements and both sides of sourced contradictions.
- In a v2 QuestionRecord, use `unresolved` for retained contradictory claims
  and two distinct exact citations in `## Contradictory evidence`. Before
  `preferred`, ask and record approval, use the selected citation in both
  required sections, store the matching approval ID, and use the
  `resolve_contradiction` manifest intent. Never infer this from prose.
- Ask before deleting, merging, or materially splitting pages; **Removing a sourced claim**; choosing a materially contradictory interpretation; a major uncertain rewrite; or an ambiguous rename. An unambiguous rename still uses the staged transaction and is never a direct live edit.
- Write proposed pages/questions only beneath `.brain/wiki-staging/<run-id>/files/`, mirroring each full `wiki/pages/...` or `wiki/questions/...` logical target. Never write directly to `wiki/`. Calculate each staged write's SHA-256, create the exact version-1 manifest with the current expected corpus revision, intent, approval event ID when required, any pending citation rewrites, sorted `link_candidate_runs` proofs covering every write/delete, and the complete write/delete set, then invoke `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`.
- After an approved removal or rename, search and reconcile every affected page/question in the same staged transaction, then run `./brain --json links check` and `./brain --json validate --full`.
