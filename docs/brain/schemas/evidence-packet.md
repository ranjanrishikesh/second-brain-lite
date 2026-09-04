# Evidence packet

An `EvidencePacket` is an immutable researcher-to-curator JSON handoff. It
contains a canonical question ID, a current 64-lowercase-hex corpus revision,
and exactly three completed, distinct logical source passes in this order:
`discovery`, `expansion`, `verification`. Each pass has nonempty search terms,
a `srch_` plus 32 lowercase-hex run ID, candidate-manifest checksum, contiguous
result pages beginning at zero, and no pass coverage gap. A completed zero-hit
pass is valid. Continuation pages extend their one logical pass; they never
become a fourth pass.

Supporting and contradictory evidence records carry the exact `source_id`,
content SHA-256, derivation ID, canonical anchor, and passage. Coverage gaps
are canonical diagnostics; incomplete, expired, stale, or spool-limited search
gaps cannot enter a completed packet. `freshness_probe_source_ids` is an
optional sorted record of the pre-research sync probe and is not a research
pass.

Codecs accept only object JSON with known fields, reject duplicate object keys,
invalid IDs and paths, and noncanonical list order, and emit sorted-key UTF-8
JSON with exactly one trailing newline.

`WikiEvidencePacket` is the only wiki-sufficient fast-path handoff. A complete
packet has a current revision, one fully drained wiki run, at least one matched
wiki page/question record, sorted unique revalidated exact citations,
nonempty document-qualified supporting `CitationRef` values that resolve to
those citations, explicit contradictions, no coverage gap, and `complete:
true`. Counterevidence refs also resolve to that set and cannot overlap
support. The Task 5 builder, rather than this codec, proves run freshness and
revision currentness. Both packet types are read-only handoffs and carry no
proposed Markdown edits.

## Required answer-workflow checkpoint

The milestone-5 `brain-answer` skill MUST begin every substantive answer
workflow with `./brain --json sync`; source sync MUST precede substantive
research. It parses the bounded envelope's `result_manifest`,
verifies that content-addressed JSONL before reading an event, drains the
stream, and requires its exact counts and corpus revision to agree with the
envelope. It durably applies/deduplicates that `result_id` before issuing
`./brain --json source acknowledge-sync-result --result-id "$result_id"`.
An incomplete, failed, expired, stale, spool-limited, or tampered stream is a
blocking coverage gap and is never acknowledged; bounded display samples never
prove completeness.

After every acknowledged sync, the workflow passes the exact sorted
`citation_rewrite` event tuple—including an empty tuple—through an
expected-revision empty or staged `./brain --json wiki apply` before it reads
wiki content. It derives nonempty initial terms, drains every wiki-search page,
completes the run, and verifies its proof. When the exact
`new_active_representation` set is nonempty, it runs and fully verifies a
freshness probe against exactly those source IDs before accepting older wiki
material; an empty set records `freshness_probe_source_ids=()` and makes no
freshness call.

If current wiki evidence is sufficient and the freshness probe finds no
relevant newer conflict, the skill builds one complete `WikiEvidencePacket`
from exact revalidated citations. The curator still updates the single
evolving topic Q&A record, drains link candidates for every proposed change,
publishes with `wiki apply`, and validates before answering.

Otherwise source research consists of exactly three logical passes:
`discovery`, then expansion terms derived from completed discovery context,
then `verification` terms for decisive support, temporal qualifiers,
exceptions, conflicts, and counterexamples. Each pass drains every cursor and
passes `complete_search_run`, `verify_search_run_proof`, and
`completed_search_pass` before the next begins. All three records share the
current revision; a freshness probe is sync evidence, never a fourth pass.
Only the resulting complete `EvidencePacket` is handed to the curator.

For incomplete local evidence, the workflow writes a `partial` or `unanswered`
record and requests web approval; it does not browse. After approval, each
successful `source snapshot-url` response is consumed as its canonical
`data.snapshot`: `source_id`, `raw_path`, `content_sha256`, `source_version`,
`retrieval`, `extraction_result`, `active_representation`, and
`corpus_revision`. For agent work it also consumes the sibling
repo-relative-or-null `handoff_manifest` and sorted `handoffs` summaries before
registering extraction. It persists only sources actually used, does not
invent a post-web `SyncReport`, and cannot rely on a later sanity sync to
rediscover already-active captures. Once every used capture has a non-null
active representation (or a registered agent extraction), it uses those exact
new representations and the latest returned revision to restart and completely
drain discovery, expansion, and verification before a durable answer is
created. This contract fixture documents control flow only and never calls the
network.
