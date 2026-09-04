from __future__ import annotations

import os
import hashlib
import io
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping, Callable, TYPE_CHECKING

from brainlib.evidence import SearchPageRecord, SearchPassName, SearchPassRecord
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore, VersionAdoption, adopt_version
from brainlib.contracts import (
    FileFingerprint,
    SourceRecord,
    SourceRepresentation,
    SourceState,
    compute_sha256,
)
from brainlib.inventory import InventoryItem

if TYPE_CHECKING:
    from brainlib.search import SearchResult


SOURCE_ID = "src_" + "a" * 64
DERIVATION_ID = "drv_" + "b" * 64
CONTENT_SHA256 = "c" * 64
FIXED_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def make_completed_pass(
    name: SearchPassName,
    terms: tuple[str, ...],
    revision: str,
    number: int,
    *,
    candidate_count: int = 1,
    match_count: int = 1,
) -> SearchPassRecord:
    digest = f"{number:064x}"
    return SearchPassRecord(
        name=name,
        terms=terms,
        # The low 32 hexadecimal digits carry the test run number.  Taking the
        # leading half of a zero-padded digest would make every small fixture
        # run ID identical and violate the packet's distinct-run contract.
        run_id="srch_" + digest[-32:],
        corpus_revision=revision,
        candidate_count=candidate_count,
        candidate_manifest_sha256=digest,
        pages=(SearchPageRecord(0, match_count, digest),),
        complete=True,
    )


@dataclass(frozen=True)
class KnowledgeScenario:
    root: Path
    paths: RepoPaths
    ledger: LedgerStore


class ScenarioHistoricalResolver:
    def __init__(self, source_id: str, content_sha256: str, content: bytes) -> None:
        self.expected = (source_id, content_sha256)
        self.content = content

    def read_exact(
        self, source_id: str, raw_path: PurePosixPath, sha256: str
    ) -> bytes | None:
        if (
            (source_id, sha256) == self.expected
            and hashlib.sha256(self.content).hexdigest() == sha256
        ):
            return self.content
        return None


def adopt_changed_scenario_source(
    scenario: KnowledgeScenario, prior: bytes, *, approval_note: str
) -> VersionAdoption:
    records = scenario.ledger.load_all()
    integrity_records = [
        record for record in records.values()
        if record.state is SourceState.INTEGRITY_ERROR
    ]
    if len(integrity_records) != 1:
        raise AssertionError(
            "adoption scenario must contain exactly one integrity-error record"
        )
    record = integrity_records[0]
    candidate_path = scenario.paths.raw / record.current_raw_path
    candidate_stat = candidate_path.stat()
    candidate = InventoryItem(
        FileFingerprint(
            record.current_raw_path, candidate_stat.st_size, candidate_stat.st_mtime_ns
        ),
        record.media_type,
        candidate_path.suffix.lower(),
        compute_sha256(candidate_path),
    )
    old_sha256 = record.active_content_sha256
    if old_sha256 is None:
        raise AssertionError("adoption scenario has no active content version")
    adoption = adopt_version(
        record, candidate, paths=scenario.paths,
        resolver=ScenarioHistoricalResolver(record.source_id, old_sha256, prior),
        approval_note=approval_note, now=FIXED_NOW,
    )
    scenario.ledger.save(adoption.record)
    scenario.ledger.write_summary(
        scenario.ledger.load_all().values(), generated_at=FIXED_NOW
    )
    return adoption


def restore_scenario_metadata(
    paths: RepoPaths, records: Mapping[str, SourceRecord]
) -> None:
    for record in records.values():
        for version in record.versions.values():
            target = paths.raw / version.raw_path
            os.utime(
                target,
                ns=(version.fingerprint.mtime_ns, version.fingerprint.mtime_ns),
                follow_symlinks=False,
            )
        for derivation in record.derivations.values():
            target = paths.root / derivation.output_path
            os.utime(
                target,
                ns=(derivation.output_mtime_ns, derivation.output_mtime_ns),
                follow_symlinks=False,
            )


class FakeRgProcess:
    def __init__(
        self,
        stdout: str | bytes,
        *,
        on_wait: Callable[[], None] = lambda: None,
        returncode: int = 0,
    ) -> None:
        self.stdout = io.BytesIO(stdout.encode() if isinstance(stdout, str) else stdout)
        self.returncode = returncode
        self._on_wait = on_wait
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self) -> int:
        self._on_wait()
        return self.returncode


@dataclass(frozen=True)
class FakeRevisionRecord:
    source_id: str
    active_content_sha256: str
    active_derivation_id: str


class FakeLedger:
    def __init__(self, representations: tuple[SourceRepresentation, ...]) -> None:
        self._representations = representations

    def active_representations(self) -> tuple[SourceRepresentation, ...]:
        return self._representations

    def load_all(self) -> Mapping[str, FakeRevisionRecord]:
        return {
            item.source_id: FakeRevisionRecord(
                item.source_id, item.content_sha256, item.derivation_id
            )
            for item in self._representations
        }


def make_source_representation(
    paths: RepoPaths,
    number: int,
    *,
    extracted_path: PurePosixPath | None = None,
    matches: bool = True,
) -> SourceRepresentation:
    digest = f"{number:064x}"
    representation = SourceRepresentation(
        source_id="src_" + digest,
        content_sha256=digest,
        derivation_id="drv_" + digest,
        raw_path=PurePosixPath(f"search/{number}.txt"),
        extracted_path=extracted_path
        or PurePosixPath(
            f"sources/extracted/search/{number}.txt/{digest}/drv_{digest}.md"
        ),
        output_sha256=digest,
        quality_state="ok",
        anchors=(),
    )
    destination = paths.root / representation.extracted_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "Alpha exception\n" if matches else "unrelated\n", encoding="utf-8"
    )
    return representation


def make_active_ledger(
    paths: RepoPaths, count: int, *, matching_numbers: set[int]
) -> FakeLedger:
    return FakeLedger(
        tuple(
            make_source_representation(
                paths, number, matches=number in matching_numbers
            )
            for number in range(count)
        )
    )


def drain_search(
    paths: RepoPaths, ledger: LedgerStore, first: SearchResult, **kwargs
) -> tuple[SearchResult, ...]:
    from brainlib.search import resume_search

    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(resume_search(paths, ledger, pages[-1].next_cursor, **kwargs))
    return tuple(pages)


class ContentAwareRgRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pattern_files: list[Path] = []
        self.overlap = False
        self._running = False

    def __call__(self, argv: list[str], **kwargs: object) -> FakeRgProcess:
        assert kwargs.get("shell") is False
        assert "capture_output" not in kwargs
        assert kwargs["stdout"] == subprocess.PIPE
        assert hasattr(kwargs["stderr"], "write")
        self.overlap |= self._running
        self._running = True
        self.calls.append(argv)
        pattern = Path(argv[argv.index("--file") + 1])
        assert pattern.stat().st_mode & 0o777 == 0o600
        self.pattern_files.append(pattern)
        terms = pattern.read_text(encoding="utf-8").splitlines()
        operands = argv[argv.index("--") + 1 :]
        matching = [
            operand
            for operand in operands
            if any(term in Path(operand).read_text(encoding="utf-8") for term in terms)
        ]
        if "--files-with-matches" in argv:
            stdout = "".join(operand + "\0" for operand in matching)
        else:
            stdout = "".join(
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": operand},
                            "lines": {"text": "Alpha\n"},
                            "line_number": 1,
                            "submatches": [],
                        },
                    }
                )
                + "\n"
                for operand in matching
            )
        return FakeRgProcess(
            stdout,
            on_wait=lambda: setattr(self, "_running", False),
            returncode=0 if matching else 1,
        )


def run_search_cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    from brainlib.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(arguments, cwd=root, stdout=stdout, stderr=stderr)
    return subprocess.CompletedProcess(
        arguments, code, stdout.getvalue(), stderr.getvalue()
    )


_TEST_WIKI_STAGING_RUN = "wstg_" + "0" * 32


def stage_wiki_write(
    scenario: KnowledgeScenario, logical_path: str, markdown: str
):
    """Create one strict-UTF-8 manifest staging entry for transaction tests."""

    from brainlib.wiki_transaction import WikiChange

    logical = PurePosixPath(logical_path)
    staging = (
        scenario.root
        / ".brain/wiki-staging"
        / _TEST_WIKI_STAGING_RUN
        / "files"
        / logical
    )
    staging.parent.mkdir(parents=True, exist_ok=True)
    payload = markdown.encode("utf-8", errors="strict")
    staging.write_bytes(payload)
    return WikiChange.write(
        logical,
        PurePosixPath(staging.relative_to(scenario.root).as_posix()),
        hashlib.sha256(payload).hexdigest(),
    )


def drain_link_candidates(scenario: KnowledgeScenario, first):
    from brainlib.graph import resume_link_candidates

    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(
            resume_link_candidates(scenario.paths, scenario.ledger, pages[-1].next_cursor)
        )
    return tuple(pages)


def link_proof_for_change(scenario: KnowledgeScenario, change):
    """Make a real, fully served retained candidate proof for one target."""

    from brainlib.graph import complete_link_candidate_run, find_link_candidates
    from brainlib.wiki_models import parse_page, parse_question

    target = scenario.root / change.path
    if change.operation == "write":
        assert change.staging_path is not None
        text = (scenario.root / change.staging_path).read_text(encoding="utf-8")
    else:
        text = target.read_text(encoding="utf-8")
    model = (
        parse_page(target, text=text)
        if change.path.parts[1] == "pages"
        else parse_question(target, text=text)
    )
    values = [model.title]
    if hasattr(model, "aliases"):
        values.extend(model.aliases)
    else:
        values.extend((model.canonical_question, *model.prior_phrasings))
    terms = tuple(dict.fromkeys(value for value in values if value.strip()))
    first = find_link_candidates(
        scenario.paths,
        scenario.ledger,
        page_path=target,
        terms=terms,
        page_size=1,
    )
    return complete_link_candidate_run(drain_link_candidates(scenario, first))


def manifest_for(
    scenario: KnowledgeScenario,
    changes: tuple | list | tuple[object, ...],
    *,
    intent: str = "routine",
    approval_event_id: str | None = None,
    citation_rewrites: tuple = (),
):
    """Build a valid manifest with real proofs unless the caller supplies none."""

    from brainlib.contracts import compute_corpus_revision
    from brainlib.wiki_transaction import WikiManifest

    entries = tuple(changes)
    proofs = tuple(
        sorted(
            (link_proof_for_change(scenario, change) for change in entries),
            key=lambda proof: proof.page_path.as_posix(),
        )
    )
    rewrites = tuple(
        sorted(
            citation_rewrites,
            key=lambda item: (
                item.source_id,
                item.content_sha256,
                item.raw_path.as_posix(),
            ),
        )
    )
    return WikiManifest(
        1,
        compute_corpus_revision(scenario.ledger.load_all().values()),
        intent,
        approval_event_id,
        rewrites,
        proofs,
        entries,
    )


def write_wiki_manifest(scenario: KnowledgeScenario, manifest) -> Path:
    run = _TEST_WIKI_STAGING_RUN
    if manifest.changes:
        writes = [change for change in manifest.changes if change.staging_path is not None]
        if writes:
            run = writes[0].staging_path.parts[2]
    path = scenario.root / ".brain/wiki-staging" / run / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.to_json(), encoding="utf-8")
    return path
