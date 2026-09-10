"""Parsing helpers for repository-owned agent instructions."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat


class FrontmatterError(ValueError):
    """Raised when a skill document has invalid scalar frontmatter."""


@dataclass(frozen=True)
class SkillDocument:
    """A canonical skill document with its validated metadata."""

    name: str
    description: str
    body: str
    path: PurePosixPath


def parse_scalar_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """Load the deliberately small YAML-like frontmatter grammar for skills."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FrontmatterError(f"{path}: cannot read skill document: {error}") from error
    return _parse_scalar_frontmatter_text(path, text)


def _parse_scalar_frontmatter_text(path: Path, text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise FrontmatterError(f"{path}: missing opening frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise FrontmatterError(f"{path}: missing closing frontmatter delimiter") from error

    values: dict[str, str] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise FrontmatterError(f"{path}:{number}: expected scalar key: value")
        key, raw_value = line.split(":", 1)
        key, value = key.strip(), raw_value.strip()
        if key in values:
            raise FrontmatterError(f"{path}:{number}: duplicate key: {key}")
        normalized_value = value.strip("\"'")
        if not key or not value or value[0] in "[{|>" or not normalized_value:
            raise FrontmatterError(
                f"{path}:{number}: only nonempty scalar values are allowed"
            )
        values[key] = normalized_value

    return values, "\n".join(lines[end + 1 :]).strip() + "\n"


def load_skill(root: Path, path: Path) -> SkillDocument:
    """Load a canonical skill and enforce its directory/name identity."""

    metadata, body = parse_scalar_frontmatter(path)
    return _skill_document(root, path, metadata, body)


def load_canonical_skill(root: Path, path: Path) -> SkillDocument:
    """Safely load a regular canonical skill without following path symlinks."""

    text = _read_regular_file_beneath(root, path)
    metadata, body = _parse_scalar_frontmatter_text(path, text)
    return _skill_document(root, path, metadata, body)


def _skill_document(
    root: Path,
    path: Path,
    metadata: dict[str, str],
    body: str,
) -> SkillDocument:
    missing = {"name", "description"} - metadata.keys()
    if missing:
        raise FrontmatterError(f"{path}: missing {', '.join(sorted(missing))}")
    if metadata["name"] != path.parent.name:
        raise FrontmatterError(
            f"{path}: skill name must match directory {path.parent.name}"
        )
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise FrontmatterError(f"{path}: skill is outside repository root") from error
    return SkillDocument(
        name=metadata["name"],
        description=metadata["description"],
        body=body,
        path=PurePosixPath(relative.as_posix()),
    )


def _read_regular_file_beneath(root: Path, path: Path) -> str:
    """Read ``path`` via no-follow descriptors rooted at ``root``.

    Canonical skills are repository-owned regular files.  Open each directory
    edge without following links, read the final regular file descriptor, and
    recheck the observed edges before accepting its bytes.
    """

    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise FrontmatterError(f"{path}: skill is outside repository root") from error
    if not relative.parts or any(component in {".", ".."} for component in relative.parts):
        raise FrontmatterError(f"{path}: canonical skill path is invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nonblocking = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or directory_flag is None or nonblocking is None:
        raise FrontmatterError(f"{path}: safe canonical skill loading is unsupported")

    descriptors: list[int] = []
    edges: list[tuple[int, str, int, os.stat_result]] = []
    try:
        directory = os.open(root, os.O_RDONLY | directory_flag | nofollow)
        descriptors.append(directory)
        for component in relative.parts[:-1]:
            observed = os.stat(component, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(observed.st_mode):
                raise FrontmatterError(f"{path}: canonical skill ancestor is not a directory")
            child = os.open(
                component,
                os.O_RDONLY | directory_flag | nofollow,
                dir_fd=directory,
            )
            descriptors.append(child)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or not _same_file(observed, opened):
                raise FrontmatterError(f"{path}: canonical skill ancestor changed")
            edges.append((directory, component, child, observed))
            directory = child

        filename = relative.parts[-1]
        observed_file = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(observed_file.st_mode):
            raise FrontmatterError(f"{path}: canonical skill must be a regular file")
        file_descriptor = os.open(
            filename, os.O_RDONLY | nofollow | nonblocking, dir_fd=directory
        )
        descriptors.append(file_descriptor)
        opened_file = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened_file.st_mode) or not _same_file(
            observed_file, opened_file
        ):
            raise FrontmatterError(f"{path}: canonical skill changed")

        chunks: list[bytes] = []
        while chunk := os.read(file_descriptor, 64 * 1024):
            chunks.append(chunk)
        after_file = os.fstat(file_descriptor)
        named_file = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if not (
            _same_file(observed_file, opened_file)
            and _same_file(opened_file, after_file)
            and _same_file(after_file, named_file)
        ):
            raise FrontmatterError(f"{path}: canonical skill changed while read")
        for parent, component, child, observed in reversed(edges):
            if not _same_file(observed, os.fstat(child)) or not _same_file(
                observed,
                os.stat(component, dir_fd=parent, follow_symlinks=False),
            ):
                raise FrontmatterError(f"{path}: canonical skill ancestor changed")
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise FrontmatterError(f"{path}: cannot safely read canonical skill: {error}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino
