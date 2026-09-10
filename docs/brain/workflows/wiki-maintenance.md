# Wiki-maintenance workflow

Read `docs/brain/policies/wiki.md` and `docs/brain/policies/citations.md` before acting.

1. Parse, do not regex-rewrite, Markdown structure.
2. Enforce evidence-backed page qualification and claim-level citations.
3. For every proposed write/delete, run `./brain --json links candidates "$changed_page" --term "$term"` for all titles, aliases, entities, dates, and important phrases, repeating `--term` in that one initial command.
4. Drain every result with `./brain --json links candidates --cursor "$next_cursor"` until `complete: true`; stop if a run is stale, expired, tampered, or incomplete. Let the curator accept only genuine, unambiguous relationships.
5. Write proposed pages/questions only beneath `.brain/wiki-staging/<run-id>/files/wiki/pages/` or `files/wiki/questions/`, mirroring each full logical target, update relationships reciprocally in that staged set, and write the version-1 manifest with checksums, the current expected corpus revision, and sorted `link_candidate_runs` proofs covering every write/delete.
6. Publish only with `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`; never edit live `wiki/` files directly.
7. Run `./brain --json links check` and `./brain --json validate --full`.
8. Stop for approval at every destructive or contradiction gate in the wiki policy and put the approval event ID in the manifest.

For a retained contradiction, set the v2 QuestionRecord to `unresolved` and
keep two distinct exact citations in `## Contradictory evidence`. For a
preferred reading, ask first, record the approved citation ID and approval ID,
cite it in both required sections, and use `resolve_contradiction`. Never infer
a preference from prose.
