from pathlib import Path, PurePosixPath

import pytest

from brainlib.layout import RepoPaths, is_ignored_source_path


def test_discover_finds_repository_from_nested_directory(repo_root: Path) -> None:
    nested = repo_root / "sources" / "raw" / "notes"
    nested.mkdir(parents=True)

    assert RepoPaths.discover(nested).root == repo_root


def test_discover_populates_the_canonical_repository_paths(repo_root: Path) -> None:
    paths = RepoPaths.discover(repo_root)

    assert paths == RepoPaths(
        root=repo_root,
        raw=repo_root / "sources/raw",
        extracted=repo_root / "sources/extracted",
        ledger_dir=repo_root / "sources/ledger",
        ledger_summary=repo_root / "sources/ledger.md",
        wiki_pages=repo_root / "wiki/pages",
        wiki_questions=repo_root / "wiki/questions",
        registry=repo_root / "config/extractors.toml",
        lock=repo_root / ".brain/source-write.lock",
    )
    assert paths.repo_relative(paths.wiki_pages / "example.md") == PurePosixPath(
        "wiki/pages/example.md"
    )


def test_discover_rejects_a_directory_without_both_root_markers(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").touch()

    with pytest.raises(
        FileNotFoundError, match="^second-brain repository root not found$"
    ):
        RepoPaths.discover(tmp_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        PurePosixPath("_versions/src_a/file.pdf"),
        PurePosixPath("_web/site/page.html"),
        PurePosixPath(".gitkeep"),
        PurePosixPath("notes/.keep"),
        PurePosixPath("notes/.DS_Store"),
        PurePosixPath("notes/.brain-tmp-import"),
        PurePosixPath("notes/source.brain.lock"),
    ],
)
def test_reserved_source_paths_are_ignored(relative_path: PurePosixPath) -> None:
    assert is_ignored_source_path(relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        PurePosixPath("letters/2026-09-04.md"),
        PurePosixPath("archive/_versions/src_a/file.pdf"),
        PurePosixPath("archive/_web/site/page.html"),
        PurePosixPath("letters/_versions-notes.md"),
        PurePosixPath("letters/.keep.txt"),
        PurePosixPath("letters/source.brain.lock.old"),
    ],
)
def test_user_source_paths_are_not_ignored(relative_path: PurePosixPath) -> None:
    assert not is_ignored_source_path(relative_path)
