from pathlib import Path

import pytest

from brainlib.frontmatter import FrontmatterError, parse_frontmatter


def test_parse_frontmatter_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\nid: one\nid: two\n---\nbody\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="duplicate key: id"):
        parse_frontmatter(path)


def test_parse_frontmatter_parses_schema_scalar_and_inline_list_values(tmp_path: Path) -> None:
    path = tmp_path / "good.md"
    path.write_text(
        "---\nid: alpha\naliases: [Alpha, 'A, B']\ncreated: 2026-09-04\n---\nbody\n",
        encoding="utf-8",
    )
    parsed = parse_frontmatter(path)
    assert parsed.data == {
        "id": "alpha",
        "aliases": ("Alpha", "A, B"),
        "created": "2026-09-04",
    }
    assert parsed.body == "body\n"


@pytest.mark.parametrize("text", ["id: one\n", "---\nid: one\n", "---\nid:\n---\n"])
def test_parse_frontmatter_rejects_missing_delimiters_and_wrong_scalar_values(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(FrontmatterError):
        parse_frontmatter(tmp_path / "bad.md", text=text)


@pytest.mark.parametrize(
    "entry",
    (
        "aliases: [a: b]",
        'title: "a"b"',
    ),
)
def test_parse_frontmatter_rejects_nested_and_malformed_scalar_forms(
    tmp_path: Path, entry: str
) -> None:
    with pytest.raises(FrontmatterError):
        parse_frontmatter(tmp_path / "bad.md", text=f"---\n{entry}\n---\n")
