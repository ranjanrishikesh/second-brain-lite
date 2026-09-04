"""A deliberately small, strict YAML-frontmatter reader.

The repository only needs strings and inline string lists.  Keeping that
grammar here avoids accepting a broad YAML language whose coercions would make
the Markdown contracts ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class FrontmatterError(ValueError):
    pass


@dataclass(frozen=True)
class FrontmatterDocument:
    data: Mapping[str, object]
    body: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


def parse_frontmatter(path: Path, *, text: str | None = None) -> FrontmatterDocument:
    """Parse schema frontmatter from *path* or a staged text override."""

    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise FrontmatterError(f"{path}: Markdown must be UTF-8") from error
    if not isinstance(text, str):
        raise FrontmatterError(f"{path}: text must be a string")
    if "\t" in text:
        raise FrontmatterError(f"{path}: tabs are not permitted in frontmatter")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise FrontmatterError(f"{path}: missing opening frontmatter delimiter")
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
    if closing is None:
        raise FrontmatterError(f"{path}: missing closing frontmatter delimiter")
    data: dict[str, object] = {}
    for line_number, raw in enumerate(lines[1:closing], 2):
        line = raw.rstrip("\r\n")
        if not line or line.lstrip() != line or ":" not in line:
            raise FrontmatterError(f"{path}:{line_number}: malformed frontmatter entry")
        key, value = line.split(":", 1)
        if not key or key.strip() != key or any(char.isspace() for char in key):
            raise FrontmatterError(f"{path}:{line_number}: malformed frontmatter key")
        if key in data:
            raise FrontmatterError(f"{path}:{line_number}: duplicate key: {key}")
        if not value.startswith(" ") or value.startswith("  "):
            raise FrontmatterError(f"{path}:{line_number}: malformed frontmatter value")
        data[key] = _parse_value(value[1:], path, line_number)
    return FrontmatterDocument(data, "".join(lines[closing + 1 :]))


def _parse_value(value: str, path: Path, line_number: int) -> str | tuple[str, ...]:
    if not value or value.strip() != value:
        raise FrontmatterError(f"{path}:{line_number}: frontmatter value must be nonempty")
    if value.startswith("[") or value.endswith("]"):
        return _parse_list(value, path, line_number)
    if value.startswith(("{", "-", "|", ">")) or ": " in value:
        raise FrontmatterError(f"{path}:{line_number}: nested values are not permitted")
    if value[0] in "'\"" or value[-1] in "'\"":
        return _parse_quoted_scalar(value, path, line_number)
    if any(char in value for char in "[]{}"):
        raise FrontmatterError(f"{path}:{line_number}: malformed scalar")
    return value


def _parse_list(value: str, path: Path, line_number: int) -> tuple[str, ...]:
    if not (value.startswith("[") and value.endswith("]")):
        raise FrontmatterError(f"{path}:{line_number}: malformed inline list")
    content = value[1:-1]
    if not content:
        return ()
    items: list[str] = []
    index = 0
    while index < len(content):
        if content[index] == " ":
            raise FrontmatterError(f"{path}:{line_number}: malformed inline list spacing")
        if content[index] in "'\"":
            quote = content[index]
            end = index + 1
            while end < len(content) and content[end] != quote:
                if content[end] == "\\":
                    raise FrontmatterError(f"{path}:{line_number}: escapes are not supported")
                end += 1
            if end == len(content):
                raise FrontmatterError(f"{path}:{line_number}: malformed list quoting")
            item = _parse_quoted_scalar(
                content[index : end + 1], path, line_number
            )
            index = end + 1
        else:
            end = content.find(",", index)
            if end == -1:
                end = len(content)
            item = content[index:end]
            if (
                not item
                or item.strip() != item
                or ": " in item
                or any(char in item for char in "[]{}'\"")
            ):
                raise FrontmatterError(f"{path}:{line_number}: malformed inline list value")
            index = end
        items.append(item)
        if index == len(content):
            break
        if content[index] != ",":
            raise FrontmatterError(f"{path}:{line_number}: malformed inline list")
        index += 1
        if index == len(content):
            raise FrontmatterError(f"{path}:{line_number}: malformed inline list")
        if content[index] == " ":
            index += 1
            if index == len(content) or content[index] == " ":
                raise FrontmatterError(f"{path}:{line_number}: malformed inline list spacing")
    return tuple(items)


def _parse_quoted_scalar(value: str, path: Path, line_number: int) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "'\"":
        raise FrontmatterError(f"{path}:{line_number}: malformed scalar quoting")
    interior = value[1:-1]
    if (
        not interior
        or not interior.strip()
        or "\\" in interior
        or any(char in interior for char in "'\"")
    ):
        raise FrontmatterError(f"{path}:{line_number}: malformed scalar quoting")
    return interior
