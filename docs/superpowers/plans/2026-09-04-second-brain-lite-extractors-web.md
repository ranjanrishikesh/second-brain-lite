# Second Brain Lite Extractors and Web Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every supported local or user-approved public-web source yield a validated, versioned, searchable Markdown representation through the canonical source-ledger workflow.

**Architecture:** Plan 2 owns the extractor registry, inventory, source ledger, reconciliation, and outer source-write lock. This plan supplies a zero-argument deterministic processor factory, bounded worker execution behind Plan 2's reconciliation seam, immutable agent handoffs, and a web-capture command that versions faithful raw bytes before invoking the same processor. Workers create results only; reconciliation or the owning command activates results and invokes the caller-supplied checkpoint.

**Tech Stack:** Python 3.11+, standard library (`concurrent.futures`, `hashlib`, `html.parser`, `http.client`, `importlib.util`, `ipaddress`, `json`, `pathlib`, `socket`, `ssl`, `subprocess`, `urllib.parse`, `zipfile`, `zlib`), pytest, and optional allowlisted converter extras.

**Spec:** `docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`

## Global Constraints

- Code performs mechanics; LLMs perform judgment; Markdown and Git preserve truth.
- Add no database, vector index, daemon, hosted backend, browser service, automatic package installation, or implicit network access.
- Plan 2 solely owns `config/extractors.toml`, `brainlib/registry.py`, `brainlib/inventory.py`, `LedgerStore`, `SourceWriteLock`, and source-reconciliation policy. Extend only the documented processor seam and CLI/service wiring; do not create competing schemas.
- Invoke only an installed converter returned by Plan 2's `resolve_converter()` and build argv only with `build_converter_argv()`. Never execute a shell string, unknown tool, document macro, embedded executable, input-supplied command, or package manager.
- Including native Markdown and text, every active representation is normalized to one `.md` file below `sources/extracted/` at canonical `derive_extraction_path()` output. No active representation points directly to mutable raw bytes.
- Generic inventory excludes `_versions/**` and `_web/**`; explicit web capture alone creates and reconciles `_web` content versions.
- URL descriptors are mutable control metadata, not evidence. `./brain init` and `./brain sync` perform no network access. A never-captured descriptor, or a descriptor whose URL changed since its active retrieval, is `awaiting_approval`; a captured unchanged descriptor retains its current state and active representation until an approved refresh occurs.
- Before public-web access, the calling agent must ask the user. One nonempty `approval_event_id` may cover every retrieval inside the same explicitly bounded, user-approved research event and scope. Its first successful use records one `approval_recorded_at`; subsequent uses reuse that timestamp and must supply byte-for-byte-identical scope/note. A later event or expanded scope requires a new event ID. The CLI records the agent's approval claim in every `RetrievalMetadata`; it does not independently prove that approval occurred.
- Re-fetching identical bytes appends a new immutable `RetrievalMetadata` to the existing hash-keyed `ContentVersion`. Changed bytes create a new `ContentVersion`. Neither case overwrites an earlier retrieval event, version, or raw artifact.
- Empty, corrupt, encrypted, timed-out, truncated, unsupported, or low-confidence work remains a visible `failed`, `warning`, `pending`, `needs_agent`, or `unsupported` gap; it never becomes silent `ok`.
- The command owns the single outer `SourceWriteLock`. Workers and processor helpers never acquire that lock, construct a `LedgerStore`, or persist records. Plan 2 reconciliation owns init/sync result activation and calls its `checkpoint` callback after every changed record; other source commands use the same coordinator pattern.
- Never auto-commit. Each task ends in a non-committing review checkpoint. Only the last plan in a combined conversation may prepare one user- or host-requested session/PR commit after all applicable gates pass.

---

## Canonical dependencies

Import, do not duplicate:

- From `brainlib.contracts`: `Anchor`, `ContentVersion`, `Derivation`, `FileFingerprint`, `ProcessingAttempt`, `RetrievalMetadata`, `SourceRecord`, `SourceState`, `UrlDescriptorMetadata`, `compute_corpus_revision`, `compute_sha256`, and `derivation_id`. Every newly published deterministic derivation sets `method="deterministic"` and exact nonempty-string metadata `{converter_id, converter_version}`; every agent derivation sets `method="agent"` and exact nonempty-string metadata `{handoff_id, agent_revision, note}`.
- From `brainlib.ledger`: `LedgerStore`, `ActivationGuard`, `SourceRepresentation`, `activate_derivation`, `derive_extraction_path`, and `transition`. Canonical `ContentVersion.raw_path` is relative to `sources/raw`; canonical `Derivation.output_path` and `SourceRepresentation.extracted_path` are relative to the repository root.
- From `brainlib.registry`: `ConverterSpec`, `ExecutionMode`, `ExtractorRegistry`, `ExtractorSpec`, `ResolvedConverter`, `RunVersionProbe`, `build_converter_argv`, `detect_converter_version`, `effective_extractor_version`, `prerequisite_digest`, and `resolve_converter`. Each derivation uses `ExtractorSpec.config_sha256` and the already-computed `ProcessingContext.extractor_version`; it does not substitute the registry-wide digest or accept identity fields from a CLI caller.
- From `brainlib.inventory`: `InventoryItem`, `UrlDescriptor`, `inventory_raw_sources`, `parse_url_descriptor`, and `source_id_for_url_descriptor`.
- From `brainlib.sync`: `ProcessResult`, `ProcessingContext`, `SourceProcessor`, `SyncReport`, and `reconcile_inventory`.
- From `brainlib.commands`: `CommandServices(processor_factory: Callable[[], SourceProcessor], prerequisite_digest: Callable[[ExtractorSpec], str])`.

Plan 2 also adds `ExtractorSpec.agent_fallback: bool = False` and `ExtractorSpec.agent_revision: str | None = None`. Deterministic preferred/fallback implementations are attempted first; if none is available or suitable and `agent_fallback` is true, the result is `NEEDS_AGENT` and receives an extraction handoff. Such an extractor must have a canonical positive-decimal `agent_revision`; an extractor without agent fallback must have `None`. It does not change `ExtractorSpec.mode` or permit an unregistered command.

The deterministic processor resolves once per attempt with `resolve_converter(extractor, run=version_probe)`. It uses only `ResolvedConverter.converter` and verifies both `ResolvedConverter.prerequisite_digest == ProcessingContext.prerequisite_digest` and `effective_extractor_version(extractor, resolved.prerequisite_digest) == ProcessingContext.extractor_version`. If either differs, return a retryable `prerequisite_changed` diagnostic before executing the converter. This prevents selected converter/version drift between reconciliation and execution.

Use canonical IDs only: `src_` plus 64 lowercase hexadecimal characters, `drv_` plus 64 lowercase hexadecimal characters, and the deterministic `hnd_` handoff ID specified in Task 3.

## File structure and execution order

Implement Tasks 1 through 5 in order. Every test in a task uses only Plan 1, Plan 2, or interfaces introduced no later than that task.

```text
pyproject.toml
brainlib/capabilities.py
brainlib/extractors/__init__.py
brainlib/extractors/processor.py
brainlib/extractors/native.py
brainlib/extractors/adapters.py
brainlib/extractors/handoff.py
brainlib/sources/__init__.py
brainlib/sources/web.py
brainlib/commands.py
brainlib/cli.py
brainlib/sync.py
brainlib/validation.py
tests/conftest.py
tests/helpers_extractors.py
tests/unit/test_capabilities.py
tests/unit/test_deterministic_processor.py
tests/unit/test_external_adapters.py
tests/unit/test_agent_handoff.py
tests/unit/test_web_capture.py
tests/integration/test_core_extractors.py
tests/integration/test_url_snapshot_flow.py
tests/fixtures/generate_extractors.py
tests/fixtures/extractors/manifest.json
tests/fixtures/extractors/core/
tests/fixtures/extractors/failures/
tests/fixtures/extractors/expected/README.md
tests/fixtures/web/
```

### Task 1: Add capability probes, exact processor interfaces, and bounded reconciliation

**Files:**
- Modify: `pyproject.toml`
- Create: `brainlib/capabilities.py`
- Create: `brainlib/extractors/__init__.py`
- Create: `brainlib/extractors/processor.py`
- Modify: `brainlib/commands.py`
- Modify: `brainlib/cli.py`
- Modify: `brainlib/validation.py`
- Modify: `brainlib/sync.py`
- Create: `tests/helpers_extractors.py`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_capabilities.py`
- Test: `tests/unit/test_deterministic_processor.py`

**Interfaces:**
- Consumes: Plan 2 `ExtractorSpec`, `ResolvedConverter`, `ProcessingContext`, `ProcessResult`, `SourceProcessor`, `CommandServices`, `reconcile_inventory()`, and Plan 1's `repo_root`/`repo_paths` fixtures.
- Produces: the exact types and functions below. Tasks 2-4 import these names rather than redefining them.

```python
# brainlib/capabilities.py
@dataclass(frozen=True)
class Capability:
    converter_id: str
    available: bool
    detected_version: str | None
    detail: str
    install_recipes: Mapping[str, tuple[str, ...]]

def probe_capabilities(
    registry: ExtractorRegistry, *, run: RunVersionProbe
) -> Mapping[str, Capability]: ...

# brainlib/extractors/processor.py
@dataclass(frozen=True)
class CommandExecution:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False

class RunCommand(Protocol):
    def __call__(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: int,
        max_output_bytes: int, pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution: ...

ResolveConverter: TypeAlias = Callable[[ExtractorSpec], ResolvedConverter | None]

@dataclass
class StagedInput(AbstractContextManager["StagedInput"]):
    """Owned, descriptor-pinned copy of the already verified Plan 2 input."""
    logical_path: PurePosixPath
    sha256: str
    byte_size: int
    descriptor: int
    descriptor_path: Path
    def close(self) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *args: object) -> None: ...

def stage_pinned_input(
    context: ProcessingContext, *, paths: RepoPaths,
    logical_path: PurePosixPath, expected_byte_size: int,
) -> StagedInput: ...

@dataclass(frozen=True)
class Job:
    record: SourceRecord
    item: InventoryItem
    extractor: ExtractorSpec
    context: ProcessingContext
    staged_input: StagedInput

@runtime_checkable
class BatchSourceProcessor(SourceProcessor, Protocol):
    def iter_batch(
        self, jobs: Iterable[Job], *, paths: RepoPaths,
        max_workers: int | None = None,
    ) -> Iterator[tuple[str, ProcessResult]]: ...

class DeterministicSourceProcessor:
    def __init__(
        self, *, run: RunCommand | None = None,
        resolve: ResolveConverter | None = None,
    ) -> None: ...
    def process(
        self, record: SourceRecord, item: InventoryItem,
        extractor: ExtractorSpec, *, paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult: ...
    def iter_batch(
        self, jobs: Iterable[Job], *, paths: RepoPaths,
        max_workers: int | None = None,
    ) -> Iterator[tuple[str, ProcessResult]]: ...

def build_source_processor() -> SourceProcessor:
    return DeterministicSourceProcessor()

# additive Plan 2 signature extension in brainlib/sync.py
def reconcile_inventory(
    inventory: InventoryReport,
    records: Mapping[str, SourceRecord],
    *,
    registry: ExtractorRegistry,
    processor: SourceProcessor,
    paths: RepoPaths,
    prerequisite_digests: Mapping[str, str],
    max_workers: int | None = None,
    hash_file: Callable[[Path], str] = compute_sha256,
    checkpoint: Callable[[SourceRecord], None] | None = None,
    now: datetime,
) -> tuple[dict[str, SourceRecord], SyncReport]: ...
```

- [ ] **Step 1: Add concrete shared test builders before referring to fixtures**

Add these helpers to `tests/helpers_extractors.py`. `make_source_record` comes from Plan 2's `tests.helpers`; all path fields remain in canonical coordinate systems.

```python
import io
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from brainlib.contracts import (
    ContentVersion, FileFingerprint, SourceState, compute_sha256,
    source_id_for_first_seen,
)
from brainlib.cli import main
from brainlib.commands import CommandServices
from brainlib.diagnostics import JSONValue
from brainlib.extractors.processor import Job
from brainlib.inventory import InventoryItem
from brainlib.layout import RepoPaths
from brainlib.registry import (
    ExtractorRegistry, effective_extractor_version,
)
from brainlib.sync import ProcessingContext
from tests.helpers import make_source_record

FIXED_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

def make_job(
    paths: RepoPaths, relative: str, body: bytes, media_type: str,
    *, prerequisite: str = "a" * 64,
) -> Job:
    relative_path = PurePosixPath(relative)
    absolute = paths.raw / relative_path
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(body)
    stat = absolute.stat()
    sha256 = compute_sha256(absolute)
    fingerprint = FileFingerprint(relative_path, stat.st_size, stat.st_mtime_ns)
    item = InventoryItem(
        fingerprint=fingerprint, media_type=media_type,
        extension=absolute.suffix.lower(), sha256=sha256,
    )
    extractor = ExtractorRegistry.load(paths.registry).select(
        media_type, relative_path,
    )
    assert extractor is not None
    version = ContentVersion(
        sha256, relative_path, stat.st_size, fingerprint, FIXED_NOW, (),
    )
    record = replace(
        make_source_record(
            source_id=source_id_for_first_seen(relative_path, sha256)
        ),
        current_raw_path=relative_path,
        previous_raw_paths=(),
        media_type=media_type,
        byte_size=stat.st_size,
        state=SourceState.PENDING,
        versions={sha256: version},
        active_content_sha256=sha256,
        derivations={},
        active_derivation_id=None,
        last_attempt=None,
        diagnostics=(),
        url_descriptor=None,
    )
    staged_input = stage_test_input_from_verified_bytes(paths, relative_path, body)
    context = ProcessingContext(
        input_sha256=sha256,
        extractor_version=effective_extractor_version(extractor, prerequisite),
        config_sha256=extractor.config_sha256,
        prerequisite_digest=prerequisite,
        attempted_at=FIXED_NOW,
        input_path=staged_input.descriptor_path,
        input_descriptor=staged_input.descriptor,
    )
    return Job(record, item, extractor, context, staged_input)

def make_jobs(paths: RepoPaths, count: int = 6) -> list[Job]:
    return [
        make_job(paths, f"notes/{index}.txt", f"item {index}\n".encode(), "text/plain")
        for index in range(count)
    ]

@dataclass(frozen=True)
class InProcessResult:
    returncode: int
    stdout: str
    stderr: str

def run_brain(
    repo_root: Path, *argv: str,
    services: CommandServices | None = None,
) -> InProcessResult:
    stdout, stderr = io.StringIO(), io.StringIO()
    returncode = main(
        list(argv), cwd=repo_root, stdout=stdout, stderr=stderr,
        services=services,
    )
    return InProcessResult(returncode, stdout.getvalue(), stderr.getvalue())

def run_brain_json(
    repo_root: Path, *argv: str,
    services: CommandServices | None = None,
    expected_returncodes: frozenset[int] = frozenset({0, 1}),
) -> dict[str, JSONValue]:
    result = run_brain(repo_root, "--json", *argv, services=services)
    assert result.returncode in expected_returncodes, result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == {"command", "ok", "data", "warnings", "errors"}
    return payload
```

In `tests/conftest.py`, expose only thin pytest fixtures; do not shadow Plan 1's `repo_root` or `repo_paths`:

```python
@pytest.fixture
def jobs(repo_paths: RepoPaths) -> list[Job]:
    return make_jobs(repo_paths)

@pytest.fixture
def processor() -> DeterministicSourceProcessor:
    return DeterministicSourceProcessor()
```

- [ ] **Step 2: Write the failing capability, factory, queue-bound, and checkpoint tests**

```python
def test_doctor_reports_missing_optional_tool_without_installing(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brainlib.capabilities.shutil.which", lambda _: None)
    payload = run_brain_json(repo_root, "doctor")
    assert payload["command"] == "doctor"
    assert payload["ok"] is True
    assert payload["data"]["capabilities"]["pdftotext"]["available"] is False
    assert payload["data"]["capabilities"]["pdftotext"]["install_recipes"]

def test_factory_returns_deterministic_processor() -> None:
    assert isinstance(build_source_processor(), DeterministicSourceProcessor)

def test_iter_batch_does_not_consume_more_than_worker_bound_before_yield(
    jobs: list[Job], repo_paths: RepoPaths,
) -> None:
    tracking = TrackingJobs(jobs)
    processor = ImmediateProcessor()
    iterator = processor.iter_batch(tracking, paths=repo_paths, max_workers=2)
    first = next(iterator)
    assert tracking.consumed == 2
    results = [first, *iterator]
    assert {source_id for source_id, _ in results} == {
        job.record.source_id for job in jobs
    }

@pytest.mark.parametrize("value", [0, 17])
def test_worker_bound_rejects_values_outside_one_to_sixteen(
    value: int, processor: DeterministicSourceProcessor,
    jobs: list[Job], repo_paths: RepoPaths,
) -> None:
    with pytest.raises(ValueError, match="1..16"):
        list(processor.iter_batch(jobs, paths=repo_paths, max_workers=value))

def test_reconciliation_uses_checkpoint_and_does_not_construct_ledger_store(
    repo_paths: RepoPaths, jobs: list[Job], monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointed: list[SourceRecord] = []
    monkeypatch.setattr(
        "brainlib.sync.LedgerStore",
        lambda *_: pytest.fail("reconciliation must not construct LedgerStore"),
        raising=False,
    )
    records, _ = reconcile_jobs_fixture(
        jobs, paths=repo_paths, checkpoint=checkpointed.append, max_workers=2,
    )
    assert {record.source_id for record in checkpointed} == set(records)

def test_checkpointed_result_survives_interruption(
    repo_paths: RepoPaths, jobs: list[Job],
) -> None:
    durable: dict[str, SourceRecord] = {}
    def checkpoint(record: SourceRecord) -> None:
        durable[record.source_id] = record
        if len(durable) == 1:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        reconcile_jobs_fixture(
            jobs, paths=repo_paths, checkpoint=checkpoint, max_workers=2,
        )
    assert len(durable) == 1
    assert next(iter(durable.values())).active_derivation_id is not None
```

Implement these exact scheduler fakes in `tests/unit/test_deterministic_processor.py`:

```python
def successful_process_result(job: Job, *, paths: RepoPaths) -> ProcessResult:
    identifier = derivation_id(
        source_sha256=job.context.input_sha256,
        extractor_id=job.extractor.extractor_id,
        extractor_version=job.context.extractor_version,
        config_sha256=job.extractor.config_sha256,
    )
    relative = derive_extraction_path(
        job.item.fingerprint.path, job.context.input_sha256, identifier,
    )
    output = paths.extracted / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('<a id="line:1"></a>\nfixture\n', encoding="utf-8")
    stat = output.stat()
    derivation = Derivation(
        identifier,
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        paths.repo_relative(output),
        compute_sha256(output),
        stat.st_size,
        stat.st_mtime_ns,
        "ok",
        (Anchor("line", "1"),),
        job.context.attempted_at,
        method="deterministic",
        method_metadata={
            "converter_id": "fixture.converter",
            "converter_version": "fixture-1",
        },
    )
    attempt = ProcessingAttempt(
        job.context.input_sha256,
        job.extractor.extractor_id,
        job.context.extractor_version,
        job.extractor.config_sha256,
        job.context.prerequisite_digest,
        SourceState.OK,
        job.context.attempted_at,
        (),
    )
    return ProcessResult(SourceState.OK, derivation, attempt, ())

class TrackingJobs:
    def __init__(self, jobs: Sequence[Job]) -> None:
        self.jobs = jobs
        self.consumed = 0
    def __iter__(self) -> Iterator[Job]:
        for job in self.jobs:
            self.consumed += 1
            yield job

class ImmediateProcessor(DeterministicSourceProcessor):
    def process(
        self, record: SourceRecord, item: InventoryItem,
        extractor: ExtractorSpec, *, paths: RepoPaths,
        context: ProcessingContext,
    ) -> ProcessResult:
        with stage_pinned_input(
            context,
            paths=paths,
            logical_path=item.fingerprint.path,
            expected_byte_size=item.fingerprint.byte_size,
        ) as staged_input:
            return successful_process_result(
                Job(
                    record,
                    item,
                    extractor,
                    context,
                    staged_input,
                ),
                paths=paths,
            )

def reconcile_jobs_fixture(
    jobs: Sequence[Job], *, paths: RepoPaths,
    checkpoint: Callable[[SourceRecord], None],
    max_workers: int,
) -> tuple[dict[str, SourceRecord], SyncReport]:
    extractor_by_id = {
        job.extractor.extractor_id: job.extractor for job in jobs
    }
    prerequisite_by_id = {
        job.extractor.extractor_id: job.context.prerequisite_digest
        for job in jobs
    }
    return reconcile_inventory(
        InventoryReport(tuple(job.item for job in jobs), ()),
        {job.record.source_id: job.record for job in jobs},
        registry=ExtractorRegistry(
            schema_version=1,
            config_sha256="f" * 64,
            extractors=tuple(extractor_by_id.values()),
        ),
        processor=ImmediateProcessor(),
        paths=paths,
        prerequisite_digests=prerequisite_by_id,
        max_workers=max_workers,
        checkpoint=checkpoint,
        now=FIXED_NOW,
    )
```

`stage_test_input_from_verified_bytes()` creates a mode-`0700` private staging directory, writes and fsyncs a content-addressed file from the fixture bytes, opens it with no-follow semantics, verifies its SHA-256 and identity, and registers descriptor/directory cleanup with the test fixture finalizer. Production `stage_pinned_input()` performs the same copy only from `context.input_descriptor` while Plan 2 still owns and will revalidate that descriptor; it never opens the logical raw path. A batch `Job` owns this immutable staged descriptor until its worker result has been consumed and the stage identity has been revalidated, then closes and removes it in `finally`. The tracking assertion fails if all jobs are pre-submitted, while the fixtures remain runnable without Task 2 converters or any undeclared `Job.path`/`Job.paths` fields. A `Job` is never constructed with a mutable `paths.raw` pathname.

- [ ] **Step 3: Run the new tests and observe the intended failures**

Run: `python3 -m pytest tests/unit/test_capabilities.py tests/unit/test_deterministic_processor.py -v`

Expected: FAIL because the capability module, processor factory, bounded iterator, and `max_workers` reconciliation extension do not exist.

- [ ] **Step 4: Implement capability-only dependency handling and bounded scheduling**

Add:

```toml
[project.optional-dependencies]
extract-pdf = ["PyMuPDF>=1.24,<2"]
extract-office = ["python-docx>=1.1,<2", "python-pptx>=1.0,<2", "openpyxl>=3.1,<4"]
```

`probe_capabilities()` checks command executables with `shutil.which`, Python distributions with `importlib.util.find_spec`, and versions through Plan 2's bounded `RunVersionProbe`. It copies install recipes from each `ConverterSpec`. `./brain --json doctor` serializes capabilities below `data.capabilities` and never invokes installers or extraction commands.

`DeterministicSourceProcessor.process()` calls its injected resolver once. The production resolver delegates to `resolve_converter()`; tests inject a stable `ResolvedConverter`. It rejects a prerequisite/effective-version mismatch before passing `resolved.converter` to Task 2. If resolution returns `None`, it returns `NEEDS_AGENT` only when `extractor.agent_fallback` is true; otherwise it emits the canonical unavailable coverage gap.

`iter_batch()` validates the worker count before consuming `jobs`. Its default is `min(4, os.cpu_count() or 1)`. Implement a refill loop with `concurrent.futures.wait(..., return_when=FIRST_COMPLETED)`: submit only until `len(futures) == effective_max_workers`, yield completed results, then submit replacements. Do not call `executor.map()` and do not pre-submit the entire sequence. Cancel futures that have not started when the consumer closes or raises. Each queued job owns one `StagedInput` copied from Plan 2's live pinned descriptor, not from a reopened raw path. The private stage is atomically published and fsynced, independently hashed, kept open, and identity-revalidated after the worker returns; only then may the coordinator validate the canonical output and activate it. At most the worker bound of staged descriptors exists. Each `process()` call creates its own `.brain-tmp-<source-id-prefix>-*` output directory below the repository and removes it in `finally`.

Add `--max-workers N` to both `./brain init` and `./brain sync`. The handlers validate `1..16` and call:

```python
records, report = reconcile_inventory(
    inventory,
    store.load_all(),
    registry=registry,
    processor=services.processor_factory(),
    paths=paths,
    prerequisite_digests=digests,
    max_workers=args.max_workers,
    checkpoint=store.save,
    now=now,
)
```

Inside `reconcile_inventory()`, Plan 2 remains the sole coordinator. For each eligible decision it first uses the existing `use_stable_file(..., RAW_USER, ..., expected_fingerprint=...)` authority and verifies the active checksum, then stages from that descriptor. It forms `Job` values only from those pinned stages, uses `iter_batch()` only when the processor satisfies `BatchSourceProcessor`, and revalidates the staged identity after each yielded result. It then opens Plan 2's canonical `EXTRACTED` stable authority, validates the exact artifact, prepares the result-manifest intent, activates through `activate_derivation()`, and durably prepares Plan 2's per-source `ActivationGuard` before invoking the existing `checkpoint(record)` while that authority remains live. Only after the canonical output and input/stage authorities pass their post-checkpoint revalidation may it commit the event or accept the next yielded result. A post-checkpoint authority change compensates to the exact `extracting` record with `active_derivation_id=None`. The guard remains beside the canonical shard across repeated compensation failures, and ledger readers fail closed until the locked `sync`/`init` recovery durably restores that inactive record. After successful activation, clear the unchanged guard only after both authorities pass; after compensation, clear it only after the safe checkpoint succeeds. A result, cancellation, source/stage replacement, or output mismatch closes the stage without exposing an unverified active representation. For non-batch processors, preserve Plan 2's single-source callback. Reconciliation never imports or instantiates `LedgerStore`, and the command has no second record-save loop.

- [ ] **Step 5: Verify Task 1**

Run:

```bash
python3 -m pytest tests/unit/test_capabilities.py tests/unit/test_deterministic_processor.py tests/unit/test_cli.py tests/unit/test_sync.py -v
git diff --check -- pyproject.toml brainlib/capabilities.py brainlib/extractors/processor.py brainlib/commands.py brainlib/cli.py brainlib/sync.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_capabilities.py tests/unit/test_deterministic_processor.py
```

Expected: PASS; at most the requested number of jobs is in flight, completed records are saved only through the passed checkpoint, and no test references a field absent from `Job`.

- [ ] **Step 6: Review without committing**

Run: `git diff -- pyproject.toml brainlib/capabilities.py brainlib/extractors/processor.py brainlib/commands.py brainlib/cli.py brainlib/sync.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_capabilities.py tests/unit/test_deterministic_processor.py`

Expected: only the Plan 2 processor seam and service/CLI wiring are extended; registry, ledger, inventory, and lock ownership remain unchanged.

### Task 2: Produce canonical Markdown for native and allowlisted formats

**Files:**
- Create: `brainlib/extractors/native.py`
- Create: `brainlib/extractors/adapters.py`
- Modify: `brainlib/extractors/processor.py`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_external_adapters.py`
- Test: `tests/integration/test_core_extractors.py`

**Interfaces:**
- Consumes: Task 1 `CommandExecution`, `RunCommand`, `Job`, and `DeterministicSourceProcessor`; Plan 2 `ResolvedConverter`; canonical Plan 1/2 hashing, derivation, path, registry, and activation contracts.
- Produces:

```python
ExtractionResult = ProcessResult

@dataclass(frozen=True)
class ExtractedPayload:
    markdown: str
    anchors: tuple[Anchor, ...]
    warning: Diagnostic | None

class ExtractionQualityError(ValueError): ...

def anchor_html_id(anchor: Anchor) -> str: ...
def extract_payload(
    job: Job, resolved: ResolvedConverter, *,
    paths: RepoPaths, run: RunCommand,
) -> ExtractedPayload: ...
def publish_payload(
    job: Job, payload: ExtractedPayload, *, paths: RepoPaths
) -> ProcessResult: ...
def run_job(
    job: Job, *, paths: RepoPaths, run: RunCommand,
    resolved: ResolvedConverter,
) -> ProcessResult: ...
```

- [ ] **Step 1: Add exact task-local fixtures and failing tests**

Add to `tests/conftest.py`:

```python
@pytest.fixture
def pdf_job(repo_paths: RepoPaths) -> Job:
    return make_job(
        repo_paths, "papers/sample.pdf", b"%PDF-1.4\nfixture\n%%EOF\n",
        "application/pdf",
    )

@pytest.fixture
def pdf_resolved(pdf_job: Job) -> ResolvedConverter:
    assert pdf_job.extractor.preferred is not None
    return ResolvedConverter(
        converter=pdf_job.extractor.preferred,
        detected_version="fixture-1",
        prerequisite_digest=pdf_job.context.prerequisite_digest,
    )

@pytest.fixture
def recording_run() -> RecordingRun:
    return RecordingRun(markdown="<a id=\"page:1\"></a>\nPage one\n")
```

Append these exact fakes to `tests/helpers_extractors.py`; the PDF registry's output placeholder is the last argv element:

```python
class FailIfCalled:
    def __call__(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: int,
        max_output_bytes: int, pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution:
        del pass_fds
        raise AssertionError(f"unexpected command: {argv!r}")

class RecordingRun:
    def __init__(self, *, markdown: str, returncode: int = 0) -> None:
        self.markdown = markdown
        self.returncode = returncode
        self.argv: tuple[str, ...] | None = None
        self.cwd: Path | None = None
        self.output_path: Path | None = None
        self.max_output_bytes: int | None = None

    def __call__(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: int,
        max_output_bytes: int, pass_fds: tuple[int, ...] = (),
    ) -> CommandExecution:
        assert pass_fds
        self.argv = argv
        self.cwd = cwd
        self.max_output_bytes = max_output_bytes
        self.output_path = Path(argv[-1])
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if self.returncode == 0:
            self.output_path.write_text(self.markdown, encoding="utf-8")
        return CommandExecution(self.returncode, b"", b"")
```

`RecordingRun(markdown="", returncode=0)` supplies the empty-output case.

```python
def test_native_markdown_is_published_below_extracted(
    repo_paths: RepoPaths,
) -> None:
    job = make_job(
        repo_paths, "notes/readme.md", b"# Heading\nFact\n", "text/markdown",
    )
    assert job.extractor.preferred is not None
    resolved = ResolvedConverter(
        converter=job.extractor.preferred,
        detected_version="fixture-1",
        prerequisite_digest=job.context.prerequisite_digest,
    )
    result = run_job(
        job, paths=repo_paths, run=FailIfCalled(), resolved=resolved,
    )
    assert result.derivation is not None
    assert result.derivation.output_path.as_posix().startswith(
        "sources/extracted/notes/readme.md/"
    )
    assert result.derivation.output_path.suffix == ".md"

def test_pdf_uses_resolved_converter_and_page_anchors(
    pdf_job: Job, pdf_resolved: ResolvedConverter,
    recording_run: RecordingRun, repo_paths: RepoPaths,
) -> None:
    result = run_job(
        pdf_job, paths=repo_paths, run=recording_run, resolved=pdf_resolved,
    )
    assert recording_run.argv == (
        "pdftotext", "-layout", "-enc", "UTF-8",
        str(pdf_job.staged_input.descriptor_path), str(recording_run.output_path),
    )
    assert result.state is SourceState.OK
    assert result.derivation is not None
    assert result.derivation.method == "deterministic"
    assert result.derivation.method_metadata == {
        "converter_id": pdf_resolved.converter.converter_id,
        "converter_version": pdf_resolved.detected_version,
    }

def test_empty_converter_output_is_failed(
    pdf_job: Job, pdf_resolved: ResolvedConverter,
    repo_paths: RepoPaths,
) -> None:
    result = run_job(
        pdf_job, paths=repo_paths, run=RecordingRun(markdown=""),
        resolved=pdf_resolved,
    )
    assert result.state is SourceState.FAILED
    assert result.diagnostics[0].code == "empty_extraction"

def test_processor_rejects_resolver_drift_before_execution(
    pdf_job: Job, recording_run: RecordingRun, repo_paths: RepoPaths,
    pdf_resolved: ResolvedConverter,
) -> None:
    drifted = replace(pdf_resolved, prerequisite_digest="b" * 64)
    processor = DeterministicSourceProcessor(
        run=recording_run, resolve=lambda _: drifted,
    )
    result = processor.process(
        pdf_job.record, pdf_job.item, pdf_job.extractor,
        paths=repo_paths, context=pdf_job.context,
    )
    assert result.diagnostics[0].code == "prerequisite_changed"
    assert recording_run.argv is None

def test_every_ledger_anchor_has_one_navigable_html_id(
    pdf_job: Job, pdf_resolved: ResolvedConverter,
    recording_run: RecordingRun, repo_paths: RepoPaths,
) -> None:
    result = run_job(
        pdf_job, paths=repo_paths, run=recording_run, resolved=pdf_resolved,
    )
    assert result.derivation is not None
    markdown = (
        repo_paths.root / result.derivation.output_path
    ).read_text(encoding="utf-8")
    for anchor in result.derivation.anchors:
        assert markdown.count(anchor_html_id(anchor)) == 1

def test_anchor_value_cannot_inject_html() -> None:
    with pytest.raises(ExtractionQualityError, match="invalid anchor value"):
        anchor_html_id(Anchor("page", '2" onclick="alert(1)'))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_external_adapters.py tests/integration/test_core_extractors.py -v`

Expected: FAIL because payload normalization, safe adapter execution, and publication do not exist.

- [ ] **Step 3: Implement normalized extraction and collision-safe publication**

Normalize every successful result to UTF-8 Markdown:

- Markdown/text: preserve text and add stable line anchors.
- CSV/TSV: emit deterministic Markdown tables with row anchors.
- JSON: parse and emit a sorted, two-space-indented fenced block with block anchors.
- HTML: use allowlisted Pandoc first, then the standard-library `HTMLParser` fallback; remove active scripts/styles while retaining text and headings.
- PDF: resolved Poppler `pdftotext -layout -enc UTF-8 INPUT OUTPUT`, then resolved PyMuPDF.
- DOCX: resolved Pandoc, then `python-docx`.
- PPTX: `python-pptx`, then isolated headless LibreOffice.
- XLSX: `openpyxl`, then per-sheet isolated headless LibreOffice.
- Text-focused images: resolved Tesseract. If resolution/output is unavailable or low-confidence and `agent_fallback` is true, return `NEEDS_AGENT`; complex images go directly to that visible outcome.

Use only `resolved.converter`; do not re-scan preferred/fallback lists inside adapters. Build argv with `build_converter_argv(resolved.converter, input_path=job.staged_input.descriptor_path, ...)`, never with `paths.raw / item.fingerprint.path`, then invoke the injected `RunCommand(..., max_output_bytes=extractor.max_output_bytes, pass_fds=(job.staged_input.descriptor,))`. The production POSIX runner uses `subprocess.Popen(list(argv), shell=False, cwd=staging_dir, stdout=stdout_file, stderr=stderr_file, start_new_session=True, pass_fds=pass_fds)` with separate temporary files. A platform without an equivalently proven inherited-handle or immutable-stage backend fails this converter closed; it never falls back to reopening the mutable source pathname. The runner waits for at most `extractor.timeout_seconds`, kills the child process group on timeout, and reads at most the configured ceiling plus one byte from stdout and 16 KiB plus one byte from stderr to set the truncation flags. It never accumulates unbounded pipe output in memory. Every converter writes a staging file except the explicitly allowlisted Tesseract recipe `tesseract INPUT stdout`: for that recipe only, treat non-truncated bounded stdout as converter output, decode UTF-8, and write it to the normal staging file before validation/publication. Never treat stdout from any other converter as evidence. Validate nonzero exit, timeout, either truncation flag, malformed/encrypted input, invalid UTF-8, empty output, and `extractor.max_output_bytes` before publication. Before returning success, fsync and atomically publish the canonical output; Plan 2 then independently stable-opens and hashes that `EXTRACTED` artifact before activation.

Derive provenance exactly:

```python
assert job.context.config_sha256 == job.extractor.config_sha256
assert resolved.prerequisite_digest == job.context.prerequisite_digest
identifier = derivation_id(
    source_sha256=job.context.input_sha256,
    extractor_id=job.extractor.extractor_id,
    extractor_version=job.context.extractor_version,
    config_sha256=job.extractor.config_sha256,
)
relative_to_extracted = derive_extraction_path(
    job.item.fingerprint.path, job.context.input_sha256, identifier,
)
destination = paths.extracted / relative_to_extracted
```

`derive_extraction_path()` already returns a `.md` path relative to `sources/extracted`; do not pass a suffix. Its directory includes the full `raw_path.name`, including extension (`notes/readme.md/<sha>/<drv>.md`), never `raw_path.stem`, so same-stem source files cannot collide. Store `paths.repo_relative(destination)` in `Derivation.output_path`. Publish with a same-directory temporary file, flush/fsync, and `os.replace()`. If `destination` exists, reuse it only when its checksum equals the staged checksum; otherwise return `output_path_collision` and never overwrite.

Construct the `Derivation` with `method="deterministic"` and `method_metadata={"converter_id": resolved.converter.converter_id, "converter_version": resolved.detected_version}`. Those are the only metadata keys; both values are nonempty strings. The same `ResolvedConverter` is therefore the sole source for command execution, prerequisite identity, and persisted method provenance.

`anchor_html_id()` accepts only canonical anchor kinds and values matching `[A-Za-z0-9._:/-]+`, returning exactly `<a id="kind:value"></a>`. Require every `expected_anchors` kind and require each recorded marker to occur exactly once in final Markdown before constructing the `Derivation`.

- [ ] **Step 4: Verify Task 2**

Run:

```bash
python3 -m pytest tests/unit/test_external_adapters.py tests/integration/test_core_extractors.py tests/unit/test_deterministic_processor.py -v
git diff --check -- brainlib/extractors/native.py brainlib/extractors/adapters.py brainlib/extractors/processor.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_external_adapters.py tests/integration/test_core_extractors.py
```

Expected: PASS with canonical repository-relative derivation paths, one resolved converter per attempt, and no references to nonexistent `Job.path` or `Job.paths`.

- [ ] **Step 5: Review without committing**

Run: `git diff -- brainlib/extractors/native.py brainlib/extractors/adapters.py brainlib/extractors/processor.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_external_adapters.py tests/integration/test_core_extractors.py`

Expected: all successful formats publish `.md`; no non-allowlisted command or raw active representation exists.

### Task 3: Add immutable agent handoffs and manifest-bound registration

**Files:**
- Create: `brainlib/extractors/handoff.py`
- Modify: `brainlib/extractors/processor.py`
- Modify: `brainlib/commands.py`
- Modify: `brainlib/cli.py`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_agent_handoff.py`

**Interfaces:**
- Consumes: Task 2 publication/anchor validation and Plan 2 `activate_derivation()`/`LedgerStore`.
- Produces:

```python
HandoffKind = Literal["extraction", "rendered_web_capture"]

@dataclass(frozen=True)
class HandoffItem:
    handoff_id: str
    kind: HandoffKind
    source_id: str
    content_sha256: str
    raw_path: PurePosixPath
    media_type: str
    reason: str
    required_anchor_kinds: tuple[str, ...]
    extractor_id: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    agent_revision: str
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class HandoffManifest:
    schema_version: int
    run_id: str
    created_at: datetime
    items: tuple[HandoffItem, ...]

@dataclass(frozen=True)
class HandoffSummary:
    handoff_id: str
    kind: HandoffKind
    source_id: str
    content_sha256: str
    reason: str
    def to_dict(self) -> dict[str, JSONValue]: ...

def handoff_id_for(item_without_id: Mapping[str, JSONValue]) -> str: ...
def handoff_to_dict(item: HandoffItem) -> dict[str, JSONValue]: ...
def write_handoff_manifest(
    paths: RepoPaths, *, run_id: str, created_at: datetime,
    items: Sequence[HandoffItem],
) -> Path: ...
def load_handoff_item(paths: RepoPaths, handoff_id: str) -> HandoffItem: ...
def register_staged_agent_extraction(
    *,
    handoff: HandoffItem,
    record: SourceRecord,
    staging_path: Path,
    anchors: tuple[Anchor, ...],
    quality_state: Literal["ok", "warning"],
    note: str,
    paths: RepoPaths,
    now: datetime,
) -> ProcessResult: ...
```

- [ ] **Step 1: Add exact handoff fixtures and failing provenance tests**

Add these fixtures to `tests/conftest.py`:

```python
@pytest.fixture
def extraction_handoff(repo_paths: RepoPaths) -> HandoffItem:
    job = make_job(repo_paths, "images/diagram.png", PNG_BYTES, "image/png")
    return handoff_for_job(
        job, kind="extraction", reason="complex_image",
        diagnostics=(Diagnostic("agent_required", "Vision judgment required"),),
    )

@pytest.fixture
def staging_markdown(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
) -> Path:
    path = (
        repo_paths.root / ".brain/agent-staging"
        / extraction_handoff.handoff_id / "result.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<a id="block:image-1"></a>\n## Image 1\nDiagram text\n',
        encoding="utf-8",
    )
    return path

@pytest.fixture
def repo_with_agent_sources(repo_root: Path) -> Path:
    target = repo_root / "sources/raw/images/diagram.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_BYTES)
    return repo_root

@pytest.fixture
def repo_with_checkpointed_needs_agent_record(repo_root: Path) -> Path:
    paths = RepoPaths.discover(repo_root)
    job = make_job(paths, "images/diagram.png", PNG_BYTES, "image/png")
    handoff = handoff_for_job(
        job, kind="extraction", reason="complex_image",
        diagnostics=(Diagnostic("agent_required", "Vision judgment required"),),
    )
    LedgerStore(paths).save(record_for_handoff(handoff, paths))
    return repo_root

def durable_source_id(repo_root: Path) -> str:
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((repo_root / "sources/ledger").glob("src_*.json"))
    ]
    assert len(documents) == 1
    return str(documents[0]["source_id"])
```

Add these exact helpers to `tests/helpers_extractors.py`:

```python
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

def handoff_for_job(
    job: Job, *, kind: HandoffKind, reason: str,
    diagnostics: tuple[Diagnostic, ...],
) -> HandoffItem:
    assert job.extractor.agent_fallback
    assert job.extractor.agent_revision is not None
    fields: dict[str, JSONValue] = {
        "kind": kind,
        "source_id": job.record.source_id,
        "content_sha256": job.context.input_sha256,
        "raw_path": job.item.fingerprint.path.as_posix(),
        "media_type": job.item.media_type,
        "reason": reason,
        "required_anchor_kinds": list(job.extractor.expected_anchors),
        "extractor_id": job.extractor.extractor_id,
        "extractor_version": job.context.extractor_version,
        "config_sha256": job.extractor.config_sha256,
        "prerequisite_digest": job.context.prerequisite_digest,
        "agent_revision": job.extractor.agent_revision,
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": diagnostic.message,
                "path": (
                    None if diagnostic.path is None
                    else diagnostic.path.as_posix()
                ),
                "details": dict(diagnostic.details),
            }
            for diagnostic in diagnostics
        ],
    }
    return HandoffItem(
        handoff_id=handoff_id_for(fields),
        kind=kind,
        source_id=job.record.source_id,
        content_sha256=job.context.input_sha256,
        raw_path=job.item.fingerprint.path,
        media_type=job.item.media_type,
        reason=reason,
        required_anchor_kinds=job.extractor.expected_anchors,
        extractor_id=job.extractor.extractor_id,
        extractor_version=job.context.extractor_version,
        config_sha256=job.extractor.config_sha256,
        prerequisite_digest=job.context.prerequisite_digest,
        agent_revision=job.extractor.agent_revision,
        diagnostics=diagnostics,
    )

def record_for_handoff(
    handoff: HandoffItem, paths: RepoPaths,
) -> SourceRecord:
    raw = paths.raw / handoff.raw_path
    stat = raw.stat()
    version = ContentVersion(
        handoff.content_sha256,
        handoff.raw_path,
        stat.st_size,
        FileFingerprint(handoff.raw_path, stat.st_size, stat.st_mtime_ns),
        FIXED_NOW,
        (),
    )
    attempt = ProcessingAttempt(
        handoff.content_sha256,
        handoff.extractor_id,
        handoff.extractor_version,
        handoff.config_sha256,
        handoff.prerequisite_digest,
        SourceState.NEEDS_AGENT,
        FIXED_NOW,
        tuple(item.code for item in handoff.diagnostics),
    )
    return replace(
        make_source_record(source_id=handoff.source_id),
        current_raw_path=handoff.raw_path,
        media_type=handoff.media_type,
        byte_size=stat.st_size,
        state=SourceState.NEEDS_AGENT,
        versions={handoff.content_sha256: version},
        active_content_sha256=handoff.content_sha256,
        derivations={},
        active_derivation_id=None,
        last_attempt=attempt,
        diagnostics=handoff.diagnostics,
    )
```

`handoff_for_job()` obtains every provenance field from `job.extractor` and `job.context`; it does not accept extractor identity arguments.

```python
def test_needs_agent_manifest_freezes_recipe_identity(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
) -> None:
    path = write_handoff_manifest(
        repo_paths, run_id="run-20260904", created_at=FIXED_NOW,
        items=(extraction_handoff,),
    )
    item = json.loads(path.read_text(encoding="utf-8"))["items"][0]
    assert item["extractor_id"] == extraction_handoff.extractor_id
    assert item["extractor_version"] == extraction_handoff.extractor_version
    assert item["config_sha256"] == extraction_handoff.config_sha256
    assert item["agent_revision"] == extraction_handoff.agent_revision

def test_registration_derives_identity_only_from_manifest(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
    staging_markdown: Path,
) -> None:
    result = register_staged_agent_extraction(
        handoff=extraction_handoff,
        record=record_for_handoff(extraction_handoff, repo_paths),
        staging_path=staging_markdown,
        anchors=(Anchor("block", "image-1"),),
        quality_state="ok",
        note="faithful diagram transcription",
        paths=repo_paths,
        now=FIXED_NOW,
    )
    assert result.derivation is not None
    assert result.derivation.extractor_id == extraction_handoff.extractor_id
    assert result.derivation.extractor_version == extraction_handoff.extractor_version
    assert result.derivation.config_sha256 == extraction_handoff.config_sha256
    assert result.derivation.method == "agent"
    assert result.derivation.method_metadata == {
        "handoff_id": extraction_handoff.handoff_id,
        "agent_revision": extraction_handoff.agent_revision,
        "note": "faithful diagram transcription",
    }

def test_same_agent_revision_replays_only_identical_output(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
    staging_markdown: Path,
) -> None:
    record = record_for_handoff(extraction_handoff, repo_paths)
    arguments = {
        "handoff": extraction_handoff,
        "staging_path": staging_markdown,
        "anchors": (Anchor("block", "image-1"),),
        "quality_state": "ok",
        "note": "faithful diagram transcription",
        "paths": repo_paths,
        "now": FIXED_NOW,
    }
    first = register_staged_agent_extraction(record=record, **arguments)
    assert first.derivation is not None
    activated = activate_derivation(record, first.derivation, now=FIXED_NOW)
    replayed = register_staged_agent_extraction(record=activated, **arguments)
    assert replayed.derivation == first.derivation
    staging_markdown.write_text(
        '<a id="block:image-1"></a>\n## Changed\nDifferent bytes\n',
        encoding="utf-8",
    )
    with pytest.raises(AgentRegistrationError, match="output_path_collision"):
        register_staged_agent_extraction(record=activated, **arguments)

def test_agent_staging_is_scoped_to_exact_handoff_directory(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
) -> None:
    sibling = (
        repo_paths.root / ".brain/agent-staging"
        / ("hnd_" + "f" * 64) / "result.md"
    )
    sibling.parent.mkdir(parents=True, exist_ok=True)
    sibling.write_text(
        '<a id="block:image-1"></a>\n## Image 1\n', encoding="utf-8",
    )
    with pytest.raises(AgentRegistrationError, match="staging scope"):
        register_staged_agent_extraction(
            handoff=extraction_handoff,
            record=record_for_handoff(extraction_handoff, repo_paths),
            staging_path=sibling,
            anchors=(Anchor("block", "image-1"),), quality_state="ok",
            note="faithful diagram transcription", paths=repo_paths,
            now=FIXED_NOW,
        )

def test_agent_revision_bump_changes_handoff_and_derivation_identity(
    extraction_handoff: HandoffItem,
) -> None:
    manifest_fields = handoff_to_dict(extraction_handoff)
    manifest_fields.pop("handoff_id")
    manifest_fields["agent_revision"] = "2"
    manifest_fields["config_sha256"] = "e" * 64
    assert handoff_id_for(manifest_fields) != extraction_handoff.handoff_id
    assert derivation_id(
        source_sha256=extraction_handoff.content_sha256,
        extractor_id=extraction_handoff.extractor_id,
        extractor_version=extraction_handoff.extractor_version,
        config_sha256=extraction_handoff.config_sha256,
    ) != derivation_id(
        source_sha256=extraction_handoff.content_sha256,
        extractor_id=extraction_handoff.extractor_id,
        extractor_version=extraction_handoff.extractor_version,
        config_sha256="e" * 64,
    )

def test_registration_rejects_web_capture_handoff(
    repo_paths: RepoPaths, extraction_handoff: HandoffItem,
    staging_markdown: Path,
) -> None:
    rendered_fields = handoff_to_dict(extraction_handoff)
    rendered_fields.pop("handoff_id")
    rendered_fields["kind"] = "rendered_web_capture"
    rendered = replace(
        extraction_handoff, kind="rendered_web_capture",
        handoff_id=handoff_id_for(rendered_fields),
    )
    with pytest.raises(AgentRegistrationError, match="not an extraction handoff"):
        register_staged_agent_extraction(
            handoff=rendered,
            record=record_for_handoff(rendered, repo_paths),
            staging_path=staging_markdown,
            anchors=(Anchor("block", "image-1"),),
            quality_state="ok",
            note="invalid route",
            paths=repo_paths,
            now=FIXED_NOW,
        )

@pytest.mark.parametrize(
    ("forged_flag", "forged_value"),
    (
        ("--extractor-id", "forged"),
        ("--extractor-version", "forged"),
        ("--config-sha256", "f" * 64),
        ("--method", "deterministic"),
        ("--agent-revision", "999"),
    ),
)
def test_cli_has_no_caller_controlled_recipe_flags(
    repo_root: Path, forged_flag: str, forged_value: str,
) -> None:
    result = run_brain(
        repo_root, "--json", "source", "register-extraction",
        "--handoff-id", "hnd_" + "b" * 64,
        "--staging-path", ".brain/agent-staging/hnd_" + "b" * 64 + "/result.md",
        "--anchors-json", '[{"kind":"block","value":"image-1"}]',
        "--quality-state", "ok", "--note", "diagram",
        forged_flag, forged_value,
    )
    assert result.returncode == 2

def test_init_returns_manifest_and_sorted_handoff_ids(
    repo_with_agent_sources: Path,
) -> None:
    payload = run_brain_json(repo_with_agent_sources, "init")
    assert payload["data"]["handoff_manifest"].startswith(
        "sources/ledger/handoffs/"
    )
    summaries = payload["data"]["handoffs"]
    assert summaries == sorted(
        summaries,
        key=lambda item: (
            item["source_id"], item["kind"], item["handoff_id"],
        ),
    )
    assert all(
        re.fullmatch(r"hnd_[0-9a-f]{64}", item["handoff_id"])
        for item in summaries
    )
    manifest = (
        repo_with_agent_sources
        / payload["data"]["handoff_manifest"]
    )
    assert manifest.is_file()

def test_sync_reconstructs_handoff_after_interrupted_manifest_write(
    repo_with_checkpointed_needs_agent_record: Path,
) -> None:
    payload = run_brain_json(
        repo_with_checkpointed_needs_agent_record, "sync",
    )
    assert payload["data"]["handoffs"][0]["source_id"] == durable_source_id(
        repo_with_checkpointed_needs_agent_record
    )

def test_full_validation_rejects_conflicting_same_handoff_id(
    repo_with_agent_sources: Path,
) -> None:
    payload = run_brain_json(repo_with_agent_sources, "init")
    original = repo_with_agent_sources / payload["data"]["handoff_manifest"]
    conflicting = original.with_name("conflicting.json")
    document = json.loads(original.read_text(encoding="utf-8"))
    document["items"][0]["reason"] = "different"
    conflicting.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    result = run_brain(
        repo_with_agent_sources, "--json", "validate", "--full",
    )
    assert result.returncode == 1
    assert "handoff_id_collision" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_agent_handoff.py -v`

Expected: FAIL because immutable handoffs and manifest-bound registration do not exist.

- [ ] **Step 3: Implement immutable manifests and exact registration CLI**

For each `NEEDS_AGENT` result, the coordinator creates a `HandoffItem`. Its `extractor_id`, `extractor_version`, `config_sha256`, `prerequisite_digest`, and non-null `agent_revision` are copied from the selected `ExtractorSpec` and `ProcessingContext` used for that attempt. `handoff_for_job()` rejects a missing or invalid agent revision. `handoff_to_dict()` is the sole canonical serializer. `handoff_id_for()` is `hnd_` plus the SHA-256 of canonical compact JSON for every other item field, including `agent_revision`. A command creates `run_id = now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12]` and writes canonical sorted bytes to `sources/ledger/handoffs/<run-id>.json`; if the path exists, accept it only when bytes match. A resumed init/sync may place the same deterministic item in multiple immutable run manifests. `load_handoff_item()` accepts one or more byte/field-identical matches and returns their canonical decoded item; it rejects zero matches as `handoff_not_found` and any same-ID payload conflict as `handoff_id_collision`. Extend full source validation to parse every manifest, recompute every handoff ID, validate field shapes and that the referenced source/content version is retained, allow identical repeats, and reject conflicting same-ID payloads. Historical recipe fields are immutable provenance and need not equal today's registry; only registration applies the current-recipe stale check.

For `./brain --json init` and `./brain --json sync`, reconciliation still checkpoints every `NEEDS_AGENT` record first. After reconciliation, the command collects both new needs-agent outcomes and any durable current `NEEDS_AGENT` records that lack a manifest in this run, reconstructing items from `active_content_sha256`, `current_raw_path`, `media_type`, `last_attempt`, and the matching current registry entry. It writes one immutable run manifest and returns:

```json
{
  "data": {
    "handoff_manifest": "sources/ledger/handoffs/<run-id>.json",
    "handoffs": [
      {
        "handoff_id": "hnd_<64hex>",
        "kind": "extraction",
        "source_id": "src_<64hex>",
        "content_sha256": "<64hex>",
        "reason": "complex_image"
      }
    ]
  }
}
```

`handoff_manifest` is a repository-relative string or `null` when `handoffs` is empty. `handoffs` is always an array sorted by `(source_id, kind, handoff_id)`. This is additive to Plan 2's existing sync data and is the sole machine-readable way an agent obtains `HANDOFF_ID`; agents never glob manifests or derive IDs. If interruption happens after a record checkpoint but before manifest publication, the exception propagates. The next init/sync reconstructs the byte-identical deterministic item from that durable record even when retry eligibility would otherwise skip conversion, writes the new run manifest, and returns its summary.

Expose exactly:

```text
./brain --json source register-extraction --handoff-id HANDOFF_ID --staging-path PATH --anchors-json JSON --quality-state ok|warning --note TEXT
```

There is deliberately no positional source ID, `--content-sha256`, `--extractor-id`, `--extractor-version`, `--config-sha256`, `--method`, or `--agent-revision`. Under `SourceWriteLock`, the handler loads the canonical identical manifest item(s) by `handoff_id`, loads the record named by that item, and requires matching `kind == "extraction"`, active content hash, raw path, media type, and current selected extractor ID/config/agent revision. Compute `selected_digest=services.prerequisite_digest(extractor)` and require it to equal `handoff.prerequisite_digest`; also require `effective_extractor_version(extractor, selected_digest) == handoff.extractor_version`. A registry, prerequisite, or agent-revision change makes an old handoff stale and returns `handoff_recipe_stale`; it never silently relabels output.

Allow `staging_path` only as a regular file whose fully resolved path remains below the exact handoff-specific directory `.brain/agent-staging/<handoff.handoff_id>/`; a sibling handoff directory and the staging root itself are invalid. Reject path escapes/symlinks out, missing/empty/non-UTF-8/oversized input, blank note, invalid/duplicate anchors, failure to include every `handoff.required_anchor_kinds` kind, missing/duplicate HTML anchor markers, a record without the matching `NEEDS_AGENT` attempt except for the exact idempotent replay described below, or destination collision. The helper derives `derivation_id`, output path, and all recipe identity only from `handoff`; it returns `ProcessResult` and never saves. It constructs `Derivation(method="agent", method_metadata={"handoff_id": handoff.handoff_id, "agent_revision": handoff.agent_revision, "note": note})`; those are the only metadata keys, and no CLI flag can select method, handoff identity, revision, extractor identity, or config identity.

The command activates the returned derivation, calls `store.save(updated_record)` exactly once, regenerates the summary, and serializes:

```json
{
  "command": "source register-extraction",
  "ok": true,
  "data": {
    "registration": {
      "source_id": "src_...",
      "content_sha256": "...",
      "derivation_id": "drv_...",
      "output_path": "sources/extracted/...",
      "active_representation": {},
      "corpus_revision": "..."
    }
  },
  "warnings": [],
  "errors": []
}
```

Delete staging only after publication and the ledger checkpoint both succeed. A consumed handoff may be replayed only as an idempotent recovery: the record must retain and activate the exact derivation ID with identical agent method metadata, anchors, and quality, and the staged and published bytes must match the retained output checksum, size, and mtime. Return that same derivation without appending or overwriting provenance. Different staged bytes under the unchanged `agent_revision` fail `output_path_collision`. An approved bumped revision changes the per-extractor config digest, handoff ID, derivation ID, and output path, so it publishes a distinct derivation and retains the older one. Manifests remain immutable audit data.

Commands that directly create a needs-agent outcome, including `source snapshot-url`, use the same manifest writer and add sibling `data.handoff_manifest`/`data.handoffs` fields beside their command-specific object. Therefore rendered-capture agents receive the exact `handoff_id` from command JSON rather than guessing it.

- [ ] **Step 4: Verify Task 3**

Run:

```bash
python3 -m pytest tests/unit/test_agent_handoff.py tests/unit/test_cli.py tests/integration/test_validate_ledger.py -v
git diff --check -- brainlib/extractors/handoff.py brainlib/extractors/processor.py brainlib/commands.py brainlib/cli.py brainlib/validation.py tests/conftest.py tests/unit/test_agent_handoff.py
```

Expected: PASS; a CLI caller cannot choose derivation identity and a rendered-web handoff cannot be registered as derived Markdown before raw capture.

- [ ] **Step 5: Review without committing**

Run: `git diff -- brainlib/extractors/handoff.py brainlib/extractors/processor.py brainlib/commands.py brainlib/cli.py brainlib/validation.py tests/conftest.py tests/unit/test_agent_handoff.py`

Expected: handoff manifests freeze the selected recipe and registration remains a staging-only, lock-owned command.

### Task 4: Add refreshable approved web snapshots and faithful rendered capture

**Files:**
- Create: `brainlib/sources/__init__.py`
- Create: `brainlib/sources/web.py`
- Modify: `brainlib/commands.py`
- Modify: `brainlib/cli.py`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_web_capture.py`
- Test: `tests/integration/test_url_snapshot_flow.py`

**Interfaces:**
- Consumes: Plan 1 content/retrieval/corpus contracts, Plan 2 descriptor/ledger/processor contracts, Task 2 extraction, and Task 3 `rendered_web_capture` handoffs.
- Produces:

```python
@dataclass(frozen=True)
class ApprovalClaim:
    event_id: str
    scope: str
    note: str

@dataclass(frozen=True)
class RenderedCapture:
    staging_path: Path
    retrieved_at: datetime
    final_url: str
    redirects: tuple[str, ...]
    detected_media_type: str
    handoff_id: str | None = None

@dataclass(frozen=True)
class SnapshotRequest:
    source_id: str
    approval: ApprovalClaim
    rendered: RenderedCapture | None = None

@dataclass(frozen=True)
class SnapshotResult:
    source_id: str
    raw_path: PurePosixPath
    content_sha256: str
    source_version: ContentVersion
    retrieval: RetrievalMetadata
    extraction_result: ProcessResult | None
    active_representation: SourceRepresentation | None
    corpus_revision: str

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address

@dataclass(frozen=True)
class VettedEndpoint:
    url: str
    scheme: Literal["http", "https"]
    hostname: str
    port: int
    host_header: str
    addresses: tuple[IPAddress, ...]

class ResolveHost(Protocol):
    def __call__(self, hostname: str, port: int) -> tuple[str, ...]: ...

class NetworkPolicy(Protocol):
    def vet_url(self, url: str) -> VettedEndpoint: ...
    def verify_connected_peer(
        self, endpoint: VettedEndpoint, peer_address: str,
    ) -> None: ...

class PublicAddressNetworkPolicy:
    def __init__(self, *, resolve: ResolveHost | None = None) -> None: ...
    def vet_url(self, url: str) -> VettedEndpoint: ...
    def verify_connected_peer(
        self, endpoint: VettedEndpoint, peer_address: str,
    ) -> None: ...

@dataclass
class HTTPHop:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO
    peer_address: str

class PinnedRequester(Protocol):
    def __call__(
        self, endpoint: VettedEndpoint, address: IPAddress, *,
        target: str, timeout_seconds: int,
    ) -> HTTPHop: ...

@dataclass(frozen=True)
class NetworkCapture:
    staging_path: Path
    retrieved_at: datetime
    final_url: str
    redirects: tuple[str, ...]
    detected_media_type: str
    safe_filename: str

class WebTransport(Protocol):
    def capture(
        self, requested_url: str, *, paths: RepoPaths,
        timeout_seconds: int, max_output_bytes: int,
        now: datetime,
    ) -> NetworkCapture: ...

class PublicHTTPTransport:
    def __init__(
        self, *, policy: NetworkPolicy | None = None,
        request_hop: PinnedRequester | None = None,
        max_redirects: int = 10,
    ) -> None: ...
    def capture(
        self, requested_url: str, *, paths: RepoPaths,
        timeout_seconds: int, max_output_bytes: int,
        now: datetime,
    ) -> NetworkCapture: ...

# Add this field to Plan 2's existing brainlib.commands.CommandServices in Task 4.
# The production default is public-only; tests inject a transport factory, never a CLI flag.
web_transport_factory: Callable[[], WebTransport] = PublicHTTPTransport

def approval_recorded_at_for(
    records: Iterable[SourceRecord], claim: ApprovalClaim, *, now: datetime,
) -> datetime: ...

def serialize_url_descriptor(
    *, canonical_url: str, description: str, added: date,
) -> bytes: ...

def publish_url_descriptor(
    paths: RepoPaths, *, canonical_url: str,
    description: str, added: date,
) -> UrlDescriptor: ...

def active_representation_for(
    record: SourceRecord,
) -> SourceRepresentation | None: ...

def snapshot_url(
    request: SnapshotRequest,
    *,
    descriptor: UrlDescriptorMetadata,
    record: SourceRecord,
    paths: RepoPaths,
    registry: ExtractorRegistry,
    processor: SourceProcessor,
    prerequisite_digest: Callable[[ExtractorSpec], str],
    records: Mapping[str, SourceRecord],
    checkpoint: Callable[[SourceRecord], None],
    transport: WebTransport,
    now: datetime,
) -> SnapshotResult: ...
```

`snapshot_url()` is command orchestration, not a generic inventory path. It may update only the supplied source record and invokes `checkpoint` after adding retrieval/version data and again after activating a non-null derivation. It does not construct `LedgerStore`. Task 4 adds the shown `web_transport_factory` field to the existing `CommandServices`; the production command calls that factory after approval validation, while tests replace it with a mock or `lambda: PublicHTTPTransport(policy=AllowLoopbackTestPolicy())`. Rendered mode never calls it.

- [ ] **Step 1: Add exact HTTP fixtures and failing approval/version tests**

Add `LocalWebServer` to `tests/helpers_extractors.py` using `ThreadingHTTPServer(("127.0.0.1", 0), Handler)`. Its fixed route table is:

```python
ROUTES = {
    "/static": (200, {"Content-Type": "text/html"}, b"<html><body><h1>Fact</h1></body></html>"),
    "/redirect": (302, {"Location": "/report.pdf"}, b""),
    "/report.pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4\nfixture\n%%EOF\n"),
    "/same": (200, {"Content-Type": "text/plain"}, b"same bytes\n"),
    "/changed-v1": (200, {"Content-Type": "text/plain"}, b"version one\n"),
    "/changed-v2": (200, {"Content-Type": "text/plain"}, b"version two\n"),
    "/render-shell": (200, {"Content-Type": "text/html"}, b"<html><div id='app'></div><script>render()</script></html>"),
}
```

The fixture starts one daemon thread, yields the `http://127.0.0.1:<port>` base URL, and always calls `shutdown()`, `server_close()`, and `thread.join()`. `capture_fixture()` and `snapshot_cli()` must inject `PublicHTTPTransport(policy=AllowLoopbackTestPolicy())`; that explicitly named test-only policy accepts only the fixture's loopback address and must never be a production default or command option. Add a `recording_transport` fixture as an injected `WebTransport` mock whose `capture` method returns a fixed `NetworkCapture`; it never performs network access.

Add these transport-only fakes to `tests/helpers_extractors.py` before the tests that consume them:

```python
class StaticHostResolver:
    def __init__(self, answers: Mapping[tuple[str, int], tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []
    def __call__(self, hostname: str, port: int) -> tuple[str, ...]:
        self.calls.append((hostname, port))
        return self.answers[(hostname, port)]

class ScriptedPinnedRequester:
    def __init__(self, hops: Mapping[str, Sequence[HTTPHop]]) -> None:
        self.hops = {url: list(values) for url, values in hops.items()}
        self.calls: list[tuple[VettedEndpoint, IPAddress, str]] = []
    def __call__(
        self, endpoint: VettedEndpoint, address: IPAddress, *,
        target: str, timeout_seconds: int,
    ) -> HTTPHop:
        self.calls.append((endpoint, address, target))
        return self.hops[endpoint.url].pop(0)

def hop(
    status: int, body: bytes = b"", *, peer: str = "93.184.216.34",
    headers: Mapping[str, str] | None = None,
) -> HTTPHop:
    return HTTPHop(status, dict(headers or {}), io.BytesIO(body), peer)

class AllowLoopbackTestPolicy:
    """Tests only; never wire this into production CommandServices."""
    def vet_url(self, url: str) -> VettedEndpoint:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise UnsafeNetworkTarget(url)
        port = parsed.port or 80
        return VettedEndpoint(
            url, "http", "127.0.0.1", port, f"127.0.0.1:{port}",
            (ipaddress.ip_address("127.0.0.1"),),
        )
    def verify_connected_peer(
        self, endpoint: VettedEndpoint, peer_address: str,
    ) -> None:
        if ipaddress.ip_address(peer_address) not in endpoint.addresses:
            raise NetworkPeerMismatch(peer_address)
```

```python
def test_missing_approval_rejects_before_network(
    descriptor_record: SourceRecord, repo_paths: RepoPaths,
) -> None:
    resolver = StaticHostResolver({})
    requester = ScriptedPinnedRequester({})
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    )
    with pytest.raises(ApprovalRequired):
        run_snapshot_fixture(
            descriptor_record,
            approval=ApprovalClaim("", "", ""),
            paths=repo_paths,
            transport=transport,
        )
    assert resolver.calls == []
    assert requester.calls == []

def test_same_bytes_reuse_version_and_append_retrieval_event(
    captured_descriptor_record: SourceRecord, local_web_server: str,
    repo_paths: RepoPaths,
) -> None:
    first = capture_fixture(
        captured_descriptor_record, f"{local_web_server}/same",
        event_id="evt_bounded_1", paths=repo_paths,
    )
    second = capture_fixture(
        first.record, f"{local_web_server}/same",
        event_id="evt_bounded_2", paths=repo_paths,
    )
    assert set(second.record.versions) == {first.result.content_sha256}
    version = second.record.versions[first.result.content_sha256]
    assert [event.approval_event_id for event in version.retrieval_events] == [
        "evt_bounded_1", "evt_bounded_2",
    ]
    assert second.result.source_version == version

def test_same_bytes_with_valid_derivation_does_not_run_processor(
    captured_ok_descriptor_record: SourceRecord, recording_transport: Mock,
    processor_spy: Mock, repo_paths: RepoPaths,
) -> None:
    refreshed = run_snapshot_fixture(
        captured_ok_descriptor_record,
        approval=ApprovalClaim("evt_reuse", "refresh one URL", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
        processor=processor_spy,
    )
    processor_spy.process.assert_not_called()
    assert refreshed.extraction_result is None
    assert refreshed.active_representation is not None
    assert (
        refreshed.active_representation.content_sha256
        == refreshed.content_sha256
    )

def test_changed_bytes_create_new_immutable_version(
    captured_descriptor_record: SourceRecord, local_web_server: str,
    repo_paths: RepoPaths,
) -> None:
    first = capture_fixture(
        captured_descriptor_record, f"{local_web_server}/changed-v1",
        event_id="evt_change_1", paths=repo_paths,
    )
    second = capture_fixture(
        first.record, f"{local_web_server}/changed-v2",
        event_id="evt_change_2", paths=repo_paths,
    )
    assert len(second.record.versions) == 2
    assert first.result.content_sha256 != second.result.content_sha256
    assert all(
        (repo_paths.raw / version.raw_path).exists()
        for version in second.record.versions.values()
    )

def test_ok_descriptor_can_be_refreshed(
    captured_ok_descriptor_record: SourceRecord, recording_transport: Mock,
    repo_paths: RepoPaths,
) -> None:
    result = run_snapshot_fixture(
        captured_ok_descriptor_record,
        approval=ApprovalClaim("evt_refresh", "refresh one URL", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
    )
    assert result.source_id == captured_ok_descriptor_record.source_id

def test_one_bounded_event_id_can_cover_multiple_urls(
    repo_root: Path, local_web_server: str,
) -> None:
    first = snapshot_cli(
        repo_root, f"{local_web_server}/static", event_id="evt_question_7",
    )
    second = snapshot_cli(
        repo_root, f"{local_web_server}/report.pdf", event_id="evt_question_7",
    )
    assert first["retrieval"]["approval_event_id"] == "evt_question_7"
    assert second["retrieval"]["approval_event_id"] == "evt_question_7"
    assert (
        first["retrieval"]["approval_recorded_at"]
        == second["retrieval"]["approval_recorded_at"]
    )

def test_reused_event_id_rejects_changed_scope_before_network(
    records_with_approval_event: Mapping[str, SourceRecord],
    recording_transport: Mock,
) -> None:
    with pytest.raises(ApprovalEventConflict):
        approval_recorded_at_for(
            records_with_approval_event.values(),
            ApprovalClaim("evt_existing", "expanded scope", "user approved"),
            now=FIXED_NOW,
        )
    recording_transport.capture.assert_not_called()

def test_public_transport_captures_direct_url_with_original_authority(
    repo_paths: RepoPaths,
) -> None:
    url = "https://public.test/fact?q=1"
    resolver = StaticHostResolver({("public.test", 443): ("8.8.8.8",)})
    requester = ScriptedPinnedRequester({
        url: (hop(200, b"fact\n", peer="8.8.8.8", headers={"Content-Type": "text/plain"}),),
    })
    capture = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    ).capture(url, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert capture.staging_path.read_bytes() == b"fact\n"
    endpoint, address, target = requester.calls[0]
    assert (endpoint.hostname, endpoint.host_header, str(address), target) == (
        "public.test", "public.test", "8.8.8.8", "/fact?q=1",
    )

@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.1.1/",
        "http://224.0.0.1/", "http://192.0.2.1/", "http://0.0.0.0/",
        "http://[::1]/",
    ),
)
def test_non_public_literal_is_rejected_without_request(
    url: str, repo_paths: RepoPaths,
) -> None:
    requester = ScriptedPinnedRequester({})
    with pytest.raises(UnsafeNetworkTarget):
        PublicHTTPTransport(request_hop=requester).capture(
            url, paths=repo_paths, timeout_seconds=10,
            max_output_bytes=1024, now=FIXED_NOW,
        )
    assert requester.calls == []

def test_public_transport_vets_every_redirect(
    repo_paths: RepoPaths,
) -> None:
    start, final = "https://public.test/start", "https://next.test/final"
    resolver = StaticHostResolver({
        ("public.test", 443): ("8.8.8.8",),
        ("next.test", 443): ("1.1.1.1",),
    })
    requester = ScriptedPinnedRequester({
        start: (hop(302, peer="8.8.8.8", headers={"Location": final}),),
        final: (hop(200, b"done", peer="1.1.1.1", headers={"Content-Type": "text/plain"}),),
    })
    capture = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=resolver),
        request_hop=requester,
    ).capture(start, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert capture.final_url == final
    assert capture.redirects == (final,)
    assert [call[0].hostname for call in requester.calls] == ["public.test", "next.test"]

def test_redirect_to_private_literal_is_rejected_before_second_request(
    repo_paths: RepoPaths,
) -> None:
    start = "https://public.test/start"
    requester = ScriptedPinnedRequester({
        start: (hop(302, peer="8.8.8.8", headers={"Location": "http://127.0.0.1/private"}),),
    })
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(
            resolve=StaticHostResolver({("public.test", 443): ("8.8.8.8",)}),
        ),
        request_hop=requester,
    )
    with pytest.raises(UnsafeNetworkTarget):
        transport.capture(start, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert len(requester.calls) == 1

def test_mixed_public_private_dns_answer_is_rejected_without_request(
    repo_paths: RepoPaths,
) -> None:
    requester = ScriptedPinnedRequester({})
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=StaticHostResolver({
            ("mixed.test", 443): ("8.8.8.8", "10.0.0.8"),
        })),
        request_hop=requester,
    )
    with pytest.raises(UnsafeNetworkTarget, match="non-public DNS answer"):
        transport.capture("https://mixed.test/", paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert requester.calls == []

def test_connected_peer_must_match_vetted_address_set(
    repo_paths: RepoPaths,
) -> None:
    url = "https://rebind.test/"
    requester = ScriptedPinnedRequester({
        url: (hop(200, b"secret", peer="10.0.0.9", headers={"Content-Type": "text/plain"}),),
    })
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=StaticHostResolver({
            ("rebind.test", 443): ("8.8.8.8",),
        })),
        request_hop=requester,
    )
    with pytest.raises(NetworkPeerMismatch):
        transport.capture(url, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert not list((repo_paths.root / ".brain/web-staging").glob("*"))

def test_redirect_limit_is_bounded(repo_paths: RepoPaths) -> None:
    first = "https://public.test/0"
    requester = ScriptedPinnedRequester({
        f"https://public.test/{index}": (
            hop(302, peer="8.8.8.8", headers={"Location": f"/{index + 1}"}),
        )
        for index in range(3)
    })
    transport = PublicHTTPTransport(
        policy=PublicAddressNetworkPolicy(resolve=StaticHostResolver({
            ("public.test", 443): ("8.8.8.8",),
        })),
        request_hop=requester,
        max_redirects=2,
    )
    with pytest.raises(TooManyRedirects):
        transport.capture(first, paths=repo_paths, timeout_seconds=10, max_output_bytes=1024, now=FIXED_NOW)
    assert len(requester.calls) == 3

@pytest.mark.parametrize(
    "description",
    (
        "colon: value", "hash # value", 'quote " value',
        "brackets [value]", "Unicode 雪 café",
    ),
)
def test_ad_hoc_descriptor_scalars_round_trip_before_publish(
    description: str, repo_paths: RepoPaths,
) -> None:
    canonical_url = "https://example.test/a:b?q=%5Bvalue%5D%23quoted"
    descriptor = publish_url_descriptor(
        repo_paths, canonical_url=canonical_url,
        description=description, added=FIXED_NOW.date(),
    )
    absolute = repo_paths.raw / descriptor.path
    assert parse_url_descriptor(absolute, descriptor.path) == descriptor
    assert json.dumps(description, ensure_ascii=False) in absolute.read_text(
        encoding="utf-8",
    )

def test_invalid_descriptor_is_never_atomically_published(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "brainlib.sources.web.parse_url_descriptor",
        lambda *_: (_ for _ in ()).throw(ValueError("invalid fixture")),
    )
    with pytest.raises(ValueError, match="invalid fixture"):
        publish_url_descriptor(
            repo_paths, canonical_url="https://example.test/new",
            description="valid", added=FIXED_NOW.date(),
        )
    assert not list((repo_paths.raw / "urls").glob("*.url.md"))
    assert not list((repo_paths.raw / "urls").glob(".brain-tmp-*"))
```

- [ ] **Step 2: Add failing rendered-raw and result-envelope tests**

```python
def test_rendered_staging_is_saved_as_raw_before_extraction(
    repo_root: Path, rendered_staging_file: Path,
    descriptor_with_render_handoff: SourceRecord,
) -> None:
    before = rendered_staging_file.read_bytes()
    payload = run_brain_json(
        repo_root, "source", "snapshot-url",
        "--source-id", descriptor_with_render_handoff.source_id,
        "--rendered-staging-path", str(rendered_staging_file),
        "--handoff-id", render_handoff_id(descriptor_with_render_handoff),
        "--retrieved-at", "2026-09-04T12:01:00Z",
        "--final-url", "https://example.test/report",
        "--detected-media-type", "text/html",
        "--approval-event-id", "evt_render_1",
        "--approval-scope", "capture rendered report",
        "--approval-note", "user approved this bounded event",
    )
    snapshot = payload["data"]["snapshot"]
    raw = repo_root / "sources/raw" / snapshot["raw_path"]
    assert raw.read_bytes() == before
    assert snapshot["source_version"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert snapshot["active_representation"] is not None
    assert snapshot["corpus_revision"]

def test_render_handoff_cannot_publish_markdown_without_raw_snapshot(
    repo_paths: RepoPaths, render_handoff: HandoffItem,
    staging_markdown: Path,
) -> None:
    with pytest.raises(AgentRegistrationError, match="not an extraction handoff"):
        register_staged_agent_extraction(
            handoff=render_handoff,
            record=record_for_handoff(render_handoff, repo_paths),
            staging_path=staging_markdown,
            anchors=(Anchor("section", "one"),),
            quality_state="ok",
            note="must use snapshot-url",
            paths=repo_paths,
            now=FIXED_NOW,
        )

def test_snapshot_json_uses_canonical_envelope(
    repo_root: Path, local_web_server: str,
) -> None:
    payload = run_brain_json(
        repo_root, "source", "snapshot-url",
        "--url", f"{local_web_server}/static",
        "--description", "fixture",
        "--approval-event-id", "evt_envelope",
        "--approval-scope", "one fixture URL",
        "--approval-note", "user approved",
    )
    assert set(payload) == {"command", "ok", "data", "warnings", "errors"}
    assert payload["command"] == "source snapshot-url"
    assert set(payload["data"]["snapshot"]) >= {
        "source_id", "raw_path", "content_sha256", "source_version",
        "retrieval", "extraction_result", "active_representation",
        "corpus_revision",
    }

def test_snapshot_result_field_contract_is_exact_for_consumers() -> None:
    assert tuple(field.name for field in dataclasses.fields(SnapshotResult)) == (
        "source_id", "raw_path", "content_sha256", "source_version",
        "retrieval", "extraction_result", "active_representation",
        "corpus_revision",
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_web_capture.py tests/integration/test_url_snapshot_flow.py -v`

Expected: FAIL because refresh, content-version reuse, rendered raw capture, and the complete snapshot envelope do not exist.

- [ ] **Step 4: Implement the exact root-first CLI contract**

Expose these three forms:

```text
./brain --json source snapshot-url --source-id SOURCE_ID --approval-event-id EVENT_ID --approval-scope TEXT --approval-note TEXT
./brain --json source snapshot-url --url HTTP_OR_HTTPS_URL --description TEXT --approval-event-id EVENT_ID --approval-scope TEXT --approval-note TEXT
./brain --json source snapshot-url (--source-id SOURCE_ID | --url HTTP_OR_HTTPS_URL --description TEXT) --rendered-staging-path PATH [--handoff-id HANDOFF_ID] --retrieved-at RFC3339_UTC --final-url HTTP_OR_HTTPS_URL [--redirect-url HTTP_OR_HTTPS_URL ...] --detected-media-type MEDIA_TYPE --approval-event-id EVENT_ID --approval-scope TEXT --approval-note TEXT
```

The first form refreshes any descriptor-backed record, including `OK`, `WARNING`, `FAILED`, or `NEEDS_AGENT`; it is not limited to `AWAITING_APPROVAL`. It fetches the current `record.url_descriptor.url`, so a descriptor URL edit creates retrieval facts for the new requested URL while preserving all prior versions/events. Reject records without `url_descriptor`.

The second form runs entirely under the same outer `SourceWriteLock` as capture. Canonicalize with `urllib.parse.urlsplit()`: lowercase HTTP(S) scheme and IDNA hostname, reject credentials/control characters/invalid ports, remove the scheme's default port, use `/` for an empty path, preserve path/query bytes, and discard the fragment; rebuild with `urlunsplit()`. Require a nonempty single-line description with no control characters. Choose the final relative path first as `urls/<safe-slug>-<sha256(canonical-url)[:12]>.url.md`. `serialize_url_descriptor()` renders exactly, using JSON string encoding as the canonical safe scalar form accepted by Plan 2's parser:

```python
descriptor_bytes = (
    "---\n"
    "kind: url\n"
    f"url: {json.dumps(canonical_url, ensure_ascii=False)}\n"
    f"description: {json.dumps(description.strip(), ensure_ascii=False)}\n"
    f"added: {added.isoformat()}\n"
    "---\n"
).encode("utf-8")
```

If the path contains identical bytes, parse it again and reuse it; if it contains different bytes, fail with `descriptor_path_collision`. For a missing target, `publish_url_descriptor()` writes and fsyncs a unique same-directory temporary file, calls `parse_url_descriptor(temporary, final_relative_path)`, verifies the parsed URL/description/date equal the inputs, and only then calls `os.replace(temporary, final_path)` plus a directory fsync. On parse/validation failure it removes the temporary and leaves no final descriptor. Inventory that final path, then compute `source_id_for_url_descriptor()` from the final-path descriptor and checkpoint its `AWAITING_APPROVAL` record before any DNS resolution, socket connection, network transport call, or rendered-staging read. Never compute an ID from a provisional path and then rename it. The `.url.md` control record remains a normal Git-tracked file below `sources/raw` even if retrieval later fails, satisfying the durable used-URL record without an agent-side write or automatic commit.

The third form performs no network access. It imports a faithful browser-exported serialized DOM, print-to-PDF, or downloaded claim-bearing document from a regular file below `.brain/web-staging/`. It requires nonempty bounded bytes and an allowlisted detected media type. For `text/html`, require UTF-8 serialized HTML containing an HTML element; for PDF/Office/image types, validate the corresponding magic/container signature without decoding as text. Reject Markdown summaries and already-extracted output in every case. `retrieved_at`, final URL, and ordered repeated `--redirect-url` values are browser-observed facts recorded in `RetrievalMetadata`. When `--handoff-id` is supplied, require a matching `rendered_web_capture` item; when omitted, this is a direct user-approved browser research capture. In both cases, bytes must be persisted below `sources/raw/_web` before any extraction is invoked.

Every form validates event ID, bounded scope, and note before reading staging or invoking `web_transport_factory`; therefore invalid/conflicting approval causes no DNS lookup, connection, redirect, or response read. Validate HTTP(S) requested/final/redirect URLs. `approval_recorded_at_for()` scans every retained retrieval event under the source lock. On zero matches it returns `now`; on one or more matches it requires one identical prior scope, note, and recorded-at value and returns that original timestamp. Reusing an event ID with changed scope/note or inconsistent prior timestamps fails with `approval_event_conflict` before network/staging access. Thus every retrieval has its own `retrieved_at`, while all captures in one bounded approval event share the original `approval_recorded_at`. Never infer approval from descriptor metadata.

- [ ] **Step 5: Implement immutable byte staging, version reuse, processing, and return facts**

Network mode calls `transport.capture()` only after the approval claim and any reused event metadata have passed validation. The production `PublicHTTPTransport` uses `PublicAddressNetworkPolicy` for the requested URL and again for every redirect target; it does not use an automatically redirecting opener. Canonicalize and accept only HTTP(S). For an IP literal, parse it directly. For a hostname, resolve A/AAAA answers once for that hop, canonicalize/deduplicate/sort them, require at least one answer, and reject the entire hop if **any** answer is not globally routable. Specifically reject private, loopback, link-local, multicast, reserved, unspecified, and every other address for which `ipaddress.ip_address(value).is_global` is false. Thus a mixed public/private DNS response is rejected rather than selecting its public member.

For each hop, select only an address from `VettedEndpoint.addresses` and make a numeric-address connection through the pinned requester; never hand the hostname back to a connector that can resolve it again. Immediately after TCP connect and before any TLS handshake, compare the normalized `socket.getpeername()[0]` with that exact vetted set through `verify_connected_peer()`; check it again after TLS wrapping and abort before sending a request or reading response bytes if either differs. The standard-library requester sends the original URL authority as the HTTP `Host` header. For HTTPS it wraps the pinned socket with the default verifying SSL context and `server_hostname=endpoint.hostname`, preserving certificate hostname validation and SNI even though the TCP destination is numeric. These invariants are inside the production requester and cannot be disabled by CLI flags.

Handle only 301, 302, 303, 307, and 308 manually, resolve `Location` with `urllib.parse.urljoin()`, and permit at most `max_redirects` (production default 10); a missing location, redirect cycle, or the next redirect beyond the cap fails. Close every response on every path. Stream the final 2xx body to a unique file below `.brain/web-staging/` with the configured ceiling, removing the partial file on policy, peer, HTTP, timeout, truncation, or I/O failure. Record requested URL, ordered redirect targets, final URL, UTC retrieval time, detected media type, byte size, and SHA-256. Choose a safe filename from `Content-Disposition`, then the final URL basename, otherwise `snapshot`; remove separators/control characters and add a MIME suffix when absent. `AllowLoopbackTestPolicy` exists only in test helpers so the local server can exercise real HTTP plumbing; the default constructor and production `CommandServices` always use `PublicAddressNetworkPolicy`.

For both network and rendered modes, target:

```python
relative_raw = PurePosixPath("_web", record.source_id, sha256, safe_filename)
destination = paths.raw / relative_raw
```

If the hash is new, atomically publish bytes and add `ContentVersion(sha256, relative_raw, byte_size, FileFingerprint(relative_raw, byte_size, destination.stat().st_mtime_ns), now, (retrieval,))`. If the hash already exists, observe it with `stable_file_snapshot(paths, SnapshotNamespace.RAW_WEB, existing.raw_path, include_sha256=True, expected_fingerprint=existing.fingerprint)`; require the returned relative path, byte size, mtime, and SHA-256 to match the retained version and require `existing.byte_size == retrieval.byte_size`. Never stat, resolve, or hash `paths.raw / existing.raw_path` directly. Any mismatch is `web_snapshot_integrity_error` and blocks reuse. Otherwise leave its path/fingerprint/first-seen data unchanged and replace only that mapping entry with `retrieval_events=existing.retrieval_events + (retrieval,)`. This is content-version reuse, not retrieval-event overwrite. A destination that exists with different bytes is `web_snapshot_collision`.

In the same immutable record replacement, set `active_content_sha256=sha256`, `media_type=retrieval.detected_media_type`, `byte_size=retrieval.byte_size`, and the inspection/update timestamps; keep `current_raw_path` pointing to the `.url.md` descriptor because that is the logical source's control path. When there is no reusable derivation for the captured hash, retain historical derivations in the mapping but set `active_derivation_id=None` and transition to `PENDING` before the first checkpoint. For identical bytes with a reusable derivation, preserve/reactivate that derivation's `OK`/`WARNING` quality state.

Before processing, search the record's retained derivations for the captured content hash. A reusable derivation must reference that hash and must pass a namespace-aware `EXTRACTED` stable snapshot whose SHA-256, byte size, mtime, canonical path, and stable identity match its `Derivation` metadata. Prefer the current active derivation when valid; otherwise choose the valid candidate with greatest `(created_at, derivation_id)` and reactivate it in the same record update. For identical bytes with a reusable derivation, checkpoint the retrieval-event update (and any reactivation) exactly once, do not call the processor, return `extraction_result=None`, and return that exact `active_representation`.

When the hash has no reusable valid derivation, checkpoint the record containing the new retrieval/version before processing. Build an `InventoryItem` for the exact `_web` raw bytes and select with `registry.select(detected_media_type, relative_web_path)`, preserving the full logical path for compound suffixes. Obtain `selected_digest=services.prerequisite_digest(extractor)`. Then use `use_stable_file(paths, SnapshotNamespace.RAW_WEB, relative_web_path, ..., include_sha256=True, expected_fingerprint=version.fingerprint)` and require the pinned hash to equal the retained content checksum. Build the complete `ProcessingContext`, including the pinned `input_path` and `input_descriptor`, only inside that callback; batch work first copies from that authority into the owned `StagedInput`. The deterministic processor resolves exactly once with `resolve_converter()` and rejects any resolved digest/version drift before execution. Plan 2 independently opens and hashes the canonical `EXTRACTED` artifact, keeps that authority live through activation and the final checkpoint, and revalidates both output and input/stage afterward. Before the active checkpoint it writes Plan 2's durable activation guard containing the exact inactive `extracting` post-image and candidate identity. If either post-checkpoint proof fails, it attempts that compensation with `active_derivation_id=None`; a failed write leaves the guard blocking ledger readers until locked `sync`/`init` recovery restores the safe record before normal reconciliation. If no deterministic converter resolves and `agent_fallback` is true, processing returns `NEEDS_AGENT` with a manifest; it never silently changes converter identity or reopens the `_web` pathname.

After any attempted processing, apply `ProcessResult.attempt`, state, and diagnostics to the record and checkpoint that outcome. If it returns a derivation, prepare its event, activate it, and checkpoint it only inside the live canonical output/input authority lifecycle above; commit the event and set `active_representation` only after both post-images are proven. On proof failure, compensate to the guarded inactive `extracting` post-image; if compensation fails, preserve the guard and return the error with ledger readers still blocked. If there is no extractor, checkpoint an `UNSUPPORTED` diagnostic and return `extraction_result=None`; if processing returns a gap, return that non-null result and no active representation unless a valid retained derivation belongs to the same active content hash. No process outcome remains only in command output.

`active_representation_for()` returns `None` unless the active content/version/derivation links are internally valid; otherwise it constructs the canonical Plan 2 `SourceRepresentation` with the content version's raw-relative `raw_path` and the derivation's repository-relative `output_path` as `extracted_path`. Replace the source entry in a local copy of `records`, compute `corpus_revision=compute_corpus_revision(updated_records.values())` after the last checkpoint, and return the final persisted `ContentVersion`, retrieval event, processing result, active representation, and revision in `SnapshotResult`. The command serializer uses the canonical Plan 1 `CommandResult` envelope shown in the test; it does not add alternate top-level fields.

A static HTML shell judged rendering-required is still saved as its own faithful raw version, returns `NEEDS_AGENT`, and emits `HandoffItem(kind="rendered_web_capture", ...)`. Task 3's registration command rejects that handoff. The browser agent must call the rendered form of `./brain --json source snapshot-url`, producing a new/reused faithful rendered raw version before derived Markdown can become active.

- [ ] **Step 6: Verify Task 4**

Run:

```bash
python3 -m pytest tests/unit/test_web_capture.py tests/integration/test_url_snapshot_flow.py tests/unit/test_agent_handoff.py -v
git diff --check -- brainlib/sources/web.py brainlib/commands.py brainlib/cli.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_web_capture.py tests/integration/test_url_snapshot_flow.py
```

Expected: PASS; no network opens before claim validation, unchanged bytes reuse a version with another retrieval event, changed bytes retain both versions, descriptor-backed records can refresh from any processing state, and rendered evidence is raw-ledgered before extraction.

- [ ] **Step 7: Review without committing**

Run: `git diff -- brainlib/sources/web.py brainlib/commands.py brainlib/cli.py tests/helpers_extractors.py tests/conftest.py tests/unit/test_web_capture.py tests/integration/test_url_snapshot_flow.py`

Expected: no duplicate descriptor parser/ledger model and no assertion that CLI flags independently prove approval.

### Task 5: Generate deterministic binary fixtures and run the milestone gate

**Files:**
- Create: `tests/fixtures/generate_extractors.py`
- Create: `tests/fixtures/extractors/manifest.json`
- Create: `tests/fixtures/extractors/core/sample.md`
- Create: `tests/fixtures/extractors/core/sample.txt`
- Create: `tests/fixtures/extractors/core/sample.csv`
- Create: `tests/fixtures/extractors/core/sample.tsv`
- Create: `tests/fixtures/extractors/core/sample.json`
- Create: `tests/fixtures/extractors/core/sample.html`
- Create: `tests/fixtures/extractors/core/sample.pdf`
- Create: `tests/fixtures/extractors/core/sample.docx`
- Create: `tests/fixtures/extractors/core/sample.pptx`
- Create: `tests/fixtures/extractors/core/sample.xlsx`
- Create: `tests/fixtures/extractors/core/sample.png`
- Create: `tests/fixtures/extractors/failures/empty.txt`
- Create: `tests/fixtures/extractors/failures/malformed.docx`
- Create: `tests/fixtures/extractors/failures/encrypted.pdf`
- Create: `tests/fixtures/extractors/failures/unsupported.bin`
- Create: `tests/fixtures/extractors/expected/README.md`
- Create: `tests/fixtures/web/static-page.html`
- Create: `tests/fixtures/web/rendered-page-marker.html`
- Modify: `tests/integration/test_core_extractors.py`

**Interfaces:**
- Consumes: Tasks 1-4 and Plan 2 init/sync/full-validation commands.
- Produces: reproducible checked-in fixture bytes, a checksum manifest, complete format/gap coverage, and the milestone gate.

- [ ] **Step 1: Implement the deterministic generator before adding binary files**

`tests/fixtures/generate_extractors.py` uses only the standard library and exposes:

```python
FIXED_ZIP_TIME = (2000, 1, 1, 0, 0, 0)

def zip_document(entries: Mapping[str, bytes]) -> bytes: ...
def minimal_pdf(text: str) -> bytes: ...
def zlib_stored(payload: bytes) -> bytes: ...
def minimal_png() -> bytes: ...
def build_fixtures() -> Mapping[PurePosixPath, bytes]: ...
def build_manifest(fixtures: Mapping[PurePosixPath, bytes]) -> bytes: ...
def write_fixtures(root: Path) -> None: ...
def check_fixtures(root: Path) -> tuple[str, ...]: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

Implement `zip_document()` with `zipfile.ZipFile(..., mode="w", compression=ZIP_STORED)`, sorted entry names, `ZipInfo.date_time=FIXED_ZIP_TIME`, `create_system=3`, `external_attr=0o100644 << 16`, and empty extra/comment. Using stored entries avoids zlib-version-dependent OOXML bytes. Hand-author the minimal OOXML parts for DOCX (one paragraph), PPTX (one slide), and XLSX (one sheet/shared string). `minimal_pdf()` constructs numbered objects, calculates byte offsets, and emits a valid xref/trailer. `zlib_stored()` emits the RFC 1950 header, one or more uncompressed DEFLATE stored blocks with explicit little-endian LEN/NLEN pairs, and `zlib.adler32(payload)`; `minimal_png()` uses it with `struct.pack` and `zlib.crc32` to avoid compressor-version drift. Use fixed UTF-8 bytes for text/CSV/TSV/JSON/HTML and both web fixtures, `b""` for empty, a truncated ZIP header for malformed DOCX, a syntactically PDF-marked `/Encrypt` fixture for encrypted PDF, and fixed NUL-containing bytes for unsupported.

`build_manifest()` emits compact sorted JSON with schema version and, for every generated extractor/web fixture other than the manifest itself, byte size and SHA-256. `--write` atomically writes every file plus `manifest.json`. `--check` rebuilds everything in memory, reports missing/extra/nonmatching files in sorted order, and exits 1 on any difference. The modes are mutually exclusive and default to `--check`.

- [ ] **Step 2: Generate and immediately prove fixture reproducibility**

Run:

```bash
python3 tests/fixtures/generate_extractors.py --write
python3 tests/fixtures/generate_extractors.py --check
git diff --check -- tests/fixtures
```

Expected: both generator commands exit 0. Record `sha256sum` values in generated `manifest.json`; a second `--write` followed by `--check` remains clean.

- [ ] **Step 3: Write the complete fixture matrix**

```python
@pytest.mark.parametrize(
    ("relative", "state", "anchor_kind"),
    [
        ("core/sample.md", "ok", "line"),
        ("core/sample.txt", "ok", "line"),
        ("core/sample.csv", "ok", "row"),
        ("core/sample.tsv", "ok", "row"),
        ("core/sample.json", "ok", "block"),
        ("core/sample.html", "ok", "section"),
        ("core/sample.pdf", "ok", "page"),
        ("core/sample.docx", "ok", "section"),
        ("core/sample.pptx", "ok", "slide"),
        ("core/sample.xlsx", "ok", "sheet"),
        ("core/sample.png", "needs_agent", None),
        ("failures/empty.txt", "failed", None),
        ("failures/malformed.docx", "failed", None),
        ("failures/encrypted.pdf", "failed", None),
        ("failures/unsupported.bin", "unsupported", None),
    ],
)
def test_fixture_has_explicit_outcome(
    relative: str, state: str, anchor_kind: str | None,
    initialized_repo: Path,
) -> None:
    install_fixture(initialized_repo, relative)
    payload = run_brain_json(initialized_repo, "sync")
    assert payload["command"] == "sync"
    record = record_for_fixture(initialized_repo, Path(relative).name)
    assert record["state"] == state
    if anchor_kind is not None:
        representation = active_representation(record)
        assert any(
            anchor["kind"] == anchor_kind
            for anchor in representation["anchors"]
        )
        assert representation["extracted_path"].endswith(".md")

def test_fixture_generator_is_clean(repo_root: Path) -> None:
    result = subprocess.run(
        (sys.executable, "tests/fixtures/generate_extractors.py", "--check"),
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Define `initialized_repo`, `install_fixture`, `record_for_fixture`, and `active_representation` in `tests/conftest.py` before this test is added. `initialized_repo` copies the template to `tmp_path` with `symlinks=True`, removes any user fixture content, and invokes `run_brain_json(copy, "init")`. `install_fixture` copies exactly one named file from the checked-in fixture root into `sources/raw/fixtures/<name>`. `record_for_fixture(repo_root, name)` loads every `sources/ledger/src_*.json`, selects the unique document whose `current_raw_path == f"fixtures/{name}"`, and fails on zero/multiple matches; it never expects `SyncReport` JSON to embed complete records. Core CI injects fake allowlisted resolution/commands through `CommandServices`; separately marked capability tests may use installed tools and must skip with the missing capability ID.

- [ ] **Step 4: Run Task 5 tests**

Run:

```bash
python3 tests/fixtures/generate_extractors.py --check
python3 -m pytest tests/integration/test_core_extractors.py tests/integration/test_url_snapshot_flow.py -v
```

Expected: PASS for all fifteen matrix entries; image fallback is `needs_agent` because its registry entry has `agent_fallback=true`; malformed/encrypted/empty inputs remain explicit gaps.

- [ ] **Step 5: Run the complete extractor/web milestone gate**

Run:

```bash
python3 tests/fixtures/generate_extractors.py --check
python3 -m pytest tests/unit/test_capabilities.py tests/unit/test_deterministic_processor.py tests/unit/test_external_adapters.py tests/unit/test_agent_handoff.py tests/unit/test_web_capture.py tests/integration/test_core_extractors.py tests/integration/test_url_snapshot_flow.py -v
./brain validate --full
./brain --json validate --full
```

Expected: all tests PASS; unavailable optional-tool tests skip with the exact capability name; full validation rehashes every retained raw version and derived output and reports remaining source gaps as non-success.

- [ ] **Step 6: Review the milestone without committing**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: only intended implementation, test, and generated fixture files are changed. Do not commit; the final plan in the combined conversation owns any explicitly requested session/PR commit.

## Plan self-review

**Spec coverage:** Tasks 1-2 implement allowlisted deterministic extraction, native normalization, optional capability reporting, 1..16 bounded in-flight work, resolved converter/version consistency, and immediate checkpoint ownership through Plan 2 reconciliation. Task 3 freezes agent recipe identity/configuration in durable handoffs and prevents CLI provenance forgery. Task 4 supports approved refresh of every descriptor-backed source, event-scoped approval claims, identical-byte content-version reuse with new retrieval facts, changed-byte versioning, faithful browser-rendered raw staging, canonical active-representation lookup, and corpus revision. Task 5 generates every binary fixture deterministically and validates the complete matrix.

**Path and type consistency:** `Job` contains only `record`, `item`, `extractor`, and `context`; paths always arrive through `RepoPaths`. `ContentVersion.raw_path` is relative to `sources/raw`. `derive_extraction_path()` is relative to `sources/extracted` and preserves the full raw basename, while stored derivation/representation paths are repository-relative. Every deterministic and registered derivation uses `ProcessingContext.extractor_version` and `ExtractorSpec.config_sha256`.

**CLI and JSON consistency:** Every executable example is root-first `./brain`. Source commands return the Plan 1 envelope `{command, ok, data, warnings, errors}`. `register-extraction` accepts a handoff ID but no caller-selected extractor fields. `snapshot-url` always returns retrieval, exact source version, processing outcome, current active representation, and corpus revision.

**Execution-order check:** Task 1 batching tests use an in-test processor and Plan 2 constructors; Task 2 defines adapter fixtures before using them; Task 3 uses only Task 2 publication; Task 4 defines its server, network-policy, pinned-requester, and transport fixtures before web tests; Task 5 alone consumes generated binaries. No task relies on a later fixture or symbol.

## Execution handoff

Execute this plan after the Core Foundation and Source Ledger plans expose their listed contracts. Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` and stop at each non-committing review checkpoint. The last plan in the combined conversation owns any optional, explicitly requested single session/PR commit after all completed plans pass their final validation gates.
