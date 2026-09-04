# Second Brain Lite

## Directory roles

- `sources/raw/` retains acquired source evidence. Its `_versions/` and `_web/` stores are reserved implementation evidence.
- `sources/extracted/` holds normalized source material.
- `sources/ledger/*.json` records are the canonical source inventory. `sources/ledger.md` is their generated human-readable summary.
- `wiki/pages/` holds durable notes; `wiki/questions/` holds open questions; `wiki/index.md` is the graph entrypoint.
- `docs/brain/` contains repository policies and operating workflows.

## Lifecycle

Add original source files to `sources/raw/`, then run `./brain init`. The command is deterministic and local: it inventories the corpus, checkpoints source-ledger records, and attempts only configured local processing. It does not create wiki pages, install software, browse the web, or perform agent-assisted extraction.

Run `./brain sync` before substantive questions. In Plan 2 it reconciles the source ledger and extracted representations only; it does not update the wiki. A URL descriptor remains control metadata in `awaiting_approval` until an agent workflow obtains explicit public-web approval.

Both commands exit `1` with `complete_with_gaps` when work remains pending, failed, unsupported, warning-quality, in an integrity error, awaiting approval, needs agent assistance, or could not be inventoried safely. Continue through the `brain-initialize` or `brain-answer` workflow rather than treating that exit as a complete corpus. Run `./brain validate --full` before a completed handoff or readiness claim; it rehashes every retained raw version and extraction, including inactive history.

Use `./brain status` for the current coverage decision. Validation is the integrity half of the readiness gate: it checks the complete retained ledger and immutable evidence, while init, sync, and status decide whether operational coverage gaps remain. Historical derivations stay valid under their stored provenance when extractor policy later changes; synchronization decides whether the active representation is current under the new policy.
