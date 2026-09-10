from __future__ import annotations

import copy
import hashlib
import json
import runpy
import shutil
from pathlib import Path

import pytest

from tests.evals.event_log_contract import (
    EventLogContractError,
    TrustedRunContext,
    _manifest,
    _receipt_identity,
    validate_event_log,
)
from tests.evals.generate_scenarios import REQUIRED_IDS, rendered_scenarios
import tests.evals.generate_scenarios as scenario_generator
from tests.evals.scenario_contract import ScenarioContractError, validate_against_schema


ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "tests/evals/scenarios"


def _schema(name: str) -> dict[str, object]:
    return json.loads((ROOT / "tests/evals" / name).read_text(encoding="utf-8"))


def _scenarios() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCENARIO_DIR.glob("*.json"))
    ]


def test_generated_question_fixtures_are_canonical_v2(tmp_path):
    from brainlib.wiki_models import parse_question

    scenario_generator._build_fixture(tmp_path, "contradictory-evidence")
    question = tmp_path / "contradictory-evidence/repo/wiki/questions/what-is-alpha.md"
    record = parse_question(question, text=question.read_text())
    assert record.question_id == "question-what-is-alpha"
    assert record.interpretation.decision == "not_applicable"


def test_contradictory_policy_names_the_exact_sorted_fixture_triples():
    scenario = next(item for item in scenario_generator.SCENARIOS if item["id"] == "contradictory-evidence")
    policy = scenario["interpretation_policy"]
    ledger = ROOT / "tests/evals/fixtures/contradictory-evidence/repo/sources/ledger"
    expected = []
    for path in ledger.glob("src_*.json"):
        record = json.loads(path.read_bytes())
        expected.append({"source_id": record["source_id"], "content_sha256": record["active_content_sha256"],
                         "derivation_id": record["active_derivation_id"]})
    assert policy["claim_identities"] == sorted(expected, key=lambda item: tuple(item.values()))
    assert policy["question_id"] == "question-what-is-alpha"
    assert policy["expected_decision"] == "unresolved"
    assert policy["expected_approval_decision"] == "withheld"
    scenario_generator.validate_interpretation_policy(scenario)


@pytest.mark.parametrize("attack", ["missing", "other", "unsorted", "duplicate", "incomplete", "path", "question_id", "preserve", "decision", "approval", "triple", "extra"])
def test_generated_interpretation_policy_rejects_noncanonical_inputs(attack):
    scenario = copy.deepcopy(next(item for item in scenario_generator.SCENARIOS if item["id"] == "contradictory-evidence"))
    policy = scenario["interpretation_policy"]
    if attack == "missing":
        del scenario["interpretation_policy"]
    elif attack == "other":
        scenario["id"] = "current-wiki-fast-path"
    elif attack == "unsorted":
        policy["claim_identities"].reverse()
    elif attack == "duplicate":
        policy["claim_identities"][1] = dict(policy["claim_identities"][0])
    elif attack == "incomplete":
        del policy["claim_identities"][0]["derivation_id"]
    elif attack == "triple":
        policy["claim_identities"][0]["content_sha256"] = "f" * 64
    else:
        field, value = {"path": ("question_path", "wiki/questions/other.md"), "question_id": ("question_id", "other"),
                        "preserve": ("preserve_both", False), "decision": ("expected_decision", "preferred"),
                        "approval": ("expected_approval_decision", "approved"), "extra": ("prose_authority", "prefer")} [attack]
        policy[field] = value
    with pytest.raises(ScenarioContractError):
        scenario_generator.validate_interpretation_policy(scenario)


def test_policy_rejects_a_changed_fixture_ledger_identity(tmp_path):
    scenario_generator._build_fixture(tmp_path, "contradictory-evidence")
    repo = tmp_path / "contradictory-evidence/repo"
    ledger = next((repo / "sources/ledger").glob("src_*.json"))
    record = json.loads(ledger.read_bytes())
    record["active_content_sha256"] = "f" * 64
    ledger.write_text(json.dumps(record))
    scenario = next(item for item in scenario_generator.SCENARIOS if item["id"] == "contradictory-evidence")
    with pytest.raises(ScenarioContractError):
        scenario_generator.validate_interpretation_policy(scenario, fixture_repo=repo)


def test_generator_bootstraps_without_existing_generated_fixtures(tmp_path):
    evaluator = tmp_path / "tests/evals"
    evaluator.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    shutil.copy2(ROOT / "config/extractors.toml", tmp_path / "config/extractors.toml")
    for name in ("generate_scenarios.py", "scenario.v1.schema.json"):
        shutil.copy2(ROOT / "tests/evals" / name, evaluator / name)
    assert not (evaluator / "fixtures").exists()
    generator = runpy.run_path(str(evaluator / "generate_scenarios.py"))
    generator["_render"](evaluator / "generated")
    scenario = json.loads((evaluator / "generated/scenarios/contradictory-evidence.json").read_bytes())
    assert scenario["interpretation_policy"]["question_id"] == "question-what-is-alpha"
    assert len(scenario["interpretation_policy"]["claim_identities"]) == 2


def test_all_required_scenarios_are_versioned_and_exact_generator_output() -> None:
    scenarios = _scenarios()
    assert {item["id"] for item in scenarios} == REQUIRED_IDS
    for item in scenarios:
        validate_against_schema(item, _schema("scenario.v1.schema.json"))
    actual = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(SCENARIO_DIR.glob("*.json"))
    }
    assert actual == rendered_scenarios()


def test_scenario_schema_rejects_boolean_integer_extra_keys_bad_ids_and_duplicate_events() -> (
    None
):
    base = _scenarios()[0]
    schema = _schema("scenario.v1.schema.json")
    invalid = (
        {**base, "schema_version": True},
        {**base, "id": "Bad ID"},
        {**base, "unexpected": True},
        {**base, "required_events": ["same", "same"]},
    )
    for value in invalid:
        with pytest.raises(ScenarioContractError):
            validate_against_schema(value, schema)


def test_event_log_schema_rejects_generic_event_payloads_and_terminal_newlines() -> None:
    """v1 logs are references to host records, never self-reported evidence."""
    schema = _schema("event-log.v1.schema.json")
    event_data = schema["properties"]["events"]["items"]["properties"]["data"]
    with pytest.raises(ScenarioContractError):
        validate_against_schema({"invented_receipt": "pass"}, event_data)
    for key in ("run_id", "fixture_sha256"):
        with pytest.raises(ScenarioContractError):
            validate_against_schema(
                "a" * 64 + "\n", schema["properties"][key]
            )


def test_receipt_parser_rejects_reviewers_nonproduction_reference_and_missing_consumption_fields() -> None:
    """Regression for PASS_WITH_INVALID_SYNC_RESULT_REFERENCE... from round 3."""
    with pytest.raises(EventLogContractError):
        _manifest({"data": {"result_manifest": {"result_id": "not-a-sync-id", "path": True, "sha256": "a" * 64, "corpus_revision": "b" * 64, "event_counts": {"invented_count": 0}}}})
    digest = "a" * 64
    reference = {"result_id": f"sync_{digest}", "path": f".brain/sync-results/sync_{digest}.jsonl", "sha256": digest, "corpus_revision": "b" * 64, "event_counts": {"hashed_path": 0, "new_active_representation": 0, "citation_rewrite": 0, "handoff_source_id": 0, "coverage_gap": 0}}
    verify = {"data": {"result_manifest": reference}}
    with pytest.raises(EventLogContractError):
        _receipt_identity(verify, {"data": {"result_id": reference["result_id"]}}, {"data": {"result_id": reference["result_id"]}})


def test_scenarios_encode_receipt_delivery_and_activation_boundaries() -> None:
    by_id = {item["id"]: item for item in _scenarios()}
    for item in by_id.values():
        events = item["required_events"]
        for operation in item["receipt_operations"]:
            lifecycle = [
                f"{operation}_receipt_verified",
                f"{operation}_receipt_consumed",
                f"{operation}_receipt_durable",
                f"{operation}_receipt_acknowledged",
            ]
            assert [events.index(name) for name in lifecycle] == sorted(
                events.index(name) for name in lifecycle
            )
    empty = by_id["empty-wiki-first-question"]["required_events"]
    assert (
        "build_wiki_evidence_packet"
        in by_id["empty-wiki-first-question"]["forbidden_events"]
    )
    assert (
        empty.index("wiki_search_drained")
        < empty.index("wiki_no_supported_evidence")
        < empty.index("judge_insufficient")
    )
    binary = by_id["new-binary-before-question"]["required_events"]
    assert binary.index("initial_sync_receipt_acknowledged") < binary.index(
        "branch_extraction_handoff"
    )
    assert binary.index("register_extraction_handoff") < binary.index(
        "verify_registration_active_representation"
    )
    web = by_id["web-approval-and-capture"]["required_events"]
    assert web.index("ask_web_approval") < web.index("public_web_access")
    assert web.index("rendered_snapshot_receipt_acknowledged") < web.index(
        "verify_snapshot_active_representation"
    )
    assert (
        "register_rendered_handoff"
        in by_id["web-approval-and-capture"]["forbidden_events"]
    )


def test_fixture_manifests_are_complete_tree_hashes_and_keep_brain_outside_overlay() -> (
    None
):
    for scenario in _scenarios():
        fixture = ROOT / "tests/evals/fixtures" / str(scenario["fixture_id"])
        manifest = json.loads(
            (fixture / "fixture-manifest.json").read_text(encoding="utf-8")
        )
        assert set(manifest) == {
            "schema_version",
            "scenario_id",
            "tree_sha256",
            "paths",
        }
        assert manifest["scenario_id"] == scenario["id"]
        assert manifest["paths"] == sorted(manifest["paths"])
        digest = hashlib.sha256()
        for relative in manifest["paths"]:
            assert relative.startswith("repo/")
            assert not relative.startswith("repo/.brain/")
            data = (fixture / relative).read_bytes()
            digest.update(relative.removeprefix("repo/").encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
            digest.update(b"\0")
        assert digest.hexdigest() == manifest["tree_sha256"]


def test_web_fixture_has_cited_prior_question_for_real_prepublication_link_search():
    repo = ROOT / "tests/evals/fixtures/web-approval-and-capture/repo"
    question = repo / "wiki/questions/external-standard.md"
    assert question.is_file(), "fixture promises prior-standard wiki evidence"
    body = question.read_text(encoding="utf-8")
    assert "id: question-external-standard\n" in body
    assert "The prior standard limit is 10.[^standard-1]" in body
    assert "prior-standard.txt" in body
    assert "limit 12" not in body
    scenario_generator._validated(repo)


def test_real_cli_can_start_link_candidates_for_canonical_absent_first_question(tmp_path):
    import io
    from brainlib.cli import main
    from tests.evals.test_workflow_acceptance import Workflow

    flow = Workflow(tmp_path, "empty-wiki-first-question")
    question = "wiki/questions/alpha.md"
    assert not (flow.workspace / question).exists()
    output = io.StringIO()
    code = main(["--json", "links", "candidates", question, "--term", "Alpha"],
                cwd=flow.workspace, stdout=output, stderr=io.StringIO())
    assert code == 0, output.getvalue()
    assert json.loads(output.getvalue())["data"]["complete"] is True
    assert not (flow.workspace / question).exists()


def _write_evidence(root: Path, artifact_id: str, payload: bytes) -> dict[str, str]:
    target = root / artifact_id
    target.write_bytes(payload)
    return {"artifact_id": artifact_id, "sha256": hashlib.sha256(payload).hexdigest()}


def _seal_control_run(control_root: Path, run_id: str, log: dict[str, object]) -> None:
    run_root = control_root / "runs" / run_id
    entries = [
        {
            "id": path.name,
            "relative_path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": len(path.read_bytes()),
        }
        for path in sorted(run_root.iterdir())
        if path.is_file()
        and path.name not in {"evidence-index.json", "log-attestation.json"}
    ]
    index = json.dumps(
        {"schema_version": 1, "run_id": run_id, "entries": entries}, sort_keys=True
    ).encode()
    (run_root / "evidence-index.json").write_bytes(index)
    log["evidence_index_sha256"] = hashlib.sha256(index).hexdigest()
    digest_payload = dict(log)
    digest_payload.pop("log_attestation_sha256", None)
    log_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attestation = json.dumps(
        {
            "schema_version": 1,
            "run_id": run_id,
            "client": log["client"],
            "scenario_id": log["scenario_id"],
            "fixture_sha256": log["fixture_sha256"],
            "evidence_index_sha256": log["evidence_index_sha256"],
            "log_sha256": log_digest,
        },
        sort_keys=True,
    ).encode()
    (run_root / "log-attestation.json").write_bytes(attestation)
    log["log_attestation_sha256"] = hashlib.sha256(attestation).hexdigest()


def test_event_log_rejects_forged_complete_looking_workspace_strings(
    tmp_path: Path,
) -> None:
    scenario = next(
        item for item in _scenarios() if item["id"] == "empty-wiki-first-question"
    )
    fixture = json.loads(
        (
            ROOT
            / "tests/evals/fixtures"
            / str(scenario["fixture_id"])
            / "fixture-manifest.json"
        ).read_text(encoding="utf-8")
    )
    names = list(scenario["required_events"])
    log = {
        "schema_version": 1,
        "run_id": "0" * 64,
        "scenario_id": scenario["id"],
        "client": "codex",
        "client_version": "test",
        "fixture_sha256": fixture["tree_sha256"],
        "network_mode": scenario["network_mode"],
        "evidence_index_sha256": "a" * 64,
        "log_attestation_sha256": "a" * 64,
        "policy_proof": {"artifact_id": "forged-policy.json", "sha256": "a" * 64},
        "executions": [
            {
                "kind": "actual_client_process",
                "process": {"artifact_id": "forged-process.json", "sha256": "a" * 64},
                "transcript": {
                    "artifact_id": "forged-transcript.json",
                    "sha256": "a" * 64,
                },
            }
        ],
        "events": [
            {
                "sequence": index,
                "name": name,
                "evidence_kind": "command",
                "evidence": [
                    {"artifact_id": f"forged-{index}.json", "sha256": "a" * 64}
                ],
                "data": {},
            }
            for index, name in enumerate(names, 1)
        ],
        "repository_assertions": [
            {
                "text": text,
                "passed": True,
                "evidence": [{"artifact_id": "forged-diff.json", "sha256": "a" * 64}],
            }
            for text in scenario["repository_assertions"]
        ],
        "result": "pass",
        "incomplete_reasons": [],
    }
    context = TrustedRunContext(tmp_path, ROOT)
    with pytest.raises(EventLogContractError):
        validate_event_log(
            log,
            _schema("event-log.v1.schema.json"),
            scenario,
            fixture["tree_sha256"],
            context,
        )


def test_legacy_generic_evidence_shape_is_rejected_even_from_external_root(
    tmp_path: Path,
) -> None:
    scenario = next(
        item
        for item in _scenarios()
        if item["id"] == "repository-development-not-archived"
    )
    fixture = json.loads(
        (
            ROOT
            / "tests/evals/fixtures"
            / str(scenario["fixture_id"])
            / "fixture-manifest.json"
        ).read_text(encoding="utf-8")
    )
    run_id = "1" * 64
    evidence_root = tmp_path / "runs" / run_id
    evidence_root.mkdir(parents=True)
    argv = [
        "codex",
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-C",
        "/runner/fixture-workspace",
        "-",
    ]
    policy = _write_evidence(
        evidence_root,
        "policy.json",
        json.dumps(
            {
                "schema_version": 1,
                "client": "codex",
                "argv": argv,
                "policy_profile": "codex-workspace-egress-denied",
                "network_attestation": "workspace-egress-denied",
            },
            sort_keys=True,
        ).encode(),
    )
    transcript_bytes = b"actual client transcript\n"
    transcript = _write_evidence(evidence_root, "transcript.ndjson", transcript_bytes)
    process = _write_evidence(
        evidence_root,
        "process.json",
        json.dumps(
            {
                "schema_version": 1,
                "client": "codex",
                "client_version": "trusted-test",
                "argv": argv,
                "exit_code": 0,
                "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
            },
            sort_keys=True,
        ).encode(),
    )
    events = []
    for sequence, name in enumerate(scenario["required_events"], 1):
        proof = _write_evidence(
            evidence_root,
            f"event-{sequence}.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "event_name": name,
                    "data": {},
                    "kind": "client_trace",
                },
                sort_keys=True,
            ).encode(),
        )
        events.append(
            {
                "sequence": sequence,
                "name": name,
                "evidence_kind": "client_trace",
                "evidence": [proof],
                "data": {},
            }
        )
    diff = _write_evidence(evidence_root, "diff.json", b"clean wiki diff\n")
    log = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario_id": scenario["id"],
        "client": "codex",
        "client_version": "trusted-test",
        "fixture_sha256": fixture["tree_sha256"],
        "network_mode": "disabled",
        "evidence_index_sha256": "",
        "log_attestation_sha256": "",
        "policy_proof": policy,
        "executions": [
            {
                "kind": "actual_client_process",
                "process": process,
                "transcript": transcript,
            }
        ],
        "events": events,
        "repository_assertions": [
            {"text": text, "passed": True, "evidence": [diff]}
            for text in scenario["repository_assertions"]
        ],
        "result": "pass",
        "incomplete_reasons": [],
    }
    _seal_control_run(tmp_path, run_id, log)
    context = TrustedRunContext(tmp_path, ROOT)
    with pytest.raises(EventLogContractError):
        validate_event_log(
            log,
            _schema("event-log.v1.schema.json"),
            scenario,
            fixture["tree_sha256"],
            context,
        )


def test_binary_fixture_runs_full_source_validation_and_allows_only_its_documented_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ROOT / "tests/evals/fixtures/new-binary-before-question/repo"
    original = scenario_generator.validate_source_ledger
    observed: list[object] = []

    def spy(*args: object, **kwargs: object) -> object:
        observed.append(kwargs.get("full"))
        return original(*args, **kwargs)

    monkeypatch.setattr(scenario_generator, "validate_source_ledger", spy)
    scenario_generator._validated(repo, allow_unledgered_pdf=True)
    assert observed == [True]


def test_closed_host_index_marker_command_and_diff_can_prove_a_minimal_pass(tmp_path: Path) -> None:
    from tests.evals.test_event_evidence import test_closed_marker_diff_minimal_pass

    test_closed_marker_diff_minimal_pass(tmp_path)
