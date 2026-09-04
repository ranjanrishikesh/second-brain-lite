import json
import subprocess

import pytest

from tests.helpers_knowledge import run_search_cli


@pytest.mark.parametrize(
    "arguments",
    [
        ("--scope", "sources", "--term", "Alpha"),
        ("--scope", "wiki"),
        ("--scope", "wiki", "--term", "Alpha", "--page-size", "0"),
        ("--cursor", "bad"),
        ("--cursor", "bad", "--context", "2"),
        ("--scope", "wiki", "--term", "Alpha", "--unknown"),
    ],
)
def test_invalid_arguments_use_canonical_json_envelope(repo_root, arguments):
    result = run_search_cli(repo_root, "--json", "search", *arguments)
    assert result.returncode == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "search" and not payload["ok"]
    assert payload["errors"][0]["code"] in {
        "invalid_arguments",
        "invalid_search_cursor",
    }


def test_real_rg_wiki_search_and_zero_hit(repo_root):
    (repo_root / "wiki/pages/alpha.md").write_text("before\nAlpha\nafter\n")
    result = run_search_cli(
        repo_root,
        "--json",
        "search",
        "--scope",
        "wiki",
        "--term",
        "Alpha",
        "--page-size",
        "1",
    )
    assert result.returncode == 0 and result.stderr == ""
    pages = [json.loads(result.stdout)["data"]]
    while pages[-1]["next_cursor"]:
        next_page = run_search_cli(
            repo_root, "--json", "search", "--cursor", pages[-1]["next_cursor"]
        )
        assert next_page.returncode == 0 and next_page.stderr == ""
        pages.append(json.loads(next_page.stdout)["data"])
    assert pages[-1]["complete"] is True
    assert [match["kind"] for page in pages for match in page["matches"]] == [
        "context",
        "match",
        "context",
    ]


def test_missing_rg_canonical_error(repo_root, monkeypatch):
    (repo_root / "wiki/pages/alpha.md").write_text("Alpha")

    def missing(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(subprocess, "Popen", missing)
    result = run_search_cli(
        repo_root, "--json", "search", "--scope", "wiki", "--term", "Alpha"
    )
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout)["errors"][0]["code"] == "rg_unavailable"


def test_real_source_fixture_continuations_reach_unusual_and_late_files(scenario_repo):
    scenario = scenario_repo("search/valid")
    result = run_search_cli(
        scenario.root,
        "--json",
        "search",
        "--scope",
        "sources",
        "--pass",
        "discovery",
        "--term",
        "Alpha",
        "--page-size",
        "1",
    )
    assert result.returncode == 0 and result.stderr == ""
    pages = [json.loads(result.stdout)["data"]]
    while pages[-1]["next_cursor"] is not None:
        result = run_search_cli(
            scenario.root, "--json", "search", "--cursor", pages[-1]["next_cursor"]
        )
        assert result.returncode == 0 and result.stderr == ""
        pages.append(json.loads(result.stdout)["data"])
    assert pages[-1]["complete"] is True
    assert all(
        len(page["matches"]) <= 1 and page["candidate_count"] == 3 for page in pages
    )
    paths = [match["path"] for page in pages for match in page["matches"]]
    assert any(path.startswith("sources/extracted/zzz-late.txt/") for path in paths)
    assert any(
        path.startswith("sources/extracted/unusual/résumé (final).md/")
        for path in paths
    )
    assert [page["page_index"] for page in pages] == list(range(len(pages)))


def test_spool_limit_is_validation_style_blocking_gap(scenario_repo):
    scenario = scenario_repo("search/valid")
    result = run_search_cli(
        scenario.root,
        "--json",
        "search",
        "--scope",
        "sources",
        "--pass",
        "discovery",
        "--term",
        "Alpha",
        "--max-run-bytes",
        "256",
    )
    assert result.returncode == 1 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert not payload["ok"] and not payload["data"]["complete"]
    assert payload["data"]["next_cursor"] is None
    assert payload["errors"][0]["code"] == "search_spool_limit"


def test_run_state_is_gitignored(repo_root):
    subprocess.run(
        ["git", "init", "--quiet"], cwd=repo_root, check=True, capture_output=True
    )
    result = subprocess.run(
        ["git", "check-ignore", ".brain/search-runs/probe"],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0


def test_human_output_includes_continuation_and_completion(repo_root):
    (repo_root / "wiki/pages/alpha.md").write_text("Alpha\nsecond line\n")
    result = run_search_cli(
        repo_root, "search", "--scope", "wiki", "--term", "Alpha", "--page-size", "1"
    )
    assert result.returncode == 0 and result.stderr == ""
    assert "1 candidates; 1 returned records" in result.stdout
    assert "Continue: ./brain search --cursor " in result.stdout
    cursor = result.stdout.split("--cursor ")[1].strip()
    result = run_search_cli(repo_root, "search", "--cursor", cursor)
    assert result.returncode == 0 and result.stdout.endswith("complete\n")


def test_source_positional_query_is_invalid_json(repo_root):
    result = run_search_cli(repo_root, "--json", "search", "renewable")
    assert result.returncode == 2 and result.stderr == ""
    assert json.loads(result.stdout)["errors"][0]["code"] == "invalid_arguments"
