#!/usr/bin/env python3
"""Build the deterministic knowledge-workflow scenario fixtures.

Only ``tests/fixtures/wiki/scenarios/**`` and the one external workflow
question are declared outputs. Builders use the same record, ledger, index,
receipt, and evidence codecs exercised by the product; ``--check`` never
touches the committed fixture tree.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
import tempfile

try:
    import fcntl
except ImportError:
    fcntl = None

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from brainlib.contracts import (
    Anchor,
    ContentVersion,
    Derivation,
    FileFingerprint,
    SourceRecord,
    SourceState,
    VersionAdoptionEvent,
    compute_corpus_revision,
    derivation_id,
    source_id_for_first_seen,
)
from brainlib.diagnostics import Diagnostic
from brainlib.evidence import (
    CitationRef,
    EvidenceItem,
    EvidencePacket,
    RevalidatedCitation,
    SearchPageRecord,
    SearchPassRecord,
    WikiEvidencePacket,
    WikiRecordMatch,
)
from brainlib.graph import render_wiki_index
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore
from brainlib.sync_results import SyncResultReference, SyncResultStore, SyncResultWriter


FIXTURE_ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
CONFIG = "c" * 64
TEXT_DERIVATION = "drv_" + "b" * 64
LEDGER_README = "This scenario intentionally has no source ledger records.\n"


class FixtureGenerationError(RuntimeError):
    """A builder or authoritative codec disagreed with a fixture contract."""


def _require_lock_backend() -> None:
    if fcntl is None:
        raise FixtureGenerationError("safe fixture lock backend is unavailable (requires fcntl)")


@dataclass(frozen=True)
class GeneratedEntry:
    relative: PurePosixPath
    kind: str
    mode: int
    data: bytes | str


@dataclass(frozen=True)
class ObservedEntry:
    """A no-follow observation of one declared-output-surface directory entry."""

    relative: PurePosixPath
    kind: str
    mode: int
    data: bytes | str


@dataclass(frozen=True)
class ScenarioSpec:
    """A scenario's executable base builder and its sole optional mutation."""

    name: str
    base: str | None
    mutation: str | None = None


SCENARIO_SPECS = (
    ScenarioSpec("citations/current", None),
    ScenarioSpec("citations/code-example", "citations/current", "citation-markers-inside-code-only"),
    ScenarioSpec("citations/missing-definition", "citations/current", "one-missing-citation-definition"),
    ScenarioSpec("citations/historical", "citations/current", "historical-original-target"),
    ScenarioSpec("citations/adoption", "citations/current", "one-version-adoption-history"),
    ScenarioSpec("citations/encoded-path", "citations/current", "canonical-unicode-evidence-path"),
    ScenarioSpec("search/valid", None),
    ScenarioSpec("graph/valid", None),
    ScenarioSpec("graph/unlinked", "graph/valid", "one-unlinked-first-meaningful-occurrence"),
    ScenarioSpec("graph/nonreciprocal", "graph/valid", "one-nonreciprocal-relationship"),
    ScenarioSpec("graph/encoded", "graph/valid", "canonical-non-ascii-target"),
    ScenarioSpec("graph/candidates", "graph/valid", "candidate-search-corpus-expansion"),
    ScenarioSpec("removal/approved", "graph/valid", "approved-removal-postimage"),
    ScenarioSpec("transaction/interrupted", "graph/valid", "interrupted-transaction-preimage"),
    ScenarioSpec("workflow/answer", None),
)

# Explicitly recorded retired outputs only. Unknown fixture content is never
# cleaned up by --write; it remains visible to --check as unexpected drift.
STALE_GENERATED_PATHS: frozenset[PurePosixPath] = frozenset()

README = """# Knowledge scenario overlays

Each `scenarios/<name>/repo/` directory is a complete, internally consistent
repository overlay. It contains committed content in `sources/raw`,
`sources/extracted`, `sources/ledger`, `sources/ledger.md`, `wiki/pages`,
`wiki/questions`, and `wiki/index.md`; every referenced ledger shard and
summary row exists, with checksums, metadata, paths, and navigable anchor IDs
matching the committed bytes.

Canonical extracted fixtures preserve the complete raw basename:
`sources/extracted/<raw-parent>/<raw-name>/<content-sha256>/<derivation-id>.md`.
For example, a raw `notes/a.txt` is extracted beneath
`sources/extracted/notes/a.txt/<sha>/<derivation>.md`.

Broken scenarios contain exactly the one defect named by the test. Tests use
`scenario_repo` to install exactly one overlay, ensuring incompatible broken
states do not leak into another validation or transaction.
"""


def _paths_for(root: Path) -> RepoPaths:
    root = root.resolve()
    return RepoPaths(root, root / "sources/raw", root / "sources/extracted", root / "sources/ledger", root / "sources/ledger.md", root / "wiki/pages", root / "wiki/questions", root / "config/extractors.toml", root / ".brain/source-write.lock")


class FixtureTree:
    """Mutable temporary tree; no builder reads the committed overlays."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mutation_calls: list[str] = []
        self.rendered_indexes: set[str] = set()

    def path(self, relative: str | PurePosixPath) -> Path:
        value = PurePosixPath(relative)
        if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
            raise FixtureGenerationError(f"unsafe generated path: {value}")
        return self.root.joinpath(*value.parts)

    def write(self, relative: str | PurePosixPath, data: str | bytes, *, mode: int = 0o644) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        os.chmod(target, mode)
        return target

    def repo(self, scenario: str) -> Path:
        target = self.path(PurePosixPath("scenarios") / scenario / "repo")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def remove_repo(self, scenario: str, relative: str | PurePosixPath) -> None:
        target = self.path(PurePosixPath("scenarios") / scenario / "repo" / PurePosixPath(relative))
        if not (target.is_file() or target.is_symlink()):
            raise FixtureGenerationError(f"mutation expected generated file: {target}")
        target.unlink()

    def write_repo(self, scenario: str, relative: str | PurePosixPath, data: str | bytes) -> Path:
        return self.write(PurePosixPath("scenarios") / scenario / "repo" / PurePosixPath(relative), data)

    def render_index(self, scenario: str) -> None:
        repo = self.repo(scenario)
        paths = _paths_for(repo)
        documents = {
            item: item.read_text(encoding="utf-8")
            for directory in (paths.wiki_pages, paths.wiki_questions)
            if directory.exists()
            for item in sorted(directory.glob("*.md"))
        }
        rendered = render_wiki_index(paths, documents)
        self.write_repo(scenario, "wiki/index.md", rendered)
        self.rendered_indexes.add(scenario)
        if (repo / "wiki/index.md").read_text(encoding="utf-8") != rendered:
            raise FixtureGenerationError(f"index serialization drift for {scenario}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_hash(data: bytes, expected: str, label: str) -> None:
    if _sha(data) != expected:
        raise FixtureGenerationError(f"{label} has an unexpected SHA-256")


def _output_path(raw_path: PurePosixPath, checksum: str, derivation_id: str) -> PurePosixPath:
    return PurePosixPath("sources", "extracted", *raw_path.parts, checksum, f"{derivation_id}.md")


def _write_record(
    tree: FixtureTree, scenario: str, *, identity_path: PurePosixPath,
    identity_bytes: bytes, current_path: PurePosixPath, live_bytes: bytes,
    versions: tuple[tuple[PurePosixPath, bytes], ...], state: SourceState,
    active_sha: str | None,
    derivations: tuple[tuple[str, str, PurePosixPath, bytes, Anchor, str, str], ...],
    active_derivation: str | None, diagnostics: tuple[Diagnostic, ...] = (),
    adoptions: tuple[VersionAdoptionEvent, ...] = (), record_size: int | None = None,
    expected_source_id: str | None = None,
) -> SourceRecord:
    """Create raw/version/extracted bytes and serialize one SourceRecord."""
    repo, paths = tree.repo(scenario), _paths_for(tree.repo(scenario))
    paths.ledger_dir.mkdir(parents=True, exist_ok=True)
    tree.write_repo(scenario, PurePosixPath("sources/raw") / current_path, live_bytes)
    record_versions: dict[str, ContentVersion] = {}
    for logical, payload in versions:
        checksum = _sha(payload)
        record_versions[checksum] = ContentVersion(checksum, logical, len(payload), FileFingerprint(logical, len(payload), 1), NOW, ())
        if logical != current_path:
            tree.write_repo(scenario, PurePosixPath("sources/raw") / logical, payload)
    source_id = source_id_for_first_seen(identity_path, _sha(identity_bytes))
    if expected_source_id is not None and source_id != expected_source_id:
        raise FixtureGenerationError(f"source ID derivation changed for {current_path}")
    built_derivations: dict[str, Derivation] = {}
    for identifier, source_sha, raw_path, output, anchor, extractor_id, extractor_version in derivations:
        output_path = _output_path(raw_path, source_sha, identifier)
        tree.write_repo(scenario, output_path, output)
        config_sha = CONFIG if extractor_id == "builtin.text" else "9dc46f66262448f1fdbe0110cc638a25b4e9014ec6f257b14a20f298c4ea34ea"
        if extractor_id == "text" and identifier != derivation_id(
            source_sha256=source_sha,
            extractor_id=extractor_id,
            extractor_version=extractor_version,
            config_sha256=config_sha,
        ):
            raise FixtureGenerationError(f"derivation ID derivation changed for {raw_path}")
        built_derivations[identifier] = Derivation(
            identifier, source_sha, extractor_id, extractor_version,
            config_sha,
            output_path, _sha(output), len(output), 1, "ok", (anchor,), NOW,
            method="deterministic", method_metadata={"converter_id": "builtin.text", "converter_version": "builtin:builtin.text:1"},
        )
    record = SourceRecord(1, source_id, current_path, (), "text/plain", len(live_bytes) if record_size is None else record_size, state, record_versions, active_sha, built_derivations, active_derivation, None, diagnostics, NOW, NOW, NOW, adoptions)
    shard = LedgerStore(paths).save(record)
    # The legacy citation encoded-path overlay deliberately preserves direct
    # UTF-8 JSON. Other records retain LedgerStore.save's canonical encoding.
    if current_path == PurePosixPath("notes/Résumé (100% #1).txt"):
        canonical = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if shard.read_bytes() != canonical:
            shard.write_bytes(canonical)
    for version in record.versions.values():
        target = paths.raw / version.raw_path
        if target.exists():
            os.utime(target, ns=(1, 1))
    for derivation in record.derivations.values():
        os.utime(repo / derivation.output_path, ns=(1, 1))
    return record


def _finish_ledger(tree: FixtureTree, scenario: str, records: Iterable[SourceRecord]) -> tuple[SourceRecord, ...]:
    result = tuple(records)
    LedgerStore(_paths_for(tree.repo(scenario))).write_summary(result, generated_at=NOW)
    return result


def _page(page_id: str, title: str, description: str, summary: str, details: str, related_pages: str = "", related_questions: str = "", aliases: str = "[]") -> str:
    pages_break = "\n\n" if related_pages else "\n"
    questions_break = "\n\n" if related_questions else "\n"
    return f"""---
id: {page_id}
title: {title}
description: {description}
type: concept
aliases: {aliases}
created: 2026-09-04
updated: 2026-09-04
---
# {title}

## Summary
{summary}

## Details
{details}

## Related pages
{related_pages}{pages_break}## Related questions
{related_questions}{questions_break}## Sources
"""


def _question(question_id: str, title: str, description: str, current: str, support: str, contradiction: str, related: str, *, canonical: str | None = None, prior: str = "[]", revision: str = "a" * 64, discovery: str = "[Alpha]", expansion: str = "[Alpha topic]", verification: str = "[Alpha check]") -> str:
    related_break = "\n\n" if related else "\n"
    return f"""---
schema_version: 2
id: {question_id}
title: {title}
description: {description}
canonical_question: {canonical or title}
prior_phrasings: {prior}
answer_status: answered
interpretation_decision: not_applicable
corpus_revision: {revision}
last_researched: 2026-09-04
discovery_terms: {discovery}
expansion_terms: {expansion}
verification_terms: {verification}
---
# {title}

## Current answer
{current}

## Supporting evidence
{support}

## Contradictory evidence
{contradiction}

## Related pages
{related}{related_break}## Sources
"""


ALPHA_RAW = b"Alpha is supported.\n"
ALPHA_SHA = "fe2d43470d64244d579014da2e87daab2fb3ff8b1abf69766d5d9fcff2447b4a"
ALPHA_EXTRACTED = b'<a id="page:2"></a>\nAlpha is supported.\n'
ALPHA_SOURCE_ID = "src_4e31593e866bff93b2f886960e05fc213d357cd0117f2a94489036d1d22cb9df"


def _citation_record(tree: FixtureTree, scenario: str, raw_path: PurePosixPath = PurePosixPath("notes/a.txt")) -> SourceRecord:
    _assert_hash(ALPHA_RAW, ALPHA_SHA, "citation raw fixture")
    tree.write_repo(scenario, "wiki/questions/.gitkeep", b"")
    return _write_record(tree, scenario, identity_path=raw_path, identity_bytes=ALPHA_RAW, current_path=raw_path, live_bytes=ALPHA_RAW, versions=((raw_path, ALPHA_RAW),), state=SourceState.OK, active_sha=ALPHA_SHA, derivations=((TEXT_DERIVATION, ALPHA_SHA, raw_path, ALPHA_EXTRACTED, Anchor("page", "2"), "builtin.text", "1"),), active_derivation=TEXT_DERIVATION)


def _citation_definition(record: SourceRecord, raw_path: PurePosixPath, *, original: str | None = None, checksum: str = ALPHA_SHA, derivation: str = TEXT_DERIVATION) -> str:
    original_target = original or f"../../sources/raw/{raw_path.as_posix()}"
    extracted = _output_path(raw_path, checksum, derivation).as_posix().removeprefix("sources/")
    return f"source_id: `{record.source_id}`; content_sha256: `{checksum}`; derivation_id: `{derivation}`; anchor: `page:2`; [original]({original_target}); [extracted](../../sources/{extracted}#page:2)"


def _empty_index(tree: FixtureTree, scenario: str) -> None:
    tree.write_repo(scenario, "wiki/index.md", "# Second Brain Lite\n")


def _build_citations_current(tree: FixtureTree, scenario: str = "citations/current") -> None:
    record = _citation_record(tree, scenario)
    _finish_ledger(tree, scenario, (record,))
    tree.write_repo(scenario, "wiki/pages/current.md", f"""---
id: current
title: Current
description: Current citation evidence supports Alpha.
type: note
aliases: []
created: 2026-09-04
updated: 2026-09-04
---
# Current

## Summary
Alpha is supported.[^cite-alpha-page-2]

## Evidence
This page retains a directly cited Alpha claim.

## Related pages

## Related questions

## Sources

[^cite-alpha-page-2]: {_citation_definition(record, PurePosixPath('notes/a.txt'))}
""")
    tree.render_index(scenario)


def _mutate_code_example(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/pages/current.md")
    _empty_index(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/code-example.md", """# Code example

```text
A factual claim.[^missing]
[^bogus]: not a citation
```

## Sources
""")


def _mutate_missing_definition(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/pages/current.md")
    _empty_index(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/broken.md", """# Missing definition

A factual claim.[^missing]

## Sources
""")


def _mutate_historical(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/pages/current.md")
    current = b"Alpha has changed.\n"
    old_path = PurePosixPath("_versions", ALPHA_SOURCE_ID, ALPHA_SHA, "a.txt")
    record = _write_record(tree, scenario, identity_path=PurePosixPath("notes/a.txt"), identity_bytes=ALPHA_RAW, current_path=PurePosixPath("notes/a.txt"), live_bytes=current, versions=((PurePosixPath("notes/a.txt"), current), (old_path, ALPHA_RAW)), state=SourceState.PENDING, active_sha=_sha(current), derivations=((TEXT_DERIVATION, ALPHA_SHA, PurePosixPath("notes/a.txt"), ALPHA_EXTRACTED, Anchor("page", "2"), "builtin.text", "1"),), active_derivation=None, adoptions=(VersionAdoptionEvent(ALPHA_SHA, _sha(current), "Approved replacement", NOW),), expected_source_id=ALPHA_SOURCE_ID)
    _finish_ledger(tree, scenario, (record,))
    _empty_index(tree, scenario)
    definition = _citation_definition(record, PurePosixPath("notes/a.txt"), original=f"../../sources/raw/{old_path.as_posix()}")
    tree.write_repo(scenario, "wiki/pages/historical.md", f"""# Citation example

Alpha is supported.[^cite-alpha-page-2]

## Sources

[^cite-alpha-page-2]: {definition}
""")


def _mutate_adoption(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/pages/current.md")
    tree.remove_repo(scenario, f"sources/ledger/{ALPHA_SOURCE_ID}.json")
    prior = b"Alpha earlier detail.\n"
    prior_sha = "41680cc2584beb210d6a6309eaa9344129f02e88785c653524925cd1784f3d21"
    prior_output = b'<a id="page:2"></a>\nAlpha earlier detail.\n'
    source_id = source_id_for_first_seen(PurePosixPath("notes/a.txt"), prior_sha)
    prior_path = PurePosixPath("_versions", source_id, prior_sha, "a.txt")
    record = _write_record(tree, scenario, identity_path=PurePosixPath("notes/a.txt"), identity_bytes=prior, current_path=PurePosixPath("notes/a.txt"), live_bytes=b"Alpha has changed.\n", versions=((prior_path, prior), (PurePosixPath("notes/a.txt"), ALPHA_RAW)), state=SourceState.INTEGRITY_ERROR, active_sha=ALPHA_SHA, derivations=((TEXT_DERIVATION, ALPHA_SHA, PurePosixPath("notes/a.txt"), ALPHA_EXTRACTED, Anchor("page", "2"), "builtin.text", "1"), ("drv_" + "e" * 64, prior_sha, PurePosixPath("notes/a.txt"), prior_output, Anchor("page", "2"), "builtin.text", "1")), active_derivation=TEXT_DERIVATION, diagnostics=(Diagnostic("raw_checksum_mismatch", "Replacement detected"),), adoptions=(VersionAdoptionEvent(prior_sha, ALPHA_SHA, "Approved prior replacement", NOW),), record_size=len(ALPHA_RAW), expected_source_id="src_19a0797cd1279e786ef242e747d37c761f733fdc1969bcbb41756c492a025af1")
    tree.write(PurePosixPath("scenarios") / scenario / "prior-original.txt", ALPHA_RAW)
    _finish_ledger(tree, scenario, (record,))
    _empty_index(tree, scenario)
    current_definition = _citation_definition(record, PurePosixPath("notes/a.txt"))
    prior_definition = _citation_definition(record, PurePosixPath("notes/a.txt"), original=f"../../sources/raw/{prior_path.as_posix()}", checksum=prior_sha, derivation="drv_" + "e" * 64)
    tree.write_repo(scenario, "wiki/pages/adoption.md", f"""# Citation adoption

Alpha is supported.[^cite-alpha-page-2] Earlier detail.[^same-source-other-version]

A prose mention of sources/raw/notes/a.txt stays unchanged.
```text
[original](../../sources/raw/notes/a.txt)
```

## Sources

[^cite-alpha-page-2]: {current_definition}
[^same-source-other-version]: {prior_definition}
""")


def _mutate_encoded_citation(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/pages/current.md")
    tree.remove_repo(scenario, f"sources/ledger/{ALPHA_SOURCE_ID}.json")
    tree.remove_repo(scenario, "sources/raw/notes/a.txt")
    tree.remove_repo(scenario, f"sources/extracted/notes/a.txt/{ALPHA_SHA}/{TEXT_DERIVATION}.md")
    raw_path = PurePosixPath("notes/Résumé (100% #1).txt")
    record = _citation_record(tree, scenario, raw_path)
    _finish_ledger(tree, scenario, (record,))
    _empty_index(tree, scenario)
    encoded = "notes/R%C3%A9sum%C3%A9%20%28100%25%20%231%29.txt"
    definition = _citation_definition(record, raw_path, original=f"../../sources/raw/{encoded}").replace(f"../../sources/extracted/{raw_path.as_posix()}", f"../../sources/extracted/{encoded}")
    tree.write_repo(scenario, "wiki/pages/encoded.md", f"""# Citation example

Alpha is supported.[^cite-alpha-page-2]

## Sources

[^cite-alpha-page-2]: {definition}
""")


def _write_empty_ledger(tree: FixtureTree, scenario: str) -> None:
    tree.write_repo(scenario, "sources/ledger/README", LEDGER_README)


def _build_graph_valid(tree: FixtureTree, scenario: str = "graph/valid") -> None:
    _write_empty_ledger(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented topic.", "Alpha is a small valid topic.", "This page has a stable topic section.", related_questions="- [What is Alpha?](../questions/what-is-alpha.md): The durable question for Alpha.", aliases="[A]"))
    tree.write_repo(scenario, "wiki/questions/what-is-alpha.md", _question("question-what-is-alpha", "What is Alpha?", "What is Alpha is a documented question.", "[Alpha](../pages/alpha.md) is a small valid topic.", "None.", "None.", "- [Alpha](../pages/alpha.md): The durable page for Alpha."))
    tree.render_index(scenario)


def _build_graph_pair(tree: FixtureTree, scenario: str, *, alpha_summary: str, alpha_details: str, alpha_related: str, beta_details: str, beta_related: str) -> None:
    _write_empty_ledger(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented topic.", alpha_summary, alpha_details, alpha_related))
    tree.write_repo(scenario, "wiki/pages/beta.md", _page("beta", "Beta", "Beta is a documented topic.", "Beta is a small topic.", beta_details, beta_related))
    tree.render_index(scenario)


def _mutate_graph_unlinked(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/questions/what-is-alpha.md")
    _build_graph_pair(tree, scenario, alpha_summary="Alpha is a small topic.", alpha_details="Beta appears before its later relationship link.", alpha_related="- [Beta](beta.md): A reciprocal related topic.", beta_details="[Alpha](alpha.md) is the reciprocal linked topic.", beta_related="- [Alpha](alpha.md): A reciprocal related topic.")


def _mutate_graph_nonreciprocal(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/questions/what-is-alpha.md")
    _build_graph_pair(tree, scenario, alpha_summary="Alpha is a small topic.", alpha_details="[Beta](beta.md) is related to this page.", alpha_related="- [Beta](beta.md): A related topic without the required reverse edge.", beta_details="This record deliberately lacks the reverse edge.", beta_related="")


def _mutate_graph_encoded(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/questions/what-is-alpha.md")
    _write_empty_ledger(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented topic.", "Alpha is a small topic.", "See [Béta topic](b%C3%A9ta%20topic.md#Details)."))
    tree.write_repo(scenario, "wiki/pages/béta topic.md", _page("beta-topic", "Béta topic", "Béta topic is a documented topic.", "Béta topic is a small topic.", "This target section has a canonical fragment name."))
    tree.render_index(scenario)


def _mutate_graph_candidates(tree: FixtureTree, scenario: str) -> None:
    _write_empty_ledger(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented topic.", "Alpha is the excluded candidate record.", "This record proves that self is not returned.", aliases="[A. Example]"))
    tree.write_repo(scenario, "wiki/pages/beta.md", _page("beta", "Beta", "Beta is a documented topic.", "Alpha appears in this candidate page.", "This page has stable details."))
    tree.write_repo(scenario, "wiki/pages/zzz-related.md", _page("zzz-related", "Zzz related", "Zzz related is a documented topic.", "A. Example appears in this late candidate page.", "This page has stable details."))
    tree.write_repo(scenario, "wiki/questions/what-is-alpha.md", _question("question-what-is-alpha", "What is Alpha?", "What is Alpha is a documented question.", "Alpha is a candidate question record.", "None.", "None.", "", prior="[Alpha question]"))
    tree.render_index(scenario)


def _mutate_removal(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/questions/what-is-alpha.md")
    _write_empty_ledger(tree, scenario)
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented topic.", "[Beta](beta.md) and [Zzz inbound](zzz-inbound.md) both retain inbound relationships.", "This is the record selected for approved removal.", "- [Beta](beta.md): Beta is related to Alpha.\n- [Zzz inbound](zzz-inbound.md): Zzz inbound is related to Alpha."))
    tree.write_repo(scenario, "wiki/pages/beta.md", _page("beta", "Beta", "Beta is a documented topic.", "[Alpha](alpha.md) is the current related topic.", "This inbound record must be reconciled with the removal.", "- [Alpha](alpha.md): Alpha is related to Beta."))
    tree.write_repo(scenario, "wiki/pages/zzz-inbound.md", _page("zzz-inbound", "Zzz inbound", "Zzz inbound is a documented topic.", "[Alpha](alpha.md) is the current related topic.", "This late-alphabet inbound record must also be reconciled.", "- [Alpha](alpha.md): Alpha is related to Zzz inbound."))
    tree.render_index(scenario)
    tree.write(PurePosixPath("scenarios") / scenario / "beta-after-removal.md", _page("beta", "Beta", "Beta is a documented topic.", "Beta remains a durable topic after the approved removal.", "The inbound relationship has been reconciled."))
    tree.write(PurePosixPath("scenarios") / scenario / "zzz-inbound-after-removal.md", _page("zzz-inbound", "Zzz inbound", "Zzz inbound is a documented topic.", "Zzz inbound remains a durable topic after the approved removal.", "The late inbound relationship has been reconciled."))


def _mutate_transaction(tree: FixtureTree, scenario: str) -> None:
    tree.remove_repo(scenario, "wiki/questions/what-is-alpha.md")
    _write_empty_ledger(tree, scenario)
    for page_id, title, order in (("alpha", "Alpha", "first"), ("beta", "Beta", "second")):
        tree.write_repo(scenario, f"wiki/pages/{page_id}.md", _page(page_id, title, f"{title} is a documented topic.", f"{title} is the {order} record in the interrupted transaction fixture.", "This content must return exactly after recovery."))
    tree.render_index(scenario)


def _build_search(tree: FixtureTree, scenario: str = "search/valid") -> None:
    sources = ((PurePosixPath("early.txt"), b"Alpha source early.txt\n", b"# early.txt\n\nAlpha retained evidence.\n"), (PurePosixPath("unusual/résumé (final).md"), "Alpha source unusual/résumé (final).md\n".encode(), "# unusual/résumé (final).md\n\nAlpha retained evidence.\n".encode()), (PurePosixPath("zzz-late.txt"), b"Alpha source zzz-late.txt\n", b"# zzz-late.txt\n\nAlpha retained evidence.\n"))
    records = []
    for raw_path, raw, extracted in sources:
        checksum = _sha(raw)
        records.append(_write_record(tree, scenario, identity_path=raw_path, identity_bytes=raw, current_path=raw_path, live_bytes=raw, versions=((raw_path, raw),), state=SourceState.OK, active_sha=checksum, derivations=((TEXT_DERIVATION, checksum, raw_path, extracted, Anchor("line", "3"), "builtin.text", "1"),), active_derivation=TEXT_DERIVATION))
    _finish_ledger(tree, scenario, records)
    tree.write_repo(scenario, "wiki/pages/.gitkeep", b"\n")
    tree.write_repo(scenario, "wiki/questions/.gitkeep", b"\n")
    tree.write_repo(scenario, "wiki/index.md", "# Search fixture\n")


WORKFLOW_ALPHA_RAW = b"Alpha supports the relationship.\nBeta relationship is documented.\n"
WORKFLOW_EXCEPTION_RAW = b"Alpha exception qualifies the relationship.\n"
WORKFLOW_UNAVAILABLE_RAW = b"This retained source remains unavailable to the workflow.\n"
WORKFLOW_ALPHA_ID = "src_0b07c1a90cd7fda7dacb804718961b3d27739f3d725e0925cb555e92a7835772"
WORKFLOW_EXCEPTION_ID = "src_1062015e8f1e7bb34b7f8fda272868598099cc77edd27edfce9b2243146cd4de"
WORKFLOW_UNAVAILABLE_ID = "src_27fe870ea05b0e56ea6a71fb7e88e33b57010f4014aaca9b6162fe24ff5c744b"
WORKFLOW_REVISION = "826135e493e0f4bdc571dcc4eadd1dc37dc4b3b52110c73339d236508b0d56af"


def _workflow_question(revision: str) -> str:
    alpha_path = _output_path(PurePosixPath("alpha.txt"), _sha(WORKFLOW_ALPHA_RAW), "drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de")
    exception_path = _output_path(PurePosixPath("exception.txt"), _sha(WORKFLOW_EXCEPTION_RAW), "drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec")
    return _question("question-alpha", "What is Alpha?", "Alpha is a documented relationship topic.", "[Alpha](../pages/alpha.md) supports the relationship, with a documented exception.[^cite-alpha-line-4][^cite-alpha-exception-line-4]", "[Alpha](../pages/alpha.md) supports the relationship.[^cite-alpha-line-4]", "[Alpha](../pages/alpha.md) has a documented exception.[^cite-alpha-exception-line-4]", "- [Alpha](../pages/alpha.md): The durable topic record for Alpha.", canonical="How does Alpha relate to Beta?", prior="[What is Alpha?]", revision=revision, expansion="[Beta relationship]", verification="[Alpha exception]") + f"""
[^cite-alpha-line-4]: source_id: `{WORKFLOW_ALPHA_ID}`; content_sha256: `{_sha(WORKFLOW_ALPHA_RAW)}`; derivation_id: `drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de`; anchor: `line:4`; [original](../../sources/raw/alpha.txt); [extracted](../../{alpha_path.as_posix()}#line:4)
[^cite-alpha-exception-line-4]: source_id: `{WORKFLOW_EXCEPTION_ID}`; content_sha256: `{_sha(WORKFLOW_EXCEPTION_RAW)}`; derivation_id: `drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec`; anchor: `line:4`; [original](../../sources/raw/exception.txt); [extracted](../../{exception_path.as_posix()}#line:4)
"""


def _workflow_packet(revision: str) -> EvidencePacket:
    alpha_sha, exception_sha = _sha(WORKFLOW_ALPHA_RAW), _sha(WORKFLOW_EXCEPTION_RAW)
    return EvidencePacket("question-alpha", revision, (
        SearchPassRecord("discovery", ("Alpha",), "srch_983169a42b12cb783eb2e184a9431e05", revision, 2, "822fb2d02ea5dd15edcb9cd21bf8fc3c82822e6472c39d98ba5469974aac5bf3", (SearchPageRecord(0, 1, "14828c94bcd622d94237d2ea7ec8dd82a32814475782f67293fd245bf5afe706"), SearchPageRecord(1, 1, "c95d991ac2d0b76f196f0c7635cbb8c64a95ce3bb669be2f860f302261d03d81")), True),
        SearchPassRecord("expansion", ("Beta relationship",), "srch_15330f167620ea62fa3490af6d93c17e", revision, 1, "ff3b789ff609a39dcc4d4fa24b32bb517de901f90e6e1ca77815daafd2b4366c", (SearchPageRecord(0, 1, "cc1b543587fde4aca68effd21bab8311948184f50b45db30e86c91a43f547bab"),), True),
        SearchPassRecord("verification", ("Alpha exception",), "srch_17af1281749387cf0df614f53707477c", revision, 1, "e1a78bc56d3aa1dcd32b7c7d4d57bd849acd3f195470cd6fc461cea065c97eac", (SearchPageRecord(0, 1, "c95d991ac2d0b76f196f0c7635cbb8c64a95ce3bb669be2f860f302261d03d81"),), True),
    ), (EvidenceItem(WORKFLOW_ALPHA_ID, alpha_sha, "drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de", Anchor("line", "4"), "Alpha supports the relationship."),), (EvidenceItem(WORKFLOW_EXCEPTION_ID, exception_sha, "drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec", Anchor("line", "4"), "Alpha exception qualifies the relationship."),), (Diagnostic("source_failed", "src-unavailable is failed"),), (WORKFLOW_ALPHA_ID,))


def _workflow_wiki_packet(revision: str) -> WikiEvidencePacket:
    logical, alpha_sha, exception_sha = PurePosixPath("wiki/questions/what-is-alpha.md"), _sha(WORKFLOW_ALPHA_RAW), _sha(WORKFLOW_EXCEPTION_RAW)
    return WikiEvidencePacket("question-alpha", revision, "srch_97c75b518dbafac470a66774c953e5fc", (WikiRecordMatch(logical, "question-alpha", ("Alpha",)),), (RevalidatedCitation(logical, "cite-alpha-exception-line-4", WORKFLOW_EXCEPTION_ID, exception_sha, "drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec", Anchor("line", "4")), RevalidatedCitation(logical, "cite-alpha-line-4", WORKFLOW_ALPHA_ID, alpha_sha, "drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de", Anchor("line", "4"))), (CitationRef(logical, "cite-alpha-line-4"),), (CitationRef(logical, "cite-alpha-exception-line-4"),), (Diagnostic("alpha_exception", "Alpha has a documented exception."),), (), True)


def _write_workflow_receipt(tree: FixtureTree, scenario: str, revision: str, alpha: SourceRecord) -> SyncResultReference:
    paths = _paths_for(tree.repo(scenario))
    writer: SyncResultWriter = SyncResultStore(paths).writer("sync", NOW)
    for raw_path in ("alpha.txt", "exception.txt", "unavailable.txt"):
        writer.emit("hashed_path", {"path": raw_path})
    derivation = alpha.derivations[alpha.active_derivation_id or ""]
    writer.emit("new_active_representation", {"source_id": alpha.source_id, "content_sha256": alpha.active_content_sha256, "derivation_id": derivation.derivation_id, "raw_path": alpha.current_raw_path.as_posix(), "extracted_path": derivation.output_path.as_posix(), "output_sha256": derivation.output_sha256, "quality_state": derivation.quality_state, "anchors": [{"kind": item.kind, "value": item.value} for item in derivation.anchors]})
    writer.emit("coverage_gap", {"code": "source_failed", "message": "src-unavailable is failed", "path": None, "details": {}})
    reference = writer.finalize(revision)
    if reference.result_id != "sync_fcdc3f71961f03ac1de80bc4c7408f91195c067be42e6d34d7c1c77e24e65f70":
        raise FixtureGenerationError("workflow receipt event construction changed")
    return reference


def _build_workflow(tree: FixtureTree, scenario: str = "workflow/answer") -> None:
    alpha_sha, exception_sha, unavailable_sha = _sha(WORKFLOW_ALPHA_RAW), _sha(WORKFLOW_EXCEPTION_RAW), _sha(WORKFLOW_UNAVAILABLE_RAW)
    alpha = _write_record(tree, scenario, identity_path=PurePosixPath("alpha.txt"), identity_bytes=WORKFLOW_ALPHA_RAW, current_path=PurePosixPath("alpha.txt"), live_bytes=WORKFLOW_ALPHA_RAW, versions=((PurePosixPath("alpha.txt"), WORKFLOW_ALPHA_RAW),), state=SourceState.OK, active_sha=alpha_sha, derivations=(("drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de", alpha_sha, PurePosixPath("alpha.txt"), b'<a id="line:4"></a>\n\n\nAlpha supports the relationship.\nBeta relationship is documented.\n', Anchor("line", "4"), "text", "1+1e640bc25086fea6912b90b5141234c1f73eb18cbcf072f9cde9a934f91c9eb2"),), active_derivation="drv_d1a0d7e4ad37bcf76dddf9cc73b9b3950fddae30198d111dec027dfac18009de", expected_source_id=WORKFLOW_ALPHA_ID)
    exception = _write_record(tree, scenario, identity_path=PurePosixPath("exception.txt"), identity_bytes=WORKFLOW_EXCEPTION_RAW, current_path=PurePosixPath("exception.txt"), live_bytes=WORKFLOW_EXCEPTION_RAW, versions=((PurePosixPath("exception.txt"), WORKFLOW_EXCEPTION_RAW),), state=SourceState.OK, active_sha=exception_sha, derivations=(("drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec", exception_sha, PurePosixPath("exception.txt"), b'<a id="line:4"></a>\n\n\nAlpha exception qualifies the relationship.\n', Anchor("line", "4"), "text", "1+1e640bc25086fea6912b90b5141234c1f73eb18cbcf072f9cde9a934f91c9eb2"),), active_derivation="drv_26d45709c2e1c5a96c284e269590597f7608771946187ad74508a7f0476447ec", expected_source_id=WORKFLOW_EXCEPTION_ID)
    unavailable = _write_record(tree, scenario, identity_path=PurePosixPath("unavailable.txt"), identity_bytes=WORKFLOW_UNAVAILABLE_RAW, current_path=PurePosixPath("unavailable.txt"), live_bytes=WORKFLOW_UNAVAILABLE_RAW, versions=((PurePosixPath("unavailable.txt"), WORKFLOW_UNAVAILABLE_RAW),), state=SourceState.FAILED, active_sha=unavailable_sha, derivations=(), active_derivation=None, diagnostics=(Diagnostic("source_failed", "src-unavailable is failed"),), expected_source_id=WORKFLOW_UNAVAILABLE_ID)
    records = _finish_ledger(tree, scenario, (alpha, exception, unavailable))
    revision = compute_corpus_revision(records)
    if revision != WORKFLOW_REVISION:
        raise FixtureGenerationError("workflow corpus revision derivation changed")
    if LedgerStore(_paths_for(tree.repo(scenario))).load_all() != {item.source_id: item for item in records}:
        raise FixtureGenerationError("workflow ledger did not round-trip")
    tree.write_repo(scenario, "wiki/pages/alpha.md", _page("alpha", "Alpha", "Alpha is a documented relationship topic.", "Alpha is a documented relationship topic.", "Alpha has both supporting evidence and a documented exception.", related_questions="- [What is Alpha?](../questions/what-is-alpha.md): The durable answer record for Alpha.", aliases="[A]"))
    question = _workflow_question(revision)
    tree.write_repo(scenario, "wiki/questions/what-is-alpha.md", question)
    tree.render_index(scenario)
    reference = _write_workflow_receipt(tree, scenario, revision, alpha)
    packet, wiki_packet = _workflow_packet(revision), _workflow_wiki_packet(revision)
    tree.write(PurePosixPath("scenarios") / scenario / "expected-evidence-packet.json", packet.to_json())
    tree.write(PurePosixPath("scenarios") / scenario / "expected-wiki-evidence-packet.json", wiki_packet.to_json())
    tree.write(PurePosixPath("scenarios") / scenario / "question-input.md", "How does Alpha relate to Beta?\n")
    events = ["sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete", "freshness_start", "freshness_complete", "source_discovery_start", "source_discovery_complete", "source_expansion_start", "source_expansion_complete", "source_verification_start", "source_verification_complete", "curator", "link_candidates_start", "link_candidates_complete", "wiki_apply", "validate", "answer"]
    fast_events = ["sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete", "freshness_start", "freshness_complete", "wiki_evidence_packet", "curator_update_question", "link_candidates_start", "link_candidates_complete", "wiki_apply", "validate", "answer"]
    tree.write(PurePosixPath("scenarios") / scenario / "workflow-events.json", json.dumps(events, separators=(",", ":")) + "\n")
    tree.write(PurePosixPath("scenarios") / scenario / "wiki-fast-path-events.json", json.dumps(fast_events, separators=(",", ":")) + "\n")
    derivation = next(iter(alpha.derivations.values()))
    data = {"status": "complete_with_gaps", "corpus_revision": revision, "decision_counts": {"await_web_approval": 0, "create": 0, "mark_integrity_error": 0, "mark_missing": 0, "process": 0, "queue_needs_agent": 0, "rename": 0, "retain": 3, "update_descriptor": 0}, "sampled_decisions": [{"action": "retain", "item": None, "reason": "alpha.txt: retain", "source_id": WORKFLOW_ALPHA_ID}, {"action": "retain", "item": None, "reason": "exception.txt: retain", "source_id": WORKFLOW_EXCEPTION_ID}, {"action": "retain", "item": None, "reason": "unavailable.txt: retain", "source_id": WORKFLOW_UNAVAILABLE_ID}], "hashed_paths": ["alpha.txt", "exception.txt", "unavailable.txt"], "hashed_path_count": 3, "new_active_representations": [{"source_id": alpha.source_id, "content_sha256": alpha_sha, "derivation_id": alpha.active_derivation_id, "raw_path": "alpha.txt", "extracted_path": derivation.output_path.as_posix(), "output_sha256": derivation.output_sha256, "quality_state": "ok", "anchors": [{"kind": "line", "value": "4"}]}], "new_active_representation_count": 1, "citation_rewrites": [], "citation_rewrite_count": 0, "handoff_source_ids": [], "handoff_source_id_count": 0, "handoff_manifest": None, "handoffs": [], "coverage_gaps": [{"code": "source_failed", "message": "src-unavailable is failed", "path": None, "details": {}}], "coverage_gap_count": 1, "sample_limits": {"max_bytes_per_field": 32768, "max_items_per_field": 100}, "result_manifest": reference.to_dict()}
    envelope = {"command": "sync", "ok": False, "data": data, "warnings": [], "errors": [{"code": "source_coverage_gaps", "message": "Synchronization completed with unresolved source coverage gaps.", "path": None, "details": {}}]}
    tree.write(PurePosixPath("scenarios") / scenario / "sync-command-result.json", json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    tree.write("workflow/what-is-alpha.md", question)


BASE_BUILDERS: dict[str, Callable[[FixtureTree, str], None]] = {"citations/current": _build_citations_current, "search/valid": _build_search, "graph/valid": _build_graph_valid, "workflow/answer": _build_workflow}
MUTATIONS: dict[str, Callable[[FixtureTree, str], None]] = {"citation-markers-inside-code-only": _mutate_code_example, "one-missing-citation-definition": _mutate_missing_definition, "historical-original-target": _mutate_historical, "one-version-adoption-history": _mutate_adoption, "canonical-unicode-evidence-path": _mutate_encoded_citation, "one-unlinked-first-meaningful-occurrence": _mutate_graph_unlinked, "one-nonreciprocal-relationship": _mutate_graph_nonreciprocal, "canonical-non-ascii-target": _mutate_graph_encoded, "candidate-search-corpus-expansion": _mutate_graph_candidates, "approved-removal-postimage": _mutate_removal, "interrupted-transaction-preimage": _mutate_transaction}

MUTATION_ALLOWED_DELTAS: dict[str, frozenset[PurePosixPath]] = {
    "citation-markers-inside-code-only": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/current.md", "repo/wiki/pages/code-example.md"})),
    "one-missing-citation-definition": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/current.md", "repo/wiki/pages/broken.md"})),
    "historical-original-target": frozenset(map(PurePosixPath, {"repo/sources/raw/notes/a.txt", f"repo/sources/raw/_versions/{ALPHA_SOURCE_ID}/{ALPHA_SHA}/a.txt", f"repo/sources/ledger/{ALPHA_SOURCE_ID}.json", "repo/sources/ledger.md", "repo/wiki/index.md", "repo/wiki/pages/current.md", "repo/wiki/pages/historical.md"})),
    "one-version-adoption-history": frozenset(map(PurePosixPath, {"prior-original.txt", "repo/sources/raw/notes/a.txt", "repo/sources/raw/_versions/src_19a0797cd1279e786ef242e747d37c761f733fdc1969bcbb41756c492a025af1/41680cc2584beb210d6a6309eaa9344129f02e88785c653524925cd1784f3d21/a.txt", f"repo/sources/extracted/notes/a.txt/41680cc2584beb210d6a6309eaa9344129f02e88785c653524925cd1784f3d21/{'drv_' + 'e' * 64}.md", f"repo/sources/ledger/{ALPHA_SOURCE_ID}.json", "repo/sources/ledger/src_19a0797cd1279e786ef242e747d37c761f733fdc1969bcbb41756c492a025af1.json", "repo/sources/ledger.md", "repo/wiki/index.md", "repo/wiki/pages/current.md", "repo/wiki/pages/adoption.md"})),
    "canonical-unicode-evidence-path": frozenset(map(PurePosixPath, {"repo/sources/raw/notes/a.txt", "repo/sources/raw/notes/Résumé (100% #1).txt", f"repo/sources/extracted/notes/a.txt/{ALPHA_SHA}/{TEXT_DERIVATION}.md", f"repo/sources/extracted/notes/Résumé (100% #1).txt/{ALPHA_SHA}/{TEXT_DERIVATION}.md", f"repo/sources/ledger/{ALPHA_SOURCE_ID}.json", "repo/sources/ledger/src_787b84c57a553260197dd6b18cc37ef996d3427b8a24d56c46fcf78cb1d50fad.json", "repo/sources/ledger.md", "repo/wiki/index.md", "repo/wiki/pages/current.md", "repo/wiki/pages/encoded.md"})),
    "one-unlinked-first-meaningful-occurrence": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/beta.md", "repo/wiki/questions/what-is-alpha.md"})),
    "one-nonreciprocal-relationship": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/beta.md", "repo/wiki/questions/what-is-alpha.md"})),
    "canonical-non-ascii-target": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/béta topic.md", "repo/wiki/questions/what-is-alpha.md"})),
    "candidate-search-corpus-expansion": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/beta.md", "repo/wiki/pages/zzz-related.md", "repo/wiki/questions/what-is-alpha.md"})),
    "approved-removal-postimage": frozenset(map(PurePosixPath, {"beta-after-removal.md", "zzz-inbound-after-removal.md", "repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/beta.md", "repo/wiki/pages/zzz-inbound.md", "repo/wiki/questions/what-is-alpha.md"})),
    "interrupted-transaction-preimage": frozenset(map(PurePosixPath, {"repo/wiki/index.md", "repo/wiki/pages/alpha.md", "repo/wiki/pages/beta.md", "repo/wiki/questions/what-is-alpha.md"})),
}


def _scenario_entries(tree: FixtureTree, scenario: str) -> tuple[PurePosixPath, ...]:
    root = tree.path(PurePosixPath("scenarios") / scenario)
    return () if not root.exists() else tuple(PurePosixPath(path.relative_to(tree.root).as_posix()) for path in sorted(root.rglob("*")) if path.is_file() or path.is_symlink())


def _scenario_snapshot(tree: FixtureTree, scenario: str) -> dict[PurePosixPath, bytes | str]:
    root = tree.path(PurePosixPath("scenarios") / scenario)
    result: dict[PurePosixPath, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[PurePosixPath(path.relative_to(root).as_posix())] = path.read_bytes()
        elif path.is_symlink():
            result[PurePosixPath(path.relative_to(root).as_posix())] = os.readlink(path)
    return result


def _build_spec(tree: FixtureTree, spec: ScenarioSpec) -> None:
    base_name = spec.name if spec.base is None else spec.base
    try:
        BASE_BUILDERS[base_name](tree, spec.name)
    except KeyError as error:
        raise FixtureGenerationError(f"no executable base builder for {base_name}") from error
    if spec.mutation is None:
        return
    before = _scenario_snapshot(tree, spec.name)
    try:
        MUTATIONS[spec.mutation](tree, spec.name)
    except KeyError as error:
        raise FixtureGenerationError(f"no executable mutation {spec.mutation}") from error
    after = _scenario_snapshot(tree, spec.name)
    delta = frozenset(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    if not delta:
        raise FixtureGenerationError(f"mutation {spec.mutation} made no delta")
    if delta != MUTATION_ALLOWED_DELTAS[spec.mutation]:
        raise FixtureGenerationError(f"mutation {spec.mutation} escaped its declared delta")
    tree.mutation_calls.append(spec.mutation)


def _all_entries(root: Path) -> tuple[GeneratedEntry, ...]:
    output: list[GeneratedEntry] = []
    for prefix in (PurePosixPath("scenarios"), PurePosixPath("workflow/what-is-alpha.md")):
        target = root.joinpath(*prefix.parts)
        candidates = [target] if target.is_file() or target.is_symlink() else sorted(target.rglob("*"))
        for item in candidates:
            metadata = item.lstat()
            if stat.S_ISREG(metadata.st_mode):
                output.append(GeneratedEntry(PurePosixPath(item.relative_to(root).as_posix()), "file", stat.S_IMODE(metadata.st_mode), item.read_bytes()))
            elif stat.S_ISLNK(metadata.st_mode):
                output.append(GeneratedEntry(PurePosixPath(item.relative_to(root).as_posix()), "symlink", stat.S_IMODE(metadata.st_mode), os.readlink(item)))
            elif item != target and not stat.S_ISDIR(metadata.st_mode):
                raise FixtureGenerationError(f"unsupported generated entry: {item}")
    output.sort(key=lambda item: item.relative.as_posix())
    if len({item.relative for item in output}) != len(output):
        raise FixtureGenerationError("duplicate generated path")
    return tuple(output)


def _audit_ledger(repo: Path) -> None:
    paths, records = _paths_for(repo), []
    for shard in sorted(paths.ledger_dir.glob("src_*.json")):
        record = SourceRecord.from_dict(json.loads(shard.read_text(encoding="utf-8")))
        canonical = {
            json.dumps(record.to_dict(), ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for ensure_ascii in (False, True)
        }
        if shard.read_bytes() not in canonical:
            raise FixtureGenerationError(f"SourceRecord.to_dict drift: {shard}")
        for checksum, version in record.versions.items():
            candidates = (paths.raw / version.raw_path, repo.parent / "prior-original.txt")
            if not any(path.is_file() and _sha(path.read_bytes()) == checksum for path in candidates):
                raise FixtureGenerationError(f"missing retained version bytes for {record.source_id}")
        for derivation in record.derivations.values():
            output = repo / derivation.output_path
            if not output.is_file() or _sha(output.read_bytes()) != derivation.output_sha256:
                raise FixtureGenerationError(f"derived output drift: {derivation.output_path}")
        records.append(record)
    expected = paths.ledger_summary.read_text(encoding="utf-8")
    if LedgerStore(paths).write_summary(records, generated_at=NOW) != expected:
        raise FixtureGenerationError(f"LedgerStore.write_summary drift: {repo}")


def _audit_workflow(repo: Path, generated_root: Path) -> None:
    envelope = json.loads((repo.parent / "sync-command-result.json").read_text(encoding="utf-8"))
    if envelope["ok"] is not False or envelope["data"]["status"] != "complete_with_gaps":
        raise FixtureGenerationError("workflow gap semantics changed")
    reference = SyncResultReference.from_dict(envelope["data"]["result_manifest"])
    store = SyncResultStore(_paths_for(repo))
    store.verify(reference)
    tuple(store.iter_events(reference))
    for relative, codec in (("expected-evidence-packet.json", EvidencePacket), ("expected-wiki-evidence-packet.json", WikiEvidencePacket)):
        payload = (repo.parent / relative).read_text(encoding="utf-8")
        if codec.from_json(payload).to_json() != payload:
            raise FixtureGenerationError(f"noncanonical workflow artifact: {relative}")
    if (generated_root / "workflow/what-is-alpha.md").read_bytes() != (repo / "wiki/questions/what-is-alpha.md").read_bytes():
        raise FixtureGenerationError("external workflow question diverged")


def _audit(tree: FixtureTree) -> None:
    ledger_scenarios = {"citations/current", "citations/code-example", "citations/missing-definition", "citations/historical", "citations/adoption", "citations/encoded-path", "search/valid", "workflow/answer"}
    rendered_scenarios = {"citations/current", "graph/valid", "graph/unlinked", "graph/nonreciprocal", "graph/encoded", "graph/candidates", "removal/approved", "transaction/interrupted", "workflow/answer"}
    citation_variants = {spec.name for spec in SCENARIO_SPECS if spec.name.startswith("citations/")}
    if not citation_variants.issubset(tree.rendered_indexes):
        raise FixtureGenerationError("every citation variant must render its strict base index")
    for spec in SCENARIO_SPECS:
        repo = tree.repo(spec.name)
        if spec.name in ledger_scenarios:
            _audit_ledger(repo)
        if spec.name in rendered_scenarios:
            paths = _paths_for(repo)
            documents = {path: path.read_text(encoding="utf-8") for directory in (paths.wiki_pages, paths.wiki_questions) if directory.exists() for path in sorted(directory.glob("*.md"))}
            if render_wiki_index(paths, documents) != (repo / "wiki/index.md").read_text(encoding="utf-8"):
                raise FixtureGenerationError(f"render_wiki_index drift: {spec.name}")
    _audit_workflow(tree.repo("workflow/answer"), tree.root)


def _materialize(root: Path) -> tuple[GeneratedEntry, ...]:
    tree = FixtureTree(root)
    tree.write("scenarios/README.md", README)
    for spec in SCENARIO_SPECS:
        _build_spec(tree, spec)
    if tree.mutation_calls != [spec.mutation for spec in SCENARIO_SPECS if spec.mutation is not None]:
        raise FixtureGenerationError("ScenarioSpec mutation execution is incomplete")
    _audit(tree)
    return _all_entries(root)


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise FixtureGenerationError("no-follow descriptor operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    required = ("O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required):
        raise FixtureGenerationError("nonblocking no-follow descriptor operations are unavailable")
    # An observed regular file may become a FIFO before open; fstat must stay reachable.
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _validated_relative(relative: PurePosixPath) -> tuple[str, ...]:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise FixtureGenerationError(f"unsafe generated path: {relative}")
    return relative.parts


def _validated_output(relative: PurePosixPath) -> tuple[str, ...]:
    parts = _validated_relative(relative)
    if not (parts[0] == "scenarios" and len(parts) > 1) and parts != ("workflow", "what-is-alpha.md"):
        raise FixtureGenerationError(f"output escapes ownership: {relative}")
    return parts


@dataclass(frozen=True)
class GenerationPlan:
    entries: tuple[GeneratedEntry, ...]
    stale: tuple[PurePosixPath, ...]

    @classmethod
    def build(cls, entries: Iterable[GeneratedEntry]) -> GenerationPlan:
        plan = cls(tuple(entries), tuple(sorted(STALE_GENERATED_PATHS)))
        seen: set[PurePosixPath] = set()
        for entry in plan.entries:
            _validated_output(entry.relative)
            if entry.relative in seen:
                raise FixtureGenerationError(f"duplicate generated output: {entry.relative}")
            seen.add(entry.relative)
            if not (
                entry.kind == "file" and isinstance(entry.data, bytes)
                or entry.kind == "symlink" and isinstance(entry.data, str)
            ):
                raise FixtureGenerationError(f"unsupported generated entry: {entry.relative}")
        for relative in plan.stale:
            _validated_output(relative)
            if relative in seen:
                raise FixtureGenerationError(f"generated output is also stale: {relative}")
        return plan


@dataclass(frozen=True)
class _DirectoryEdge:
    parent_fd: int
    child_fd: int
    name: str
    parent_identity: os.stat_result
    child_identity: os.stat_result

    def validate(self) -> None:
        try:
            parent = os.fstat(self.parent_fd)
            child = os.fstat(self.child_fd)
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError as error:
            raise FixtureGenerationError(f"output directory changed: {self.name}") from error
        if not (
            stat.S_ISDIR(named.st_mode)
            and os.path.samestat(parent, self.parent_identity)
            and os.path.samestat(child, self.child_identity)
            and os.path.samestat(named, self.child_identity)
        ):
            raise FixtureGenerationError(f"output directory changed: {self.name}")


def _close_descriptors(descriptors: list[int]) -> None:
    pending = tuple(reversed(descriptors))
    descriptors.clear()
    first_error = None
    for descriptor in pending:
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


class _Ownership:
    """One pinned root and advisory directory lock for a complete operation.

    Cooperating generators use a shared check / exclusive write lock. Retained
    named edges detect observed moves, including replacement of the root. POSIX
    cannot atomically validate ancestry and rename into a descendant: an
    uncooperative same-UID process can still detach it after the final validation
    and before renameat/unlinkat/mkdirat. Post-mutation validation reports such
    changes; this is not an absolute containment guarantee against that actor.
    """

    def __init__(self, *, write: bool) -> None:
        self.write = write
        self.descriptors: list[int] = []
        self.edges: list[_DirectoryEdge] = []

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def __enter__(self) -> _Ownership:
        _require_lock_backend()
        root = FIXTURE_ROOT
        if not root.is_absolute() or ".." in root.parts:
            raise FixtureGenerationError(f"fixture root must be canonical and absolute: {root}")
        try:
            self.descriptors.append(os.open(root.anchor, _directory_flags()))
            for index, name in enumerate(root.parts[1:], 1):
                parent = self.descriptor
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                child = _open_directory_at(parent, name, PurePosixPath(*root.parts[:index + 1]))
                self.descriptors.append(child)
                self.edges.append(_DirectoryEdge(parent, child, name, os.fstat(parent), named))
                self.validate()
            fcntl.flock(self.descriptor, fcntl.LOCK_EX if self.write else fcntl.LOCK_SH)
            self.validate()
            return self
        except BaseException:
            self.close()
            raise

    def validate(self) -> None:
        for edge in self.edges:
            edge.validate()

    def close(self) -> None:
        self.edges.clear()
        _close_descriptors(self.descriptors)

    def __exit__(self, *_args: object) -> None:
        self.close()


class _OutputParent:
    """Descendant handles retain every named edge until observation/mutation ends."""

    def __init__(self, ownership: _Ownership, parent: _OutputParent | None = None) -> None:
        self.ownership = ownership
        self.base_fd = ownership.descriptor if parent is None else parent.descriptor
        self.edges = [] if parent is None else list(parent.edges)
        self.descriptors: list[int] = []

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1] if self.descriptors else self.base_fd

    def validate(self) -> None:
        self.ownership.validate()
        for edge in self.edges:
            edge.validate()

    def open_child(self, name: str, relative: PurePosixPath, *, create: bool = False) -> bool:
        self.validate()
        parent = self.descriptor
        named = _entry_metadata(parent, name)
        if named is None:
            if not create:
                self.validate()
                return False
            self.validate()
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                pass
            self.validate()
            named = _entry_metadata(parent, name)
        if named is None or not stat.S_ISDIR(named.st_mode):
            raise FixtureGenerationError(f"unsafe output ancestor: {relative}")
        descriptor = _open_directory_at(parent, name, relative)
        self.descriptors.append(descriptor)
        self.edges.append(_DirectoryEdge(parent, descriptor, name, os.fstat(parent), named))
        self.validate()
        return True

    def close(self) -> None:
        self.edges.clear()
        _close_descriptors(self.descriptors)

    def __enter__(self) -> _OutputParent:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _open_directory_at(parent_fd: int, name: str, relative: PurePosixPath) -> int:
    try:
        return os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise FixtureGenerationError(f"unsafe output ancestor: {relative.as_posix()}") from error


def _open_output_parent(ownership: _Ownership, relative: PurePosixPath, *, create: bool) -> _OutputParent | None:
    parts = _validated_output(relative)
    parent = _OutputParent(ownership)
    try:
        prefix: tuple[str, ...] = ()
        for name in parts[:-1]:
            prefix += (name,)
            if not parent.open_child(name, PurePosixPath(*prefix), create=create):
                parent.close()
                return None
        return parent
    except BaseException:
        parent.close()
        raise


def _entry_metadata(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _preflight_declared_path(ownership: _Ownership, relative: PurePosixPath) -> None:
    parent = _open_output_parent(ownership, relative, create=False)
    if parent is None:
        return
    with parent:
        metadata = _entry_metadata(parent.descriptor, _validated_output(relative)[-1])
        if metadata is not None and not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise FixtureGenerationError(f"refusing unsafe declared output: {relative.as_posix()}")
        parent.validate()


def _read_regular_at(parent_fd: int, name: str, relative: PurePosixPath) -> ObservedEntry:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise FixtureGenerationError(f"unsafe observed output: {relative.as_posix()}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FixtureGenerationError(f"unsupported observed output: {relative.as_posix()}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return ObservedEntry(relative, "file", stat.S_IMODE(metadata.st_mode), b"".join(chunks))
    finally:
        os.close(descriptor)


def _observe_symlink_at(parent_fd: int, name: str, relative: PurePosixPath, mode: int) -> ObservedEntry:
    try:
        target = os.readlink(name, dir_fd=parent_fd)
    except OSError:
        return ObservedEntry(relative, "other", mode, b"")
    return ObservedEntry(relative, "symlink", mode, target)


def _walk_owned_directory(parent: _OutputParent, relative: PurePosixPath, values: list[ObservedEntry]) -> None:
    parent.validate()
    parent_fd = parent.descriptor
    for name in sorted(os.listdir(parent_fd)):
        parent.validate()
        logical = relative / name
        metadata = _entry_metadata(parent_fd, name)
        if metadata is None:
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            with _OutputParent(parent.ownership, parent) as child:
                try:
                    present = child.open_child(name, logical)
                except FixtureGenerationError:
                    values.append(ObservedEntry(logical, "other", mode, b""))
                    continue
                if not present:
                    values.append(ObservedEntry(logical, "other", mode, b""))
                    continue
                values.append(ObservedEntry(logical, "directory", mode, b""))
                _walk_owned_directory(child, logical, values)
                child.validate()
        elif stat.S_ISREG(metadata.st_mode):
            try:
                observation = _read_regular_at(parent_fd, name, logical)
            except FixtureGenerationError:
                values.append(ObservedEntry(logical, "other", mode, b""))
            else:
                values.append(observation)
        elif stat.S_ISLNK(metadata.st_mode):
            values.append(_observe_symlink_at(parent_fd, name, logical, mode))
        else:
            values.append(ObservedEntry(logical, "other", mode, b""))
        parent.validate()
    parent.validate()


def _owned_targets(ownership: _Ownership | None = None) -> tuple[ObservedEntry, ...]:
    if ownership is None:
        with _Ownership(write=False) as pinned:
            return _owned_targets(pinned)
    values: list[ObservedEntry] = []
    root = ownership.descriptor
    ownership.validate()
    with _OutputParent(ownership) as scenarios_parent:
        scenarios = _entry_metadata(root, "scenarios")
        if scenarios is not None:
            mode = stat.S_IMODE(scenarios.st_mode)
            if stat.S_ISDIR(scenarios.st_mode):
                try:
                    present = scenarios_parent.open_child("scenarios", PurePosixPath("scenarios"))
                except FixtureGenerationError:
                    values.append(ObservedEntry(PurePosixPath("scenarios"), "other", mode, b""))
                else:
                    if present:
                        values.append(ObservedEntry(PurePosixPath("scenarios"), "directory", mode, b""))
                        _walk_owned_directory(scenarios_parent, PurePosixPath("scenarios"), values)
                        scenarios_parent.validate()
                    else:
                        values.append(ObservedEntry(PurePosixPath("scenarios"), "other", mode, b""))
            elif stat.S_ISLNK(scenarios.st_mode):
                values.append(_observe_symlink_at(root, "scenarios", PurePosixPath("scenarios"), mode))
            else:
                values.append(ObservedEntry(PurePosixPath("scenarios"), "other", mode, b""))

    external = PurePosixPath("workflow/what-is-alpha.md")
    try:
        parent = _open_output_parent(ownership, external, create=False)
    except FixtureGenerationError:
        values.append(ObservedEntry(external, "other", 0, b""))
    else:
        if parent is not None:
            with parent:
                metadata = _entry_metadata(parent.descriptor, external.name)
                if metadata is not None:
                    mode = stat.S_IMODE(metadata.st_mode)
                    if stat.S_ISREG(metadata.st_mode):
                        try:
                            observation = _read_regular_at(parent.descriptor, external.name, external)
                        except FixtureGenerationError:
                            values.append(ObservedEntry(external, "other", mode, b""))
                        else:
                            values.append(observation)
                    elif stat.S_ISLNK(metadata.st_mode):
                        values.append(_observe_symlink_at(parent.descriptor, external.name, external, mode))
                    else:
                        values.append(ObservedEntry(external, "other", mode, b""))
                parent.validate()
    ownership.validate()
    return tuple(values)


def _compare(entries: Iterable[GeneratedEntry]) -> tuple[str, ...]:
    plan = GenerationPlan.build(entries)
    with _Ownership(write=False) as ownership:
        differences = _compare_owned(plan, ownership)
        ownership.validate()
        return differences


def _compare_owned(plan: GenerationPlan, ownership: _Ownership) -> tuple[str, ...]:
    expected = {entry.relative: entry for entry in plan.entries}
    expected_directories = {parent for path in expected for parent in path.parents}
    observed = {entry.relative: entry for entry in _owned_targets(ownership)}
    differences = [f"missing {path.as_posix()}" for path in sorted(set(expected) - set(observed), key=lambda item: item.as_posix())]
    unexpected = (
        path for path in set(observed) - set(expected)
        if observed[path].kind != "other"
        and not (observed[path].kind == "directory" and path in expected_directories)
    )
    differences.extend(f"unexpected {path.as_posix()}" for path in sorted(unexpected, key=lambda item: item.as_posix()))
    differences.extend(f"unsupported {path.as_posix()}" for path in sorted((path for path, entry in observed.items() if entry.kind == "other"), key=lambda item: item.as_posix()))
    for relative in sorted(set(expected) & set(observed), key=lambda item: item.as_posix()):
        desired, current = expected[relative], observed[relative]
        if current.kind == "other":
            continue
        if current.kind != desired.kind:
            differences.append(f"type {relative.as_posix()}")
            continue
        if (current.mode & 0o111) != (desired.mode & 0o111):
            differences.append(f"executable-bit {relative.as_posix()}")
        if current.data != desired.data:
            differences.append(f"{'symlink' if current.kind == 'symlink' else 'content'} {relative.as_posix()}")
    return tuple(differences)


def _write_bytes(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _temporary_name(leaf: str) -> str:
    return f".{leaf}.generate-scenarios.{secrets.token_hex(16)}.tmp"


def _validate_scratch(ownership: _Ownership, name: str, identity: os.stat_result) -> None:
    ownership.validate()
    current = _entry_metadata(ownership.descriptor, name)
    if current is None or not os.path.samestat(current, identity):
        raise FixtureGenerationError(f"output scratch changed: {name}")


def _discard_scratch(ownership: _Ownership, name: str, identity: os.stat_result) -> None:
    ownership.validate()
    if _entry_metadata(ownership.descriptor, name) is None:
        return
    _validate_scratch(ownership, name, identity)
    ownership.validate()
    os.unlink(name, dir_fd=ownership.descriptor)
    ownership.validate()


def _stage_entry(ownership: _Ownership, entry: GeneratedEntry) -> tuple[str, os.stat_result]:
    # Flat scratch belongs to the pinned root, never to a movable descendant.
    for _ in range(32):
        temporary = _temporary_name("output")
        descriptor = None
        identity = None
        ownership.validate()
        try:
            try:
                if entry.kind == "file":
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=ownership.descriptor,
                    )
                    identity = os.fstat(descriptor)
                else:
                    assert isinstance(entry.data, str)
                    os.symlink(entry.data, temporary, dir_fd=ownership.descriptor)
                    identity = os.stat(temporary, dir_fd=ownership.descriptor, follow_symlinks=False)
            except FileExistsError:
                continue
            ownership.validate()
            if descriptor is not None:
                assert isinstance(entry.data, bytes)
                _write_bytes(descriptor, entry.data)
                ownership.validate()
                os.fchmod(descriptor, entry.mode)
                ownership.validate()
            _validate_scratch(ownership, temporary, identity)
            return temporary, identity
        except BaseException:
            # Without a captured identity, leave the root scratch entry untouched
            # rather than unlinking a name that could now belong to somebody else.
            if identity is not None:
                _discard_scratch(ownership, temporary, identity)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
    raise FixtureGenerationError(f"could not reserve exclusive output temporary for {entry.relative}")


def _write_entry(ownership: _Ownership, entry: GeneratedEntry) -> None:
    parts = _validated_output(entry.relative)
    parent = _open_output_parent(ownership, entry.relative, create=True)
    assert parent is not None
    with parent:
        temporary, identity = _stage_entry(ownership, entry)
        try:
            _validate_scratch(ownership, temporary, identity)
            metadata = _entry_metadata(parent.descriptor, parts[-1])
            if metadata is not None and not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                raise FixtureGenerationError(f"refusing unsafe declared output: {entry.relative.as_posix()}")
            parent.validate()
            os.replace(temporary, parts[-1], src_dir_fd=ownership.descriptor, dst_dir_fd=parent.descriptor)
            parent.validate()
        finally:
            _discard_scratch(ownership, temporary, identity)


def _remove_stale_entry(ownership: _Ownership, relative: PurePosixPath) -> None:
    parent = _open_output_parent(ownership, relative, create=False)
    if parent is None:
        return
    with parent:
        metadata = _entry_metadata(parent.descriptor, _validated_output(relative)[-1])
        if metadata is None:
            parent.validate()
            return
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise FixtureGenerationError(f"refusing unsafe stale output: {relative.as_posix()}")
        parent.validate()
        os.unlink(_validated_output(relative)[-1], dir_fd=parent.descriptor)
        parent.validate()


def _write(entries: Iterable[GeneratedEntry]) -> None:
    plan = GenerationPlan.build(entries)
    with _Ownership(write=True) as ownership:
        for entry in plan.entries:
            _preflight_declared_path(ownership, entry.relative)
        for relative in plan.stale:
            _preflight_declared_path(ownership, relative)
        for entry in plan.entries:
            _write_entry(ownership, entry)
        for relative in plan.stale:
            _remove_stale_entry(ownership, relative)
        ownership.validate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="replace declared generated outputs")
    mode.add_argument("--check", action="store_true", help="compare every declared output")
    arguments = parser.parse_args(argv)
    _require_lock_backend()
    with tempfile.TemporaryDirectory(prefix="brain-scenario-generator-") as temporary:
        entries = _materialize(Path(temporary) / "wiki")
        if arguments.write:
            _write(entries)
            return 0
        differences = _compare(entries)
    if differences:
        print("scenario fixtures differ:")
        for difference in differences[:40]:
            print(f"  {difference}")
        if len(differences) > 40:
            print(f"  ... {len(differences) - 40} more")
        return 1
    print("scenario fixtures are reproducible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
