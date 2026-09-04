"""Build strict, current wiki evidence packets from a completed wiki search."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .citations import parse_citation_definitions, validate_citations
from .contracts import compute_corpus_revision
from .diagnostics import Diagnostic
from .evidence import (
    CitationRef,
    RevalidatedCitation,
    WikiEvidencePacket,
    WikiRecordMatch,
)
from .layout import RepoPaths
from .ledger import LedgerStore
from .locking import SourceWriteLock
from .search import (
    SearchResult,
    SearchRunBlocked,
    canonical_wiki_logical_path,
    complete_search_run,
)
from .search_runs import _verify_search_run_proof_locked
from .wiki_models import parse_page, parse_question
from .graph import _load_live_documents


class WikiEvidenceError(ValueError):
    def __init__(self, diagnostic: Diagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


def build_wiki_evidence_packet(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    question_id: str,
    search_pages: Sequence[SearchResult],
    matched_paths: tuple[PurePosixPath, ...],
    supporting_citations: tuple[CitationRef, ...],
    counterevidence_citations: tuple[CitationRef, ...],
    contradictions: tuple[Diagnostic, ...] = (),
    coverage_gaps: tuple[Diagnostic, ...] = (),
) -> WikiEvidencePacket:
    """Revalidate selected current wiki records and their exact citations."""

    try:
        proof = complete_search_run(search_pages)
    except (TypeError, ValueError) as error:
        raise _search_error("The wiki search pages are not a complete retained run.") from error
    if (
        proof.scope != "wiki"
        or proof.mode != "research"
        or proof.pass_name is not None
    ):
        raise _search_error("Wiki evidence requires a completed research wiki search.")
    if coverage_gaps:
        raise _search_error("Wiki evidence cannot be built with coverage gaps.")

    try:
        with SourceWriteLock.acquire(paths.lock):
            with SourceWriteLock.acquire(paths.root / ".brain/wiki-write.lock"):
                _verify_search_run_proof_locked(paths, ledger, proof)
                selected_paths = _canonical_matched_paths(paths, matched_paths)
                documents = _load_live_documents(paths)
                # The captured bytes are only trustworthy if the retained
                # operand proof still authenticates after that capture.
                _verify_search_run_proof_locked(
                    paths,
                    ledger,
                    proof,
                    captured_wiki_documents=documents,
                )
                return _build_locked(
                    paths,
                    ledger,
                    question_id=question_id,
                    proof_terms=proof.terms,
                    proof_run_id=proof.run_id,
                    proof_revision=proof.corpus_revision,
                    search_pages=search_pages,
                    matched_paths=selected_paths,
                    documents=documents,
                    supporting_citations=supporting_citations,
                    counterevidence_citations=counterevidence_citations,
                    contradictions=contradictions,
                )
    except SearchRunBlocked as error:
        raise _search_error(error.diagnostic.message) from error
    except WikiEvidenceError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise _citation_error("Wiki evidence could not be revalidated safely.") from error


def _build_locked(
    paths: RepoPaths,
    ledger: LedgerStore,
    *,
    question_id: str,
    proof_terms: tuple[str, ...],
    proof_run_id: str,
    proof_revision: str,
    search_pages: Sequence[SearchResult],
    matched_paths: tuple[PurePosixPath, ...],
    documents: dict[PurePosixPath, tuple[Path, str]],
    supporting_citations: tuple[CitationRef, ...],
    counterevidence_citations: tuple[CitationRef, ...],
    contradictions: tuple[Diagnostic, ...],
) -> WikiEvidencePacket:
    if (
        not matched_paths
        or tuple(sorted(matched_paths, key=lambda item: item.as_posix())) != matched_paths
        or len(set(matched_paths)) != len(matched_paths)
    ):
        raise _citation_error("Matched wiki paths must be sorted, unique, and nonempty.")
    if (
        not supporting_citations
        or tuple(sorted(supporting_citations)) != supporting_citations
        or len(set(supporting_citations)) != len(supporting_citations)
        or tuple(sorted(counterevidence_citations)) != counterevidence_citations
        or len(set(counterevidence_citations)) != len(counterevidence_citations)
        or set(supporting_citations) & set(counterevidence_citations)
    ):
        raise _citation_error("Citation references are not sorted, unique, and disjoint.")

    matched_by_path: dict[PurePosixPath, set[str]] = {}
    for page in search_pages:
        for match in page.matches:
            if match.kind != "match":
                continue
            exact_terms = {term for term in proof_terms if term in match.text}
            if exact_terms:
                matched_by_path.setdefault(match.path, set()).update(exact_terms)

    selected_documents: dict[Path, str] = {}
    records: list[WikiRecordMatch] = []
    for logical in matched_paths:
        terms = tuple(sorted(matched_by_path.get(logical, ())))
        if not terms:
            raise _citation_error("A matched wiki record has no exact search-term match.")
        try:
            document_path, text = documents[logical]
        except KeyError as error:
            raise _citation_error("A matched wiki path is not a current direct record.") from error
        try:
            model = (
                parse_page(document_path, text=text)
                if logical.parts[1] == "pages"
                else parse_question(document_path, text=text)
            )
        except ValueError as error:
            raise _citation_error("A matched wiki record cannot be parsed safely.") from error
        record_id = model.page_id if logical.parts[1] == "pages" else model.question_id
        selected_documents[document_path] = text
        records.append(WikiRecordMatch(logical, record_id, terms))

    report = validate_citations(paths, ledger, selected_documents, full=False)
    if not report.ok:
        raise _citation_error("Selected wiki citations are no longer valid.")
    citations: list[RevalidatedCitation] = []
    for document_path, text in sorted(selected_documents.items()):
        logical = PurePosixPath(document_path.relative_to(paths.root).as_posix())
        try:
            definitions = parse_citation_definitions(text, path=document_path)
        except ValueError as error:
            raise _citation_error("Selected wiki citations cannot be parsed safely.") from error
        for citation in definitions:
            if ledger.find_representation(
                citation.source_id, citation.content_sha256, citation.derivation_id
            ) is None:
                raise _citation_error("Selected citation no longer resolves to retained evidence.")
            citations.append(
                RevalidatedCitation(
                    logical,
                    citation.citation_id,
                    citation.source_id,
                    citation.content_sha256,
                    citation.derivation_id,
                    citation.anchor,
                )
            )
    citations.sort(
        key=lambda item: (
            item.document_path.as_posix(),
            item.citation_id,
            item.source_id,
            item.content_sha256,
            item.derivation_id,
            item.anchor.kind,
            item.anchor.value,
        )
    )
    if not citations or len({(item.document_path, item.citation_id) for item in citations}) != len(citations):
        raise _citation_error("Selected records do not provide unique revalidated citations.")
    known = {CitationRef(item.document_path, item.citation_id) for item in citations}
    if not set(supporting_citations).issubset(known) or not set(counterevidence_citations).issubset(known):
        raise _citation_error("A requested citation is absent from the selected records.")
    records.sort(key=lambda item: (item.path.as_posix(), item.record_id, item.matched_terms))
    current_revision = compute_corpus_revision(ledger.load_all().values())
    if current_revision != proof_revision:
        raise _search_error("The source corpus changed while wiki evidence was revalidated.")
    try:
        return WikiEvidencePacket(
            question_id,
            current_revision,
            proof_run_id,
            tuple(records),
            tuple(citations),
            supporting_citations,
            counterevidence_citations,
            contradictions,
            (),
            True,
        )
    except ValueError as error:
        raise _citation_error("Wiki evidence fields are not canonical.") from error


def _canonical_matched_paths(
    paths: RepoPaths, matched_paths: tuple[PurePosixPath, ...]
) -> tuple[PurePosixPath, ...]:
    """Authenticate selected direct paths before taking the pinned document snapshot."""

    canonical: list[PurePosixPath] = []
    for requested in matched_paths:
        try:
            logical = canonical_wiki_logical_path(paths, requested, allow_absent=False)
        except (OSError, TypeError, ValueError) as error:
            raise _citation_error("A matched wiki path is not a current direct record.") from error
        canonical.append(logical)
    return tuple(canonical)


def _search_error(message: str) -> WikiEvidenceError:
    return WikiEvidenceError(Diagnostic("wiki_search_incomplete", message))


def _citation_error(message: str) -> WikiEvidenceError:
    return WikiEvidenceError(Diagnostic("wiki_citation_invalid", message))
