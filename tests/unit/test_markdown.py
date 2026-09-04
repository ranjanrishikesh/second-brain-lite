from types import SimpleNamespace

import pytest

from brainlib import markdown as markdown_module
from brainlib.markdown import (
    MarkdownHeading,
    MarkdownLink,
    MarkdownScan,
    MarkdownSourceSpan,
    scan_markdown,
)


def test_scan_markdown_does_not_report_link_or_citation_inside_code() -> None:
    scan = scan_markdown(
        """[Alpha](alpha.md)[^cite-alpha-line-1]
```python
ignored = "[Alpha](alpha.md)[^cite-alpha-line-1]"
```
"""
    )
    assert [link.destination for link in scan.links] == ["alpha.md"]
    assert [marker.citation_id for marker in scan.citation_markers] == [
        "cite-alpha-line-1"
    ]


def test_scan_markdown_excludes_frontmatter_and_indented_code() -> None:
    scan = scan_markdown(
        "---\ntitle: [Ignored](ignored.md)[^ignored]\n---\n# Alpha\n    [No](no.md)[^no]\n## Section\n[Yes](yes.md)[^yes]\n"
    )
    assert [heading.text for heading in scan.headings] == ["Alpha", "Section"]
    assert [link.destination for link in scan.links] == ["yes.md"]
    assert [marker.citation_id for marker in scan.citation_markers] == ["yes"]


def test_scan_markdown_keeps_scanning_disabled_until_a_complete_closing_fence() -> None:
    scan = scan_markdown(
        """```python
```not-a-close
[Hidden](hidden.md)[^hidden]
```
[Visible](visible.md)[^visible]
"""
    )
    assert [link.destination for link in scan.links] == ["visible.md"]
    assert [marker.citation_id for marker in scan.citation_markers] == ["visible"]


def test_scanner_keeps_definition_positions_separate_from_colon_suffixed_claims():
    scan = scan_markdown("Claim.[^missing]: prose\n\n[^source]: a definition\n")
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("missing", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [
        ("source", 3)
    ]


@pytest.mark.parametrize(
    "backslashes,visible", [(0, True), (1, False), (2, True), (3, False), (4, True)]
)
def test_scanner_respects_escape_parity_for_links_and_markers(backslashes, visible):
    prefix = "\\" * backslashes
    scan = scan_markdown(prefix + "[Link](target.md) " + prefix + "[^claim]: prose")
    assert bool(scan.links) is visible
    assert bool(scan.citation_markers) is visible


def test_scanner_masks_only_inline_spans_in_their_own_blocks():
    scan = scan_markdown(
        "    `example\nReal [Link](real.md)[^real] `unmatched\n\n`inline\n[Code](code.md)[^code]\nspan`\n"
    )
    assert [item.citation_id for item in scan.citation_markers] == ["real"]
    assert [item.destination for item in scan.links] == ["real.md"]


def test_scanner_reports_indented_atx_and_setext_heading_locations():
    scan = scan_markdown(
        " # Title\n\n  ## Sources\n\nLater\n---\n\n    ## Code\n~~~\nHidden\n===\n~~~\n"
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [
        (1, "Title", 1),
        (2, "Sources", 3),
        (2, "Later", 5),
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_scanner_exposes_complete_physical_heading_spans(newline):
    scan = scan_markdown(
        newline.join(
            [
                "---",
                "title: Heading spans",
                "---",
                "# ATX",
                "",
                "> First line",
                "> second line",
                "> ---",
                "",
            ]
        )
    )

    assert scan.headings == (
        MarkdownHeading(1, "ATX", 4),
        MarkdownHeading(2, "First line second line", 6),
    )
    assert [getattr(heading, "end_line", None) for heading in scan.headings] == [
        4,
        8,
    ]
    assert repr(scan.headings[0]) == "MarkdownHeading(level=1, text='ATX', line=4)"
    assert scan.diagnostics == ()


def test_scanner_definition_cannot_be_hidden_by_preceding_unmatched_tick():
    scan = scan_markdown(
        "``unfinished paragraph\n[^cite]: source_id: `src_value`; [original](original.txt)[^real]\n"
    )
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [
        ("cite", 2)
    ]
    assert [item.destination for item in scan.links] == ["original.txt"]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("real", 2)
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize(
    "opener,continuation",
    [
        ("- ", "  "),
        ("+ ", "  "),
        ("* ", "  "),
        ("1. ", "   "),
        ("10. ", "    "),
        ("  - ", "    "),
        ("- ", ""),
    ],
)
def test_review2_list_paragraph_keeps_multiline_inline_code(opener, continuation):
    scan = scan_markdown(
        opener
        + "`start\n"
        + continuation
        + "[Example](example.md)[^example]\n"
        + continuation
        + "end` [Real](real.md)[^real]\n"
    )
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("real", 3)
    ]
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Real", "real.md", 3)
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        "\n",
        "- second item\n",
        "  - nested item\n",
        "> separate quote\n",
        "  # Heading\n",
        "  ***\n",
        "  ---\n",
        "  Title\n  ===\n",
        "  ~~~\n  `fenced\n  ~~~\n",
        "      `indented code\n",
    ],
)
def test_review2_list_ticks_do_not_cross_distinct_blocks(boundary):
    scan = scan_markdown(
        "- `unfinished\n" + boundary + "  Claim [Real](real.md)[^real] `unmatched\n"
    )
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("real", len(boundary.splitlines()) + 2)
    ]
    assert [item.destination for item in scan.links] == ["real.md"]


@pytest.mark.parametrize(
    "markdown,label,destination",
    [
        ("[Alpha](foo`bar`.md)", "Alpha", "foo`bar`.md"),
        ("[`Alpha`](alpha.md)", "`Alpha`", "alpha.md"),
        ("[A `label` here](foo`bar`.md)", "A `label` here", "foo`bar`.md"),
    ],
)
def test_review2_link_tokens_keep_original_source_bytes(markdown, label, destination):
    scan = scan_markdown("\n" + markdown)
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        (label, destination, 2)
    ]


@pytest.mark.parametrize(
    "example",
    [
        "`[Hidden](hidden.md)`",
        "``[`Hidden`](foo`bar`.md)``",
        "`start\n[Hidden](hidden.md)\nend`",
    ],
)
def test_review2_original_token_bytes_do_not_revive_code_links(example):
    scan = scan_markdown(example + "\n\n[Visible](visible.md)")
    assert [(item.text, item.destination) for item in scan.links] == [
        ("Visible", "visible.md")
    ]


def test_review3_list_fence_reprocesses_first_outside_line():
    scan = scan_markdown("- ~~~\n  ignored code\n\nOutside [Real](real.md)[^missing]\n")
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("missing", 4)
    ]
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Real", "real.md", 4)
    ]


@pytest.mark.parametrize("fence", ["~~~", "```"])
@pytest.mark.parametrize(
    "prefix,external,line",
    [
        ("- {fence}\n  ignored\n", "- ", 3),
        ("1. {fence}\n   ignored\n", "2. ", 3),
        ("- parent\n  - {fence}\n    ignored\n", "  - ", 4),
        ("- {fence}\n  ignored\n", "> ", 3),
    ],
)
def test_review3_list_fence_ends_before_new_container(prefix, external, line, fence):
    scan = scan_markdown(
        prefix.format(fence=fence) + external + "[Real](real.md)[^missing]\n"
    )
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("missing", line)
    ]
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Real", "real.md", line)
    ]


@pytest.mark.parametrize(
    "example",
    [
        "- ~~~\n  [Hidden](hidden.md)[^hidden]\n  ~~~",
        "~~~\n[Hidden](hidden.md)[^hidden]\n\n~~~",
        "- ~~~\n\n  [Hidden](hidden.md)[^hidden]\n \n\n  [Still](still.md)[^still]\n  ~~~",
        "- ~~~\n  ~~~not-close\n  [Hidden](hidden.md)[^hidden]\n  ~~~~",
        "- parent\n  - ```\n    [Hidden](hidden.md)[^hidden]\n    ```",
        "- `start\n  [Hidden](hidden.md)[^hidden]\n  end`",
    ],
)
def test_review3_closed_fences_and_inline_list_code_preserve_exclusions(example):
    scan = scan_markdown(example + "\n\n[Real](real.md)[^real]\n")
    assert [item.citation_id for item in scan.citation_markers] == ["real"]
    assert [(item.text, item.destination) for item in scan.links] == [
        ("Real", "real.md")
    ]


def test_review3_top_level_fence_does_not_end_at_unindented_or_blank_lines():
    scan = scan_markdown("~~~\n\n[Hidden](hidden.md)[^hidden]\n")
    assert not scan.links
    assert not scan.citation_markers


def test_scan_markdown_uses_commonmark_ranges_for_tabbed_nested_fences() -> None:
    scan = scan_markdown(
        "- parent\n\t- ~~~\n\t  [Hidden](hidden.md)[^hidden]\n"
        "\t  ~~~\n\nParent prose [Real](real.md)[^real]\n"
    )
    assert [(link.text, link.destination, link.line) for link in scan.links] == [
        ("Real", "real.md", 6)
    ]
    assert [(marker.citation_id, marker.line) for marker in scan.citation_markers] == [
        ("real", 6)
    ]


@pytest.mark.parametrize(
    "prefix,outside,line",
    [
        ("- parent\n\t~~~\n\t[Hidden](hidden.md)[^hidden]\n\t~~~\n\n", "", 6),
        ("- parent\n \t~~~\n \t[Hidden](hidden.md)[^hidden]\n \t~~~\n\n", "", 6),
        ("- parent\n  - ~~~\n    [Hidden](hidden.md)[^hidden]\n\n", "  Parent ", 5),
        ("- parent\n\t- ~~~\n\t  [Hidden](hidden.md)[^hidden]\n", "\t- ", 4),
        ("- ~~~\n  [Hidden](hidden.md)[^hidden]\n", "> ", 3),
        ("> - ~~~\n>   [Hidden](hidden.md)[^hidden]\n\n", "", 4),
    ],
    ids=["tab", "space-tab", "parent-continuation", "sibling", "new-quote", "outside-quote"],
)
def test_commonmark_container_code_exclusion_preserves_first_semantic_raw_link(
    prefix, outside, line
):
    scan = scan_markdown(prefix + outside + "[R `raw`](dir%20name/foo`bar`.md)[^real]\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("R `raw`", "dir%20name/foo`bar`.md", line)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("real", line)
    ]


def test_commonmark_heading_tokens_select_atx_and_setext_in_containers():
    scan = scan_markdown(
        "> ## Quoted *Heading* ##\n\n- # Listed\n\n> Setext `raw`\n> ===\n\n"
        "    ## Hidden\n"
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [
        (2, "Quoted *Heading*", 1),
        (1, "Listed", 3),
        (1, "Setext `raw`", 5),
    ]


def test_only_parser_inline_blocks_report_custom_definitions_and_markers():
    scan = scan_markdown(
        "<div>\n[^hidden]: [Hidden](hidden.md)[^hidden]\n</div>\n\n"
        "[^real]: [Raw](raw%20path.md) source details\n"
    )
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [
        ("real", 5)
    ]
    assert not scan.citation_markers
    assert [(item.destination, item.line) for item in scan.links] == [("raw%20path.md", 5)]


@pytest.mark.parametrize("closing", ["---\r\n", ""])
def test_leading_frontmatter_keeps_raw_crlf_line_locations(closing):
    scan = scan_markdown(
        "---\r\n[Metadata](metadata.md)[^metadata]\r\n" + closing
        + "\r\n## Sources\r\n[Raw `label`](raw%20path.md)[^real]\r\n"
    )
    assert [(item.text, item.destination, item.line) for item in scan.links] == (
        [("Raw `label`", "raw%20path.md", 6)] if closing else [
            ("Metadata", "metadata.md", 2),
            ("Raw `label`", "raw%20path.md", 5),
        ]
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [
        (2, "Sources", 5 if closing else 4)
    ]


def test_approved_inline_ranges_fail_closed_for_an_invalid_parser_map() -> None:
    ranges, diagnostics = markdown_module._approved_inline_ranges(
        (SimpleNamespace(type="inline", map=(1, 7)),),
        body_line_count=3,
        line_offset=2,
    )
    assert ranges == ()
    assert diagnostics == (
        markdown_module.MarkdownDiagnostic(
            "markdown_reconciliation_ambiguous",
            "Markdown parser returned an invalid inline source range.",
            4,
        ),
    )


@pytest.mark.parametrize(
    "maps,line",
    [
        ([None], 3),
        ([(0,)], 3),
        ([(0, 1, 2)], 3),
        ([(-1, 1)], 3),
        ([(1, 1)], 4),
        ([(2, 1)], 5),
        ([(True, 2)], 3),
        ([(0.5, 2)], 3),
        ([(0, 2), (1, 3)], 4),
        ([(2, 3), (0, 1)], 3),
        ([(0, 1), (0, 1)], 3),
        ([(8, 9)], 11),
    ],
)
def test_approved_inline_ranges_reject_malformed_overlapping_and_unordered_maps(maps, line):
    ranges, diagnostics = markdown_module._approved_inline_ranges(
        [SimpleNamespace(type="inline", map=value) for value in maps],
        body_line_count=3,
        line_offset=2,
    )
    assert ranges == ()
    assert [(item.code, item.line) for item in diagnostics] == [
        ("markdown_reconciliation_ambiguous", line)
    ]


def test_approved_inline_ranges_only_translates_semantic_maps():
    ranges, diagnostics = markdown_module._approved_inline_ranges(
        [
            SimpleNamespace(type="blockquote_open", map=(0, 4)),
            SimpleNamespace(type="inline", map=[0, 1]),
            SimpleNamespace(type="fence", map=(1, 3)),
            SimpleNamespace(type="inline", map=(3, 4)),
        ],
        body_line_count=4,
        line_offset=3,
    )
    assert ranges == ((3, 4), (6, 7))
    assert diagnostics == ()


def test_scanner_reports_parser_exceptions_as_diagnostics(monkeypatch):
    def fail_parse(_body):
        raise ValueError("invalid parser state")

    monkeypatch.setattr(
        markdown_module, "_PARSER", SimpleNamespace(parse=fail_parse), raising=False
    )
    scan = scan_markdown("Claim [Real](real.md)[^real]\n")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize("source_map", [None, (0, 5), (1, 0)])
def test_scanner_reports_invalid_heading_maps_without_guessing(monkeypatch, source_map):
    monkeypatch.setattr(
        markdown_module,
        "_PARSER",
        SimpleNamespace(parse=lambda _body: [
            SimpleNamespace(type="heading_open", map=source_map, tag="h2"),
            SimpleNamespace(type="inline", map=(0, 1), content="Sources"),
            SimpleNamespace(type="heading_close", map=None, tag="h2"),
        ]),
        raising=False,
    )
    scan = scan_markdown("## Sources\n")
    assert not scan.headings
    assert [item.code for item in scan.diagnostics] == ["markdown_reconciliation_ambiguous"]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\x0b", "\x1c"])
def test_parser_maps_count_only_physical_cr_lf_lines(separator):
    scan = scan_markdown("A" + separator + "B [Raw](raw.md)[^real]\r\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Raw", "raw.md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


def test_non_newline_separators_do_not_change_frontmatter_or_heading_maps():
    scan = scan_markdown(
        "---\r\ntitle: A\u2028B\r\n---\r\n"
        "## Source\u2028heading\r\n[Raw](raw.md)[^real]\r\n"
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [
        (2, "Source\u2028heading", 4)
    ]
    assert [(item.destination, item.line) for item in scan.links] == [("raw.md", 5)]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 5)]
    assert scan.diagnostics == ()


def test_adjacent_parser_inline_maps_remain_separate_blocks():
    ranges, diagnostics = markdown_module._approved_inline_ranges(
        [SimpleNamespace(type="inline", map=(0, 1)), SimpleNamespace(type="inline", map=(1, 2))],
        body_line_count=2,
        line_offset=3,
    )
    assert ranges == ((3, 4), (4, 5))
    assert diagnostics == ()


@pytest.mark.parametrize(
    "link,label,destination",
    [
        (r"[a\]](x.md)", r"a\]", "x.md"),
        ("[a](foo(and)bar.md)", "a", "foo(and)bar.md"),
        (r"[a](foo\(and\).md)", "a", r"foo\(and\).md"),
    ],
)
def test_raw_links_preserve_escaped_labels_and_balanced_destinations(link, label, destination):
    scan = scan_markdown(r"\[literal](x.md) " + link + "[^real]\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        (label, destination, 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


def test_parser_failure_after_frontmatter_reports_body_line_and_preserves_raw_input(monkeypatch):
    parser_inputs = []

    def fail_parse(body):
        parser_inputs.append(body)
        raise ValueError("invalid parser state")

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=fail_parse))
    scan = scan_markdown("---\r\ntitle: A\u2028B\r\n---\r\nBody\r\n")
    assert parser_inputs == ["Body\r\n"]
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 4)
    ]


def test_multiline_setext_heading_uses_paired_inline_content_in_a_container():
    scan = scan_markdown(
        "> First *raw* line\n>   second `raw` line\n> ===\n\n[Real](real.md)[^real]\n"
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [
        (1, "First *raw* line second `raw` line", 1)
    ]
    assert [(item.destination, item.line) for item in scan.links] == [("real.md", 5)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("closing", [None, SimpleNamespace(type="paragraph_close", tag="p")])
def test_heading_reconciliation_requires_a_complete_heading_pair(monkeypatch, closing):
    tokens = [
        SimpleNamespace(type="heading_open", tag="h2", map=(0, 1)),
        SimpleNamespace(type="inline", map=(0, 1), content="Sources"),
    ]
    if closing:
        tokens.append(closing)
    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=lambda _body: tokens))
    scan = scan_markdown("## Sources\n")
    assert not scan.headings
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


def test_failed_heading_reconciliation_prevents_raw_extraction(monkeypatch):
    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=lambda _body: [
        SimpleNamespace(type="heading_open", tag="h2", map=None),
        SimpleNamespace(type="inline", map=(0, 1), content="[Real](real.md)[^real]"),
        SimpleNamespace(type="heading_close", tag="h2", map=None),
    ]))
    scan = scan_markdown("## [Real](real.md)[^real]\n")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


def test_literal_destination_backtick_cannot_mask_a_later_visible_marker():
    scan = scan_markdown("> [A](foo`bar.md)[^real]\n> `later`\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("A", "foo`bar.md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("depth", [20, 50, 100])
def test_commonmark_nesting_above_default_limit_keeps_claims_visible(depth):
    scan = scan_markdown("> " * depth + "Claim [Real](real.md)[^real]\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Real", "real.md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


def test_extreme_nesting_returns_claims_or_a_fail_closed_diagnostic():
    scan = scan_markdown("> " * 1000 + "Claim [Real](real.md)[^real]\n")
    if scan.diagnostics:
        assert [(item.code, item.line) for item in scan.diagnostics] == [
            ("markdown_reconciliation_ambiguous", 1)
        ]
    else:
        assert [(item.text, item.destination, item.line) for item in scan.links] == [
            ("Real", "real.md", 1)
        ]
        assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]


def test_nested_link_syntax_reports_the_actual_commonmark_inner_link():
    scan = scan_markdown("[outer [inner](in.md)](out.md)\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("inner", "in.md", 1)
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize(
    "inner,label,destination",
    [("[inner]()", "inner", ""), ("[](in.md)", "", "in.md")],
)
def test_empty_inner_link_fields_still_prevent_a_false_outer_link(inner, label, destination):
    scan = scan_markdown("[outer " + inner + "](out.md)\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        (label, destination, 1)
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize(
    "text,links,markers",
    [
        (
            "`[Hidden](foo`bar.md)[^real] `later`",
            [],
            [("real", 1)],
        ),
        (
            "``[Hidden](foo`bar.md)[^hidden]`` [Real](real.md)[^real]",
            [("Real", "real.md", 1)],
            [("real", 1)],
        ),
        (
            "[outer `[inner](in.md)`](out.md)",
            [("outer `[inner](in.md)`", "out.md", 1)],
            [],
        ),
        (
            "[outer [inner](foo bar)](out.md)",
            [("outer [inner](foo bar)", "out.md", 1)],
            [],
        ),
        (
            "[`[^hidden]`](x.md)[^real]",
            [("`[^hidden]`", "x.md", 1)],
            [("real", 1)],
        ),
        (
            "[One](one.md) [Two](two.md)[^real]",
            [("One", "one.md", 1), ("Two", "two.md", 1)],
            [("real", 1)],
        ),
    ],
    ids=["existing-code-wins", "code-example", "code-link-in-label", "invalid-link-in-label", "code-marker-in-label", "neighbors"],
)
def test_link_code_precedence_preserves_code_and_neighbor_controls(text, links, markers):
    scan = scan_markdown(text + "\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == markers
    assert scan.diagnostics == ()


def test_deep_mixed_container_preserves_heading_and_fenced_code_exclusion():
    prefix = "> " * 20
    scan = scan_markdown(
        prefix + "- # Deep\n\n"
        + prefix + "~~~\n" + prefix + "[Hidden](hidden.md)[^hidden]\n"
        + prefix + "~~~\n\n[Real](real.md)[^real]\n"
    )
    assert [(item.level, item.text, item.line) for item in scan.headings] == [(1, "Deep", 1)]
    assert [(item.destination, item.line) for item in scan.links] == [("real.md", 7)]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 7)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("destination", ["out.md", "out`side.md"])
def test_image_in_link_label_preserves_outer_link_and_following_marker(destination):
    scan = scan_markdown("[outer ![alt](in.md)](" + destination + ")[^real] `later`\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("outer ![alt](in.md)", destination, 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize(
    "text,links",
    [
        ("![alt](image.md)[^real]", []),
        ("`![alt](image.md)` [Real](real.md)[^real]", [("Real", "real.md", 1)]),
        (r"[outer \![inner](in.md)](out.md)[^real]", [("inner", "in.md", 1)]),
        (r"[outer \\![alt](image.md)](out.md)[^real]", [(r"outer \\![alt](image.md)", "out.md", 1)]),
    ],
    ids=["standalone", "code", "escaped-exclamation", "even-exclamation-escape"],
)
def test_image_lexical_context_does_not_invent_graph_links(text, links):
    scan = scan_markdown(text + "\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]


@pytest.mark.parametrize(
    "image_destination,expected",
    [
        ("image.md", [("outer ![alt [inner](in.md)](image.md)", "out`side.md", 1)]),
        ("bad suffix", [("inner", "in.md", 1)]),
    ],
)
def test_link_inside_image_alt_text_respects_the_image_scope(image_destination, expected):
    outer_destination = "out`side.md" if image_destination == "image.md" else "out.md"
    scan = scan_markdown(
        "[outer ![alt [inner](in.md)](" + image_destination + ")](" + outer_destination
        + ")[^real] `later`\n"
    )
    assert [(item.text, item.destination, item.line) for item in scan.links] == expected
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("backslashes", [0, 1, 2, 3, 4])
def test_escaped_first_tick_keeps_the_remaining_code_delimiter(backslashes):
    opener = "\\" * backslashes + "``"
    closer = "`" if backslashes % 2 else "``"
    scan = scan_markdown(opener + "[Hidden](hidden.md)[^hidden]" + closer + " [Real](real.md)[^real]\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Real", "real.md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("tail", ["", "bad suffix" + ")" * 512], ids=["unclosed", "invalid-balanced"])
def test_malformed_destination_scanning_uses_linear_source_reads(tail):
    class CountedText(str):
        reads = 0

        def __getitem__(self, key):
            value = super().__getitem__(key)
            self.reads += len(value)
            return value

    text = CountedText("[x](" * 512 + tail)
    masked, links = markdown_module._scan_inline_block(text)
    assert masked == text
    assert links == ()
    assert text.reads <= 64 * len(text)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "candidate",
    [
        "[A](foo\\{newline}bar.md)[^real]",
        "[long{newline}label](dest.md)[^real]",
        "[A]({newline}dest.md)[^real]",
        '[A](dest.md "line{newline}title")[^real]',
    ],
    ids=["escaped-destination", "label", "destination-leading-space", "title"],
)
def test_multiline_raw_link_candidates_fail_closed_at_the_original_start_line(newline, candidate):
    scan = scan_markdown(
        newline.join(["---", "title: test", "---", candidate.format(newline=newline), ""])
    )
    assert not scan.links
    assert not scan.headings
    assert not scan.citation_markers
    assert not scan.citation_definitions
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 4)
    ]


@pytest.mark.parametrize(
    "destination",
    [
        'foo(and)bar.md "A title"',
        r"foo\(and\).md 'A \'quoted\' title'",
        '<foo(and)bar.md> (A title)',
        r'<foo\>bar.md> "A \"quoted\" title"',
    ],
)
def test_destination_index_keeps_escaped_parentheses_angles_and_titles_raw(destination):
    scan = scan_markdown("[Raw](" + destination + ")[^real]\n")
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Raw", destination, 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "candidate,line",
    [
        ("> [A](foo\\{newline}> bar`baz.md)[^real] `later`", 1),
        ("> [A]({newline}> dest.md)[^real]", 1),
        ("- [A](foo\\{newline}  bar.md)[^real]", 1),
        ("> Intro{newline}> [A](foo\\{newline}> bar`baz.md)[^real] `later`", 2),
    ],
    ids=["quote-escaped", "quote-leading-space", "list", "quote-later-line"],
)
def test_parser_witness_rejects_multiline_container_links(newline, candidate, line):
    scan = scan_markdown(candidate.format(newline=newline))
    assert not scan.links
    assert not scan.citation_markers
    assert not scan.citation_definitions
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", line)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("frontmatter", [False, True])
def test_source_nul_fails_closed_without_parser_normalization(newline, frontmatter):
    prefix = newline.join(["---", "title: test", "---", ""]) if frontmatter else ""
    scan = scan_markdown(prefix + "[A](foo\x00bar`baz.md)[^real] `later`")
    assert not scan.links
    assert not scan.citation_markers
    assert not scan.citation_definitions
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 4 if frontmatter else 1)
    ]


@pytest.mark.parametrize(
    "text",
    ['Text <span title="`">[^real] `later`', 'Text <span>[^real]</span>'],
)
def test_parser_witness_rejects_unsupported_inline_html(text):
    scan = scan_markdown(text)
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize(
    "text",
    ["[Alias][target][^real]\n\n[target]: real.md\n", "<https://example.com>[^real]"],
    ids=["reference-link", "autolink"],
)
def test_parser_witness_rejects_links_without_exact_supported_raw_spans(text):
    scan = scan_markdown(text)
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize("omitted_kind", ["link_open", "code_inline"])
def test_parser_child_kind_counts_must_match_raw_links_and_code(monkeypatch, omitted_kind):
    real_parser = markdown_module._PARSER

    def parse_with_missing_kind(body):
        tokens = real_parser.parse(body)
        for token in tokens:
            if token.type == "inline":
                token.children = [child for child in token.children if child.type != omitted_kind]
        return tokens

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=parse_with_missing_kind))
    scan = scan_markdown("`hidden` [Raw](raw.md)[^real]")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "![A](javascript:foo`bar) `[^real]` a `last`",
        "[Fake](javascript:alert) <https://example.com>[^real]",
        "[Fake](javascript:alert) [Alias][target][^real]\n\n[target]: real.md\n",
    ],
    ids=["image-code-cancellation", "autolink-cancellation", "reference-cancellation"],
)
def test_parser_span_witness_rejects_unrelated_raw_count_cancellation(text):
    scan = scan_markdown(text)
    assert not scan.links
    assert not scan.headings
    assert not scan.citation_markers
    assert not scan.citation_definitions
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize(
    "text,links",
    [
        ("[![](thumb.png)](page.md)[^real]", [("![](thumb.png)", "page.md", 1)]),
        ("![](image.md)[^real]", []),
    ],
    ids=["linked-empty-image", "empty-image"],
)
def test_parser_span_witness_accepts_valid_empty_image_children(text, links):
    scan = scan_markdown(text)
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 1)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("invalid_children", [None, "invalid", []])
def test_parser_span_witness_rejects_corrupt_nonempty_image_children(monkeypatch, invalid_children):
    real_parser = markdown_module._PARSER

    def parse_with_corrupt_image(body):
        tokens = real_parser.parse(body)
        for token in tokens:
            if token.type == "inline":
                for child in token.children:
                    if child.type == "image":
                        child.children = invalid_children
        return tokens

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=parse_with_corrupt_image))
    scan = scan_markdown("![nonempty](image.md)[^real]")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


def test_parser_span_witness_rejects_same_counts_from_different_source_positions(monkeypatch):
    real_parser = markdown_module._PARSER

    def parse_with_displaced_children(body):
        tokens = real_parser.parse(body)
        displaced = real_parser.parse("[A](a.md) `first`[^real]")
        children = next(token.children for token in displaced if token.type == "inline")
        next(token for token in tokens if token.type == "inline").children = children
        return tokens

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=parse_with_displaced_children))
    scan = scan_markdown("`first` [A](a.md)[^real]")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


def test_parser_span_witness_requires_actual_empty_alt_source_for_absent_children(monkeypatch):
    real_parser = markdown_module._PARSER

    def parse_with_false_empty_image(body):
        tokens = real_parser.parse(body)
        for token in tokens:
            if token.type == "inline":
                for child in token.children:
                    if child.type == "image":
                        child.children = None
                        child.content = ""
        return tokens

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=parse_with_false_empty_image))
    scan = scan_markdown("![nonempty](image.md)[^real]")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


def test_parser_span_witness_rejects_invalid_metadata_shape(monkeypatch):
    real_parser = markdown_module._PARSER

    def parse_with_invalid_metadata(body):
        tokens = real_parser.parse(body)
        for token in tokens:
            if token.type == "inline":
                for child in token.children:
                    if child.type == "code_inline":
                        child.meta = None
        return tokens

    monkeypatch.setattr(markdown_module, "_PARSER", SimpleNamespace(parse=parse_with_invalid_metadata))
    scan = scan_markdown("`[^hidden]` [Real](real.md)[^real]")
    assert not scan.links
    assert not scan.citation_markers
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 1)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("prefix", ["", "> "])
def test_parser_span_witness_preserves_tab_expanded_list_continuation(newline, prefix):
    scan = scan_markdown(
        newline.join([
            prefix + "- parent",
            prefix + "\t[A](x.md)[^real] `[^hidden]`",
            "",
        ])
    )
    assert [(item.text, item.destination, item.line) for item in scan.links] == [("A", "x.md", 2)]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 2)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_definition_shaped_line_cannot_split_a_commonmark_code_span(newline):
    scan = scan_markdown(newline.join([
        "`open",
        "[^d]: [L](x.md)[^hidden]",
        "end`",
        "",
    ]))
    assert scan.links == ()
    assert scan.headings == ()
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_definition_shaped_line_is_invisible_when_its_prefix_is_in_code(newline):
    scan = scan_markdown(newline.join([
        "`unfinished paragraph",
        "[^cite]: source_id: `src_value`; [original](original.txt)",
        "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("original", "original.txt", 2)
    ]
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("prior_content", [False, True])
def test_definition_shaped_line_keeps_the_full_multiline_link_witness(newline, prior_content):
    prefix = ["# Heading", "", "[Earlier](earlier.md)[^earlier]", ""] if prior_content else []
    scan = scan_markdown(newline.join(prefix + [
        "[^d]: [L](x\\",
        "y.md)",
        "",
    ]))
    assert scan.links == ()
    assert scan.headings == ()
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 5 if prior_content else 1)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("destination", ["foo[^hidden].md", 'foo.md "title [^hidden]"'])
def test_direct_link_nonrendered_marker_syntax_is_not_a_citation(newline, destination):
    scan = scan_markdown("[L](" + destination + ")" + newline)
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("L", destination, 1)
    ]
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_direct_link_keeps_a_visible_label_marker(newline):
    scan = scan_markdown("[Claim [^visible]](target.md)" + newline)
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("Claim [^visible]", "target.md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 1)
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "label,destination",
    [
        ("Claim [^visible]", 'foo[^destination].md "title [^title]"'),
        ("Claim [^visible]", "foo`bar[^destination].md"),
        (r"Claim \] [^visible]", r"foo\(and\)[^destination].md"),
        ("Claim [^visible]", "foo(and)[^destination].md"),
        ("Claim [^visible]", '<foo[^destination].md> "title [^title]"'),
        ("Claim [^visible]", "foo.md 'title [^title]'"),
        ("Claim [^visible]", "foo.md (title [^title])"),
        ("![alt](image.md) [^visible]", "foo[^destination].md"),
        ("`[^code]` [^visible]", "foo[^destination].md"),
    ],
    ids=["mixed", "backtick", "escaped", "balanced", "angle", "single-title", "paren-title", "image", "code"],
)
def test_direct_link_nonrendered_markers_preserve_raw_labels_and_neighbors(newline, label, destination):
    scan = scan_markdown(newline.join([
        "---", "title: Exact bytes", "---",
        "> [^before] [" + label + "](" + destination + ")[^after] `[^code_after]`",
        "> Following [Other](other.md)[^next]",
        "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        (label, destination, 4),
        ("Other", "other.md", 5),
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("before", 4), ("visible", 4), ("after", 4), ("next", 5),
    ]
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_direct_link_nonrendered_markers_preserve_repository_definitions(newline):
    scan = scan_markdown(newline.join([
        "``unfinished paragraph",
        '[^cite]: source_id: `src_value`; [original [^visible]](foo[^hidden].md "[^title]")[^real]',
        "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("original [^visible]", 'foo[^hidden].md "[^title]"', 2)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 2), ("real", 2),
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [
        ("cite", 2)
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_direct_link_nonrendered_mask_requires_a_reconciled_span(newline):
    scan = scan_markdown(newline.join([
        "# Heading", "", "[Earlier](earlier.md)[^earlier]", "",
        "[Claim [^visible]](javascript:alert[^hidden])",
        "",
    ]))
    assert scan.links == ()
    assert scan.headings == ()
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 5)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_direct_link_nonrendered_mask_keeps_rejected_outer_syntax_visible(newline):
    scan = scan_markdown(
        "[outer [inner [^visible]](in[^hidden].md)](out[^outer].md)[^after]" + newline
    )
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("inner [^visible]", "in[^hidden].md", 1)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 1), ("outer", 1), ("after", 1),
    ]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("container", ["", "> ", "> 1. "], ids=["plain", "quote", "quoted-list"])
@pytest.mark.parametrize(
    "destination",
    [
        "assets/[^phantom].png",
        "assets/(size)[^phantom].png",
        r"assets/\(size\)[^phantom].png",
        "<assets/[^phantom].png>",
        "assets/`size[^phantom].png",
        'assets/image.png "caption [^title]"',
        "assets/image.png 'caption [^title]'",
        "assets/image.png (caption [^title])",
        r'<assets/\>[^phantom].png> "caption \"quoted\" [^title]"',
    ],
    ids=["plain", "balanced", "escaped", "angle", "backtick", "double-title", "single-title", "paren-title", "escaped-title"],
)
def test_image_nonrendered_destination_and_title_markers_are_hidden(newline, container, destination):
    scan = scan_markdown(newline.join([
        "---", "title: Physical lines", "---",
        container + "![thumbnail](" + destination + ")[^real] `[^code]`",
        "",
    ]))
    assert scan.links == ()
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [("real", 4)]
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "text,links,markers",
    [
        (
            '[outer [^label] ![alt [^alt]](img[^image].png "[^image_title]")]'
            '(out\\(and\\)[^outer].md "[^outer_title]")[^after]',
            [(
                'outer [^label] ![alt [^alt]](img[^image].png "[^image_title]")',
                'out\\(and\\)[^outer].md "[^outer_title]"',
                4,
            )],
            [("label", 4), ("alt", 4), ("after", 4)],
        ),
        (
            '![photo [^alt] [Learn [^label]](page[^inner].md "[^inner_title]")]'
            '(image[^image].png "[^image_title]")[^after]',
            [],
            [("alt", 4), ("label", 4), ("after", 4)],
        ),
        (
            '![outer [^alt] ![inner [^nested]](inner[^inner].png "[^inner_title]")]'
            '(outer[^outer].png "[^outer_title]")[^after]',
            [],
            [("alt", 4), ("nested", 4), ("after", 4)],
        ),
        (
            '[![outer ![nested [^alt]](inner[^inner].png) '
            '[Learn [^label]](page`raw[^page].md)](image[^image].png)]'
            '(outer[^outer].md)[^after] `[^code]`',
            [(
                '![outer ![nested [^alt]](inner[^inner].png) '
                '[Learn [^label]](page`raw[^page].md)](image[^image].png)',
                'outer[^outer].md',
                4,
            )],
            [("alt", 4), ("label", 4), ("after", 4)],
        ),
        (
            '![alt [outer [inner [^visible]](in[^hidden].md)]'
            '(out[^literal].md)](image[^image].png)[^after]',
            [],
            [("visible", 4), ("literal", 4), ("after", 4)],
        ),
        (
            '[![](image[^image].png "[^title]")](page[^page].md)[^after]',
            [('![](image[^image].png "[^title]")', 'page[^page].md', 4)],
            [("after", 4)],
        ),
    ],
    ids=["image-in-link", "link-in-image", "nested-images", "all-nesting", "rejected-inner-outer-link", "empty-alt"],
)
def test_image_nonrendered_descendant_markers_preserve_visible_text_and_graph_output(newline, text, links, markers):
    scan = scan_markdown(newline.join([
        "---", "title: Exact bytes", "---", "> " + text,
        "> Following [Other](other.md)[^next]", "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == links + [
        ("Other", "other.md", 5)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == markers + [
        ("next", 5)
    ]
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "text,links,markers",
    [
        (
            r"!\[alt [^alt]\](image[^literal].png)[^after]",
            [],
            [("alt", 1), ("literal", 1), ("after", 1)],
        ),
        (
            r"\![alt [^alt]](page[^hidden].md)[^after]",
            [("alt [^alt]", "page[^hidden].md", 1)],
            [("alt", 1), ("after", 1)],
        ),
        (
            "![alt [^alt]](bad suffix [^literal])[^after]",
            [],
            [("alt", 1), ("literal", 1), ("after", 1)],
        ),
        (
            "![alt [inner [^visible]](in[^hidden].md)](bad suffix [^literal])[^after]",
            [("inner [^visible]", "in[^hidden].md", 1)],
            [("visible", 1), ("literal", 1), ("after", 1)],
        ),
        (
            '![`[^code]` [^alt]](image[^hidden].png "[^title]")[^after] '
            '`![alt](image[^code_image].png)`',
            [],
            [("alt", 1), ("after", 1)],
        ),
    ],
    ids=["escaped-brackets", "escaped-exclamation", "invalid-image", "invalid-image-with-link", "code"],
)
def test_image_nonrendered_mask_respects_literal_syntax_and_code(newline, text, links, markers):
    scan = scan_markdown(text + newline)
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == markers
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_image_nonrendered_mask_preserves_definition_recognition_and_multiline_code(newline):
    scan = scan_markdown(newline.join([
        "`open",
        '[^code_definition]: ![code [^code_alt]](hidden[^code_image].png "[^code_title]")',
        "end`",
        "",
        '[^cite]: ![alt [^visible]](image[^image].png "[^title]")[^after]',
        "",
    ]))
    assert scan.links == ()
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 5), ("after", 5)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [("cite", 5)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "candidate",
    [
        "![alt [^visible]](javascript:alert[^hidden])",
        "![alt [^visible]](image\\{newline}> path[^hidden].png)",
        "![alt{newline}> [^visible]](image[^hidden].png)",
        '![alt [^visible]](image.png "title{newline}> [^hidden]")',
        "![alt [inner](page\\{newline}> path[^hidden].md)](image.png)",
    ],
    ids=["unreconciled-image", "multiline-destination", "multiline-alt", "multiline-title", "multiline-descendant"],
)
def test_image_nonrendered_mask_requires_reconciled_single_line_spans(newline, candidate):
    scan = scan_markdown(newline.join([
        "# Heading", "", "[Earlier](earlier.md)[^earlier]", "",
        "> " + candidate.format(newline=newline), "",
    ]))
    assert scan.links == ()
    assert scan.headings == ()
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 5)
    ]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("container", ["", "> ", "> 1. "], ids=["plain", "quote", "quoted-list"])
@pytest.mark.parametrize(
    "text,links",
    [
        ("[^id](x.md)", [("^id", "x.md", 4)]),
        ("![^id](x.png)", []),
        ("[![^id](x.png)](outer.md)", [("![^id](x.png)", "outer.md", 4)]),
        ("![photo [^id](x.md)](image.png)", []),
        ("![outer ![^id](inner.png)](outer.png)", []),
        ("[[^id](inner.md)](outer.md)", [("^id", "inner.md", 4)]),
        (
            "[![alt [^id](inner.md)](image.png)](outer.md)",
            [("![alt [^id](inner.md)](image.png)", "outer.md", 4)],
        ),
    ],
    ids=["link", "image", "image-in-link", "link-in-image", "nested-images", "rejected-outer-link", "all-nesting"],
)
def test_label_delimiters_cannot_form_citation_markers(newline, container, text, links):
    scan = scan_markdown(newline.join([
        "---", "title: Physical lines", "---",
        container + "[^before] " + text + "[^after]", "", "Following[^next]", "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("before", 4), ("after", 4), ("next", 6),
    ]
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "text,links",
    [
        ("[[^visible]](x.md)", [("[^visible]", "x.md", 1)]),
        ("![[^visible]](x.png)", []),
        ("[prefix [^visible]](x.md)", [("prefix [^visible]", "x.md", 1)]),
        ("![prefix [^visible]](x.png)", []),
        (
            "[![[^visible]](x.png)](outer.md)",
            [("![[^visible]](x.png)", "outer.md", 1)],
        ),
        ("![photo [[^visible]](x.md)](image.png)", []),
        ("![outer ![[^visible]](inner.png)](outer.png)", []),
        (
            '[^phantom](raw\\(and\\)[^destination].md "[^title]") '
            '[label [^visible] ![^image](inner[^target].png)](out`raw.md)',
            [
                ("^phantom", 'raw\\(and\\)[^destination].md "[^title]"', 1),
                ("label [^visible] ![^image](inner[^target].png)", "out`raw.md", 1),
            ],
        ),
    ],
    ids=["link", "image", "link-prefix", "image-prefix", "image-in-link", "link-in-image", "nested-images", "mixed-raw-spelling"],
)
def test_label_delimiter_mask_preserves_visible_interiors(newline, text, links):
    scan = scan_markdown(text + "[^after] `[^code]`" + newline)
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 1), ("after", 1),
    ]
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "text,links,markers",
    [
        ("[^literal](bad suffix)", [], [("literal", 1), ("after", 1)]),
        ("![^literal](bad suffix)", [], [("literal", 1), ("after", 1)]),
        (r"\[^escaped](x.md)", [], [("after", 1)]),
        (r"!\[^escaped](x.png)", [], [("after", 1)]),
        (r"\![^id](x.md)", [("^id", "x.md", 1)], [("after", 1)]),
        (
            "![alt [^id](inner.md)](bad suffix [^literal])",
            [("^id", "inner.md", 1)],
            [("literal", 1), ("after", 1)],
        ),
    ],
    ids=["invalid-link", "invalid-image", "escaped-link", "escaped-image", "escaped-exclamation", "rejected-image-with-link"],
)
def test_label_delimiter_mask_requires_semantic_label_syntax(newline, text, links, markers):
    scan = scan_markdown(text + "[^after]" + newline)
    assert [(item.text, item.destination, item.line) for item in scan.links] == links
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == markers
    assert scan.citation_definitions == ()
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_label_delimiter_mask_preserves_definitions_and_full_code_scope(newline):
    scan = scan_markdown(newline.join([
        "`open", "[^hidden]: [^code](x.md) ![^image](x.png)", "end`", "",
        '[^cite]: [^id](x[^target].md "[^title]") ![[^visible]](image.png)[^after]', "",
    ]))
    assert [(item.text, item.destination, item.line) for item in scan.links] == [
        ("^id", 'x[^target].md "[^title]"', 5)
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_markers] == [
        ("visible", 5), ("after", 5),
    ]
    assert [(item.citation_id, item.line) for item in scan.citation_definitions] == [("cite", 5)]
    assert scan.diagnostics == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "candidate",
    [
        "[^id](javascript:alert)",
        "![^id](javascript:alert)",
        "[^id](x\\{newline}> y.md)",
        "![^id](x\\{newline}> y.png)",
    ],
    ids=["unreconciled-link", "unreconciled-image", "multiline-link", "multiline-image"],
)
def test_label_delimiter_mask_cannot_publish_partial_unreconciled_data(newline, candidate):
    scan = scan_markdown(newline.join([
        "# Heading", "", "[Earlier](earlier.md)[^earlier]", "",
        "> " + candidate.format(newline=newline), "",
    ]))
    assert scan.links == ()
    assert scan.headings == ()
    assert scan.citation_markers == ()
    assert scan.citation_definitions == ()
    assert [(item.code, item.line) for item in scan.diagnostics] == [
        ("markdown_reconciliation_ambiguous", 5)
    ]


def test_visible_text_spans_keep_raw_chunks_and_tag_reconciled_direct_links():
    source = 'Plain [Target](target.md "title") tail\n'

    scan = scan_markdown(source)

    link_start = source.index("[Target]")
    label_start = source.index("Target")
    tail_start = source.index(" tail")
    assert scan.links == (MarkdownLink("target.md \"title\"", "Target", 1),)
    assert scan.links[0].source_span == MarkdownSourceSpan(
        link_start, tail_start, 1, 1
    )
    assert scan.links[0].label_span == MarkdownSourceSpan(
        label_start, label_start + len("Target"), 1, 1
    )
    assert [(item.text, item.source_span) for item in scan.visible_text_spans] == [
        ("Plain ", MarkdownSourceSpan(0, link_start, 1, 1)),
        ("Target", MarkdownSourceSpan(label_start, label_start + len("Target"), 1, 1)),
        (" tail", MarkdownSourceSpan(tail_start, len(source) - 1, 1, 1)),
    ]


def test_visible_text_spans_hide_code_images_and_direct_link_syntax_without_joining():
    source = "before `hidden` [Kept ![alt](image.png) after](target.md) tail\n"

    scan = scan_markdown(source)

    assert [(item.text, item.source_span.start, item.source_span.end) for item in scan.visible_text_spans] == [
        ("before ", 0, source.index("`hidden`")),
        (" ", source.index(" [Kept"), source.index("[Kept")),
        ("Kept ", source.index("Kept"), source.index("![alt]")),
        (" after", source.index(" after"), source.index("](target.md)")),
        (" tail", source.index(" tail"), len(source) - 1),
    ]
    assert [item.source_span.line for item in scan.visible_text_spans] == [1, 1, 1, 1, 1]
    assert [item.source_span.end_line for item in scan.visible_text_spans] == [1, 1, 1, 1, 1]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_visible_text_spans_use_physical_cr_lf_lines_only(newline):
    source = "first" + newline + "[Label](destination.md)" + newline + "tail\u2028more"

    scan = scan_markdown(source)

    label_start = source.index("Label")
    tail_start = source.index("tail")
    assert [(item.text, item.source_span) for item in scan.visible_text_spans] == [
        ("first", MarkdownSourceSpan(0, 5, 1, 1)),
        ("Label", MarkdownSourceSpan(label_start, label_start + 5, 2, 2)),
        (
            "tail\u2028more",
            MarkdownSourceSpan(tail_start, len(source), 3, 3),
        ),
    ]


def test_visible_text_spans_keep_literal_and_escaped_syntax_but_not_nonrendered_blocks():
    source = (
        "---\n"
        "title: [frontmatter](hidden.md)\n"
        "---\n"
        "Visible [bad](bad suffix) \\[escaped](literal.md)\n"
        "\n"
        "    [indented](hidden.md)\n"
        "```\n"
        "[fenced](hidden.md)\n"
        "```\n"
        "Tail\n"
    )

    scan = scan_markdown(source)

    assert scan.diagnostics == ()
    assert [item.text for item in scan.visible_text_spans] == [
        "Visible [bad](bad suffix) \\[escaped](literal.md)",
        "Tail",
    ]
    assert [item.source_span.line for item in scan.visible_text_spans] == [4, 10]


def test_visible_text_spans_fail_closed_and_new_fields_preserve_legacy_value_contracts():
    source = "visible\x00"

    scan = scan_markdown(source)

    assert scan.visible_text_spans == ()
    span = MarkdownSourceSpan(1, 2, 1, 1)
    assert MarkdownLink("target.md", "Target", 1) == MarkdownLink(
        "target.md", "Target", 1, source_span=span, label_span=span
    )
    assert repr(MarkdownLink("target.md", "Target", 1, source_span=span)) == (
        "MarkdownLink(destination='target.md', text='Target', line=1)"
    )
    assert MarkdownScan((), (), ()) == MarkdownScan((), (), (), visible_text_spans=())
