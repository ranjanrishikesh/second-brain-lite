"""Pure native-marker audit regressions for parser source mapping and joins."""

from types import SimpleNamespace

import pytest

from tests.evals import event_log_contract as contract
from tests.evals.generate_scenarios import SCENARIOS


def _audit_suffix(suffix: str, *, prefix: str = "") -> None:
    """Index two literal events and audit additional assistant text against them."""

    names = ("classify_repository_development", "use_software_workflow")
    pointer = "/message/content/0/text"
    text = prefix
    markers, events = {}, []
    for name in names:
        start = len(text.encode())
        text += f"EVENT:{name}\n"
        markers[name] = {
            "native_record_start": 17, "text_pointer": pointer,
            "byte_start": start, "byte_end": len(text.encode()),
        }
        events.append({"execution_id": "main", "name": name, "transcript_marker_id": name})
    text += suffix
    control = SimpleNamespace(
        obj=lambda artifact_id, kind: markers[artifact_id],
        native_records={"main": {17: {"texts": {pointer: text}}}},
    )
    scenario = next(item for item in SCENARIOS if item["id"] == "repository-development-not-archived")
    contract._audit_native_event_markers(
        control, {"events": events}, scenario, {"main": {"phase": "main"}},
    )


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("kind", ["fence", "inline"])
def test_code_suppression_preserves_physical_newline_semantics(newline, kind):
    lines = ["```text", "EVENT:unindexed_marker", "```"] if kind == "fence" else [
        "Before ` unmatched ``", "EVENT:unindexed_marker", "`` after",
    ]
    _audit_suffix("\n" + newline.join(lines) + newline)


@pytest.mark.parametrize("suffix", [
    "\n> Before ``\nEVENT:unindexed_marker\n`` after\n",
    "\n- Before ``\nEVENT:unindexed_marker\n`` after\n",
    "\nBefore ``\nEVENT:unindexed_marker\n`` after\n===\n",
    "\n[Before ``\nEVENT:unindexed_marker\n`` after](/url)\n",
    "\n![Before ``\nEVENT:unindexed_marker\n`` after](/image)\n",
    "\n![outer ![Before ``\nEVENT:unindexed_marker\n`` after](/inner)](/outer)\n",
    "\n\v\nBefore ``\nEVENT:unindexed_marker\n`` after\n",
    "\n> \v\n> Before ``\nEVENT:unindexed_marker\n`` after\n",
], ids=["blockquote", "list", "setext", "link", "image", "nested-image", "stripped-line", "stripped-quote-line"])
def test_code_suppression_uses_parser_container_and_child_source_positions(suffix):
    _audit_suffix(suffix)


@pytest.mark.parametrize("line", [
    "EVENT:unindexed_marker\r", "EVENT:unindexed_marker\r\n", "EVENT:unindexed_marker",
    "EVENT:Uppercase\n", "EVENT:unindexed_marker trailing\n", "EVENT:\n",
    "EVENT:unindexed_marker`\n",
])
def test_non_code_malformed_event_lines_fail_closed(line):
    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        _audit_suffix("\n" + line)


@pytest.mark.parametrize("line", [
    "EVENT:Uppercase", "EVENT:", "EVENT:unindexed_marker trailing", "EVENT:\x00",
])
def test_complete_malformed_event_lines_inside_code_are_ordinary_code(line):
    _audit_suffix(f"\nBefore ``\n{line}\n`` after\n")


def test_partly_code_malformed_event_line_still_fails_closed():
    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        _audit_suffix("\nBefore `\nEVENT:unindexed_marker` outside\n")


@pytest.mark.parametrize("name", ["unindexed_marker", "use_software_workflow", "public_web_access"])
def test_unknown_duplicate_and_foreign_phase_markers_cannot_hide_in_prose(name):
    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        _audit_suffix(f"\nprose\rEVENT:{name}\n")


def test_marker_joins_keep_original_utf8_bytes_after_mixed_line_endings():
    _audit_suffix(
        "\nRésumé ``\rEVENT:unindexed_marker\r\n`` after\n",
        prefix="Résumé\rprose\r\n\n",
    )


def test_inline_code_occurrence_cannot_hide_an_identical_non_code_marker():
    with pytest.raises(contract.EventLogContractError, match="native event marker"):
        _audit_suffix(
            "\nBefore ``\nEVENT:unindexed_marker\n`` after\nEVENT:unindexed_marker\n",
        )
