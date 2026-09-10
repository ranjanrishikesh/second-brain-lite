# Second Brain Lite

## Route every request

- A greeting ("hi", "hello"), getting started, a setup-status question, or "save this work to main": follow `docs/brain/workflows/onboarding.md`. Check existing state before welcoming or recommending setup. Do not archive greetings.
- “initialize this brain,” first-time ingestion, or resume: load and follow the `brain-initialize` skill.
- "I added more files; update my brain": use `brain-initialize` to process eligible additions. If the request includes a substantive question, use `brain-answer`, which handles synchronization first.
- A substantive knowledge question: load and follow the `brain-answer` skill. Synchronization is part of that skill.
- For public-web research after local insufficiency, load and follow the `brain-web-research` skill; it must ask before access.
- For wiki maintenance—manual page, link, rename, contradiction, merge, split, or removal work—load and follow the `brain-wiki-maintenance` skill.
- Completed handoff or PR readiness: load and follow the `brain-validate` skill and require `./brain --json validate --full`.
- A repository development request—CLI, tests, docs, schemas, skills, or architecture—uses normal software-development instructions and must not become a knowledge Q&A record.

## Authority

`AGENTS.md` is canonical and `CLAUDE.md` must be its symlink. `BRAIN.md` indexes detailed rules. Deterministic CLI output owns checksums, IDs, ledger state, and validation; agents own semantic judgment. Never bypass an approval gate, invent provenance, auto-commit, or hide a coverage gap.

After source or knowledge changes, use the save-and-return guidance in `docs/brain/workflows/onboarding.md`. Run the CLI on the user's behalf; keep user-facing guidance conversational.
