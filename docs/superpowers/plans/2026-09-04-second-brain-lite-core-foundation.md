# Second Brain Lite Core Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a clean, empty repository template with canonical instructions, versioned data contracts, a safe CLI protocol, and an honest validation gate.

**Architecture:** The repository launcher calls a small standard-library Python package. Immutable typed contracts sit below all commands, while a validation engine composes named checks and reports only the checks that exist. Markdown instructions route knowledge work to future shared workflows without duplicating policy.

**Tech Stack:** Python 3.11+, standard library (`argparse`, `dataclasses`, `hashlib`, `json`, `pathlib`, `tomllib`), pytest, Git-tracked Markdown and JSON Schema.

**Spec:** `docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`

## Global Constraints

- Use Python 3.11+ and no runtime third-party dependency; `pytest` is a development-only dependency.
- Code performs deterministic mechanics only. LLMs make semantic judgments; no command invents evidence, citations, or success state.
- `AGENTS.md` is the concise canonical routing file and `CLAUDE.md` is a symlink to it, never a copied policy fork.
- The empty-corpus rule is a distribution-template test only. Structural validation must accept a populated user corpus and populated wiki after initialization.
- Source discovery exclusions are exact: `.gitkeep`, `.keep`, `.DS_Store`, `sources/raw/_versions/**`, `sources/raw/_web/**`, documented `.brain-tmp-*` files, documented lock files, and a symlink resolving outside `sources/raw/`.
- Persist repository paths as normalized, relative POSIX paths only; reject absolute paths, `..`, and backslash-separated paths.
- Persist timestamps as UTC RFC 3339 strings ending in `Z`; persist JSON with sorted keys, compact separators, and one trailing newline.
- All command JSON goes to stdout in one stable envelope: `{"command": str, "ok": bool, "data": object, "warnings": list, "errors": list}`. Human output is concise and goes to stdout; human diagnostics go to stderr.
- Exit code `0` means clean completion, `1` means a completed operation with validation or coverage gaps, `2` means argument syntax error, and `64` means a recognized command whose implementation has not been delivered by the current milestone.
- Never install packages, execute a shell string, access the network, or alter Git branches.
- The repository never auto-commits and does not infer a session boundary. Make at most one logical commit for a completed conversation or PR, and only when the user or host workflow explicitly requests it; every task ends with a non-committing scoped review checkpoint.
- When several plans run in one conversation, only the final plan executed may present an optional session-commit block; every earlier plan leaves its verified worktree uncommitted.

---

## File Structure

```text
.gitignore                                  # ignores local runtime artifacts only
pyproject.toml                              # Python and pytest configuration
brain                                       # executable launcher
AGENTS.md                                   # canonical concise routing instructions
CLAUDE.md -> AGENTS.md                      # one instruction source
BRAIN.md                                    # operating model and documentation index
docs/brain/policies/repository-contract.md  # directory and authority rules
docs/brain/policies/source-handling.md      # immutable-source and exclusion rules
docs/brain/policies/approvals.md            # approval gates and actor responsibilities
docs/brain/workflows/initialize.md          # deterministic/agent initialization boundary
docs/brain/workflows/synchronize.md         # pre-question reconciliation boundary
docs/brain/schemas/README.md                # versioned contract index
docs/brain/schemas/source-record.v1.schema.json
docs/brain/schemas/page-frontmatter.v1.schema.json
docs/brain/schemas/question-frontmatter.v1.schema.json
brainlib/__init__.py
brainlib/diagnostics.py                     # JSON-safe diagnostics and reports
brainlib/contracts.py                       # source/derivation contracts and identity
brainlib/layout.py                          # repository paths and source exclusions
brainlib/output.py                          # CLI result rendering
brainlib/commands.py                        # command handlers
brainlib/cli.py                             # argparse entrypoint
brainlib/validation.py                      # composable structural validation
tests/conftest.py                           # isolated repository fixture
tests/integration/test_template_contract.py
tests/unit/test_contracts.py
tests/unit/test_cli.py
tests/unit/test_layout.py
tests/unit/test_validation.py
```

## Shared interfaces established by this plan

```python
# brainlib/diagnostics.py
JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: PurePosixPath | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

@dataclass(frozen=True)
class ValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    path: PurePosixPath | None = None
    details: Mapping[str, JSONValue] = field(default_factory=dict)

@dataclass(frozen=True)
class ValidationReport:
    checks: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]
    corpus_revision: str | None
    @property
    def ok(self) -> bool: ...
```

```python
# brainlib/contracts.py
class SourceState(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    OK = "ok"
    WARNING = "warning"
    NEEDS_AGENT = "needs_agent"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    INTEGRITY_ERROR = "integrity_error"
    AWAITING_APPROVAL = "awaiting_approval"

@dataclass(frozen=True)
class FileFingerprint:
    path: PurePosixPath
    byte_size: int
    mtime_ns: int

@dataclass(frozen=True)
class Anchor:
    kind: Literal["line", "page", "slide", "sheet", "section", "row", "block"]
    value: str

@dataclass(frozen=True)
class RetrievalMetadata:
    requested_url: str
    final_url: str
    redirects: tuple[str, ...]
    retrieved_at: datetime
    detected_media_type: str
    byte_size: int
    sha256: str
    approval_event_id: str
    approval_recorded_at: datetime
    approval_scope: str
    approval_note: str

@dataclass(frozen=True)
class UrlDescriptorMetadata:
    url: str
    description: str
    added: date
    fingerprint: FileFingerprint

@dataclass(frozen=True)
class VersionAdoptionEvent:
    prior_sha256: str
    adopted_sha256: str
    approval_note: str
    recorded_at: datetime

@dataclass(frozen=True)
class ContentVersion:
    sha256: str
    raw_path: PurePosixPath
    byte_size: int
    fingerprint: FileFingerprint
    first_seen_at: datetime
    retrieval_events: tuple[RetrievalMetadata, ...]

@dataclass(frozen=True)
class Derivation:
    derivation_id: str
    source_sha256: str
    extractor_id: str
    extractor_version: str
    config_sha256: str
    output_path: PurePosixPath
    output_sha256: str
    output_byte_size: int
    output_mtime_ns: int
    quality_state: Literal["ok", "warning"]
    anchors: tuple[Anchor, ...]
    created_at: datetime
    method: Literal["deterministic", "agent"] = "deterministic"
    method_metadata: Mapping[str, JSONValue] = field(default_factory=dict)

@dataclass(frozen=True)
class ProcessingAttempt:
    input_sha256: str
    extractor_id: str
    extractor_version: str
    config_sha256: str
    prerequisite_digest: str
    outcome: SourceState
    attempted_at: datetime
    diagnostic_codes: tuple[str, ...]

@dataclass(frozen=True)
class SourceRecord:
    schema_version: int
    source_id: str
    current_raw_path: PurePosixPath
    previous_raw_paths: tuple[PurePosixPath, ...]
    media_type: str
    byte_size: int
    state: SourceState
    versions: Mapping[str, ContentVersion]
    active_content_sha256: str | None
    derivations: Mapping[str, Derivation]
    active_derivation_id: str | None
    last_attempt: ProcessingAttempt | None
    diagnostics: tuple[Diagnostic, ...]
    created_at: datetime
    inspected_at: datetime
    updated_at: datetime
    adoption_events: tuple[VersionAdoptionEvent, ...] = ()
    url_descriptor: UrlDescriptorMetadata | None = None
    def to_dict(self) -> dict[str, JSONValue]: ...
    @classmethod
    def from_dict(cls, value: Mapping[str, JSONValue]) -> Self: ...

def source_id_for_first_seen(raw_path: PurePosixPath, content_sha256: str) -> str: ...
def compute_sha256(path: Path, *, chunk_size: int = 1_048_576) -> str: ...
def derivation_id(*, source_sha256: str, extractor_id: str, extractor_version: str, config_sha256: str) -> str: ...
def compute_corpus_revision(records: Iterable[SourceRecord]) -> str: ...
```

`source_id_for_first_seen()` is deliberately deterministic: it returns `src_` plus the full SHA-256 of UTF-8 bytes `b"source-v1\0" + raw_path.as_posix().encode() + b"\0" + content_sha256.encode()`. A record retains that ID through an unambiguous rename and all later content versions. Two identical files at different first-seen paths receive different IDs. This makes fixtures reproducible without a random-ID seam while retaining a stable logical identity after ledgering.

`SourceRecord.current_raw_path` and every `ContentVersion.raw_path` are relative to `sources/raw`; they never contain the `sources/raw/` prefix. `ContentVersion`, `RetrievalMetadata`, `UrlDescriptorMetadata`, `VersionAdoptionEvent`, `Anchor`, `Derivation`, and `ProcessingAttempt` are immutable value objects. A content version's fingerprint describes its current materialized raw path; adoption or an unambiguous rename replaces the value object with one carrying the new path/observed metadata while preserving its hash and `first_seen_at`. URL descriptor metadata describes mutable control metadata and is not a content version. A web capture appends its retrieval event to the exact content version whose bytes were fetched; identical bytes reuse that hash-keyed content version but retain the additional immutable event, while changed bytes create another content version. No refresh rewrites an older URL, redirect chain, approval event, approval note, bytes, or hash. Each approved in-place source adoption appends a `VersionAdoptionEvent`; this durable audit event is distinct from web retrieval approval. Every derivation carries only anchors derived from its own output and immutable method provenance. `method_metadata` contains exactly nonempty string keys `{converter_id, converter_version}` for `method="deterministic"`, or `{handoff_id, agent_revision, note}` for `method="agent"`; unknown, missing, blank, or non-string values are invalid. The defaults keep older positional constructor examples mechanically stable, but serialized records and activation validation require the complete method-specific mapping. `derivation_id()` hashes the four NUL-delimited fields `source_sha256`, `extractor_id`, `extractor_version`, and `config_sha256`, so the same converter configuration cannot collide across source versions. For agent extraction, the approval-gated registry `agent_revision` participates in the per-extractor configuration digest: changing the agent recipe creates a new derivation identity, while different output under an unchanged revision is a collision and is never overwritten. `last_attempt` records the exact input/extractor/configuration/prerequisite tuple used to decide whether retry is eligible.

```python
# brainlib/layout.py
@dataclass(frozen=True)
class RepoPaths:
    root: Path
    raw: Path
    extracted: Path
    ledger_dir: Path
    ledger_summary: Path
    wiki_pages: Path
    wiki_questions: Path
    registry: Path
    lock: Path
    @classmethod
    def discover(cls, start: Path) -> Self: ...
    def repo_relative(self, path: Path) -> PurePosixPath: ...

def is_ignored_source_path(relative_path: PurePosixPath) -> bool: ...

# brainlib/cli.py
def main(
    argv: Sequence[str] | None = None, *, cwd: Path | None = None,
    stdout: TextIO | None = None, stderr: TextIO | None = None,
) -> int: ...

# brainlib/validation.py
def validate_template_layout(paths: RepoPaths) -> ValidationReport: ...
def merge_reports(*reports: ValidationReport) -> ValidationReport: ...
```

### Task 1: Establish the empty repository contract and instruction skeleton

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `brain`
- Create: `AGENTS.md`
- Create: `CLAUDE.md` as a relative symlink to `AGENTS.md`
- Create: `BRAIN.md`
- Create: `docs/brain/policies/repository-contract.md`
- Create: `docs/brain/policies/source-handling.md`
- Create: `docs/brain/policies/approvals.md`
- Create: `docs/brain/workflows/initialize.md`
- Create: `docs/brain/workflows/synchronize.md`
- Create: `.agents/skills/.gitkeep`, `.claude/skills/.gitkeep`, `.codex/agents/.gitkeep`, `.claude/agents/.gitkeep`
- Create: `sources/raw/_versions/.gitkeep`, `sources/raw/_web/.gitkeep`, `sources/extracted/.gitkeep`, `sources/ledger/.gitkeep`, `sources/ledger.md`, `wiki/pages/.gitkeep`, `wiki/questions/.gitkeep`
- Create: `wiki/index.md`
- Create: `tests/conftest.py`
- Test: `tests/integration/test_template_contract.py`

**Interfaces:**
- Consumes: none.
- Produces: executable `./brain`, canonical instruction paths, empty retained directories, and `repo_root`/`repo_paths` pytest fixtures used by every later task.

- [ ] **Step 1: Write the failing template contract tests**

```python
def test_claude_entrypoint_is_a_symlink_to_agents_md(repo_root: Path) -> None:
    assert (repo_root / "CLAUDE.md").is_symlink()
    assert (repo_root / "CLAUDE.md").resolve() == repo_root / "AGENTS.md"

def test_reserved_directories_have_only_sentinels(repo_root: Path) -> None:
    for relative in (
        "sources/raw/_versions", "sources/raw/_web", "sources/extracted",
        "sources/ledger", "wiki/pages", "wiki/questions",
    ):
        assert [path.name for path in (repo_root / relative).iterdir()] == [".gitkeep"]

def test_launcher_is_executable(repo_root: Path) -> None:
    assert (repo_root / "brain").stat().st_mode & stat.S_IXUSR

def test_distribution_template_has_no_user_content(repo_root: Path) -> None:
    assert (repo_root / "wiki/index.md").read_text() == "# Second Brain Lite\n"
    assert (repo_root / "sources/ledger.md").read_text() == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"
    assert list((repo_root / "wiki/pages").iterdir()) == [repo_root / "wiki/pages/.gitkeep"]
```

- [ ] **Step 2: Run the contract tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_template_contract.py -v`

Expected: FAIL because the template files and retained directories do not exist.

- [ ] **Step 3: Create the minimum repository contract**

```toml
# pyproject.toml
[project]
name = "second-brain-lite"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8,<9"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Write `AGENTS.md` as concise routing instructions: first-time setup invokes `brain-initialize`; substantive questions invoke `brain-answer`; approved web research invokes `brain-web-research`; manual graph work invokes `brain-wiki-maintenance`; readiness invokes `brain-validate`; CLI/test/documentation architecture changes use normal software development and are not knowledge questions. Write `BRAIN.md` as the directory-role and lifecycle index. Add `wiki/index.md` containing exactly `# Second Brain Lite\n` and the canonical pre-initialization `sources/ledger.md` containing exactly `# Source Ledger\n\nNot initialized. Run `./brain init`.\n`. Document `_versions/**` and `_web/**` as reserved, non-discoverable evidence stores in `source-handling.md`. Make `brain` executable with exactly:

```python
#!/usr/bin/env python3
from brainlib.cli import main

raise SystemExit(main())
```

Create the shared fixture with these exact definitions; later task-specific helpers are appended in their owning tasks:

```python
import shutil
from pathlib import Path

import pytest

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "second-brain-lite"
    def ignore_live_state(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in {".git", ".context", ".brain", ".pytest_cache", "__pycache__", ".coverage"}}
        if Path(directory) == source_root:
            ignored.update({"sources", "wiki"} & set(names))
        return ignored
    shutil.copytree(
        source_root,
        destination,
        symlinks=True,
        ignore=ignore_live_state,
    )
    for relative in (
        "sources/raw/_versions", "sources/raw/_web", "sources/extracted",
        "sources/ledger", "wiki/pages", "wiki/questions",
    ):
        directory = destination / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()
    (destination / "sources/ledger.md").write_text("# Source Ledger\n\nNot initialized. Run `./brain init`.\n", encoding="utf-8")
    (destination / "wiki/index.md").write_text("# Second Brain Lite\n", encoding="utf-8")
    return destination

@pytest.fixture
def repo_paths(repo_root: Path) -> "RepoPaths":
    # Import RepoPaths inside the fixture so Task 1's initially failing test can
    # be written before Task 4 creates brainlib.layout.
    from brainlib.layout import RepoPaths
    return RepoPaths.discover(repo_root)
```

This fixture deliberately excludes the live repository's `sources/`, `wiki/`, `.context/`, and `.brain/` state, so a large private brain is never recopied into every test. Distribution-emptiness assertions run only against this generated pristine template fixture; the normal validators and full test suite remain valid after a user populates their own repository.

- [ ] **Step 4: Run the contract tests to verify they pass**

Run: `python3 -m pytest tests/integration/test_template_contract.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable contract without committing**

```bash
git diff --check -- .gitignore pyproject.toml brain AGENTS.md CLAUDE.md BRAIN.md docs/brain .agents .claude .codex sources wiki tests/conftest.py tests/integration/test_template_contract.py
git diff -- .gitignore pyproject.toml brain AGENTS.md CLAUDE.md BRAIN.md docs/brain .agents .claude .codex sources wiki tests/conftest.py tests/integration/test_template_contract.py
```

**Dependency:** none.

### Task 2: Add versioned source and Markdown schema contracts

**Files:**
- Create: `brainlib/__init__.py`
- Create: `brainlib/diagnostics.py`
- Create: `brainlib/contracts.py`
- Modify: `tests/conftest.py`
- Create: `docs/brain/schemas/README.md`
- Create: `docs/brain/schemas/source-record.v1.schema.json`
- Create: `docs/brain/schemas/page-frontmatter.v1.schema.json`
- Create: `docs/brain/schemas/question-frontmatter.v1.schema.json`
- Test: `tests/unit/test_contracts.py`

**Interfaces:**
- Consumes: Task 1 repository and Python test configuration.
- Produces: `Diagnostic`, `ValidationIssue`, `ValidationReport`, `SourceState`, `SourceRecord`, version/derivation value types, deterministic source identity, and canonical JSON conversion.

- [ ] **Step 1: Write failing typed-contract and schema tests**

```python
def test_source_id_is_reproducible_and_path_sensitive() -> None:
    checksum = "a" * 64
    assert source_id_for_first_seen(PurePosixPath("books/a.pdf"), checksum) == source_id_for_first_seen(PurePosixPath("books/a.pdf"), checksum)
    assert source_id_for_first_seen(PurePosixPath("books/a.pdf"), checksum) != source_id_for_first_seen(PurePosixPath("books/b.pdf"), checksum)

def test_source_record_round_trips_without_losing_active_derivation() -> None:
    record = make_source_record()
    assert SourceRecord.from_dict(record.to_dict()) == record

def test_source_record_rejects_absolute_raw_path() -> None:
    payload = make_source_record().to_dict()
    with pytest.raises(ValueError, match="repository-relative"):
        SourceRecord.from_dict({**payload, "current_raw_path": "/tmp/private.pdf"})

def test_corpus_revision_is_order_independent() -> None:
    record_a = make_source_record(source_id="src_" + "a" * 64)
    record_b = make_source_record(source_id="src_" + "b" * 64)
    assert compute_corpus_revision([record_a, record_b]) == compute_corpus_revision([record_b, record_a])

def test_web_retrieval_is_bound_to_exact_content_bytes() -> None:
    record = make_source_record(retrieval=make_retrieval_metadata(sha256="a" * 64, byte_size=7))
    event = record.versions["a" * 64].retrieval_events[-1]
    assert event.final_url == "https://example.test/final"
    assert event.approval_note == "User approved one capture for this question."

def test_version_adoption_event_round_trips() -> None:
    event = VersionAdoptionEvent("a" * 64, "b" * 64, "User approved replacement.", FIXED_NOW)
    record = replace(make_source_record(), adoption_events=(event,))
    assert SourceRecord.from_dict(record.to_dict()).adoption_events == (event,)

def test_uncaptured_url_descriptor_is_the_only_zero_version_record() -> None:
    fingerprint = FileFingerprint(PurePosixPath("urls/example.url.md"), 99, 1)
    descriptor = UrlDescriptorMetadata("https://example.test/a", "Example", date(2026, 9, 4), fingerprint)
    record = SourceRecord(
        1, "src_" + "e" * 64, fingerprint.path, (),
        "application/x.second-brain-url-descriptor", fingerprint.byte_size,
        SourceState.AWAITING_APPROVAL, {}, None, {}, None, None, (),
        FIXED_NOW, FIXED_NOW, FIXED_NOW, (), descriptor,
    )
    assert SourceRecord.from_dict(record.to_dict()) == record
    with pytest.raises(ValueError, match="zero versions"):
        SourceRecord.from_dict({**record.to_dict(), "url_descriptor": None})

def test_derivation_identity_includes_source_version_and_record_retains_anchors() -> None:
    assert derivation_id(source_sha256="a" * 64, extractor_id="pdf", extractor_version="1", config_sha256="b" * 64) != derivation_id(source_sha256="c" * 64, extractor_id="pdf", extractor_version="1", config_sha256="b" * 64)
    assert make_source_record().derivations[next(iter(make_source_record().derivations))].anchors == (Anchor("page", "1"),)

def test_derivation_method_provenance_round_trips_and_rejects_incomplete_metadata() -> None:
    record = make_source_record()
    derivation = record.derivations[record.active_derivation_id or ""]
    assert derivation.method == "deterministic"
    assert derivation.method_metadata == {
        "converter_id": "builtin.text",
        "converter_version": "builtin:builtin.text:1",
    }
    payload = record.to_dict()
    payload["derivations"][derivation.derivation_id]["method_metadata"] = {}
    with pytest.raises(ValueError, match="method_metadata"):
        SourceRecord.from_dict(payload)
```

- [ ] **Step 2: Run the contract tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_contracts.py -v`

Expected: FAIL with missing `brainlib.contracts` and `brainlib.diagnostics` imports.

- [ ] **Step 3: Implement the exact v1 contracts and schemas**

Implement all interfaces in the shared-interface block above. Require active derivations to reference an existing active content checksum; require a derivation ID of `drv_` plus 64 lower-case hexadecimal characters. Validate the exact method-specific `method_metadata` key sets and nonempty string values defined above on construction from JSON, activation, and serialization. A record may have zero versions only when it has non-null URL descriptor metadata, state `awaiting_approval`, and null active content/derivation IDs; every file-backed record and every captured web record has at least one version. Write `source-record.v1.schema.json` as JSON Schema 2020-12 with required source ID, state, paths, versions, derivations, derivation method/provenance metadata, attempts, timestamps, diagnostics, immutable `retrieval_events` entries including `approval_note`, immutable version-adoption audit events, optional URL descriptor control metadata, and anchors. Define page frontmatter fields `id`, `title`, `description`, `type`, `aliases`, `created`, and `updated`; define question frontmatter fields `id`, `title`, `description`, `answer_status`, `corpus_revision`, and `last_researched`, where `answer_status` is one of `answered`, `partial`, `unanswered`, or `conflicted`. The Markdown schema files define their frontmatter contracts only; they do not parse or validate wiki prose.

Append these concrete helper definitions to `tests/conftest.py` in this task; every later test in both plans imports these helpers rather than relying on an undefined local factory:

```python
FIXED_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

def make_retrieval_metadata(*, sha256: str = "a" * 64, byte_size: int = 7) -> RetrievalMetadata:
    return RetrievalMetadata("https://example.test/requested", "https://example.test/final", ("https://example.test/redirect",), FIXED_NOW, "text/html", byte_size, sha256, "approval-2026-09-04", FIXED_NOW, "one approved capture", "User approved one capture for this question.")

def make_source_record(*, source_id: str = "src_" + "a" * 64, retrieval: RetrievalMetadata | None = None) -> SourceRecord:
    checksum = "a" * 64
    derivation = Derivation(
        "drv_" + "b" * 64, checksum, "builtin.text", "1", "c" * 64,
        PurePosixPath("sources/extracted/notes/a.txt", checksum, "drv_" + "b" * 64 + ".md"),
        "d" * 64, 9, 1, "ok", (Anchor("page", "1"),), FIXED_NOW,
        method="deterministic",
        method_metadata={
            "converter_id": "builtin.text",
            "converter_version": "builtin:builtin.text:1",
        },
    )
    version = ContentVersion(checksum, PurePosixPath("notes/a.txt"), 7, FileFingerprint(PurePosixPath("notes/a.txt"), 7, 1), FIXED_NOW, () if retrieval is None else (retrieval,))
    return SourceRecord(1, source_id, PurePosixPath("notes/a.txt"), (), "text/plain", 7, SourceState.OK, {checksum: version}, checksum, {derivation.derivation_id: derivation}, derivation.derivation_id, None, (), FIXED_NOW, FIXED_NOW, FIXED_NOW)
```

Use this corpus-revision implementation shape:

```python
def compute_corpus_revision(records: Iterable[SourceRecord]) -> str:
    rows = sorted(
        f"{record.source_id}\0{record.active_content_sha256 or ''}\0{record.active_derivation_id or ''}"
        for record in records
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run the contract tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_contracts.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable data contract without committing**

```bash
git diff --check -- brainlib/__init__.py brainlib/diagnostics.py brainlib/contracts.py docs/brain/schemas tests/unit/test_contracts.py
git diff -- brainlib/__init__.py brainlib/diagnostics.py brainlib/contracts.py docs/brain/schemas tests/unit/test_contracts.py
```

**Dependency:** Task 1.

### Task 3: Implement the safe CLI protocol and doctor command

**Files:**
- Create: `brainlib/output.py`
- Create: `brainlib/commands.py`
- Create: `brainlib/cli.py`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `Diagnostic`, `JSONValue`, and `ValidationReport` from Task 2.
- Produces: `main()`, a stable command response envelope, `brain doctor`, and explicit code-64 behavior for recognized commands not owned by this plan.

- [ ] **Step 1: Write failing CLI protocol tests**

```python
def test_help_lists_every_public_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "adopt-version" in capsys.readouterr().out

def test_doctor_json_never_installs_or_executes_a_converter(tmp_path: Path) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(["--json", "doctor"], cwd=tmp_path, stdout=stdout, stderr=stderr) == 0
    result = json.loads(stdout.getvalue())
    assert result["command"] == "doctor"
    assert result["ok"] is True

def test_recognized_later_command_is_explicitly_unavailable(tmp_path: Path) -> None:
    stdout = io.StringIO()
    assert main(["--json", "source", "adopt-version", "src_" + "a" * 64], cwd=tmp_path, stdout=stdout) == 64
    assert json.loads(stdout.getvalue())["errors"][0]["code"] == "command_not_available"
```

- [ ] **Step 2: Run the CLI tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_cli.py -v`

Expected: FAIL because `main()` and command handlers do not exist.

- [ ] **Step 3: Implement parser, renderer, and doctor without side effects**

Define the parser routes exactly: `doctor`, `init`, `sync`, `status`, `search`, `source snapshot-url`, `source register-extraction`, `source adopt-version`, `wiki apply`, `wiki recover`, `links candidates`, `links check`, and `validate --full`. `doctor` may inspect `sys.version_info`, `shutil.which("rg")`, and registry file presence only. It must print install recipes as data and never call package managers or `subprocess.run()`.

Use this response type:

```python
@dataclass(frozen=True)
class CommandResult:
    command: str
    ok: bool
    data: Mapping[str, JSONValue]
    warnings: tuple[Diagnostic, ...] = ()
    errors: tuple[Diagnostic, ...] = ()

def unavailable(command: str, milestone: int) -> CommandResult:
    return CommandResult(
        command=command,
        ok=False,
        data={"available_after_milestone": milestone},
        errors=(Diagnostic("command_not_available", f"{command} is not available until milestone {milestone}."),),
    )
```

Routes owned by this plan are `doctor` and `validate`; all others return `unavailable()` until their owning plan changes the handler.

- [ ] **Step 4: Run the CLI tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable CLI protocol without committing**

```bash
git diff --check -- brain brainlib/output.py brainlib/commands.py brainlib/cli.py tests/unit/test_cli.py
git diff -- brain brainlib/output.py brainlib/commands.py brainlib/cli.py tests/unit/test_cli.py
```

**Dependency:** Task 2.

### Task 4: Add repository layout resolution and validation foundation

**Files:**
- Create: `brainlib/layout.py`
- Create: `brainlib/validation.py`
- Modify: `brainlib/commands.py`
- Test: `tests/unit/test_layout.py`
- Test: `tests/unit/test_validation.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: Task 1 directory contract; Task 2 `ValidationReport`; Task 3 `CommandResult` and `main()`.
- Produces: `RepoPaths.discover()`, exact source-discovery exclusion predicate, `validate_template_layout()`, and working `brain validate [--full]` for structural checks.

- [ ] **Step 1: Write failing layout and validation tests**

```python
def test_discover_finds_repository_from_nested_directory(repo_root: Path) -> None:
    nested = repo_root / "sources" / "raw" / "notes"
    nested.mkdir(parents=True)
    assert RepoPaths.discover(nested).root == repo_root

def test_exclusions_cover_versions_web_and_sentinels() -> None:
    assert is_ignored_source_path(PurePosixPath("_versions/src_a/file.pdf"))
    assert is_ignored_source_path(PurePosixPath("_web/site/page.html"))
    assert is_ignored_source_path(PurePosixPath(".gitkeep"))
    assert not is_ignored_source_path(PurePosixPath("letters/2026-09-04.md"))

def test_pristine_template_passes_structural_validation(repo_root: Path) -> None:
    report = validate_template_layout(RepoPaths.discover(repo_root))
    assert report.ok
    assert report.checks == ("template-layout",)

def test_structural_validation_accepts_populated_user_corpus(repo_root: Path) -> None:
    source = repo_root / "sources/raw/notes/a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("private note\n")
    (repo_root / "wiki/pages/example.md").write_text("# Example\n")
    assert validate_template_layout(RepoPaths.discover(repo_root)).ok

def test_validate_reports_wrong_claude_link(repo_root: Path) -> None:
    (repo_root / "CLAUDE.md").unlink()
    (repo_root / "CLAUDE.md").write_text("copied instructions\n")
    assert "claude_entrypoint_invalid" in {issue.code for issue in validate_template_layout(RepoPaths.discover(repo_root)).issues}
```

- [ ] **Step 2: Run the layout and validation tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_layout.py tests/unit/test_validation.py -v`

Expected: FAIL because layout discovery and validation are absent.

- [ ] **Step 3: Implement exact structural validation**

`RepoPaths.discover()` walks parents from `start.resolve()` and requires both `pyproject.toml` and `AGENTS.md`; otherwise it raises `FileNotFoundError("second-brain repository root not found")`. `is_ignored_source_path()` returns true for a basename in `{'.gitkeep', '.keep', '.DS_Store'}`, any component `_versions` or `_web`, a basename beginning `.brain-tmp-`, or a basename ending `.brain.lock`.

`validate_template_layout()` must check the executable launcher, required directories, canonical symlink, required schema files, and `wiki/index.md`; it must not inspect user files for emptiness. The empty-corpus assertion belongs exclusively to `test_distribution_template_has_no_user_content` against the distributed template. Structural validation must say only `template-layout` has run; it must not imply that ledger, extraction, citation, or graph checks have already run. Wire `brain validate` to this report and preserve the same scope for `--full` until Plan 2 adds ledger hashing.

- [ ] **Step 4: Run the layout and validation tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_layout.py tests/unit/test_validation.py tests/unit/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Review the independently reviewable validation foundation without committing**

```bash
git diff --check -- brainlib/layout.py brainlib/validation.py brainlib/commands.py tests/unit/test_layout.py tests/unit/test_validation.py tests/unit/test_cli.py
git diff -- brainlib/layout.py brainlib/validation.py brainlib/commands.py tests/unit/test_layout.py tests/unit/test_validation.py tests/unit/test_cli.py
```

**Dependency:** Tasks 1–3. Source Ledger and Synchronization Tasks 1–6 require this completed task.

## Plan self-review

- Spec coverage: Tasks 1–4 cover implementation boundary 1: distribution-template emptiness, concise canonical instructions, Claude symlink, launcher, CLI skeleton, source/page/question schema contracts, exact reserved-tree exclusions, canonical anchors, immutable retrieval metadata, processing attempts, and an honest validation foundation that accepts populated user content.
- Deferred scope is intentional: no source inventory, ledger persistence, extraction execution, web access, wiki parsing, citations, or graph updates occur in this plan.
- Type consistency: Source Ledger and Synchronization consumes only the interfaces declared above, especially `Anchor`, `RetrievalMetadata`, `ProcessingAttempt`, `SourceRecord`, `RepoPaths`, `CommandResult`, and `ValidationReport`.
- Placeholder scan: every ellipsis in an interface block is a Python type stub; implementation steps use concrete commands, constructors, and atomic-write code. Each task identifies exact paths, concrete test functions, fail/pass commands, implementation behavior, and a scoped non-committing review checkpoint.

## Optional final session commit

Only if this is the last plan executed in the complete conversation or PR, and the user or host workflow explicitly requests a commit, make one logical commit after reviewing every task in scope. Otherwise leave the verified worktree uncommitted.

- [ ] **Optional: create one user-requested session commit**

```bash
git diff --check
git status --short
git add .gitignore pyproject.toml brain AGENTS.md CLAUDE.md BRAIN.md docs/brain .agents .claude .codex sources wiki brainlib tests
git commit -m "feat: establish second-brain foundation"
```
