# Initialize a Brain

1. Add original source files to `sources/raw/` under the applicable source-handling rules.
2. Run `./brain init` from the repository root.
3. Inspect the bounded command report. `complete` exits `0`; `complete_with_gaps` exits `1` and reports exact counts plus bounded samples for pending, failed, unsupported, warning-quality, integrity, approval-gated, agent-assisted, or safely uninspectable work. For complete event detail, verify and drain the referenced content-addressed result manifest; never treat samples as exhaustive. Durably apply/deduplicate its `result_id`, then explicitly run `./brain source acknowledge-sync-result --result-id "$result_id"`. Do not acknowledge a partial stream; without acknowledgement, the exact result remains pending and replayable.
4. For gaps, continue through the `brain-initialize` agent workflow. A direct CLI run does not install converters, browse the public web, invoke agent vision, or fetch URL descriptors. Public-web capture requires explicit approval.
5. Finish by running `./brain validate --full`. This is the integrity half of the required handoff gate because it rehashes every retained raw version and extraction, including inactive history. Also require a gap-free init/status result; structural validation alone does not turn pending work into complete coverage.

Initialization is deterministic and local. It creates canonical `sources/ledger/*.json` shards and the generated `sources/ledger.md` summary, but it does not create or update wiki pages. The distribution template remains in the exact pre-initialization sentinel state until the first completed empty or nonempty init/sync publishes a generated summary, including a completion that reports coverage gaps.

Retained file history is bound to `_versions/<source-id>/<checksum>/...`; approved web captures are bound to `_web/<source-id>/<checksum>/...`. Full validation checks those roles and their bytes without reinterpreting an older derivation through the current extractor registry.
