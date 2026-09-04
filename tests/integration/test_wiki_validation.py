from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import json
from pathlib import Path

import pytest

from brainlib.contracts import compute_corpus_revision, compute_sha256
from brainlib.validation import ChecksumCache, validate_repository
from tests.helpers_extractors import run_brain
from tests.helpers_knowledge import (
    KnowledgeScenario,
    manifest_for,
    stage_wiki_write,
    write_wiki_manifest,
)


def test_brain_links_check_json_reports_broken_graph_issue(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/nonreciprocal")
    completed = run_brain(scenario.root, "--json", "links", "check")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert completed.stderr == ""
    assert payload["command"] == "links check"
    assert payload["ok"] is False
    assert "relationship_not_reciprocal" in {issue["code"] for issue in payload["data"]["report"]["issues"]}


def test_brain_links_candidates_must_resume_through_late_record(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    completed = run_brain(
        scenario.root, "--json", "links", "candidates", "wiki/pages/alpha.md",
        "--term", "Alpha", "--term", "A. Example", "--page-size", "1",
    )
    assert completed.returncode == 0 and completed.stderr == ""
    pages = [json.loads(completed.stdout)["data"]]
    while pages[-1]["next_cursor"] is not None:
        resumed = run_brain(
            scenario.root, "--json", "links", "candidates",
            "--cursor", pages[-1]["next_cursor"],
        )
        assert resumed.returncode == 0 and resumed.stderr == ""
        pages.append(json.loads(resumed.stdout)["data"])
    assert pages[-1]["complete"] is True
    assert all(len(page["candidates"]) <= 1 for page in pages)
    assert "wiki/pages/zzz-related.md" in {
        candidate["path"] for page in pages for candidate in page["candidates"]
    }
    assert all(len(page["result_sha256"]) == 64 for page in pages)


def test_brain_links_candidates_rejects_noncanonical_raw_path_spellings(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/candidates")
    spellings = (
        str(scenario.paths.wiki_pages / "alpha.md"),
        "./wiki/pages/alpha.md",
        "wiki//pages/alpha.md",
    )

    for spelling in spellings:
        completed = run_brain(
            scenario.root,
            "--json",
            "links",
            "candidates",
            spelling,
            "--term",
            "Alpha",
        )
        payload = json.loads(completed.stdout)
        assert completed.returncode == 2
        assert completed.stderr == ""
        assert payload["command"] == "links candidates"
        assert payload["errors"][0]["code"] == "invalid_arguments"


def test_brain_links_candidates_lock_cleanup_failure_exits_two(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib import search
    from brainlib.locking import LockCleanupError

    scenario = scenario_repo("graph/candidates")

    def cleanup_failure(*_args, **_kwargs):
        raise LockCleanupError("injected search-run cleanup failure")

    monkeypatch.setattr(search, "cleanup_search_runs", cleanup_failure)
    completed = run_brain(
        scenario.root,
        "--json",
        "links",
        "candidates",
        "wiki/pages/alpha.md",
        "--term",
        "Alpha",
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["errors"][0]["code"] == "links_candidates_failed"


def test_brain_validate_full_combines_ledger_citation_and_graph_checks(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "validate", "--full")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert {check for report in payload["data"]["reports"] for check in report["checks"]} >= {"source-ledger", "citations", "wiki-graph"}


def test_repository_validation_is_metadata_fast_and_full_mode_shares_one_cache(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    calls: Counter[Path] = Counter()
    cache = ChecksumCache(hash_file=lambda path: calls.update((path.resolve(),)) or compute_sha256(path))
    validate_repository(scenario.paths, scenario.ledger, full=False, checksum_cache=cache)
    assert calls == Counter()
    validate_repository(scenario.paths, scenario.ledger, full=True, checksum_cache=cache)
    assert calls and set(calls.values()) == {1}


def test_brain_wiki_apply_loads_same_run_manifest_and_only_then_publishes(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario, "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- approved staged update -->\n",
    )
    manifest_path = write_wiki_manifest(scenario, manifest_for(scenario, (change,)))
    completed = run_brain(
        scenario.root, "--json", "wiki", "apply", "--manifest",
        scenario.paths.repo_relative(manifest_path).as_posix(),
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == ""
    assert payload == {
        "command": "wiki apply",
        "ok": True,
        "data": {
            "corpus_revision": compute_corpus_revision(scenario.ledger.load_all().values()),
            "changed_paths": ["wiki/pages/alpha.md"],
            "index_path": "wiki/index.md",
            "recovered": False,
        },
        "warnings": [],
        "errors": [],
    }
    assert "approved staged update" in alpha.read_text(encoding="utf-8")


def test_brain_wiki_apply_rejects_manifest_outside_run_with_canonical_json_error(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "wiki", "apply", "--manifest", "wiki/index.md")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["command"] == "wiki apply"
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_manifest"


def test_brain_wiki_apply_catches_staging_revalidation_value_error(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib import wiki_transaction

    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- staged then changed -->\n",
    )
    manifest_path = write_wiki_manifest(scenario, manifest_for(scenario, (change,)))
    original_load = wiki_transaction.load_wiki_manifest

    def load_then_change(*args, **kwargs):
        manifest = original_load(*args, **kwargs)
        assert change.staging_path is not None
        (scenario.root / change.staging_path).write_text("changed", encoding="utf-8")
        return manifest

    monkeypatch.setattr(wiki_transaction, "load_wiki_manifest", load_then_change)
    completed = run_brain(
        scenario.root,
        "--json",
        "wiki",
        "apply",
        "--manifest",
        scenario.paths.repo_relative(manifest_path).as_posix(),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["errors"][0]["code"] == "invalid_manifest"


def test_brain_wiki_apply_execution_failure_exits_two(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib import wiki_transaction

    scenario = scenario_repo("graph/valid")
    alpha = scenario.paths.wiki_pages / "alpha.md"
    change = stage_wiki_write(
        scenario,
        "wiki/pages/alpha.md",
        alpha.read_text(encoding="utf-8") + "\n<!-- staging retained -->\n",
    )
    manifest_path = write_wiki_manifest(scenario, manifest_for(scenario, (change,)))

    def fail_apply(*_args, **_kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(wiki_transaction, "apply_wiki_manifest", fail_apply)
    completed = run_brain(
        scenario.root,
        "--json",
        "wiki",
        "apply",
        "--manifest",
        scenario.paths.repo_relative(manifest_path).as_posix(),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["errors"][0]["code"] == "wiki_transaction_failed"


def test_brain_wiki_recover_is_structured_and_idempotent(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    completed = run_brain(scenario.root, "--json", "wiki", "recover")
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["data"] == {"recovered": False, "restored_paths": []}


def test_brain_links_check_execution_failure_exits_two(
    scenario_repo: Callable[[str], KnowledgeScenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    from brainlib import graph

    scenario = scenario_repo("graph/valid")

    def fail_check(*_args, **_kwargs):
        raise OSError("injected graph reader failure")

    monkeypatch.setattr(graph, "validate_graph", fail_check)
    completed = run_brain(scenario.root, "--json", "links", "check")

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert payload["errors"][0]["code"] == "links_check_failed"


def test_validate_reports_pending_transaction_without_mutating_it(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("graph/valid")
    journal = scenario.paths.root / ".brain/wiki-transaction.json"
    journal.parent.mkdir(exist_ok=True)
    journal.write_text("{}", encoding="utf-8")
    completed = run_brain(scenario.root, "--json", "validate")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert "wiki_transaction_pending" in {
        issue["code"] for report in payload["data"]["reports"] for issue in report["issues"]
    }
    assert journal.read_text(encoding="utf-8") == "{}"


def test_validate_preserves_reports_for_unsafe_wiki_document(
    scenario_repo: Callable[[str], KnowledgeScenario], tmp_path: Path
) -> None:
    scenario = scenario_repo("graph/valid")
    external = tmp_path / "outside.md"
    external.write_text("outside bytes must never become wiki text", encoding="utf-8")
    target = scenario.paths.wiki_pages / "alpha.md"
    target.unlink()
    target.symlink_to(external)

    completed = run_brain(scenario.root, "--json", "validate")
    payload = json.loads(completed.stdout)
    reports = payload["data"]["reports"]

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert reports[0]["checks"] == ["template-layout"]
    assert "source-ledger" in reports[1]["checks"]
    assert "wiki-transaction" in reports[2]["checks"]
    assert "wiki_documents_invalid" in {issue["code"] for issue in reports[3]["issues"]}
    assert external.read_text(encoding="utf-8") == "outside bytes must never become wiki text"


def test_validate_returns_reports_when_the_retained_ledger_is_malformed(
    scenario_repo: Callable[[str], KnowledgeScenario]
) -> None:
    scenario = scenario_repo("citations/current")
    source_id = scenario.ledger.active_representations()[0].source_id
    (scenario.paths.ledger_dir / f"{source_id}.json").write_text("{", encoding="utf-8")

    completed = run_brain(scenario.root, "--json", "validate")
    payload = json.loads(completed.stdout)
    reports = payload["data"]["reports"]

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert reports[0]["checks"] == ["template-layout"]
    assert reports[1]["checks"] == ["source-ledger"]
    assert reports[1]["corpus_revision"] is None
    assert reports[1]["issues"][0]["code"] == "ledger_records_invalid"
