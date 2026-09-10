"""Full production workflows with declared contract-test process captures.

These exercise the public evidence validator; they are not live client runs.
Product commands and archive bytes are real, confined to temporary fixtures.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from pathlib import Path

import pytest

from brainlib.citations import encode_markdown_path, parse_citation_definitions
from brainlib.cli import main
from brainlib.commands import CommandServices
from brainlib.contracts import compute_corpus_revision
from brainlib.extractors.processor import DeterministicSourceProcessor
from brainlib.layout import RepoPaths
from brainlib.ledger import LedgerStore, representation_for
from brainlib.sources.web import NetworkCapture
from brainlib.sync import _representation_data
from tests.evals import event_log_contract as contract
from tests.evals.generate_scenarios import SCENARIOS, _build_fixture
from tests.evals.test_event_evidence import CapturedEvidence, add_event, encoded, seal_run
from tests.evals.test_semantic_evidence import capture_source_state
from tests.helpers_extractors import FIXED_NOW, FailIfCalled, web_test_resolve

ROOT = Path(__file__).resolve().parents[2]


def services(transport=None):
    def digest(extractor):
        resolved = web_test_resolve(extractor)
        return resolved.prerequisite_digest if resolved else hashlib.sha256(
            f"converter-v1\0unavailable\0{extractor.extractor_id}".encode()).hexdigest()

    def forbidden_transport():
        raise AssertionError("unexpected transport construction")

    return CommandServices(lambda: DeterministicSourceProcessor(resolve=web_test_resolve, run=FailIfCalled()),
                           digest, transport or forbidden_transport)


class Workflow:
    def __init__(self, tmp_path, scenario_id):
        self.root = tmp_path
        _build_fixture(tmp_path / "fixtures", scenario_id)
        self.workspace = tmp_path / "fixtures" / scenario_id / "repo"
        for name in ("AGENTS.md", "BRAIN.md", "pyproject.toml", "brain"):
            shutil.copy2(ROOT / name, self.workspace / name)
        (self.workspace / "CLAUDE.md").symlink_to("AGENTS.md")
        for name in (".agents", ".claude", ".codex", "docs"):
            shutil.copytree(ROOT / name, self.workspace / name, symlinks=True)
        self.scenario = next(item for item in SCENARIOS if item["id"] == scenario_id)
        self.evidence = CapturedEvidence()
        self.evidence.workspace = self.workspace
        self.events, self.receipts, self.deliveries = [], [], []
        self.phase = "main"
        self.before = self.inventory("initial", self.archive_paths())
        self.link_proofs = []
        self.command_results = {}

    @property
    def ledger(self):
        return LedgerStore(RepoPaths.discover(self.workspace))

    def archive_paths(self):
        return sorted(path.relative_to(self.workspace).as_posix()
                      for root in (self.workspace / "sources", self.workspace / "wiki")
                      for path in root.rglob("*") if path.is_file())

    def write(self, logical, body):
        target = self.workspace / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else body.encode())

    def inventory(self, prefix, paths):
        result = []
        for index, logical in enumerate(sorted(set(paths))):
            target = self.workspace / logical
            if not target.is_file():
                continue
            raw = target.read_bytes()
            content_id = self.evidence.add(f"{prefix}-{index}", "file_capture", raw)
            result.append({"path": logical, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "content_id": content_id})
        return result

    def diff(self, name, before, after):
        old = {item["path"]: item["sha256"] for item in before}
        new = {item["path"]: item["sha256"] for item in after}
        changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
        self.evidence.add(name, "diff", {"run_id": self.evidence.run_id, "execution_id": self.phase,
                                        "before": before, "after": after, "changed_paths": changed, "staged_paths": []})

    def state(self, command_id, result_id=None):
        state_id = capture_source_state(self.evidence, self.workspace, command_id, result_id=result_id)
        value = self.evidence.obj(state_id, "source_state")
        value["execution_id"] = self.phase
        self.evidence.add(state_id, "source_state", value)

    def record(self, command_id, args, result, code=0):
        self.evidence.command(command_id, ["./brain", "--json", *args], result, exit_code=code)
        observation = self.evidence.obj(command_id, "command_observation")
        observation["execution_id"] = self.phase
        self.evidence.add(command_id, "command_observation", observation)
        self.command_results[command_id] = result
        return result

    def invoke(self, command_id, args, *, expected=0, service=None):
        output, error = io.StringIO(), io.StringIO()
        code = main(["--json", *args], cwd=self.workspace, stdout=output, stderr=error, services=service or services())
        assert code == expected, output.getvalue() + error.getvalue()
        result = self.record(command_id, args, json.loads(output.getvalue()), code)
        if "result_manifest" in result["data"]:
            self.state(command_id, result["data"]["result_manifest"]["result_id"])
        elif args[:2] in (["source", "consume-sync-result"], ["source", "register-extraction"], ["wiki", "apply"]):
            self.state(command_id, result["data"].get("result_id"))
        return result

    def event(self, name, command_id=None, **references):
        data = references.pop("data", None)
        rule = contract.EVENT_RULES[name]
        add_event(self.evidence, self.events, name, rule.mode, command_id, references, data)
        event = self.events[-1]
        event["execution_id"] = self.phase
        record = self.evidence.obj(event["event_record_id"], "event_record")
        record["execution_id"] = self.phase
        self.evidence.add(event["event_record_id"], "event_record", record)

    def receipt(self, operation, command_id, result):
        ref = result["data"]["result_manifest"]
        receipt_id = "receipt-" + operation
        self.event(operation + "_receipt_verified", command_id, data={"receipt_id": receipt_id})
        consume_id, durable_id, ack_id = (operation + suffix for suffix in ("-consume", "-durable", "-ack"))
        consumed = self.invoke(consume_id, ["source", "consume-sync-result", "--result-id", ref["result_id"]])
        self.event(operation + "_receipt_consumed", consume_id, data={"receipt_id": receipt_id})
        durable = self.invoke(durable_id, ["source", "consume-sync-result", "--result-id", ref["result_id"]])
        assert durable["data"]["status"] == "already_consumed"
        self.event(operation + "_receipt_durable", durable_id, data={"receipt_id": receipt_id})
        stream_id = self.evidence.add(operation + "-stream", "sync_stream", (self.workspace / ref["path"]).read_bytes())
        receipt_artifact = self.evidence.add(operation + "-receipt", "consumption_receipt", (self.workspace / ".brain/sync-results" / f"consumed_{ref['result_id']}.json").read_bytes())
        delivery_id, items = None, []
        if consumed["data"]["handoff_delivery"] is not None:
            delivery_id = "delivery-" + operation
            raw = (self.workspace / consumed["data"]["handoff_delivery"]["path"]).read_bytes()
            self.evidence.add(delivery_id, "delivery", raw)
            items = json.loads(raw)["items"]
            projected = [{"item_id": item["handoff_id"], "kind": item["kind"], "handoff_id": item["handoff_id"],
                          "handoff_source_id": item["source_id"], "payload_sha256": hashlib.sha256(encoded(item)).hexdigest()} for item in items]
            self.deliveries.append({"id": delivery_id, "receipt_id": receipt_id, "delivery_artifact_id": delivery_id, "items": projected})
            self.event(operation + "_handoff_delivery_verified", data={"delivery_id": delivery_id,
                       "item_mappings": [{"item_id": item["item_id"], "handoff_source_id": item["handoff_source_id"]} for item in projected]})
        self.invoke(ack_id, ["source", "acknowledge-sync-result", "--result-id", ref["result_id"]])
        self.event(operation + "_receipt_acknowledged", ack_id, data={"receipt_id": receipt_id})
        self.receipts.append({"id": receipt_id, "operation": operation, "verify_command_id": command_id,
                              "consume_command_id": consume_id, "durable_command_id": durable_id,
                              "acknowledge_command_id": ack_id, "stream_artifact_id": stream_id,
                              "durable_artifact_id": receipt_artifact, "delivery_id": delivery_id})
        self.revision = ref["corpus_revision"]
        return items

    def branch(self, name, item, operation):
        self.event(name, data={"delivery_id": "delivery-" + operation, "item_id": item["handoff_id"],
                              "handoff_id": item["handoff_id"], "handoff_source_id": item["source_id"]})

    def search(self, family, args):
        cid = "search-" + family
        result = self.invoke(cid, args)["data"]
        assert result["complete"] is True, result
        proof_id = self.evidence.add(family + "-proof", "cursor_proof", {"run_id": self.evidence.run_id,
                            "execution_id": self.phase, "family": family, "command_ids": [cid]})
        if family.startswith("source_pass_") or family == "freshness_search_batched":
            self.event(family, cid)
        drain = "freshness_search_drained" if family == "freshness_search_batched" else family + "_drained"
        self.event(drain, cid, cursor_proof_id=proof_id)
        if family == "link_candidates":
            self.link_proofs.append({key: result[key] for key in ("run_id", "corpus_revision", "page_path", "terms", "candidate_manifest_sha256", "candidate_count")} | {"page_count": 1})
        return proof_id, result

    def passes(self, terms):
        for name, term in zip(("discovery", "expansion", "verification"), terms):
            self.search("source_pass_" + name, ["search", "--scope", "sources", "--pass", name, "--term", term, "--context", "3"])

    def capture_manifest(self, name, path, value):
        self.write(path, encoded(value))
        self.evidence.add(name + "-body", "file_capture", (self.workspace / path).read_bytes())
        self.evidence.add(name, "wiki_manifest", {"path": path, "content_id": name + "-body", "sha256": hashlib.sha256(encoded(value)).hexdigest()})

    def manifest(self, changes=()):
        return {"schema_version": 1, "expected_corpus_revision": self.revision, "change_intent": "routine", "approval_event_id": None,
                "citation_rewrites": [], "link_candidate_runs": self.link_proofs, "changes": list(changes)}

    def reconcile(self):
        path = ".brain/wiki-staging/wstg_11111111111111111111111111111111/manifest.json"
        self.capture_manifest("reconcile-manifest", path, self.manifest())
        self.invoke("reconcile", ["wiki", "apply", "--manifest", path])
        self.event("wiki_reconcile_citations", "reconcile", manifest_id="reconcile-manifest")

    def publish(self, question, body, *, term, event="wiki_apply"):
        staging_root = ".brain/wiki-staging/wstg_22222222222222222222222222222222"
        staging = staging_root + "/files/" + question
        self.write(staging, body)
        if event == "wiki_apply":
            self.diff("question-stage", [], self.inventory("question-stage", [staging]))
            self.event("write_wiki_question", diff_id="question-stage")
        self.search("link_candidates", ["links", "candidates", question, "--term", term])
        changes = [{"operation": "write", "path": question, "staging_path": staging, "sha256": hashlib.sha256(body).hexdigest()}]
        self.capture_manifest("apply-manifest", staging_root + "/manifest.json", self.manifest(changes))
        before = self.inventory("apply-before", [question, staging, "wiki/index.md"])
        self.invoke("apply", ["wiki", "apply", "--manifest", staging_root + "/manifest.json"])
        after = self.inventory("apply-after", [question, staging, "wiki/index.md"])
        assert (self.workspace / question).read_bytes() == body
        self.diff("apply-diff", before, after)
        self.event(event, "apply", manifest_id="apply-manifest", diff_id="apply-diff")

    def validate(self):
        assert [event["name"] for event in self.events] == self.scenario["required_events"]
        after = self.inventory("final", self.archive_paths())
        if self.scenario["id"] == "contradictory-evidence":
            # The retained final capture is the writer's exact promoted capture.
            applied = {entry["path"]: entry for entry in self.evidence.obj("apply-diff", "diff")["after"]}
            after = [applied[entry["path"]] if entry["path"] in applied and applied[entry["path"]]["sha256"] == entry["sha256"] else entry for entry in after]
        self.diff("final-diff", self.before, after)
        args = seal_run(self.root, self.evidence, self.scenario, self.events, self.receipts, self.deliveries)
        contract.validate_event_log(args[0], args[1], self.scenario, args[2], args[3])
        return args


def citation(workflow, question, representation, name):
    doc = workflow.workspace / question
    original = encode_markdown_path(doc, workflow.workspace / "sources/raw" / representation["raw_path"])
    extracted = encode_markdown_path(doc, workflow.workspace / representation["extracted_path"])
    anchor = representation["anchors"][0]
    return (f"[^{name}]: source_id: `{representation['source_id']}`; content_sha256: `{representation['content_sha256']}`; "
            f"derivation_id: `{representation['derivation_id']}`; anchor: `{anchor['kind']}:{anchor['value']}`; "
            f"[original]({original}); [extracted]({extracted}#{anchor['kind']}:{anchor['value']})\n")


def build_binary_workflow(tmp_path):
    flow = Workflow(tmp_path, "new-binary-before-question")
    result = flow.invoke("sync", ["sync"], expected=1)
    assert result["data"]["coverage_gap_count"] > 0
    items = flow.receipt("initial_sync", "sync", result)
    assert len(items) == 1 and items[0]["kind"] == "extraction"
    item = items[0]
    assert item["prerequisite_digest"] == hashlib.sha256(b"converter-v1\0unavailable\0pdf").hexdigest()
    flow.branch("branch_extraction_handoff", item, "initial_sync")
    staging = ".brain/agent-staging/" + item["handoff_id"] + "/quarterly.md"
    body = (ROOT / "tests/evals/fixtures/new-binary-before-question/expected-quarterly.md").read_bytes()
    flow.write(staging, body)
    flow.diff("extraction-stage", [], flow.inventory("extraction-stage", [staging]))
    flow.event("stage_handoff_scoped_extraction", diff_id="extraction-stage")
    result = flow.invoke("register", ["source", "register-extraction", "--handoff-id", item["handoff_id"], "--staging-path", staging,
                        "--anchors-json", '[{"kind":"page","value":"1"}]', "--quality-state", "ok", "--note", "Faithful quarterly PDF extraction"])
    representation = result["data"]["registration"]["active_representation"]
    assert representation["source_id"] == item["source_id"]
    flow.event("register_extraction_handoff", "register")
    flow.event("verify_registration_active_representation", "register")
    flow.receipt("post_registration_sync", "post-sync", flow.invoke("post-sync", ["sync"]))
    flow.reconcile()
    flow.search("freshness_search_batched", ["search", "--scope", "sources", "--freshness", "--source-id", item["source_id"], "--term", "Alpha"])
    proof_id, _ = flow.search("wiki_search", ["search", "--scope", "wiki", "--term", "Alpha"])
    question = "wiki/questions/what-is-alpha.md"
    old = (flow.workspace / question).read_bytes()
    doc_id = flow.evidence.add("old-question", "file_capture", old)
    records = [flow.evidence.add("packet-" + path.stem, "source_record", path.read_bytes()) for path in sorted((flow.workspace / "sources/ledger").glob("src_*.json"))]
    packet = {"run_id": flow.evidence.run_id, "execution_id": "main", "kind": "wiki", "corpus_revision": flow.revision,
              "cursor_proof_ids": [proof_id], "ledger_ids": records, "citations": [{"document_path": question, "citation_id": "alpha-1", "document_id": doc_id}],
              "complete": True, "current": True, "reason": None}
    flow.evidence.add("revalidation", "evidence_packet", packet)
    flow.event("revalidate_underlying_citations", packet_id="revalidation")
    flow.evidence.add("insufficiency", "evidence_packet", {**packet, "kind": "insufficiency", "reason": "stale"})
    flow.event("judge_insufficient", packet_id="insufficiency")
    flow.passes(("Alpha", "quarterly", "limit"))
    text = old.decode()
    text = re.sub(r"corpus_revision: [0-9a-f]{64}", "corpus_revision: " + flow.revision, text)
    text = text.replace("expansion_terms: [Beta]", "expansion_terms: [quarterly]").replace("verification_terms: [Alpha]", "verification_terms: [limit]")
    text = text.replace("## Current answer\n", "## Current answer\nThe new quarterly PDF sets the [Alpha](../pages/alpha.md) limit to 12.[^quarterly]\n\n")
    text += "\n" + citation(flow, question, representation, "quarterly")
    flow.publish(question, text.encode(), term="Alpha")
    for cid, args, event in (("links-check", ["links", "check"], "links_check"), ("validate", ["validate"], "validate")):
        flow.invoke(cid, args)
        flow.event(event, cid)
    assert not (flow.workspace / ".brain/sync-results/pending.json").exists()
    return flow


def build_contradictory_workflow(tmp_path, *, answer_suffix=""):
    flow = Workflow(tmp_path, "contradictory-evidence")
    flow.receipt("initial_sync", "sync", flow.invoke("sync", ["sync"]))
    flow.reconcile()
    proof_id, _ = flow.search("wiki_search", ["search", "--scope", "wiki", "--term", "Alpha"])
    question = "wiki/questions/what-is-alpha.md"
    old = (flow.workspace / question).read_bytes()
    doc_id = flow.evidence.add("old-question", "file_capture", old)
    records = [flow.evidence.add("packet-" + path.stem, "source_record", path.read_bytes())
               for path in sorted((flow.workspace / "sources/ledger").glob("src_*.json"))]
    packet = {"run_id": flow.evidence.run_id, "execution_id": "main", "kind": "wiki", "corpus_revision": flow.revision,
              "cursor_proof_ids": [proof_id], "ledger_ids": records,
              "citations": [{"document_path": question, "citation_id": "alpha-1", "document_id": doc_id}],
              "complete": True, "current": True, "reason": None}
    flow.evidence.add("revalidation", "evidence_packet", packet)
    flow.event("revalidate_underlying_citations", packet_id="revalidation")
    flow.evidence.add("insufficiency", "evidence_packet", {**packet, "kind": "insufficiency", "reason": "contradiction"})
    flow.event("judge_insufficient", packet_id="insufficiency")
    flow.passes(("Alpha", "limit", "2025"))
    body = old.decode().replace("answer_status: answered", "answer_status: conflicted")
    body = body.replace("interpretation_decision: not_applicable", "interpretation_decision: unresolved")
    body = body.replace("expansion_terms: [Beta]", "expansion_terms: [limit]").replace("verification_terms: [Alpha]", "verification_terms: [2025]")
    body = body.replace("## Current answer\n", "## Current answer\n2024 [Alpha](../pages/alpha.md) limit is 10.[^alpha-1] 2025 Alpha limit is 12.[^alpha-2]\n" + answer_suffix)
    body = body.replace("## Contradictory evidence\nNone.", "## Contradictory evidence\n2024 [Alpha](../pages/alpha.md) limit is 10.[^alpha-1] 2025 Alpha limit is 12.[^alpha-2]")
    newer = next(record for record in flow.ledger.load_all().values() if record.current_raw_path.as_posix() == "alpha-2025.txt")
    representation = _representation_data(representation_for(newer, newer.active_content_sha256, newer.active_derivation_id))
    body += "\n" + citation(flow, question, representation, "alpha-2")
    staging = ".brain/wiki-staging/wstg_22222222222222222222222222222222/files/" + question
    flow.write(staging, body)
    flow.diff("claims-stage", [], flow.inventory("claims-stage", [staging]))
    flow.event("preserve_both_claims", diff_id="claims-stage")
    flow.event("cite_both_sides", diff_id="claims-stage")
    approval = {"event_id": "interpretation-withheld", "scope": "Alpha limits", "note": "Preserve both sourced limits", "decision": "withheld"}
    flow.evidence.manifest_overrides = {"approval": approval}
    flow.evidence.add("interpretation-approval", "approval", {"run_id": flow.evidence.run_id, "execution_id": "main", **approval,
                      "fixture_id": None, "capability_id": None, "manifest_id": "apply-manifest"})
    flow.event("ask_interpretation_approval", approval_id="interpretation-approval")
    flow.publish(question, body.encode(), term="Alpha")
    for cid, args, name in (("links-check", ["links", "check"], "links_check"), ("validate", ["validate"], "validate")):
        flow.invoke(cid, args)
        flow.event(name, cid)
    return flow


def build_web_workflow(tmp_path):
    flow = Workflow(tmp_path, "web-approval-and-capture")
    assets = ROOT / "tests/evals/fixtures/web-approval-and-capture"
    fixture_hash = json.loads((assets / "fixture-manifest.json").read_bytes())["tree_sha256"]
    approval = {"event_id": "approval-standard", "scope": "one controlled standard fixture",
                "note": "Approve static and faithful rendered fixture capture", "decision": "approved"}
    flow.evidence.manifest_overrides = {"approval": approval, "fixture_capability_id": "capability"}
    descriptor = {"fixture_id": "web-approval-and-capture", "fixture_sha256": fixture_hash,
                  "static_sha256": hashlib.sha256((assets / "static-shell.html").read_bytes()).hexdigest(),
                  "rendered_sha256": hashlib.sha256((assets / "rendered-dom.html").read_bytes()).hexdigest(),
                  "unused_sha256": hashlib.sha256((assets / "unused-candidate.html").read_bytes()).hexdigest(),
                  "final_url": "https://example.test/standard", "media_type": "text/html",
                  "retrieved_at": "2026-09-04T12:00:00Z", "redirect_urls": []}
    flow.evidence.add("descriptor", "fixture_descriptor", descriptor)
    flow.evidence.add("capability", "fixture_capability", {"run_id": flow.evidence.run_id, "execution_id": "approved_capture",
                      "phase": "approved_capture", "fixture_id": "web-approval-and-capture", "fixture_sha256": fixture_hash,
                      "capability_id": "capability", "approval_event_id": approval["event_id"], "scope": approval["scope"], "note": approval["note"],
                      "enabled_fixture_ids": ["web-approval-and-capture.initial", "web-approval-and-capture.rendered"], "descriptor_id": "descriptor"})
    flow.phase = "approval"
    assert len(flow.ledger.load_all()) == 1
    assert b"12" not in (flow.workspace / "sources/raw/prior-standard.txt").read_bytes()
    flow.receipt("initial_sync", "initial-sync", flow.invoke("initial-sync", ["sync"]))
    flow.event("report_local_evidence_gap")
    flow.evidence.add("approval", "approval", {"run_id": flow.evidence.run_id, "execution_id": "approval", **approval,
                      "fixture_id": "web-approval-and-capture.initial", "capability_id": "capability", "manifest_id": None})
    flow.event("ask_web_approval", approval_id="approval")
    flow.phase = "approved_capture"
    captures = []

    class FixtureTransport:
        def capture(self, requested_url, *, paths, timeout_seconds, max_output_bytes, now):
            assert flow.phase == "approved_capture"
            assert requested_url == descriptor["final_url"]
            assert not captures, "only one static fixture capture is authorized"
            body = (assets / "static-shell.html").read_bytes()
            assert len(body) <= max_output_bytes
            staging = ".brain/web-staging/fixture-static.html"
            flow.write(staging, body)
            captures.append(requested_url)
            return NetworkCapture(paths.root / staging, FIXED_NOW, requested_url, (), "text/html", "snapshot.html")

    # Test-only fixture adapter, matching Task 7b's API. The inner workflow is
    # the real product CLI with a local byte transport, never public networking.
    initial_args = ["eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.initial",
                    "--approval-event-id", approval["event_id"], "--approval-scope", approval["scope"], "--approval-note", approval["note"]]
    output, error = io.StringIO(), io.StringIO()
    code = main(["--json", "source", "snapshot-url", "--url", descriptor["final_url"], "--description", "controlled standard fixture",
                 "--approval-event-id", approval["event_id"], "--approval-scope", approval["scope"], "--approval-note", approval["note"]],
                cwd=flow.workspace, stdout=output, stderr=error, services=services(FixtureTransport))
    assert code == 0, output.getvalue() + error.getvalue()
    initial = json.loads(output.getvalue())
    assert initial["data"]["snapshot"]["active_representation"] is None
    wrapper = {"command": "eval mock-web-capture", "ok": True, "data": {"fixture_id": "web-approval-and-capture.initial", "product_result": initial}, "warnings": [], "errors": []}
    flow.record("static", initial_args, wrapper)
    flow.state("static", initial["data"]["result_manifest"]["result_id"])
    flow.event("public_web_access", "static", approval_id="approval", capability_id="capability")
    static_path = "sources/raw/" + initial["data"]["snapshot"]["raw_path"]
    flow.diff("select-used", [], flow.inventory("select-used", [static_path]))
    flow.event("select_used_source", diff_id="select-used", capability_id="capability")
    flow.event("snapshot_used_source", "static", approval_id="approval", capability_id="capability")
    items = flow.receipt("initial_snapshot", "static", initial)
    assert len(items) == 1 and items[0]["kind"] == "rendered_web_capture"
    item = items[0]
    flow.branch("branch_rendered_web_capture_handoff", item, "initial_snapshot")
    staging = ".brain/web-staging/" + item["handoff_id"] + "/rendered.html"
    rendered_bytes = (assets / "rendered-dom.html").read_bytes()
    flow.write(staging, rendered_bytes)
    rendered_stage_result = {"command": "eval mock-web-capture", "ok": True, "data": {"fixture_id": "web-approval-and-capture.rendered",
                            "handoff_id": item["handoff_id"], "path": staging, "sha256": hashlib.sha256(rendered_bytes).hexdigest(), "bytes": len(rendered_bytes)},
                            "warnings": [], "errors": []}
    flow.record("rendered-stage", ["eval", "mock-web-capture", "--fixture-id", "web-approval-and-capture.rendered", "--handoff-id", item["handoff_id"]], rendered_stage_result)
    flow.event("stage_faithful_browser_capture", "rendered-stage", approval_id="approval", capability_id="capability")
    rendered = flow.invoke("rendered", ["source", "snapshot-url", "--source-id", item["source_id"], "--rendered-staging-path", staging,
                           "--handoff-id", item["handoff_id"], "--retrieved-at", descriptor["retrieved_at"], "--final-url", descriptor["final_url"],
                           "--detected-media-type", descriptor["media_type"], "--approval-event-id", approval["event_id"], "--approval-scope", approval["scope"], "--approval-note", approval["note"]])
    assert captures == [descriptor["final_url"]], "direct rendered capture must not construct transport"
    representation = rendered["data"]["snapshot"]["active_representation"]
    assert representation is not None and representation["content_sha256"] == descriptor["rendered_sha256"]
    flow.event("rendered_snapshot_url", "rendered")
    assert flow.receipt("rendered_snapshot", "rendered", rendered) == []
    flow.event("verify_snapshot_active_representation", "rendered")
    flow.passes(("standard", "limit", "12"))
    question = "wiki/questions/external-standard.md"
    # This is the evolving prior-standard question promised by fixture_setup.
    # Preserve its identity, existing citation, and topic links when updating.
    prior_body = (flow.workspace / question).read_text()
    prior_citations = parse_citation_definitions(prior_body, path=flow.workspace / question)
    assert prior_citations and all(item.content_sha256 != descriptor["rendered_sha256"] for item in prior_citations)
    body = re.sub(r"(?m)^corpus_revision: [0-9a-f]{64}$", "corpus_revision: " + flow.revision, prior_body)
    body = re.sub(r"(?m)^answer_status: .*?$", "answer_status: answered", body)
    for field, terms in (("discovery_terms", "[standard]"), ("expansion_terms", "[limit]"), ("verification_terms", '["12"]')):
        body = re.sub(r"(?m)^" + field + r": .*?$", field + ": " + terms, body)
    body = body.replace("\n## Supporting evidence\n", "\nThe captured current standard now requires limit 12.[^rendered-standard]\n\n## Supporting evidence\n")
    body += "\n" + citation(flow, question, representation, "rendered-standard")
    flow.publish(question, body.encode(), term="standard", event="persist_claim")
    records = flow.ledger.load_all()
    assert len(records) == 2 and sum(record.url_descriptor is not None for record in records.values()) == 1
    web_record = records[item["source_id"]]
    assert set(web_record.versions) == {descriptor["static_sha256"], descriptor["rendered_sha256"]}
    assert all(descriptor["unused_sha256"] not in record.versions for record in records.values())
    assert compute_corpus_revision(records.values()) == flow.revision
    assert not (flow.workspace / ".brain/sync-results/pending.json").exists()
    return flow


def test_binary_workflow_reaches_public_validator(tmp_path):
    workflow = build_binary_workflow(tmp_path)
    workflow.validate()


def build_empty_workflow(tmp_path):
    flow = Workflow(tmp_path, "empty-wiki-first-question")
    flow.receipt("initial_sync", "sync", flow.invoke("sync", ["sync"]))
    flow.reconcile()
    proof_id, search = flow.search("wiki_search", ["search", "--scope", "wiki", "--term", "Alpha"])
    assert search["matches"] == []
    flow.event("wiki_no_supported_evidence", cursor_proof_id=proof_id)
    records = [flow.evidence.add("packet-" + path.stem, "source_record", path.read_bytes())
               for path in sorted((flow.workspace / "sources/ledger").glob("src_*.json"))]
    packet_id = flow.evidence.add("insufficiency", "evidence_packet", {
        "run_id": flow.evidence.run_id, "execution_id": "main", "kind": "insufficiency",
        "corpus_revision": flow.revision, "cursor_proof_ids": [proof_id], "ledger_ids": records,
        "citations": [], "complete": True, "current": True, "reason": "no_supported_evidence"})
    flow.event("judge_insufficient", packet_id=packet_id)
    flow.passes(("Alpha", "Beta", "relationship"))
    question = "wiki/questions/alpha-beta.md"
    record = next(iter(flow.ledger.load_all().values()))
    representation = _representation_data(representation_for(record, record.active_content_sha256, record.active_derivation_id))
    body = f"""---
schema_version: 2
id: question-alpha-beta
title: What does Alpha say about Beta?
description: Alpha documents support for the Beta relationship.
canonical_question: What does Alpha say about Beta?
prior_phrasings: [What does Alpha say about Beta?]
answer_status: answered
interpretation_decision: not_applicable
corpus_revision: {flow.revision}
last_researched: 2026-09-04
discovery_terms: [Alpha]
expansion_terms: [Beta]
verification_terms: [relationship]
---
# What does Alpha say about Beta?

## Current answer
Alpha supports the Beta relationship.[^alpha-beta]

## Supporting evidence
The retained Alpha source explicitly supports that relationship.[^alpha-beta]

## Contradictory evidence
None in the retained source.

## Related pages

## Sources

""" + citation(flow, question, representation, "alpha-beta")
    assert not (flow.workspace / question).exists()
    flow.publish(question, body.encode(), term="Alpha")
    for cid, args, event in (("links-check", ["links", "check"], "links_check"), ("validate", ["validate"], "validate")):
        flow.invoke(cid, args)
        flow.event(event, cid)
    assert [path.name for path in (flow.workspace / "wiki/questions").glob("*.md")] == ["alpha-beta.md"]
    assert not (flow.workspace / ".brain/sync-results/pending.json").exists()
    return flow


def test_empty_wiki_first_question_reaches_public_validator_without_live_placeholder(tmp_path):
    build_empty_workflow(tmp_path).validate()


def test_two_phase_rendered_web_workflow_reaches_public_validator(tmp_path):
    workflow = build_web_workflow(tmp_path)
    workflow.validate()


def _add_unused_web_candidate_to_diff(workflow, diff_id, *, changed):
    """Model a full workspace snapshot that also retains evaluator fixture bytes."""

    raw = (ROOT / "tests/evals/fixtures/web-approval-and-capture/unused-candidate.html").read_bytes()
    candidate = {
        "path": "tests/evals/fixtures/web-approval-and-capture/unused-candidate.html",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "content_id": workflow.evidence.add(diff_id + "-unused-candidate", "file_capture", raw),
    }
    diff = workflow.evidence.obj(diff_id, "diff")
    if not changed:
        diff["before"].append(candidate)
    diff["after"].append(candidate)
    diff["before"].sort(key=lambda item: item["path"])
    diff["after"].sort(key=lambda item: item["path"])
    before = {item["path"]: item for item in diff["before"]}
    after = {item["path"]: item for item in diff["after"]}
    diff["changed_paths"] = sorted(
        path for path in before.keys() | after.keys()
        if (before.get(path, {}).get("sha256"), before.get(path, {}).get("bytes"))
        != (after.get(path, {}).get("sha256"), after.get(path, {}).get("bytes"))
    )
    workflow.evidence.add(diff_id, "diff", diff)


def _mutate_web_candidate_to_unused_bytes(workflow, diff_id):
    """Model an existing full-snapshot path whose bytes changed to the unused fixture."""

    path = "tests/evals/fixtures/web-approval-and-capture/unused-candidate.html"
    before_raw = b"nonmatching evaluator fixture candidate\n"
    unused_raw = (ROOT / path).read_bytes()
    assert hashlib.sha256(before_raw).hexdigest() != hashlib.sha256(unused_raw).hexdigest()
    diff = workflow.evidence.obj(diff_id, "diff")
    diff["before"].append({
        "path": path,
        "sha256": hashlib.sha256(before_raw).hexdigest(),
        "bytes": len(before_raw),
        "content_id": workflow.evidence.add(diff_id + "-candidate-before", "file_capture", before_raw),
    })
    diff["after"].append({
        "path": path,
        "sha256": hashlib.sha256(unused_raw).hexdigest(),
        "bytes": len(unused_raw),
        "content_id": workflow.evidence.add(diff_id + "-candidate-after", "file_capture", unused_raw),
    })
    diff["before"].sort(key=lambda item: item["path"])
    diff["after"].sort(key=lambda item: item["path"])
    before = {item["path"]: item for item in diff["before"]}
    after = {item["path"]: item for item in diff["after"]}
    diff["changed_paths"] = sorted(
        path for path in before.keys() | after.keys()
        if (before.get(path, {}).get("sha256"), before.get(path, {}).get("bytes"))
        != (after.get(path, {}).get("sha256"), after.get(path, {}).get("bytes"))
    )
    workflow.evidence.add(diff_id, "diff", diff)


@pytest.mark.parametrize("diff_id", ("select-used", "apply-diff"))
def test_web_validator_allows_unchanged_unused_candidate_in_full_snapshot(tmp_path, diff_id):
    workflow = build_web_workflow(tmp_path)
    _add_unused_web_candidate_to_diff(workflow, diff_id, changed=False)

    workflow.validate()


@pytest.mark.parametrize("diff_id", ("select-used", "apply-diff"))
def test_web_validator_rejects_changed_unused_candidate_in_full_snapshot(tmp_path, diff_id):
    workflow = build_web_workflow(tmp_path)
    _add_unused_web_candidate_to_diff(workflow, diff_id, changed=True)

    with pytest.raises(contract.EventLogContractError, match="unused (web )?candidate persisted"):
        workflow.validate()


@pytest.mark.parametrize("diff_id", ("select-used", "apply-diff"))
def test_web_validator_rejects_existing_candidate_mutated_to_unused_bytes(tmp_path, diff_id):
    workflow = build_web_workflow(tmp_path)
    _mutate_web_candidate_to_unused_bytes(workflow, diff_id)

    with pytest.raises(contract.EventLogContractError, match="unused (web )?candidate persisted"):
        workflow.validate()


def test_binary_public_workflow_rejects_receipt_state_changed_after_capture(tmp_path):
    workflow = build_binary_workflow(tmp_path)
    workflow.validate()
    state = workflow.evidence.obj("initial_sync-consume-source-state", "source_state")
    item = workflow.deliveries[0]["items"][0]
    artifact = next(entry["artifact_id"] for entry in state["records"] if entry["path"].endswith(item["handoff_source_id"] + ".json"))
    record = workflow.evidence.obj(artifact, "source_record")
    record["diagnostics"][0]["message"] += " changed after consumption"
    workflow.evidence.add(artifact, "source_record", record)
    with pytest.raises(contract.EventLogContractError, match="source state changed|checkpoint|digest"):
        workflow.validate()


def test_web_public_workflow_rejects_unused_bytes_as_citation_source(tmp_path):
    workflow = build_web_workflow(tmp_path)
    workflow.validate()
    state = workflow.evidence.obj("apply-source-state", "source_state")
    representation = workflow.command_results["rendered"]["data"]["snapshot"]["active_representation"]
    artifact = next(entry["artifact_id"] for entry in state["files"] if entry["path"] == "sources/raw/" + representation["raw_path"])
    workflow.evidence.add(artifact, "file_capture", (ROOT / "tests/evals/fixtures/web-approval-and-capture/unused-candidate.html").read_bytes())
    with pytest.raises(contract.EventLogContractError, match="captured bytes|retained bytes"):
        workflow.validate()
