from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self


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
    def discover(cls, start: Path) -> Self:
        resolved = start.resolve()
        for candidate in (resolved, *resolved.parents):
            if (candidate / "pyproject.toml").is_file() and (
                candidate / "AGENTS.md"
            ).is_file():
                return cls(
                    root=candidate,
                    raw=candidate / "sources/raw",
                    extracted=candidate / "sources/extracted",
                    ledger_dir=candidate / "sources/ledger",
                    ledger_summary=candidate / "sources/ledger.md",
                    wiki_pages=candidate / "wiki/pages",
                    wiki_questions=candidate / "wiki/questions",
                    registry=candidate / "config/extractors.toml",
                    lock=candidate / ".brain/source-write.lock",
                )
        raise FileNotFoundError("second-brain repository root not found")

    def repo_relative(self, path: Path) -> PurePosixPath:
        relative = path.resolve().relative_to(self.root)
        return PurePosixPath(relative.as_posix())


def is_ignored_source_path(relative_path: PurePosixPath) -> bool:
    basename = relative_path.name
    return (
        basename in {".gitkeep", ".keep", ".DS_Store"}
        or relative_path.parts[:1] in {("_versions",), ("_web",)}
        or basename.startswith(".brain-tmp-")
        or basename.endswith(".brain.lock")
    )
