import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from brainlib.contracts import (
    Anchor,
    FileFingerprint,
    ProcessingAttempt,
    SourceRecord,
    SourceState,
    UrlDescriptorMetadata,
    VersionAdoptionEvent,
    compute_corpus_revision,
    compute_sha256,
    derivation_id,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic, ValidationIssue, ValidationReport
from conftest import FIXED_NOW, make_retrieval_metadata, make_source_record


SCHEMAS = Path(__file__).resolve().parents[2] / "docs/brain/schemas"
RFC3339_FORMAT_CHECKER = FormatChecker()


@RFC3339_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    datetime.fromisoformat(value[:-1] + "+00:00")
    return True


def load_schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def test_jsonschema_is_declared_as_a_development_only_dependency() -> None:
    config = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert config["project"]["dependencies"] == ["markdown-it-py>=4,<5"]
    assert "jsonschema>=4,<5" in config["project"]["optional-dependencies"]["dev"]


def test_source_states_are_the_stable_serialized_vocabulary() -> None:
    assert [state.value for state in SourceState] == [
        "pending",
        "extracting",
        "ok",
        "warning",
        "needs_agent",
        "failed",
        "unsupported",
        "integrity_error",
        "awaiting_approval",
    ]


def test_validation_report_is_blocked_only_by_errors() -> None:
    warning = ValidationIssue("warning", "stale", "Needs refresh")
    error = ValidationIssue("error", "missing", "Missing source")

    assert ValidationReport(("ledger",), (), "a" * 64).ok
    assert ValidationReport(("ledger",), (warning,), "a" * 64).ok
    assert not ValidationReport(("ledger",), (warning, error), "a" * 64).ok


def test_source_id_is_reproducible_path_sensitive_and_exact() -> None:
    checksum = "a" * 64
    first = source_id_for_first_seen(PurePosixPath("books/a.pdf"), checksum)

    assert (
        first == "src_006a1476aa24e7347b822cb81e31b96bd8b6e288811e38895cfd89d2677c66e6"
    )
    assert first == source_id_for_first_seen(PurePosixPath("books/a.pdf"), checksum)
    assert first != source_id_for_first_seen(PurePosixPath("books/b.pdf"), checksum)


@pytest.mark.parametrize(
    ("raw_path", "checksum"),
    [
        (PurePosixPath("/books/a.pdf"), "a" * 64),
        (PurePosixPath("books/../a.pdf"), "a" * 64),
        (PurePosixPath("sources/raw/books/a.pdf"), "a" * 64),
        (PurePosixPath("books/a.pdf"), "A" * 64),
    ],
)
def test_source_id_rejects_noncanonical_identity_inputs(
    raw_path: PurePosixPath, checksum: str
) -> None:
    with pytest.raises(ValueError):
        source_id_for_first_seen(raw_path, checksum)


def test_compute_sha256_streams_exact_file_bytes(tmp_path: Path) -> None:
    source = tmp_path / "content.bin"
    source.write_bytes(b"abc")

    assert compute_sha256(source, chunk_size=1) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_compute_sha256_rejects_nonpositive_chunk_size(tmp_path: Path) -> None:
    source = tmp_path / "content.bin"
    source.write_bytes(b"abc")

    with pytest.raises(ValueError, match="chunk_size"):
        compute_sha256(source, chunk_size=0)


def test_derivation_identity_is_exact_and_includes_source_version() -> None:
    first = derivation_id(
        source_sha256="a" * 64,
        extractor_id="pdf",
        extractor_version="1",
        config_sha256="b" * 64,
    )

    assert (
        first == "drv_02373c7efd58c583a3ef6f21ad4f5f24c898fa2da8aea5c172dabe51f15f7d6e"
    )
    assert first != derivation_id(
        source_sha256="c" * 64,
        extractor_id="pdf",
        extractor_version="1",
        config_sha256="b" * 64,
    )


def test_source_record_round_trips_canonical_json_without_losing_values() -> None:
    attempt = ProcessingAttempt(
        "a" * 64,
        "builtin.text",
        "1",
        "c" * 64,
        "e" * 64,
        SourceState.WARNING,
        FIXED_NOW,
        ("low_quality",),
    )
    diagnostic = Diagnostic(
        "low_quality",
        "Only one anchor",
        PurePosixPath("notes/a.txt"),
        {"anchor_count": 1, "retryable": False},
    )
    record = replace(
        make_source_record(retrieval=make_retrieval_metadata()),
        source_id=source_id_for_first_seen(PurePosixPath("notes/old-a.txt"), "a" * 64),
        previous_raw_paths=(PurePosixPath("notes/old-a.txt"),),
        last_attempt=attempt,
        diagnostics=(diagnostic,),
    )

    payload = record.to_dict()

    assert payload["current_raw_path"] == "notes/a.txt"
    assert payload["created_at"] == "2026-09-04T00:00:00Z"
    assert payload["state"] == "ok"
    assert payload["diagnostics"][0]["details"] == {
        "anchor_count": 1,
        "retryable": False,
    }
    assert SourceRecord.from_dict(payload) == record


def test_diagnostic_path_round_trips_as_a_repository_relative_path() -> None:
    diagnostic = Diagnostic(
        "raw_changed",
        "Raw source changed in place",
        PurePosixPath("sources/raw/notes/a.txt"),
    )
    record = replace(make_source_record(), diagnostics=(diagnostic,))

    assert SourceRecord.from_dict(record.to_dict()).diagnostics == (diagnostic,)
    Draft202012Validator(load_schema("source-record.v1.schema.json")).validate(
        record.to_dict()
    )


def test_diagnostic_details_detach_from_caller_owned_nested_json() -> None:
    details = {"context": {"attempts": [1]}}
    diagnostic = Diagnostic("retry", "Retry scheduled", details=details)
    record = replace(make_source_record(), diagnostics=(diagnostic,))

    details["context"]["attempts"].append(2)
    details["new"] = True

    assert record.to_dict()["diagnostics"][0]["details"] == {
        "context": {"attempts": [1]}
    }
    assert diagnostic.details == {"context": {"attempts": [1]}}
    assert json.loads(json.dumps(diagnostic.details)) == {"context": {"attempts": [1]}}
    assert SourceRecord.from_dict(record.to_dict()) == record


def test_validation_issue_details_detach_from_caller_owned_nested_json() -> None:
    details = {"context": {"attempt": 1}}
    issue = ValidationIssue("warning", "retry", "Retry scheduled", details=details)

    details["context"]["attempt"] = 2

    assert issue.details == {"context": {"attempt": 1}}


@pytest.mark.parametrize(
    "diagnostic",
    [
        Diagnostic(
            "retry",
            "Retry scheduled",
            details={"context": {"attempts": [1]}},
        ),
        ValidationIssue(
            "warning",
            "retry",
            "Retry scheduled",
            details={"context": {"attempts": [1]}},
        ),
    ],
)
def test_diagnostic_details_are_recursively_read_only(
    diagnostic: Diagnostic | ValidationIssue,
) -> None:
    with pytest.raises(TypeError):
        diagnostic.details["new"] = True  # type: ignore[index]

    context = diagnostic.details["context"]
    assert isinstance(context, Mapping)
    with pytest.raises(TypeError):
        context["new"] = True  # type: ignore[index]

    attempts = context["attempts"]
    assert isinstance(attempts, Sequence)
    with pytest.raises(TypeError):
        attempts[0] = 2  # type: ignore[index]


def test_derivation_metadata_detaches_from_its_source_and_is_read_only() -> None:
    base = make_source_record()
    active_id = base.active_derivation_id or ""
    metadata = {
        "converter_id": "builtin.text",
        "converter_version": "builtin:builtin.text:1",
    }
    derivation = replace(base.derivations[active_id], method_metadata=metadata)

    metadata["converter_id"] = "mutated"

    assert derivation.method_metadata == {
        "converter_id": "builtin.text",
        "converter_version": "builtin:builtin.text:1",
    }
    assert json.loads(json.dumps(derivation.method_metadata)) == {
        "converter_id": "builtin.text",
        "converter_version": "builtin:builtin.text:1",
    }
    with pytest.raises(TypeError):
        derivation.method_metadata["converter_id"] = "mutated"  # type: ignore[index]


def test_source_record_mappings_detach_from_their_sources_and_are_read_only() -> None:
    base = make_source_record()
    active_id = base.active_derivation_id or ""
    versions = dict(base.versions)
    derivations = dict(base.derivations)
    record = replace(base, versions=versions, derivations=derivations)

    versions.clear()
    derivations.clear()

    assert tuple(record.versions) == ("a" * 64,)
    assert tuple(record.derivations) == (active_id,)
    with pytest.raises(TypeError):
        record.versions["b" * 64] = base.versions["a" * 64]  # type: ignore[index]
    with pytest.raises(TypeError):
        record.derivations["drv_" + "c" * 64] = base.derivations[active_id]  # type: ignore[index]
    assert SourceRecord.from_dict(record.to_dict()) == record


def test_source_record_rejects_absolute_raw_path() -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="repository-relative"):
        SourceRecord.from_dict({**payload, "current_raw_path": "/tmp/private.pdf"})


@pytest.mark.parametrize("raw_path", ["C:/private.txt", "C:private.txt"])
def test_source_record_rejects_windows_drive_paths(raw_path: str) -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="repository-relative"):
        SourceRecord.from_dict({**payload, "current_raw_path": raw_path})


@pytest.mark.parametrize("raw_path", ["C:/private.txt", "C:private.txt"])
def test_source_record_schema_rejects_windows_drive_paths(raw_path: str) -> None:
    validator = Draft202012Validator(load_schema("source-record.v1.schema.json"))
    payload = make_source_record().to_dict()
    payload["current_raw_path"] = raw_path

    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    "raw_path",
    ["notes/../private.pdf", "sources/raw/notes/a.txt", "notes\\a.txt", "notes//a.txt"],
)
def test_source_record_rejects_noncanonical_raw_paths(raw_path: str) -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="canonical repository-relative"):
        SourceRecord.from_dict({**payload, "current_raw_path": raw_path})


def test_source_record_rejects_noncanonical_timestamp() -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="canonical UTC timestamp"):
        SourceRecord.from_dict({**payload, "created_at": "2026-09-04T00:00:00+00:00"})


def test_python_and_schema_reject_duplicate_previous_raw_paths() -> None:
    payload = make_source_record().to_dict()
    payload["previous_raw_paths"] = ["notes/old-a.txt", "notes/old-a.txt"]

    with pytest.raises(ValueError, match="previous_raw_paths must be unique"):
        SourceRecord.from_dict(payload)
    with pytest.raises(ValidationError):
        Draft202012Validator(load_schema("source-record.v1.schema.json")).validate(
            payload
        )


def test_source_record_rejects_unknown_or_non_json_fields() -> None:
    payload = make_source_record().to_dict()
    with pytest.raises(ValueError, match="unexpected fields"):
        SourceRecord.from_dict({**payload, "legacy_path": "notes/a.txt"})

    invalid = replace(
        make_source_record(),
        diagnostics=(Diagnostic("bad", "Bad details", details={"value": (1, 2)}),),
    )
    with pytest.raises(ValueError, match="canonical JSON"):
        invalid.to_dict()


def test_corpus_revision_is_order_independent_and_exact() -> None:
    record_a = make_source_record()
    record_b = make_source_record(
        raw_path=PurePosixPath("notes/b.txt"),
        content_sha256="b" * 64,
    )

    assert compute_corpus_revision([record_a, record_b]) == (
        "f9d6a749d1e3e4c4037c5def3628c15f189a93679f929428cd52270468cb49bf"
    )
    assert compute_corpus_revision([record_a, record_b]) == compute_corpus_revision(
        [record_b, record_a]
    )


def test_web_retrieval_is_bound_to_exact_content_bytes() -> None:
    record = make_source_record(
        retrieval=make_retrieval_metadata(sha256="a" * 64, byte_size=7)
    )
    event = record.versions["a" * 64].retrieval_events[-1]

    assert event.sha256 == "a" * 64
    assert event.byte_size == 7
    assert event.final_url == "https://example.test/final"
    assert event.approval_note == "User approved one capture for this question."


def test_retrieval_rejects_bytes_that_do_not_match_owning_version() -> None:
    record = make_source_record(
        retrieval=make_retrieval_metadata(sha256="b" * 64, byte_size=7)
    )

    with pytest.raises(ValueError, match="retrieval event"):
        record.to_dict()


def test_version_adoption_event_round_trips() -> None:
    event = VersionAdoptionEvent(
        "a" * 64, "b" * 64, "User approved replacement.", FIXED_NOW
    )
    base = make_source_record()
    adopted = replace(base.versions["a" * 64], sha256="b" * 64)
    record = replace(
        base,
        state=SourceState.PENDING,
        versions={"a" * 64: base.versions["a" * 64], "b" * 64: adopted},
        active_content_sha256="b" * 64,
        active_derivation_id=None,
        adoption_events=(event,),
    )

    assert SourceRecord.from_dict(record.to_dict()).adoption_events == (event,)


def test_file_source_id_must_bind_creation_path_and_adoption_chain_root() -> None:
    record = make_source_record()
    root_sha = record.active_content_sha256 or ""
    adopted_sha = "b" * 64
    adopted_version = replace(
        record.versions[root_sha],
        sha256=adopted_sha,
        first_seen_at=FIXED_NOW,
    )
    event = VersionAdoptionEvent(
        root_sha,
        adopted_sha,
        "User approved replacement.",
        FIXED_NOW,
    )
    forged = replace(
        record,
        source_id=source_id_for_first_seen(record.current_raw_path, adopted_sha),
        state=SourceState.PENDING,
        versions={root_sha: record.versions[root_sha], adopted_sha: adopted_version},
        active_content_sha256=adopted_sha,
        derivations={},
        active_derivation_id=None,
        adoption_events=(event,),
    )

    with pytest.raises(ValueError, match="creation identity"):
        forged.to_dict()


def test_file_adoption_events_must_form_one_ordered_complete_chain() -> None:
    record = make_source_record()
    root_sha = record.active_content_sha256 or ""
    second_sha = "b" * 64
    third_sha = "c" * 64
    versions = {
        root_sha: record.versions[root_sha],
        second_sha: replace(record.versions[root_sha], sha256=second_sha),
        third_sha: replace(record.versions[root_sha], sha256=third_sha),
    }
    branched = replace(
        record,
        state=SourceState.PENDING,
        versions=versions,
        active_content_sha256=third_sha,
        derivations={},
        active_derivation_id=None,
        adoption_events=(
            VersionAdoptionEvent(root_sha, second_sha, "Approved second.", FIXED_NOW),
            VersionAdoptionEvent(root_sha, third_sha, "Approved third.", FIXED_NOW),
        ),
    )

    with pytest.raises(ValueError, match="ordered chain"):
        branched.to_dict()


def test_multiple_file_adoptions_with_intervening_renames_form_a_valid_chain() -> None:
    record = make_source_record()
    root_sha = record.active_content_sha256 or ""
    second_sha = "b" * 64
    third_sha = "c" * 64
    second_at = FIXED_NOW + timedelta(seconds=1)
    third_at = FIXED_NOW + timedelta(seconds=2)
    creation_path = record.current_raw_path
    current_path = PurePosixPath("renamed/final.txt")
    versions = {
        root_sha: record.versions[root_sha],
        second_sha: replace(
            record.versions[root_sha],
            sha256=second_sha,
            first_seen_at=second_at,
        ),
        third_sha: replace(
            record.versions[root_sha],
            sha256=third_sha,
            raw_path=current_path,
            fingerprint=replace(
                record.versions[root_sha].fingerprint,
                path=current_path,
            ),
            first_seen_at=third_at,
        ),
    }
    chained = replace(
        record,
        current_raw_path=current_path,
        previous_raw_paths=(creation_path, PurePosixPath("renamed/intermediate.txt")),
        state=SourceState.PENDING,
        versions=versions,
        active_content_sha256=third_sha,
        derivations={},
        active_derivation_id=None,
        adoption_events=(
            VersionAdoptionEvent(root_sha, second_sha, "Approved second.", second_at),
            VersionAdoptionEvent(second_sha, third_sha, "Approved third.", third_at),
        ),
    )

    assert SourceRecord.from_dict(chained.to_dict()) == chained


@pytest.mark.parametrize(
    "fault",
    (
        "missing_event",
        "missing_checksum",
        "duplicate",
        "reversed",
        "branched",
        "out_of_time",
        "active_not_tail",
        "rebound_to_tail",
    ),
)
def test_file_adoption_chain_rejects_every_noncanonical_shape(fault: str) -> None:
    record = make_source_record()
    root_sha = record.active_content_sha256 or ""
    second_sha = "b" * 64
    third_sha = "c" * 64
    missing_sha = "d" * 64
    second_at = FIXED_NOW + timedelta(seconds=1)
    third_at = FIXED_NOW + timedelta(seconds=2)
    versions = {
        root_sha: record.versions[root_sha],
        second_sha: replace(
            record.versions[root_sha],
            sha256=second_sha,
            first_seen_at=second_at,
        ),
        third_sha: replace(
            record.versions[root_sha],
            sha256=third_sha,
            first_seen_at=third_at,
        ),
    }
    events = (
        VersionAdoptionEvent(root_sha, second_sha, "Approved second.", second_at),
        VersionAdoptionEvent(second_sha, third_sha, "Approved third.", third_at),
    )
    source_id = record.source_id
    active_sha = third_sha
    if fault == "missing_event":
        events = events[:1]
    elif fault == "missing_checksum":
        events = (
            VersionAdoptionEvent(missing_sha, second_sha, "Missing root.", second_at),
            events[1],
        )
    elif fault == "duplicate":
        events = (
            events[0],
            VersionAdoptionEvent(second_sha, second_sha, "Duplicate.", third_at),
        )
    elif fault == "reversed":
        events = tuple(reversed(events))
    elif fault == "branched":
        events = (
            events[0],
            VersionAdoptionEvent(root_sha, third_sha, "Branched.", third_at),
        )
    elif fault == "out_of_time":
        out_of_time = FIXED_NOW
        versions[third_sha] = replace(
            versions[third_sha],
            first_seen_at=out_of_time,
        )
        events = (
            events[0],
            VersionAdoptionEvent(
                second_sha,
                third_sha,
                "Out of time.",
                out_of_time,
            ),
        )
    elif fault == "active_not_tail":
        active_sha = second_sha
    else:
        source_id = source_id_for_first_seen(record.current_raw_path, third_sha)
    invalid = replace(
        record,
        source_id=source_id,
        state=SourceState.PENDING,
        versions=versions,
        active_content_sha256=active_sha,
        derivations={},
        active_derivation_id=None,
        adoption_events=events,
    )

    with pytest.raises(ValueError, match="chain|creation identity"):
        invalid.to_dict()


def test_uncaptured_url_descriptor_is_the_only_zero_version_record() -> None:
    fingerprint = FileFingerprint(PurePosixPath("urls/example.url.md"), 99, 1)
    descriptor = UrlDescriptorMetadata(
        "https://example.test/a", "Example", date(2026, 9, 4), fingerprint
    )
    record = SourceRecord(
        1,
        "src_" + "e" * 64,
        fingerprint.path,
        (),
        "application/x.second-brain-url-descriptor",
        fingerprint.byte_size,
        SourceState.AWAITING_APPROVAL,
        {},
        None,
        {},
        None,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
        (),
        descriptor,
    )

    assert SourceRecord.from_dict(record.to_dict()) == record
    with pytest.raises(ValueError, match="zero versions"):
        SourceRecord.from_dict({**record.to_dict(), "url_descriptor": None})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "ok"),
        ("active_content_sha256", "a" * 64),
        ("active_derivation_id", "drv_" + "b" * 64),
    ],
)
def test_zero_version_descriptor_requires_inactive_approval_state(
    field: str, value: str
) -> None:
    fingerprint = FileFingerprint(PurePosixPath("urls/example.url.md"), 99, 1)
    descriptor = UrlDescriptorMetadata(
        "https://example.test/a", "Example", date(2026, 9, 4), fingerprint
    )
    record = SourceRecord(
        1,
        "src_" + "e" * 64,
        fingerprint.path,
        (),
        "application/x.second-brain-url-descriptor",
        fingerprint.byte_size,
        SourceState.AWAITING_APPROVAL,
        {},
        None,
        {},
        None,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
        (),
        descriptor,
    )
    payload = {**record.to_dict(), field: value}

    with pytest.raises(ValueError, match="zero versions"):
        SourceRecord.from_dict(payload)


def test_derivation_record_retains_anchors() -> None:
    record = make_source_record()
    derivation = record.derivations[record.active_derivation_id or ""]

    assert derivation.anchors == (Anchor("page", "1"),)


@pytest.mark.parametrize(
    ("method", "metadata"),
    [
        ("deterministic", {}),
        ("deterministic", {"converter_id": "builtin.text", "converter_version": ""}),
        (
            "deterministic",
            {"converter_id": "builtin.text", "converter_version": "1", "note": "extra"},
        ),
        ("agent", {"handoff_id": "handoff-1", "agent_revision": "1"}),
        ("agent", {"handoff_id": "handoff-1", "agent_revision": "1", "note": 7}),
    ],
)
def test_derivation_rejects_incomplete_blank_extra_or_nonstring_metadata(
    method: str, metadata: dict[str, object]
) -> None:
    record = make_source_record()
    derivation = record.derivations[record.active_derivation_id or ""]
    payload = record.to_dict()
    payload["derivations"][derivation.derivation_id]["method"] = method
    payload["derivations"][derivation.derivation_id]["method_metadata"] = metadata

    with pytest.raises(ValueError, match="method_metadata"):
        SourceRecord.from_dict(payload)


def test_agent_derivation_method_provenance_round_trips() -> None:
    record = make_source_record()
    active_id = record.active_derivation_id or ""
    agent = replace(
        record.derivations[active_id],
        method="agent",
        method_metadata={
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "2",
            "note": "Approved OCR transcription.",
        },
    )
    record = replace(record, derivations={active_id: agent})

    assert SourceRecord.from_dict(record.to_dict()).derivations[active_id] == agent


@pytest.mark.parametrize(
    "metadata",
    (
        {
            "handoff_id": "handoff-1",
            "agent_revision": "1",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "A" * 64,
            "agent_revision": "1",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "a" * 63,
            "agent_revision": "1",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "g" * 64,
            "agent_revision": "1",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "vision-v2",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "0",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "01",
            "note": "Approved OCR transcription.",
        },
        {
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "1",
            "note": " padded ",
        },
        {
            "handoff_id": "hnd_" + "a" * 64,
            "agent_revision": "1",
            "note": "bad\0note",
        },
    ),
)
def test_agent_derivation_requires_permanent_handoff_revision_grammar(
    metadata: dict[str, str],
) -> None:
    record = make_source_record()
    active_id = record.active_derivation_id or ""
    agent = replace(
        record.derivations[active_id],
        method="agent",
        method_metadata=metadata,
    )

    with pytest.raises(ValueError, match="method_metadata"):
        replace(record, derivations={active_id: agent}).to_dict()


def test_serialization_validates_directly_constructed_derivation_metadata() -> None:
    record = make_source_record()
    active_id = record.active_derivation_id or ""
    invalid = replace(
        record.derivations[active_id], method_metadata={"converter_id": "builtin.text"}
    )

    with pytest.raises(ValueError, match="method_metadata"):
        replace(record, derivations={active_id: invalid}).to_dict()


@pytest.mark.parametrize(
    "identifier", ["drv_" + "A" * 64, "drv_short", "x_" + "b" * 64]
)
def test_derivation_id_must_be_prefixed_lowercase_sha256(identifier: str) -> None:
    record = make_source_record()
    active_id = record.active_derivation_id or ""
    payload = record.to_dict()
    payload["derivations"] = {
        identifier: {**payload["derivations"][active_id], "derivation_id": identifier}
    }
    payload["active_derivation_id"] = identifier

    with pytest.raises(ValueError, match="derivation_id"):
        SourceRecord.from_dict(payload)


def test_active_derivation_must_exist_and_match_active_content() -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="active_derivation_id"):
        SourceRecord.from_dict({**payload, "active_derivation_id": "drv_" + "e" * 64})

    payload["derivations"]["drv_" + "b" * 64]["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="active content"):
        SourceRecord.from_dict(payload)


def test_active_content_must_name_an_existing_version() -> None:
    payload = make_source_record().to_dict()

    with pytest.raises(ValueError, match="active_content_sha256"):
        SourceRecord.from_dict({**payload, "active_content_sha256": "c" * 64})


def test_source_record_schema_validates_real_records_and_zero_version_exception() -> (
    None
):
    schema = load_schema("source-record.v1.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    validator.validate(
        make_source_record(retrieval=make_retrieval_metadata()).to_dict()
    )

    fingerprint = FileFingerprint(PurePosixPath("urls/example.url.md"), 99, 1)
    descriptor = UrlDescriptorMetadata(
        "https://example.test/a", "Example", date(2026, 9, 4), fingerprint
    )
    zero_version = SourceRecord(
        1,
        "src_" + "e" * 64,
        fingerprint.path,
        (),
        "application/x.second-brain-url-descriptor",
        99,
        SourceState.AWAITING_APPROVAL,
        {},
        None,
        {},
        None,
        None,
        (),
        FIXED_NOW,
        FIXED_NOW,
        FIXED_NOW,
        (),
        descriptor,
    ).to_dict()
    validator.validate(zero_version)

    with pytest.raises(Exception):
        validator.validate({**zero_version, "url_descriptor": None})


def test_python_and_schema_require_active_content_for_file_backed_records() -> None:
    validator = Draft202012Validator(load_schema("source-record.v1.schema.json"))
    payload = make_source_record().to_dict()
    payload["active_content_sha256"] = None
    payload["active_derivation_id"] = None

    with pytest.raises(ValueError, match="file-backed records require"):
        SourceRecord.from_dict(payload)
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    "output_path",
    ["sources/extracted/../escape.md", "sources/extracted/./escape.md"],
)
def test_source_record_schema_rejects_immediate_output_path_traversal(
    output_path: str,
) -> None:
    validator = Draft202012Validator(load_schema("source-record.v1.schema.json"))
    payload = make_source_record().to_dict()
    active_id = payload["active_derivation_id"]
    assert isinstance(active_id, str)
    payload["derivations"][active_id]["output_path"] = output_path

    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_source_record_schema_rejects_semantically_invalid_timestamp() -> None:
    validator = Draft202012Validator(
        load_schema("source-record.v1.schema.json"),
        format_checker=RFC3339_FORMAT_CHECKER,
    )
    payload = make_source_record().to_dict()
    payload["created_at"] = "2026-99-99T99:99:99Z"

    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    "name", ["page-frontmatter.v1.schema.json", "question-frontmatter.v1.schema.json"]
)
def test_markdown_frontmatter_schemas_are_valid_draft_2020_12(name: str) -> None:
    schema = load_schema(name)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_page_frontmatter_schema_accepts_only_the_v1_contract() -> None:
    validator = Draft202012Validator(load_schema("page-frontmatter.v1.schema.json"))
    valid = {
        "id": "page_example",
        "title": "Example",
        "description": "An example page.",
        "type": "concept",
        "aliases": ["Sample"],
        "created": "2026-09-04",
        "updated": "2026-09-04",
    }

    validator.validate(valid)
    with pytest.raises(Exception):
        validator.validate({**valid, "body": "Wiki prose is outside frontmatter."})
    with pytest.raises(Exception):
        validator.validate(
            {key: value for key, value in valid.items() if key != "description"}
        )


@pytest.mark.parametrize(
    "answer_status", ["answered", "partial", "unanswered", "conflicted"]
)
def test_question_frontmatter_schema_accepts_each_answer_status(
    answer_status: str,
) -> None:
    validator = Draft202012Validator(load_schema("question-frontmatter.v1.schema.json"))

    validator.validate(
        {
            "id": "question_example",
            "title": "What is the example?",
            "description": "Tracks the example answer.",
            "answer_status": answer_status,
            "corpus_revision": "a" * 64,
            "last_researched": "2026-09-04",
        }
    )


def test_question_frontmatter_schema_rejects_unknown_status_and_wiki_prose() -> None:
    validator = Draft202012Validator(load_schema("question-frontmatter.v1.schema.json"))
    valid = {
        "id": "question_example",
        "title": "What is the example?",
        "description": "Tracks the example answer.",
        "answer_status": "answered",
        "corpus_revision": "a" * 64,
        "last_researched": "2026-09-04",
    }

    with pytest.raises(Exception):
        validator.validate({**valid, "answer_status": "complete"})
    with pytest.raises(Exception):
        validator.validate({**valid, "markdown": "# Answer"})
