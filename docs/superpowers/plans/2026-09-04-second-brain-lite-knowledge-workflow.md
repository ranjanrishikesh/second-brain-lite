# Second Brain Lite Knowledge Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic Markdown schema, safe corpus search, evidence handoff, citation, and graph-validation foundation that lets the answer workflow create and maintain a grounded interconnected wiki.

**Architecture:** The CLI remains responsible for safe `rg` invocation and deterministic validation; agents retain semantic control over search terms, evidence judgment, topic selection, and link decisions. Markdown records are parsed into strict dataclasses, and validation joins their claim citations to immutable ledger versions and their links to the complete page/question graph. The later shared-skill milestone consumes these stable contracts to orchestrate sync, wiki-first research, and exactly three source-search passes.

**Tech Stack:** Python 3.11+ standard library, existing repository-local `brain` CLI, `rg` (ripgrep), strict repository-local YAML-frontmatter parser, pytest.

**Spec:** `docs/superpowers/specs/2026-09-04-second-brain-lite-design.md`

## Global Constraints

- Code performs mechanics; LLMs perform judgment; Markdown and Git preserve truth.
- The system uses no database, vector index, daemon, always-on service, or semantic-search service.
- The search engine is `rg`; commands invoke it with argument arrays and never interpolate a shell command.
- `./brain search` searches only `wiki/pages/`, `wiki/questions/`, or ledger-selected active representations; it never recursively scans arbitrary repository paths. Freshness probing is a bounded source-ID subset of active representations. Every path returned to an agent is repository-relative POSIX text even though `rg` receives absolute filesystem operands.
- A substantive question runs `./brain --json sync` before research, verifies and drains the referenced durable sync-result manifest, durably deduplicates/applies its `result_id`, and only then explicitly acknowledges it with `./brain --json source acknowledge-sync-result --result-id "$result_id"`; it then always runs an expected-revision `./brain --json wiki apply` (even with no proposed edits or citation rewrites) so recovery and ledger-driven citation canonicalization complete before wiki search. Exact rewrites and new active representations come from manifest events; the command arrays are bounded samples only. A consumer/output failure before acknowledgement leaves the exact result replayable, and repeated acknowledgement is idempotent.
- The wiki is searched before broader sources. When the wiki is insufficient, source investigation runs exactly three logical `rg` passes in order: discovery, expansion, verification. A logical pass may require many bounded CLI result pages, but it is one pass and must be completely drained before the next pass or synthesis.
- The pre-sufficiency freshness probe is distinct from those three research passes: it searches only the source IDs in verified `new_active_representation` events for the current sync result and is never stored in `EvidencePacket.passes`.
- Each source pass scans every bound operand batch for matching filenames, spools a complete deterministic candidate/context manifest beneath gitignored `.brain/search-runs/`, and returns bounded context pages through authenticated cursors. Fixed output caps never masquerade as completeness; stale, expired, resource-limited, or undrained runs are blocking coverage gaps.
- Public-web access is a fallback requiring explicit, current-question user approval. Approval covers one clearly stated research event; a materially expanded scope requires new approval.
- After approved web evidence is saved, extracted, ledgered, and registered, the workflow returns to source research and runs all three source passes again before synthesis.
- Originals are authoritative. A citation resolves to the cited bytes at the cited SHA-256, either at the unchanged raw path, an immutable web snapshot, or `sources/raw/_versions/<source-id>/<content-sha256>/<original-name>`.
- Factual prose has claim-level citations. A page-level source list without a marker-to-claim mapping is invalid.
- Standard relative Markdown links are canonical. Self-links, unresolved links, duplicate IDs/slugs, and unresolved ambiguous aliases are validation failures.
- The first meaningful occurrence of a genuine related topic in each content section is linked; repeated occurrences need not be linked. Raw global string replacement is forbidden.
- A changed page or Q&A record receives complete, resumably paged candidate discovery across every page and question, reciprocal relationship updates, and complete graph validation in one logical wiki write set. Wiki apply rejects an absent, stale, or not-fully-drained link-candidate proof.
- Every wiki write, recovery, and index publication goes through `./brain wiki apply` or `./brain wiki recover`, acquires the source lock and then the dedicated `.brain/wiki-write.lock` in that global order, and verifies the expected corpus revision. Agents never write live wiki files directly.
- Ledger/source/extractor behavior is supplied by earlier plans. This plan neither changes extractor allowlists nor accesses the network.
- Normal `./brain validate` is metadata-fast and compares recorded size/mtime/fingerprint/checksum fields without reading content for a new hash; `./brain validate --full` rehashes every retained content version and derivation and is required before completed handoff or PR readiness.
- The repository never auto-commits, detects sessions, creates branches, or defines PR behavior. Only the last executed plan in the plan set may prepare one logical commit for a completed conversation or PR, and only when the user or host workflow requests it; every task here uses a non-committing scoped review checkpoint.

---

## Prerequisites and stable dependencies

This is milestone 4 and starts only after the foundation, ledger, and extractor plans are implemented and their tests pass. Resolve incompatible names in those earlier plans before starting this one; do not duplicate ledger logic here.

The following prior public contracts are consumed verbatim:

```python
# brainlib/contracts.py, brainlib/diagnostics.py, brainlib/ledger.py, brainlib/locking.py, and brainlib/sync.py
from brainlib.contracts import Anchor, ContentVersion, FileFingerprint, RetrievalMetadata, SourceRecord, SourceState, compute_corpus_revision, compute_sha256
from brainlib.diagnostics import Diagnostic, ValidationIssue, ValidationReport
from brainlib.inventory import InventoryItem, SnapshotNamespace
from brainlib.layout import RepoPaths
from brainlib.ledger import CitationRewrite, LedgerStore, SourceRepresentation, VersionAdoption, adopt_version
from brainlib.locking import LockHeldError, SourceWriteLock
from brainlib.output import CommandResult
from brainlib.sync import ProcessResult, SyncAction, SyncDecision, SyncReport
from brainlib.sync_results import SyncEvent, SyncResultReference, SyncResultStore
from brainlib.sources.web import SnapshotResult
from brainlib.validation import ChecksumCache, merge_reports, validate_source_ledger, validate_template_layout

# brainlib/ledger.py
class LedgerStore:
    def active_representations(self) -> tuple[SourceRepresentation, ...]: ...
    def find_representation(
        self, source_id: str, content_sha256: str, derivation_id: str
    ) -> SourceRepresentation | None: ...

@dataclass(frozen=True)
class VersionAdoption:
    record: SourceRecord
    citation_rewrites: tuple[CitationRewrite, ...]

# `find_representation` resolves a historical source version and derivation as
# well as the current active one. `SourceRepresentation` exposes the canonical
# `source_id`, `content_sha256`, `derivation_id`, `raw_path`, `extracted_path`,
# `output_sha256`, `quality_state`, and `anchors: tuple[Anchor, ...]` fields.
# Each `CitationRewrite(source_id, content_sha256, raw_path)` names the exact
# materialized historical original path relative to `sources/raw` (for example,
# `_versions/<source-id>/<content-sha256>/<original-name>`). Exact sync rewrites
# and new-active identities are streamed from the digest-verified result manifest;
# SyncReport tuples are deterministic bounded samples. Canonical
# `SourceRepresentation.raw_path` is likewise relative to `sources/raw`, while
# `SourceRepresentation.extracted_path` is relative to the repository root.

# brainlib/sync.py
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

# brainlib/sources/web.py
class SnapshotResult:
    source_id: str
    raw_path: PurePosixPath
    content_sha256: str
    source_version: ContentVersion
    retrieval: RetrievalMetadata
    extraction_result: ProcessResult | None
    active_representation: SourceRepresentation | None
    corpus_revision: str

# brainlib/diagnostics.py
class ValidationReport:
    checks: tuple[str, ...]
    issues: tuple[ValidationIssue, ...]
    corpus_revision: str | None
```

The Core/Ledger/Extractor plan contracts above are canonical. Do not add compatibility aliases, inspect ledger JSON directly, or reimplement hash/anchor verification in this milestone.

## File structure

```text
brainlib/
  evidence.py                    # immutable evidence packet dataclasses and JSON codec
  frontmatter.py                 # strict YAML-frontmatter parser owned by this milestone
  markdown.py                    # body-aware Markdown scanning; never edits content
  search.py                      # literal rg argv construction and result decoding
  search_runs.py                 # authenticated cursors, bounded spools, run cleanup/proofs
  wiki_models.py                 # strict page/question/citation/relationship parsing
  citations.py                   # claim marker and immutable-version validation
  graph.py                       # candidate discovery and graph validation
  wiki_transaction.py            # manifest codec and all-or-recover wiki transaction
  wiki_evidence.py               # complete wiki-run and exact-citation evidence builder
  validation.py                  # add combined deterministic wiki validation
  cli.py                         # add search, links, wiki apply/recover, validate integration
docs/brain/schemas/
  citation.md                    # normative visible citation grammar
  wiki-page.md                   # page frontmatter/body contract
  question-record.md             # evolving topic Q&A contract
  evidence-packet.md             # researcher-to-curator handoff contract
tests/unit/
  test_evidence.py
  test_frontmatter.py
  test_markdown.py
  test_search.py
  test_search_runs.py
  test_wiki_models.py
  test_citations.py
  test_graph.py
  test_wiki_transaction.py
  test_wiki_evidence.py
tests/integration/
  test_brain_search.py
  test_wiki_validation.py
  test_knowledge_workflow_contract.py
tests/fixtures/wiki/
  generate_scenarios.py          # deterministic --write/--check corpus fixture generator
  valid/                         # parser-only page/question examples
  scenarios/                     # complete repo overlays: sources, ledger, wiki, and index
    citations/                   # current, historical, adoption, encoded-path, and code cases
    graph/                       # links, aliases, reciprocal edges, rename fixtures
    removal/                     # approval-gated knowledge removal fixtures
    search/                      # real-rg fixture, including an unusual UTF-8 filename
    transaction/                 # interrupted publication/recovery fixture
wiki/index.md                    # deterministically generated page/question index
```

The workflow/skill, tool-adapter, LLM-evaluation, and root-documentation work belongs to milestone 5. This milestone exposes the schema and deterministic command contracts that those later files invoke.

---

### Task 1: Define evidence packet and Markdown record contracts

**Files:**
- Create: `brainlib/evidence.py`
- Create: `brainlib/frontmatter.py`
- Create: `brainlib/markdown.py`
- Create: `brainlib/wiki_models.py`
- Create: `docs/brain/schemas/evidence-packet.md`
- Create: `docs/brain/schemas/wiki-page.md`
- Create: `docs/brain/schemas/question-record.md`
- Create: `tests/helpers_knowledge.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/test_evidence.py`
- Create: `tests/unit/test_frontmatter.py`
- Create: `tests/unit/test_markdown.py`
- Create: `tests/unit/test_wiki_models.py`
- Create: `tests/fixtures/wiki/valid/alpha.md`
- Create: `tests/fixtures/wiki/valid/what-is-alpha.md`
- Create: `tests/fixtures/wiki/scenarios/README.md`

**Interfaces:**
- Consumes: canonical `Anchor`, `Diagnostic`, `ValidationIssue`, `ValidationReport`, `SyncReport`, and the existing `repo_paths` fixture/`run_brain` test helper from the prerequisite plans.
- Produces: `parse_page`, `parse_question`, `scan_markdown`, strict completed `EvidencePacket`/`WikiEvidencePacket` handoffs, and `CuratorEvidence`; Tasks 2–5 use these exact names and fields.

- [ ] **Step 1: Write the failing tests for frontmatter, code-aware scanning, and a complete evidence handoff**

````python
from tests.helpers_knowledge import CONTENT_SHA256, DERIVATION_ID, SOURCE_ID

# tests/unit/test_wiki_models.py
def test_parse_question_requires_all_three_search_pass_term_sets(tmp_path: Path) -> None:
    record = tmp_path / "topic.md"
    record.write_text(f"""---
id: question-topic
title: Topic
description: A single sentence.
canonical_question: What is Topic?
prior_phrasings: []
answer_status: answered
corpus_revision: {"d" * 64}
last_researched: 2026-09-04
discovery_terms: [Topic]
expansion_terms: [Alpha]
verification_terms: []
---
# Topic
## Current answer
Answer.
## Supporting evidence
[^{SOURCE_ID}-line-1]
## Contradictory evidence
None.
## Related pages
## Sources
[^{SOURCE_ID}-line-1]: source_id: `{SOURCE_ID}`; content_sha256: `{CONTENT_SHA256}`; derivation_id: `{DERIVATION_ID}`; anchor: `line:1`; [original](../../sources/raw/a.txt); [extracted](../../sources/extracted/a.txt/{CONTENT_SHA256}/{DERIVATION_ID}.md#line:1)
""")
    parsed = parse_question(record)
    assert parsed.search_terms.verification == ()
    assert parsed.corpus_revision == "d" * 64


def test_scan_markdown_does_not_report_link_or_citation_inside_code() -> None:
    scan = scan_markdown("""[Alpha](alpha.md)[^cite-alpha-line-1]
```python
ignored = "[Alpha](alpha.md)[^cite-alpha-line-1]"
```
""")
    assert [link.destination for link in scan.links] == ["alpha.md"]
    assert [marker.citation_id for marker in scan.citation_markers] == ["cite-alpha-line-1"]
````

````python
# tests/unit/test_evidence.py
def test_evidence_packet_serializes_source_versions_and_counterevidence() -> None:
    revision = "d" * 64
    packet = EvidencePacket(
        question_id="question-topic",
        corpus_revision=revision,
        passes=(
            make_completed_pass("discovery", ("Topic",), revision, 1),
            make_completed_pass("expansion", ("Alpha",), revision, 2),
            make_completed_pass("verification", ("exception",), revision, 3),
        ),
        support=(EvidenceItem(SOURCE_ID, CONTENT_SHA256, DERIVATION_ID, Anchor("line", "1"), "supports Topic"),),
        counterevidence=(EvidenceItem("src_" + "b" * 64, "b" * 64, "drv_" + "c" * 64, Anchor("line", "8"), "qualifies Topic"),),
        coverage_gaps=(Diagnostic("source_failed", "src-c is failed"),),
    )
    assert EvidencePacket.from_json(packet.to_json()) == packet


@pytest.mark.parametrize(
    "passes",
    (
        ("discovery", "verification", "expansion"),
        ("discovery", "discovery", "verification"),
        ("discovery", "expansion", "verification", "verification"),
    ),
)
def test_evidence_packet_rejects_reordered_duplicate_or_fourth_pass(passes: tuple[SearchPassName, ...]) -> None:
    revision = "d" * 64
    with pytest.raises(ValueError, match="discovery, expansion, verification"):
        EvidencePacket(
            "question-topic", revision,
            tuple(make_completed_pass(name, (name,), revision, index) for index, name in enumerate(passes)),
            (), (), (),
        )


def test_evidence_packet_allows_completed_zero_hit_pass_but_rejects_undrained_or_empty_terms() -> None:
    revision = "d" * 64
    zero_hit = make_completed_pass("discovery", ("absent",), revision, 1, candidate_count=0, match_count=0)
    assert zero_hit.complete and zero_hit.candidate_count == 0
    with pytest.raises(ValueError, match="complete"):
        EvidencePacket(
            "question-topic", revision,
            (
                replace(zero_hit, complete=False),
                make_completed_pass("expansion", ("related",), revision, 2),
                make_completed_pass("verification", ("exception",), revision, 3),
            ), (), (), (),
        )
    with pytest.raises(ValueError, match="nonempty term"):
        replace(zero_hit, terms=())


def test_evidence_packet_from_json_rejects_missing_pass() -> None:
    with pytest.raises(ValueError, match="exactly three"):
        EvidencePacket.from_json('{"question_id":"question-topic","corpus_revision":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","passes":[],"support":[],"counterevidence":[],"coverage_gaps":[]}')


def test_wiki_evidence_packet_uses_document_qualified_citation_refs() -> None:
    citation = RevalidatedCitation(
        PurePosixPath("wiki/questions/what-is-alpha.md"), "cite-alpha-line-1",
        SOURCE_ID, CONTENT_SHA256, DERIVATION_ID, Anchor("line", "1"),
    )
    packet = WikiEvidencePacket(
        question_id="question-alpha", corpus_revision="d" * 64,
        search_run_id="srch_" + "1" * 32,
        matched_records=(WikiRecordMatch(
            PurePosixPath("wiki/questions/what-is-alpha.md"), "question-alpha", ("Alpha",),
        ),),
        revalidated_citations=(citation,),
        supporting_citations=(CitationRef(citation.document_path, citation.citation_id),),
        counterevidence_citations=(), contradictions=(), coverage_gaps=(), complete=True,
    )
    assert WikiEvidencePacket.from_json(packet.to_json()) == packet
    with pytest.raises(ValueError, match="revalidated citation"):
        replace(packet, supporting_citations=(
            CitationRef(PurePosixPath("wiki/pages/other.md"), citation.citation_id),
        ))
````

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_wiki_models.py -v`

Expected: FAIL because `brainlib.evidence`, `brainlib.markdown`, and `brainlib.wiki_models` do not exist.

- [ ] **Step 3: Implement the strict public types and parsers**

```python
# tests/helpers_knowledge.py
SOURCE_ID = "src_" + "a" * 64
DERIVATION_ID = "drv_" + "b" * 64
CONTENT_SHA256 = "c" * 64
FIXED_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

def make_completed_pass(
    name: SearchPassName, terms: tuple[str, ...], revision: str, number: int,
    *, candidate_count: int = 1, match_count: int = 1,
) -> SearchPassRecord:
    digest = f"{number:064x}"
    return SearchPassRecord(
        name=name, terms=terms, run_id="srch_" + digest[:32], corpus_revision=revision,
        candidate_count=candidate_count, candidate_manifest_sha256=digest,
        pages=(SearchPageRecord(0, match_count, digest),), complete=True,
    )

@dataclass(frozen=True)
class KnowledgeScenario:
    root: Path
    paths: RepoPaths
    ledger: LedgerStore

def restore_scenario_metadata(paths: RepoPaths, records: Mapping[str, SourceRecord]) -> None:
    for record in records.values():
        for version in record.versions.values():
            target = paths.raw / version.raw_path
            os.utime(target, ns=(version.fingerprint.mtime_ns, version.fingerprint.mtime_ns), follow_symlinks=False)
        for derivation in record.derivations.values():
            target = paths.root / derivation.output_path
            os.utime(target, ns=(derivation.output_mtime_ns, derivation.output_mtime_ns), follow_symlinks=False)

# tests/conftest.py
@pytest.fixture
def scenario_repo(repo_root: Path) -> Callable[[str], KnowledgeScenario]:
    installed = False

    def install(name: str) -> KnowledgeScenario:
        nonlocal installed
        if installed:
            raise RuntimeError("install exactly one knowledge scenario per test")
        if not re.fullmatch(r"[a-z0-9-]+(?:/[a-z0-9-]+)*", name):
            raise ValueError("invalid scenario name")
        overlay = repo_root / "tests/fixtures/wiki/scenarios" / name / "repo"
        if not (overlay / "sources/ledger").is_dir() or not (overlay / "wiki").is_dir():
            raise FileNotFoundError(f"incomplete knowledge scenario: {name}")
        shutil.copytree(overlay, repo_root, dirs_exist_ok=True, symlinks=True)
        paths = RepoPaths.discover(repo_root)
        ledger = LedgerStore(paths)
        records = ledger.load_all()  # fail in fixture setup if a committed record is malformed
        restore_scenario_metadata(paths, records)
        installed = True
        return KnowledgeScenario(repo_root, paths, ledger)

    return install

# tests/unit/test_frontmatter.py
def test_parse_frontmatter_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\\nid: one\\nid: two\\n---\\nbody\\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="duplicate key: id"):
        parse_frontmatter(path)

# brainlib/evidence.py
SearchPassName = Literal["discovery", "expansion", "verification"]

@dataclass(frozen=True)
class SearchPassRecord:
    name: SearchPassName
    terms: tuple[str, ...]
    run_id: str
    corpus_revision: str
    candidate_count: int
    candidate_manifest_sha256: str
    pages: tuple["SearchPageRecord", ...]
    complete: bool
    coverage_gaps: tuple[Diagnostic, ...] = ()

@dataclass(frozen=True)
class SearchPageRecord:
    page_index: int
    match_count: int
    result_sha256: str

@dataclass(frozen=True)
class EvidenceItem:
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor
    passage: str

@dataclass(frozen=True)
class EvidencePacket:
    question_id: str
    corpus_revision: str
    passes: tuple[SearchPassRecord, SearchPassRecord, SearchPassRecord]
    support: tuple[EvidenceItem, ...]
    counterevidence: tuple[EvidenceItem, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    freshness_probe_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if tuple(item.name for item in self.passes) != ("discovery", "expansion", "verification"):
            raise ValueError("passes must be exactly discovery, expansion, verification")
        if any(not item.complete for item in self.passes):
            raise ValueError("every logical search pass must be complete")

    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, value: str) -> "EvidencePacket": ...

@dataclass(frozen=True)
class WikiRecordMatch:
    path: PurePosixPath
    record_id: str
    matched_terms: tuple[str, ...]

@dataclass(frozen=True)
class RevalidatedCitation:
    document_path: PurePosixPath
    citation_id: str
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor

@dataclass(frozen=True, order=True)
class CitationRef:
    document_path: PurePosixPath
    citation_id: str

@dataclass(frozen=True)
class WikiEvidencePacket:
    question_id: str
    corpus_revision: str
    search_run_id: str
    matched_records: tuple[WikiRecordMatch, ...]
    revalidated_citations: tuple[RevalidatedCitation, ...]
    supporting_citations: tuple[CitationRef, ...]
    counterevidence_citations: tuple[CitationRef, ...]
    contradictions: tuple[Diagnostic, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    complete: bool

    def __post_init__(self) -> None: ...
    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, value: str) -> "WikiEvidencePacket": ...

CuratorEvidence: TypeAlias = WikiEvidencePacket | EvidencePacket
```

```python
# brainlib/wiki_models.py
AnswerStatus = Literal["answered", "partial", "unanswered", "conflicted"]

@dataclass(frozen=True)
class SearchTerms:
    discovery: tuple[str, ...]
    expansion: tuple[str, ...]
    verification: tuple[str, ...]

@dataclass(frozen=True)
class RelatedTarget:
    destination: str
    description: str

@dataclass(frozen=True)
class WikiPage:
    path: Path
    page_id: str
    title: str
    description: str
    page_type: str
    aliases: tuple[str, ...]
    created: date
    updated: date
    related_pages: tuple[RelatedTarget, ...]
    related_questions: tuple[RelatedTarget, ...]
    body: str

@dataclass(frozen=True)
class QuestionRecord:
    path: Path
    question_id: str
    title: str
    description: str
    canonical_question: str
    prior_phrasings: tuple[str, ...]
    answer_status: AnswerStatus
    corpus_revision: str
    last_researched: date
    search_terms: SearchTerms
    related_pages: tuple[RelatedTarget, ...]
    body: str

def parse_page(path: Path, *, text: str | None = None) -> WikiPage: ...
def parse_question(path: Path, *, text: str | None = None) -> QuestionRecord: ...
```

```python
# brainlib/frontmatter.py
class FrontmatterError(ValueError):
    pass

@dataclass(frozen=True)
class FrontmatterDocument:
    data: Mapping[str, object]
    body: str

def parse_frontmatter(path: Path, *, text: str | None = None) -> FrontmatterDocument: ...
```

`docs/brain/schemas/wiki-page.md` must state that frontmatter has `id`, `title`, `description`, `type`, `aliases`, `created`, and `updated`; body has a title, summary, topic sections, `Related pages`, `Related questions`, and final `Sources`. `docs/brain/schemas/question-record.md` must require the fields represented above plus `Current answer`, `Supporting evidence`, `Contradictory evidence`, `Related pages`, and final `Sources`; its persisted `corpus_revision` is exactly 64 lowercase hex characters. Both require one-sentence nonempty description and ISO dates. The parser rejects missing/unknown answer status, duplicate section headings, malformed list relationships, and frontmatter values of the wrong scalar/list type.

`parse_frontmatter` is owned by this task and supports only the schema-needed YAML subset: nonempty scalar strings, ISO dates retained as strings, inline scalar lists (`[Alpha, Beta]`), and `[]`. It reads `path` when `text` is absent; a supplied text override is parsed under that logical path for transaction preflight. It rejects duplicate keys, nested mappings/lists, tabs, missing delimiters, and malformed list quoting without importing PyYAML. `parse_page` and `parse_question` pass through the same optional override. `scan_markdown` must return only links, headings, and `[^citation-id]` markers outside YAML frontmatter and fenced/indented code blocks. It reads but does not modify Markdown.

`SearchPassRecord.__post_init__` requires a full `srch_<32-lowercase-hex>` run ID, a 64-lowercase-hex corpus revision and candidate-manifest checksum, one or more nonempty CR/LF/NUL-free terms, nonnegative counts, at least one page, contiguous page indexes beginning at zero, valid page checksums, and an empty pass-level `coverage_gaps` tuple when `complete=True`. Zero candidates and zero matches are valid completed outcomes. `EvidencePacket.__post_init__` and `from_json` require exactly discovery/expansion/verification in that order, three distinct run IDs, the packet revision on every pass, every pass complete, and no `search_run_incomplete`, `search_run_expired`, `search_run_stale`, or `search_spool_limit` gap. A continuation page is not a fourth pass; it contributes another `SearchPageRecord` to the same named pass.

`docs/brain/schemas/evidence-packet.md` must define that completed JSON packet plus source-versioned supporting and contradictory evidence, an optional `freshness_probe_source_ids` record separate from research, and canonical diagnostics for source coverage gaps. Both packet codecs reject duplicate JSON object keys, non-object roots, unknown/missing fields, invalid scalar types/IDs/paths, and noncanonical list order; `to_json()` emits sorted-key UTF-8 JSON with one trailing newline. It also defines `WikiEvidencePacket` as the only wiki-sufficient fast-path handoff. A curator-ready wiki packet has a current 64-hex corpus revision, one fully drained current wiki search run, at least one matched wiki record, unique revalidated exact citations, document-qualified `CitationRef` support/counterevidence entries that resolve within that citation set, explicit contradictions, an empty `coverage_gaps` tuple, and `complete: true`; the builder in Task 5 is the only production constructor. The codec rejects duplicate or unsorted records/citations/refs, an empty supporting set, unknown refs, invalid question/run/revision formats, or a record path outside `wiki/pages` or `wiki/questions`; `complete: true` additionally requires no coverage gap. The builder—not the standalone codec—proves the run and revision are current. Contradictions may remain explicit in a complete packet and drive a `conflicted` answer. `CuratorEvidence` is exactly `WikiEvidencePacket | EvidencePacket`, but the curator entry point rejects an incomplete wiki member. Both are read-only researcher-to-curator handoffs and carry no proposed Markdown edits.

`tests/fixtures/wiki/scenarios/README.md` defines the overlay convention used by every later test. Each `scenarios/<name>/repo/` is a complete, internally consistent overlay containing the scenario's `sources/raw`, `sources/extracted`, `sources/ledger`, `sources/ledger.md`, `wiki/pages`, `wiki/questions`, and `wiki/index.md`; every directory has committed content, every referenced record JSON and summary row exists, and ledger checksums, metadata, paths, and navigable anchor IDs match the committed bytes. Canonical extraction fixtures preserve the entire raw basename: `sources/extracted/<raw-parent>/<raw-name>/<content-sha256>/<derivation-id>.md` (for example, `sources/extracted/notes/a.txt/<sha>/<derivation>.md`). Broken scenarios contain only the one intentional defect named by that test. Tests install exactly one overlay through `scenario_repo`, so mutually incompatible broken fixtures never leak into another validation or transaction.

Keep reusable constants, dataclasses, and builders in `tests/helpers_knowledge.py`; tests import them there, never from `tests.conftest`. This milestone adds only pytest fixtures to `tests/conftest.py` and does not redefine the prerequisite `repo_paths` fixture or `run_brain` helper.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_wiki_models.py -v`

Expected: PASS.

- [ ] **Step 5: Review the record and evidence checkpoint without committing**

```bash
git diff --check
git diff -- brainlib/evidence.py brainlib/frontmatter.py brainlib/markdown.py brainlib/wiki_models.py docs/brain/schemas/evidence-packet.md docs/brain/schemas/wiki-page.md docs/brain/schemas/question-record.md tests/helpers_knowledge.py tests/conftest.py tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_wiki_models.py tests/fixtures/wiki/valid
```

### Task 2: Add safe, ledger-scoped `rg` search

**Files:**
- Create: `brainlib/search.py`
- Create: `brainlib/search_runs.py`
- Modify: `brainlib/cli.py`
- Modify: `brain`
- Modify: `tests/helpers_knowledge.py`
- Modify: `tests/conftest.py`
- Create: `tests/unit/test_search.py`
- Create: `tests/unit/test_search_runs.py`
- Create: `tests/integration/test_brain_search.py`
- Create: `tests/fixtures/wiki/scenarios/search/valid/repo/sources/extracted/early.txt/<content-sha256>/<derivation-id>.md`
- Create: `tests/fixtures/wiki/scenarios/search/valid/repo/sources/extracted/unusual/résumé (final).md/<content-sha256>/<derivation-id>.md`
- Create: `tests/fixtures/wiki/scenarios/search/valid/repo/sources/extracted/zzz-late.txt/<content-sha256>/<derivation-id>.md`
- Create: `tests/fixtures/wiki/scenarios/search/valid/repo/sources/ledger/<source-id>.json` for each source

**Interfaces:**
- Consumes: `LedgerStore.active_representations()`, `compute_corpus_revision`, `RepoPaths`, source/wiki locks, `Diagnostic`, and Task 1's `SearchPassName`, `SearchPageRecord`, and `SearchPassRecord`.
- Produces: `search_active_sources`, revision-bound `search_wiki`, `resume_search`, `complete_search_run`, `verify_search_run_proof`, `completed_search_pass`, `cleanup_search_runs`, and the paged `./brain search` command. Tasks 4–6 use these exact contracts.

- [ ] **Step 1: Write failing tests for complete filename scans, bounded result pages, and authenticated continuation**

```python
# tests/unit/test_search.py
def test_build_rg_argv_uses_literal_pattern_file_and_no_shell_metacharacters(tmp_path: Path) -> None:
    request = SearchRequest(scope="sources", pass_name="discovery", terms=("C++", "[draft]", "a|b"), context_lines=3)
    argv = build_rg_argv(request, phase="filenames", pattern_file=tmp_path / "terms")
    assert argv[:9] == ["rg", "--no-config", "--sort", "path", "--fixed-strings", "--files-with-matches", "--null", "--file", str(tmp_path / "terms")]
    assert "C++" not in argv
    assert "[draft]" not in argv
    assert "a|b" not in argv


def test_filename_discovery_scans_every_operand_batch_and_finds_late_alphabet_evidence(
    monkeypatch: pytest.MonkeyPatch, repo_paths: RepoPaths,
) -> None:
    ledger = make_active_ledger(repo_paths, 600, matching_numbers={599})
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    first = search_active_sources(
        repo_paths, ledger,
        SearchRequest("sources", "discovery", ("Alpha",), 2, page_size=1),
    )
    filename_calls = [argv for argv in recorder.calls if "--files-with-matches" in argv]
    assert len(filename_calls) == math.ceil(600 / MAX_RG_PATHS)
    assert sum(len(argv[argv.index("--") + 1:]) for argv in filename_calls) == 600
    first_context_index = next(index for index, argv in enumerate(recorder.calls) if "--json" in argv)
    assert all("--files-with-matches" in argv for argv in recorder.calls[:first_context_index])
    assert first_context_index == len(filename_calls)
    pages = drain_search(repo_paths, ledger, first)
    assert pages[-1].complete is True
    assert pages[-1].next_cursor is None
    assert any(match.path.as_posix().startswith("sources/extracted/search/599.txt/") for page in pages for match in page.matches)


def test_one_logical_pass_resumes_bounded_pages_and_builds_one_completed_pass_record(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 5, matching_numbers=set(range(5)))
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    first = search_active_sources(
        repo_paths, ledger, SearchRequest("sources", "verification", ("Alpha",), 2, page_size=2),
    )
    pages = drain_search(repo_paths, ledger, first)
    assert [page.page_index for page in pages] == [0, 1, 2]
    assert all(len(page.matches) <= 2 for page in pages)
    assert all(page.run_id == first.run_id for page in pages)
    pass_record = completed_search_pass(pages)
    assert pass_record.name == "verification"
    assert pass_record.complete is True
    assert len(pass_record.pages) == 3


@pytest.mark.parametrize("raw_name", ("résumé (final).md", "line\nbreak.txt", "-leading.txt"))
def test_search_round_trips_unusual_filenames_as_repo_relative_posix_paths(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
    streaming_rg_recorder: ContentAwareRgRecorder, raw_name: str,
) -> None:
    digest = f"{7:064x}"
    relative = PurePosixPath(
        "sources/extracted/unusual", raw_name, digest, f"drv_{digest}.md"
    )
    representation = make_source_representation(repo_paths, 7, extracted_path=relative)
    monkeypatch.setattr(subprocess, "Popen", streaming_rg_recorder)
    result = search_active_sources(
        repo_paths, FakeLedger((representation,)), SearchRequest("sources", "discovery", ("Alpha",), 1)
    )
    assert result.matches[0].path == relative


def test_source_search_rejects_missing_active_operand_before_spawning_rg(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
    streaming_rg_recorder: ContentAwareRgRecorder,
) -> None:
    representation = make_source_representation(repo_paths, 9)
    (repo_paths.root / representation.extracted_path).unlink()
    monkeypatch.setattr(subprocess, "Popen", streaming_rg_recorder)
    with pytest.raises(SearchOperandError, match="missing active representation"):
        search_active_sources(
            repo_paths, FakeLedger((representation,)),
            SearchRequest("sources", "discovery", ("Alpha",), 1),
        )
    assert streaming_rg_recorder.calls == []


def test_freshness_probe_searches_only_sync_report_source_ids(
    repo_paths: RepoPaths, active_ledger_factory: Callable[[int], LedgerStore], streaming_rg_recorder: ContentAwareRgRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = "src_" + f"{0:064x}"
    monkeypatch.setattr(subprocess, "Popen", streaming_rg_recorder)
    result = search_active_sources(
        repo_paths,
        active_ledger_factory(2),
        SearchRequest("sources", None, ("Alpha",), 2, freshness_source_ids=(source_id,)),
    )
    assert result.mode == "freshness"
    assert result.searched_source_ids == (source_id,)
    assert all("/search/1/" not in argument for argv in streaming_rg_recorder.calls for argument in argv)


@pytest.mark.parametrize("term", ("Alpha\\nBeta", "Alpha\\rBeta", "Alpha\\x00Beta"))
def test_search_rejects_control_characters_in_literal_terms(term: str) -> None:
    with pytest.raises(ValueError, match="CR, LF, or NUL"):
        SearchRequest("wiki", None, (term,), 2)
```

```python
# tests/unit/test_search_runs.py
def test_cursor_tamper_and_stale_corpus_revision_fail_closed(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 3, matching_numbers={0, 1, 2})
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    first = search_active_sources(
        repo_paths, ledger, SearchRequest("sources", "discovery", ("Alpha",), 1, page_size=1),
    )
    assert first.next_cursor is not None
    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    with pytest.raises(InvalidSearchCursor):
        resume_search(repo_paths, ledger, tampered)
    changed_ledger = FakeLedger(ledger.active_representations() + (make_source_representation(repo_paths, 9),))
    with pytest.raises(SearchRunStale) as raised:
        resume_search(repo_paths, changed_ledger, first.next_cursor)
    assert raised.value.diagnostic.code == "search_run_stale"


def test_expired_run_is_a_blocking_gap_and_never_becomes_completed_evidence(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 2, matching_numbers={0, 1})
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    first = search_active_sources(
        repo_paths, ledger, SearchRequest("sources", "expansion", ("Alpha",), 1, page_size=1),
        now=FIXED_NOW,
    )
    with pytest.raises(SearchRunExpired) as raised:
        resume_search(repo_paths, ledger, first.next_cursor or "", now=FIXED_NOW + SEARCH_RUN_TTL + timedelta(seconds=1))
    assert raised.value.diagnostic.code == "search_run_expired"
    with pytest.raises(ValueError, match="complete"):
        completed_search_pass((first,))


def test_search_spools_with_bounded_memory_and_page_output_then_cleans_expired_runs(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 10_000, matching_numbers=set(range(10_000)))
    recorder = ContentAwareRgRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    tracemalloc.start()
    first = search_active_sources(
        repo_paths, ledger, SearchRequest("sources", "discovery", ("Alpha",), 1, page_size=25),
        now=FIXED_NOW,
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    assert len(first.matches) <= 25
    run_dir = repo_paths.root / ".brain/search-runs" / first.run_id
    assert {path.name for path in run_dir.iterdir()} == {
        "metadata.json", "secret", "candidates.jsonl", "results.jsonl", "page-index.jsonl",
    }
    assert all(path.is_file() and not path.is_symlink() for path in run_dir.iterdir())
    complete_ledger = make_active_ledger(repo_paths, 1, matching_numbers=set())
    completed = search_active_sources(
        repo_paths, complete_ledger,
        SearchRequest("sources", "verification", ("absent",), 1), now=FIXED_NOW,
    )
    assert completed.complete is True
    unrelated = repo_paths.root / ".brain/search-runs/not-a-search-run"
    unrelated.mkdir()
    removed = cleanup_search_runs(repo_paths, now=FIXED_NOW + SEARCH_RUN_TTL + timedelta(seconds=1))
    assert PurePosixPath(".brain/search-runs") / completed.run_id in removed
    assert run_dir.is_dir()  # an undrained run is retained so resume reports expiry explicitly
    assert unrelated.is_dir()
    removed_later = cleanup_search_runs(
        repo_paths, now=FIXED_NOW + 2 * SEARCH_RUN_TTL + timedelta(seconds=2),
    )
    assert PurePosixPath(".brain/search-runs") / first.run_id in removed_later
    assert not run_dir.exists()
    assert unrelated.is_dir()


def test_spool_budget_exhaustion_is_explicit_incomplete_coverage_not_truncation(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 10, matching_numbers=set(range(10)))
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    result = search_active_sources(
        repo_paths, ledger,
        SearchRequest("sources", "discovery", ("Alpha",), 1, page_size=2, max_run_bytes=256),
    )
    assert result.complete is False
    assert result.next_cursor is None
    assert [gap.code for gap in result.coverage_gaps] == ["search_spool_limit"]
    with pytest.raises(ValueError, match="complete"):
        completed_search_pass((result,))


def test_zero_hit_run_is_complete_and_has_one_empty_page(
    repo_paths: RepoPaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = make_active_ledger(repo_paths, 3, matching_numbers=set())
    monkeypatch.setattr(subprocess, "Popen", ContentAwareRgRecorder())
    result = search_active_sources(
        repo_paths, ledger, SearchRequest("sources", "verification", ("absent",), 1),
    )
    assert result.complete is True
    assert result.candidate_count == 0
    assert result.matches == ()
    assert completed_search_pass((result,)).pages[0].match_count == 0
```

```python
# tests/integration/test_brain_search.py
def test_brain_search_json_requires_a_pass_for_source_scope(repo_root: Path) -> None:
    completed = run_brain(repo_root, "--json", "search", "--scope", "sources", "--term", "Alpha")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["command"] == "search"
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_arguments"


def test_brain_search_json_continuations_reach_late_real_rg_evidence(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("search/valid")
    completed = run_brain(
        scenario.root, "--json", "search", "--scope", "sources", "--pass", "discovery",
        "--term", "Alpha", "--page-size", "1",
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["ok"] is True
    pages = [payload["data"]]
    while pages[-1]["next_cursor"] is not None:
        resumed = run_brain(scenario.root, "--json", "search", "--cursor", pages[-1]["next_cursor"])
        assert resumed.returncode == 0 and resumed.stderr == ""
        pages.append(json.loads(resumed.stdout)["data"])
    assert pages[-1]["complete"] is True
    assert all(len(page["matches"]) <= 1 for page in pages)
    returned_paths = [match["path"] for page in pages for match in page["matches"]]
    assert any(path.startswith("sources/extracted/zzz-late.txt/") for path in returned_paths)
    assert any(path.startswith("sources/extracted/unusual/résumé (final).md/") for path in returned_paths)


def test_brain_search_missing_rg_returns_canonical_json_error(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("search/valid")
    def missing_rg(*args: object, **kwargs: object) -> NoReturn:
        raise FileNotFoundError("rg")
    monkeypatch.setattr(subprocess, "Popen", missing_rg)
    completed = run_brain(
        scenario.root, "--json", "search", "--scope", "sources",
        "--pass", "discovery", "--term", "Alpha",
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["command"] == "search"
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "rg_unavailable"


def test_search_run_directory_is_gitignored(repo_root: Path) -> None:
    completed = subprocess.run(
        ["git", "check-ignore", ".brain/search-runs/probe"], cwd=repo_root,
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py -v`

Expected: FAIL because search runs, resumable cursors, and the `./brain search` parser are absent.

- [ ] **Step 3: Implement argument-array search and structured output**

```python
# brainlib/search.py
SearchScope = Literal["wiki", "sources"]
SearchPhase = Literal["filenames", "context"]
SearchMode = Literal["research", "freshness"]

class SearchError(RuntimeError):
    pass

class SearchArgumentLimitError(SearchError):
    pass

class SearchOperandError(SearchError):
    pass

class SearchOutputLimitError(SearchError):
    pass

class SearchExecutionError(SearchError):
    pass

class InvalidSearchCursor(SearchError):
    pass

class SearchRunBlocked(SearchError):
    diagnostic: Diagnostic

class SearchRunStale(SearchRunBlocked):
    pass

class SearchRunExpired(SearchRunBlocked):
    pass

@dataclass(frozen=True)
class SearchRequest:
    scope: SearchScope
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    context_lines: int
    page_size: int = 100
    max_run_bytes: int = 1_073_741_824
    freshness_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(any(character in term for character in ("\\r", "\\n", "\\x00")) for term in self.terms):
            raise ValueError("search terms cannot contain CR, LF, or NUL")
        if self.freshness_source_ids and self.pass_name is not None:
            raise ValueError("freshness searches cannot set a research pass")

@dataclass(frozen=True)
class SearchMatch:
    path: PurePosixPath
    line_number: int
    text: str
    kind: Literal["match", "context"]

@dataclass(frozen=True)
class SearchResult:
    run_id: str
    corpus_revision: str
    scope: SearchScope
    mode: SearchMode
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    page_index: int
    request_cursor: str | None
    next_cursor: str | None
    complete: bool
    candidate_count: int
    candidate_manifest: PurePosixPath
    candidate_manifest_sha256: str
    result_sha256: str
    matches: tuple[SearchMatch, ...]
    searched_source_ids: tuple[str, ...]
    coverage_gaps: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class SearchRunProof:
    run_id: str
    corpus_revision: str
    scope: SearchScope
    mode: SearchMode
    pass_name: SearchPassName | None
    terms: tuple[str, ...]
    candidate_count: int
    candidate_manifest_sha256: str
    page_count: int
    match_count: int

MAX_RG_PATHS = 256
MAX_RG_ARGV_BYTES = 131_072
MAX_RG_EVENT_BYTES = 65_536
MAX_PAGE_SIZE = 500
SEARCH_RUN_TTL = timedelta(hours=24)

def batch_paths(paths: tuple[Path, ...], *, fixed_argv: tuple[str, ...]) -> tuple[tuple[Path, ...], ...]: ...
def build_rg_argv(request: SearchRequest, *, phase: SearchPhase, pattern_file: Path, paths: tuple[Path, ...] = ()) -> list[str]: ...
def search_active_sources(
    paths: RepoPaths, ledger: LedgerStore, request: SearchRequest, *, now: datetime | None = None,
) -> SearchResult: ...
def search_wiki(
    paths: RepoPaths, ledger: LedgerStore, request: SearchRequest, *, now: datetime | None = None,
) -> SearchResult: ...
def resume_search(
    paths: RepoPaths, ledger: LedgerStore, cursor: str, *, now: datetime | None = None,
) -> SearchResult: ...
def complete_search_run(pages: Sequence[SearchResult]) -> SearchRunProof: ...
def verify_search_run_proof(
    paths: RepoPaths, ledger: LedgerStore, proof: SearchRunProof, *, now: datetime | None = None,
) -> None: ...
def completed_search_pass(pages: Sequence[SearchResult]) -> SearchPassRecord: ...
def cleanup_search_runs(
    paths: RepoPaths, *, now: datetime | None = None, retention: timedelta = SEARCH_RUN_TTL,
) -> tuple[PurePosixPath, ...]: ...
```

```python
# tests/helpers_knowledge.py
class FakeRgProcess:
    def __init__(self, stdout: str, *, on_wait: Callable[[], None]) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")
        self.returncode = 0
        self._on_wait = on_wait

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self) -> int:
        self._on_wait()
        return self.returncode

class FakeLedger:
    def __init__(self, representations: tuple[SourceRepresentation, ...]) -> None:
        self._representations = representations

    def active_representations(self) -> tuple[SourceRepresentation, ...]:
        return self._representations

    def load_all(self) -> Mapping[str, "FakeRevisionRecord"]:
        return {
            item.source_id: FakeRevisionRecord(
                item.source_id, item.content_sha256, item.derivation_id,
            )
            for item in self._representations
        }

@dataclass(frozen=True)
class FakeRevisionRecord:
    source_id: str
    active_content_sha256: str
    active_derivation_id: str

def make_source_representation(
    paths: RepoPaths, number: int, *, extracted_path: PurePosixPath | None = None,
    matches: bool = True,
) -> SourceRepresentation:
    digest = f"{number:064x}"
    representation = SourceRepresentation(
        source_id="src_" + digest,
        content_sha256=digest,
        derivation_id="drv_" + digest,
        raw_path=PurePosixPath(f"search/{number}.txt"),
        extracted_path=extracted_path or PurePosixPath(f"sources/extracted/search/{number}.txt/{digest}/drv_{digest}.md"),
        output_sha256=digest,
        quality_state="ok",
        anchors=(),
    )
    destination = paths.root / representation.extracted_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("Alpha exception\n" if matches else "unrelated\n", encoding="utf-8")
    return representation

def make_active_ledger(
    paths: RepoPaths, count: int, *, matching_numbers: set[int],
) -> FakeLedger:
    return FakeLedger(tuple(
        make_source_representation(paths, number, matches=number in matching_numbers)
        for number in range(count)
    ))

def drain_search(paths: RepoPaths, ledger: LedgerStore, first: SearchResult) -> tuple[SearchResult, ...]:
    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(resume_search(paths, ledger, pages[-1].next_cursor))
    return tuple(pages)

class ContentAwareRgRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.overlap = False
        self._running = False

    def __call__(self, argv: list[str], **kwargs: object) -> FakeRgProcess:
        self.overlap = self.overlap or self._running
        self._running = True
        self.calls.append(argv)
        operands = argv[argv.index("--") + 1:]
        matching = [operand for operand in operands if "Alpha" in Path(operand).read_text(encoding="utf-8")]
        if "--files-with-matches" in argv:
            stdout = "".join(f"{operand}\0" for operand in matching)
        else:
            stdout = "".join(
                json.dumps({
                    "type": "match",
                    "data": {
                        "path": {"text": operand},
                        "lines": {"text": "Alpha\n"},
                        "line_number": 1,
                        "submatches": [],
                    },
                }, sort_keys=True) + "\n"
                for operand in matching
            )
        return FakeRgProcess(stdout, on_wait=lambda: setattr(self, "_running", False))

# tests/conftest.py
@pytest.fixture
def streaming_rg_recorder() -> ContentAwareRgRecorder:
    return ContentAwareRgRecorder()

@pytest.fixture
def one_active_ledger(active_ledger_factory: Callable[[int], LedgerStore]) -> LedgerStore:
    return active_ledger_factory(1)

@pytest.fixture
def active_ledger_factory(repo_paths: RepoPaths) -> Callable[[int], LedgerStore]:
    def build(count: int) -> LedgerStore:
        return FakeLedger(tuple(make_source_representation(repo_paths, number) for number in range(count)))
    return build
```

`FakeRgProcess`, `FakeLedger`, `ContentAwareRgRecorder`, `make_source_representation`, `make_active_ledger`, and `drain_search` live in `tests/helpers_knowledge.py`; `tests/conftest.py` imports only the test doubles it exposes as fixtures. Builders materialize every advertised extracted operand because production search rejects missing or non-regular active paths before spawning `rg`. The real-rg scenario has multiple matching files, including a UTF-8/space name and `zzz-late.txt`, so pagination—not fixture order—must reach the late evidence.

Complete `SearchRequest.__post_init__` by requiring at least one ordered-unique nonempty term, `0 <= context_lines <= 20`, `1 <= page_size <= 500`, `max_run_bytes >= 256`, source scope for freshness mode, sorted unique valid full source IDs, and exactly one named pass for non-freshness source research; wiki scope forbids both pass and freshness IDs. A zero-hit result is valid; empty terms are not. Resolve source operands from exact active representations, require present regular nonsymlink files beneath `paths.extracted`, and pass only those absolute operands to `rg`. Source research records all active source IDs in sorted order; freshness records exactly its requested IDs; wiki results record `searched_source_ids=()`. Wiki search now accepts `LedgerStore`, binds the source corpus revision too, and enumerates only sorted direct regular nonsymlink `*.md` children of `wiki/pages` and `wiki/questions`.

`batch_paths` counts the `os.fsencode()` byte length plus one NUL separator for every fixed argument and operand and partitions sorted operands into at most 256 paths and 131,072 argv bytes; one oversized operand raises `SearchArgumentLimitError`. Write the validated terms one per line to a mode-`0o600` private temporary pattern file and unlink it before returning or raising; it is not retained in the run directory. The filename phase is exactly `("rg", "--no-config", "--sort", "path", "--fixed-strings", "--files-with-matches", "--null", "--file", str(pattern_file), "--", *batch)`. It runs for **every** operand batch before any result page is returned. Stream NUL-delimited names, validate operand membership, sort only the at-most-256 results for that batch, and append globally ordered unique repo-relative JSON records to `.brain/search-runs/<run-id>/candidates.jsonl`; never accumulate the complete candidate set in memory. Zero bound operands invoke no process and still produce authenticated empty manifests plus one complete empty page.

The context phase is exactly `("rg", "--no-config", "--sort", "path", "--json", "--fixed-strings", "--line-number", "--context", str(request.context_lines), "--file", str(pattern_file), "--", *candidate_batch)`. Stream normalized `match` and `context` records to `results.jsonl`, supporting ripgrep `text` and base64 `bytes` values; base64 must be canonical and decode as strict UTF-8, otherwise block the run. Reject malformed events and noncandidate paths. While streaming, write `page-index.jsonl` entries containing page index, byte start/end, record count, and SHA-256 of each canonical page payload. The command returns at most `page_size` records; continuation reads and verifies only the indexed page range. A filename token or JSON event over 65,536 bytes is a blocking run failure. Open stderr in a private temporary file, use argument-array `Popen(..., shell=False)`, accept exit 1 as no hits, bound read stderr to 16 KiB, wait before each next process, and never use `capture_output=True` for `rg`.

`brainlib/search_runs.py` owns state. Create `.brain/search-runs/<run-id>/` mode `0o700` and regular nonsymlink files mode `0o600`: `metadata.json`, random 256-bit `secret`, `candidates.jsonl`, `results.jsonl`, and `page-index.jsonl`. `run_id` is `srch_` plus 32 random lowercase hex characters. Metadata binds repository identity, creation/expiry, corpus revision, scope/mode/pass, exact ordered terms, context, source IDs, an operand-identity digest, both manifest hashes/counts, page count, blocked diagnostic, and highest page served; publish metadata atomically and fsync. Source operand identity rows are `(source_id, content_sha256, derivation_id, extracted_path, output_sha256)`; wiki rows are `(repo_relative_path, byte_size, mtime_ns, sha256)`. Initial and resume calls acquire the source lock, plus the wiki lock for wiki scope, and recompute the revision/operand digest before serving anything.

An opaque URL-safe cursor encodes only version, run ID, next page index, expiry, and an HMAC-SHA256 made with the run secret. Resume rejects malformed/base64-invalid/unknown-version/wrong-MAC/out-of-range cursors as `InvalidSearchCursor`; an expired valid cursor raises `SearchRunExpired`, and a changed corpus revision, operand identity, repository, or run manifest raises `SearchRunStale`. Both blocked exceptions carry a canonical diagnostic and atomically mark the run incomplete. Healthy nonfinal pages have `complete=False`, a `next_cursor`, and no coverage gap. The final page—including the one empty page for zero hits—has `complete=True` and `next_cursor=None`. Enforce `max_run_bytes` over the combined canonical bytes of `candidates.jsonl`, `results.jsonl`, and `page-index.jsonl`; a breach stops safely, marks the run incomplete with `search_spool_limit`, returns no cursor, and exits validation-style code 1. It never reports truncation as success.

`complete_search_run` accepts all returned pages of any one run and proves page indexes start at zero and are contiguous, each `request_cursor` equals the prior `next_cursor`, immutable binding fields and manifest checksum agree, page payload checksums/counts match, the final page alone is complete with no cursor/gap, and terms are nonempty. It returns the exact `SearchRunProof`. `verify_search_run_proof` acquires the source lock and, for wiki scope, then the wiki lock; it verifies the proof against authenticated retained run metadata, the current corpus revision and operand-identity digest, the candidate/result manifest hashes, page and match counts, expiry, and `highest_page_served == final_page`. It raises the same stale/expired/blocked exceptions and never accepts proof fields alone. `completed_search_pass` calls `complete_search_run`, additionally requires `scope="sources"`, `mode="research"`, and a named pass, and converts page metadata into one `SearchPassRecord`. These helpers reject missing pages, expired/stale/resource-limited runs, and an undrained last cursor. Therefore three logical research passes remain exactly three even if each has thousands of CLI pages. A caller must also complete and verify a freshness or wiki run before judging sufficiency, but freshness never enters `EvidencePacket.passes`.

`cleanup_search_runs` examines only non-symlink `srch_<32hex>` children beneath the resolved search-run root and takes the relevant locks. An expired undrained run is first atomically marked with `search_run_expired` and retained for one further retention window so its authenticated cursor produces that explicit blocking gap; completed/already-blocked runs older than the horizon are removed from their validated exact directory. It never follows links or removes an unknown directory. Run cleanup occurs at command start and after evidence handoff; `.brain/` is already ignored by the template, and the integration test proves `.brain/search-runs/**` cannot enter Git.

The CLI syntax is:

```text
./brain --json search --scope wiki --term Alpha --term "2026-09-04"
./brain --json search --scope sources --pass discovery --term Alpha --term "A. Example" --context 3 --page-size 100 --max-run-bytes 1073741824
./brain --json search --scope sources --freshness --source-id src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --term Alpha
./brain --json search --cursor OPAQUE_TOKEN
```

Start mode requires the existing scope/pass/freshness rules plus terms and accepts bounded `--context`, `--page-size`, and `--max-run-bytes`; continuation mode requires only `--cursor` and rejects every start flag. Root-first JSON always emits `CommandResult`. Data is the exact `SearchResult` shape with bounded `matches`, repo-relative `candidate_manifest`, opaque `next_cursor`, `complete`, and coverage gaps; it never returns a complete candidate array. A healthy nonfinal page is exit 0/`ok=true` and its cursor is mandatory; only `complete_search_run`/handoff rejects stopping there. Invalid/tampered cursors and arguments return code 2; stale, expired, or spool-limited runs are code 1 blocking gaps; unavailable/failed `rg` is code 2 and leaves a blocked run record when creation began. Human output prints run/page, candidate count, returned match count, and either the continuation instruction, `complete`, or the blocking gap. Neither CLI nor library creates semantic terms or decides relevance.

- [ ] **Step 4: Run focused tests and the CLI fixture**

Run: `python3 -m pytest tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py -v && ./brain --json search --scope wiki --term Alpha`

Expected: PASS; all batches are scanned, pages remain bounded, continuations reach late evidence, zero hits complete honestly, and stale/tampered/incomplete runs fail closed.

- [ ] **Step 5: Review the search checkpoint without committing**

```bash
git diff --check
git diff -- brain brainlib/search.py brainlib/search_runs.py brainlib/cli.py tests/helpers_knowledge.py tests/conftest.py tests/unit/test_search.py tests/unit/test_search_runs.py tests/integration/test_brain_search.py tests/fixtures/wiki/scenarios/search
```

### Task 3: Define and validate immutable claim-level citations

**Files:**
- Create: `brainlib/citations.py`
- Create: `docs/brain/schemas/citation.md`
- Create: `tests/unit/test_citations.py`
- Modify: `tests/helpers_knowledge.py`
- Create: `tests/fixtures/wiki/scenarios/citations/current/repo/wiki/pages/current.md`
- Create: `tests/fixtures/wiki/scenarios/citations/historical/repo/wiki/pages/historical.md`
- Create: `tests/fixtures/wiki/scenarios/citations/adoption/repo/wiki/pages/adoption.md`
- Create: `tests/fixtures/wiki/scenarios/citations/missing-definition/repo/wiki/pages/broken.md`
- Create: `tests/fixtures/wiki/scenarios/citations/code-example/repo/wiki/pages/code-example.md`
- Create: `tests/fixtures/wiki/scenarios/citations/encoded-path/repo/wiki/pages/encoded.md`
- Create: `tests/fixtures/wiki/scenarios/citations/adoption/prior-original.txt`

**Interfaces:**
- Consumes: `scan_markdown`, canonical `SourceRepresentation`, `CitationRewrite`, `LedgerStore.find_representation()`, canonical `Anchor`, `ChecksumCache`, and `ValidationReport`.
- Produces: `Citation`, `MarkdownDocuments`, canonical Markdown-path encoding/decoding, `read_markdown_documents`, `parse_citation_definitions`, `rewrite_historical_original_links`, and metadata-fast/full-cached `validate_citations`; Tasks 4–6 consume the same staged-document view and compose its report into graph/CLI validation.

- [ ] **Step 1: Write failing tests for claim mapping and historical raw bytes**

````python
# tests/unit/test_citations.py
def test_validate_citation_accepts_materialized_original_when_current_path_changed(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/historical")
    report = validate_citations(scenario.paths, scenario.ledger, (scenario.paths.wiki_pages / "historical.md",))
    assert report.ok


def test_validate_citation_rejects_factual_marker_without_a_resolving_definition(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/missing-definition")
    report = validate_citations(scenario.paths, scenario.ledger, (scenario.paths.wiki_pages / "broken.md",))
    assert any(issue.code == "citation_definition_missing" for issue in report.issues)


def test_validate_citation_ignores_marker_shaped_text_in_code_block(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/code-example")
    report = validate_citations(scenario.paths, scenario.ledger, (scenario.paths.wiki_pages / "code-example.md",))
    assert not any(issue.code == "citation_definition_missing" for issue in report.issues)


def test_rewrite_historical_original_link_changes_only_matching_citation_definition(repo_paths: RepoPaths) -> None:
    markdown = """A prose mention of sources/raw/notes/a.txt stays unchanged.
```text
[original](../../sources/raw/notes/a.txt)
```
[^cite-a]: source_id: `src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`; content_sha256: `cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`; derivation_id: `drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`; anchor: `page:2`; [original](../../sources/raw/notes/a.txt); [extracted](../../sources/extracted/notes/a.txt/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.md#page:2)
"""
    rewritten = rewrite_historical_original_links(
        markdown,
        (CitationRewrite("src_" + "a" * 64, "c" * 64, PurePosixPath("_versions/src_" + "a" * 64 + "/" + "c" * 64 + "/a.txt")),),
        paths=repo_paths,
        document_path=repo_paths.wiki_pages / "a.md",
    )
    assert "A prose mention of sources/raw/notes/a.txt stays unchanged." in rewritten
    assert "```text\n[original](../../sources/raw/notes/a.txt)\n```" in rewritten
    assert "../../sources/raw/_versions/src_" in rewritten


def test_real_adoption_rewrite_preserves_other_versions_and_then_validates(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/adoption")
    prior = (scenario.root / "tests/fixtures/wiki/scenarios/citations/adoption/prior-original.txt").read_bytes()
    adoption = adopt_changed_scenario_source(scenario, prior, approval_note="User approved replacement in this conversation")
    before = (scenario.paths.wiki_pages / "adoption.md").read_text(encoding="utf-8")
    rewritten = rewrite_historical_original_links(
        before, adoption.citation_rewrites, paths=scenario.paths,
        document_path=scenario.paths.wiki_pages / "adoption.md",
    )
    assert rewritten.count("sources/raw/_versions/") == 1
    assert "[^same-source-other-version]" in rewritten
    assert "```text\n[original](../../sources/raw/notes/a.txt)\n```" in rewritten
    cache = ChecksumCache()
    cache.begin_transaction()
    report = validate_citations(
        scenario.paths, scenario.ledger,
        {scenario.paths.wiki_pages / "adoption.md": rewritten}, full=True, checksum_cache=cache,
    )
    assert report.ok


def test_encoded_destinations_round_trip_and_malformed_escape_is_rejected(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/encoded-path")
    page = scenario.paths.wiki_pages / "encoded.md"
    markdown = page.read_text(encoding="utf-8")
    assert "R%C3%A9sum%C3%A9%20%28100%25%20%231%29.txt" in markdown
    assert validate_citations(scenario.paths, scenario.ledger, (page,)).ok
    malformed = markdown.replace("%28", "%2G", 1)
    report = validate_citations(scenario.paths, scenario.ledger, {page: malformed})
    assert any(issue.code == "citation_destination_noncanonical" for issue in report.issues)


@pytest.mark.parametrize(
    "destination",
    (
        "../../sources/raw/%2G.txt",                 # malformed escape
        "../../sources/raw/%2fetc.txt",             # lowercase and encoded separator
        "../../sources/raw/%00.txt",                 # NUL
        "../../sources/raw/%41.txt",                 # nonminimal escape for A
        "../../sources/raw/%2E/alias.txt",           # encoded dot component
        "../../sources/raw/./alias.txt",             # gratuitous dot component
        "../../sources/raw/folder/../alias.txt",     # canceling traversal
        "https://example.test/evidence.txt",          # scheme/authority
        "../../../../outside.txt",                    # repository escape
    ),
)
def test_resolve_markdown_path_strictly_rejects_noncanonical_or_escaping_destinations(
    repo_paths: RepoPaths, destination: str
) -> None:
    with pytest.raises(CitationPathError):
        resolve_markdown_path(repo_paths, repo_paths.wiki_pages / "encoded.md", destination)


def test_normal_citation_validation_never_hashes_and_full_mode_hashes_each_path_once(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    page = scenario.paths.wiki_pages / "current.md"
    calls: list[Path] = []
    real_sha256 = compute_sha256
    cache = ChecksumCache(hash_file=lambda path: calls.append(path.resolve()) or real_sha256(path))
    assert validate_citations(scenario.paths, scenario.ledger, (page,), full=False, checksum_cache=cache).ok
    assert calls == []
    cache.begin_transaction()
    assert validate_citations(scenario.paths, scenario.ledger, (page,), full=True, checksum_cache=cache).ok
    assert len(calls) == len(set(calls)) == 2
````

- [ ] **Step 2: Run the citation tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_citations.py -v`

Expected: FAIL because the citation parser and immutable-version validator do not exist.

- [ ] **Step 3: Implement the normative grammar and deterministic validation**

```python
# tests/helpers_knowledge.py
class ScenarioHistoricalResolver:
    def __init__(self, source_id: str, content_sha256: str, content: bytes) -> None:
        self.expected = (source_id, content_sha256)
        self.content = content

    def read_exact(self, source_id: str, raw_path: PurePosixPath, sha256: str) -> bytes | None:
        return self.content if (source_id, sha256) == self.expected and hashlib.sha256(self.content).hexdigest() == sha256 else None

def adopt_changed_scenario_source(
    scenario: KnowledgeScenario, prior: bytes, *, approval_note: str
) -> VersionAdoption:
    records = scenario.ledger.load_all()
    integrity_records = [record for record in records.values() if record.state is SourceState.INTEGRITY_ERROR]
    if len(integrity_records) != 1:
        raise AssertionError("adoption scenario must contain exactly one integrity-error record")
    record = integrity_records[0]
    candidate_path = scenario.paths.raw / record.current_raw_path
    candidate_stat = candidate_path.stat()
    candidate = InventoryItem(
        FileFingerprint(record.current_raw_path, candidate_stat.st_size, candidate_stat.st_mtime_ns),
        record.media_type, candidate_path.suffix.lower(), compute_sha256(candidate_path),
    )
    old_sha256 = record.active_content_sha256
    if old_sha256 is None:
        raise AssertionError("adoption scenario has no active content version")
    adoption = adopt_version(
        record, candidate, paths=scenario.paths,
        resolver=ScenarioHistoricalResolver(record.source_id, old_sha256, prior),
        approval_note=approval_note, now=FIXED_NOW,
    )
    scenario.ledger.save(adoption.record)
    scenario.ledger.write_summary(scenario.ledger.load_all().values(), generated_at=FIXED_NOW)
    return adoption
```

````markdown
<!-- docs/brain/schemas/citation.md -->
# Citation grammar

Put one or more citation markers immediately after every factual claim. Define
each marker exactly once under the document's final `## Sources` heading. A
source list without adjacent claim markers does not make factual prose cited.

For a direct child of `wiki/pages/` or `wiki/questions/`, the exact form is:

```text
A factual claim.[^cite-alpha-page-2]

## Sources

[^cite-alpha-page-2]: source_id: `src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`; content_sha256: `cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`; derivation_id: `drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`; anchor: `page:2`; [original](../../sources/raw/_versions/src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/a.pdf); [extracted](../../sources/extracted/a.pdf/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.md#page:2)
```
````

```python
# brainlib/citations.py
MarkdownDocuments: TypeAlias = Mapping[Path, str]

class CitationPathError(ValueError):
    pass

@dataclass(frozen=True)
class Citation:
    citation_id: str
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor
    original_destination: str
    extracted_destination: str
    definition_line: int

def parse_citation_definitions(markdown: str, *, path: Path) -> tuple[Citation, ...]: ...
def read_markdown_documents(documents: Iterable[Path]) -> dict[Path, str]: ...
def encode_markdown_path(document_path: Path, target_path: Path) -> str: ...
def resolve_markdown_path(paths: RepoPaths, document_path: Path, destination: str) -> Path: ...
def canonicalize_citation_destinations(
    markdown: str, ledger: LedgerStore, *, paths: RepoPaths, document_path: Path,
) -> str: ...
def rewrite_historical_original_links(
    markdown: str, rewrites: tuple[CitationRewrite, ...], *, paths: RepoPaths, document_path: Path
) -> str: ...
def validate_citations(
    paths: RepoPaths, ledger: LedgerStore,
    documents: MarkdownDocuments | Iterable[Path],
    *, full: bool = False, checksum_cache: ChecksumCache | None = None,
) -> ValidationReport: ...
```

Require every citation marker outside code to have exactly one definition in that document and every definition to appear under the final `Sources` heading. Parse each semicolon-delimited identity field exactly once. Require `source_id` to match `src_` plus 64 lowercase hex characters, `derivation_id` to match `drv_` plus 64 lowercase hex characters, `content_sha256` to be 64 lowercase hex characters, and anchor text to parse exactly as `<kind>:<value>` into the canonical `Anchor`; require the extracted fragment to equal that serialization, such as `#page:2`.

`encode_markdown_path` computes the minimal relative POSIX path and percent-encodes each UTF-8 component with uppercase escapes; `/` separators remain literal, while spaces, `(`, `)`, `#`, `%`, control characters, and non-ASCII bytes are encoded. `resolve_markdown_path` rejects schemes, authorities, query strings, backslashes, malformed/lowercase/nonminimal escapes, encoded `/` or `\\`, NUL, and absolute paths. It allows only the exact minimal leading `..` sequence that `encode_markdown_path(document_path, resolved_target)` requires; it rejects percent-encoded dot components, literal `.` components, and any extra, embedded, or canceling `..` traversal. Decode exactly once, resolve below `paths.root`, re-encode the resolved target, and require byte-for-byte equality with the supplied path. Fragments are parsed separately and are forbidden on original links. This supplies one canonical grammar for arbitrary valid filenames rather than relying on ambiguous bare Markdown destinations.

At entry, normalize an iterable with `read_markdown_documents`; mapping keys are absolute logical repository paths and mapping values are the staged authoritative text. Resolve the exact retained representation with all three IDs. The only original destination is `paths.raw / representation.raw_path`; the only derivation destination is `paths.root / representation.extracted_path`. Require the two decoded destinations to equal those paths, the fragment to equal the canonical anchor, and the anchor to occur in `representation.anchors`.

All filesystem proof goes through the namespace-aware cache, never through a resolved-path shortcut. Map a user original to `SnapshotNamespace.RAW_USER`, `_versions/...` to `RAW_VERSION`, `_web/...` to `RAW_WEB`, and the repository-relative derivation path to `EXTRACTED`. Call `cache.observe(paths, namespace, logical_path, full=full)` and compare the returned size/mtime plus SHA-256 in full mode. A standalone validator creates a cache and begins its own transaction; a supplied cache is caller-owned and must not be reset. Repeated citations therefore reuse the checksum only for the same `(namespace, logical_path, stable_identity)` while still revalidating identity on each reference. A namespace-owner mismatch or any between-reference replacement poisons the key and fails closed.

Emit distinct codes for missing/duplicate/out-of-section definitions, invalid full IDs or anchor, missing source/version/derivation/anchor/files, stale adopted target, noncanonical destination, target escape, original/extracted target mismatch, extracted-fragment mismatch, and full-mode checksum mismatch. In particular, a wrong existing same-byte file still produces `citation_original_target_mismatch` or `citation_extracted_target_mismatch`; it is never accepted because its checksum happens to match.

`canonicalize_citation_destinations` parses definitions with the Markdown-aware scanner, resolves each exact source/version/derivation through `LedgerStore.find_representation`, and replaces only that definition's original and extracted destinations with `encode_markdown_path(document_path, paths.raw / representation.raw_path)` and `encode_markdown_path(document_path, paths.root / representation.extracted_path)` plus the canonical anchor fragment. An unresolved identity is left unchanged so normal validation emits its precise missing-record/version/derivation issue. This idempotent operation never changes prose, code, markers, or identity fields and is safe to run across every document on every wiki transaction. It is the durability fallback when a process loses a sync/adoption report after the ledger checkpoint.

`rewrite_historical_original_links` accepts only a canonical sorted `tuple[CitationRewrite, ...]`, rejects duplicate/conflicting `(source_id, content_sha256)` keys, and replaces only a matching definition's original destination with `encode_markdown_path(document_path, paths.raw / rewrite.raw_path)`. It never changes prose, code, markers, extracted links, another source, or another version. The tuple comes either from the exact `VersionAdoption.citation_rewrites` result or by streaming every verified `citation_rewrite` event from `SyncResultStore.iter_events(reference)`; `SyncReport.citation_rewrites` is only a bounded display sample. Transaction correctness still uses the ledger-driven canonicalizer as the durable fallback. Consumers drain the exact event set, durably deduplicate/apply its `result_id`, and explicitly acknowledge only afterward.

Compute `current_revision = compute_corpus_revision(ledger.load_all().values())` once and return `ValidationReport(checks=("citations",), issues=tuple(issues), corpus_revision=current_revision)`. It never turns a warning or coverage diagnostic into successful claim support.

The schema document must state that factual prose needs one or more marker references adjacent to its claim, that a final `Sources` section contains all definitions, and that a source list alone does not make uncited factual prose valid.

- [ ] **Step 4: Run focused citation tests**

Run: `python3 -m pytest tests/unit/test_citations.py -v`

Expected: PASS, including a citation that resolves through `_versions` after a source replacement.

- [ ] **Step 5: Review the citation checkpoint without committing**

```bash
git diff --check
git diff -- brainlib/citations.py docs/brain/schemas/citation.md tests/helpers_knowledge.py tests/unit/test_citations.py tests/fixtures/wiki/scenarios/citations
```

### Task 4: Build graph validation and the only wiki-write transaction

**Files:**
- Create: `brainlib/graph.py`
- Create: `brainlib/wiki_transaction.py`
- Modify: `tests/helpers_knowledge.py`
- Create: `tests/unit/test_graph.py`
- Create: `tests/unit/test_wiki_transaction.py`
- Create: `tests/fixtures/wiki/scenarios/graph/candidates/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/graph/candidates/repo/wiki/pages/candidate-000.md` through `candidate-299.md`
- Create: `tests/fixtures/wiki/scenarios/graph/candidates/repo/wiki/pages/zzz-related.md`
- Create: `tests/fixtures/wiki/scenarios/graph/nonreciprocal/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/graph/unlinked/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/graph/valid/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/graph/encoded/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/graph/encoded/repo/wiki/pages/béta topic.md`
- Create: `tests/fixtures/wiki/scenarios/removal/approved/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/removal/approved/repo/wiki/pages/zzz-inbound.md`
- Create: `tests/fixtures/wiki/scenarios/removal/approved/beta-after-removal.md`
- Create: `tests/fixtures/wiki/scenarios/removal/approved/zzz-inbound-after-removal.md`
- Create: `tests/fixtures/wiki/scenarios/transaction/interrupted/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/transaction/interrupted/repo/wiki/pages/beta.md`
- Modify: `wiki/index.md`

**Interfaces:**
- Consumes: parsed `WikiPage`/`QuestionRecord`, `scan_markdown`, the Task 2 search-run/cursor store, `CitationRewrite`, source/wiki locks, corpus revision, citation validation, and validator types.
- Produces: paged `find_link_candidates`/`resume_link_candidates`, `complete_link_candidate_run`, `LinkCandidateRunProof`, `render_wiki_index`, `validate_graph`, `WikiManifest`, `load_wiki_manifest`, `validate_wiki_transaction_state`, `apply_wiki_manifest`, and `recover_wiki_transaction`. No other API may publish a wiki file or index.

- [ ] **Step 1: Write failing graph and transaction tests**

```python
# tests/unit/test_graph.py
def test_find_link_candidates_searches_pages_and_questions_but_excludes_self(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    first = find_link_candidates(
        scenario.paths, scenario.ledger,
        page_path=scenario.paths.wiki_pages / "alpha.md", terms=("Alpha", "A. Example"),
        page_size=1,
    )
    with pytest.raises(ValueError, match="drain"):
        complete_link_candidate_run((first,))
    pages = drain_link_candidates(scenario.paths, scenario.ledger, first)
    candidates = tuple(candidate for page in pages for candidate in page.candidates)
    assert pages[-1].complete is True and pages[-1].next_cursor is None
    assert {candidate.path.name for candidate in candidates} == {
        "beta.md", "what-is-alpha.md", "zzz-related.md",
    }
    assert all(candidate.path.name != "alpha.md" for candidate in candidates)
    proof = complete_link_candidate_run(pages)
    assert proof.page_count == len(pages)
    assert proof.candidate_count == 3


def test_link_candidate_cursor_tamper_and_live_wiki_change_fail_closed(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    first = find_link_candidates(
        scenario.paths, scenario.ledger,
        page_path=scenario.paths.wiki_pages / "alpha.md", terms=("Alpha",), page_size=1,
    )
    assert first.next_cursor is not None
    tampered = first.next_cursor[:-1] + ("A" if first.next_cursor[-1] != "A" else "B")
    with pytest.raises(InvalidSearchCursor):
        resume_link_candidates(scenario.paths, scenario.ledger, tampered)
    late = scenario.paths.wiki_pages / "zzz-related.md"
    late.write_text(late.read_text(encoding="utf-8") + "\nconcurrent change\n", encoding="utf-8")
    with pytest.raises(SearchRunStale):
        resume_link_candidates(scenario.paths, scenario.ledger, first.next_cursor)


def test_validate_graph_requires_reciprocal_page_question_relationship(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/nonreciprocal")
    assert "relationship_not_reciprocal" in {issue.code for issue in validate_graph(scenario.paths).issues}


def test_validate_graph_reports_unlinked_first_meaningful_occurrence(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/unlinked")
    assert "related_topic_first_occurrence_unlinked" in {issue.code for issue in validate_graph(scenario.paths).issues}


def test_graph_links_use_same_canonical_percent_encoded_path_grammar(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/encoded")
    source = scenario.paths.wiki_pages / "alpha.md"
    target = scenario.paths.wiki_pages / "béta topic.md"
    destination = encode_markdown_path(source, target)
    assert destination == "b%C3%A9ta%20topic.md"
    assert resolve_markdown_path(scenario.paths, source, destination) == target.resolve()
    assert validate_graph(scenario.paths).ok
```

```python
# tests/unit/test_wiki_transaction.py
def test_delete_and_relationship_removal_require_approved_destructive_intent(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("removal/approved")
    delete = WikiChange.delete(PurePosixPath("wiki/pages/alpha.md"))
    with pytest.raises(ApprovalRequired):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, (delete,), intent="delete"))
    content = (scenario.root / "tests/fixtures/wiki/scenarios/removal/approved/beta-after-removal.md").read_text(encoding="utf-8")
    changed_beta = stage_wiki_write(scenario, "wiki/pages/beta.md", content)
    with pytest.raises(ApprovalRequired):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, (changed_beta,), intent="routine"))


def test_approved_removal_reconciles_inbound_record_and_generated_index(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("removal/approved")
    content = (scenario.root / "tests/fixtures/wiki/scenarios/removal/approved/beta-after-removal.md").read_text(encoding="utf-8")
    late_content = (scenario.root / "tests/fixtures/wiki/scenarios/removal/approved/zzz-inbound-after-removal.md").read_text(encoding="utf-8")
    changes = (
        WikiChange.delete(PurePosixPath("wiki/pages/alpha.md")),
        stage_wiki_write(scenario, "wiki/pages/beta.md", content),
        stage_wiki_write(scenario, "wiki/pages/zzz-inbound.md", late_content),
    )
    result = apply_wiki_manifest(
        scenario.paths, scenario.ledger,
        manifest_for(scenario, changes, intent="delete", approval_event_id="approval-removal-2026-09-04"),
    )
    assert result.changed_paths == (
        PurePosixPath("wiki/index.md"), PurePosixPath("wiki/pages/alpha.md"),
        PurePosixPath("wiki/pages/beta.md"), PurePosixPath("wiki/pages/zzz-inbound.md"),
    )
    assert not (scenario.paths.wiki_pages / "alpha.md").exists()
    assert "alpha.md" not in (scenario.paths.root / "wiki/index.md").read_text(encoding="utf-8")


def test_apply_rejects_missing_link_candidate_proof_before_live_write(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md", alpha.read_text(encoding="utf-8") + "\n<!-- staged -->\n",
    )
    manifest = manifest_for(scenario, (change,))
    without_proof = replace(manifest, link_candidate_runs=())
    before = alpha.read_bytes()
    with pytest.raises(LinkCandidateCoverageError) as raised:
        apply_wiki_manifest(scenario.paths, scenario.ledger, without_proof)
    assert raised.value.diagnostic.code == "link_candidate_search_incomplete"
    assert alpha.read_bytes() == before


def test_apply_rejects_stale_or_tampered_retained_link_candidate_proof(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md", alpha.read_text(encoding="utf-8") + "\n<!-- staged -->\n",
    )
    manifest = manifest_for(scenario, (change,))
    late = scenario.paths.wiki_pages / "zzz-related.md"
    late.write_text(late.read_text(encoding="utf-8") + "\nconcurrent edit\n", encoding="utf-8")
    before = alpha.read_bytes()
    with pytest.raises(LinkCandidateCoverageError) as stale:
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest)
    assert stale.value.diagnostic.code == "link_candidate_search_incomplete"
    assert alpha.read_bytes() == before

    fresh = manifest_for(scenario, (change,))
    tampered = replace(
        fresh,
        link_candidate_runs=(replace(fresh.link_candidate_runs[0], candidate_count=999_999),),
    )
    with pytest.raises(LinkCandidateCoverageError):
        apply_wiki_manifest(scenario.paths, scenario.ledger, tampered)
    assert alpha.read_bytes() == before


def test_manifest_repairs_adoption_paths_from_ledger_after_ephemeral_report_is_lost(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/adoption")
    prior = (scenario.root / "tests/fixtures/wiki/scenarios/citations/adoption/prior-original.txt").read_bytes()
    adopt_changed_scenario_source(scenario, prior, approval_note="User approved replacement")  # discard report
    result = apply_wiki_manifest(
        scenario.paths, scenario.ledger,
        manifest_for(scenario, (), citation_rewrites=()),
    )
    rewritten = (scenario.paths.wiki_pages / "adoption.md").read_text(encoding="utf-8")
    assert result.changed_paths == (PurePosixPath("wiki/pages/adoption.md"),)
    assert rewritten.count("sources/raw/_versions/") == 1
    assert validate_citations(scenario.paths, scenario.ledger, (scenario.paths.wiki_pages / "adoption.md",)).ok


def test_stale_corpus_revision_aborts_before_any_live_write(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    change = stage_wiki_write(scenario, "wiki/pages/alpha.md", "changed")
    manifest = replace(manifest_for(scenario, (change,)), expected_corpus_revision="0" * 64)
    before = (scenario.paths.wiki_pages / "alpha.md").read_bytes()
    with pytest.raises(CorpusRevisionChanged):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest)
    assert (scenario.paths.wiki_pages / "alpha.md").read_bytes() == before


def test_interruption_after_first_live_replace_recovers_complete_original_graph(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("transaction/interrupted")
    before = {path: path.read_bytes() for path in sorted((*scenario.paths.wiki_pages.glob("*.md"), scenario.paths.root / "wiki/index.md"))}
    changes = (
        stage_wiki_write(scenario, "wiki/pages/alpha.md", (scenario.paths.wiki_pages / "alpha.md").read_text(encoding="utf-8") + "\n<!-- transaction update -->\n"),
        stage_wiki_write(scenario, "wiki/pages/beta.md", (scenario.paths.wiki_pages / "beta.md").read_text(encoding="utf-8") + "\n<!-- transaction update -->\n"),
    )
    live_replaces = 0

    def interrupting_replace(source: Path, target: Path) -> None:
        nonlocal live_replaces
        if target.is_relative_to(scenario.paths.root / "wiki"):
            live_replaces += 1
            if live_replaces == 2:
                raise OSError("simulated interruption")
        os.replace(source, target)

    with pytest.raises(OSError, match="simulated interruption"):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, changes), replace_file=interrupting_replace)
    assert (scenario.paths.root / ".brain/wiki-transaction.json").is_file()
    recovery = recover_wiki_transaction(scenario.paths)
    assert recovery.recovered is True
    assert {path: path.read_bytes() for path in before} == before


def test_lock_order_is_source_then_wiki_and_manifest_paths_cannot_escape(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    with SourceWriteLock.acquire(scenario.paths.root / ".brain/wiki-write.lock"):
        with pytest.raises(LockHeldError):
            apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, ()))
    escaped = WikiChange.delete(PurePosixPath("wiki/pages/../../AGENTS.md"))
    with pytest.raises(ValueError, match="wiki/pages or wiki/questions"):
        apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, (escaped,), intent="delete", approval_event_id="approved"))


def test_lock_acquisition_order_is_observably_source_then_wiki(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = scenario_repo("graph/valid")
    acquired: list[Path] = []

    @contextmanager
    def recording_lock(path: Path, **_: object) -> Iterator[None]:
        acquired.append(path)
        yield

    monkeypatch.setattr(SourceWriteLock, "acquire", recording_lock)
    apply_wiki_manifest(scenario.paths, scenario.ledger, manifest_for(scenario, ()))
    assert acquired[:2] == [scenario.paths.lock, scenario.paths.root / ".brain/wiki-write.lock"]


def test_manifest_codec_rejects_unknown_fields_and_cross_run_staging(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(scenario, "wiki/pages/alpha.md", alpha.read_text(encoding="utf-8"))
    manifest = manifest_for(scenario, (change,))
    path = write_wiki_manifest(scenario, manifest)
    assert load_wiki_manifest(scenario.paths, path) == manifest
    payload = json.loads(manifest.to_json())
    payload["unknown"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        load_wiki_manifest(scenario.paths, path)
    payload.pop("unknown")
    payload["changes"][0]["staging_path"] = ".brain/wiki-staging/other-run/files/wiki/pages/alpha.md"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="same run"):
        load_wiki_manifest(scenario.paths, path)
```

- [ ] **Step 2: Run graph tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_graph.py tests/unit/test_wiki_transaction.py -v`

Expected: FAIL because graph validation and the manifest transaction do not exist.

- [ ] **Step 3: Implement graph validation and manifest-only publication**

```python
# brainlib/graph.py
@dataclass(frozen=True)
class LinkCandidate:
    path: PurePosixPath
    line: int
    matched_term: str
    context: str
    kind: Literal["page", "question"]

@dataclass(frozen=True)
class LinkCandidateResult:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    page_index: int
    request_cursor: str | None
    next_cursor: str | None
    complete: bool
    candidate_count: int
    candidate_manifest_sha256: str
    result_sha256: str
    candidates: tuple[LinkCandidate, ...]
    coverage_gaps: tuple[Diagnostic, ...]

@dataclass(frozen=True)
class LinkCandidateRunProof:
    run_id: str
    corpus_revision: str
    page_path: PurePosixPath
    terms: tuple[str, ...]
    candidate_manifest_sha256: str
    page_count: int
    candidate_count: int

def find_link_candidates(
    paths: RepoPaths, ledger: LedgerStore, *, page_path: Path, terms: tuple[str, ...],
    page_size: int = 100, max_run_bytes: int = 1_073_741_824,
) -> LinkCandidateResult: ...
def resume_link_candidates(
    paths: RepoPaths, ledger: LedgerStore, cursor: str,
) -> LinkCandidateResult: ...
def complete_link_candidate_run(
    pages: Sequence[LinkCandidateResult],
) -> LinkCandidateRunProof: ...
def render_wiki_index(paths: RepoPaths, documents: MarkdownDocuments) -> str: ...
def validate_graph(
    paths: RepoPaths, *, documents: MarkdownDocuments | None = None,
    index_text: str | None = None, corpus_revision: str | None = None,
) -> ValidationReport: ...
```

```python
# brainlib/wiki_transaction.py
class WikiTransactionError(RuntimeError):
    pass

class ApprovalRequired(WikiTransactionError):
    pass

class CorpusRevisionChanged(WikiTransactionError):
    pass

class InvalidWikiJournal(WikiTransactionError):
    pass

class LinkCandidateCoverageError(WikiTransactionError):
    diagnostic: Diagnostic

ChangeIntent = Literal[
    "routine", "rename", "delete", "merge", "split", "remove_claim",
    "remove_relationship", "resolve_contradiction", "major_uncertain_rewrite", "ambiguous_rename",
]

@dataclass(frozen=True)
class WikiChange:
    operation: Literal["write", "delete"]
    path: PurePosixPath
    staging_path: PurePosixPath | None
    sha256: str | None
    @classmethod
    def write(cls, path: PurePosixPath, staging_path: PurePosixPath, sha256: str) -> "WikiChange": ...
    @classmethod
    def delete(cls, path: PurePosixPath) -> "WikiChange": ...

@dataclass(frozen=True)
class WikiManifest:
    schema_version: Literal[1]
    expected_corpus_revision: str
    change_intent: ChangeIntent
    approval_event_id: str | None
    citation_rewrites: tuple[CitationRewrite, ...]
    link_candidate_runs: tuple[LinkCandidateRunProof, ...]
    changes: tuple[WikiChange, ...]
    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, text: str) -> "WikiManifest": ...

@dataclass(frozen=True)
class WikiApplyResult:
    corpus_revision: str
    changed_paths: tuple[PurePosixPath, ...]
    index_path: PurePosixPath
    recovered: bool

@dataclass(frozen=True)
class WikiRecoveryResult:
    recovered: bool
    restored_paths: tuple[PurePosixPath, ...]

def apply_wiki_manifest(
    paths: RepoPaths, ledger: LedgerStore, manifest: WikiManifest, *,
    replace_file: Callable[[Path, Path], None] = os.replace,
) -> WikiApplyResult: ...
def load_wiki_manifest(paths: RepoPaths, manifest_path: Path) -> WikiManifest: ...
def validate_wiki_transaction_state(
    paths: RepoPaths, *, corpus_revision: str,
) -> ValidationReport: ...
def recover_wiki_transaction(paths: RepoPaths) -> WikiRecoveryResult: ...
```

The versioned manifest JSON is exactly:

```json
{
  "schema_version": 1,
  "expected_corpus_revision": "<64 lowercase hex>",
  "change_intent": "routine",
  "approval_event_id": null,
  "citation_rewrites": [
    {
      "source_id": "src_<64 lowercase hex>",
      "content_sha256": "<64 lowercase hex>",
      "raw_path": "_versions/src_<64 lowercase hex>/<64 lowercase hex>/original.pdf"
    }
  ],
  "link_candidate_runs": [
    {
      "run_id": "srch_<32 lowercase hex>",
      "corpus_revision": "<64 lowercase hex>",
      "page_path": "wiki/pages/alpha.md",
      "terms": ["Alpha", "A. Example"],
      "candidate_manifest_sha256": "<64 lowercase hex>",
      "page_count": 3,
      "candidate_count": 201
    }
  ],
  "changes": [
    {
      "operation": "write",
      "path": "wiki/pages/alpha.md",
      "staging_path": ".brain/wiki-staging/<run-id>/files/wiki/pages/alpha.md",
      "sha256": "<64 lowercase hex>"
    }
  ]
}
```

Delete entries have exactly `operation` and `path`; write entries have exactly all four write fields. All paths are repository-relative POSIX text. `to_json()` emits deterministic UTF-8 JSON with sorted object keys, list order preserved after validation, and one trailing newline; `from_json()` rejects duplicate JSON object keys and non-object roots before field validation.

Add `stage_wiki_write`, `drain_link_candidates`, `link_proof_for_change`, `manifest_for`, and `write_wiki_manifest` to `tests/helpers_knowledge.py`. `stage_wiki_write` writes UTF-8 only beneath `.brain/wiki-staging/test-run/files/`, hashes it, and returns repository-relative `WikiChange.write(...)`. `link_proof_for_change` derives nonempty title/alias/slug terms from the live or staged logical record, drains the real paged link search, and calls `complete_link_candidate_run`. Unless explicitly supplied, `manifest_for` builds one proof for every explicit change, computes `compute_corpus_revision(scenario.ledger.load_all().values())`, sorts canonical `CitationRewrite` and proof tuples, and returns schema version 1. `write_wiki_manifest` writes `manifest.to_json()` to `.brain/wiki-staging/test-run/manifest.json` and returns that path. No test writes a live wiki path except when deliberately simulating concurrent mutation or corrupting an index for validation.

`find_link_candidates` requires nonempty ordered-unique CR/LF/NUL-free terms and creates a Task 2 search run with purpose `link_candidates`, binding the excluded canonical logical `page_path` plus those exact terms. It scans every page/question operand batch and spools exactly one deterministic candidate record per matching record (its first match line/context) in repo-path order. It never returns the complete array: `resume_link_candidates` serves bounded authenticated pages, and `complete_link_candidate_run` requires page zero through the final page, the exact cursor chain, invariant bindings, canonical per-page `result_sha256` values, aggregate counts, a gap-free final `complete: true`, and no unconsumed cursor before producing a proof. Zero candidates is one valid completed empty page/proof. Invalid, stale, expired, spool-limited, tampered, or undrained runs cannot produce a proof.

`validate_graph` parses the supplied logical-path-to-text mapping or the complete live page/question tree, validates unique IDs/slugs, relative targets, no self-links, reciprocal page/question relationships, aliases, first meaningful occurrence for each declared related target in each section, and deterministic index text. Ordinary wiki destinations use the same `encode_markdown_path`/`resolve_markdown_path` grammar as citations: split an optional heading fragment first, require the path portion to round-trip canonically (including spaces and non-ASCII filenames), and require any fragment to name a real scanned heading. There is no second permissive bare-path decoder. It reports every issue with the existing separate graph codes; it never rewrites content.

`WikiManifest.from_json` requires exactly the documented fields and rejects unknown fields, duplicate target paths, non-lowercase 64-hex hashes/revisions, `CitationRewrite` entries not strictly sorted by `(source_id, content_sha256, raw_path.as_posix())`, duplicate/conflicting rewrite keys, or link proofs not strictly sorted by `page_path`. It applies the same strict path rules to proof paths and change paths and requires exactly one proof path for every explicit change path; an empty change set has no proof requirement. `load_wiki_manifest` requires the manifest itself to be the regular nonsymlink file `.brain/wiki-staging/<run-id>/manifest.json`, reads strict UTF-8, and proves that write staging files live beneath the same run, are regular nonsymlink UTF-8, and match declared hashes. Neither parser silently normalizes a path. `wiki/index.md` is generator-owned and cannot appear in `changes`.

Both apply and recovery acquire `SourceWriteLock.acquire(paths.lock)` and then `SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock")`; no inverse ordering exists. Apply first invokes an internal `_recover_locked` when `.brain/wiki-transaction.json` exists, then revalidates every staged file/path/hash while locked, computes the canonical ledger revision, and compares it to `expected_corpus_revision`. For every explicit change, it calls an internal locked run verifier that requires the proof's HMAC-authenticated run metadata, purpose, page path, terms, corpus revision, live-wiki operand digest, candidate manifest checksum/count, page count, and `highest_page_served == final_page`; missing, stale, expired, corrupt, or undrained proof raises `LinkCandidateCoverageError(Diagnostic("link_candidate_search_incomplete", ...))`. It never trusts proof fields without the retained run state.

Only after those coverage gates does apply load all live Markdown into an absolute logical-path mapping, overlay writes/deletes in memory, verify every supplied `CitationRewrite` agrees with the retained ledger version path, and canonicalize every parsed citation destination from the ledger. It renders the candidate index and runs metadata-fast citation plus complete graph/index validation with the exact revision. Any error aborts without touching live files. Thus late-alphabet inbound links cannot be skipped, and a later empty manifest still repairs stale citation destinations after a lost adoption/rename report without requiring a link proof.

Approval is mandatory for `delete`, `merge`, `split`, `remove_claim`, `remove_relationship`, `resolve_contradiction`, `major_uncertain_rewrite`, and `ambiguous_rename`. Deterministic structural comparison also rejects a `routine`/`rename` manifest that deletes a record or removes a citation marker/definition or declared relationship; the curator must resubmit it with the matching approved intent. Semantic claim removal that retains its marker cannot be inferred mechanically, so the skill must declare `remove_claim`.

After preflight, stage only changed candidate documents plus the generated index. Before the first live replacement/deletion, write `.brain/wiki-transaction.json` containing schema version, target paths, prior-existence flags, old/new hashes, and base64 original bytes; fsync the file and `.brain` directory. Publish in sorted target order, fsync each parent directory, and remove/fsync the journal only after all changes succeed. `_recover_locked` validates journal paths, hashes, and base64 before restoring every prior byte sequence or prior absence in sorted order. Invalid journals fail closed. Public recovery only acquires locks and delegates to that internal helper.

`validate_wiki_transaction_state` is read-only: no journal yields `checks=("wiki-transaction",)` with no issues; any journal yields error `wiki_transaction_pending` and directs the caller to `./brain --json wiki recover`. It does not parse, repair, or delete the journal during validation.

Agents create staging files and manifests but never edit `wiki/pages`, `wiki/questions`, or `wiki/index.md` directly. Task 5 exposes the only two publication commands.

- [ ] **Step 4: Run focused graph and transaction tests**

Run: `python3 -m pytest tests/unit/test_graph.py tests/unit/test_wiki_transaction.py -v`

Expected: PASS; failures occur before publication, interruption leaves a recoverable journal, recovery restores the whole original graph, and no direct write bypass exists.

- [ ] **Step 5: Review the graph/transaction checkpoint without committing**

```bash
git diff --check
git diff -- brainlib/graph.py brainlib/wiki_transaction.py wiki/index.md tests/helpers_knowledge.py tests/unit/test_graph.py tests/unit/test_wiki_transaction.py tests/fixtures/wiki/scenarios/graph tests/fixtures/wiki/scenarios/removal tests/fixtures/wiki/scenarios/transaction
```

### Task 5: Expose search and graph checks through the CLI and combined validator

**Files:**
- Create: `brainlib/wiki_evidence.py`
- Modify: `brainlib/validation.py`
- Modify: `brainlib/cli.py`
- Modify: `brain`
- Create: `tests/unit/test_wiki_evidence.py`
- Create: `tests/integration/test_wiki_validation.py`

**Interfaces:**
- Consumes: `WikiEvidencePacket`, `CitationRef`, `validate_citations`, `validate_graph`, `apply_wiki_manifest`, `recover_wiki_transaction`, canonical `validate_template_layout`, `validate_source_ledger`, `ChecksumCache`, `CommandResult`, and Task 2 search functions/proofs.
- Produces: `build_wiki_evidence_packet`, `validate_wiki`, locked `validate_repository`, paged `./brain links candidates`, `./brain links check`, `./brain wiki apply/recover`, and the final `./brain validate [--full]` integration.

- [ ] **Step 1: Write failing CLI and combined-validation tests**

```python
# tests/unit/test_wiki_evidence.py
def test_build_wiki_evidence_packet_revalidates_exact_citations_and_complete_run(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    first = search_wiki(
        scenario.paths, scenario.ledger,
        SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
    )
    pages = drain_search(scenario.paths, scenario.ledger, first)
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-line-1")
    packet = build_wiki_evidence_packet(
        scenario.paths, scenario.ledger, question_id="question-alpha",
        search_pages=pages, matched_paths=(ref.document_path,),
        supporting_citations=(ref,), counterevidence_citations=(),
    )
    assert packet.complete is True
    assert packet.search_run_id == first.run_id
    assert packet.corpus_revision == first.corpus_revision
    assert packet.matched_records[0].path == ref.document_path
    assert packet.revalidated_citations[0].citation_id == ref.citation_id


def test_wiki_evidence_builder_rejects_undrained_search(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    first = search_wiki(
        scenario.paths, scenario.ledger,
        SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/alpha.md"), "unused")
    with pytest.raises(WikiEvidenceError) as undrained:
        build_wiki_evidence_packet(
            scenario.paths, scenario.ledger, question_id="question-alpha",
            search_pages=(first,), matched_paths=(ref.document_path,),
            supporting_citations=(ref,), counterevidence_citations=(),
        )
    assert undrained.value.diagnostic.code == "wiki_search_incomplete"


def test_wiki_evidence_builder_rejects_changed_exact_citation_target(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    pages = drain_search(
        scenario.paths, scenario.ledger,
        search_wiki(
            scenario.paths, scenario.ledger,
            SearchRequest("wiki", None, ("Alpha",), 1, page_size=1),
        ),
    )
    ref = CitationRef(PurePosixPath("wiki/pages/current.md"), "cite-alpha-line-1")
    representation = scenario.ledger.active_representations()[0]
    raw = scenario.paths.raw / representation.raw_path
    raw.write_bytes(raw.read_bytes() + b"changed after search")
    with pytest.raises(WikiEvidenceError) as invalid:
        build_wiki_evidence_packet(
            scenario.paths, scenario.ledger, question_id="question-alpha",
            search_pages=pages, matched_paths=(ref.document_path,),
            supporting_citations=(ref,), counterevidence_citations=(),
        )
    assert invalid.value.diagnostic.code == "wiki_citation_invalid"
```

```python
# tests/integration/test_wiki_validation.py
def test_brain_links_check_json_reports_broken_graph_issue(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/nonreciprocal")
    completed = run_brain(scenario.root, "--json", "links", "check")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert completed.stderr == ""
    assert payload["command"] == "links check"
    assert payload["ok"] is False
    assert "relationship_not_reciprocal" in {issue["code"] for issue in payload["data"]["report"]["issues"]}


def test_brain_links_candidates_must_resume_through_late_record(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    completed = run_brain(
        scenario.root, "--json", "links", "candidates", "wiki/pages/alpha.md",
        "--term", "Alpha", "--term", "A. Example", "--page-size", "1",
    )
    assert completed.returncode == 0 and completed.stderr == ""
    pages = [json.loads(completed.stdout)["data"]]
    while pages[-1]["next_cursor"] is not None:
        resumed = run_brain(
            scenario.root, "--json", "links", "candidates",
            "--cursor", pages[-1]["next_cursor"],
        )
        assert resumed.returncode == 0 and resumed.stderr == ""
        pages.append(json.loads(resumed.stdout)["data"])
    assert pages[-1]["complete"] is True
    assert all(len(page["candidates"]) <= 1 for page in pages)
    assert "wiki/pages/zzz-related.md" in {
        candidate["path"] for page in pages for candidate in page["candidates"]
    }
    assert all(len(page["result_sha256"]) == 64 for page in pages)


def test_brain_validate_full_combines_ledger_citation_and_graph_checks(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "validate", "--full")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert {check for report in payload["data"]["reports"] for check in report["checks"]} >= {"source-ledger", "citations", "wiki-graph"}


def test_repository_validation_is_metadata_fast_and_full_mode_shares_one_cache(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    calls: Counter[Path] = Counter()
    cache = ChecksumCache(hash_file=lambda path: calls.update((path.resolve(),)) or compute_sha256(path))
    assert all(report.ok for report in validate_repository(scenario.paths, scenario.ledger, full=False, checksum_cache=cache))
    assert calls == Counter()
    assert all(report.ok for report in validate_repository(scenario.paths, scenario.ledger, full=True, checksum_cache=cache))
    assert calls and set(calls.values()) == {1}


def test_brain_wiki_apply_loads_same_run_manifest_and_only_then_publishes(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- approved staged update -->\n",
    )
    manifest_path = write_wiki_manifest(scenario, manifest_for(scenario, (change,)))
    completed = run_brain(
        scenario.root, "--json", "wiki", "apply", "--manifest",
        scenario.paths.repo_relative(manifest_path).as_posix(),
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert payload == {
        "command": "wiki apply",
        "ok": True,
        "data": {
            "corpus_revision": compute_corpus_revision(scenario.ledger.load_all().values()),
            "changed_paths": ["wiki/pages/alpha.md"],
            "index_path": "wiki/index.md",
            "recovered": False,
        },
        "warnings": [],
        "errors": [],
    }
    assert "approved staged update" in alpha.read_text(encoding="utf-8")


def test_brain_wiki_apply_rejects_manifest_outside_run_with_canonical_json_error(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "wiki", "apply", "--manifest", "wiki/index.md")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["command"] == "wiki apply"
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_manifest"


def test_brain_wiki_recover_is_structured_and_idempotent(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "wiki", "recover")
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["data"] == {"recovered": False, "restored_paths": []}


def test_validate_reports_pending_transaction_without_mutating_it(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    journal = scenario.paths.root / ".brain/wiki-transaction.json"
    journal.write_text("{}", encoding="utf-8")
    completed = run_brain(scenario.root, "--json", "validate")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert "wiki_transaction_pending" in {
        issue["code"] for report in payload["data"]["reports"] for issue in report["issues"]
    }
    assert journal.read_text(encoding="utf-8") == "{}"
```

- [ ] **Step 2: Run unit and integration tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_wiki_evidence.py tests/integration/test_wiki_validation.py -v`

Expected: FAIL because the strict wiki-evidence builder, paged `./brain links candidates`, `./brain wiki apply/recover`, and the combined validator are not registered.

- [ ] **Step 3: Implement report composition and command handlers**

```python
# brainlib/wiki_evidence.py
class WikiEvidenceError(ValueError):
    diagnostic: Diagnostic

def build_wiki_evidence_packet(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    question_id: str,
    search_pages: Sequence[SearchResult],
    matched_paths: tuple[PurePosixPath, ...],
    supporting_citations: tuple[CitationRef, ...],
    counterevidence_citations: tuple[CitationRef, ...],
    contradictions: tuple[Diagnostic, ...] = (),
    coverage_gaps: tuple[Diagnostic, ...] = (),
) -> WikiEvidencePacket: ...
```

`build_wiki_evidence_packet` first calls `complete_search_run` and requires a `scope="wiki"`, `mode="research"`, `pass_name=None` proof. It then holds the source lock followed by the wiki lock for one coherent operation, uses the same internal authenticated-run verifier as `verify_search_run_proof`, and requires the proof revision and wiki operand digest to remain current. `matched_paths` must be sorted, unique, direct repository-relative page/question Markdown paths present in the completed result set; each is parsed into a `WikiRecordMatch` and must have at least one exact matched request term. The builder runs metadata-fast `validate_citations` over those exact documents, resolves every marker/definition through `LedgerStore.find_representation`, and creates sorted unique `RevalidatedCitation` values. Every sorted unique `CitationRef` must resolve to one of those values, supporting citations must be nonempty, supporting and counterevidence sets must not overlap, and any caller-supplied coverage gap rejects construction. An undrained/expired/stale/tampered wiki run raises `WikiEvidenceError(Diagnostic("wiki_search_incomplete", ...))`; any invalid target or citation raises `WikiEvidenceError(Diagnostic("wiki_citation_invalid", ...))`. On success the returned packet carries the current corpus revision, an empty coverage-gap tuple, and `complete=True`; contradictions remain explicit. The curator accepts this packet instead of source research only after checking `complete`; it still creates or updates the single evolving topic question record and publishes it through a proved manifest transaction.

```python
# brainlib/validation.py
def wiki_documents(paths: RepoPaths) -> tuple[Path, ...]: ...

def validate_wiki(
    paths: RepoPaths, ledger: LedgerStore, *, full: bool,
    checksum_cache: ChecksumCache | None = None,
) -> ValidationReport:
    documents = read_markdown_documents(wiki_documents(paths))
    citation_report = validate_citations(
        paths, ledger, documents, full=full, checksum_cache=checksum_cache,
    )
    graph_report = validate_graph(paths, documents=documents, corpus_revision=citation_report.corpus_revision)
    return merge_reports(citation_report, graph_report)

def validate_repository(
    paths: RepoPaths, ledger: LedgerStore, *, full: bool,
    checksum_cache: ChecksumCache | None = None,
) -> tuple[ValidationReport, ...]:
    with SourceWriteLock.acquire(paths.lock):
        with SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock"):
            records = ledger.load_all()
            cache = (checksum_cache or ChecksumCache()) if full else checksum_cache
            if full:
                assert cache is not None
                cache.begin_transaction()  # exactly once for the combined snapshot
            revision = compute_corpus_revision(records.values())
            layout_report = validate_template_layout(paths)
            source_report = validate_source_ledger(
                paths, records, full=full, checksum_cache=cache,
            )
            transaction_report = validate_wiki_transaction_state(
                paths, corpus_revision=revision,
            )
            if not transaction_report.ok:
                return (layout_report, source_report, transaction_report)
            return (
                layout_report, source_report, transaction_report,
                validate_wiki(paths, ledger, full=full, checksum_cache=cache),
            )
```

`wiki_documents` returns the sorted direct `*.md` children of both wiki collections, never follows symlinks, and never treats `wiki/index.md` as claim prose. `validate_repository` is the sole final validation orchestration and the sole source of `./brain validate` reports: this task extends the prerequisite function in place, and milestone 5 may extend this same function again but must never assemble or append a second report set in `brainlib.cli`. It acquires the checked `SourceWriteLock` context and then the checked wiki-lock context; false or raised release is non-clean, body exceptions remain primary with cleanup notes, and a manually proven release stays idempotent. It loads one coherent ledger view and checks for a pending or invalid transaction journal before reading live wiki files. A pending journal is a validation error directing the caller to `./brain --json wiki recover`; validation never mutates it. Normal mode performs only stable namespace observations plus ledger-recorded size/mtime/fingerprint checks and never requests SHA-256. Full mode creates exactly one cache unless the test injects one, begins the transaction exactly once at this outer boundary, and passes that same instance through `validate_source_ledger` and `validate_wiki`/`validate_citations`. Neither child resets it, so a raw or extracted target cited many times is hashed once while identity is revalidated on each use. Do not leave the prerequisite source-lock-only CLI handler around this orchestration.

Implement these commands with identical `CommandResult` structured/human semantics:

```text
./brain --json links candidates wiki/pages/alpha.md --term Alpha --term "A. Example"
./brain --json links candidates --cursor OPAQUE_TOKEN
./brain --json links check
./brain --json wiki apply --manifest .brain/wiki-staging/<run-id>/manifest.json
./brain --json wiki recover
./brain --json validate
./brain --json validate --full
```

`links candidates` start mode requires exactly one canonical repository-relative logical path under `wiki/pages` or `wiki/questions`, one or more nonempty `--term` values, and optional `--page-size`/`--max-run-bytes`; it rejects `--cursor`. Continuation mode requires only `--cursor` and rejects path, term, page-size, and run-budget arguments. Each success envelope has `command="links candidates"` and its exact `LinkCandidateResult` directly in `data`: `run_id`, `corpus_revision`, `page_path`, `terms`, `page_index`, `request_cursor`, `next_cursor`, `complete`, `candidate_count`, `candidate_manifest_sha256`, `result_sha256`, bounded `candidates`, and `coverage_gaps`. A caller must retain every page and call `complete_link_candidate_run` before placing its proof in `WikiManifest.link_candidate_runs`; seeing an interesting early page is never sufficient. Malformed/tampered cursors and invalid arguments are exit 2 with canonical `invalid_search_cursor`/`invalid_arguments` errors. Stale, expired, resource-limited, or otherwise incomplete runs are exit 1 with their blocking diagnostic. Missing/failed `rg` is exit 2. Zero candidates is one successful complete empty page.

`./brain wiki apply` resolves the repo-relative manifest operand, calls `load_wiki_manifest` and `apply_wiki_manifest`, and returns data `{corpus_revision, changed_paths, index_path, recovered}`. `./brain wiki recover` returns `{recovered, restored_paths}`. Both serialize paths as sorted repository-relative POSIX strings. Manifest/schema/path failures return code 2 and `Diagnostic("invalid_manifest", ...)`; approval, stale-revision, lock, preflight-validation, invalid-journal, and I/O failures are caught and mapped to stable specific diagnostics instead of tracebacks. A preflight validation failure returns code 1. JSON mode always writes exactly one envelope to stdout and nothing to stderr.

`./brain validate` calls `validate_repository`. `--full` adds rehashing but does not suppress citation, graph, index, or pending-transaction checks. Return exit status 0 only when every report has no error-severity issue, 1 when validation/preflight reports errors, and 2 for invalid CLI arguments, invalid manifests, missing `rg`, or execution failure. Root-first JSON stores the report list at `payload["data"]["reports"]`; every report serializes canonical `checks`, `issues`, and `corpus_revision`, while every issue serializes canonical `severity`, `code`, `message`, `path`, and `details`. No subcommand emits an ad-hoc JSON shape, invokes argparse's default JSON-mode stderr/`SystemExit` path, or writes a live wiki file outside the transaction module.

- [ ] **Step 4: Run integration and full local test suite**

Run: `python3 -m pytest tests/unit/test_wiki_evidence.py tests/integration/test_wiki_validation.py -v && python3 -m pytest -q`

Expected: PASS; each isolated broken scenario returns its exact structured issue, the valid scenario passes full validation, staging is invisible until apply, and apply/recover always return the canonical envelope.

- [ ] **Step 5: Review the command and validator checkpoint without committing**

```bash
git diff --check
git diff -- brain brainlib/cli.py brainlib/validation.py brainlib/wiki_evidence.py tests/unit/test_wiki_evidence.py tests/integration/test_wiki_validation.py
```

### Task 6: Add an executable answer-workflow contract test fixture

**Files:**
- Create: `tests/integration/test_knowledge_workflow_contract.py`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/raw/alpha.txt`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/raw/exception.txt`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/extracted/alpha.txt/<content-sha256>/<derivation-id>.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/extracted/exception.txt/<content-sha256>/<derivation-id>.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/ledger/<source-id>.json` for both sources
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/sources/ledger.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/wiki/pages/alpha.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/wiki/questions/what-is-alpha.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/wiki/index.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/question-input.md`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/sync-command-result.json`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/repo/.brain/sync-results/<result-id>.jsonl`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/workflow-events.json`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/wiki-fast-path-events.json`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/expected-evidence-packet.json`
- Create: `tests/fixtures/wiki/scenarios/workflow/answer/expected-wiki-evidence-packet.json`
- Create: `tests/fixtures/wiki/workflow/what-is-alpha.md`
- Modify: `docs/brain/schemas/evidence-packet.md`

**Interfaces:**
- Consumes: bounded `SyncReport` command serialization plus its canonical digest-verified result manifest, `SnapshotResult`, paged `search_wiki`/`search_active_sources`, `WikiEvidencePacket`/`EvidencePacket`, citation grammar, manifest link-candidate proofs, and `validate_wiki`.
- Produces: a deterministic contract fixture for milestone 5’s `brain-answer` skill; it does not implement LLM reasoning or create a `brain ask` command.

- [ ] **Step 1: Write the failing workflow-boundary tests**

```python
# tests/integration/test_knowledge_workflow_contract.py
WORKFLOW_FIXTURE = Path("tests/fixtures/wiki/scenarios/workflow/answer")

def load_packet(scenario: KnowledgeScenario) -> EvidencePacket:
    return EvidencePacket.from_json(
        (scenario.root / WORKFLOW_FIXTURE / "expected-evidence-packet.json").read_text(encoding="utf-8")
    )

def load_wiki_packet(scenario: KnowledgeScenario) -> WikiEvidencePacket:
    return WikiEvidencePacket.from_json(
        (scenario.root / WORKFLOW_FIXTURE / "expected-wiki-evidence-packet.json").read_text(encoding="utf-8")
    )

def test_insufficient_wiki_requires_exactly_discovery_expansion_verification_packet(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    assert tuple(item.name for item in packet.passes) == ("discovery", "expansion", "verification")
    assert packet.passes[0].terms == ("Alpha",)
    assert packet.passes[1].terms == ("Beta relationship",)
    assert packet.passes[2].terms == ("Alpha exception",)
    assert all(item.complete and item.pages for item in packet.passes)
    assert len({item.run_id for item in packet.passes}) == 3


def test_packet_consumes_exact_sync_freshness_fields_and_coverage_gap(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    sync_payload = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    assert set(sync_payload) == {"command", "ok", "data", "warnings", "errors"}
    sync_data = sync_payload["data"]
    assert packet.corpus_revision == sync_data["corpus_revision"]
    reference = SyncResultReference.from_dict(sync_data["result_manifest"])
    events = tuple(SyncResultStore(scenario.paths).iter_events(reference))
    expected_fresh_ids = tuple(
        event.data["source_id"]
        for event in events
        if event.kind == "new_active_representation"
    )
    assert packet.freshness_probe_source_ids == expected_fresh_ids
    assert all(item.corpus_revision == sync_data["corpus_revision"] for item in packet.passes)
    assert packet.support[0].source_id == expected_fresh_ids[0]
    assert packet.coverage_gaps == (Diagnostic("source_failed", "src-unavailable is failed"),)


def test_relevant_freshness_probe_triggers_three_research_passes_not_a_fourth_pass(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    sync_data = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )["data"]
    reference = SyncResultReference.from_dict(sync_data["result_manifest"])
    assert packet.freshness_probe_source_ids == tuple(
        event.data["source_id"]
        for event in SyncResultStore(scenario.paths).iter_events(reference)
        if event.kind == "new_active_representation"
    )
    assert tuple(item.name for item in packet.passes) == ("discovery", "expansion", "verification")


def test_every_sync_is_followed_by_wiki_apply_before_wiki_search_even_without_rewrites(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    sync_payload = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    reference = SyncResultReference.from_dict(
        sync_payload["data"]["result_manifest"]
    )
    assert not any(
        event.kind == "citation_rewrite"
        for event in SyncResultStore(scenario.paths).iter_events(reference)
    )
    events = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "workflow-events.json").read_text(encoding="utf-8")
    )
    assert events[:4] == ["sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete"]


def test_every_logical_source_pass_is_drained_before_the_next_one_or_synthesis(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    events = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "workflow-events.json").read_text(encoding="utf-8")
    )
    milestones = [
        "source_discovery_start", "source_discovery_complete",
        "source_expansion_start", "source_expansion_complete",
        "source_verification_start", "source_verification_complete", "curator",
    ]
    assert [event for event in events if event in milestones] == milestones


def test_sufficient_wiki_fast_path_still_updates_topic_record_without_source_passes(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_wiki_packet(scenario)
    events = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "wiki-fast-path-events.json").read_text(encoding="utf-8")
    )
    assert packet.complete is True
    assert packet.supporting_citations
    assert events[:4] == ["sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete"]
    assert not any(event.startswith("source_") for event in events)
    assert "curator_update_question" in events
    assert events[-2:] == ["validate", "answer"]
```

- [ ] **Step 2: Run the workflow-boundary tests to verify they fail**

Run: `python3 -m pytest tests/integration/test_knowledge_workflow_contract.py -v`

Expected: FAIL because the fixture and documented packet requirements are absent.

- [ ] **Step 3: Add the fixed semantic handoff requirements and fixture**

```json
{
  "question_id": "question-alpha",
  "corpus_revision": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  "freshness_probe_source_ids": ["src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
  "passes": [
    {
      "name": "discovery",
      "terms": ["Alpha"],
      "run_id": "srch_11111111111111111111111111111111",
      "corpus_revision": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "candidate_count": 2,
      "candidate_manifest_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "pages": [
        {"page_index": 0, "match_count": 1, "result_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"page_index": 1, "match_count": 1, "result_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}
      ],
      "complete": true,
      "coverage_gaps": []
    },
    {
      "name": "expansion",
      "terms": ["Beta relationship"],
      "run_id": "srch_22222222222222222222222222222222",
      "corpus_revision": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "candidate_count": 1,
      "candidate_manifest_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
      "pages": [{"page_index": 0, "match_count": 1, "result_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}],
      "complete": true,
      "coverage_gaps": []
    },
    {
      "name": "verification",
      "terms": ["Alpha exception"],
      "run_id": "srch_33333333333333333333333333333333",
      "corpus_revision": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
      "candidate_count": 1,
      "candidate_manifest_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
      "pages": [{"page_index": 0, "match_count": 1, "result_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}],
      "complete": true,
      "coverage_gaps": []
    }
  ],
  "support": [{"source_id": "src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "content_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "derivation_id": "drv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "anchor": {"kind": "line", "value": "4"}, "passage": "Alpha supports the relationship."}],
  "counterevidence": [{"source_id": "src_dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "content_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "derivation_id": "drv_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "anchor": {"kind": "line", "value": "8"}, "passage": "The relationship has an exception."}],
  "coverage_gaps": [{"code": "source_failed", "message": "src-unavailable is failed", "path": null, "details": {}}]
}
```

`expected-wiki-evidence-packet.json` uses the exact Task 1 codec, not a reduced fixture shape. It records `question-alpha`, the same 64-hex current revision, one `srch_<32hex>` wiki run, sorted `matched_records` containing `wiki/questions/what-is-alpha.md`, all source/version/derivation/anchor fields for each `revalidated_citation`, document-qualified `supporting_citations` and `counterevidence_citations`, explicit `contradictions`/`coverage_gaps`, and `complete: true`. Its supporting reference is `{"document_path":"wiki/questions/what-is-alpha.md","citation_id":"cite-alpha-line-4"}` and resolves to the fixture's exact definition. `wiki-fast-path-events.json` is exactly `sync`, `wiki_apply_after_sync`, wiki search start/complete (with any page-resume events between them), freshness start/complete when the sync reports new active sources, `wiki_evidence_packet`, `curator_update_question`, link-candidate start/complete, `wiki_apply`, `validate`, and `answer`; it contains no source research pass event.

Create `sync-command-result.json` as the exact canonical bounded `CommandResult` envelope for the same committed scenario. Its `data` has the real `corpus_revision`, decision counts and bounded samples, exact per-event counts, `sample_limits`, and a canonical `{result_id, path, sha256, corpus_revision, event_counts}` result reference. Materialize the referenced JSONL beneath the fixture repository's `.brain/sync-results/`; it has the canonical header, monotonically sequenced exact events, and trailer with exact counts/event digest/revision, and its whole-file digest equals the result ID/reference. The fixture loader must use `SyncResultReference.from_dict()` plus `SyncResultStore.verify()`/`iter_events()` rather than trusting or reconstructing full sets from display arrays. The JSON block above is shape-only: the generator replaces every illustrative repeated-hex identity with values computed from the fixed fixture bytes and canonical identity functions.

Extend the evidence-packet schema to require the milestone-5 answer skill to execute this sequence: run `./brain --json sync`; parse the bounded reference, verify the manifest before consuming any event, stream it to completion, require its counts/revision to match the command envelope, durably deduplicate/apply the `result_id`, and only then run `./brain --json source acknowledge-sync-result --result-id "$result_id"`. A partial/failed stream is never acknowledged. After **every** acknowledged sync, always pass the exact sorted `citation_rewrite` events (including an empty tuple) through an expected-revision empty/staged `./brain --json wiki apply` before reading wiki content, so pending recovery and durable ledger-driven citation canonicalization run. Derive nonempty initial terms; start wiki search, follow every cursor, call `complete_search_run`, and verify its proof. When exact `new_active_representation` events are nonempty, start a freshness probe against exactly those source IDs, follow every cursor, and complete/verify it before accepting older wiki material. An empty exact event set means no freshness command and records `freshness_probe_source_ids=()`. Never infer completeness from bounded sample arrays. If the complete wiki run is sufficient and the freshness run supplies no relevant newer conflict, build a complete `WikiEvidencePacket` from revalidated citations and give it to the curator; the curator still creates or updates the single evolving topic Q&A record, drains link candidates for every proposed change, publishes through `./brain --json wiki apply`, and validates before answering.

If the freshness probe is relevant or wiki evidence is insufficient, start source search once for each **logical** pass in discovery → expansion → verification order and drain all continuation pages before beginning the next pass. Complete and call `verify_search_run_proof` for each run before converting it with `completed_search_pass`. Build expansion terms only from the completed discovery context; build verification terms for decisive support, temporal qualifiers, exceptions, conflicts, and counterexamples. An expired, stale, spool-limited, tampered, or otherwise incomplete run is a blocking coverage gap and cannot reach synthesis; a completely drained zero-hit run is valid. After `completed_search_pass` yields exactly three records with the same current revision, build `EvidencePacket` and give only that complete packet to the curator. The freshness probe is recorded as sync evidence, not as a fourth member of `EvidencePacket.passes`. The curator then updates the topic record, drains link candidates, publishes through `./brain --json wiki apply`, and runs the sole combined validator before replying.

For incomplete local evidence, the contract ends with a `partial` or `unanswered` record and a request for web approval. It must not browse. For an approved web event, each successful `./brain --json source snapshot-url ...` returns `payload["data"]["snapshot"]`, the canonical serialized `SnapshotResult`; the workflow consumes its `source_id`, `raw_path`, `content_sha256`, `source_version`, `retrieval`, `extraction_result`, `active_representation`, and `corpus_revision`. When agent work is needed, it also consumes the sibling repo-relative-or-null `handoff_manifest` and sorted `handoffs` summaries before registering extraction. It persists only sources actually used. It does not invent or claim a post-web `SyncReport`, and a later sanity sync cannot be relied on to report snapshots that are already active. Once every used capture has a non-null `active_representation` (or agent extraction is registered), collect those exact representations as the newly available set, take the latest returned corpus revision, and run discovery, expansion, and verification over the complete updated active ledger. Each of those three logical passes must drain every continuation page and complete successfully before creating a fresh packet. Only then may the curator persist a durable answer. This fixture documents control flow only and never calls the network.

- [ ] **Step 4: Run the workflow-contract tests**

Run: `python3 -m pytest tests/integration/test_knowledge_workflow_contract.py -v`

Expected: PASS; the source packet has exactly three complete logical passes, the wiki fast-path packet revalidates exact citations, and both carry current revision/coverage information.

- [ ] **Step 5: Review the answer-workflow handoff checkpoint without committing**

```bash
git diff --check
git diff -- docs/brain/schemas/evidence-packet.md tests/integration/test_knowledge_workflow_contract.py tests/fixtures/wiki/scenarios/workflow tests/fixtures/wiki/workflow
```

### Task 7: Run milestone acceptance checks and hand off to the shared-skill milestone

**Files:**
- Create: `tests/fixtures/wiki/generate_scenarios.py`
- Create: `tests/fixtures/wiki/workflow/what-is-alpha.md`
- Modify: `docs/brain/schemas/wiki-page.md`
- Modify: `docs/brain/schemas/question-record.md`
- Modify: `docs/brain/schemas/citation.md`
- Modify: `docs/brain/schemas/evidence-packet.md`

**Interfaces:**
- Consumes: all Tasks 1–6.
- Produces: reviewed stable contracts for milestone 5; no new runtime interface.

- [ ] **Step 1: Write the final failing acceptance test for an evolving topic record**

```python
# append to tests/integration/test_knowledge_workflow_contract.py
def test_follow_up_refines_one_topic_record_and_preserves_question_history(repo_root: Path) -> None:
    record = parse_question(repo_root / "tests/fixtures/wiki/workflow/what-is-alpha.md")
    assert record.question_id == "question-alpha"
    assert record.canonical_question == "How does Alpha relate to Beta?"
    assert record.prior_phrasings == ("What is Alpha?",)
    assert record.answer_status in {"answered", "partial", "conflicted"}


def test_committed_scenario_overlays_are_reproducible(repo_root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=repo_root, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
```

- [ ] **Step 2: Run the acceptance test to verify it fails**

Run: `python3 -m pytest tests/integration/test_knowledge_workflow_contract.py::test_follow_up_refines_one_topic_record_and_preserves_question_history -v`

Expected: FAIL until the workflow fixture represents a single evolving topic record and every committed scenario matches the deterministic generator.

- [ ] **Step 3: Complete the fixture and audit the four contracts against the spec**

Add the exact follow-up record fixture required by the test. Implement `generate_scenarios.py` with exactly one mutually exclusive option, `--write` or `--check`, no network access, and no current-time input. A declarative `ScenarioSpec` table owns every scenario named in Tasks 1–6. Generate fixed source bytes and timestamps, compute content/output hashes from those bytes, construct canonical `SourceRecord` instances and serialize `SourceRecord.to_dict()`, invoke `LedgerStore.write_summary`, and invoke `render_wiki_index` over the generated logical documents. Build each broken scenario as a valid base plus exactly one named deterministic mutation. Include the scenario README, adoption prior-original, workflow input, sync envelope, both workflow event traces, both exact evidence packets, and the external `tests/fixtures/wiki/workflow/what-is-alpha.md` acceptance record in the generated set. `--check` generates into `tempfile.TemporaryDirectory`, compares the complete generated relative path set, regular-file bytes, executable bits, and symlink targets against the declared outputs beneath `tests/fixtures/wiki/scenarios/` plus that one external workflow record, and prints a concise diff without modifying the worktree. `--write` replaces only those explicitly declared generated files and removes only stale paths previously declared by the generator; it never touches a user `sources/` or `wiki/` tree.

Confirm the schema documents say all of the following in direct normative language: source sync precedes substantive research; the corpus revision is persisted in a question record; Q&A records evolve by topic rather than conversational turn; factual claims use the citation definition grammar; first meaningful related-topic occurrences link within sections; declared page/question relationships are reciprocal; ambiguity is reported rather than auto-resolved; normal and full validation behavior differs only by full rehashing; and approved web evidence restarts all three source passes.

- [ ] **Step 4: Run all milestone tests and commands**

Run: `python3 tests/fixtures/wiki/generate_scenarios.py --check && python3 -m pytest tests/unit/test_evidence.py tests/unit/test_frontmatter.py tests/unit/test_markdown.py tests/unit/test_search.py tests/unit/test_search_runs.py tests/unit/test_wiki_models.py tests/unit/test_citations.py tests/unit/test_graph.py tests/unit/test_wiki_transaction.py tests/unit/test_wiki_evidence.py tests/integration/test_brain_search.py tests/integration/test_wiki_validation.py tests/integration/test_knowledge_workflow_contract.py -v && python3 -m pytest -q && ./brain --json validate --full`

Expected: every pytest test passes; the valid fixture emits no error issues; each broken fixture reports its exact code; no test makes a network request or invokes a subprocess with `shell=True`.

- [ ] **Step 5: Review the completed milestone without committing**

```bash
git diff --check
git status --short
git diff -- brainlib/evidence.py brainlib/frontmatter.py brainlib/markdown.py brainlib/search.py brainlib/search_runs.py brainlib/wiki_models.py brainlib/citations.py brainlib/graph.py brainlib/wiki_transaction.py brainlib/wiki_evidence.py brainlib/validation.py brainlib/cli.py docs/brain/schemas wiki/index.md tests/helpers_knowledge.py tests/conftest.py tests/unit tests/integration/test_brain_search.py tests/integration/test_wiki_validation.py tests/integration/test_knowledge_workflow_contract.py tests/fixtures/wiki
```

Only the last executed plan in the plan set may prepare the single user- or host-requested conversation/PR commit. This milestone never creates one; the plan-set index owns the one-time execution choice and final commit guidance.

## Self-review

**Spec coverage:** Tasks 1 and 7 cover strict owned frontmatter, page/Q&A records, stable evolving topic records, corpus revision, source and wiki evidence packets, and pre-answer sync assumptions. Task 2 covers safe `rg`, literal pattern files, active-representation scope, complete filenames-before-context scans, authenticated resumable pages, bounded spools, and the three named logical-pass interface. Tasks 3 and 5 cover claim-level citations to immutable source/version/derivation/`Anchor` targets and a citation-revalidated wiki fast path. Tasks 4 and 5 cover complete paged candidate discovery, transaction-bound run proofs, first meaningful section links, reciprocal page/question relationships, ambiguous aliases, self-links, unresolved-link reporting, deterministic `wiki/index.md`, approved removal boundaries, interrupted multi-file recovery, and complete graph validation. Task 6 preserves the exact discovery → expansion → verification rule, the Q&A-updating wiki fast path, unconditional post-sync apply, and the approval-gated web restart rule for milestone 5.

**Intentional scope boundary:** shared skills, Codex/Claude adapters, LLM scenario evaluations, README/BRAIN/AGENTS routing, and final template acceptance are milestone 5. This plan creates only the deterministic contracts and fixtures they require.

**Type consistency:** `SearchPassName` values are only `discovery`, `expansion`, and `verification`; `SearchRequest.pass_name`, `SearchPassRecord.name`, CLI `--pass`, and the evidence-packet JSON use those identical values. `SearchResult`/`SearchRunProof` and `LinkCandidateResult`/`LinkCandidateRunProof` preserve their revision/run/manifest/page bindings. `EvidenceItem`, `Citation`, `RevalidatedCitation`, and document-qualified `CitationRef` use canonical `source_id`, `content_sha256`, `derivation_id`, and `Anchor` consistently. `validate_citations`, `validate_graph`, and `validate_wiki` all return the exact `brainlib.diagnostics.ValidationReport` shape with `checks`, `issues`, and `corpus_revision`; `validate_repository` alone composes the final report tuple.

**Placeholder/fence/command review:** Every task identifies created/modified files, reusable `tests/helpers_knowledge.py` builders or fixture-only `tests/conftest.py` changes, a failing test, `python3 -m pytest` verification command, and a non-committing scoped review. Nested Markdown examples use a four-backtick outer fence. JSON examples use root-first `--json` and read result data at `payload["data"]`.
