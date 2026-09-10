# Task 7 Evaluation Integrity Amendments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` task-by-task. Tasks use checkbox syntax for tracking.

**Goal:** Make every eventual cross-client pass bind an exact runner-owned prompt, a complete web approval lifecycle, and a durable typed wiki interpretation decision to immutable evidence.

**Architecture:** A shared pure prompt-contract module owns canonical scenario bytes, phase partitions, and exact launch prompt bytes. The wiki model advances to a versioned v2 question frontmatter with a typed interpretation state enforced before publication. The evaluator then captures that canonical state together with its transaction/approval/snapshot evidence; the runner only consumes those stable contracts and remains incomplete until a real registered client proves them.

**Tech Stack:** Python 3, `pytest`, `ruff`, JSON Schema Draft 2020-12 subset, `markdown-it-py` CommonMark structure, existing source/citation/wiki transaction libraries.

**Spec:** [original agent-integration plan](2026-09-04-second-brain-lite-agent-integration-docs.md), the existing Task 7 briefs, and `.superpowers/sdd/2026-09-04-second-brain-lite-agent-integration-docs/task-7a-prompt-web-amendment-brief.md` / `task-7a-interpretation-state-amendment-brief.md`.

## Global Constraints

- All source, extraction, ledger, wiki, scenario, fixture, and evidence bytes remain committed; no user brain or public web is touched by tests.
- Deterministic code owns IDs, digests, artifact joins, phase state, and validation; agents own semantic judgment only where a typed canonical product field records it.
- A public web capability is released only after phase-one local evidence and explicit approved fixture authority; no raw URL or live request is accepted.
- All launch prompts are runner-owned exact UTF-8 bytes. A client marker is never evidence by itself.
- No task stages, commits, reverts, or auto-commits. The user requires one final session commit only after the entire branch passes validation.
- Every behavioral change follows RED → GREEN → focused review. Existing dirty worktree changes are other work unless explicitly owned below.

---

### Task 1: Canonical phase prompt and web receipt contract

**Files:**

- Create: `tests/evals/phase_prompt_contract.py`
- Modify: `tests/evals/event_log_contract.py`, `tests/evals/scenario.v1.schema.json`, `tests/evals/generate_scenarios.py`, generated `tests/evals/scenarios/**`, `tests/evals/README.md`
- Test: Task 7a contract/evidence/acceptance builders, excluding the concurrently owned runner files.

**Interfaces:**

```python
PROMPT_PROTOCOL = "second-brain-eval-phase-prompt-v1"

def canonical_scenario_bytes(scenario: Mapping[str, Any]) -> bytes: ...
def scenario_sha256(scenario: Mapping[str, Any]) -> str: ...
def required_events_for_phase(scenario: Mapping[str, Any], phase: str) -> tuple[str, ...]: ...
def canonical_phase_prompt_bytes(scenario: Mapping[str, Any], phase: str) -> bytes: ...
```

The runner later consumes these functions; the public validator recomputes the exact same bytes.

- [ ] **Step 1: Write failing prompt and phase-partition tests.**

```python
def test_web_approval_prompt_requires_acknowledged_local_sync_before_stop():
    scenario = generated_scenario("web-approval-and-capture")
    assert required_events_for_phase(scenario, "approval") == (
        "initial_sync_receipt_verified", "initial_sync_receipt_consumed",
        "initial_sync_receipt_durable", "initial_sync_receipt_acknowledged",
        "report_local_evidence_gap", "ask_web_approval",
    )
    assert b"EVENT:public_web_access" not in canonical_phase_prompt_bytes(scenario, "approval")
```

- [ ] **Step 2: Run the focused prompt/fixture tests and observe RED.**

Run: `pytest -q tests/evals/test_scenario_contracts.py tests/evals/test_event_evidence.py -k 'prompt or web or phase'`

Expected: imports/partitions or strict artifact joins are missing.

- [ ] **Step 3: Implement the pure contract and generated scenario repair.**

Render the request as one JSON string between fixed delimiters. Add `initial_sync` receipt events before web gap/approval and preserve all fixture tree bytes. For every execution index raw `phase_prompt` bytes and bind `phase_prompt_id`, SHA-256, and client-specific transport across run manifest, policy, and process.

- [ ] **Step 4: Add adversarial mutation tests and make them green.**

Reject a one-byte/Unicode/NUL/newline mismatch, wrong scenario SHA/protocol/transport, phase swap/reuse/orphan, raw scenario prompt in Claude argv, old evidence missing fields, missing/mismatched/unacknowledged phase-one sync, and phase-two event in approval phase.

- [ ] **Step 5: Verify and request scoped review.**

Run: `python3 tests/evals/generate_scenarios.py --check`, focused evaluation suites, `ruff check` on owned files, `python3 -m compileall -q tests/evals`, and `git diff --check`.

Record exact RED/GREEN output in the Task 7a amendment report; do not commit.

### Task 2: Versioned canonical wiki interpretation state

**Files:**

- Create: `docs/brain/schemas/question-frontmatter.v2.schema.json`, `brainlib/wiki_interpretations.py`
- Modify: `brainlib/wiki_models.py`, `brainlib/wiki_transaction.py`, `brainlib/validation.py`, BRAIN/schema/policy/workflow/curator docs, canonical answer and wiki-maintenance skills, product fixture generator and product/unit/integration/doc tests.
- Test: `tests/unit/test_wiki_models.py`, `tests/unit/test_wiki_interpretations.py`, `tests/unit/test_wiki_transaction.py`, `tests/unit/test_validation.py`, fixture and doc integration suites.

**Interfaces:**

```python
@dataclass(frozen=True)
class InterpretationDecision:
    decision: Literal["not_applicable", "unresolved", "preferred"]
    preference_citation_id: str | None
    approval_event_id: str | None

def validate_interpretation_document(
    path: Path, text: str, record: QuestionRecord,
) -> tuple[ValidationIssue, ...]: ...

def validate_interpretation_transitions(
    *, before: Mapping[Path, str], after: Mapping[Path, str],
    changed_paths: Collection[Path], change_intent: str,
    approval_event_id: str | None,
) -> None: ...
```

- [ ] **Step 1: Write v2 parse/schema RED tests.**

```python
def test_question_v2_rejects_preferred_without_exact_citation_and_approval():
    with pytest.raises(ValueError, match="interpretation"):
        parse_question(path, text=question_v2(decision="preferred"))
```

Cover v1/missing/wrong scalar schema version, invalid state/status shapes, partial/unknown companion fields, and valid v2 forms.

- [ ] **Step 2: Write scanner and transaction RED tests.**

Prove that code/definition-only, cross-document, missing, or Current-answer-only selected citations fail; unresolved needs two distinct current resolving citations in Contradictory evidence. Prove routine preferred, wrong/missing approval, multiple decision transitions, citation-ID rebinding/removal, preferred downgrade, and prohibited withdrawal fail.

- [ ] **Step 3: Implement v2 parser/model and CommonMark-backed validation.**

Keep the narrow scalar parser: `schema_version: 2` is string literal `"2"`. Keep v1 schema documentation unchanged, but require v2 for canonical QuestionRecords. Reuse existing citation parser/resolution and parser-approved headings/markers; never classify prose as preference.

- [ ] **Step 4: Enforce postimage transitions before publication.**

Require `resolve_contradiction` plus exact stored approval for every resulting/changed preferred state, and exactly one decision transition in such a manifest. Allow only conservative new/not-applicable → unresolved under routine/null approval; reject unresolved/preferred → not-applicable pending an explicit future design.

- [ ] **Step 5: Migrate docs and product fixtures, then verify.**

Regenerate source-owned fixtures; run focused unit/integration/docs tests, fixture generator `--check`, Ruff, compilation, and `git diff --check`. Obtain a fresh scoped review before downstream evaluator work.

### Task 3: Evaluation evidence binds canonical interpretation state

**Files:**

- Modify: `tests/evals/scenario.v1.schema.json`, `tests/evals/generate_scenarios.py`, generated scenarios/fixtures, `tests/evals/event_log_contract.py`, Task 7a evidence builders/tests, `tests/evals/README.md`
- Test: evaluator contract, marker snapshot, workflow acceptance, and contradictory-state mutation tests.

**Consumes:** Task 1 prompt/hash contracts and Task 2 v2 QuestionRecord/transition guarantees.

**Produces:** A typed `interpretation_decision` evidence artifact and closed third contradictory assertion semantics for the runner.

- [ ] **Step 1: Write failing artifact join tests.**

```python
def test_contradictory_decision_must_bind_terminal_v2_question_and_manifest():
    log = valid_contradictory_pass_log()
    mutate_terminal_question_state_or_snapshot(log)
    with pytest.raises(EventLogContractError):
        validate_event_log(...)
```

- [ ] **Step 2: Add generator-owned interpretation policy.**

The contradictory scenario owns sorted fixture claim triples, expected question path/ID, `preserve_both`, and withheld approval. Do not derive semantic authority from approval prose.

- [ ] **Step 3: Implement strict terminal artifact validation.**

The artifact joins run/execution/scenario hash, ask marker, final validate marker/snapshot, final apply manifest, terminal captured QuestionRecord SHA/path, exact v2 state, two ledger-resolving citation identities, and the withheld/approved approval state. Require exactly one contradictory decision and none elsewhere.

- [ ] **Step 4: Preserve the anti-prose regression and verify.**

Body prose saying `prefer ...` without typed `preferred` remains unproven. Mutate wrong snapshot, stale triple, wrong question/manifest, generic approval, selected state under routine, and terminal marker order; all must fail.

- [ ] **Step 5: Run generator/evaluator regressions and scoped review.**

Run generator check, affected full-pass builders and strict validator suites, Ruff, compilation, and diff check. Do not modify the runner in this task.

### Task 4: Runner adopts sealed prompts and private all-scenario replay

**Files:**

- Modify: `tests/evals/run_cross_client.py`, `tests/evals/test_run_cross_client.py`, runner README section.

**Consumes:** Task 1 raw prompt contract and Task 3 evaluator interpretation artifact contract.

**Produces:** Honest incomplete fake runs with private reconstructed lifecycle validation; no fake pass/evidence publication.

- [ ] **Step 1: Add RED runner launch-binding tests.**

Assert Claude gets exact decoded phase-prompt bytes as final argv, Codex gets raw bytes on stdin, and any changed/resealed/swapped prompt fails incomplete. Assert web phase two never launches before complete phase-one receipt lifecycle and approved capability.

- [ ] **Step 2: Implement sealed runner handoff.**

Write/index/seal the raw prompt before spawn, recheck it before/after spawn, then bind policy/process fields. The runner imports the shared pure contract and never reconstructs a parallel envelope.

- [ ] **Step 3: Complete private replay tests for all six scenarios.**

Each fake client uses only the real fixture shim; exact-rebuild semantic plans from writer-owned artifacts after trace/snapshot binding. The virtual overlay rejects pre-existing marker/event artifacts, collisions, orphans, and emits no public marker/event records, semantic arrays, passing assertions, or pass result.

- [ ] **Step 4: Fix remaining parser-bound assertion predicates.**

Use CommonMark inline-token maps over retained bytes—not physical line adjacency—to ensure every visible factual block is exactly cited. Keep failed-prose and mutation regressions.

- [ ] **Step 5: Run adversarial review and full Task 7c verification.**

Run focused all-six, malicious argv/MCP/raw URL/phase/snapshot/prompt/semantic tests plus Ruff, compilation, generator check, and diff check. Record runner limitations: injected fake clients remain incomplete and Codex has no supported native schema.

### Task 5: Runner-owned executable registration and real-client evidence

**Files:**

- Modify only Task 7d-owned launcher/runner artifacts and tests after Tasks 1–4 are accepted.

**Consumes:** sealed phase prompts, full phase lifecycle, v2 canonical decision/evaluator evidence, and private fake replay coverage.

**Produces:** a real Claude scenario run only if each process is runner-registered, streamed at marker time, and passes the strict public validator; Codex remains incomplete until its schema/build is source-established.

- [ ] **Step 1: Add RED live-stream registration tests.**

Require absolute pinned executable, launch/seal identity/digest, version/help probes, stream-at-arrival workspace snapshots, and exact prompt transport evidence. A final `communicate()` replay cannot satisfy marker-time evidence.

- [ ] **Step 2: Implement live Popen streaming and phase barriers.**

Record each native stdout row, trace event, and workspace snapshot as it arrives. Release phase two only after full phase-one transcript/prompt/shim/state/receipt/capability seals verify.

- [ ] **Step 3: Run bounded real-client scenarios only after registration is accepted.**

No public web browsing is authorized by this plan. Fixture-only web paths remain local/mock. Treat unsupported adapter/build, transport mismatch, any raw URL, or missed join as incomplete.

- [ ] **Step 4: Broad final review and validation.**

Run all relevant evaluator/product tests, `./brain --json validate --full`, generator checks, Ruff, compilation, and diff check. Obtain whole-branch review before the user-authorized single final commit.

## Self-review

- Prompt evidence, web receipt lifecycle, product state, terminal evaluator joins, fake replay, and real streaming each have an owning task.
- No task uses prose as semantic proof; all approval/citation state has a typed artifact or product field.
- The plan does not authorize public web, installing dependencies, a commit, push, merge, or external publication.
- Task interfaces use the exact shared prompt and typed-state names introduced above.
