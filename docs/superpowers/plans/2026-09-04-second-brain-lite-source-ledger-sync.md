# Second Brain Lite Source Ledger and Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build deterministic, locked, resumable source inventory and ledger synchronization that records every coverage gap without silently adopting changed originals, accessing the network, or claiming unfinished extraction is complete.

**Architecture:** Inventory detects raw inputs safely and reconciles them with one canonical JSON record per logical source. A registry selects approved extraction strategies, while a pluggable processor boundary lets the same init/sync flow invoke deterministic processors when installed and hand vision-dependent work to an agent. Atomic writes and a repository lock protect both the sharded ledger and generated summary.

**Tech Stack:** Python 3.11+, standard library (`dataclasses`, `hashlib`, `json`, `os`, `pathlib`, `subprocess`, `tomllib`, `zipfile`), pytest, Git only for explicit historical-byte recovery through argument-array subprocess calls.

**Spec:** `docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`

## Global Constraints

- This standalone plan depends on `docs/superpowers/plans/2026-09-04-second-brain-lite-core-foundation.md` Tasks 1–4 and uses its public types and exit-code protocol unchanged.
- `sources/raw/**` holds user originals; generic discovery excludes `.gitkeep`, `.keep`, `.DS_Store`, `_versions/**`, `_web/**`, `.brain-tmp-*`, documented lock files, and symlinks resolving outside `sources/raw/`.
- `_versions/**` holds only exact historical bytes at `sources/raw/_versions/<source-id>/<content-sha256>/<original-name>`; `_web/**` is a reserved, non-generic-discovery store for future immutable web snapshots.
- A raw-byte change at an already ledgered path becomes `integrity_error`. It is not adopted until an agent has obtained explicit user approval and invokes `./brain source adopt-version` with a nonempty approval note and exact candidate checksum.
- Adoption must verify and materialize the old bytes before activating the replacement. If exact old bytes cannot be recovered from the existing `_versions` path or Git history, adoption remains blocked and the active version remains unchanged.
- Source IDs are deterministic: `src_` plus the SHA-256 of `source-v1\0<normalized-first-path>\0<first-content-sha256>`. IDs survive unambiguous renames because reconciliation preserves the existing record; fixture IDs are therefore reproducible.
- `SourceRecord.current_raw_path` and every `ContentVersion.raw_path` are relative to `sources/raw`; `derive_extraction_path()` returns a path relative to `sources/extracted`; `Derivation.output_path` and `SourceRepresentation.extracted_path` are repository-relative paths beginning `sources/extracted/`.
- Never interpolate a source path into a shell command. Converter and Git invocations use `subprocess.run(argv, shell=False, check=False)` where `argv` is an explicit tuple of arguments.
- Sync performs no network access. URL descriptors become `awaiting_approval`; `snapshot-url` remains unavailable in this plan.
- The processor boundary is called for every eligible deterministic source. Its default implementation records `extractor_processor_unavailable` and leaves work pending; after the Plan 3 deterministic processor is registered, init/sync calls the same boundary to perform allowed conversion. A processor may return `needs_agent` only for a registry entry with `agent_fallback=true`, and that outcome appears in the handoff manifest without invoking an agent.
- Never report `complete` or exit `0` when pending, failed, unsupported, integrity-error, awaiting-approval, or needs-agent work leaves a coverage gap. Use code `1` and `complete_with_gaps`.
- Normal validation uses inventory metadata and recorded fingerprints. `./brain validate --full` rehashes every retained `ContentVersion.raw_path` and every retained derivation output before completed handoff or PR readiness.
- The repository never auto-commits and does not infer a session boundary. Make at most one logical commit for a completed conversation or PR, and only when the user or host workflow explicitly requests it; every task ends with a non-committing scoped review checkpoint.
- When several plans run in one conversation, only the final plan executed may present an optional session-commit block; every earlier plan leaves its verified worktree uncommitted.

---

## File Structure

```text
config/extractors.toml                       # committed approved strategy allowlist
brainlib/registry.py                         # parse, validate, select safe registry entry
brainlib/inventory.py                        # raw-tree scan, MIME detection, URL descriptors
brainlib/ledger.py                           # source-record persistence, versions, summary
brainlib/locking.py                          # repository-local source writer lock
brainlib/sync.py                             # state reconciliation and processor boundary
brainlib/validation.py                       # ledger and full-checksum validators
brainlib/commands.py                         # doctor/init/sync/status/adopt-version handlers
docs/brain/policies/source-handling.md       # exact reserved-tree and adoption policy
docs/brain/policies/approvals.md             # adoption approval requirements
docs/brain/workflows/initialize.md           # direct CLI and agent-assisted outcomes
docs/brain/workflows/synchronize.md          # fast path and coverage-gap behavior
tests/fixtures/inventory/                    # binary-signature and descriptor fixtures
tests/conftest.py                            # adds immutable fixture/repository helpers
tests/unit/test_registry.py
tests/unit/test_inventory.py
tests/unit/test_ledger.py
tests/unit/test_locking.py
tests/unit/test_sync.py
tests/integration/test_init_and_sync.py
tests/integration/test_validate_ledger.py
```

## Interfaces consumed from Plan 1

```python
from brainlib.contracts import (
    Anchor, ContentVersion, Derivation, FileFingerprint, ProcessingAttempt,
    RetrievalMetadata, SourceRecord, SourceState, UrlDescriptorMetadata,
    VersionAdoptionEvent,
    compute_corpus_revision, compute_sha256, source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic, JSONValue, ValidationIssue, ValidationReport
from brainlib.layout import RepoPaths
```

## Interfaces produced by this plan

```python
# brainlib/registry.py
class ExecutionMode(StrEnum):
    PYTHON = "python"
    COMMAND = "command"
    WEB_CAPTURE = "web_capture"

class RunVersionProbe(Protocol):
    def __call__(
        self, argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int,
    ) -> str: ...

@dataclass(frozen=True)
class ConverterSpec:
    converter_id: str
    executable: str | None
    argv_template: tuple[str, ...]
    version_args: tuple[str, ...]
    python_distribution: str | None
    install_recipes: Mapping[str, tuple[str, ...]]

@dataclass(frozen=True)
class ResolvedConverter:
    converter: ConverterSpec
    detected_version: str
    prerequisite_digest: str

@dataclass(frozen=True)
class ExtractorSpec:
    extractor_id: str
    extractor_version: str
    config_sha256: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    mode: ExecutionMode
    preferred: ConverterSpec
    fallbacks: tuple[ConverterSpec, ...]
    output_suffix: Literal[".md"]
    timeout_seconds: int
    max_output_bytes: int
    expected_anchors: tuple[Literal["line", "page", "slide", "sheet", "section", "row", "block"], ...]
    agent_fallback: bool = False
    agent_revision: str | None = None

@dataclass(frozen=True)
class ExtractorRegistry:
    schema_version: int
    extractors: tuple[ExtractorSpec, ...]
    config_sha256: str
    @classmethod
    def load(cls, path: Path) -> Self: ...
    def select(self, detected_media_type: str | None, path: str | Path) -> ExtractorSpec | None: ...

def build_converter_argv(converter: ConverterSpec, *, input_path: Path, output_path: Path) -> tuple[str, ...]: ...
def detect_converter_version(converter: ConverterSpec, *, extractor_version: str, run: RunVersionProbe | None = None) -> str: ...
def resolve_converter(extractor: ExtractorSpec, *, run: RunVersionProbe | None = None) -> ResolvedConverter | None: ...
def prerequisite_digest(extractor: ExtractorSpec, *, run: RunVersionProbe | None = None) -> str: ...
def effective_extractor_version(extractor: ExtractorSpec, prerequisite_digest: str) -> str: ...
```

```python
# brainlib/inventory.py
@dataclass(frozen=True)
class UrlDescriptor:
    path: PurePosixPath
    url: str
    description: str
    added: date

@dataclass(frozen=True)
class InventoryItem:
    fingerprint: FileFingerprint
    media_type: str
    extension: str
    sha256: str | None
    url_descriptor: UrlDescriptor | None = None

@dataclass(frozen=True)
class InventoryReport:
    items: tuple[InventoryItem, ...]
    skipped: tuple[Diagnostic, ...]

class MediaDetector:
    def detect(self, path: Path) -> str: ...

class SnapshotNamespace(StrEnum):
    RAW_USER = "raw_user"
    RAW_VERSION = "raw_version"
    RAW_WEB = "raw_web"
    EXTRACTED = "extracted"

@dataclass(frozen=True)
class StableFileSnapshot:
    namespace: SnapshotNamespace
    logical_path: PurePosixPath
    identity: object
    byte_size: int
    mtime_ns: int
    sha256: str | None

@dataclass(frozen=True)
class PinnedFile:
    snapshot: StableFileSnapshot
    descriptor: int
    descriptor_path: Path

def inventory_raw_sources(paths: RepoPaths, detector: MediaDetector) -> InventoryReport: ...
def parse_url_descriptor(path: Path, relative_path: PurePosixPath) -> UrlDescriptor: ...
def source_id_for_url_descriptor(descriptor: UrlDescriptor) -> str: ...
def stable_file_snapshot(paths: RepoPaths, namespace: SnapshotNamespace, logical_path: PurePosixPath, *, include_sha256: bool = False, hash_file: Callable[[Path], str] = compute_sha256, expected_fingerprint: FileFingerprint | None = None) -> StableFileSnapshot: ...
def use_stable_file(paths: RepoPaths, namespace: SnapshotNamespace, logical_path: PurePosixPath, callback: Callable[[PinnedFile], T], *, include_sha256: bool = False, hash_file: Callable[[Path], str] = compute_sha256, expected_fingerprint: FileFingerprint | None = None) -> T: ...
```

```python
# brainlib/ledger.py
class LedgerStore:
    def __init__(self, paths: RepoPaths) -> None: ...
    def load_all(self) -> dict[str, SourceRecord]: ...
    def load(self, source_id: str) -> SourceRecord: ...  # raises FileNotFoundError
    def save(self, record: SourceRecord) -> Path: ...
    def recover_activation_guards(self) -> None: ...  # caller owns SourceWriteLock
    def write_summary(self, records: Iterable[SourceRecord], *, generated_at: datetime) -> str: ...
    def active_representations(self) -> tuple["SourceRepresentation", ...]: ...
    def find_representation(self, source_id: str, content_sha256: str, derivation_id: str) -> "SourceRepresentation | None": ...

class ActivationGuard:
    @classmethod
    def prepare(cls, paths: RepoPaths, extracting: SourceRecord, candidate: SourceRecord) -> "ActivationGuard": ...
    def clear(self) -> None: ...  # only after proven activation or durable compensation

def transition(record: SourceRecord, next_state: SourceState, *, now: datetime, diagnostics: Iterable[Diagnostic] = ()) -> SourceRecord: ...
def retry_is_eligible(record: SourceRecord, *, input_sha256: str, extractor: ExtractorSpec, prerequisite_digest: str, explicit_retry: bool = False) -> bool: ...
# Returns a path relative to sources/extracted, not a repository-relative path.
def derive_extraction_path(raw_path: PurePosixPath, content_sha256: str, derivation_id: str) -> PurePosixPath: ...
def activate_derivation(record: SourceRecord, derivation: Derivation, *, now: datetime) -> SourceRecord: ...

@dataclass(frozen=True)
class CitationRewrite:
    source_id: str
    content_sha256: str
    raw_path: PurePosixPath
    def to_dict(self) -> dict[str, JSONValue]: ...

@dataclass(frozen=True)
class SourceRepresentation:
    source_id: str
    content_sha256: str
    derivation_id: str
    raw_path: PurePosixPath
    extracted_path: PurePosixPath
    output_sha256: str
    quality_state: Literal["ok", "warning"]
    anchors: tuple[Anchor, ...]
```

```python
# brainlib/locking.py
class LockHeldError(RuntimeError): ...
class LockCleanupError(RuntimeError): ...

@dataclass(frozen=True)
class LockMetadata:
    pid: int
    hostname: str
    started_at: datetime

class SourceWriteLock(AbstractContextManager["SourceWriteLock"]):
    @classmethod
    def acquire(cls, path: Path, *, stale_after: timedelta = timedelta(hours=1), now: Callable[[], datetime] = utc_now) -> Self: ...
    def release(self) -> bool: ...

def safe_lock_backend_available() -> bool: ...
```

```python
# brainlib/validation.py
class ChecksumCache:
    def __init__(self, *, hash_file: Callable[[Path], str] = compute_sha256) -> None: ...
    def begin_transaction(self) -> None: ...
    def observe(self, paths: RepoPaths, namespace: SnapshotNamespace, logical_path: PurePosixPath, *, full: bool, expected_fingerprint: FileFingerprint | None = None) -> StableFileSnapshot: ...
    def sha256(self, paths: RepoPaths, namespace: SnapshotNamespace, logical_path: PurePosixPath) -> str: ...

def validate_source_ledger(
    paths: RepoPaths, records: Mapping[str, SourceRecord], *, full: bool,
    checksum_cache: ChecksumCache | None = None,
) -> ValidationReport: ...
```

```python
# brainlib/sync.py
@dataclass(frozen=True)
class ProcessResult:
    state: SourceState
    derivation: Derivation | None
    attempt: ProcessingAttempt
    diagnostics: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class ProcessingContext:
    input_sha256: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    attempted_at: datetime
    input_path: Path       # descriptor-backed authority, valid only in process()
    input_descriptor: int # same pinned authority for pass_fds/native adapters

class SourceProcessor(Protocol):
    def process(self, record: SourceRecord, item: InventoryItem, extractor: ExtractorSpec, *, paths: RepoPaths, context: ProcessingContext) -> ProcessResult: ...

class UnavailableProcessor:
    def process(self, record: SourceRecord, item: InventoryItem, extractor: ExtractorSpec, *, paths: RepoPaths, context: ProcessingContext) -> ProcessResult: ...

class SyncAction(StrEnum):
    CREATE = "create"
    RENAME = "rename"
    UPDATE_DESCRIPTOR = "update_descriptor"
    MARK_INTEGRITY_ERROR = "mark_integrity_error"
    MARK_MISSING = "mark_missing"
    PROCESS = "process"
    QUEUE_NEEDS_AGENT = "queue_needs_agent"
    AWAIT_WEB_APPROVAL = "await_web_approval"
    RETAIN = "retain"

@dataclass(frozen=True)
class SyncDecision:
    source_id: str | None
    action: SyncAction
    reason: str
    item: InventoryItem | None

@dataclass(frozen=True)
class SyncReport:
    corpus_revision: str
    decision_counts: Mapping[SyncAction, int]
    sampled_decisions: tuple[SyncDecision, ...]
    hashed_paths: tuple[PurePosixPath, ...]
    new_active_representations: tuple[SourceRepresentation, ...]
    citation_rewrites: tuple[CitationRewrite, ...]
    handoff_source_ids: tuple[str, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    hashed_path_count: int
    new_active_representation_count: int
    citation_rewrite_count: int
    handoff_source_id_count: int
    coverage_gap_count: int
    result_manifest: SyncResultReference | None = None

def reconcile_inventory(inventory: InventoryReport, records: Mapping[str, SourceRecord], *, registry: ExtractorRegistry, processor: SourceProcessor, paths: RepoPaths, prerequisite_digests: Mapping[str, str], hash_file: Callable[[Path], str] = compute_sha256, checkpoint: Callable[[SourceRecord], object] | None = None, now: datetime, explicit_retry: bool = False, explicit_retry_source_ids: Collection[str] = (), event_sink: Callable[[str, Mapping[str, JSONValue]], None] | None = None, event_commit: Callable[[str, Mapping[str, JSONValue]], None] | None = None) -> tuple[dict[str, SourceRecord], SyncReport]: ...
```

```python
# brainlib/sync_results.py
@dataclass(frozen=True)
class SyncEvent:
    kind: Literal["hashed_path", "new_active_representation", "citation_rewrite", "handoff_source_id", "coverage_gap"]
    data: Mapping[str, JSONValue]

@dataclass(frozen=True)
class SyncResultReference:
    result_id: str
    path: PurePosixPath
    sha256: str
    corpus_revision: str
    event_counts: Mapping[str, int]

@dataclass(frozen=True)
class PendingSyncResult:
    command: Literal["init", "sync"]
    generated_at: datetime
    reference: SyncResultReference
    result_data: Mapping[str, JSONValue]

@dataclass(frozen=True)
class StagedSyncResult:
    command: Literal["init", "sync"]
    generated_at: datetime
    corpus_revision: str
    checkpoint_sha256: str
    journal_sha256: str
    result_data: Mapping[str, JSONValue]

class SyncResultWriter(AbstractContextManager["SyncResultWriter"]):
    def emit(self, kind: str, data: Mapping[str, JSONValue]) -> None: ...
    def commit(self, kind: str, data: Mapping[str, JSONValue]) -> None: ...
    def journal_sha256(self) -> str: ...
    def finalize(self, corpus_revision: str, *, event_filter: Callable[[SyncEvent], bool] | None = None) -> SyncResultReference: ...

class SyncResultStore:
    def writer(self, command: str, generated_at: datetime, *, recoverable: bool = False) -> SyncResultWriter: ...
    def verify(self, reference: SyncResultReference) -> None: ...
    def iter_events(self, reference: SyncResultReference) -> Iterator[SyncEvent]: ...
    def save_staged(self, staged: StagedSyncResult) -> None: ...
    def load_staged(self) -> StagedSyncResult | None: ...
    def save_pending(self, pending: PendingSyncResult) -> None: ...
    def load_pending(self) -> PendingSyncResult | None: ...
    def complete_inflight(self, reference: SyncResultReference, *, checkpoint_sha256: str) -> None: ...
    def clear_pending(self, reference: SyncResultReference) -> None: ...

# Consumer acknowledgement contract:
def acknowledge_sync_result(cwd: Path, result: CommandResult) -> None: ...
def acknowledge_sync_result_id(cwd: Path, result_id: str) -> CommandResult: ...
# ./brain source acknowledge-sync-result --result-id sync_<sha256>
```

```python
# brainlib/ledger.py and brainlib/commands.py
class HistoricalBytesResolver(Protocol):
    def read_exact(self, source_id: str, raw_path: PurePosixPath, sha256: str) -> bytes | None: ...

@dataclass(frozen=True)
class VersionAdoption:
    record: SourceRecord
    citation_rewrites: tuple[CitationRewrite, ...]

def adopt_version(
    record: SourceRecord, candidate: InventoryItem, *, paths: RepoPaths,
    resolver: HistoricalBytesResolver, approval_note: str, now: datetime,
) -> VersionAdoption: ...

# CLI contract:
# ./brain source adopt-version SOURCE_ID --candidate-sha256 SHA256 --approval-note TEXT
```

```python
# brainlib/commands.py and brainlib/cli.py
@dataclass(frozen=True)
class CommandServices:
    processor_factory: Callable[[], SourceProcessor]
    prerequisite_digest: Callable[[ExtractorSpec], str]

def main(
    argv: Sequence[str] | None = None, *, cwd: Path | None = None,
    stdout: TextIO | None = None, stderr: TextIO | None = None,
    services: CommandServices | None = None,
) -> int: ...
```

### Task 1: Create the approved extractor registry core

**Files:**
- Create: `config/extractors.toml`
- Create: `brainlib/registry.py`
- Modify: `brainlib/commands.py`
- Modify: `docs/brain/policies/source-handling.md`
- Modify: `tests/conftest.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: Plan 1 `RepoPaths`, `Diagnostic`, and CLI response protocol.
- Produces: `ExecutionMode`, `RunVersionProbe`, `ConverterSpec`, `ResolvedConverter`, `ExtractorSpec`, `ExtractorRegistry`, `build_converter_argv()`, `detect_converter_version()`, `resolve_converter()`, `prerequisite_digest()`, and `effective_extractor_version()`.

- [ ] **Step 1: Write failing registry tests**

```python
def test_registry_covers_all_core_families(repo_root: Path) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")
    covered = {media_type for item in registry.extractors for media_type in item.media_types}
    assert {"text/plain", "text/markdown", "text/html", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "image/png"} <= covered
    assert [item.extractor_id for item in registry.extractors] == ["text", "tabular", "json", "html", "pdf", "docx", "pptx", "xlsx", "image", "webpage"]
    assert all(item.output_suffix == ".md" for item in registry.extractors)
    agent_extractors = [item for item in registry.extractors if item.agent_fallback]
    assert {item.extractor_id for item in agent_extractors} == {"pdf", "pptx", "image", "webpage"}
    assert all(item.agent_revision == "1" for item in agent_extractors)
    assert all(item.agent_revision is None for item in registry.extractors if not item.agent_fallback)

def test_converter_argv_preserves_metacharacters_as_arguments(tmp_path: Path) -> None:
    converter = ConverterSpec(
        converter_id="poppler", executable="pdftotext",
        argv_template=("-layout", "{input}", "{output}"),
        version_args=("-v",), python_distribution=None, install_recipes={},
    )
    argv = build_converter_argv(converter, input_path=tmp_path / "a;$(bad).pdf", output_path=tmp_path / "out.md")
    assert argv[-2:] == (str(tmp_path / "a;$(bad).pdf"), str(tmp_path / "out.md"))

def test_extractor_config_digest_is_scoped_to_one_entry(repo_root: Path, tmp_path: Path) -> None:
    original_path = repo_root / "config/extractors.toml"
    original = ExtractorRegistry.load(original_path)
    changed_path = tmp_path / "extractors.toml"
    changed_path.write_text(original_path.read_text().replace('id = "pdf"\nversion = "1"\ntimeout_seconds = 120', 'id = "pdf"\nversion = "1"\ntimeout_seconds = 121', 1))
    changed = ExtractorRegistry.load(changed_path)
    before = {item.extractor_id: item.config_sha256 for item in original.extractors}
    after = {item.extractor_id: item.config_sha256 for item in changed.extractors}
    assert before["pdf"] != after["pdf"]
    assert before["text"] == after["text"]

def test_agent_revision_is_approval_gated_and_scoped_into_config_digest(repo_root: Path, tmp_path: Path) -> None:
    original_path = repo_root / "config/extractors.toml"
    original = ExtractorRegistry.load(original_path)
    changed_path = tmp_path / "extractors.toml"
    source = original_path.read_text()
    image_start = source.index('[[extractors]]\nid = "image"')
    webpage_start = source.index('[[extractors]]\nid = "webpage"')
    image_entry = source[image_start:webpage_start].replace('agent_revision = "1"', 'agent_revision = "2"', 1)
    changed_path.write_text(source[:image_start] + image_entry + source[webpage_start:])
    changed = ExtractorRegistry.load(changed_path)
    before = {item.extractor_id: item.config_sha256 for item in original.extractors}
    after = {item.extractor_id: item.config_sha256 for item in changed.extractors}
    assert next(item for item in changed.extractors if item.extractor_id == "image").agent_revision == "2"
    assert before["image"] != after["image"]
    assert before["pdf"] == after["pdf"]

@pytest.mark.parametrize(
    ("agent_fallback", "agent_revision"),
    ((True, None), (False, "1"), (True, "0"), (True, "01"), (True, "recipe-v1")),
)
def test_agent_revision_contract_is_strict(
    repo_root: Path, tmp_path: Path, agent_fallback: bool, agent_revision: str | None,
) -> None:
    source = (repo_root / "config/extractors.toml").read_text()
    entry = source[source.index('[[extractors]]\nid = "text"'):source.index('[[extractors]]\nid = "tabular"')]
    entry = entry.replace("agent_fallback = false", f"agent_fallback = {str(agent_fallback).lower()}")
    if agent_revision is not None:
        entry = entry.replace("agent_fallback = " + str(agent_fallback).lower(), "agent_fallback = " + str(agent_fallback).lower() + f'\nagent_revision = "{agent_revision}"')
    path = tmp_path / "extractors.toml"
    path.write_text("schema_version = 1\n\n" + entry)
    with pytest.raises(ValueError, match="agent_revision"):
        ExtractorRegistry.load(path)

def test_detected_converter_version_changes_prerequisite_digest(repo_root: Path) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")
    pdf = next(item for item in registry.extractors if item.extractor_id == "pdf")
    pdf = replace(pdf, fallbacks=())
    versions = {"pdftotext": "pdftotext version 24.02"}
    def run(argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int) -> str:
        return versions[Path(argv[0]).name]
    before = prerequisite_digest(pdf, run=run)
    versions["pdftotext"] = "pdftotext version 24.03"
    assert prerequisite_digest(pdf, run=run) != before

def test_resolution_returns_the_converter_bound_into_the_digest(repo_root: Path) -> None:
    html = next(item for item in ExtractorRegistry.load(repo_root / "config/extractors.toml").extractors if item.extractor_id == "html")
    def missing_preferred(argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int) -> str:
        raise FileNotFoundError(argv[0])
    resolved = resolve_converter(html, run=missing_preferred)
    assert resolved is not None
    assert resolved.converter.converter_id == "builtin.html"
    assert resolved.prerequisite_digest == prerequisite_digest(html, run=missing_preferred)

def test_invalid_template_with_shell_syntax_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "extractors.toml"
    path.write_text("""[[extractors]]\nid = 'bad'\nversion = '1'\nmimes = ['application/pdf']\nextensions = ['.pdf']\nmode = 'command'\noutput_suffix = '.md'\ntimeout_seconds = 1\nmax_output_bytes = 1\nanchors = ['page']\n[extractors.preferred]\nid = 'bad'\nexecutable = 'sh'\nargv = ['sh', '-c', 'pdftotext {input} {output}']\nversion_args = ['--version']\n""")
    with pytest.raises(ValueError, match="shell"):
        ExtractorRegistry.load(path)
```

- [ ] **Step 2: Run registry tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_registry.py -v`

Expected: FAIL because the registry and parser do not exist.

- [ ] **Step 3: Implement the allowlist and safe selection**

Create `config/extractors.toml` with exactly this initial allowlist and order. The parser maps `version` to `ExtractorSpec.extractor_version`, `mimes` to `media_types`, `anchors` to `expected_anchors`, `argv` to `argv_template`, and the nested `install` table to `install_recipes`:

```toml
schema_version = 1

[[extractors]]
id = "text"
version = "1"
timeout_seconds = 30
max_output_bytes = 268435456
mimes = ["text/plain", "text/markdown"]
extensions = [".txt", ".md", ".markdown"]
mode = "python"
output_suffix = ".md"
anchors = ["line"]
agent_fallback = false
[extractors.preferred]
id = "builtin.text"
argv = ["{input}", "{output}"]

[[extractors]]
id = "tabular"
version = "1"
timeout_seconds = 60
max_output_bytes = 268435456
mimes = ["text/csv", "text/tab-separated-values"]
extensions = [".csv", ".tsv"]
mode = "python"
output_suffix = ".md"
anchors = ["row"]
agent_fallback = false
[extractors.preferred]
id = "builtin.tabular"
argv = ["{input}", "{output}"]

[[extractors]]
id = "json"
version = "1"
timeout_seconds = 60
max_output_bytes = 268435456
mimes = ["application/json", "application/ld+json", "application/x-ndjson"]
extensions = [".json", ".jsonl", ".ndjson"]
mode = "python"
output_suffix = ".md"
anchors = ["block"]
agent_fallback = false
[extractors.preferred]
id = "builtin.json"
argv = ["{input}", "{output}"]

[[extractors]]
id = "html"
version = "1"
timeout_seconds = 120
max_output_bytes = 268435456
mimes = ["text/html", "application/xhtml+xml"]
extensions = [".html", ".htm", ".xhtml"]
mode = "command"
output_suffix = ".md"
anchors = ["section"]
agent_fallback = false
[extractors.preferred]
id = "pandoc"
executable = "pandoc"
argv = ["--from=html", "--to=gfm", "--wrap=none", "{input}", "--output", "{output}"]
version_args = ["--version"]
[extractors.preferred.install]
macos = ["brew", "install", "pandoc"]
linux = ["sudo", "apt-get", "install", "pandoc"]
windows = ["winget", "install", "--id", "JohnMacFarlane.Pandoc", "-e"]
[[extractors.fallbacks]]
id = "builtin.html"
argv = ["{input}", "{output}"]

[[extractors]]
id = "pdf"
version = "1"
timeout_seconds = 120
max_output_bytes = 536870912
mimes = ["application/pdf"]
extensions = [".pdf"]
mode = "command"
output_suffix = ".md"
anchors = ["page"]
agent_fallback = true
agent_revision = "1"
[extractors.preferred]
id = "poppler.pdftotext"
executable = "pdftotext"
argv = ["-layout", "-enc", "UTF-8", "{input}", "{output}"]
version_args = ["-v"]
[extractors.preferred.install]
macos = ["brew", "install", "poppler"]
linux = ["sudo", "apt-get", "install", "poppler-utils"]
windows = ["winget", "install", "--id", "oschwartz10612.Poppler", "-e"]
[[extractors.fallbacks]]
id = "python.pymupdf"
python_distribution = "PyMuPDF"
argv = ["{input}", "{output}"]
[extractors.fallbacks.install]
macos = ["python3", "-m", "pip", "install", "PyMuPDF"]
linux = ["python3", "-m", "pip", "install", "PyMuPDF"]
windows = ["py", "-3", "-m", "pip", "install", "PyMuPDF"]

[[extractors]]
id = "docx"
version = "1"
timeout_seconds = 120
max_output_bytes = 268435456
mimes = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
mode = "command"
output_suffix = ".md"
anchors = ["section"]
agent_fallback = false
[extractors.preferred]
id = "pandoc"
executable = "pandoc"
argv = ["--from=docx", "--to=gfm", "--wrap=none", "{input}", "--output", "{output}"]
version_args = ["--version"]
[extractors.preferred.install]
macos = ["brew", "install", "pandoc"]
linux = ["sudo", "apt-get", "install", "pandoc"]
windows = ["winget", "install", "--id", "JohnMacFarlane.Pandoc", "-e"]
[[extractors.fallbacks]]
id = "python.python-docx"
python_distribution = "python-docx"
argv = ["{input}", "{output}"]
[extractors.fallbacks.install]
macos = ["python3", "-m", "pip", "install", "python-docx"]
linux = ["python3", "-m", "pip", "install", "python-docx"]
windows = ["py", "-3", "-m", "pip", "install", "python-docx"]

[[extractors]]
id = "pptx"
version = "1"
timeout_seconds = 180
max_output_bytes = 268435456
mimes = ["application/vnd.openxmlformats-officedocument.presentationml.presentation"]
extensions = [".pptx"]
mode = "python"
output_suffix = ".md"
anchors = ["slide"]
agent_fallback = true
agent_revision = "1"
[extractors.preferred]
id = "python.python-pptx"
python_distribution = "python-pptx"
argv = ["{input}", "{output}"]
[extractors.preferred.install]
macos = ["python3", "-m", "pip", "install", "python-pptx"]
linux = ["python3", "-m", "pip", "install", "python-pptx"]
windows = ["py", "-3", "-m", "pip", "install", "python-pptx"]
[[extractors.fallbacks]]
id = "libreoffice"
executable = "soffice"
argv = ["--headless", "--convert-to", "txt", "--outdir", "{output}", "{input}"]
version_args = ["--version"]
[extractors.fallbacks.install]
macos = ["brew", "install", "--cask", "libreoffice"]
linux = ["sudo", "apt-get", "install", "libreoffice"]
windows = ["winget", "install", "--id", "TheDocumentFoundation.LibreOffice", "-e"]

[[extractors]]
id = "xlsx"
version = "1"
timeout_seconds = 180
max_output_bytes = 536870912
mimes = ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]
extensions = [".xlsx"]
mode = "python"
output_suffix = ".md"
anchors = ["sheet", "row"]
agent_fallback = false
[extractors.preferred]
id = "python.openpyxl"
python_distribution = "openpyxl"
argv = ["{input}", "{output}"]
[extractors.preferred.install]
macos = ["python3", "-m", "pip", "install", "openpyxl"]
linux = ["python3", "-m", "pip", "install", "openpyxl"]
windows = ["py", "-3", "-m", "pip", "install", "openpyxl"]
[[extractors.fallbacks]]
id = "libreoffice"
executable = "soffice"
argv = ["--headless", "--convert-to", "csv", "--outdir", "{output}", "{input}"]
version_args = ["--version"]
[extractors.fallbacks.install]
macos = ["brew", "install", "--cask", "libreoffice"]
linux = ["sudo", "apt-get", "install", "libreoffice"]
windows = ["winget", "install", "--id", "TheDocumentFoundation.LibreOffice", "-e"]

[[extractors]]
id = "image"
version = "1"
timeout_seconds = 180
max_output_bytes = 268435456
mimes = ["image/png", "image/jpeg", "image/tiff", "image/webp"]
extensions = [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"]
mode = "command"
output_suffix = ".md"
anchors = ["block"]
agent_fallback = true
agent_revision = "1"
[extractors.preferred]
id = "tesseract"
executable = "tesseract"
argv = ["{input}", "stdout", "-l", "eng"]
version_args = ["--version"]
[extractors.preferred.install]
macos = ["brew", "install", "tesseract"]
linux = ["sudo", "apt-get", "install", "tesseract-ocr"]
windows = ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e"]

[[extractors]]
id = "webpage"
version = "1"
timeout_seconds = 120
max_output_bytes = 536870912
mimes = ["application/x.second-brain-url-descriptor"]
extensions = [".url.md"]
mode = "web_capture"
output_suffix = ".md"
anchors = ["section"]
agent_fallback = true
agent_revision = "1"
[extractors.preferred]
id = "builtin.http-capture"
argv = ["{input}", "{output}"]
```

This table is a safe invocation contract, not permission to install anything. The LibreOffice adapters treat `{output}` as an isolated temporary directory, discover exactly one expected exported file there, normalize it to the final Markdown staging path, and reject macros or unexpected siblings. Tesseract writes stdout captured by the parent process; it never interprets `{output}` as a shell redirection. A converter with neither `executable` nor `python_distribution` is permitted only when its ID is one of the committed `builtin.*` adapters. Every other converter specifies exactly one of literal `version_args` for its allowlisted executable or a `python_distribution` read with `importlib.metadata.version`; those two forms are mutually exclusive. Unknown TOML keys are errors rather than silently ignored configuration. Parse `agent_revision` as a string matching `[1-9][0-9]*`. Require it exactly when `agent_fallback = true` and reject it when fallback is false; the initial approved value is `"1"`.

Compute `ExtractorSpec.config_sha256` from only that extractor's canonical parsed TOML subtree, including `agent_revision`; retain `ExtractorRegistry.config_sha256` only as a diagnostic for the whole file. `ProcessingAttempt.config_sha256`, `Derivation.config_sha256`, retry checks, and derivation paths use the per-extractor digest, so an unrelated allowlist edit does not stale the corpus. Built-in adapters are always available and report the stable implementation version `builtin:<adapter-id>:<extractor-version>`. `detect_converter_version` runs only the allowlisted executable plus literal version arguments with a five-second/16-KiB cap, or reads installed package metadata; it never uses a shell. `resolve_converter()` checks the preferred converter and then fallbacks in committed order, returning the exact converter, normalized detected version, and SHA-256 of `converter-v1\0<converter-id>\0<detected-version>`; it returns `None` only when none is installed. `prerequisite_digest(extractor, ...)` is a thin wrapper over that resolution and returns its digest, or SHA-256 of `converter-v1\0unavailable\0<extractor-id>` when resolution returns `None`. Plan 3 must execute `ResolvedConverter.converter` from the same resolver rather than repeat selection. The command builds one digest per extractor ID, so an unrelated tool install or upgrade does not make the rest of a huge corpus retry-eligible. `effective_extractor_version(extractor, digest)` returns exactly `f"{extractor.extractor_version}+{digest}"`, combining the committed adapter version and full prerequisite digest; reconciliation puts that exact value in `ProcessingContext`, and both `ProcessingAttempt` and `Derivation` copy it. A converter upgrade or fallback change therefore creates a distinct derivation ID/path while retaining the old derivation. `agent_fallback` never authorizes an LLM call from the CLI: it tells the deterministic processor to return `needs_agent` (and a handoff entry) when all deterministic implementations are unavailable or the format's deterministic quality test explicitly classifies the item as visually complex. Editing `agent_revision` is an explicit user-approval boundary. Any approved improvement to the agent recipe, prompt, vision method, or other judgment procedure must bump it; because that field is hashed into this extractor's config digest, the next handoff and derivation have new immutable identity and never overwrite the prior result.

Accept only literal argument tokens and exactly `{input}`/`{output}` placeholders. Reject `sh`, `bash`, `cmd`, `powershell`, `-c`, pipes, redirects, command substitutions, and unknown placeholders. Prefer an exact detected MIME match before an extension match.

Append this shared helper to `tests/conftest.py`; all test modules use it for parent creation and binary writes:

```python
def write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
```

- [ ] **Step 4: Run registry tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_registry.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable registry core without committing**

```bash
git diff --check -- config/extractors.toml brainlib/registry.py brainlib/commands.py docs/brain/policies/source-handling.md tests/unit/test_registry.py
git diff -- config/extractors.toml brainlib/registry.py brainlib/commands.py docs/brain/policies/source-handling.md tests/unit/test_registry.py
```

**Dependency:** Core Foundation Task 4.

### Task 2: Implement safe source inventory, MIME detection, and URL descriptor recognition

**Files:**
- Create: `brainlib/inventory.py`
- Create: `tests/fixtures/inventory/plain.txt`
- Create: `tests/fixtures/inventory/renamed-pdf.txt`
- Create: `tests/fixtures/inventory/renamed-docx.bin`
- Create: `tests/fixtures/inventory/descriptor.url.md`
- Test: `tests/unit/test_inventory.py`
- Modify: `docs/brain/policies/source-handling.md`

**Interfaces:**
- Consumes: Core Foundation `RepoPaths`/`is_ignored_source_path`; Task 1 `ExtractorRegistry` media vocabulary.
- Produces: `InventoryItem`, `InventoryReport`, `UrlDescriptor`, `MediaDetector`, `inventory_raw_sources()`, `parse_url_descriptor()`, and `source_id_for_url_descriptor()`.

- [ ] **Step 1: Write failing inventory tests**

```python
def test_inventory_excludes_reserved_versions_web_sentinels_and_temps(repo_root: Path) -> None:
    write_bytes(repo_root / "sources/raw/notes/a.txt", b"visible")
    write_bytes(repo_root / "sources/raw/_versions/src_x/a/old.txt", b"hidden")
    write_bytes(repo_root / "sources/raw/_web/example/page.html", b"hidden")
    write_bytes(repo_root / "sources/raw/.brain-tmp-123", b"hidden")
    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())
    assert [item.fingerprint.path.as_posix() for item in report.items] == ["notes/a.txt"]

def test_detector_uses_pdf_magic_not_extension(repo_root: Path) -> None:
    path = repo_root / "sources/raw/renamed.txt"
    path.write_bytes(b"%PDF-1.7\\n")
    assert MediaDetector().detect(path) == "application/pdf"

def test_external_symlink_is_skipped(repo_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (repo_root / "sources/raw/outside.txt").symlink_to(outside)
    assert inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector()).items == ()

@pytest.mark.parametrize("reserved", ("_versions/src_x/a/old.txt", "_web/src_x/a/page.html"))
def test_symlink_to_reserved_in_tree_target_is_skipped(repo_root: Path, reserved: str) -> None:
    target = write_bytes(repo_root / "sources/raw" / reserved, b"hidden")
    (repo_root / "sources/raw/alias.txt").symlink_to(target)
    assert inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector()).items == ()

def test_url_descriptor_is_control_metadata_without_network(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", pytest.fail)
    descriptor = repo_root / "sources/raw/urls/example.url.md"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text("---\\nkind: url\\nurl: https://example.test/a\\ndescription: Example\\nadded: 2026-09-04\\n---\\n")
    report = inventory_raw_sources(RepoPaths.discover(repo_root), MediaDetector())
    assert report.items[0].url_descriptor == UrlDescriptor(PurePosixPath("urls/example.url.md"), "https://example.test/a", "Example", date(2026, 9, 4))

def test_url_descriptor_decodes_canonical_json_string_scalars(repo_root: Path) -> None:
    descriptor = repo_root / "sources/raw/urls/quoted.url.md"
    descriptor.parent.mkdir(parents=True)
    url = 'https://example.test/a:b?q=%5Bvalue%5D#café'
    description = 'Colon: hash # quotes "hello" brackets [x] Unicode 雪'
    descriptor.write_text(
        "---\nkind: url\n"
        f"url: {json.dumps(url, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "added: 2026-09-04\n---\n",
        encoding="utf-8",
    )
    assert parse_url_descriptor(descriptor, PurePosixPath("urls/quoted.url.md")) == UrlDescriptor(
        PurePosixPath("urls/quoted.url.md"), url, description, date(2026, 9, 4),
    )
```

- [ ] **Step 2: Run inventory tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_inventory.py -v`

Expected: FAIL because inventory and MIME detection are absent.

- [ ] **Step 3: Implement bounded, byte-aware local discovery**

Walk only `RepoPaths.raw` using `lstat`. Accept regular files and in-tree symlinks only when the resolved target remains below `raw` and its own raw-relative target path also passes `is_ignored_source_path`; this prevents aliases from re-ingesting `_versions` or `_web`. Never follow an escaping symlink. Sort records by normalized relative path. Detect PDF, PNG/JPEG, and OOXML ZIP package markers from bytes; classify valid UTF-8 samples as text; use the extension only after signature and text checks. Do not read full file bodies during normal inventory and leave `InventoryItem.sha256` as `None`.

Parse URL descriptor frontmatter as exactly the required scalar keys. The `url` and `description` values accept either the existing conservative single-line plain scalar form or a canonical JSON double-quoted string scalar decoded with `json.loads`; canonical writers use the latter so colons, `#`, quotes, brackets, escapes, and Unicode round-trip without YAML ambiguity. Do not perform implicit YAML typing. Reject duplicate keys, missing keys, multiline values, invalid JSON strings, unsupported collections, invalid dates, and non-HTTPS/HTTP URLs with an `invalid_url_descriptor` diagnostic. The parser never makes a network call. `InventoryItem.url_descriptor` carries the parsed value while its fingerprint tracks the mutable control file; descriptor bytes never populate `ContentVersion`. For the first encounter only, `source_id_for_url_descriptor` calls the canonical source-ID function with the descriptor path and SHA-256 of `b"url-descriptor-v1\0" + descriptor.url.encode("utf-8")`; later description or URL edits at the known path preserve the ledgered source ID.

- [ ] **Step 4: Run inventory tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_inventory.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable inventory component without committing**

```bash
git diff --check -- brainlib/inventory.py docs/brain/policies/source-handling.md tests/fixtures/inventory tests/unit/test_inventory.py
git diff -- brainlib/inventory.py docs/brain/policies/source-handling.md tests/fixtures/inventory tests/unit/test_inventory.py
```

**Dependency:** Task 1 and Core Foundation Task 4.

### Task 3: Add atomic sharded ledger, derivation versioning, and approved source-version adoption

**Files:**
- Create: `brainlib/ledger.py`
- Modify: `brainlib/contracts.py`
- Modify: `brainlib/commands.py`
- Modify: `docs/brain/policies/source-handling.md`
- Modify: `docs/brain/policies/approvals.md`
- Modify: `tests/conftest.py`
- Create: `tests/__init__.py`
- Create: `tests/helpers.py`
- Test: `tests/unit/test_ledger.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Core Foundation contracts and CLI protocol; Task 2 `InventoryItem`.
- Produces: `LedgerStore`, transitions, retry eligibility, extraction paths, `HistoricalBytesResolver`, `adopt_version()`, and the approval-gated `./brain source adopt-version` contract.

Create an empty `tests/__init__.py` so every later plan can import the shared helpers with `from tests.helpers import ...` without depending on pytest's import mode.

- [ ] **Step 1: Write failing ledger and adoption tests**

```python
# tests/helpers.py (later modules use `from tests.helpers import make_integrity_inputs, StaticResolver`)
def make_integrity_inputs(repo_root: Path) -> tuple[SourceRecord, InventoryItem, str]:
    old_bytes, new_bytes = b"old", b"new"
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    new_sha = hashlib.sha256(new_bytes).hexdigest()
    write_bytes(repo_root / "sources/raw/notes/note.txt", new_bytes)
    record = replace(
        make_source_record(), state=SourceState.INTEGRITY_ERROR,
        versions={old_sha: ContentVersion(old_sha, PurePosixPath("notes/note.txt"), len(old_bytes), FileFingerprint(PurePosixPath("notes/note.txt"), len(old_bytes), 1), FIXED_NOW, ())},
        active_content_sha256=old_sha, derivations={}, active_derivation_id=None,
        diagnostics=(Diagnostic("raw_checksum_mismatch", "replacement detected"),),
    )
    item = InventoryItem(FileFingerprint(PurePosixPath("notes/note.txt"), len(new_bytes), 1), "text/plain", ".txt", new_sha)
    return record, item, old_sha

class StaticResolver:
    def __init__(self, value: bytes | None) -> None:
        self.value = value
    def read_exact(self, source_id: str, raw_path: PurePosixPath, sha256: str) -> bytes | None:
        return self.value

def test_save_writes_canonical_record_and_deterministic_summary(repo_root: Path) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    store.save(record)
    assert json.loads((repo_root / "sources/ledger" / f"{record.source_id}.json").read_text())["source_id"] == record.source_id
    assert store.write_summary([record], generated_at=FIXED_NOW) == store.write_summary([record], generated_at=FIXED_NOW)

def test_adoption_materializes_prior_bytes_before_new_version_activation(repo_root: Path) -> None:
    record, changed_item, old_sha = make_integrity_inputs(repo_root)
    adoption = adopt_version(record, changed_item, paths=RepoPaths.discover(repo_root), resolver=StaticResolver(b"old"), approval_note="User approved replacement in chat", now=FIXED_NOW)
    archived = repo_root / "sources/raw/_versions" / record.source_id / old_sha / "note.txt"
    assert archived.read_bytes() == b"old"
    assert adoption.record.active_content_sha256 == changed_item.sha256
    assert adoption.record.versions[old_sha].raw_path == PurePosixPath("_versions", record.source_id, old_sha, "note.txt")
    assert adoption.record.versions[old_sha].fingerprint.path == adoption.record.versions[old_sha].raw_path
    assert adoption.citation_rewrites == (CitationRewrite(record.source_id, old_sha, PurePosixPath("_versions", record.source_id, old_sha, "note.txt")),)
    assert adoption.record.adoption_events[-1] == VersionAdoptionEvent(old_sha, changed_item.sha256 or "", "User approved replacement in chat", FIXED_NOW)

def test_git_history_resolver_uses_repo_relative_raw_path(repo_root: Path) -> None:
    subprocess.run(("git", "init"), cwd=repo_root, check=True, capture_output=True)
    subprocess.run(("git", "config", "user.email", "fixture@example.test"), cwd=repo_root, check=True)
    subprocess.run(("git", "config", "user.name", "Fixture"), cwd=repo_root, check=True)
    original = write_bytes(repo_root / "sources/raw/notes/history.txt", b"old")
    subprocess.run(("git", "add", "sources/raw/notes/history.txt"), cwd=repo_root, check=True)
    subprocess.run(("git", "commit", "-m", "fixture"), cwd=repo_root, check=True, capture_output=True)
    original.write_bytes(b"new")
    checksum = hashlib.sha256(b"old").hexdigest()
    assert GitHistoryResolver(repo_root).read_exact("src_" + "a" * 64, PurePosixPath("notes/history.txt"), checksum) == b"old"

def test_adoption_blocks_when_prior_bytes_cannot_be_proven(repo_root: Path) -> None:
    record, changed_item, _ = make_integrity_inputs(repo_root)
    with pytest.raises(ValueError, match="cannot recover prior exact bytes"):
        adopt_version(record, changed_item, paths=RepoPaths.discover(repo_root), resolver=StaticResolver(None), approval_note="User approved", now=FIXED_NOW)

def test_adoption_requires_nonempty_approval_note(repo_root: Path) -> None:
    record, changed_item, _ = make_integrity_inputs(repo_root)
    with pytest.raises(ValueError, match="approval note"):
        adopt_version(record, changed_item, paths=RepoPaths.discover(repo_root), resolver=StaticResolver(b"old"), approval_note="", now=FIXED_NOW)

def test_activate_derivation_is_immutable_and_queryable(repo_root: Path) -> None:
    store = LedgerStore(RepoPaths.discover(repo_root))
    record = make_source_record()
    active = next(iter(record.derivations.values()))
    replacement = replace(active, derivation_id="drv_" + "e" * 64, config_sha256="f" * 64, output_path=PurePosixPath("sources/extracted/notes/a.txt", "a" * 64, "drv_" + "e" * 64 + ".md"))
    assert replacement.derivation_id not in record.derivations
    activated = activate_derivation(record, replacement, now=FIXED_NOW)
    assert activated is not record
    assert activated.derivations[replacement.derivation_id] == replacement
    assert activated.active_derivation_id == replacement.derivation_id
    assert activated.state is SourceState.OK
    store.save(activated)
    representation = store.find_representation(activated.source_id, activated.active_content_sha256 or "", activated.active_derivation_id or "")
    assert representation is not None
    assert representation.anchors == (Anchor("page", "1"),)
    historical_representation = store.find_representation(activated.source_id, "a" * 64, active.derivation_id)
    assert historical_representation is not None
    assert historical_representation.derivation_id == active.derivation_id
```

- [ ] **Step 2: Run ledger tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_ledger.py tests/unit/test_cli.py -v`

Expected: FAIL because ledger persistence, adoption, and the command handler are absent.

- [ ] **Step 3: Implement ledger persistence and adoption proof**

Write source records atomically through a same-directory temporary file, flush, `fsync`, and `os.replace()`:

```python
temporary = target.with_name(f".brain-tmp-{os.getpid()}-{uuid.uuid4().hex}")
with temporary.open("w", encoding="utf-8", newline="\n") as stream:
    stream.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, target)
```

Store one record at `sources/ledger/<source-id>.json`; generate the bounded `sources/ledger.md` projection from sorted records with corpus revision, state counts, gap diagnostics, last synchronization, record links, and active artifact links. For every file-backed schema-v1 record, the checksum root plus the creation path (`previous_raw_paths[0]`, or `current_raw_path` before any rename) permanently binds `source_id`. `len(versions) == len(adoption_events) + 1`; the events must name retained, distinct checksums in one ordered nonbranching root-to-active chain, and each adopted version's `first_seen_at` equals its event's `recorded_at`. URL records retain the accepted schema-v1 evidence ceiling and are not forced into a fabricated first-URL chain. Historical deterministic provenance is checked against its stored invariant grammar, never today's registry. Historical agent provenance requires exactly `handoff_id=hnd_<64 lowercase hex>`, canonical positive-decimal `agent_revision`, and a nonempty trimmed NUL-free note.

Implement `SourceRepresentation` as the canonical immutable citation/search view. `LedgerStore.active_representations()` returns one representation only for a record whose non-null `active_content_sha256` and non-null `active_derivation_id` resolve to a linked retained version and derivation; an awaiting-approval URL descriptor with cleared active IDs is therefore absent. `find_representation(source_id, content_sha256, derivation_id)` independently returns any retained historical or active derivation when all three identifiers match, even when that source currently has no active representation. `activate_derivation()` validates that the derivation belongs to a retained content version, copies `record.derivations` into a new dictionary, assigns `derivations[derivation.derivation_id] = derivation`, and returns `dataclasses.replace(record, derivations=derivations, active_derivation_id=derivation.derivation_id, state=SourceState.OK if derivation.quality_state == "ok" else SourceState.WARNING, updated_at=now)` without mutating the old record. Plan 3 calls this helper only after output validation succeeds.

Derive output paths exactly as:

```python
def derive_extraction_path(raw_path: PurePosixPath, content_sha256: str, derivation_id: str) -> PurePosixPath:
    return PurePosixPath(raw_path.parent, raw_path.name, content_sha256, f"{derivation_id}.md")
```

Keeping the complete raw basename as a directory component prevents `notes/example.txt` and `notes/example.md` with identical bytes from colliding while preserving the user's hierarchy.

Implement `GitHistoryResolver.read_exact(source_id, raw_path, sha256)` by checking `_versions/<source-id>/<sha256>/<raw_path.name>` first, then forming `repo_raw_path = PurePosixPath("sources/raw") / raw_path` and running `git rev-list --all -- <repo_raw_path>` and `git show <revision>:<repo_raw_path>` as argument arrays. It returns bytes only when their SHA-256 equals the requested historical checksum. `adopt_version()` passes the record's source ID to the resolver and accepts only an `integrity_error` record whose observed candidate checksum equals `--candidate-sha256`. Before it activates the replacement, it writes verified old bytes to `_versions`, replaces every retained old `ContentVersion.raw_path` with its verified `_versions/<source-id>/<content-sha256>/<original-name>` path and replaces that version's `FileFingerprint` with the archive's actual relative path/size/mtime, appends the new live-path `ContentVersion` with empty retrieval events, appends `VersionAdoptionEvent(old_sha256, new_sha256, approval_note, now)`, clears the integrity diagnostic, and changes state to `pending`. It returns `VersionAdoption(record, citation_rewrites)`, where the sorted tuple contains `CitationRewrite(source_id, old_content_sha256, archive_path)`; Plan 4's curator consumes those JSON-safe entries to rewrite exact historical citation targets. The command envelope serializes each entry as `{source_id, content_sha256, raw_path}` in `payload["data"]["citation_rewrites"]`. It never overwrites a nonmatching archive file or returns a record whose historical version still targets bytes changed in place. The CLI requires:

```text
./brain source adopt-version SOURCE_ID --candidate-sha256 SHA256 --approval-note TEXT
```

The command is a mechanical approval boundary, not evidence that approval occurred; the calling agent must obtain the user's approval before supplying its factual note.

- [ ] **Step 4: Run ledger tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_ledger.py tests/unit/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable ledger and adoption workflow without committing**

```bash
git diff --check -- brainlib/ledger.py brainlib/contracts.py brainlib/commands.py docs/brain/policies/source-handling.md docs/brain/policies/approvals.md tests/unit/test_ledger.py tests/unit/test_cli.py
git diff -- brainlib/ledger.py brainlib/contracts.py brainlib/commands.py docs/brain/policies/source-handling.md docs/brain/policies/approvals.md tests/unit/test_ledger.py tests/unit/test_cli.py
```

**Dependency:** Task 2 and Core Foundation Task 3.

### Task 4: Serialize source writers and recover interrupted work safely

**Files:**
- Create: `brainlib/locking.py`
- Modify: `brainlib/ledger.py`
- Test: `tests/unit/test_locking.py`
- Modify: `tests/unit/test_ledger.py`

**Interfaces:**
- Consumes: Task 3 atomic persistence and source states.
- Produces: `LockHeldError`, `LockMetadata`, `SourceWriteLock`, and a legal stale `extracting -> pending` recovery transition.

- [ ] **Step 1: Write failing lock tests**

```python
FIXED_OLD_TIME = FIXED_NOW - timedelta(hours=2)

def write_lock(path: Path, *, pid: int, started_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "hostname": "test-host", "started_at": started_at.isoformat().replace("+00:00", "Z")}) + "\n")

def test_second_writer_receives_owner_metadata(repo_root: Path) -> None:
    path = RepoPaths.discover(repo_root).lock
    with SourceWriteLock.acquire(path):
        with pytest.raises(LockHeldError, match="pid"):
            SourceWriteLock.acquire(path)

def test_dead_old_lock_is_recovered(repo_root: Path) -> None:
    write_lock(RepoPaths.discover(repo_root).lock, pid=999999, started_at=FIXED_OLD_TIME)
    with SourceWriteLock.acquire(RepoPaths.discover(repo_root).lock, now=lambda: FIXED_NOW):
        assert True

def test_live_lock_is_never_recovered_only_for_age(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("brainlib.locking.process_is_alive", lambda pid: True)
    write_lock(RepoPaths.discover(repo_root).lock, pid=123, started_at=FIXED_OLD_TIME)
    with pytest.raises(LockHeldError):
        SourceWriteLock.acquire(RepoPaths.discover(repo_root).lock, now=lambda: FIXED_NOW)
```

- [ ] **Step 2: Run lock tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_locking.py -v`

Expected: FAIL because the lock module does not exist.

- [ ] **Step 3: Implement the lock and interruption semantics**

Create `.brain/source-write.lock` with a concrete exclusive open and JSON metadata:

```python
lock_path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
    stream.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
```

Release only when the parsed metadata equals this lock instance's `LockMetadata`. Recover a lock only after one hour when the owner PID is not alive or cannot be checked; retain any apparently live lock. Add a transition from stale `extracting` to `pending` with diagnostic `stale_extracting_recovered`.

- [ ] **Step 4: Run lock tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_locking.py tests/unit/test_ledger.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable locking component without committing**

```bash
git diff --check -- brainlib/locking.py brainlib/ledger.py tests/unit/test_locking.py tests/unit/test_ledger.py
git diff -- brainlib/locking.py brainlib/ledger.py tests/unit/test_locking.py tests/unit/test_ledger.py
```

**Dependency:** Task 3.

### Task 5: Reconcile inventory through a pluggable processor boundary

**Files:**
- Create: `brainlib/sync.py`
- Modify: `brainlib/inventory.py`
- Modify: `brainlib/ledger.py`
- Test: `tests/unit/test_sync.py`

**Interfaces:**
- Consumes: Tasks 1–4 `ExtractorRegistry`, inventory, ledger, and lock contracts.
- Produces: `SourceProcessor`, `UnavailableProcessor`, `ProcessResult`, synchronization actions/decisions/report, and `reconcile_inventory()`.

- [ ] **Step 1: Write failing synchronization tests**

```python
def make_registry(*, media_type: str = "text/plain", extension: str = ".txt", agent_fallback: bool = False) -> ExtractorRegistry:
    converter = ConverterSpec(
        converter_id="fixture.converter", executable="fixture-converter",
        argv_template=("{input}", "{output}"), version_args=("--version",),
        python_distribution=None, install_recipes={},
    )
    extractor = ExtractorSpec(
        extractor_id="fixture", extractor_version="1",
        timeout_seconds=10, max_output_bytes=1000,
        media_types=(media_type,), extensions=(extension,), mode=ExecutionMode.PYTHON,
        output_suffix=".md", expected_anchors=(("block",) if media_type == "image/png" else ("line",)),
        agent_fallback=agent_fallback,
        agent_revision="1" if agent_fallback else None,
        preferred=converter, fallbacks=(), config_sha256="c" * 64,
    )
    return ExtractorRegistry(schema_version=1, extractors=(extractor,), config_sha256="b" * 64)

def make_item(*, path: str = "notes/a.txt", content_sha256: str | None = None, size: int = 7, mtime_ns: int = 1, agent: bool = False, url_descriptor: UrlDescriptor | None = None) -> InventoryItem:
    return InventoryItem(FileFingerprint(PurePosixPath(path), size, mtime_ns), "image/png" if agent else "text/plain", ".png" if agent else ".txt", content_sha256, url_descriptor)

def prerequisite_map(registry: ExtractorRegistry, value: str = "a" * 64) -> dict[str, str]:
    return {extractor.extractor_id: value for extractor in registry.extractors}

def never_hash(path: Path) -> str:
    raise AssertionError(f"unexpected hash for {path}")

def test_unchanged_tree_uses_metadata_fast_path_without_hashing(repo_root: Path) -> None:
    paths = RepoPaths.discover(repo_root)
    record = make_source_record()
    inventory = InventoryReport((make_item(),), ())
    registry = make_registry()
    _, report = reconcile_inventory(inventory, {record.source_id: record}, registry=registry, processor=UnavailableProcessor(), paths=paths, prerequisite_digests=prerequisite_map(registry), hash_file=never_hash, now=FIXED_NOW)
    assert report.hashed_paths == ()
    assert report.decision_counts[SyncAction.RETAIN] == 1

def test_changed_ledgered_raw_bytes_remain_unadopted_integrity_error(repo_root: Path) -> None:
    record, changed_item, old_sha = make_integrity_inputs(repo_root)
    registry = make_registry()
    updated, _ = reconcile_inventory(InventoryReport((changed_item,), ()), {record.source_id: record}, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), hash_file=lambda _: changed_item.sha256 or "", now=FIXED_NOW)
    assert updated[record.source_id].state is SourceState.INTEGRITY_ERROR
    assert updated[record.source_id].active_content_sha256 == old_sha

def test_agent_mode_source_is_handed_off_not_reported_complete(repo_root: Path) -> None:
    item = make_item(path="images/a.png", content_sha256="e" * 64, agent=True)
    registry = make_registry(media_type="image/png", extension=".png", agent_fallback=True)
    updated, report = reconcile_inventory(InventoryReport((item,), ()), {}, registry=registry, processor=NeedsAgentProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    assert updated[next(iter(updated))].state is SourceState.NEEDS_AGENT
    assert report.handoff_source_ids
    assert report.coverage_gaps

def test_url_descriptor_awaits_approval_with_no_network(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", pytest.fail)
    descriptor = UrlDescriptor(PurePosixPath("urls/a.url.md"), "https://example.test/a", "A", date(2026, 9, 4))
    item = make_item(path="urls/a.url.md", url_descriptor=descriptor)
    registry = make_registry()
    updated, _ = reconcile_inventory(InventoryReport((item,), ()), {}, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    record = next(iter(updated.values()))
    assert record.state is SourceState.AWAITING_APPROVAL
    assert record.versions == {}

def make_captured_url_record(item: InventoryItem) -> SourceRecord:
    assert item.url_descriptor is not None
    base = make_source_record()
    checksum = base.active_content_sha256 or ""
    version = replace(base.versions[checksum], raw_path=PurePosixPath("_web", base.source_id, checksum, "a.html"))
    metadata = UrlDescriptorMetadata(item.url_descriptor.url, item.url_descriptor.description, item.url_descriptor.added, item.fingerprint)
    return replace(base, current_raw_path=item.fingerprint.path, versions={checksum: version}, url_descriptor=metadata)

def test_captured_descriptor_sync_retains_local_representation_and_updates_description(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", pytest.fail)
    first = make_item(path="urls/a.url.md", size=40, mtime_ns=1, url_descriptor=UrlDescriptor(PurePosixPath("urls/a.url.md"), "https://example.test/a", "Old", date(2026, 9, 4)))
    record = make_captured_url_record(first)
    edited = make_item(path="urls/a.url.md", size=44, mtime_ns=2, url_descriptor=UrlDescriptor(PurePosixPath("urls/a.url.md"), "https://example.test/a", "New description", date(2026, 9, 4)))
    registry = make_registry()
    before_revision = compute_corpus_revision([record])
    updated, report = reconcile_inventory(InventoryReport((edited,), ()), {record.source_id: record}, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    result = updated[record.source_id]
    assert result.state is SourceState.OK
    assert result.url_descriptor.description == "New description"
    assert result.active_content_sha256 == record.active_content_sha256
    assert result.active_derivation_id == record.active_derivation_id
    assert compute_corpus_revision([result]) == before_revision
    assert report.new_active_representations == ()

def test_descriptor_url_change_awaits_fresh_approval_but_retains_snapshots(repo_root: Path) -> None:
    first = make_item(path="urls/a.url.md", url_descriptor=UrlDescriptor(PurePosixPath("urls/a.url.md"), "https://example.test/a", "A", date(2026, 9, 4)))
    record = make_captured_url_record(first)
    changed = replace(first, url_descriptor=replace(first.url_descriptor, url="https://example.test/b"), fingerprint=replace(first.fingerprint, mtime_ns=2))
    registry = make_registry()
    store = LedgerStore(RepoPaths.discover(repo_root))
    old_content_sha256 = record.active_content_sha256 or ""
    old_derivation_id = record.active_derivation_id or ""
    before_revision = compute_corpus_revision([record])
    updated, _ = reconcile_inventory(InventoryReport((changed,), ()), {record.source_id: record}, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    result = updated[record.source_id]
    assert result.state is SourceState.AWAITING_APPROVAL
    assert result.versions == record.versions
    assert result.derivations == record.derivations
    assert result.active_content_sha256 is None
    assert result.active_derivation_id is None
    assert compute_corpus_revision([result]) != before_revision
    store.save(result)
    assert all(item.source_id != record.source_id for item in store.active_representations())
    historical = store.find_representation(record.source_id, old_content_sha256, old_derivation_id)
    assert historical is not None
    assert historical.derivation_id == old_derivation_id

def test_checkpoint_runs_before_next_source_decision(repo_root: Path) -> None:
    item = make_item(path="images/a.png", content_sha256="e" * 64, agent=True)
    registry = make_registry(media_type="image/png", extension=".png", agent_fallback=True)
    checkpointed: list[SourceRecord] = []
    def checkpoint(record: SourceRecord) -> None:
        checkpointed.append(record)
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        reconcile_inventory(InventoryReport((item,), ()), {}, registry=registry, processor=NeedsAgentProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), checkpoint=checkpoint, now=FIXED_NOW)
    assert checkpointed[0].state is SourceState.NEEDS_AGENT

def test_unambiguous_rename_returns_json_safe_citation_rewrite(repo_root: Path) -> None:
    record = make_source_record()
    item = make_item(path="notes/renamed.txt", content_sha256=record.active_content_sha256)
    registry = make_registry()
    updated, report = reconcile_inventory(InventoryReport((item,), ()), {record.source_id: record}, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    assert updated[record.source_id].versions[record.active_content_sha256 or ""].raw_path == PurePosixPath("notes/renamed.txt")
    assert updated[record.source_id].versions[record.active_content_sha256 or ""].fingerprint.path == PurePosixPath("notes/renamed.txt")
    assert report.citation_rewrites == (CitationRewrite(record.source_id, record.active_content_sha256 or "", PurePosixPath("notes/renamed.txt")),)
    json.dumps([item.to_dict() for item in report.citation_rewrites])

def test_unchanged_failed_attempt_retries_only_when_prerequisite_digest_changes(repo_root: Path) -> None:
    item = make_item(content_sha256="a" * 64)
    attempt = ProcessingAttempt("a" * 64, "builtin.text", "1", "c" * 64, "missing-tool", SourceState.FAILED, FIXED_NOW, ("converter_missing",))
    record = replace(make_source_record(), state=SourceState.FAILED, last_attempt=attempt)
    assert not retry_is_eligible(record, input_sha256="a" * 64, extractor=make_registry().extractors[0], prerequisite_digest="b" * 64)
    assert retry_is_eligible(record, input_sha256="a" * 64, extractor=make_registry().extractors[0], prerequisite_digest="d" * 64)

class WarningProcessor:
    def process(self, record: SourceRecord, item: InventoryItem, extractor: ExtractorSpec, *, paths: RepoPaths, context: ProcessingContext) -> ProcessResult:
        source_bytes = context.input_path.read_bytes()  # exact pinned authority
        assert hashlib.sha256(source_bytes).hexdigest() == context.input_sha256
        identifier = derivation_id(source_sha256=context.input_sha256, extractor_id=extractor.extractor_id, extractor_version=context.extractor_version, config_sha256=context.config_sha256)
        output_path = PurePosixPath("sources/extracted/notes/a.txt", context.input_sha256, identifier + ".md")
        output = paths.root / output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'<a id="line:1"></a>\nwarning\n')
        output_stat = output.stat()
        derivation = Derivation(identifier, context.input_sha256, extractor.extractor_id, context.extractor_version, context.config_sha256, output_path, compute_sha256(output), output_stat.st_size, output_stat.st_mtime_ns, "warning", (Anchor("line", "1"),), context.attempted_at, method="deterministic", method_metadata={"converter_id": "fixture.converter", "converter_version": "fixture-1"})
        attempt = ProcessingAttempt(context.input_sha256, extractor.extractor_id, context.extractor_version, context.config_sha256, context.prerequisite_digest, SourceState.WARNING, context.attempted_at, ("partial_text",))
        return ProcessResult(SourceState.WARNING, derivation, attempt, (Diagnostic("partial_text", "searchable with a limitation"),))

def test_new_warning_derivation_is_searchable_and_reported_fresh(repo_root: Path) -> None:
    item = make_item(content_sha256="a" * 64)
    registry = make_registry()
    updated, report = reconcile_inventory(InventoryReport((item,), ()), {}, registry=registry, processor=WarningProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), now=FIXED_NOW)
    record = next(iter(updated.values()))
    assert record.state is SourceState.WARNING
    assert report.new_active_representations[0].quality_state == "warning"
    assert report.coverage_gaps

def test_large_unchanged_corpus_keeps_output_and_peak_memory_bounded(repo_root: Path) -> None:
    checksum = "a" * 64
    records: dict[str, SourceRecord] = {}
    items: list[InventoryItem] = []
    for number in range(5_000):
        path = PurePosixPath(f"bulk/{number:05d}.txt")
        fingerprint = FileFingerprint(path, 7, 1)
        source_id = source_id_for_first_seen(path, checksum)
        version = ContentVersion(checksum, path, 7, fingerprint, FIXED_NOW, ())
        records[source_id] = replace(make_source_record(source_id=source_id), current_raw_path=path, versions={checksum: version})
        items.append(InventoryItem(fingerprint, "text/plain", ".txt", None))
    tracemalloc.start()
    registry = make_registry()
    _, report = reconcile_inventory(InventoryReport(tuple(items), ()), records, registry=registry, processor=UnavailableProcessor(), paths=RepoPaths.discover(repo_root), prerequisite_digests=prerequisite_map(registry), hash_file=never_hash, now=FIXED_NOW)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rendered = json.dumps({"decision_counts": {key.value: value for key, value in report.decision_counts.items()}, "sampled_decisions": [decision.reason for decision in report.sampled_decisions]})
    assert len(report.sampled_decisions) <= 100
    assert len(rendered.encode("utf-8")) < 65_536
    assert peak < 64 * 1024 * 1024
```

- [ ] **Step 2: Run synchronization tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_sync.py -v`

Expected: FAIL because reconciliation and the processor protocol are absent.

- [ ] **Step 3: Implement reconciliation and processor outcomes**

Compare sorted normalized paths, byte sizes, and mtimes before hashing. Hash only new candidates, metadata-mismatched candidates, and potential rename pairs. Reconcile a rename only when exactly one missing ledger record and one new candidate share a checksum; otherwise create a distinct pending record and emit `ambiguous_rename`.

Apply these deterministic outcomes:

```python
if item.url_descriptor is not None:
    record = reconcile_url_descriptor(record, item, now=now)
    state = record.state
elif extractor is None:
    state = SourceState.UNSUPPORTED
elif retry_is_eligible(
    record,
    input_sha256=verified_sha256,
    extractor=extractor,
    prerequisite_digest=prerequisite_digests[extractor.extractor_id],
):
    activation_rollback: list[SourceRecord] = []
    activation_guard: ActivationGuard | None = None

    def restore_extracting_checkpoint_without_discarding_its_token() -> None:
        if not activation_rollback:
            return
        extracting = activation_rollback[-1]
        try:
            publish(extracting)
        except Exception:
            publish(extracting)
        if activation_guard is not None:
            activation_guard.clear()
        activation_rollback.clear()

    def process_pinned(
        pinned: PinnedFile,
    ) -> tuple[SourceRecord, ProcessResult, Mapping[str, JSONValue] | None]:
        assert pinned.snapshot.sha256 == verified_sha256
        extracting = extracting_checkpoint(record, now=now)
        publish(extracting)
        context = ProcessingContext(
            verified_sha256,
            effective_extractor_version(extractor, prerequisite_digest),
            extractor.config_sha256,
            prerequisite_digest,
            now,
            pinned.descriptor_path,
            pinned.descriptor,
        )
        result = processor.process(
            record, item, extractor, paths=paths, context=context
        )
        validate_result(result, context, paths)
        prepared = prepare_final_record(record, result)
        if result.derivation is None:
            publish(prepared)
            return prepared, result, None

        def activate_pinned(output: PinnedFile) -> tuple[SourceRecord, Mapping[str, JSONValue]]:
            nonlocal activation_guard
            validate_exact_output_bytes(result.derivation, output)
            final = activate_derivation(prepared, result.derivation, now=now)
            event = prepare_new_representation_event(final)
            activation_guard = ActivationGuard.prepare(paths, extracting, final)
            activation_rollback.append(replace(extracting, active_derivation_id=None))
            publish(final)
            return final, event

        # The canonical output authority stays live through activation and the
        # final shard checkpoint; use_stable_file revalidates it afterward. If
        # either authority rejects that active post-image, keep the exact
        # inactive fallback in the guard even if every local compensation fails.
        final, event = use_stable_file(
            paths,
            SnapshotNamespace.EXTRACTED,
            result.derivation.output_path,
            activate_pinned,
            include_sha256=True,
        )
        return final, result, event

    # The raw authority likewise remains live through all processor work and
    # the nested output checkpoint.
    try:
        record, result, event = use_stable_file(
            paths,
            SnapshotNamespace.RAW_USER,
            item.fingerprint.path,
            process_pinned,
            include_sha256=True,
            expected_fingerprint=item.fingerprint,
        )
    except BaseException:
        restore_extracting_checkpoint_without_discarding_its_token()
        raise
    if activation_guard is not None:
        activation_guard.clear()
    activation_rollback.clear()
    if event is not None:
        commit_new_representation_event(event)
else:
    state = record.state
```

`reconcile_url_descriptor` uses `SourceRecord.url_descriptor` and never treats descriptor bytes as evidence. A new descriptor or a descriptor with no captured content version becomes `awaiting_approval`; an unchanged URL with an active captured derivation retains its existing `ok`/`warning` state. A description/fingerprint-only edit updates `UrlDescriptorMetadata` without network or corpus change and preserves both active IDs. A URL change updates the control metadata, retains every prior `ContentVersion` and `Derivation` for explicit historical lookup, clears both `active_content_sha256` and `active_derivation_id`, and becomes `awaiting_approval`. Clearing those active IDs changes the corpus revision and removes the source from `LedgerStore.active_representations()` until an approved capture activates a representation for the new URL. Sync never refreshes it or makes a network call.

`UnavailableProcessor.process()` must return `SourceState.PENDING`, no derivation, a `ProcessingAttempt(context.input_sha256, extractor.extractor_id, context.extractor_version, context.config_sha256, context.prerequisite_digest, SourceState.PENDING, context.attempted_at, ("extractor_processor_unavailable",))`, and diagnostic `extractor_processor_unavailable`; it must not return `ok`. Reconciliation persists `result.attempt` as `last_attempt`. A processor may return `needs_agent` only when `extractor.agent_fallback` is configured; there is no `ExecutionMode.AGENT` and the CLI never invokes an agent. A synchronous Plan 3 processor consumes only `context.input_path` or `context.input_descriptor` while the callback is active. Batch integration copies only from that authority into its private fsynced content-addressed stage while the callback is active; native/external workers consume and revalidate the owned staged descriptor. Reopening `paths.raw` is forbidden in every case. Reconciliation independently opens the exact canonical `EXTRACTED` namespace path, computes its stable SHA-256, and matches checksum, size, mtime, path, and derivation identity. That pinned output authority remains live through activation, result-event preparation, and the final canonical shard checkpoint, then is revalidated before success; the raw authority is revalidated before the representation event commits. Before the active checkpoint, `ActivationGuard.prepare()` atomically publishes and fsyncs `sources/ledger/<source-id>.activation-pending`. It contains the validated exact inactive `extracting` rollback record (`active_derivation_id=None`), the previous active ID needed to identify the extracting predecessor, and the exact candidate record. An equal-metadata replacement at either authority invokes that inactive compensation. The guard is cleared only after both authorities succeed or the exact inactive compensation is durably saved; repeated compensation failures leave it beside the shard. Every `LedgerStore` record/representation reader rejects a guarded source, and full-ledger readers reject any guard. A locked retry validates the marker and its exact predecessor/candidate/rollback shard match, saves the inactive record, then removes the unchanged guard before ordinary reconciliation. Malformed, replaced, or mismatched evidence remains in place and fails closed; recovery has one attempt per invocation, with no general retry loop. Keep unchanged failures visible and unprocessed unless the `ProcessingAttempt` input hash, effective extractor version, per-extractor configuration digest, selected prerequisite digest, or explicit retry changes.

After every created or changed `SourceRecord`—including unsupported, awaiting-approval, needs-agent, integrity, missing, rename, metadata-update, failed, warning, and successful outcomes—update the returned mapping and invoke `checkpoint(record)` before considering the next source. The command supplies `LedgerStore.save`; no second post-reconciliation record-save loop exists. A shard-coupled rewrite or new-representation event is first appended and fsynced as an intent carrying the exact canonical post-record digest, then the shard is checkpointed, then a commit frame is appended and fsynced. Recovery commits an interrupted intent only when a fresh canonical shard exactly matches that post-image; otherwise it omits the intent. Handoff and record-backed gap observations carry their final record digest, while inventory gaps and successful hash actions are standalone run observations. A record-backed coverage occurrence is identified by its owning `source_id`, canonical diagnostic payload, and per-source duplicate ordinal; this preserves legitimate duplicates within and across sources without renumbering an unchanged source when another source changes. An unambiguous rename updates the matching retained `ContentVersion.raw_path` and its `FileFingerprint.path`/observed metadata to the renamed materialization, preserves `ContentVersion.first_seen_at` and `previous_raw_paths`, and emits a `citation_rewrite` event; ambiguous renames do neither. Mark missing raw paths `warning` with `raw_missing`; never delete history or derivations. Counts remain exact, while decisions, hashes, representations, rewrites, handoffs, and gaps use deterministic 100-item/32-KiB samples. Every committed event is streamed to an atomic, fsynced, content-addressed result manifest; command JSON carries only bounded samples, exact counts, and its `{result_id, path, sha256, corpus_revision, event_counts}` reference.

- [ ] **Step 4: Run synchronization tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_sync.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable reconciliation engine without committing**

```bash
git diff --check -- brainlib/sync.py brainlib/inventory.py brainlib/ledger.py tests/unit/test_sync.py
git diff -- brainlib/sync.py brainlib/inventory.py brainlib/ledger.py tests/unit/test_sync.py
```

**Dependency:** Tasks 1–4.

### Task 6: Wire init, sync, status, and full ledger validation

**Files:**
- Modify: `brainlib/commands.py`
- Modify: `brainlib/validation.py`
- Modify: `brainlib/output.py`
- Modify: `brainlib/cli.py`
- Modify: `BRAIN.md`
- Modify: `docs/brain/workflows/initialize.md`
- Modify: `docs/brain/workflows/synchronize.md`
- Test: `tests/integration/test_init_and_sync.py`
- Test: `tests/integration/test_validate_ledger.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1–5 complete source mechanics and Core Foundation `validate_template_layout()`.
- Produces: working `./brain init`, `./brain sync`, `./brain status`, `./brain source adopt-version`, normal ledger validation, and full checksum validation.

- [ ] **Step 1: Write failing command and validator integration tests**

```python
@dataclass(frozen=True)
class InProcessResult:
    returncode: int
    stdout: str
    stderr: str

def unavailable_services() -> CommandServices:
    return CommandServices(processor_factory=UnavailableProcessor, prerequisite_digest=lambda extractor: "a" * 64)

def run_brain(repo_root: Path, *argv: str, services: CommandServices | None = None) -> InProcessResult:
    stdout, stderr = io.StringIO(), io.StringIO()
    return InProcessResult(main(list(argv), cwd=repo_root, stdout=stdout, stderr=stderr, services=services), stdout.getvalue(), stderr.getvalue())

def initialize_one_active_record(repo_root: Path) -> None:
    paths = RepoPaths.discover(repo_root)
    raw = write_bytes(paths.raw / "notes/a.txt", b"original")
    raw_sha = compute_sha256(raw)
    prerequisite = hashlib.sha256(b"converter-v1\0fixture.converter\0fixture-1").hexdigest()
    extractor_version = "1+" + prerequisite
    identifier = derivation_id(source_sha256=raw_sha, extractor_id="builtin.text", extractor_version=extractor_version, config_sha256="c" * 64)
    output = write_bytes(paths.extracted / "notes/a.txt" / raw_sha / (identifier + ".md"), b'<a id="line:1"></a>\noriginal\n')
    derivation = Derivation(identifier, raw_sha, "builtin.text", extractor_version, "c" * 64, paths.repo_relative(output), compute_sha256(output), output.stat().st_size, output.stat().st_mtime_ns, "ok", (Anchor("line", "1"),), FIXED_NOW, method="deterministic", method_metadata={"converter_id": "fixture.converter", "converter_version": "fixture-1"})
    version = ContentVersion(raw_sha, PurePosixPath("notes/a.txt"), raw.stat().st_size, FileFingerprint(PurePosixPath("notes/a.txt"), raw.stat().st_size, raw.stat().st_mtime_ns), FIXED_NOW, ())
    record = SourceRecord(1, source_id_for_first_seen(PurePosixPath("notes/a.txt"), raw_sha), PurePosixPath("notes/a.txt"), (), "text/plain", raw.stat().st_size, SourceState.OK, {raw_sha: version}, raw_sha, {derivation.derivation_id: derivation}, derivation.derivation_id, None, (), FIXED_NOW, FIXED_NOW, FIXED_NOW)
    store = LedgerStore(paths)
    store.save(record)
    store.write_summary([record], generated_at=FIXED_NOW)

def test_init_records_all_inputs_and_exits_complete_with_gaps_for_unavailable_processor(repo_root: Path) -> None:
    write_bytes(repo_root / "sources/raw/notes/a.txt", b"hello")
    result = run_brain(repo_root, "--json", "init", services=unavailable_services())
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["data"]["handoff_source_ids"] == []

def test_sync_is_idempotent_and_reports_no_hashed_paths_for_unchanged_tree(repo_root: Path) -> None:
    services = unavailable_services()
    run_brain(repo_root, "init", services=services)
    result = run_brain(repo_root, "--json", "sync", services=services)
    assert json.loads(result.stdout)["data"]["hashed_paths"] == []

def test_pristine_uninitialized_ledger_summary_is_valid(repo_root: Path) -> None:
    result = run_brain(repo_root, "--json", "validate", "--full")
    assert result.returncode == 0
    assert (repo_root / "sources/ledger.md").read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"

def test_validate_full_reports_raw_checksum_mismatch(repo_root: Path) -> None:
    initialize_one_active_record(repo_root)
    (repo_root / "sources/raw/notes/a.txt").write_text("changed")
    result = run_brain(repo_root, "--json", "validate", "--full")
    assert result.returncode == 1
    assert "raw_checksum_mismatch" in result.stdout

def test_validate_full_hashes_archived_content_versions(repo_root: Path) -> None:
    record, candidate, old_sha = make_integrity_inputs(repo_root)
    adoption = adopt_version(record, candidate, paths=RepoPaths.discover(repo_root), resolver=StaticResolver(b"old"), approval_note="User approved", now=FIXED_NOW)
    store = LedgerStore(RepoPaths.discover(repo_root))
    store.save(adoption.record)
    archived = repo_root / "sources/raw" / adoption.record.versions[old_sha].raw_path
    archived.write_bytes(b"corrupt archive")
    result = run_brain(repo_root, "--json", "validate", "--full")
    assert result.returncode == 1
    assert "raw_checksum_mismatch" in result.stdout

def test_full_validation_reuses_one_checksum_cache(repo_root: Path) -> None:
    initialize_one_active_record(repo_root)
    paths = RepoPaths.discover(repo_root)
    records = LedgerStore(paths).load_all()
    calls: Counter[Path] = Counter()
    def counted_hash(path: Path) -> str:
        calls[path.resolve()] += 1
        return compute_sha256(path)
    cache = ChecksumCache(hash_file=counted_hash)
    cache.begin_transaction()
    assert validate_source_ledger(paths, records, full=True, checksum_cache=cache).ok
    cache.begin_transaction()
    assert validate_source_ledger(paths, records, full=True, checksum_cache=cache).ok
    assert calls and set(calls.values()) == {2}

def test_status_reports_revision_counts_warnings_and_agent_work(repo_root: Path) -> None:
    result = run_brain(repo_root, "--json", "status")
    assert set(json.loads(result.stdout)["data"]) >= {"corpus_revision", "state_counts", "warnings", "needs_agent"}
```

- [ ] **Step 2: Run integration tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_init_and_sync.py tests/integration/test_validate_ledger.py -v`

Expected: FAIL because init, sync, status, and ledger validation handlers are unavailable.

- [ ] **Step 3: Implement command orchestration and validator scope**

Under `SourceWriteLock`, `init` and `sync` discover paths, call `LedgerStore.recover_activation_guards()` before loading the ledger or replaying results, then load registry/ledger and inventory sources, compute `{extractor_id: services.prerequisite_digest(extractor)}` from only each extractor's selected allowlisted implementation/version, and open a recoverable `SyncResultWriter` before calling `reconcile_inventory(..., prerequisite_digests=digests, checkpoint=store.save, event_sink=result_writer.emit, event_commit=result_writer.commit)`. Reconciliation checkpoints every changed record before advancing; the command performs no second record-save pass. Before manifest finalization it durably stages the bounded envelope, public corpus revision, exact journal digest, and a strong digest of every full canonical shard. Finalization publishes only committed or exact-post-image-qualified intents. A retry after finalization or pending-publication failure requires the same journal and strong checkpoint and completes that staged result without reconciling or mixing a new inventory. This strong check applies whether `pending.json` is absent or already published: replay, staged/in-flight cleanup, and acknowledgement of a pending/staged result all fail closed before removing evidence if any full shard differs, even when the public corpus revision is unchanged. The command then verifies the content-addressed manifest, durably records `pending.json`, and only then regenerates `ledger.md`. Summary or source-lock cleanup failure preserves that completed result for exact replay.

Rendering or flushing producer output is not consumer acknowledgement. Every direct or CLI sync result remains pending and is replayed unchanged until the consumer verifies and drains the referenced manifest, durably deduplicates/applies its `result_id`, and explicitly calls `acknowledge_sync_result(...)` or `./brain source acknowledge-sync-result --result-id "$result_id"`. A wrong or malformed ID fails closed, a repeated acknowledgement of the last bounded receipt is idempotent, and the receipt is durably published before `pending.json` is removed. Consumers must never acknowledge from bounded display samples or before durable result-ID recording.

Define `CommandServices(processor_factory: Callable[[], SourceProcessor], prerequisite_digest: Callable[[ExtractorSpec], str])` and add the optional `services` keyword parameter to `main()` and command handlers; integration tests call `run_brain(..., services=unavailable_services())`, so they never depend on the production default processor factory. Direct Plan 2 CLI uses `UnavailableProcessor`; a Plan 3 installation registers `DeterministicSourceProcessor` with the same zero-argument factory. Records returned as `needs_agent` populate exact manifest events and bounded `handoff_source_ids` samples; pending unavailable deterministic work remains a coverage gap. `VersionAdoption.citation_rewrites` remains the exact, small adoption command result, while sync result arrays are bounded samples backed by the manifest.

`status` reads ledger records without writing and reports corpus revision, counts by all states, failure diagnostics, warnings, and agent-assistance IDs. `validate` remains integrity-only: it acquires `SourceWriteLock` before loading a coherent ledger snapshot and composes `template-layout` and `source-ledger` checks; readiness gaps remain owned by `init`/`sync`/`status`. Plan 4 extends validation with the wiki lock in the fixed global order source lock then wiki lock. When there are zero records and zero discoverable user sources, ledger validation accepts only the exact committed pre-initialization `sources/ledger.md`; the first `init` or `sync` replaces it with the generated summary. Otherwise, normal ledger checks require each discoverable raw source to have one record, valid root-bound linear file history (while retaining the schema-v1 URL ceiling), valid permanent deterministic/agent provenance grammar, existing active output paths, matching recorded output metadata, summary equivalence, and no unadopted raw metadata mismatch; normal mode does not hash bytes.

For `--full`, the outer combined command creates one `ChecksumCache`, calls `begin_transaction()` exactly once, and shares it across source and citation validation. Source validation never resets a supplied cache. Each content version maps to its authoritative namespace (`RAW_USER`, `RAW_VERSION`, or `RAW_WEB`), and every derivation maps to `EXTRACTED`; calls are `cache.sha256(paths, namespace, logical_path)`. The cache key is `(namespace, logical_path, stable_identity)`, every repeated use revalidates identity, and any replacement poisons that logical key for the transaction. It must not substitute `SourceRecord.current_raw_path` for a historical version path or compare stored producer provenance with today's registry.

`LedgerStore.render_summary()` uses deterministic byte budgets for source rows and coverage-gap rows and records exact omitted counts. Its output can never exceed the same 16-MiB limit enforced by `write_summary()`, `read_summary()`, and exact projection validation. `fcntl` is optional at import time: help and `doctor` remain available without it, `doctor` reports `source_write_lock.available=false`, and all lock-requiring routes fail closed with a bounded structured diagnostic when no proven backend exists.

Keep `search`, `snapshot-url`, `register-extraction`, and `links` as code-64 commands. Document that direct init/sync is successful only when no coverage gaps remain; it does not perform agent vision, outbound web access, or undocumented extraction.

- [ ] **Step 4: Run focused and complete verification**

Run:

```bash
python3 -m pytest tests/integration/test_init_and_sync.py tests/integration/test_validate_ledger.py tests/unit/test_cli.py -v
python3 -m pytest -v
./brain validate --full
./brain --json validate --full
```

Expected: all tests PASS; the pristine template validation commands exit `0` and report no coverage gaps.

- [ ] **Step 5: Review the independently reviewable source mechanics milestone without committing**

```bash
git diff --check -- brainlib/commands.py brainlib/validation.py brainlib/output.py brainlib/cli.py BRAIN.md docs/brain/workflows/initialize.md docs/brain/workflows/synchronize.md tests/integration/test_init_and_sync.py tests/integration/test_validate_ledger.py tests/unit/test_cli.py
git diff -- brainlib/commands.py brainlib/validation.py brainlib/output.py brainlib/cli.py BRAIN.md docs/brain/workflows/initialize.md docs/brain/workflows/synchronize.md tests/integration/test_init_and_sync.py tests/integration/test_validate_ledger.py tests/unit/test_cli.py
```

**Dependency:** Tasks 1–5 and Core Foundation Task 4.

## Plan self-review

- Spec coverage: Tasks 1–6 implement implementation boundary 2: approved registry parsing, safe inventory, deterministic source identity, source/content/derivation versions, `_versions` materialization and version-keyed citation rewrites before approval-gated adoption, sharded ledger and summary, locking, stale-state recovery, fast-path synchronization, processing-attempt retry discipline, URL approval state, processor handoffs, active/historical representations, and full validation of every retained content-version path.
- The explicit processor protocol satisfies the initialization requirement without falsely claiming that Plan 2 contains converters. Plan 3 only needs to supply a conforming processor; init/sync already invokes it for eligible deterministic inputs.
- Network and public-web capture are impossible on the sync path: URL descriptors are local control metadata and produce `awaiting_approval` only.
- Type consistency: all types consumed from Core Foundation appear in its public-interface section; every Task 1–6 output is declared before a later task consumes it.
- Placeholder scan: every ellipsis in an interface block is a Python type stub; implementation steps use concrete subprocess, lock, atomic-write, fixture, and in-process command examples. Every task has exact paths, concrete tests, failure and passing commands, implementation behavior, dependency, and a scoped non-committing review checkpoint.

## Optional final session commit

Only if this is the last plan executed in the complete conversation or PR, and the user or host workflow explicitly requests a commit, make one logical commit after reviewing every task in scope. Otherwise leave the verified worktree uncommitted.

- [ ] **Optional: create one user-requested session commit**

```bash
git diff --check
git status --short
git add config/extractors.toml brainlib docs/brain/policies docs/brain/workflows BRAIN.md tests
git commit -m "feat: add source ledger and synchronization"
```
