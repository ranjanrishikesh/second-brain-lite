"""Generate the complete deterministic Task 7a evaluation corpus.

The checked-in JSON contracts and fixture overlays are products of this module.
``--check`` renders into a temporary tree and compares bytes; it never writes
the worktree.  Fixture mtimes are encoded in records as ``FIXED_MTIME_NS``;
the later isolated runner must restore them after copying an overlay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from brainlib.contracts import (
    ContentVersion,
    Derivation,
    FileFingerprint,
    SourceRecord,
    SourceState,
    derivation_id,
    source_id_for_first_seen,
)
from brainlib.extractors.native import text_payload
from brainlib.graph import render_wiki_index
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore, derive_extraction_path
from brainlib.registry import ExtractorRegistry, effective_extractor_version, prerequisite_digest
from brainlib.validation import ChecksumCache, validate_source_ledger, validate_wiki
from tests.evals.scenario_contract import ScenarioContractError, validate_against_schema


ROOT = Path(__file__).resolve().parents[2]
EVAL_ROOT = Path(__file__).resolve().parent
FIXTURES = EVAL_ROOT / "fixtures"
SCENARIO_DIR = EVAL_ROOT / "scenarios"
FIXED_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
FIXED_MTIME_NS = 1_725_451_200_000_000_000
REQUIRED_IDS = frozenset({
    "empty-wiki-first-question", "current-wiki-fast-path", "new-binary-before-question",
    "web-approval-and-capture", "contradictory-evidence", "repository-development-not-archived",
})


def _receipt_events(operation: str, *, handoff: bool = False) -> list[str]:
    result = [f"{operation}_receipt_verified", f"{operation}_receipt_consumed", f"{operation}_receipt_durable"]
    if handoff:
        result.append(f"{operation}_handoff_delivery_verified")
    return [*result, f"{operation}_receipt_acknowledged"]


def _interpretation_policy(fixture_repo: Path) -> dict[str, object]:
    """Derive the closed policy from the immutable fixture's active ledger."""
    try:
        records = [SourceRecord.from_dict(json.loads(path.read_bytes()))
                   for path in sorted((fixture_repo / "sources/ledger").glob("src_*.json"))]
        triples = sorted((record.source_id, record.active_content_sha256, record.active_derivation_id)
                         for record in records)
        if len(triples) != 2 or len(set(triples)) != 2 or any(None in triple for triple in triples):
            raise ValueError("two complete distinct active identities required")
        for record in records:
            if record.derivations[record.active_derivation_id].source_sha256 != record.active_content_sha256:
                raise ValueError("active derivation/content mismatch")
        return {"question_path": "wiki/questions/what-is-alpha.md", "question_id": "question-what-is-alpha",
                "preserve_both": True, "expected_decision": "unresolved", "expected_approval_decision": "withheld",
                "claim_identities": [dict(zip(("source_id", "content_sha256", "derivation_id"), triple)) for triple in triples]}
    except (ValueError, KeyError, TypeError) as error:
        raise ScenarioContractError("interpretation policy fixture ledger") from error


def validate_interpretation_policy(scenario: dict, *, fixture_repo: Path | None = None) -> None:
    """The schema owns shape; the generated ledger owns order and identity."""
    if scenario["id"] != "contradictory-evidence":
        if "interpretation_policy" in scenario:
            raise ScenarioContractError("interpretation policy is only valid for contradictory-evidence")
        return
    schema = json.loads((EVAL_ROOT / "scenario.v1.schema.json").read_bytes())
    policy = scenario.get("interpretation_policy")
    validate_against_schema(policy, schema["properties"]["interpretation_policy"])
    expected = _interpretation_policy(fixture_repo or FIXTURES / "contradictory-evidence/repo")
    if policy != expected:
        raise ScenarioContractError("interpretation policy does not match exact sorted fixture identities")


def interpretation_policy_sha256(scenario: dict) -> str:
    validate_interpretation_policy(scenario)
    return hashlib.sha256(json.dumps(scenario["interpretation_policy"], ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "schema_version": 1, "id": "empty-wiki-first-question",
        "title": "An empty wiki requires complete local research before its first answer",
        "skill": "brain-answer", "prompt": "What does the Alpha source say about the Beta relationship?",
        "fixture_id": "empty-wiki-first-question", "network_mode": "disabled", "approval_script": [],
        "fixture_setup": ["One active Alpha/Beta text representation is ledgered.", "Wiki pages and questions contain only approved sentinels.", "The initial corpus revision is deterministic."],
        "receipt_operations": ["initial_sync"],
        "required_events": [*_receipt_events("initial_sync"), "wiki_reconcile_citations", "wiki_search_drained", "wiki_no_supported_evidence", "judge_insufficient", "source_pass_discovery", "source_pass_discovery_drained", "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification", "source_pass_verification_drained", "write_wiki_question", "link_candidates_drained", "wiki_apply", "links_check", "validate"],
        "forbidden_events": ["build_wiki_evidence_packet", "answer_without_citation", "skip_source_pass", "create_empty_stub"],
        "repository_assertions": ["Exactly one evolving Alpha question record exists.", "Every factual answer passage has an exact resolving source-version-derivation citation.", "Every qualifying Alpha or Beta page relationship is reciprocal."],
    },
    {
        "schema_version": 1, "id": "current-wiki-fast-path",
        "title": "A current complete wiki answer uses the validated fast path",
        "skill": "brain-answer", "prompt": "Restate the documented Alpha decision for a risk review, preserving this new phrasing in the same topic record.",
        "fixture_id": "current-wiki-fast-path", "network_mode": "disabled", "approval_script": [],
        "fixture_setup": ["The Alpha page and question cover the prompt.", "All citations resolve at the current revision.", "The current question lacks the requested risk-review phrasing."],
        "receipt_operations": ["initial_sync"],
        "required_events": [*_receipt_events("initial_sync"), "wiki_reconcile_citations", "wiki_search_drained", "revalidate_underlying_citations", "build_wiki_evidence_packet", "judge_sufficient", "stage_existing_question_update", "link_candidates_drained", "wiki_apply", "links_check", "validate"],
        "forbidden_events": ["source_pass_discovery", "source_pass_expansion", "source_pass_verification", "public_web_access", "duplicate_question_record", "direct_live_wiki_write"],
        "repository_assertions": ["The existing question ID remains unique and its same file records the new risk-review phrasing.", "The Q&A update is published by one wiki transaction while source and ledger content stay unchanged.", "Every underlying citation is revalidated against the current ledger before WikiEvidencePacket sufficiency."],
    },
    {
        "schema_version": 1, "id": "new-binary-before-question",
        "title": "A newly added binary is extracted before wiki sufficiency is judged",
        "skill": "brain-answer", "prompt": "Does the new quarterly PDF change the Alpha conclusion?",
        "fixture_id": "new-binary-before-question", "network_mode": "disabled", "approval_script": [],
        "fixture_setup": ["A valid quarterly PDF is unledgered under sources/raw.", "An older cited Alpha conclusion is present.", "Fixture services resolve no PDF converter and use its unavailable prerequisite digest."],
        "receipt_operations": ["initial_sync", "post_registration_sync"],
        "required_events": [*_receipt_events("initial_sync", handoff=True), "branch_extraction_handoff", "stage_handoff_scoped_extraction", "register_extraction_handoff", "verify_registration_active_representation", *_receipt_events("post_registration_sync"), "wiki_reconcile_citations", "freshness_search_batched", "freshness_search_drained", "wiki_search_drained", "revalidate_underlying_citations", "judge_insufficient", "source_pass_discovery", "source_pass_discovery_drained", "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification", "source_pass_verification_drained", "write_wiki_question", "link_candidates_drained", "wiki_apply", "links_check", "validate"],
        "forbidden_events": ["answer_before_ingestion", "hide_coverage_gap", "rehash_unchanged_corpus", "caller_controls_agent_revision", "rendered_handoff_registered_as_extraction", "snapshot_active_representation_for_registered_extraction"],
        "repository_assertions": ["The new PDF has a ledgered content version and Markdown derivation before sufficiency judgment.", "Registration activation is proved by data.registration.active_representation and its corpus revision.", "The freshness probe is scoped to the newly active source ID."],
    },
    {
        "schema_version": 1, "id": "web-approval-and-capture",
        "title": "Insufficient local evidence requires approval and durable capture",
        "skill": "brain-web-research", "prompt": "What changed in the external standard this year?",
        "fixture_id": "web-approval-and-capture", "network_mode": "mock_only",
        "approval_script": ["Approve only the mock standard capture after the agent states the local evidence gap and bounded scope."],
        "fixture_setup": ["The ledger and wiki contain only the prior standard.", "Local three-pass research has no current evidence.", "Static, faithful rendered, and unused web candidates are fixture assets outside repo/."],
        "receipt_operations": ["initial_sync", "initial_snapshot", "rendered_snapshot"],
        "required_events": [*_receipt_events("initial_sync"), "report_local_evidence_gap", "ask_web_approval", "public_web_access", "select_used_source", "snapshot_used_source", *_receipt_events("initial_snapshot", handoff=True), "branch_rendered_web_capture_handoff", "stage_faithful_browser_capture", "rendered_snapshot_url", *_receipt_events("rendered_snapshot"), "verify_snapshot_active_representation", "source_pass_discovery", "source_pass_discovery_drained", "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification", "source_pass_verification_drained", "link_candidates_drained", "persist_claim"],
        "forbidden_events": ["web_access_before_approval", "persist_unused_result", "cite_browser_text", "persist_claim_before_snapshot", "skip_renewed_source_pass", "register_rendered_handoff"],
        "repository_assertions": ["Used page has an immutable local sources/raw/_web version.", "Unused candidate has no source record.", "Claim citation resolves to the local source version and derivation.", "The question record stores the post-capture corpus revision and three pass terms."],
    },
    {
        "schema_version": 1, "id": "contradictory-evidence",
        "title": "Materially contradictory evidence is preserved and interpretation gated",
        "skill": "brain-answer", "prompt": "Which of the conflicting Alpha limits should I rely on?",
        "fixture_id": "contradictory-evidence", "network_mode": "disabled",
        "approval_script": ["Do not approve a preferred interpretation; preserve both sourced limits."],
        "fixture_setup": ["Two active dated Alpha derivations state materially different limits.", "Both claims expose resolving anchors.", "The existing Alpha Q&A contains only the older limit."],
        "receipt_operations": ["initial_sync"],
        "required_events": [*_receipt_events("initial_sync"), "wiki_reconcile_citations", "wiki_search_drained", "revalidate_underlying_citations", "judge_insufficient", "source_pass_discovery", "source_pass_discovery_drained", "source_pass_expansion", "source_pass_expansion_drained", "source_pass_verification", "source_pass_verification_drained", "preserve_both_claims", "cite_both_sides", "ask_interpretation_approval", "write_wiki_question", "link_candidates_drained", "wiki_apply", "links_check", "validate"],
        "forbidden_events": ["silently_overwrite_claim", "choose_without_approval", "remove_old_citation"],
        "repository_assertions": ["The evolving question has conflicted status.", "Both dated claims retain exact resolving citations.", "The terminal canonical v2 QuestionRecord stores the policy's typed unresolved decision with null preference and approval IDs."],
    },
    {
        "schema_version": 1, "id": "repository-development-not-archived",
        "title": "Repository development remains outside the knowledge archive",
        "skill": "brain-answer", "prompt": "Add a --dry-run flag to the sync command and test it.",
        "fixture_id": "repository-development-not-archived", "network_mode": "disabled", "approval_script": [],
        "fixture_setup": ["The repository has a valid initialized empty corpus.", "The request concerns CLI code and tests.", "Wiki pages and question trees are clean before development work."],
        "receipt_operations": [], "required_events": ["classify_repository_development", "use_software_workflow"],
        "forbidden_events": ["invoke_brain_answer", "write_wiki_question", "write_wiki_page", "wiki_apply", "source_sync"],
        "repository_assertions": ["No wiki question file is added or changed.", "No wiki page file is added or changed.", "Only requested software-development paths may differ."],
    },
)


class FixtureGenerationError(RuntimeError):
    pass


def rendered_scenarios(*, fixtures: Path = FIXTURES) -> dict[str, str]:
    result = {}
    for item in SCENARIOS:
        if item["id"] == "contradictory-evidence":
            item = {**item, "interpretation_policy": _interpretation_policy(fixtures / "contradictory-evidence/repo")}
        validate_interpretation_policy(item, fixture_repo=fixtures / str(item["id"]) / "repo")
        result[f"{item['id']}.json"] = json.dumps(item, indent=2, sort_keys=True) + "\n"
    return result


def _paths(root: Path) -> RepoPaths:
    return RepoPaths(root, root / "sources/raw", root / "sources/extracted", root / "sources/ledger", root / "sources/ledger.md", root / "wiki/pages", root / "wiki/questions", root / "config/extractors.toml", root / ".brain/source-write.lock")


def _write(path: Path, payload: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)


def _prepare_repo(repo: Path) -> None:
    for relative in ("sources/raw/_versions/.gitkeep", "sources/raw/_web/.gitkeep", "sources/extracted/.gitkeep", "sources/ledger/.gitkeep", "wiki/pages/.gitkeep", "wiki/questions/.gitkeep"):
        _write(repo / relative, b"")
    _write(repo / "config/extractors.toml", (ROOT / "config/extractors.toml").read_bytes())


def _text_record(repo: Path, raw_path: str, text: str) -> SourceRecord:
    paths = _paths(repo)
    raw = text.encode("utf-8")
    logical = PurePosixPath(raw_path)
    _write(paths.raw / logical, raw)
    registry = ExtractorRegistry.load(paths.registry)
    extractor = registry.select("text/plain", logical)
    if extractor is None:
        raise FixtureGenerationError("text extractor is unexpectedly unavailable")
    digest = prerequisite_digest(extractor)
    version = effective_extractor_version(extractor, digest)
    payload = text_payload(text)
    source_sha = hashlib.sha256(raw).hexdigest()
    identifier = derivation_id(source_sha256=source_sha, extractor_id=extractor.extractor_id, extractor_version=version, config_sha256=extractor.config_sha256)
    output = derive_extraction_path(logical, source_sha, identifier)
    output_path = PurePosixPath("sources/extracted", *output.parts)
    encoded = payload.markdown.encode("utf-8")
    _write(repo / output_path, encoded)
    return SourceRecord(
        1, source_id_for_first_seen(logical, source_sha), logical, (), "text/plain", len(raw), SourceState.OK,
        {source_sha: ContentVersion(source_sha, logical, len(raw), FileFingerprint(logical, len(raw), FIXED_MTIME_NS), FIXED_NOW, ())}, source_sha,
        {identifier: Derivation(identifier, source_sha, extractor.extractor_id, version, extractor.config_sha256, output_path, hashlib.sha256(encoded).hexdigest(), len(encoded), FIXED_MTIME_NS, "ok", payload.anchors, FIXED_NOW, method="deterministic", method_metadata={"converter_id": extractor.preferred.converter_id, "converter_version": f"builtin:{extractor.preferred.converter_id}:{extractor.extractor_version}"})},
        identifier, None, (), FIXED_NOW, FIXED_NOW, FIXED_NOW,
    )


def _save_records(repo: Path, records: Iterable[SourceRecord]) -> tuple[SourceRecord, ...]:
    values = tuple(records)
    ledger = LedgerStore(_paths(repo))
    for record in values:
        ledger.save(record)
    ledger.write_summary(values, generated_at=FIXED_NOW)
    for record in values:
        for version in record.versions.values():
            os.utime(repo / "sources/raw" / version.raw_path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
        for derivation in record.derivations.values():
            os.utime(repo / derivation.output_path, ns=(FIXED_MTIME_NS, FIXED_MTIME_NS))
    return values


def _citation(record: SourceRecord, *, citation_id: str) -> str:
    derivation = record.derivations[record.active_derivation_id or ""]
    anchor = derivation.anchors[0]
    return f"[^${citation_id}]: source_id: `{record.source_id}`; content_sha256: `{record.active_content_sha256}`; derivation_id: `{derivation.derivation_id}`; anchor: `{anchor.kind}:{anchor.value}`; [original](../../sources/raw/{record.current_raw_path.as_posix()}); [extracted](../../{derivation.output_path.as_posix()}#{anchor.kind}:{anchor.value})".replace("[^$", "[^")


def _page(record: SourceRecord, *, title: str = "Alpha") -> str:
    cite = _citation(record, citation_id="alpha-line")
    return f"""---
id: alpha
title: {title}
description: {title} is documented.
type: concept
aliases: []
created: 2026-09-04
updated: 2026-09-04
---
# {title}

## Summary
{title} is documented.[^alpha-line]

## Details
The current local record is cited below.

## Related pages

## Related questions
- [What is Alpha?](../questions/what-is-alpha.md): The durable answer record.

## Sources

{cite}
"""


def _question(records: tuple[SourceRecord, ...], *, conflicted: bool = False) -> str:
    citations = "\n".join(_citation(record, citation_id=f"alpha-{index}") for index, record in enumerate(records, 1))
    uses = "".join(f"[^alpha-{index}]" for index in range(1, len(records) + 1))
    # The literal corpus revision is populated by the caller after the ledger exists.
    status = "conflicted" if conflicted else "answered"
    return f"""---
schema_version: 2
id: question-what-is-alpha
title: What is Alpha?
description: Alpha has a documented local answer.
canonical_question: What is Alpha?
prior_phrasings: [What is Alpha?]
answer_status: {status}
interpretation_decision: {"unresolved" if conflicted else "not_applicable"}
corpus_revision: __REVISION__
last_researched: 2026-09-04
discovery_terms: [Alpha]
expansion_terms: [Beta]
verification_terms: [Alpha]
---
# What is Alpha?

## Current answer
[Alpha](../pages/alpha.md) has a documented local answer.{uses}

## Supporting evidence
The cited record is retained locally.{uses}

## Contradictory evidence
{"Both dated claims are preserved." + uses if conflicted else "None."}

## Related pages
- [Alpha](../pages/alpha.md): The durable topic record.

## Sources

{citations}
"""


def _index(repo: Path) -> None:
    paths = _paths(repo)
    documents = {path: path.read_text(encoding="utf-8") for directory in (paths.wiki_pages, paths.wiki_questions) for path in sorted(directory.glob("*.md")) if path.name != ".gitkeep"}
    _write(repo / "wiki/index.md", render_wiki_index(paths, documents))


def _prior_standard_wiki(repo: Path, record: SourceRecord) -> None:
    """Existing cited evidence, not a placeholder written during the run."""
    citation = _citation(record, citation_id="standard-1")
    revision = LedgerStore(_paths(repo)).render_summary((record,), generated_at=FIXED_NOW).split("Corpus revision: `")[1].split("`")[0]
    _write(repo / "wiki/pages/standard.md", f"""---
id: external-standard
title: External standard
description: The prior external standard limit is documented locally.
type: concept
aliases: []
created: 2026-09-04
updated: 2026-09-04
---
# External standard

## Summary
The prior standard limit is 10.[^standard-1]

## Details
The retained prior standard does not establish this year's changes.

## Related pages

## Related questions
- [What changed in the external standard?](../questions/external-standard.md): The evolving standard comparison.

## Sources

{citation}
""")
    _write(repo / "wiki/questions/external-standard.md", f"""---
schema_version: 2
id: question-external-standard
title: What changed in the external standard?
description: The prior limit is known but current-year changes need evidence.
canonical_question: What changed in the external standard?
prior_phrasings: [What changed in the external standard?]
answer_status: partial
interpretation_decision: not_applicable
corpus_revision: {revision}
last_researched: 2026-09-04
discovery_terms: [standard]
expansion_terms: [limit]
verification_terms: [prior]
---
# What changed in the external standard?

## Current answer
The prior standard limit is 10.[^standard-1]
The retained evidence does not establish this year's changes.

## Supporting evidence
The prior [external standard](../pages/standard.md) remains available locally.[^standard-1]

## Contradictory evidence
None in the retained prior-standard evidence.

## Related pages
- [External standard](../pages/standard.md): The durable prior-standard topic.

## Sources

{citation}
""")


def _validated(repo: Path, *, allow_unledgered_pdf: bool = False) -> None:
    paths = _paths(repo)
    records = LedgerStore(paths).load_all()
    report = validate_source_ledger(paths, records, full=True, checksum_cache=ChecksumCache())
    if allow_unledgered_pdf:
        observed = {(item.code, None if item.path is None else item.path.as_posix()) for item in report.issues}
        if observed != {("source_missing_from_ledger", "quarterly.pdf")}:
            raise FixtureGenerationError(f"binary pre-sync source validation drift: {report.issues}")
    elif not report.ok:
        raise FixtureGenerationError(f"source validation failed: {report.issues}")
    report = validate_wiki(paths, LedgerStore(paths), full=True, checksum_cache=ChecksumCache())
    if not report.ok:
        raise FixtureGenerationError(f"wiki validation failed: {report.issues}")


def _build_fixture(root: Path, scenario_id: str) -> None:
    fixture = root / scenario_id
    repo = fixture / "repo"
    _prepare_repo(repo)
    records: tuple[SourceRecord, ...] = ()
    if scenario_id == "empty-wiki-first-question":
        records = (_text_record(repo, "alpha-beta.txt", "Alpha supports the Beta relationship.\n"),)
    elif scenario_id in {"current-wiki-fast-path", "new-binary-before-question"}:
        records = (_text_record(repo, "alpha.txt", "Alpha has a documented decision.\n"),)
        _save_records(repo, records)
        _write(repo / "wiki/pages/alpha.md", _page(records[0]))
        question = _question(records).replace("__REVISION__", LedgerStore(_paths(repo)).render_summary(records, generated_at=FIXED_NOW).split("Corpus revision: `")[1].split("`")[0])
        _write(repo / "wiki/questions/what-is-alpha.md", question)
        if scenario_id == "new-binary-before-question":
            _write(repo / "sources/raw/quarterly.pdf", b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n")
            _write(fixture / "expected-quarterly.md", "<a id=\"page:1\"></a>\n\nThe quarterly Alpha limit is 12.\n")
    elif scenario_id == "web-approval-and-capture":
        records = (_text_record(repo, "prior-standard.txt", "The prior standard limit is 10.\n"),)
        _save_records(repo, records)
        _prior_standard_wiki(repo, records[0])
        _write(fixture / "static-shell.html", "<html><body><script>rendered standard capture</script></body></html>\n")
        _write(fixture / "rendered-dom.html", "<html><body><main>The external standard now requires limit 12.</main></body></html>\n")
        _write(fixture / "unused-candidate.html", "<html><body>Unused candidate.</body></html>\n")
    elif scenario_id == "contradictory-evidence":
        records = (_text_record(repo, "alpha-2024.txt", "2024 Alpha limit is 10.\n"), _text_record(repo, "alpha-2025.txt", "2025 Alpha limit is 12.\n"))
        _save_records(repo, records)
        _write(repo / "wiki/pages/alpha.md", _page(records[0]))
        question = _question((records[0],), conflicted=False).replace("__REVISION__", LedgerStore(_paths(repo)).render_summary(records, generated_at=FIXED_NOW).split("Corpus revision: `")[1].split("`")[0])
        _write(repo / "wiki/questions/what-is-alpha.md", question)
    elif scenario_id == "repository-development-not-archived":
        _save_records(repo, ())
    else:
        raise FixtureGenerationError(f"unknown scenario {scenario_id}")
    if scenario_id not in {"current-wiki-fast-path", "new-binary-before-question", "contradictory-evidence", "repository-development-not-archived"}:
        _save_records(repo, records)
    _index(repo)
    _validated(repo, allow_unledgered_pdf=scenario_id == "new-binary-before-question")
    _write_manifest(fixture, scenario_id)


def _write_manifest(fixture: Path, scenario_id: str) -> None:
    repo = fixture / "repo"
    paths = sorted((path for path in repo.rglob("*") if path.is_file()), key=lambda path: path.relative_to(repo).as_posix())
    names = ["repo/" + path.relative_to(repo).as_posix() for path in paths]
    digest = hashlib.sha256()
    for name, path in zip(names, paths):
        digest.update(name.removeprefix("repo/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\0")
    _write(fixture / "fixture-manifest.json", json.dumps({"schema_version": 1, "scenario_id": scenario_id, "tree_sha256": digest.hexdigest(), "paths": names}, indent=2, sort_keys=True) + "\n")


def _render(destination: Path) -> None:
    fixtures = destination / "fixtures"
    for scenario_id in sorted(REQUIRED_IDS):
        _build_fixture(fixtures, scenario_id)
    scenarios = destination / "scenarios"
    for name, content in rendered_scenarios(fixtures=fixtures).items():
        _write(scenarios / name, content)


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _write_generated() -> None:
    with tempfile.TemporaryDirectory(prefix="second-brain-evals-", dir=EVAL_ROOT) as temporary:
        generated = Path(temporary) / "generated"
        _render(generated)
        for name in ("scenarios", "fixtures"):
            target = EVAL_ROOT / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(generated / name, target)


def _check() -> None:
    with tempfile.TemporaryDirectory(prefix="second-brain-evals-", dir=EVAL_ROOT) as temporary:
        generated = Path(temporary) / "generated"
        _render(generated)
        expected = _files(generated)
    actual = {**{f"scenarios/{key}": value for key, value in _files(SCENARIO_DIR).items()}, **{f"fixtures/{key}": value for key, value in _files(FIXTURES).items()}}
    if actual != expected:
        raise SystemExit("generated evaluation files are stale; run python3 -m tests.evals.generate_scenarios --write")


def main() -> None:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.write:
        _write_generated()
    else:
        _check()


def _generated_interpretation_policy() -> dict[str, object]:
    # Bootstrapping must never depend on already generated fixture files.
    # Use the same fixture recipe/ledger writer as --write and --check.
    with tempfile.TemporaryDirectory(prefix="second-brain-eval-policy-") as temporary:
        root = Path(temporary).resolve()
        _build_fixture(root, "contradictory-evidence")
        return _interpretation_policy(root / "contradictory-evidence/repo")


SCENARIOS = tuple(
    {**item, "interpretation_policy": _generated_interpretation_policy()}
    if item["id"] == "contradictory-evidence" else item
    for item in SCENARIOS
)


if __name__ == "__main__":
    main()
