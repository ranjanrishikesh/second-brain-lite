from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_agents_routes_six_request_classes_without_copying_policies() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in (
        "initialize this brain",
        "substantive knowledge question",
        "public-web research",
        "wiki maintenance",
        "handoff or PR",
        "repository development",
    ):
        assert phrase in text
    assert len(text.splitlines()) <= 90
    assert "CLAUDE.md" in text


def test_readme_documents_complete_empty_template_journey() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    ordered = (
        "Create a private repository",
        "sources/raw/",
        "initialize this brain",
        "dependency",
        "sources/ledger.md",
        "Ask a question",
        "Add new sources",
        "validate --full",
    )
    offsets = [text.index(item) for item in ordered]
    assert offsets == sorted(offsets)
    for warning in (
        "ordinary Git objects",
        "file-size limits",
        "Git history",
        "public web",
        "one logical commit",
    ):
        assert warning in text
    assert "does not reconvert unchanged sources" in text
    assert (
        "Commit `sources/raw/`, `sources/extracted/`, `sources/ledger/`, "
        "`sources/ledger.md`, and `wiki/`"
    ) in text
    for command in (
        "./brain --json doctor",
        "./brain --json init",
        "./brain --json sync",
        "./brain --json status",
        "./brain --json search --scope wiki --term",
        "./brain --json search --scope sources --pass discovery --term",
        "./brain --json search --cursor",
        "./brain --json source snapshot-url --source-id",
        "./brain --json source snapshot-url --url",
        "--rendered-staging-path",
        "./brain --json source adopt-version",
        "./brain --json source register-extraction",
        "./brain --json source consume-sync-result --result-id",
        "./brain --json links candidates",
        "./brain --json links candidates --cursor",
        "./brain --json links check",
        "./brain --json wiki apply --manifest",
        "./brain --json validate --full",
    ):
        assert command in text
    assert "Show `doctor`" not in text
    assert text.index("source consume-sync-result") < text.index("source acknowledge-sync-result")


def test_readme_documents_the_committed_extractor_allowlist_without_drift() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    registry = tomllib.loads((ROOT / "config/extractors.toml").read_text(encoding="utf-8"))
    for extractor in registry["extractors"]:
        assert f"`{extractor['id']}`" in readme
        for extension in extractor["extensions"]:
            assert f"`{extension}`" in readme
    assert "`config/extractors.toml` is authoritative" in readme
    assert "Never edit the allowlist without explicit approval" in readme
    for strategy in (
        "Built-in UTF-8 Markdown/text normalization",
        "Built-in deterministic Markdown tables",
        "Built-in sorted JSON Markdown representation",
        "Pandoc → standard-library HTML parser",
        "`pdftotext -layout` → PyMuPDF",
        "Pandoc → python-docx",
        "python-pptx → isolated headless LibreOffice",
        "openpyxl → isolated headless LibreOffice",
        "Tesseract for text-focused images → approved agent handoff for complex images",
        "Explicitly approved immutable web capture → normal extraction",
    ):
        assert strategy in readme


def test_brain_manual_links_every_canonical_document() -> None:
    text = (ROOT / "BRAIN.md").read_text(encoding="utf-8")
    for relative in (
        "docs/brain/policies/source-handling.md",
        "docs/brain/policies/approvals.md",
        "docs/brain/policies/citations.md",
        "docs/brain/policies/wiki.md",
        "docs/brain/policies/web.md",
        "docs/brain/workflows/initialize.md",
        "docs/brain/workflows/synchronize.md",
        "docs/brain/workflows/answer.md",
        "docs/brain/workflows/web-research.md",
        "docs/brain/workflows/wiki-maintenance.md",
        "docs/brain/workflows/validate.md",
        "docs/brain/schemas/README.md",
        "docs/brain/schemas/source-record.v1.schema.json",
        "docs/brain/schemas/page-frontmatter.v1.schema.json",
        "docs/brain/schemas/question-frontmatter.v1.schema.json",
        "docs/brain/schemas/citation.md",
        "docs/brain/schemas/evidence-packet.md",
        "docs/brain/schemas/wiki-page.md",
        "docs/brain/schemas/question-record.md",
        "docs/brain/agent-briefs/source-researcher.md",
        "docs/brain/agent-briefs/source-ingester.md",
        "docs/brain/agent-briefs/wiki-curator.md",
        "docs/brain/agent-briefs/brain-auditor.md",
    ):
        assert relative in text
    for heading in (
        "## Authority",
        "## Directory roles",
        "## Evidence lifecycle",
        "## Question lifecycle",
        "## Approval boundary",
        "## Canonical document index",
    ):
        assert heading in text


def test_initialize_and_sync_docs_state_the_agent_and_network_boundaries() -> None:
    initialize = (ROOT / "docs/brain/workflows/initialize.md").read_text(encoding="utf-8")
    synchronize = (ROOT / "docs/brain/workflows/synchronize.md").read_text(encoding="utf-8")
    assert "## Agent completion boundary" in initialize
    assert "./brain --json init" in initialize
    assert "`needs_agent`" in initialize
    assert "## Network and citation-rewrite boundary" in synchronize
    assert "./brain --json sync" in synchronize
    assert "never performs network access" in synchronize
    assert "citation_rewrites" in synchronize


def test_user_docs_preserve_manifest_and_evidence_packet_rulings() -> None:
    manual = (ROOT / "BRAIN.md").read_text(encoding="utf-8")
    initialize = (ROOT / "docs/brain/workflows/initialize.md").read_text(encoding="utf-8")
    synchronize = (ROOT / "docs/brain/workflows/synchronize.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (manual, initialize, synchronize, readme):
        assert './brain --json source consume-sync-result --result-id "$result_id"' in text
        assert "manifest_path" in text
        assert "source acknowledge-sync-result" in text
        assert "handoff_delivery" in text
    for text in (manual, initialize, readme):
        assert "data.registration.active_representation" in text
        assert "SnapshotResult.active_representation" in text
    assert "no supported wiki record proceeds directly to the three source passes" in manual
    assert "must not construct an empty `WikiEvidencePacket`" in manual


def test_user_docs_preserve_activation_freshness_and_readiness_boundaries() -> None:
    manual = (ROOT / "BRAIN.md").read_text(encoding="utf-8")
    initialize = (ROOT / "docs/brain/workflows/initialize.md").read_text(encoding="utf-8")
    synchronize = (ROOT / "docs/brain/workflows/synchronize.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text in (manual, initialize, readme):
        assert "handoff.kind == rendered_web_capture" in text
        assert "normal allowlisted processing directly activates" in text
        assert "non-null `SnapshotResult.active_representation`" in text
        assert "matching corpus revision" in text
        assert "handoff.kind == extraction" in text
        assert "data.registration.active_representation" in text
        assert "immutable snapshot may remain null" in text
    assert "rendered capture emits an extraction fallback" not in readme
    for text in (manual, synchronize):
        assert "only when accumulated newly-active source IDs is nonempty" in text
        assert "otherwise proceed directly to wiki search" in text
    readiness = (
        "no unresolved pending, failed, unsupported, warning, integrity, approval, "
        "agent, or coverage gaps"
    )
    for text in (manual, initialize, synchronize, readme):
        assert readiness in text
        assert "./brain --json validate --full" in text
