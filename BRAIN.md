# Second Brain operating manual

## Authority

`AGENTS.md` is the concise routing authority and `CLAUDE.md` is a relative symlink to it. This manual indexes the detailed repository-owned policies, workflows, schemas, and shared agent briefs. Deterministic `./brain` output owns IDs, checksums, corpus revision, ledger state, staged publication, and validation. Agents own term generation, evidence interpretation, page qualification, relationship judgment, and asking for approvals. A client adapter may route to a shared brief but may not redefine it.

## Directory roles

- `sources/raw/` contains authoritative user originals, immutable `_versions/`, and approved `_web/` snapshots.
- `sources/extracted/` contains committed, versioned Markdown representations that `rg` can search.
- `sources/ledger/` and `sources/ledger.md` contain canonical machine records and their generated human summary.
- `wiki/questions/` contains one evolving record per stable topic; `wiki/pages/` contains evidence-backed atomic pages; `wiki/index.md` is generated.
- `.brain/agent-staging/` and `.brain/wiki-staging/` contain temporary agent proposals. Only the CLI may promote them to canonical source or wiki paths.
- `.agents/skills/` contains canonical client-independent workflows. `.claude/skills/` contains only relative symlinks to them.
- `docs/brain/agent-briefs/` contains canonical semantic roles. `.codex/agents/` and `.claude/agents/` are thin discovery adapters.

## Evidence lifecycle

`sources/raw` authoritative bytes → one stable logical source ID → SHA-256 content version → versioned searchable derivation with `method` and immutable `method_metadata` → validated wiki packet or completed three-pass source packet → claim-level citation → evolving Q&A and atomic wiki pages.

The ledger records mechanics; the wiki records supported interpretation. Deterministic derivations have `method: deterministic` and exact converter ID/version metadata. Agent derivations have `method: agent` and exact `{handoff_id, agent_revision, note}` metadata derived from the immutable handoff; callers never choose provenance fields. A citation is valid only while both the exact original version and exact derivation resolve.

## Question lifecycle

For a substantive knowledge question, run `./brain --json sync`. Every init, sync, or snapshot response with a result manifest must retain its result ID, revision, and counts, then run `./brain --json source consume-sync-result --result-id "$result_id"`. Require its `result_id`, immutable `manifest_path`, corpus revision, exact event counts, effect digest, and `handoff_delivery` to match the retained reference. This production CLI verifies and drains the full immutable stream and records a durable deduplicated receipt without acknowledging, dispatching, mutating sources/wiki, installing, or using network. Read `manifest_path` only as needed for exact rewrites, new-active IDs, and gaps; when non-null, read only the receipt-bound immutable `handoff_delivery`, whose typed items map to consumed handoff effects. Verify, drain, durably deduplicate, and acknowledge its effects by permanent `result_id`, running the separate `./brain --json source acknowledge-sync-result --result-id "$result_id"` only after consumption. Acknowledgement requires the matching receipt and is idempotent. Do this before dependent mutation, registration, or handoff dispatch. For a rendered `handoff.kind == rendered_web_capture`, complete rendered `snapshot-url` first. If normal allowlisted processing directly activates a representation, require non-null `SnapshotResult.active_representation` and the matching corpus revision. Only if that snapshot returns a subsequent `handoff.kind == extraction` does the agent stage and register it; then `data.registration.active_representation` and the corpus revision prove activation, while the immutable snapshot may remain null. Complete eligible typed handoffs and rerun sync after each ingestion mutation until stable; accumulate exact citation rewrites and newly active source IDs across the loop; reconcile citations through `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`; run one batched freshness probe only when accumulated newly-active source IDs is nonempty, otherwise proceed directly to wiki search; and drain that search. Revalidate its underlying citations into a `WikiEvidencePacket` before judging revision, completeness, and contradiction sufficiency. A complete authenticated search with no supported wiki record proceeds directly to the three source passes and must not construct an empty `WikiEvidencePacket`. If otherwise insufficient, run exactly one discovery, one expansion, and one verification logical source pass. Drain every opaque cursor before starting the next pass; incomplete runs produce partial/unanswered status. Give only the validated wiki packet or completed three-pass packet to the curator. Both paths create/update one evolving topic Q&A. The curator writes only `.brain/wiki-staging/`, drains link-candidate runs for every mutation, publishes through `./brain --json wiki apply --manifest ".brain/wiki-staging/$run_id/manifest.json"`, and then runs `./brain --json links check` and `./brain --json validate`. Repository-development and incidental conversational requests are not archived.

Readiness requires `./brain --json validate --full` and a current, gap-free init, sync, or status report: no unresolved pending, failed, unsupported, warning, integrity, approval, agent, or coverage gaps. Full validation alone proves retained-byte integrity, not operational coverage.

## Approval boundary

Routine grounded additions may proceed. Ask first for public-web access, dependency installation, extractor-allowlist changes, source-byte adoption, delete/merge/material split, sourced-claim or relationship removal, materially contradictory interpretation, major uncertain rewrite, or ambiguous rename. Put the approval event ID in the command or wiki manifest that consumes it. Approval is bounded to the stated event and scope. The repository never installs, browses, changes authoritative source versions, performs destructive wiki work, or commits automatically.

## Canonical document index

- [Agent command and extraction reference](docs/brain/commands.md)

Policies:

- [Source handling](docs/brain/policies/source-handling.md)
- [Approvals](docs/brain/policies/approvals.md)
- [Citations](docs/brain/policies/citations.md)
- [Wiki](docs/brain/policies/wiki.md)
- [Public web](docs/brain/policies/web.md)

Workflows:

- [Onboarding, saving, and returning](docs/brain/workflows/onboarding.md)
- [Initialize](docs/brain/workflows/initialize.md)
- [Synchronize](docs/brain/workflows/synchronize.md)
- [Answer](docs/brain/workflows/answer.md)
- [Web research](docs/brain/workflows/web-research.md)
- [Wiki maintenance](docs/brain/workflows/wiki-maintenance.md)
- [Validate](docs/brain/workflows/validate.md)

Schemas:

- [Schema index](docs/brain/schemas/README.md)
- [Source record v1](docs/brain/schemas/source-record.v1.schema.json)
- [Page frontmatter v1](docs/brain/schemas/page-frontmatter.v1.schema.json)
- [Question frontmatter v2](docs/brain/schemas/question-frontmatter.v2.schema.json) (canonical)
- [Question frontmatter v1](docs/brain/schemas/question-frontmatter.v1.schema.json) (historical)
- [Citation grammar](docs/brain/schemas/citation.md)
- [Evidence packet](docs/brain/schemas/evidence-packet.md)
- [Wiki page](docs/brain/schemas/wiki-page.md)
- [Question record](docs/brain/schemas/question-record.md)

Shared role briefs:

- [Source researcher](docs/brain/agent-briefs/source-researcher.md)
- [Source ingester](docs/brain/agent-briefs/source-ingester.md)
- [Wiki curator](docs/brain/agent-briefs/wiki-curator.md)
- [Brain auditor](docs/brain/agent-briefs/brain-auditor.md)
