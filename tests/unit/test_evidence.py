from dataclasses import replace
import json
from pathlib import PurePosixPath

import pytest

from brainlib.contracts import Anchor
from brainlib.diagnostics import Diagnostic
from brainlib.evidence import (
    CitationRef,
    EvidenceItem,
    EvidencePacket,
    RevalidatedCitation,
    SearchPassName,
    WikiEvidencePacket,
    WikiRecordMatch,
)
from tests.helpers_knowledge import (
    CONTENT_SHA256,
    DERIVATION_ID,
    SOURCE_ID,
    make_completed_pass,
)


def test_evidence_packet_serializes_source_versions_and_counterevidence() -> None:
    revision = "d" * 64
    packet = EvidencePacket(
        question_id="question-topic",
        corpus_revision=revision,
        passes=(
            make_completed_pass("discovery", ("Topic",), revision, 1),
            make_completed_pass("expansion", ("Alpha",), revision, 2),
            make_completed_pass("verification", ("exception",), revision, 3),
        ),
        support=(
            EvidenceItem(
                SOURCE_ID,
                CONTENT_SHA256,
                DERIVATION_ID,
                Anchor("line", "1"),
                "supports Topic",
            ),
        ),
        counterevidence=(
            EvidenceItem(
                "src_" + "b" * 64,
                "b" * 64,
                "drv_" + "c" * 64,
                Anchor("line", "8"),
                "qualifies Topic",
            ),
        ),
        coverage_gaps=(Diagnostic("source_failed", "src-c is failed"),),
    )
    assert EvidencePacket.from_json(packet.to_json()) == packet


@pytest.mark.parametrize(
    "passes",
    (
        ("discovery", "verification", "expansion"),
        ("discovery", "discovery", "verification"),
        ("discovery", "expansion", "verification", "verification"),
    ),
)
def test_evidence_packet_rejects_reordered_duplicate_or_fourth_pass(
    passes: tuple[SearchPassName, ...]
) -> None:
    revision = "d" * 64
    with pytest.raises(ValueError, match="discovery, expansion, verification"):
        EvidencePacket(
            "question-topic",
            revision,
            tuple(
                make_completed_pass(name, (name,), revision, index)
                for index, name in enumerate(passes)
            ),
            (),
            (),
            (),
        )


def test_evidence_packet_allows_completed_zero_hit_pass_but_rejects_undrained_or_empty_terms() -> None:
    revision = "d" * 64
    zero_hit = make_completed_pass(
        "discovery", ("absent",), revision, 1, candidate_count=0, match_count=0
    )
    assert zero_hit.complete and zero_hit.candidate_count == 0
    with pytest.raises(ValueError, match="complete"):
        EvidencePacket(
            "question-topic",
            revision,
            (
                replace(zero_hit, complete=False),
                make_completed_pass("expansion", ("related",), revision, 2),
                make_completed_pass("verification", ("exception",), revision, 3),
            ),
            (),
            (),
            (),
        )
    with pytest.raises(ValueError, match="nonempty term"):
        replace(zero_hit, terms=())


def test_evidence_packet_from_json_rejects_missing_pass() -> None:
    with pytest.raises(ValueError, match="exactly three"):
        EvidencePacket.from_json(
            '{"question_id":"question-topic","corpus_revision":"'
            + "d" * 64
            + '","passes":[],"support":[],"counterevidence":[],"coverage_gaps":[]}'
        )


def test_wiki_evidence_packet_uses_document_qualified_citation_refs() -> None:
    citation = RevalidatedCitation(
        PurePosixPath("wiki/questions/what-is-alpha.md"),
        "cite-alpha-line-1",
        SOURCE_ID,
        CONTENT_SHA256,
        DERIVATION_ID,
        Anchor("line", "1"),
    )
    packet = WikiEvidencePacket(
        question_id="question-alpha",
        corpus_revision="d" * 64,
        search_run_id="srch_" + "1" * 32,
        matched_records=(
            WikiRecordMatch(
                PurePosixPath("wiki/questions/what-is-alpha.md"),
                "question-alpha",
                ("Alpha",),
            ),
        ),
        revalidated_citations=(citation,),
        supporting_citations=(CitationRef(citation.document_path, citation.citation_id),),
        counterevidence_citations=(),
        contradictions=(),
        coverage_gaps=(),
        complete=True,
    )
    assert WikiEvidencePacket.from_json(packet.to_json()) == packet
    with pytest.raises(ValueError, match="revalidated citation"):
        replace(
            packet,
            supporting_citations=(
                CitationRef(PurePosixPath("wiki/pages/other.md"), citation.citation_id),
            ),
        )


def test_wiki_evidence_packet_rejects_noncanonical_or_duplicate_record_identities() -> None:
    packet = _wiki_packet()
    with pytest.raises(ValueError, match="record_id"):
        replace(
            packet,
            matched_records=(
                WikiRecordMatch(
                    PurePosixPath("wiki/questions/what-is-alpha.md"),
                    "Question Alpha!",
                    ("Alpha",),
                ),
            ),
        )
    with pytest.raises(ValueError, match="record identity"):
        replace(
            packet,
            matched_records=(
                packet.matched_records[0],
                WikiRecordMatch(
                    packet.matched_records[0].path,
                    packet.matched_records[0].record_id,
                    ("Beta",),
                ),
            ),
        )
    encoded = json.loads(packet.to_json())
    encoded["matched_records"].append(
        {
            "path": "wiki/questions/what-is-alpha.md",
            "record_id": "question-alpha",
            "matched_terms": ["Beta"],
        }
    )
    with pytest.raises(ValueError, match="record identity"):
        WikiEvidencePacket.from_json(json.dumps(encoded))


def _wiki_packet() -> WikiEvidencePacket:
    citation = RevalidatedCitation(
        PurePosixPath("wiki/questions/what-is-alpha.md"),
        "cite-alpha-line-1",
        SOURCE_ID,
        CONTENT_SHA256,
        DERIVATION_ID,
        Anchor("line", "1"),
    )
    return WikiEvidencePacket(
        question_id="question-alpha",
        corpus_revision="d" * 64,
        search_run_id="srch_" + "1" * 32,
        matched_records=(
            WikiRecordMatch(
                PurePosixPath("wiki/questions/what-is-alpha.md"),
                "question-alpha",
                ("Alpha",),
            ),
        ),
        revalidated_citations=(citation,),
        supporting_citations=(CitationRef(citation.document_path, citation.citation_id),),
        counterevidence_citations=(),
        contradictions=(),
        coverage_gaps=(),
        complete=True,
    )
