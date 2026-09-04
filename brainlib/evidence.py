"""Immutable, JSON-serializable research-to-curator evidence handoffs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, TypeAlias, cast

from .contracts import Anchor
from .diagnostics import Diagnostic, JSONValue


SearchPassName = Literal["discovery", "expansion", "verification"]

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{64}$")
_DERIVATION_ID = re.compile(r"^drv_[0-9a-f]{64}$")
_SEARCH_RUN_ID = re.compile(r"^srch_[0-9a-f]{32}$")
_QUESTION_ID = re.compile(r"^question-[a-z0-9]+(?:-[a-z0-9]+)*$")
_CITATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ANCHOR_KINDS = frozenset({"line", "page", "slide", "sheet", "section", "row", "block"})
_BLOCKING_GAP_CODES = frozenset(
    {"search_run_incomplete", "search_run_expired", "search_run_stale", "search_spool_limit"}
)


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _require_nonempty_string(value: object, label: str) -> str:
    result = _require_string(value, label)
    if not result.strip() or any(char in result for char in "\r\n\0"):
        raise ValueError(f"{label} must be a nonempty string")
    return result


def _require_hex(value: object, label: str) -> str:
    result = _require_string(value, label)
    if not _HEX_64.fullmatch(result):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return result


def _require_question_id(value: object) -> str:
    result = _require_string(value, "question_id")
    if not _QUESTION_ID.fullmatch(result):
        raise ValueError("question_id must be a canonical question identifier")
    return result


def _require_run_id(value: object) -> str:
    result = _require_string(value, "run_id")
    if not _SEARCH_RUN_ID.fullmatch(result):
        raise ValueError("run_id must be srch_ followed by 32 lowercase hexadecimal characters")
    return result


def _require_source_id(value: object) -> str:
    result = _require_string(value, "source_id")
    if not _SOURCE_ID.fullmatch(result):
        raise ValueError("source_id must be src_ followed by 64 lowercase hexadecimal characters")
    return result


def _require_derivation_id(value: object) -> str:
    result = _require_string(value, "derivation_id")
    if not _DERIVATION_ID.fullmatch(result):
        raise ValueError("derivation_id must be drv_ followed by 64 lowercase hexadecimal characters")
    return result


def _require_tuple(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be a tuple")
    return value


def _require_canonical_path(value: object, label: str, *, wiki: bool = False) -> PurePosixPath:
    if not isinstance(value, PurePosixPath):
        raise ValueError(f"{label} must be a PurePosixPath")
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    if wiki and (value.parts[:2] not in {("wiki", "pages"), ("wiki", "questions")} or value.suffix != ".md"):
        raise ValueError("record path must be under wiki/pages or wiki/questions")
    return value


def _validate_anchor(anchor: object) -> Anchor:
    if not isinstance(anchor, Anchor):
        raise ValueError("anchor must be an Anchor")
    if anchor.kind not in _ANCHOR_KINDS or not isinstance(anchor.value, str) or not anchor.value or any(
        char in anchor.value for char in "\r\n\0"
    ):
        raise ValueError("anchor must have a canonical kind and nonempty value")
    return anchor


def _diagnostic_key(item: Diagnostic) -> tuple[str, str, str, str]:
    return (
        "" if item.path is None else item.path.as_posix(),
        item.code,
        item.message,
        json.dumps(dict(item.details), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")),
    )


def _validate_diagnostics(value: object, label: str) -> tuple[Diagnostic, ...]:
    entries = _require_tuple(value, label)
    if any(not isinstance(item, Diagnostic) for item in entries):
        raise ValueError(f"{label} must contain diagnostics")
    diagnostics = cast(tuple[Diagnostic, ...], entries)
    if tuple(sorted(diagnostics, key=_diagnostic_key)) != diagnostics or len(set(_diagnostic_key(item) for item in diagnostics)) != len(diagnostics):
        raise ValueError(f"{label} must be sorted and unique")
    return diagnostics


@dataclass(frozen=True)
class SearchPageRecord:
    page_index: int
    match_count: int
    result_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int) or self.page_index < 0:
            raise ValueError("page_index must be nonnegative")
        if isinstance(self.match_count, bool) or not isinstance(self.match_count, int) or self.match_count < 0:
            raise ValueError("match_count must be nonnegative")
        _require_hex(self.result_sha256, "result_sha256")


@dataclass(frozen=True)
class SearchPassRecord:
    name: SearchPassName
    terms: tuple[str, ...]
    run_id: str
    corpus_revision: str
    candidate_count: int
    candidate_manifest_sha256: str
    pages: tuple[SearchPageRecord, ...]
    complete: bool
    coverage_gaps: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if self.name not in {"discovery", "expansion", "verification"}:
            raise ValueError("name must be discovery, expansion, or verification")
        terms = _require_tuple(self.terms, "terms")
        if not terms:
            raise ValueError("terms must contain at least one nonempty term")
        for term in terms:
            _require_nonempty_string(term, "term")
        _require_run_id(self.run_id)
        _require_hex(self.corpus_revision, "corpus_revision")
        _require_hex(self.candidate_manifest_sha256, "candidate_manifest_sha256")
        if isinstance(self.candidate_count, bool) or not isinstance(self.candidate_count, int) or self.candidate_count < 0:
            raise ValueError("candidate_count must be nonnegative")
        pages = _require_tuple(self.pages, "pages")
        if not pages or any(not isinstance(page, SearchPageRecord) for page in pages):
            raise ValueError("pages must contain at least one SearchPageRecord")
        if tuple(page.page_index for page in pages) != tuple(range(len(pages))):
            raise ValueError("page indexes must be contiguous beginning at zero")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        gaps = _validate_diagnostics(self.coverage_gaps, "coverage_gaps")
        if self.complete and gaps:
            raise ValueError("a complete pass must have no coverage gaps")


@dataclass(frozen=True)
class EvidenceItem:
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor
    passage: str

    def __post_init__(self) -> None:
        _require_source_id(self.source_id)
        _require_hex(self.content_sha256, "content_sha256")
        _require_derivation_id(self.derivation_id)
        _validate_anchor(self.anchor)
        _require_nonempty_string(self.passage, "passage")


def _evidence_key(item: EvidenceItem) -> tuple[str, str, str, str, str, str]:
    return (item.source_id, item.content_sha256, item.derivation_id, item.anchor.kind, item.anchor.value, item.passage)


@dataclass(frozen=True)
class EvidencePacket:
    question_id: str
    corpus_revision: str
    passes: tuple[SearchPassRecord, SearchPassRecord, SearchPassRecord]
    support: tuple[EvidenceItem, ...]
    counterevidence: tuple[EvidenceItem, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    freshness_probe_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_question_id(self.question_id)
        _require_hex(self.corpus_revision, "corpus_revision")
        passes = _require_tuple(self.passes, "passes")
        if len(passes) != 3 or any(not isinstance(item, SearchPassRecord) for item in passes) or tuple(item.name for item in passes) != ("discovery", "expansion", "verification"):
            raise ValueError("passes must be exactly three: discovery, expansion, verification")
        if len({item.run_id for item in passes}) != 3:
            raise ValueError("passes must have distinct run IDs")
        if any(item.corpus_revision != self.corpus_revision for item in passes):
            raise ValueError("every pass must use the packet corpus_revision")
        if any(not item.complete for item in passes):
            raise ValueError("every logical search pass must be complete")
        for label, entries in (("support", self.support), ("counterevidence", self.counterevidence)):
            values = _require_tuple(entries, label)
            if any(not isinstance(item, EvidenceItem) for item in values):
                raise ValueError(f"{label} must contain evidence items")
            if tuple(sorted(values, key=_evidence_key)) != values or len(set(_evidence_key(item) for item in values)) != len(values):
                raise ValueError(f"{label} must be sorted and unique")
        gaps = _validate_diagnostics(self.coverage_gaps, "coverage_gaps")
        if any(item.code in _BLOCKING_GAP_CODES for item in gaps):
            raise ValueError("coverage_gaps cannot include an incomplete search run")
        probes = _require_tuple(self.freshness_probe_source_ids, "freshness_probe_source_ids")
        if any(not isinstance(item, str) for item in probes):
            raise ValueError("freshness_probe_source_ids must contain source IDs")
        for source_id in probes:
            _require_source_id(source_id)
        if tuple(sorted(probes)) != probes or len(set(probes)) != len(probes):
            raise ValueError("freshness_probe_source_ids must be sorted and unique")

    def to_json(self) -> str:
        return _dump_json(_evidence_packet_dict(self))

    @classmethod
    def from_json(cls, value: str) -> "EvidencePacket":
        obj = _load_json_object(value)
        _required_and_optional_fields(
            obj,
            {"question_id", "corpus_revision", "passes", "support", "counterevidence", "coverage_gaps"},
            {"freshness_probe_source_ids"},
            "evidence packet",
        )
        return cls(
            _require_string(obj["question_id"], "question_id"),
            _require_string(obj["corpus_revision"], "corpus_revision"),
            _parse_passes(obj["passes"]),
            _parse_evidence_items(obj["support"], "support"),
            _parse_evidence_items(obj["counterevidence"], "counterevidence"),
            _parse_diagnostics(obj["coverage_gaps"], "coverage_gaps"),
            _parse_source_ids(obj.get("freshness_probe_source_ids", []), "freshness_probe_source_ids"),
        )


@dataclass(frozen=True)
class WikiRecordMatch:
    path: PurePosixPath
    record_id: str
    matched_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_canonical_path(self.path, "path", wiki=True)
        record_id = _require_nonempty_string(self.record_id, "record_id")
        if not _QUESTION_ID.fullmatch(record_id) and not re.fullmatch(
            r"^[a-z0-9]+(?:-[a-z0-9]+)*$", record_id
        ):
            raise ValueError("record_id must be a canonical record identifier")
        terms = _require_tuple(self.matched_terms, "matched_terms")
        if not terms:
            raise ValueError("matched_terms must be nonempty")
        for term in terms:
            _require_nonempty_string(term, "matched term")
        if tuple(sorted(terms)) != terms or len(set(terms)) != len(terms):
            raise ValueError("matched_terms must be sorted and unique")


@dataclass(frozen=True)
class RevalidatedCitation:
    document_path: PurePosixPath
    citation_id: str
    source_id: str
    content_sha256: str
    derivation_id: str
    anchor: Anchor

    def __post_init__(self) -> None:
        _require_canonical_path(self.document_path, "document_path", wiki=True)
        if not isinstance(self.citation_id, str) or not _CITATION_ID.fullmatch(self.citation_id):
            raise ValueError("citation_id must be canonical")
        _require_source_id(self.source_id)
        _require_hex(self.content_sha256, "content_sha256")
        _require_derivation_id(self.derivation_id)
        _validate_anchor(self.anchor)


@dataclass(frozen=True, order=True)
class CitationRef:
    document_path: PurePosixPath
    citation_id: str

    def __post_init__(self) -> None:
        _require_canonical_path(self.document_path, "document_path", wiki=True)
        if not isinstance(self.citation_id, str) or not _CITATION_ID.fullmatch(self.citation_id):
            raise ValueError("citation_id must be canonical")


def _match_key(item: WikiRecordMatch) -> tuple[str, str, tuple[str, ...]]:
    return (item.path.as_posix(), item.record_id, item.matched_terms)


def _citation_key(item: RevalidatedCitation) -> tuple[str, str, str, str, str, str, str]:
    return (item.document_path.as_posix(), item.citation_id, item.source_id, item.content_sha256, item.derivation_id, item.anchor.kind, item.anchor.value)


@dataclass(frozen=True)
class WikiEvidencePacket:
    question_id: str
    corpus_revision: str
    search_run_id: str
    matched_records: tuple[WikiRecordMatch, ...]
    revalidated_citations: tuple[RevalidatedCitation, ...]
    supporting_citations: tuple[CitationRef, ...]
    counterevidence_citations: tuple[CitationRef, ...]
    contradictions: tuple[Diagnostic, ...]
    coverage_gaps: tuple[Diagnostic, ...]
    complete: bool

    def __post_init__(self) -> None:
        _require_question_id(self.question_id)
        _require_hex(self.corpus_revision, "corpus_revision")
        _require_run_id(self.search_run_id)
        records = _require_tuple(self.matched_records, "matched_records")
        if not records or any(not isinstance(item, WikiRecordMatch) for item in records):
            raise ValueError("matched_records must be nonempty WikiRecordMatch values")
        if tuple(sorted(records, key=_match_key)) != records or len({item.path for item in records}) != len(records) or len({item.record_id for item in records}) != len(records):
            raise ValueError("matched_records must be sorted with no duplicate record identity")
        citations = _require_tuple(self.revalidated_citations, "revalidated_citations")
        if not citations or any(not isinstance(item, RevalidatedCitation) for item in citations):
            raise ValueError("revalidated_citations must be nonempty")
        if tuple(sorted(citations, key=_citation_key)) != citations or len(set((item.document_path, item.citation_id) for item in citations)) != len(citations):
            raise ValueError("revalidated_citations must be sorted and unique")
        known = {CitationRef(item.document_path, item.citation_id) for item in citations}
        support = self._validate_refs(self.supporting_citations, "supporting_citations")
        counter = self._validate_refs(self.counterevidence_citations, "counterevidence_citations")
        if not support:
            raise ValueError("supporting_citations must be nonempty")
        if not set(support).issubset(known) or not set(counter).issubset(known):
            raise ValueError("citation refs must resolve to a revalidated citation")
        if set(support) & set(counter):
            raise ValueError("supporting and counterevidence citations must not overlap")
        _validate_diagnostics(self.contradictions, "contradictions")
        gaps = _validate_diagnostics(self.coverage_gaps, "coverage_gaps")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.complete and gaps:
            raise ValueError("a complete wiki evidence packet must have no coverage gaps")

    @staticmethod
    def _validate_refs(value: object, label: str) -> tuple[CitationRef, ...]:
        refs = _require_tuple(value, label)
        if any(not isinstance(item, CitationRef) for item in refs):
            raise ValueError(f"{label} must contain CitationRef values")
        result = cast(tuple[CitationRef, ...], refs)
        if tuple(sorted(result)) != result or len(set(result)) != len(result):
            raise ValueError(f"{label} must be sorted and unique")
        return result

    def to_json(self) -> str:
        return _dump_json(_wiki_packet_dict(self))

    @classmethod
    def from_json(cls, value: str) -> "WikiEvidencePacket":
        obj = _load_json_object(value)
        _exact_fields(obj, {"question_id", "corpus_revision", "search_run_id", "matched_records", "revalidated_citations", "supporting_citations", "counterevidence_citations", "contradictions", "coverage_gaps", "complete"}, "wiki evidence packet")
        return cls(
            _require_string(obj["question_id"], "question_id"),
            _require_string(obj["corpus_revision"], "corpus_revision"),
            _require_string(obj["search_run_id"], "search_run_id"),
            _parse_matches(obj["matched_records"]),
            _parse_citations(obj["revalidated_citations"]),
            _parse_refs(obj["supporting_citations"], "supporting_citations"),
            _parse_refs(obj["counterevidence_citations"], "counterevidence_citations"),
            _parse_diagnostics(obj["contradictions"], "contradictions"),
            _parse_diagnostics(obj["coverage_gaps"], "coverage_gaps"),
            _require_bool(obj["complete"], "complete"),
        )


CuratorEvidence: TypeAlias = WikiEvidencePacket | EvidencePacket


def _dump_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json_object(value: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise ValueError("packet JSON must be a string")
    try:
        decoded = json.loads(value, object_pairs_hook=_no_duplicate_object, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON number: {item}")))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid packet JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError("packet JSON root must be an object")
    return cast(dict[str, object], decoded)


def _exact_fields(value: Mapping[str, object], fields: set[str], label: str) -> None:
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        pieces = []
        if missing:
            pieces.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            pieces.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"{label} has " + "; ".join(pieces) + " fields")


def _required_and_optional_fields(
    value: Mapping[str, object], required: set[str], optional: set[str], label: str
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing or unknown:
        pieces = []
        if missing:
            pieces.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            pieces.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"{label} has " + "; ".join(pieces) + " fields")


def _require_array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _anchor_dict(value: Anchor) -> dict[str, str]:
    return {"kind": value.kind, "value": value.value}


def _parse_anchor(value: object) -> Anchor:
    obj = _require_object(value, "anchor")
    _exact_fields(obj, {"kind", "value"}, "anchor")
    anchor = Anchor(cast(Any, _require_string(obj["kind"], "anchor kind")), _require_string(obj["value"], "anchor value"))
    return _validate_anchor(anchor)


def _diagnostic_dict(item: Diagnostic) -> dict[str, object]:
    return {"code": item.code, "message": item.message, "path": None if item.path is None else item.path.as_posix(), "details": dict(item.details)}


def _parse_diagnostics(value: object, label: str) -> tuple[Diagnostic, ...]:
    result = []
    for raw in _require_array(value, label):
        obj = _require_object(raw, "diagnostic")
        _exact_fields(obj, {"code", "message", "path", "details"}, "diagnostic")
        path_value = obj["path"]
        if path_value is None:
            path = None
        else:
            path = _parse_relative_path(path_value, "diagnostic path")
        details = obj["details"]
        if not isinstance(details, dict):
            raise ValueError("diagnostic details must be an object")
        result.append(Diagnostic(_require_nonempty_string(obj["code"], "diagnostic code"), _require_nonempty_string(obj["message"], "diagnostic message"), path, cast(Mapping[str, JSONValue], details)))
    return tuple(result)


def _parse_relative_path(value: object, label: str) -> PurePosixPath:
    result = _require_string(value, label)
    path = PurePosixPath(result)
    if result != path.as_posix() or path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a canonical relative path")
    return path


def _pass_dict(item: SearchPassRecord) -> dict[str, object]:
    return {"name": item.name, "terms": list(item.terms), "run_id": item.run_id, "corpus_revision": item.corpus_revision, "candidate_count": item.candidate_count, "candidate_manifest_sha256": item.candidate_manifest_sha256, "pages": [{"page_index": page.page_index, "match_count": page.match_count, "result_sha256": page.result_sha256} for page in item.pages], "complete": item.complete, "coverage_gaps": [_diagnostic_dict(gap) for gap in item.coverage_gaps]}


def _parse_passes(value: object) -> tuple[SearchPassRecord, ...]:
    result = []
    for raw in _require_array(value, "passes"):
        obj = _require_object(raw, "search pass")
        _exact_fields(obj, {"name", "terms", "run_id", "corpus_revision", "candidate_count", "candidate_manifest_sha256", "pages", "complete", "coverage_gaps"}, "search pass")
        pages = []
        for page_raw in _require_array(obj["pages"], "pages"):
            page = _require_object(page_raw, "search page")
            _exact_fields(page, {"page_index", "match_count", "result_sha256"}, "search page")
            pages.append(SearchPageRecord(_require_int(page["page_index"], "page_index"), _require_int(page["match_count"], "match_count"), _require_string(page["result_sha256"], "result_sha256")))
        result.append(SearchPassRecord(cast(SearchPassName, _require_string(obj["name"], "name")), tuple(_require_string(term, "term") for term in _require_array(obj["terms"], "terms")), _require_string(obj["run_id"], "run_id"), _require_string(obj["corpus_revision"], "corpus_revision"), _require_int(obj["candidate_count"], "candidate_count"), _require_string(obj["candidate_manifest_sha256"], "candidate_manifest_sha256"), tuple(pages), _require_bool(obj["complete"], "complete"), _parse_diagnostics(obj["coverage_gaps"], "coverage_gaps")))
    return tuple(result)


def _item_dict(item: EvidenceItem) -> dict[str, object]:
    return {"source_id": item.source_id, "content_sha256": item.content_sha256, "derivation_id": item.derivation_id, "anchor": _anchor_dict(item.anchor), "passage": item.passage}


def _parse_evidence_items(value: object, label: str) -> tuple[EvidenceItem, ...]:
    result = []
    for raw in _require_array(value, label):
        obj = _require_object(raw, "evidence item")
        _exact_fields(obj, {"source_id", "content_sha256", "derivation_id", "anchor", "passage"}, "evidence item")
        result.append(EvidenceItem(_require_string(obj["source_id"], "source_id"), _require_string(obj["content_sha256"], "content_sha256"), _require_string(obj["derivation_id"], "derivation_id"), _parse_anchor(obj["anchor"]), _require_string(obj["passage"], "passage")))
    return tuple(result)


def _parse_source_ids(value: object, label: str) -> tuple[str, ...]:
    return tuple(_require_string(item, "source_id") for item in _require_array(value, label))


def _evidence_packet_dict(packet: EvidencePacket) -> dict[str, object]:
    return {"question_id": packet.question_id, "corpus_revision": packet.corpus_revision, "passes": [_pass_dict(item) for item in packet.passes], "support": [_item_dict(item) for item in packet.support], "counterevidence": [_item_dict(item) for item in packet.counterevidence], "coverage_gaps": [_diagnostic_dict(item) for item in packet.coverage_gaps], "freshness_probe_source_ids": list(packet.freshness_probe_source_ids)}


def _ref_dict(item: CitationRef) -> dict[str, str]:
    return {"document_path": item.document_path.as_posix(), "citation_id": item.citation_id}


def _parse_refs(value: object, label: str) -> tuple[CitationRef, ...]:
    result = []
    for raw in _require_array(value, label):
        obj = _require_object(raw, "citation ref")
        _exact_fields(obj, {"document_path", "citation_id"}, "citation ref")
        result.append(CitationRef(_parse_relative_path(obj["document_path"], "document_path"), _require_string(obj["citation_id"], "citation_id")))
    return tuple(result)


def _match_dict(item: WikiRecordMatch) -> dict[str, object]:
    return {"path": item.path.as_posix(), "record_id": item.record_id, "matched_terms": list(item.matched_terms)}


def _parse_matches(value: object) -> tuple[WikiRecordMatch, ...]:
    result = []
    for raw in _require_array(value, "matched_records"):
        obj = _require_object(raw, "wiki record match")
        _exact_fields(obj, {"path", "record_id", "matched_terms"}, "wiki record match")
        result.append(WikiRecordMatch(_parse_relative_path(obj["path"], "path"), _require_string(obj["record_id"], "record_id"), tuple(_require_string(term, "matched term") for term in _require_array(obj["matched_terms"], "matched_terms"))))
    return tuple(result)


def _citation_dict(item: RevalidatedCitation) -> dict[str, object]:
    return {"document_path": item.document_path.as_posix(), "citation_id": item.citation_id, "source_id": item.source_id, "content_sha256": item.content_sha256, "derivation_id": item.derivation_id, "anchor": _anchor_dict(item.anchor)}


def _parse_citations(value: object) -> tuple[RevalidatedCitation, ...]:
    result = []
    for raw in _require_array(value, "revalidated_citations"):
        obj = _require_object(raw, "revalidated citation")
        _exact_fields(obj, {"document_path", "citation_id", "source_id", "content_sha256", "derivation_id", "anchor"}, "revalidated citation")
        result.append(RevalidatedCitation(_parse_relative_path(obj["document_path"], "document_path"), _require_string(obj["citation_id"], "citation_id"), _require_string(obj["source_id"], "source_id"), _require_string(obj["content_sha256"], "content_sha256"), _require_string(obj["derivation_id"], "derivation_id"), _parse_anchor(obj["anchor"])))
    return tuple(result)


def _wiki_packet_dict(packet: WikiEvidencePacket) -> dict[str, object]:
    return {"question_id": packet.question_id, "corpus_revision": packet.corpus_revision, "search_run_id": packet.search_run_id, "matched_records": [_match_dict(item) for item in packet.matched_records], "revalidated_citations": [_citation_dict(item) for item in packet.revalidated_citations], "supporting_citations": [_ref_dict(item) for item in packet.supporting_citations], "counterevidence_citations": [_ref_dict(item) for item in packet.counterevidence_citations], "contradictions": [_diagnostic_dict(item) for item in packet.contradictions], "coverage_gaps": [_diagnostic_dict(item) for item in packet.coverage_gaps], "complete": packet.complete}
