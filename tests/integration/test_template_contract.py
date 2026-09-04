import stat
from pathlib import Path


def test_claude_entrypoint_is_a_symlink_to_agents_md(repo_root: Path) -> None:
    assert (repo_root / "CLAUDE.md").is_symlink()
    assert (repo_root / "CLAUDE.md").resolve() == repo_root / "AGENTS.md"


def test_reserved_directories_have_only_sentinels(repo_root: Path) -> None:
    for relative in (
        "sources/raw/_versions",
        "sources/raw/_web",
        "sources/extracted",
        "sources/ledger",
        "wiki/pages",
        "wiki/questions",
    ):
        assert [path.name for path in (repo_root / relative).iterdir()] == [".gitkeep"]


def test_launcher_is_executable(repo_root: Path) -> None:
    assert (repo_root / "brain").stat().st_mode & stat.S_IXUSR


def test_distribution_template_has_no_user_content(repo_root: Path) -> None:
    assert (repo_root / "wiki/index.md").read_text() == (
        "# Second Brain Lite\n\n## Pages\n\n## Questions\n"
    )
    assert (
        (repo_root / "sources/ledger.md").read_text()
        == "# Source Ledger\n\nNot initialized. Run `./brain init`.\n"
    )
    assert list((repo_root / "wiki/pages").iterdir()) == [
        repo_root / "wiki/pages/.gitkeep"
    ]
