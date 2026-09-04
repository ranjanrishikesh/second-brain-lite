import shutil
import stat
from pathlib import Path, PurePosixPath

import pytest

import brainlib.validation as validation
from brainlib.diagnostics import ValidationIssue, ValidationReport
from brainlib.layout import RepoPaths
from brainlib.validation import validate_template_layout


def test_pristine_template_passes_structural_validation(repo_root: Path) -> None:
    report = validate_template_layout(RepoPaths.discover(repo_root))

    assert report.ok
    assert report.checks == ("template-layout",)
    assert report.corpus_revision is None


def test_structural_validation_accepts_populated_user_corpus(repo_root: Path) -> None:
    source = repo_root / "sources/raw/notes/a.txt"
    source.parent.mkdir(parents=True)
    source.write_text("private note\n")
    (repo_root / "wiki/pages/example.md").write_text("# Example\n")

    report = validate_template_layout(RepoPaths.discover(repo_root))

    assert report.ok
    assert report.checks == ("template-layout",)


def test_validate_reports_wrong_claude_link(repo_root: Path) -> None:
    (repo_root / "CLAUDE.md").unlink()
    (repo_root / "CLAUDE.md").write_text("copied instructions\n")

    report = validate_template_layout(RepoPaths.discover(repo_root))

    issue = next(
        item for item in report.issues if item.code == "claude_entrypoint_invalid"
    )
    assert issue.path == PurePosixPath("CLAUDE.md")
    assert not report.ok


def test_validate_reports_non_executable_launcher(repo_root: Path) -> None:
    launcher = repo_root / "brain"
    launcher.chmod(launcher.stat().st_mode & ~stat.S_IXUSR)

    report = validate_template_layout(RepoPaths.discover(repo_root))

    assert "launcher_invalid" in {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    "relative",
    [
        "sources/raw/_versions",
        "sources/raw/_web",
        "sources/extracted",
        "sources/ledger",
        "wiki/pages",
        "wiki/questions",
        ".agents/skills",
        ".claude/skills",
        ".codex/agents",
        ".claude/agents",
    ],
)
def test_validate_reports_each_missing_required_directory(
    repo_root: Path, relative: str
) -> None:
    shutil.rmtree(repo_root / relative)

    report = validate_template_layout(RepoPaths.discover(repo_root))

    issues = {(issue.code, issue.path) for issue in report.issues}
    assert ("required_directory_missing", PurePosixPath(relative)) in issues


@pytest.mark.parametrize(
    "relative",
    [
        "docs/brain/schemas/source-record.v1.schema.json",
        "docs/brain/schemas/page-frontmatter.v1.schema.json",
        "docs/brain/schemas/question-frontmatter.v1.schema.json",
    ],
)
def test_validate_reports_each_missing_required_schema(
    repo_root: Path, relative: str
) -> None:
    (repo_root / relative).unlink()

    report = validate_template_layout(RepoPaths.discover(repo_root))

    issues = {(issue.code, issue.path) for issue in report.issues}
    assert ("required_schema_missing", PurePosixPath(relative)) in issues


def test_validate_reports_missing_wiki_index(repo_root: Path) -> None:
    (repo_root / "wiki/index.md").unlink()

    report = validate_template_layout(RepoPaths.discover(repo_root))

    issues = {(issue.code, issue.path) for issue in report.issues}
    assert ("wiki_index_missing", PurePosixPath("wiki/index.md")) in issues


def test_merge_reports_preserves_first_check_and_report_issue_order() -> None:
    first_issue = ValidationIssue("warning", "first", "First issue")
    second_issue = ValidationIssue("error", "second", "Second issue")
    revision = "a" * 64

    merged = validation.merge_reports(
        ValidationReport(("layout", "ledger"), (first_issue,), revision),
        ValidationReport(("ledger", "graph"), (second_issue,), None),
        ValidationReport(("layout",), (), revision),
    )

    assert merged == ValidationReport(
        ("layout", "ledger", "graph"),
        (first_issue, second_issue),
        revision,
    )


def test_merge_reports_appends_an_error_for_conflicting_revisions() -> None:
    existing = ValidationIssue("warning", "stale", "Stale source")

    merged = validation.merge_reports(
        ValidationReport(("ledger",), (existing,), "a" * 64),
        ValidationReport(("citations",), (), None),
        ValidationReport(("graph",), (), "b" * 64),
    )

    assert merged.checks == ("ledger", "citations", "graph")
    assert merged.issues[:-1] == (existing,)
    assert merged.issues[-1].severity == "error"
    assert merged.issues[-1].code == "corpus_revision_conflict"
    assert merged.corpus_revision is None
    assert not merged.ok


def test_merge_reports_accepts_no_reports() -> None:
    assert validation.merge_reports() == ValidationReport((), (), None)
