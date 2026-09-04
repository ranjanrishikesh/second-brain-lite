"""Executable handoff examples for the future brain-answer skill (no network)."""

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import select
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

try:
    import fcntl
except ImportError:
    fcntl = None

from brainlib.contracts import Anchor, SourceState, compute_corpus_revision
from brainlib.diagnostics import Diagnostic
from brainlib.evidence import EvidencePacket, WikiEvidencePacket
from brainlib.search import (
    SearchRequest,
    complete_search_run,
    completed_search_pass,
    search_active_sources,
    search_wiki,
    verify_search_run_proof,
)
from brainlib.sync_results import SyncResultReference, SyncResultStore
from brainlib.validation import validate_wiki
from brainlib.wiki_evidence import build_wiki_evidence_packet
from brainlib.wiki_models import parse_question
from tests.helpers_knowledge import KnowledgeScenario, drain_search


WORKFLOW_FIXTURE = Path("tests/fixtures/wiki/scenarios/workflow/answer")
SYNC_DATA_KEYS = {
    "status",
    "corpus_revision",
    "decision_counts",
    "sampled_decisions",
    "hashed_paths",
    "hashed_path_count",
    "new_active_representations",
    "new_active_representation_count",
    "citation_rewrites",
    "citation_rewrite_count",
    "handoff_source_ids",
    "handoff_source_id_count",
    "handoff_manifest",
    "handoffs",
    "coverage_gaps",
    "coverage_gap_count",
    "sample_limits",
    "result_manifest",
}


def load_packet(scenario: KnowledgeScenario) -> EvidencePacket:
    return EvidencePacket.from_json(
        (scenario.root / WORKFLOW_FIXTURE / "expected-evidence-packet.json").read_text(encoding="utf-8")
    )


def load_wiki_packet(scenario: KnowledgeScenario) -> WikiEvidencePacket:
    return WikiEvidencePacket.from_json(
        (scenario.root / WORKFLOW_FIXTURE / "expected-wiki-evidence-packet.json").read_text(encoding="utf-8")
    )


def load_events(scenario: KnowledgeScenario, name: str = "workflow-events.json") -> list[str]:
    return json.loads((scenario.root / WORKFLOW_FIXTURE / name).read_text(encoding="utf-8"))


def load_sync(scenario: KnowledgeScenario):
    payload = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    assert set(payload) == {"command", "ok", "data", "warnings", "errors"}
    assert payload["command"] == "sync" and payload["ok"] is False
    assert payload["data"]["status"] == "complete_with_gaps"
    assert payload["errors"][0]["code"] == "source_coverage_gaps"
    data = payload["data"]
    reference = SyncResultReference.from_dict(data["result_manifest"])
    # repo_root preserves the committed receipt; verify the installed artifact.
    target = scenario.root / reference.path
    store = SyncResultStore(scenario.paths)
    store.verify(reference)
    events = tuple(store.iter_events(reference))
    assert reference.corpus_revision == data["corpus_revision"]
    counts = Counter(event.kind for event in events)
    for kind, count in reference.event_counts.items():
        assert counts[kind] == count == data[f"{kind}_count"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == reference.sha256
    return payload, reference, events


def test_insufficient_wiki_requires_exactly_discovery_expansion_verification_packet(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    packet = load_packet(scenario_repo("workflow/answer"))
    assert tuple(item.name for item in packet.passes) == ("discovery", "expansion", "verification")
    assert tuple(item.terms for item in packet.passes) == (("Alpha",), ("Beta relationship",), ("Alpha exception",))
    assert all(item.complete and item.pages for item in packet.passes)
    assert len({item.run_id for item in packet.passes}) == 3
    assert len(packet.passes[0].pages) > 1


def test_packet_consumes_exact_sync_freshness_fields_and_coverage_gap(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    payload, _, events = load_sync(scenario)
    revision = payload["data"]["corpus_revision"]
    assert packet.corpus_revision == revision == compute_corpus_revision(scenario.ledger.load_all().values())
    fresh_ids = tuple(event.data["source_id"] for event in events if event.kind == "new_active_representation")
    assert packet.freshness_probe_source_ids == fresh_ids
    assert all(item.corpus_revision == revision for item in packet.passes)
    assert packet.support[0].source_id == fresh_ids[0]
    assert packet.coverage_gaps == (Diagnostic("source_failed", "src-unavailable is failed"),)
    assert [dict(event.data) for event in events if event.kind == "coverage_gap"] == [
        {"code": "source_failed", "message": "src-unavailable is failed", "path": None, "details": {}}
    ]


def test_relevant_freshness_probe_triggers_three_research_passes_not_a_fourth_pass(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    _, _, events = load_sync(scenario)
    fresh_ids = tuple(event.data["source_id"] for event in events if event.kind == "new_active_representation")
    assert packet.freshness_probe_source_ids == fresh_ids
    first = search_active_sources(
        scenario.paths, scenario.ledger,
        SearchRequest("sources", None, ("Alpha",), 0, page_size=1, freshness_source_ids=fresh_ids),
    )
    pages = drain_search(scenario.paths, scenario.ledger, first)
    verify_search_run_proof(scenario.paths, scenario.ledger, complete_search_run(pages))
    assert first.searched_source_ids == fresh_ids
    assert any(page.matches for page in pages)
    with pytest.raises(ValueError, match="only a named source research run"):
        completed_search_pass(pages)
    assert tuple(item.name for item in packet.passes) == ("discovery", "expansion", "verification")


def test_every_sync_is_followed_by_wiki_apply_before_wiki_search_even_without_rewrites(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    _, _, events = load_sync(scenario)
    assert not any(event.kind == "citation_rewrite" for event in events)
    assert load_events(scenario)[:4] == ["sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete"]


def test_every_logical_source_pass_is_drained_before_the_next_one_or_synthesis(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    events = load_events(scenario)
    milestones = [
        "source_discovery_start", "source_discovery_complete",
        "source_expansion_start", "source_expansion_complete",
        "source_verification_start", "source_verification_complete", "curator",
    ]
    assert [event for event in events if event in milestones] == milestones
    assert events.index("freshness_complete") < events.index("source_discovery_start")
    assert events[-5:] == ["link_candidates_start", "link_candidates_complete", "wiki_apply", "validate", "answer"]


def test_sufficient_wiki_fast_path_still_updates_topic_record_without_source_passes(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_wiki_packet(scenario)
    assert packet.complete is True and packet.supporting_citations
    assert load_events(scenario, "wiki-fast-path-events.json") == [
        "sync", "wiki_apply_after_sync", "wiki_search_start", "wiki_search_complete",
        "freshness_start", "freshness_complete", "wiki_evidence_packet",
        "curator_update_question", "link_candidates_start", "link_candidates_complete",
        "wiki_apply", "validate", "answer",
    ]


def test_source_packet_proofs_replay_from_the_committed_active_corpus(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    packet = load_packet(scenario)
    for expected in packet.passes:
        first = search_active_sources(
            scenario.paths, scenario.ledger,
            SearchRequest("sources", expected.name, expected.terms, 0, page_size=1),
        )
        pages = drain_search(scenario.paths, scenario.ledger, first)
        verify_search_run_proof(scenario.paths, scenario.ledger, complete_search_run(pages))
        actual = completed_search_pass(pages)
        # Run IDs are fresh opaque capabilities; the evidence content is fixed.
        assert replace(actual, run_id=expected.run_id) == expected
    for item in (*packet.support, *packet.counterevidence):
        representation = scenario.ledger.find_representation(item.source_id, item.content_sha256, item.derivation_id)
        assert representation is not None
        lines = (scenario.root / representation.extracted_path).read_text(encoding="utf-8").splitlines()
        assert item.anchor.kind == "line"
        assert lines[int(item.anchor.value) - 1] == item.passage


def test_wiki_fast_path_revalidates_exact_citations_and_combined_validation(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    expected = load_wiki_packet(scenario)
    first = search_wiki(scenario.paths, scenario.ledger, SearchRequest("wiki", None, ("Alpha",), 0, page_size=1))
    pages = drain_search(scenario.paths, scenario.ledger, first)
    verify_search_run_proof(scenario.paths, scenario.ledger, complete_search_run(pages))
    actual = build_wiki_evidence_packet(
        scenario.paths, scenario.ledger, question_id=expected.question_id,
        search_pages=pages, matched_paths=tuple(item.path for item in expected.matched_records),
        supporting_citations=expected.supporting_citations,
        counterevidence_citations=expected.counterevidence_citations,
        contradictions=expected.contradictions,
    )
    assert replace(actual, search_run_id=expected.search_run_id) == expected
    assert expected.supporting_citations[0].document_path.as_posix() == "wiki/questions/what-is-alpha.md"
    assert expected.supporting_citations[0].citation_id == "cite-alpha-line-4"
    assert validate_wiki(scenario.paths, scenario.ledger, full=True).ok
    assert (scenario.root / "tests/fixtures/wiki/workflow/what-is-alpha.md").read_bytes() == (
        scenario.paths.wiki_questions / "what-is-alpha.md"
    ).read_bytes()


def test_tampered_sync_manifest_is_rejected_before_any_event_is_consumed(
    scenario_repo: Callable[[str], KnowledgeScenario],
) -> None:
    scenario = scenario_repo("workflow/answer")
    _, reference, _ = load_sync(scenario)
    target = scenario.root / reference.path
    target.write_bytes(target.read_bytes().replace(b"src-unavailable", b"src-untrustable"))
    with pytest.raises(ValueError):
        SyncResultStore(scenario.paths).verify(reference)
    with pytest.raises(ValueError):
        next(SyncResultStore(scenario.paths).iter_events(reference))


@pytest.mark.parametrize("damage", ("deleted", "symlink"))
def test_load_sync_verifies_installed_receipt_without_restoring_it(
    scenario_repo: Callable[[str], KnowledgeScenario], tmp_path: Path, damage: str,
) -> None:
    scenario = scenario_repo("workflow/answer")
    payload = json.loads(
        (scenario.root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    receipt = scenario.root / payload["data"]["result_manifest"]["path"]
    receipt.unlink()
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_bytes(b"must remain external\n")
    if damage == "symlink":
        receipt.symlink_to(sentinel)

    rejected = False
    try:
        load_sync(scenario)
    except (ValueError, OSError):
        rejected = True

    assert sentinel.read_bytes() == b"must remain external\n"
    assert rejected, "load_sync silently restored a missing installed receipt"
    assert receipt.is_symlink() if damage == "symlink" else not receipt.exists()


def test_fixture_ignore_rules_track_only_the_digest_verified_sync_receipt() -> None:
    repository = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (repository / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(payload["data"]) == SYNC_DATA_KEYS
    assert payload["data"]["handoff_manifest"] is None
    assert payload["data"]["handoffs"] == []
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    brain_root = WORKFLOW_FIXTURE / "repo" / ".brain"
    receipt = WORKFLOW_FIXTURE / "repo" / Path(reference.path)

    def evaluate(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "check-ignore", "-v", "--no-index", path.as_posix()),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )

    receipt_result = evaluate(receipt)
    assert receipt_result.returncode == 0
    assert ":!" in receipt_result.stdout
    assert receipt_result.stdout.rstrip().endswith("\t" + receipt.as_posix())

    for sibling in (brain_root / "other.json", brain_root / "sync-results/other.jsonl"):
        sibling_result = evaluate(sibling)
        assert sibling_result.returncode == 0
        assert ":!" not in sibling_result.stdout
        assert sibling_result.stdout.rstrip().endswith("\t" + sibling.as_posix())


def test_follow_up_refines_one_topic_record_and_preserves_question_history(repo_root: Path) -> None:
    record = parse_question(repo_root / "tests/fixtures/wiki/workflow/what-is-alpha.md")
    assert record.question_id == "question-alpha"
    assert record.canonical_question == "How does Alpha relate to Beta?"
    assert record.prior_phrasings == ("What is Alpha?",)
    assert record.answer_status in {"answered", "partial", "conflicted"}


def test_committed_scenario_overlays_are_reproducible(repo_root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_scenario_generator_check_rejects_unexpected_owned_file_without_removing_it(
    repo_root: Path,
) -> None:
    unexpected = repo_root / "tests/fixtures/wiki/scenarios/workflow/answer/unexpected.txt"
    unexpected.write_text("curator-owned fixture note\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "unexpected" in (completed.stdout + completed.stderr)
    assert unexpected.read_text(encoding="utf-8") == "curator-owned fixture note\n"


def test_scenario_generator_write_retains_undeclared_owned_file(repo_root: Path) -> None:
    unexpected = repo_root / "tests/fixtures/wiki/scenarios/workflow/answer/unexpected.txt"
    unexpected.write_text("curator-owned fixture note\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--write"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert unexpected.read_text(encoding="utf-8") == "curator-owned fixture note\n"


def test_scenario_generator_check_rejects_missing_workflow_receipt(repo_root: Path) -> None:
    payload = json.loads(
        (repo_root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    receipt = repo_root / WORKFLOW_FIXTURE / "repo" / reference.path
    assert receipt.is_file()
    receipt.unlink()

    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "sync-results" in (completed.stdout + completed.stderr)


def test_scenario_generator_check_rejects_tampered_workflow_receipt(repo_root: Path) -> None:
    payload = json.loads(
        (repo_root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    receipt = repo_root / WORKFLOW_FIXTURE / "repo" / reference.path
    original = receipt.read_bytes()
    tampered = original.replace(b'"path":"alpha.txt"', b'"path":"omega.txt"', 1)
    assert tampered != original
    receipt.write_bytes(tampered)

    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "sync-results" in (completed.stdout + completed.stderr)
    assert receipt.read_bytes() != original


def test_repo_root_copy_preserves_symlinked_workflow_receipt_for_generator_check(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import conftest as repository_fixtures

    payload = json.loads(
        (repo_root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    receipt_relative = WORKFLOW_FIXTURE / "repo" / reference.path
    receipt = repo_root / receipt_relative
    original = receipt.read_bytes()
    sentinel = tmp_path / "external-receipt.jsonl"
    sentinel.write_bytes(original)
    receipt.unlink()
    receipt.symlink_to(sentinel)

    # Use the isolated repository as the source of the real fixture-copy boundary.
    monkeypatch.setattr(repository_fixtures, "__file__", str(repo_root / "tests/conftest.py"))
    copied_root = repository_fixtures.repo_root.__wrapped__(tmp_path / "recopy")
    copied_receipt = copied_root / receipt_relative
    completed = subprocess.run(
        [sys.executable, "tests/fixtures/wiki/generate_scenarios.py", "--check"],
        cwd=copied_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert copied_receipt.is_symlink(), completed.stdout + completed.stderr
    assert os.readlink(copied_receipt) == str(sentinel)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    logical_receipt = Path("scenarios/workflow/answer/repo") / reference.path
    assert f"type {logical_receipt.as_posix()}" in completed.stdout
    assert receipt.is_symlink()
    assert sentinel.read_bytes() == original


@pytest.mark.parametrize("ancestor", (".brain", ".brain/sync-results"))
@pytest.mark.parametrize("damage", ("symlink", "missing", "regular-file"))
def test_repo_root_copy_rejects_unsafe_workflow_receipt_ancestor(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor: str,
    damage: str,
) -> None:
    from tests import conftest as repository_fixtures

    payload = json.loads(
        (repo_root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    reference = SyncResultReference.from_dict(payload["data"]["result_manifest"])
    receipt_relative = WORKFLOW_FIXTURE / "repo" / reference.path
    original = (repo_root / receipt_relative).read_bytes()
    ancestor_relative = WORKFLOW_FIXTURE / "repo" / ancestor
    source_ancestor = repo_root / ancestor_relative
    external = tmp_path / "external-receipt-directory"
    source_ancestor.rename(external)
    external_receipt = external / (repo_root / receipt_relative).relative_to(source_ancestor)
    if damage == "symlink":
        source_ancestor.symlink_to(external, target_is_directory=True)
    elif damage == "regular-file":
        source_ancestor.write_bytes(b"not a directory\n")

    monkeypatch.setattr(repository_fixtures, "__file__", str(repo_root / "tests/conftest.py"))
    destination = tmp_path / "recopy/second-brain-lite"

    with pytest.raises(ValueError, match="unsafe fixture receipt ancestor"):
        repository_fixtures.repo_root.__wrapped__(tmp_path / "recopy")

    assert not (destination / receipt_relative).exists()
    assert not (destination / WORKFLOW_FIXTURE / "repo/.brain").exists()
    assert external_receipt.read_bytes() == original
    if damage == "symlink":
        assert source_ancestor.is_symlink()
        assert os.readlink(source_ancestor) == str(external)


@pytest.mark.parametrize(
    "leaf_kind",
    (
        pytest.param(
            "fifo",
            marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO probe requires os.mkfifo"),
        ),
        "directory",
    ),
)
def test_repo_root_copy_rejects_unsafe_workflow_receipt_leaf_without_reading(
    repo_root: Path, tmp_path: Path, leaf_kind: str,
) -> None:
    payload = json.loads(
        (repo_root / WORKFLOW_FIXTURE / "sync-command-result.json").read_text(encoding="utf-8")
    )
    receipt_relative = WORKFLOW_FIXTURE / "repo" / payload["data"]["result_manifest"]["path"]
    receipt = repo_root / receipt_relative
    sentinel = tmp_path / "external-receipt.jsonl"
    sentinel.write_bytes(b"never read or modify outside receipt state\n")
    sentinel_before = sentinel.stat()
    receipt.unlink()
    if leaf_kind == "fifo":
        os.mkfifo(receipt)
    else:
        receipt.mkdir()
        (receipt / "outside.jsonl").symlink_to(sentinel)
    leaf_before = receipt.lstat()
    destination = tmp_path / "recopy/second-brain-lite"
    # Observe the real copy/read boundary without replacing its implementation.
    # The timeout also bounds failure if a regression tries to read the FIFO.
    probe = """
import json
import os
from pathlib import Path
import sys

from tests import conftest as repository_fixtures

source, receipt, sentinel, destination = map(Path, sys.argv[1:])
repository_fixtures.__file__ = str(source / "tests/conftest.py")
accesses = []

def observe_access(event, args):
    if event in {"open", "shutil.copyfile"} and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).absolute()
        if path == receipt or receipt in path.parents or path == sentinel:
            accesses.append([event, str(path)])

sys.addaudithook(observe_access)
try:
    repository_fixtures.repo_root.__wrapped__(destination)
except Exception as error:
    result = {"error": type(error).__name__, "message": str(error)}
else:
    result = {"error": None, "message": "unsafe receipt was accepted"}
print(json.dumps({**result, "accesses": accesses}), flush=True)
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe, str(repo_root), str(receipt), str(sentinel), str(destination.parent)],
            cwd=Path(__file__).resolve().parents[2],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"fixture copy blocked while rejecting a {leaf_kind} receipt leaf")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["error"] == "ValueError", result
    assert "unsafe fixture receipt" in result["message"]
    assert str(receipt) in result["message"]
    assert result["accesses"] == [], result
    assert not (destination / receipt_relative).exists()
    assert not destination.exists()
    assert os.path.samestat(receipt.lstat(), leaf_before)
    assert receipt.is_fifo() if leaf_kind == "fifo" else receipt.is_dir()
    assert sentinel.stat() == sentinel_before
    assert sentinel.read_bytes() == b"never read or modify outside receipt state\n"


@pytest.mark.parametrize(
    "module_name",
    (
        "tests.fixtures.wiki.generate_scenarios",
        "tests.integration.test_knowledge_workflow_contract",
    ),
)
def test_workflow_modules_import_without_fcntl(module_name: str) -> None:
    probe = """
import builtins
import importlib
import sys

real_import = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_fcntl
sys.modules.pop("fcntl", None)

module = importlib.import_module(sys.argv[1])
print("imported " + module.__name__, flush=True)
if sys.argv[1].startswith("tests.integration."):
    import pytest
    raise SystemExit(pytest.main([
        module.__file__, "-k", "holds_root_lock_through_operation",
        "-q", "-p", "no:cacheprovider",
    ]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, module_name],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"imported {module_name}" in completed.stdout
    if module_name.startswith("tests.integration."):
        assert "2 skipped" in completed.stdout


def _load_scenario_generator():
    name = "scenario_generator_contract_probe"
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parents[1] / "fixtures/wiki/generate_scenarios.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _copied_fixture_root(generator, tmp_path: Path) -> Path:
    root = tmp_path / "fixture-root"
    shutil.copytree(generator.FIXTURE_ROOT, root, symlinks=True)
    return root


@pytest.mark.parametrize("operation", ("--check", "--write"))
@pytest.mark.parametrize("descriptor_flags_available", (True, False))
def test_scenario_generator_fails_closed_without_lock_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    descriptor_flags_available: bool,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    fixture_root.mkdir()
    sentinel = fixture_root / "untouched.txt"
    sentinel.write_bytes(b"must remain untouched\n")
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "fcntl", None)
    if not descriptor_flags_available:
        monkeypatch.setattr(generator, "os", SimpleNamespace(**{
            name: value for name, value in vars(os).items()
            if name not in {"O_DIRECTORY", "O_NOFOLLOW"}
        }))

    with pytest.raises(generator.FixtureGenerationError, match="safe fixture lock backend is unavailable"):
        generator.main([operation])
    with pytest.raises(generator.FixtureGenerationError, match="safe fixture lock backend is unavailable"):
        if operation == "--write":
            generator._write(())
        else:
            generator._compare(())

    assert sentinel.read_bytes() == b"must remain untouched\n"
    assert list(fixture_root.iterdir()) == [sentinel]


def test_scenario_generator_check_cannot_mix_replaced_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    replacement = tmp_path / "replacement-root"
    shutil.copytree(fixture_root, replacement, symlinks=True)
    (fixture_root / "workflow/what-is-alpha.md").write_bytes(b"invalid first workflow\n")
    (replacement / "scenarios/README.md").write_bytes(b"invalid second scenarios\n")
    walk_owned = generator._walk_owned_directory
    swapped = False

    def swap_after_scenarios(parent, relative, values):
        nonlocal swapped
        walk_owned(parent, relative, values)
        if relative == PurePosixPath("scenarios") and not swapped:
            fixture_root.rename(tmp_path / "detached-root")
            replacement.rename(fixture_root)
            swapped = True

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "_walk_owned_directory", swap_after_scenarios)

    try:
        result = generator.main(["--check"])
    except generator.FixtureGenerationError:
        result = 1

    assert swapped
    assert result == 1, "check combined two invalid fixture roots into false success"


@pytest.mark.parametrize("mutation", ("write", "stale"))
def test_scenario_generator_rejects_detached_parent_before_next_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    (fixture_root / "scenarios").mkdir(parents=True)
    (fixture_root / "scenarios/README.md").write_bytes(b"external sentinel\n")
    detached = tmp_path / "detached-scenarios"
    preflight = generator._preflight_declared_path
    open_directory = generator._open_directory_at
    armed = False
    moved = False

    def arm_after_preflight(*args, **kwargs):
        nonlocal armed
        preflight(*args, **kwargs)
        armed = True

    def detach_after_open(parent_fd, name, relative):
        nonlocal moved
        descriptor = open_directory(parent_fd, name, relative)
        if armed and not moved and relative == PurePosixPath("scenarios"):
            (fixture_root / "scenarios").rename(detached)
            moved = True
        return descriptor

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "_preflight_declared_path", arm_after_preflight)
    monkeypatch.setattr(generator, "_open_directory_at", detach_after_open)
    entry = generator.GeneratedEntry(PurePosixPath("scenarios/README.md"), "file", 0o644, b"generated\n")
    if mutation == "stale":
        monkeypatch.setattr(generator, "STALE_GENERATED_PATHS", frozenset({entry.relative}))

    with pytest.raises(generator.FixtureGenerationError):
        generator._write((entry,) if mutation == "write" else ())

    assert moved
    assert (detached / "README.md").read_bytes() == b"external sentinel\n"
    assert list(detached.iterdir()) == [detached / "README.md"]


def test_scenario_generator_stages_payload_under_root_when_descendant_detaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    (fixture_root / "scenarios").mkdir(parents=True)
    (fixture_root / "scenarios/README.md").write_bytes(b"external sentinel\n")
    detached = tmp_path / "detached-scenarios"
    write_bytes = generator._write_bytes

    def detach_before_payload(descriptor, payload):
        (fixture_root / "scenarios").rename(detached)
        write_bytes(descriptor, payload)

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "_write_bytes", detach_before_payload)
    entry = generator.GeneratedEntry(PurePosixPath("scenarios/README.md"), "file", 0o644, b"generated\n")

    with pytest.raises(generator.FixtureGenerationError):
        generator._write((entry,))

    assert (detached / "README.md").read_bytes() == b"external sentinel\n"
    assert list(detached.iterdir()) == [detached / "README.md"]
    assert list(fixture_root.iterdir()) == []


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="descriptor count probe requires /dev/fd")
def test_scenario_generator_closes_scratch_descriptor_if_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    (fixture_root / "scenarios").mkdir(parents=True)
    target = fixture_root / "scenarios/README.md"
    target.write_bytes(b"before publication\n")
    open_file = os.open
    fstat = os.fstat
    scratch_fd = None

    def capture_scratch(path, flags, *args, **kwargs):
        nonlocal scratch_fd
        descriptor = open_file(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            scratch_fd = descriptor
        return descriptor

    def fail_scratch_metadata(descriptor):
        if descriptor == scratch_fd:
            raise OSError("injected scratch fstat failure")
        return fstat(descriptor)

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator.os, "open", capture_scratch)
    monkeypatch.setattr(generator.os, "fstat", fail_scratch_metadata)
    entry = generator.GeneratedEntry(PurePosixPath("scenarios/README.md"), "file", 0o644, b"generated\n")
    before = set(os.listdir("/dev/fd"))

    with pytest.raises(OSError, match="injected scratch fstat failure"):
        generator._write((entry,))

    assert scratch_fd is not None
    assert set(os.listdir("/dev/fd")) == before
    assert target.read_bytes() == b"before publication\n"


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="descriptor count probe requires /dev/fd")
@pytest.mark.parametrize("context", ("root", "descendant"))
def test_scenario_generator_closes_remaining_descriptors_after_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    (fixture_root / "scenarios/child").mkdir(parents=True)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    before = set(os.listdir("/dev/fd"))
    ownership = generator._Ownership(write=False).__enter__()
    target = ownership
    if context == "descendant":
        before = set(os.listdir("/dev/fd"))
        target = generator._open_output_parent(ownership, PurePosixPath("scenarios/child/file"), create=False)
    close = os.close
    failed = False

    def report_first_close_error(descriptor):
        nonlocal failed
        close(descriptor)
        if not failed:
            failed = True
            raise OSError("injected close failure")

    monkeypatch.setattr(generator.os, "close", report_first_close_error)
    try:
        with pytest.raises(OSError, match="injected close failure"):
            target.close()
        assert set(os.listdir("/dev/fd")) == before
        target.close()
    finally:
        if context == "descendant":
            ownership.close()


@pytest.mark.skipif(fcntl is None, reason="fixture root lock probe requires fcntl")
@pytest.mark.parametrize("operation", ("check", "write"))
def test_scenario_generator_holds_root_lock_through_operation(tmp_path: Path, operation: str) -> None:
    fixture_root = tmp_path / "fixture-root"
    (fixture_root / "scenarios").mkdir(parents=True)
    probe = """
from pathlib import Path, PurePosixPath
import sys
from tests.integration.test_knowledge_workflow_contract import _load_scenario_generator

generator = _load_scenario_generator()
generator.FIXTURE_ROOT = Path(sys.argv[1])

def pause():
    print("inside-operation", flush=True)
    assert sys.stdin.readline().strip() == "continue"

if sys.argv[2] == "check":
    compare = generator._compare_owned
    def hold_check(*args):
        pause()
        return compare(*args)
    generator._compare_owned = hold_check
    generator._compare(())
else:
    write = generator._write_entry
    def hold_write(*args):
        pause()
        return write(*args)
    generator._write_entry = hold_write
    generator._write((generator.GeneratedEntry(PurePosixPath("scenarios/README.md"), "file", 0o644, b"written"),))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", probe, str(fixture_root), operation],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready, "generator did not reach the bounded operation probe"
        assert process.stdout.readline().strip() == "inside-operation"
        descriptor = os.open(fixture_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if operation == "check":
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        stdout, stderr = process.communicate("continue\n", timeout=5)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize(
    "relative",
    ("outside.md", "sources/raw/user-source.txt", "wiki/pages/user-page.md", "workflow/other.md", "workflow", "scenarios"),
)
@pytest.mark.parametrize("mutation", ("generated", "stale"))
def test_scenario_generator_rejects_out_of_scope_plan_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, mutation: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = tmp_path / "fixture-root"
    fixture_root.mkdir()
    marker = fixture_root / "scenarios/README.md"
    marker.parent.mkdir()
    marker.write_bytes(b"before publication\n")
    valid = generator.GeneratedEntry(PurePosixPath("scenarios/README.md"), "file", 0o644, b"published\n")
    entries = (valid,)
    if mutation == "generated":
        entries += (generator.GeneratedEntry(PurePosixPath(relative), "file", 0o644, b"outside scope\n"),)
    else:
        monkeypatch.setattr(generator, "STALE_GENERATED_PATHS", frozenset({PurePosixPath(relative)}))
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(generator.FixtureGenerationError):
        generator._write(entries)

    assert marker.read_bytes() == b"before publication\n"
    assert not (fixture_root / "workflow").exists()
    assert not (fixture_root / "outside.md").exists()


@pytest.mark.parametrize(
    "relative",
    ("scenarios/unexpected-empty", "scenarios/workflow/answer/repo/wiki/pages/unexpected-empty"),
)
def test_scenario_generator_check_reports_unexpected_empty_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    relative: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    unexpected = fixture_root / relative
    unexpected.mkdir()
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)

    assert generator.main(["--check"]) == 1

    assert f"unexpected {relative}" in capsys.readouterr().out
    assert unexpected.is_dir()
    assert list(unexpected.iterdir()) == []


@pytest.mark.parametrize(
    ("owned_parent", "sentinel_relative"),
    (
        (Path("scenarios"), Path("README.md")),
        (Path("scenarios/workflow/answer"), Path("question-input.md")),
    ),
)
def test_scenario_generator_write_refuses_symlinked_owned_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned_parent: Path,
    sentinel_relative: Path,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    external = tmp_path / "external-sources"
    external.mkdir()
    sentinel = external / sentinel_relative
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_bytes(b"never modify external sources\n")
    target = fixture_root / owned_parent
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    target.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)

    with pytest.raises(generator.FixtureGenerationError):
        generator.main(["--write"])

    assert sentinel.read_bytes() == b"never modify external sources\n"


@pytest.mark.parametrize("artifact_kind", ("regular", "symlink"))
def test_scenario_generator_write_ignores_predictable_temp_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_kind: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    artifact = fixture_root / "scenarios/README.md.generate-scenarios.tmp"
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_bytes(b"never modify external sources\n")
    if artifact_kind == "regular":
        artifact.write_bytes(b"unrelated pre-existing temporary\n")
    else:
        artifact.symlink_to(sentinel)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)

    assert generator.main(["--write"]) == 0

    assert sentinel.read_bytes() == b"never modify external sources\n"
    assert not (fixture_root / "scenarios/README.md").is_symlink()
    assert (fixture_root / "scenarios/README.md").read_text(encoding="utf-8") == generator.README
    if artifact_kind == "regular":
        assert artifact.read_bytes() == b"unrelated pre-existing temporary\n"
    else:
        assert artifact.is_symlink()
        assert os.readlink(artifact) == str(sentinel)


def test_scenario_generator_check_reports_fifo_in_owned_scenarios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    fifo = fixture_root / "scenarios/unsafe.fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)

    assert generator.main(["--check"]) == 1

    output = capsys.readouterr().out
    assert "unsupported scenarios/unsafe.fifo" in output


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO race probe requires os.mkfifo")
@pytest.mark.parametrize("relative", ("scenarios/README.md", "workflow/what-is-alpha.md"))
def test_scenario_generator_check_rejects_file_swapped_to_fifo_without_blocking(
    tmp_path: Path, relative: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    target = fixture_root / relative
    # Inject the swap after the real stat, then exercise the real open and fstat.
    # A subprocess timeout bounds RED; TMPDIR keeps interrupted builds in pytest's tree.
    probe = """
import os
from pathlib import Path
import stat
import sys

from tests.integration.test_knowledge_workflow_contract import _load_scenario_generator

generator = _load_scenario_generator()
generator.FIXTURE_ROOT = Path(sys.argv[1])
target = generator.FIXTURE_ROOT / sys.argv[2]
original = target.lstat()
assert stat.S_ISREG(original.st_mode)
metadata_at = generator._entry_metadata
swapped = False

def replace_after_stat(parent_fd, name):
    global swapped
    metadata = metadata_at(parent_fd, name)
    if not swapped and metadata is not None and os.path.samestat(metadata, original):
        os.unlink(name, dir_fd=parent_fd)
        os.mkfifo(name, dir_fd=parent_fd)
        swapped = True
        print("fifo-race-injected", flush=True)
    return metadata

generator._entry_metadata = replace_after_stat
raise SystemExit(generator.main(["--check"]))
"""
    try:
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(fixture_root), relative],
                cwd=Path(__file__).resolve().parents[2],
                env={**os.environ, "TMPDIR": str(tmp_path)},
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except subprocess.TimeoutExpired as error:
            assert b"fifo-race-injected" in (error.stdout or b"")
            pytest.fail("fixture check blocked after a regular file became a FIFO")

        assert "fifo-race-injected" in completed.stdout
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert f"unsupported {relative}" in completed.stdout
        assert target.is_fifo()
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.parametrize("relative", ("scenarios/README.md", "workflow/what-is-alpha.md"))
def test_scenario_generator_check_reports_executable_bit_changed_after_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    relative: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    target = fixture_root / relative
    target.chmod(0o644)
    original = target.lstat()
    payload = target.read_bytes()
    replacement = tmp_path / "executable-replacement"
    replacement.write_bytes(payload)
    replacement.chmod(0o755)
    metadata_at = generator._entry_metadata
    swapped = False

    def replace_after_stat(parent_fd, name):
        nonlocal swapped
        metadata = metadata_at(parent_fd, name)
        if not swapped and metadata is not None and os.path.samestat(metadata, original):
            replacement.replace(target)
            swapped = True
        return metadata

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "_entry_metadata", replace_after_stat)

    raced_result = generator.main(["--check"])
    raced_output = capsys.readouterr().out

    assert swapped
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o755
    assert generator.main(["--check"]) == 1
    assert f"executable-bit {relative}" in capsys.readouterr().out
    assert raced_result == 1, raced_output
    assert f"executable-bit {relative}" in raced_output
    assert f"content {relative}" not in raced_output


def test_scenario_generator_check_fails_closed_without_nonblocking_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    os_without_nonblocking = SimpleNamespace(**{
        name: value for name, value in vars(os).items() if name != "O_NONBLOCK"
    })
    monkeypatch.setattr(generator, "os", os_without_nonblocking)

    assert generator.main(["--check"]) == 1

    assert "unsupported scenarios/README.md" in capsys.readouterr().out


@pytest.mark.parametrize("relative", ("scenarios", "scenarios/README.md", "workflow/what-is-alpha.md"))
def test_scenario_generator_reports_symlink_changed_after_metadata_as_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    target = fixture_root / relative
    if target.is_dir():
        target.rename(tmp_path / "original-scenarios")
    else:
        target.unlink()
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_bytes(b"never modify external sources\n")
    target.symlink_to(sentinel)
    original = target.lstat()
    metadata_at = generator._entry_metadata
    swapped = False

    def replace_after_stat(parent_fd, name):
        nonlocal swapped
        metadata = metadata_at(parent_fd, name)
        if not swapped and metadata is not None and os.path.samestat(metadata, original):
            target.unlink()
            target.write_bytes(b"changed entry\n")
            swapped = True
        return metadata

    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    monkeypatch.setattr(generator, "_entry_metadata", replace_after_stat)

    assert f"unsupported {relative}" in generator._compare(())

    assert target.read_bytes() == b"changed entry\n"
    assert sentinel.read_bytes() == b"never modify external sources\n"


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="descriptor count probe requires /dev/fd")
def test_scenario_generator_owned_target_scan_closes_all_directory_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    generator = _load_scenario_generator()
    fixture_root = _copied_fixture_root(generator, tmp_path)
    monkeypatch.setattr(generator, "FIXTURE_ROOT", fixture_root)
    before = set(os.listdir("/dev/fd"))

    for _ in range(2):
        generator._owned_targets()
        assert generator.main(["--check"]) == 0

    capsys.readouterr()
    assert set(os.listdir("/dev/fd")) == before


def test_scenario_generator_exercises_authoritative_build_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_scenario_generator()
    calls: Counter[str] = Counter()
    save = generator.LedgerStore.save
    summary = generator.LedgerStore.write_summary
    render = generator.render_wiki_index
    finalize = generator.SyncResultWriter.finalize

    def spy_save(*args, **kwargs):
        calls["save"] += 1
        return save(*args, **kwargs)

    def spy_summary(*args, **kwargs):
        calls["summary"] += 1
        return summary(*args, **kwargs)

    def spy_render(*args, **kwargs):
        calls["render"] += 1
        return render(*args, **kwargs)

    def spy_finalize(*args, **kwargs):
        calls["finalize"] += 1
        return finalize(*args, **kwargs)

    monkeypatch.setattr(generator.LedgerStore, "save", spy_save)
    monkeypatch.setattr(generator.LedgerStore, "write_summary", spy_summary)
    monkeypatch.setattr(generator, "render_wiki_index", spy_render)
    monkeypatch.setattr(generator.SyncResultWriter, "finalize", spy_finalize)

    generator._materialize(tmp_path / "generated")

    assert all(calls[name] > 0 for name in ("save", "summary", "render", "finalize"))


def test_scenario_generator_derives_source_identity_and_artifacts_from_payload(tmp_path: Path) -> None:
    generator = _load_scenario_generator()

    def build(name: str, raw: bytes):
        tree = generator.FixtureTree(tmp_path / name)
        raw_path = PurePosixPath("notes/example.txt")
        checksum = generator._sha(raw)
        record = generator._write_record(
            tree,
            "isolated",
            identity_path=raw_path,
            identity_bytes=raw,
            current_path=raw_path,
            live_bytes=raw,
            versions=((raw_path, raw),),
            state=SourceState.OK,
            active_sha=checksum,
            derivations=((
                "drv_" + "f" * 64,
                checksum,
                raw_path,
                b"# extracted\n" + raw,
                Anchor("line", "1"),
                "builtin.text",
                "1",
            ),),
            active_derivation="drv_" + "f" * 64,
        )
        generator._finish_ledger(tree, "isolated", (record,))
        repo = tree.repo("isolated")
        artifact = repo / next(iter(record.derivations.values())).output_path
        return record, (repo / "sources/raw/notes/example.txt").read_bytes(), artifact.read_bytes(), (repo / "sources/ledger.md").read_text(encoding="utf-8")

    first = build("first", b"Alpha source one\n")
    second = build("second", b"Alpha source two\n")

    assert first[0].source_id != second[0].source_id
    assert first[0].active_content_sha256 != second[0].active_content_sha256
    assert first[1:] != second[1:]


def test_scenario_generator_derived_specs_apply_only_declared_delta(tmp_path: Path) -> None:
    generator = _load_scenario_generator()
    for number, spec in enumerate(generator.SCENARIO_SPECS):
        if spec.mutation is None:
            continue
        tree = generator.FixtureTree(tmp_path / str(number))
        generator._build_spec(tree, spec)
        assert tree.mutation_calls == [spec.mutation]
