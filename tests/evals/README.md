# Cross-client evaluation contracts

This directory contains deterministic, version-1 inputs for the six Task 7
workflow evaluations.  The generator is the only source for checked-in
scenario JSON and fixture overlays:

```sh
python3 -m tests.evals.generate_scenarios --write
python3 -m tests.evals.generate_scenarios --check
```

`--check` builds an independent temporary tree and compares every generated
file byte-for-byte without changing this worktree.  Fixture manifests describe
only their `repo/` overlay and hash each repository-relative POSIX path with
its file digest.  Web shell, rendered-DOM, unused-candidate, and controlled
expected-PDF-Markdown assets deliberately live beside, rather than inside,
`repo/`.  Initial overlays never include `.brain`.

The web overlay contains a fully cited prior-standard topic
`wiki/pages/standard.md` and evolving question
`wiki/questions/external-standard.md`. The question is deliberately partial:
it establishes the old limit, not the current-year change. This existing
question allows the real prepublication `links candidates` command; runners
must not create an unapproved live placeholder to satisfy that command.

Raw and extracted mtimes in ledger shards are the fixed deterministic values
encoded by the generator.  The isolated runner restores those values with
`os.utime(..., follow_symlinks=False)` after copying a fixture and before any
shim or client work; ordinary copy/Git operations do not preserve them
reliably.

## Task 7c isolated runner

The public runner interface is exactly:

```sh
python3 -m tests.evals.run_cross_client --client {codex,claude} --scenario {all,<scenario-id>} --client-command-json JSON --network-attestation {workspace-egress-denied,mock-only}
```

`--client-command-json` is a JSON argv array, never a shell string. It must be
the exact closed client profile with literal runner-owned `{workspace}`,
`{mcp_config}`, and `{prompt}` slots in their documented positions. The
`{prompt}` value is only strict-decoded bytes from the runner-owned sealed
phase-prompt contract, never caller or environment text. A caller cannot
supply an executable path, workspace, MCP file, prompt, extra flag, tool,
allowlist, browser setting, or policy expansion.

Each invocation creates a disposable copied fixture workspace and a separate
private control root. It removes copied live archive/control data, overlays
only the generated fixture, restores ledgered mtimes, initializes private Git
without a commit, retains the copied original `brain` bytes, identity, and
SHA-256 beneath the private control root, and installs a root `./brain`
wrapper that package-imports the test shim. It also excludes workspace
`.context` data and any copied `tests/evals/runs/` artifacts. The command
prints the private event-log path as JSON. It never writes to a user brain,
creates a Git commit, or writes passing logs under `tests/evals/runs/`.

The public CLI and `run_scenario()` intentionally have no host-installation
registration mechanism. Consequently an ordinary invocation does not inspect
`PATH`, `HOME`, user configuration, or an arbitrary executable path; it
returns a versioned incomplete log with `client_executable_unregistered`
before probing or launching a client. Internal fake-process injection exists
only for focused tests and is permanently marked `test_process_not_actual`.
Task 7d additionally exposes a non-CLI preparation boundary,
`prepare_registered_actual_scenario()`, which accepts only a client,
scenario ID, and optional scratch root, selects the fixed symbolic profile /
attestation and runner-owned registration internally, and returns a closed
plan. It accepts no executable, JSON argv, environment, process, probe, or
Popen adapter. Its paired `run_registered_actual_scenario()` is deliberately
restricted in this slice: only the runner-registered Claude
`web-approval-and-capture` path may perform the closed phase-one probe, spawn,
streaming, and indexing sequence. It always stops after that phase with a
sealed incomplete diagnostic; it does not release phase two, perform public
semantic projection, or publish a pass. A later reviewed activation path must
bind the complete two-phase evidence boundary before any actual client launch
can be pass-capable.
For test-only capture, raw stdout is first retained byte-for-byte beneath
`<control-root>/.brain/eval-transcripts/` and then copied into indexed
evidence. A wrapper, phase configuration, or runner-state integrity failure
ends the affected run phase; it is never repaired and continued into a later
web phase.
The caller-facing template still begins with the symbolic `codex` or `claude`
token; a registered actual launch must replace only that checked token with
its registered absolute executable path before recording policy/process
evidence.

## Sealed phase-prompt contract

The following accepted Task 7a contract is consumed by Task 7c for every
injected launch. It remains the normative evidence boundary for any eventual
pass-facing registered execution.

`phase_prompt_contract.py` is the only source for client prompt bytes. It
accepts only a byte-for-byte canonical generated scenario and its declared
phase, canonicalizes the scenario JSON, and derives a deterministic UTF-8
envelope. The original request appears only as one JSON string between fixed
delimiters. The envelope lists the exact ordered standalone `EVENT:<name>`
markers, says that a marker is not evidence, and forbids any extra marker.

For ordinary scenarios, `main` owns every declared required event. The web
scenario's `approval` phase first owns the complete `initial_sync` receipt
lifecycle, then `report_local_evidence_gap` and `ask_web_approval`; it must
stop before capture or public-web work. `approved_capture` owns the remaining
declared web events and can start only after runner release.

Every pass-facing execution has exactly one indexed raw `phase_prompt`
artifact named `phase-prompt-<n>`. Its raw bytes, not a JSON wrapper, are
joined by ID, SHA-256, and transport through the run manifest, policy, and
process artifacts. The manifest also seals the canonical scenario SHA-256 and
the `second-brain-eval-phase-prompt-v1` protocol. Claude must receive the
strict UTF-8 prompt as its final argv element; Codex keeps its closed `-`
argv and records `stdin_utf8`. This proves the runner supplied exact bytes,
not that a client attended to them.

Before every injected launch, Task 7c indexes one raw `phase_prompt` artifact,
seals and rechecks its exact bytes before and after spawn, and binds the same
ID/SHA-256/transport in the fixed manifest, policy, and process records. The
Codex seam receives those raw bytes on stdin; the Claude seam receives their
strict UTF-8 decode as the final argv element. A noncanonical scenario mapping
or prompt-artifact mutation fails closed.

This adoption does not create a public pass: Task 7c still has no registered
host executable, and its injected paths remain `test_process_not_actual` with
empty public semantic arrays. Task 7d must add registered executable
provenance and a reviewed actual launch atomically; it cannot treat the sealed
prompt join alone as client-attendance evidence.

The test-only path uses one locked trace allocator for shim command intervals
and converted native JSONL rows. It copies raw command-bound shim captures only
from the private control root, checks the raw capture index against the
independent raw command log, and rejects rewritten cumulative phase prefixes.
Because its injected process seam returns complete stdout, that trace is not
evidence of live pipe-arrival interleaving and cannot support a passing actual
client run. A future registered launcher must stream stdout and allocate native
records as bytes arrive through the same host sequencing authority.

Task 7c's injected-fake path can exercise the semantic normalizer only as an
in-memory, private candidate replay. After strict writer trace/snapshot
binding, it reconstructs the candidate from sealed command captures, cursor
proofs, deliveries, references, and native transcript rows; it then
exact-rebuilds the semantic plan and exposes only canonical virtual
`marker`, `event_record`, and (for the contradictory fixture) typed
`interpretation_decision` artifacts to the replay. It rejects pre-existing
writer semantic artifacts, ID collisions, non-canonical overlays, and mutable
caller-owned trace views. The replay never writes marker, event, or decision
artifacts, populates public log semantic arrays, or marks an assertion passed:
an injected process remains `test_process_not_actual` and incomplete.

The bounded parser supports only the sourced Claude 2.1.251 stream format. It
accepts an `EVENT:<event-name>` line only from one direct root-assistant text
block when its text-byte range, native-record digest/span, transcript ID, and
shared trace sequence agree. Codex and all unsupported native formats remain
`unsupported_native_format`/incomplete.

The all-six faithful fixture-local shim replays are active and green on the
accepted v2 evaluator shape; Task 7c adds no legacy-v1 compatibility. They
remain private normalization evidence only. The marker-only negative control
is active and passing for every knowledge scenario: native markers without the
matching shim workflow cannot form a private candidate or publish semantic
evidence.

The stable validation interface is `validate_event_log(log, schema, scenario,
fixture_sha256, TrustedRunContext(...))`; it has no permissive no-context mode
and rejects a control root inside the copied workspace. A pass-capable
registered launch must create and retain that context before launch. Both paths
must already exist and be canonical absolute paths with no symlink components.
Validation pins absolute ancestries from `/`, retains every parent/child
identity edge through reads, and compares workspace/control ancestry by inode.
This is integrity evidence for the trusted host writer, not isolation from a
malicious process with the runner's Unix UID.

For each opaque 64-hex `run_id`, the control root owns
`runs/<run_id>/evidence-index.json` and `log-attestation.json`. The index is
the closed allowlist of regular, non-symlink artifact files, including
normalized relative POSIX paths, byte counts, SHA-256s, and a closed artifact
type. The validator descriptor-opens every component with no-follow semantics
and rejects unsafe root, ancestor, run, and artifact permissions. The final attestation
binds the canonical log (excluding its attestation hash), run ID, client,
scenario, fixture tree hash, and index hash.  The log may name only indexed
artifact IDs; shaped hashes, paths, evidence strings, or an invented argv are
not proof.

For a future registered launch, the policy artifact is host-written and binds
the exact client argv, client identity, policy profile, and attestation. The
locked Codex profile is its
full egress-denied `<registered-absolute-codex> exec --json --ignore-user-config --ignore-rules
--ephemeral --sandbox workspace-write -c
sandbox_workspace_write.network_access=false -C <workspace> -` command.  The
locked Claude profile is restricted/strict-MCP/no-browser/no-persistence with
its exact permitted tool and Bash surface.  Claude artifacts establish that
restricted tool profile only; they do not claim an OS firewall.
The exact Claude argv is:

```text
<registered-absolute-claude> --print --output-format stream-json --restricted --strict-mcp-config --mcp-config <indexed-absolute-MCP-path> --no-chrome --no-session-persistence --permission-mode dontAsk --tools Read,Edit,Write,Glob,Grep,Bash --allowedTools 'Read,Edit,Write,Glob,Grep,Bash(./brain *),Bash(git status *),Bash(git diff *)' --verbose <prompt>
```

Arguments are array entries, not a shell string. `--verbose` is required by
the sourced Claude print/stream-json implementation. The host-owned MCP file
must have exactly the UTF-8 bytes `{"mcpServers":{}}` followed by one LF.
Its policy `mcp_config_id` names that same indexed descriptor-opened artifact;
`mcp_path` and argv name its identical canonical absolute path. Both policy
and process record `mcp_identity` with integer fields `device`, `inode`,
`mode`, `uid`, `nlink`, `bytes`, `mtime_ns`, `ctime_ns`, observed at launch
and unchanged at validation. A second identical file is not interchangeable.
The injected test seam deliberately uses a separately sealed private phase MCP
file, so its evidence cannot be accepted as a public passing log. A future
registered launch must pass the very same indexed, descriptor-opened MCP file
to the client and retain its pre/post-launch seal.
The web scenario is two-phase: an approval-only process first, then a
fixture-ID-only mock capture process.  The fixture capability is mock-only;
workspace egress remains denied.  The runner must not overclaim that a Claude
tool allowlist is an operating-system firewall. In the current test-only
transition, phase two is enabled only after an exact Claude 2.1.251 phase-one
stream contains the trace-bound `report_local_evidence_gap` and
`ask_web_approval` markers and the raw shim audit contains only the expected
local `./brain --json sync` command. Any other output, URL attempt, or failed
phase one leaves phase two disabled. This is intentionally narrower than a
real local-research flow and is not a semantic approval implementation.

Receipt event data can only name a typed receipt. The validator derives result
ID, corpus revision, closed nonnegative event counts, and effect digest from
indexed command results and the separately captured complete JSONL stream.
Every receipt names `stream_artifact_id` (`sync_stream`) and
`durable_artifact_id` (`consumption_receipt`), containing the exact production
bytes read from `reference.path` and
`.brain/sync-results/consumed_<result_id>.json`. Consume and durable are distinct
command observations: the replay returns `already_consumed` with identical
immutable fields. The canonical delivery reference is parsed with the
production codec, and its full typed items must match the ordered,
duplicate-sensitive `handoff_source_id` effects. A delivery marker has no CLI
substitute. A branch uses the immediately preceding acknowledged delivery and
its exact extraction/rendered kind.

## Stable Task 7c normalization interface

`EVENT_RULES` in `event_log_contract.py` is the closed event registry. Every
required scenario event must have a rule; unknown events fail. Each event and
its indexed `event_record` contain the same `evidence_mode`. Every record has
exactly `run_id`, `execution_id`, `event_name`, `marker_id`, `evidence_mode`,
plus the fields below. All markers are event-specific and single-use.

| Mode | Additional record fields |
| --- | --- |
| `product_cli`, `fixture_eval` | `command_id`, `argv_sha256`, `result_sha256`, plus the rule's named references |
| `marker` | Only the rule's `packet_id` or `cursor_proof_id`, if specified |
| `marker_diff` | `diff_id`; `select_used_source` also needs `capability_id` |
| `marker_approval` | `approval_id`; `ask_interpretation_approval` also requires `interpretation_decision_id` |
| `marker_delivery` | None; event `data` contains only the delivery/item references |

Command modes have exactly one `corroboration_ids` entry, equal to
`command_id`; all other modes have none. Event `data` is empty except the
schema's receipt and delivery shapes. Command envelopes contain exactly
`ok`, `command`, `data`, `warnings`, `errors`. Successful normal commands use
exit 0, `ok: true`, and empty errors. Production `init`/`sync` with coverage
gaps may legitimately use exit 1 and `ok: false`; the complete typed envelope,
counts, gap diagnostics, receipt, delivery, and captured ledger must agree.
Other failures cannot establish a passing event. Digests of parsed values use UTF-8 canonical JSON, sorted keys,
`ensure_ascii=False`, compact separators, and no NaN. Artifact/index hashes
always hash the exact captured bytes, including any final newline.

The only command reuse groups, in event order, are:

- `public_web_access`, `snapshot_used_source`, `initial_snapshot_receipt_verified`;
- `rendered_snapshot_url`, `rendered_snapshot_receipt_verified`, `verify_snapshot_active_representation`;
- `register_extraction_handoff`, `verify_registration_active_representation`;
- a freshness/source-pass start and its drain, only when the start is already complete.

The snapshot receipt entries are views of the actual captured invocation.
They never create another command observation. The initial fixture result is
an `eval mock-web-capture` envelope whose exact `data` is
`{fixture_id, product_result}`; `product_result` is the normal full
`source snapshot-url` envelope. The rendered fixture result has exact `data`
`{fixture_id, handoff_id, path, sha256, bytes}`. Its subsequent real rendered
snapshot is a separate normal product command. Arbitrary eval commands and
raw `source snapshot-url --url` observations fail even if no event cites them.
`eval sha256` is support-only in the approved web phase and cannot prove an
event. Its result data is exactly `{path, sha256}` and must match one eventual
applied staging file, with the observation completed before publication.

Every indexed command observation must have one allowed workflow purpose;
every indexed result has exactly one observation owner. Every observed
`init`, `sync`, or static/rendered snapshot has a completed, allowed receipt.
The host trace is walked globally: after publication of a receipt only its
consume, durable replay, then acknowledgement may execute before other
commands. Extra real syncs, unreferenced mutations/captures, orphan results,
and pending receipts cannot hide outside the normalized event subsequence.

### Canonical interpretation decision evidence

Every generated evaluator QuestionRecord uses the strict product v2 schema.
Only `contradictory-evidence` has an `interpretation_policy`: its target is
`wiki/questions/what-is-alpha.md`, ID `question-what-is-alpha`, and its two
sorted complete source/content/derivation triples come from the fixture
ledger. The schema closes the policy shape; `validate_interpretation_policy`
enforces scenario ownership, exact ledger identities, sorting, and the
current `unresolved`/`withheld` combination. `approval_script` remains fixture
text and has no evaluator authority.

A contradictory pass requires exactly one indexed `interpretation_decision`
artifact, with this closed v1 shape:

```text
{schema_version: 1, run_id, execution_id, scenario_id, scenario_sha256,
 interpretation_policy_sha256,
 approval_event_record_id, approval_marker_id, approval_id,
 approval_event_id, approval_decision,
 validate_event_record_id, validate_marker_id, terminal_snapshot_id,
 wiki_apply_event_record_id, wiki_apply_command_id, wiki_manifest_id,
 question_path, question_capture_id, question_sha256, question_id,
 interpretation: {decision, preference_citation_id, approval_event_id},
 claim_identities: [{source_id, content_sha256, derivation_id},
                    {source_id, content_sha256, derivation_id}]}
```

The approval event's `interpretation_decision_id` names this artifact, which
may be created after the approval marker. The third assertion artifact also
names it through `interpretation_decision_id`. All other scenarios forbid
decision artifacts. The scenario hash uses the sealed phase-prompt helper;
the policy hash uses the same canonical JSON encoding described above.

Validation reparses the exact QuestionRecord capture in the terminal
`validate` marker's native-row workspace snapshot. Its path, capture ID,
SHA-256 and bytes must also be the final `wiki_apply` diff entry and manifest
target. A later or staged capture with equal bytes is not interchangeable.
The same snapshot supplies complete ledger, raw and extracted source bytes
for parser-backed citation definition, usage, anchor and destination checks.
The approval marker must precede the question-write marker, apply command
and marker, and terminal validate command and marker in the host trace.

For this fixture the stored decision is `unresolved`, both preference and
stored approval IDs are null, approval is `withheld`, and the final manifest
is `routine` with a null approval ID. The artifact's top-level
`approval_event_id` still identifies the withheld approval record; it is
distinct from the null stored QuestionRecord approval ID. Preference prose
cannot establish typed `preferred` state. A future generated approved policy
would also require product-valid `preferred` state, `resolve_contradiction`,
and exact equality of the stored approval ID, approval artifact event ID,
and manifest approval ID.

This contract awaits fresh review before runner adoption. The runner may
eventually build the artifact only from its own replay state and exact
terminal captures. Fake and unregistered client paths remain incomplete and
cannot publish a decision artifact, semantic events, passed assertions, or a
passing log. Contract-test builders do not constitute actual client runs.

### Receipt-time source state and whole-response parsing

A `command_observation` is exactly `{run_id, execution_id, argv, exit_code,
result_id, result_sha256, timestamp, start_sequence, end_sequence,
source_state_id}`. Timestamp is host-captured ISO-8601 and must not decrease;
the positive integer sequences come from the independent execution trace,
not timestamps or log assertions. `source_state_id` is null when unneeded,
otherwise it references an indexed `source_state` artifact:

```text
{schema_version: 1, run_id, execution_id, command_id,
 capture_point: "after_command_before_return", result_id, corpus_revision,
 records: [{path, artifact_id}], files: [{path, artifact_id}], pending_result_id}
```

`records` is the complete sorted ledger inventory, paths
`sources/ledger/src_<64hex>.json` pointing to production `source_record`
bytes. `files` is a sorted capture inventory containing `config/extractors.toml`
and the exact retained raw/extracted bytes needed for the claimed activation
and citation checks, pointing to `file_capture` bytes. Ledger omissions and
duplicate/noncanonical inventory paths fail; missing required file captures
also fail. Test capture helpers conservatively retain every source version
and derivation. The
registry must equal the fixture registry. Both arrays use repository-relative
POSIX paths, never live-workspace reads during validation. Every source-state
artifact has exactly one owning observation and a capture trace row inside
that command's start/end interval.

`pending_result_id` is null unless this state also captures the exact
`.brain/sync-results/pending.json` bytes as an indexed `pending_sync_result`
artifact. It is mandatory on each originating `sync`/`init` capture, before
consume/ack can alter or remove that file. Its production shape is exactly
`{schema_version: 1, command, generated_at, reference, result_data}`.
Command/reference/revision join the origin, and `generated_at` joins the
immutable stream header. The entire public response data, including exact
decision counts/actions/reasons/inventory metadata and sample multiplicity,
must equal this independently captured pending data. For nonempty handoffs,
production compaction replaces only `handoffs` with `handoff_response`; the
validator derives its exact canonical payload hash/path from the complete
typed public array before comparing. It does not infer historical decisions
from post-command ledger state. This establishes one canonical origin
checkpoint per receipt. The raw origin bytes must equal the producer's
compact, sorted-key UTF-8 serialization, without a trailing newline, using
the immutable stream-header time's `datetime.isoformat()` spelling. Parsed
equivalence cannot excuse changed whitespace, negative zero, or timestamp
spelling. Every supplied consume/replay pending copy must be byte-identical
to that validated origin, including schema, command, timestamp, reference
and typed data—not normalized JSON or Python object equality. Copy identity
comes from the command's owning receipt, never
from the copy's self-declared result ID/revision. Each indexed pending
artifact has one source-state owner; a different valid receipt cannot serve
as its authority. Consume/replay may omit their optional pending copy;
acknowledgement and nonreceipt captures must not retain one.
Only sync/init can establish this pending-sync authority. Snapshot origins
and every snapshot lifecycle state use `pending_result_id: null`.

Task 7b's host boundary must capture this state while it still owns the
command lock, after product completion but before returning output to the
client. Required captures are each receipt-producing invocation and first
consume (`result_id` is the production sync result ID), extraction
registration and web wiki publication (`result_id: null`). Optional captures
are permitted for durable replay, acknowledgement and other wiki apply
commands. All receipt-role source records/files must equal the validated
origin state; their expected ID/revision comes from that receipt. Registration
and wiki apply derive their null result ID and expected revision from their
actual product result. Other commands use `source_state_id: null`. Task 7c
must not reconstruct an earlier state from a later ledger or client text.

`semantic_evidence.py` validates the complete production sync/init/snapshot
data and bounded samples, not only `result_manifest`; every stream effect
kind has a closed typed parser. Public stream projections intentionally omit
internal retry/decision fields where the production codec does. Publication
and consume states must match; source effects, coverage gaps, snapshot
activation and handoff summaries join the captured ledger's exact revision.
Each delivery source additionally matches the canonical full SourceRecord
`record_sha256`, including fields excluded from the revision hash. Active
raw/output bytes, digests, sizes and visible anchors are checked. Web
publication requires an actual used citation marker plus production-parsed
definition resolving to the captured rendered source/content/derivation/
anchor; prose substrings, static/unused candidates and browser text cannot
substitute.

### Native transcript and independent ordering trace

Each process additionally records `native_format`, `trace_id`, and
`trace_sha256`. `trace_id` names an `execution_trace` artifact exactly
`{schema_version: 1, run_id, execution_id, records}`. The host writer emits
contiguous 1-based `sequence` records, never deriving them from the candidate
log or rewriting them afterward:

| `kind` | Other fields besides `sequence`, `kind` |
| --- | --- |
| `command_start`, `command_end` | `command_id` |
| `source_state` | `command_id`, `source_state_id` |
| `native_record` | `transcript_id`, `byte_start`, `byte_end`, `sha256` |

Command intervals are single-owner/nonoverlapping, all observations are
included exactly once, and source captures occur before command return.
Native rows partition the complete stdout JSONL bytes in order, including
non-marker records and the final completion/tail. Each native row hashes its
exact complete newline-terminated record. Capture ordering must come from
one host sequencing authority; Task 7b/7c must emit incomplete if separate
process streams cannot provide that witness.

A marker is exactly `{run_id, execution_id, transcript_id, native_record_start,
native_record_end, native_record_sha256, text_pointer, marker, byte_start,
byte_end, excerpt_sha256}`. The native-record fields identify a traced record; `text_pointer`
is `/message/content/<index>/text` in an approved root assistant record.
`byte_start`/`byte_end` delimit UTF-8 bytes inside that decoded text field, not
serialized JSON. The excerpt must be the full exact line `EVENT:<event-name>`
plus LF, at a line boundary, equal to `marker` and hashed by `excerpt_sha256`.
Distinct event markers must have nonoverlapping,
strictly increasing native positions. Commands finish before their marker;
new commands start after the preceding prerequisite marker. Only the closed
reuse groups above may share an invocation.

The adapter registry currently supports only
`(claude, 2.1.251, claude-stream-json-2.1.251-v1)`. Evidence is the local official
CLI's embedded schema, executable SHA-256
`625869b01e0050f260b2980fac248fd9cef9e462612bded4ec9d3d49ff8969a5`.
This is conservative source compatibility, not a real-client-run claim.
An official Python SDK installed alongside it bundles a different CLI version
and is not treated as proof of this version's wire schema. The exact sourced
field projections are implemented in `_claude_stream_2_1_251` and
`_native_nonmarker`.

The supported stream establishes one `system/init` session with matching
version, workspace, exact tool set and no MCP servers. Only root assistant
text blocks with explicit null `parent_tool_use_id` can contain markers.
Supported assistant tool-use blocks and typed `user`, `tool_progress`,
`tool_use_summary`, and allowlisted `system` payloads are never searched for
markers. System allowlist: `init`, `status` without compaction, `informational`,
`session_state_changed` without required action, basic `notification`, and
successful hook records. Unknown types/subtypes, partial streaming,
replacements, refusals, errors, denial, unsupported content/metadata or
session changes fail closed. Optional user-message identities must agree
when present. One exact typed success result is required; its `result` text
is never marker evidence. Supported assistant stop reasons are null,
`end_turn`, `tool_use`, and `stop_sequence`; completion permits null,
`end_turn`, or `stop_sequence`. Other stops, including truncation/refusal,
are incomplete in this bounded adapter. Only typed informational/system-state/basic
notification records may follow it, and all tails are parsed through EOF.

No exact Codex 0.153 wire adapter is established by the available static
evidence. Codex and any other unsupported version/format raise
`EventLogContractError("unsupported_native_format")`; Task 7c must preserve
raw stdout and emit an explicit incomplete outcome with that reason, never
invent records or a pass. New adapters require sourced or captured native
evidence and focused acceptance/rejection tests.

A `cursor_proof` is exactly
`{run_id, execution_id, family, command_ids}`. Families are `wiki_search`,
`freshness_search_batched`, `source_pass_discovery`, `source_pass_expansion`,
`source_pass_verification`, or `link_candidates`. The validator parses every
page, requiring contiguous indices, fixed run/revision/request identity,
exact continuation argv, and a final complete page without coverage gaps.
The final command is the event's primary observation.

A `diff` is exactly `{run_id, execution_id, before, after, changed_paths,
staged_paths}`. `before`/`after` are sorted inventories of
`{path, sha256, bytes, content_id}`; `content_id` references exact regular-file
bytes of type `file_capture`. Both changed and staged path lists are sorted;
changed paths must equal the computed inventory difference. The host captures
the complete relevant Git difference, including staged changes, and must not
omit archive changes. Development permits only the manifest's exact requested
`software_paths`. Staging records must match eventual publication digests.

A `wiki_manifest` artifact is `{path, content_id, sha256}` and references
the exact pre-apply manifest bytes. The production `WikiManifest` parser owns
its schema. Receipt revision and accumulated citation rewrites, completed link
proofs, staged digests, and promoted paths must all join.
Production apply may also report a regenerated `wiki/index.md` in
`changed_paths`; it must be present in the captured publication difference.
Only direct page/question changes are manifest targets. The index remains
forbidden as an agent manifest target, and an index-only update cannot prove
question publication.

An `evidence_packet` is exactly `{run_id, execution_id, kind,
corpus_revision, cursor_proof_ids, ledger_ids, citations, complete, current,
reason}`. It names completed earlier cursor proofs and a complete captured
ledger (`source_record` artifacts parsed with production codecs). Each citation
is `{document_path, citation_id, document_id}`; `document_id` references captured
Markdown, whose real citation definition must resolve to the current ledger
identity and anchor and belong to a matched wiki path. Kind is `wiki`,
`source`, or `insufficiency`; reasons are null or the closed insufficiency
values `no_supported_evidence`, `incomplete`, `stale`, `contradiction`.
Wiki sufficiency requires complete/current nonempty citations.

`run-manifest.json` has exactly `schema_version`, `run_id`, `client`,
`scenario_id`, `fixture_id`, `fixture_sha256`, `workspace`, `phases`,
`policy_profiles`, `fixture_capability_id`, `approval`, `software_paths`,
`scenario_sha256`, `phase_prompt_protocol`, and `phase_prompts`.
`phase_prompts` is execution-ordered and each entry is exactly
`{execution_id, phase, phase_prompt_id, phase_prompt_sha256,
phase_prompt_transport}`. `approval` is null or `{event_id, scope, note,
decision}`. Policy and process artifacts additionally bind `fixture_id`,
`fixture_sha256`, `fixture_capability_id`, `approval`, and the identical
phase-prompt ID/SHA-256/transport triple. Phase one has no capability or
approval grant; only the approved-capture phase repeats the manifest grant.

Approval artifacts are exactly `{run_id, execution_id, event_id, scope, note,
decision, fixture_id, capability_id, manifest_id}`. Web decisions must be
approved and name the initial fixture plus the manifest-selected capability;
`manifest_id` is null. Interpretation decisions bind the staged apply
manifest and have null fixture/capability. The contradictory scenario's
scripted `denied` or `withheld` decision is valid but cannot authorize a
preferred interpretation or web access.

Fixture capabilities contain exactly `run_id`, `execution_id`, `phase`,
`fixture_id`, `fixture_sha256`, `capability_id`, `approval_event_id`, `scope`,
`note`, `enabled_fixture_ids`, `descriptor_id`. The enabled IDs are exactly
the initial and rendered IDs in that order. The descriptor is exactly
`{fixture_id, fixture_sha256, static_sha256, rendered_sha256, unused_sha256,
final_url, media_type, retrieved_at, redirect_urls}`. Asset hashes match the
generator-owned fixture bytes. Only the descriptor's fixed final URL and
redirects may appear in the real rendered snapshot metadata command; eval
argv cannot carry a URL.

`test_event_evidence.py` provides temporary contract-test artifact builders
and exercises a complete current-wiki scenario with real product CLI outputs.
`test_workflow_acceptance.py` additionally exercises complete binary handoff
and two-phase static/rendered web workflows, plus the empty-wiki first
question, through the public validator,
including real receipt lifecycle, source capture, search and publication.
Its test process/transcript records are contract fixtures, not actual client
evaluations. Task 7c must capture real client records and must emit incomplete
when any witness is missing; it must never use these builders to produce a
passing client log.

No passing log may be hand-authored, copied between clients, or copied between
scenarios.  There are intentionally no passing logs yet: missing, failing, or
incomplete logs must block the later release gate.
