# Second Brain Lite

## Route every request

- “initialize this brain,” first-time ingestion, or resume: load and follow the `brain-initialize` skill.
- A substantive knowledge question: load and follow the `brain-answer` skill. Synchronization is part of that skill.
- For public-web research after local insufficiency, load and follow the `brain-web-research` skill; it must ask before access.
- For wiki maintenance—manual page, link, rename, contradiction, merge, split, or removal work—load and follow the `brain-wiki-maintenance` skill.
- Completed handoff or PR readiness: load and follow the `brain-validate` skill and require `./brain --json validate --full`.
- A repository development request—CLI, tests, docs, schemas, skills, or architecture—uses normal software-development instructions and must not become a knowledge Q&A record.

## Authority

`AGENTS.md` is canonical and `CLAUDE.md` must be its symlink. `BRAIN.md` indexes detailed rules. Deterministic CLI output owns checksums, IDs, ledger state, and validation; agents own semantic judgment. Never bypass an approval gate, invent provenance, auto-commit, or hide a coverage gap.
