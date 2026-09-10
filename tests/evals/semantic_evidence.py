"""Strict semantics for descriptor-captured evaluation evidence."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import wraps
from pathlib import Path, PurePosixPath

from brainlib import contracts as product
from brainlib.citations import encode_markdown_path, parse_citation_definitions
from brainlib.extractors.handoff import _decode_item, _validate_item, collect_durable_handoffs, handoff_to_dict
from brainlib.extractors.adapters import validate_markdown
from brainlib.ledger import CitationRewrite, _canonical_record_payload, representation_for
from brainlib.markdown import scan_markdown
from brainlib import registry as recipes
from brainlib.sync import SyncAction, _iter_coverage_gaps
from brainlib.sync_results import PendingSyncResult, SyncResultReference, _canonical_json


class SemanticEvidenceError(ValueError):
    """Captured artifacts do not prove the claimed product semantics."""


def boundary(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SemanticEvidenceError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
            raise SemanticEvidenceError(f"{function.__name__}: {error}") from error
    return checked


def require(condition, message):
    if not condition:
        raise SemanticEvidenceError(message)


def obj(value, keys):
    require(type(value) is dict and set(value) == set(keys), "unexpected object fields")
    return value


def text(value):
    require(type(value) is str and bool(value.strip()) and not any(c in value for c in "\0\r\n"), "invalid string")
    return value


def integer(value):
    require(type(value) is int and value >= 0, "invalid nonnegative integer")
    return value


def identifier(value, prefix=""):
    require(type(value) is str and re.fullmatch(prefix + r"[0-9a-f]{64}", value) is not None, "invalid identity")
    return value


def array(value):
    require(type(value) is list, "invalid array")
    return value


def path(value, *, raw=False, prefix=None):
    text(value)
    p = PurePosixPath(value)
    require(bool(p.parts) and not p.is_absolute() and p.as_posix() == value and ".." not in p.parts
            and "\\" not in value and not re.match(r"^[A-Za-z]:", value), "noncanonical path")
    if raw:
        require(p.parts[:2] != ("sources", "raw"), "raw path includes repository prefix")
    if prefix:
        require(value.startswith(prefix) and len(value) > len(prefix), "wrong path namespace")
    return p


def json_value(value):
    # Round-trip rejects non-JSON Python values as well as non-finite scalars.
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        json.dumps(value, allow_nan=False)
    elif type(value) is list:
        for item in value:
            json_value(item)
    elif type(value) is dict:
        for key, item in value.items():
            require(type(key) is str, "non-string JSON key")
            json_value(item)
    else:
        raise SemanticEvidenceError("not JSON")


def diagnostic(value):
    obj(value, {"code", "message", "path", "details"})
    text(value["code"])
    text(value["message"])
    if value["path"] is not None:
        path(value["path"])
    require(type(value["details"]) is dict, "diagnostic details")
    json_value(value["details"])
    parsed = product._parse_diagnostic(value)
    product._validate_diagnostic(parsed)
    return parsed


@boundary
def parse_active_representation(value):
    obj(value, {"source_id", "content_sha256", "derivation_id", "raw_path", "extracted_path", "output_sha256", "quality_state", "anchors"})
    identifier(value["source_id"], "src_")
    identifier(value["content_sha256"])
    identifier(value["derivation_id"], "drv_")
    identifier(value["output_sha256"])
    raw = path(value["raw_path"], raw=True)
    extracted = path(value["extracted_path"], prefix="sources/extracted/")
    require(type(value["quality_state"]) is str and value["quality_state"] in {"ok", "warning"}, "quality state")
    anchors = []
    for anchor in array(value["anchors"]):
        obj(anchor, {"kind", "value"})
        require(text(anchor["kind"]) in {"line", "page", "slide", "sheet", "section", "row", "block"}, "anchor kind")
        text(anchor["value"])
        anchors.append(product._parse_anchor(anchor))
    require(bool(anchors) and len(set(anchors)) == len(anchors), "empty/duplicate anchors")
    require(extracted.parts[-2:] == (value["content_sha256"], value["derivation_id"] + ".md"), "extraction ownership")
    return product.SourceRepresentation(value["source_id"], value["content_sha256"], value["derivation_id"], raw, extracted,
                                        value["output_sha256"], value["quality_state"], tuple(anchors))


@boundary
def parse_sync_effect(kind, data):
    text(kind)
    if kind == "hashed_path":
        obj(data, {"path"})
        path(data["path"], raw=True)
    elif kind == "new_active_representation":
        parse_active_representation(data)
    elif kind == "citation_rewrite":
        obj(data, {"source_id", "content_sha256", "raw_path"})
        identifier(data["source_id"], "src_")
        identifier(data["content_sha256"])
        CitationRewrite(data["source_id"], data["content_sha256"], path(data["raw_path"], raw=True))
    elif kind == "handoff_source_id":
        obj(data, {"source_id", "record_sha256"})
        identifier(data["source_id"], "src_")
        identifier(data["record_sha256"])
    elif kind == "coverage_gap":
        diagnostic(data)
    else:
        raise SemanticEvidenceError("unknown effect kind")
    return data


@dataclass(frozen=True)
class ParsedManifestResponse:
    command: str
    data: dict
    reference: SyncResultReference


def handoff_summaries(data):
    if data["handoff_manifest"] is not None:
        path(data["handoff_manifest"], prefix="sources/ledger/handoffs/")
    ids = []
    for item in array(data["handoffs"]):
        obj(item, {"handoff_id", "kind", "source_id", "content_sha256", "reason"})
        ids.append(identifier(item["handoff_id"], "hnd_"))
        identifier(item["source_id"], "src_")
        identifier(item["content_sha256"])
        require(text(item["kind"]) in {"extraction", "rendered_web_capture"}, "handoff summary kind")
        text(item["reason"])
    require(len(set(ids)) == len(ids) and bool(ids) == (data["handoff_manifest"] is not None), "handoff summary manifest")
    require(data["handoffs"] == sorted(data["handoffs"], key=lambda item: (item["source_id"], item["kind"], item["handoff_id"])), "handoff summary order")


def inventory_item(value):
    obj(value, {"fingerprint", "media_type", "extension", "sha256", "url_descriptor"})
    f = obj(value["fingerprint"], {"path", "byte_size", "mtime_ns"})
    path(f["path"], raw=True)
    integer(f["byte_size"])
    integer(f["mtime_ns"])
    if value["media_type"] is not None:
        text(value["media_type"])
    require(type(value["extension"]) is str, "inventory extension")
    if value["sha256"] is not None:
        identifier(value["sha256"])
    if value["url_descriptor"] is not None:
        desc = obj(value["url_descriptor"], {"path", "url", "description", "added"})
        path(desc["path"], raw=True)
        text(desc["url"])
        require(type(desc["description"]) is str, "descriptor description")
        date.fromisoformat(text(desc["added"]))


def snapshot_data(value, reference):
    obj(value, {"source_id", "raw_path", "content_sha256", "source_version", "retrieval", "extraction_result", "active_representation", "corpus_revision"})
    identifier(value["source_id"], "src_")
    identifier(value["content_sha256"])
    raw = path(value["raw_path"], raw=True)
    require(value["corpus_revision"] == reference.corpus_revision, "snapshot revision")
    version = product._parse_content_version(value["source_version"])
    retrieval = product._parse_retrieval(value["retrieval"])
    # Parsing alone does not validate the full nested content contract.
    product._validate_content_version(version)
    product._validate_retrieval(retrieval)
    require(version.sha256 == value["content_sha256"] == retrieval.sha256
            and version.raw_path == raw and retrieval in version.retrieval_events, "snapshot version/retrieval")
    result = value["extraction_result"]
    derivation = None
    if result is not None:
        obj(result, {"state", "derivation", "attempt", "diagnostics"})
        state = product.SourceState(text(result["state"]))
        for item in array(result["diagnostics"]):
            diagnostic(item)
        if result["attempt"] is not None:
            attempt = product._parse_attempt(result["attempt"])
            product._validate_attempt(attempt)
            require(attempt.input_sha256 == value["content_sha256"] and attempt.outcome == state, "snapshot attempt identity")
        if result["derivation"] is not None:
            derivation = product._parse_derivation(result["derivation"])
            product._validate_derivation(derivation)
            require(derivation.source_sha256 == value["content_sha256"] and state.value == derivation.quality_state, "snapshot derivation identity")
    if value["active_representation"] is not None:
        rep = parse_active_representation(value["active_representation"])
        require(rep.source_id == value["source_id"] and rep.content_sha256 == value["content_sha256"] and rep.raw_path == raw, "snapshot activation")
        if result is not None:
            require(derivation is not None and derivation.derivation_id == rep.derivation_id
                    and derivation.output_sha256 == rep.output_sha256 and derivation.anchors == rep.anchors, "snapshot extraction activation")


@boundary
def parse_manifest_response(result, *, observed_exit_code):
    obj(result, {"ok", "command", "data", "warnings", "errors"})
    require(type(result["ok"]) is bool and type(observed_exit_code) is int, "response scalar types")
    for item in array(result["warnings"]) + array(result["errors"]):
        diagnostic(item)
    command = text(result["command"])
    if command == "eval mock-web-capture":
        data = obj(result["data"], {"fixture_id", "product_result"})
        require(data["fixture_id"] == "web-approval-and-capture.initial" and result["ok"] and observed_exit_code == 0
                and not result["errors"] and not result["warnings"], "fixture wrapper")
        parsed = parse_manifest_response(data["product_result"], observed_exit_code=0)
        require(parsed.command == "source snapshot-url", "wrapped product command")
        return parsed
    require(command in {"init", "sync", "source snapshot-url"}, "manifest command")
    data = result["data"]
    require(type(data) is dict, "manifest response data")
    reference = SyncResultReference.from_dict(data.get("result_manifest"))
    if command == "source snapshot-url":
        obj(data, {"snapshot", "handoff_manifest", "handoffs", "result_manifest"})
        require(result["ok"] and observed_exit_code == 0 and not result["errors"], "snapshot failure")
        handoff_summaries(data)
        snapshot_data(data["snapshot"], reference)
        return ParsedManifestResponse(command, data, reference)
    samples = {"hashed_path": "hashed_paths", "new_active_representation": "new_active_representations", "citation_rewrite": "citation_rewrites", "handoff_source_id": "handoff_source_ids", "coverage_gap": "coverage_gaps"}
    fields = {"status", "corpus_revision", "decision_counts", "sampled_decisions", "sample_limits", "result_manifest", "handoff_manifest", "handoffs"}
    fields.update(samples.values())
    fields.update(kind + "_count" for kind in samples)
    obj(data, fields)
    identifier(data["corpus_revision"])
    require(data["corpus_revision"] == reference.corpus_revision, "response revision")
    obj(data["sample_limits"], {"max_items_per_field", "max_bytes_per_field"})
    require(integer(data["sample_limits"]["max_items_per_field"]) == 100
            and integer(data["sample_limits"]["max_bytes_per_field"]) == 32768, "sample limits")
    for kind, field in samples.items():
        values = array(data[field])
        count = integer(data[kind + "_count"])
        require(len(values) <= count == reference.event_counts[kind] and len(values) <= 100, "sample count")
        require(sum(len(item.encode()) if type(item) is str else len(json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()) for item in values) <= 32768, "sample bytes")
        for item in values:
            if kind == "hashed_path":
                path(item, raw=True)
            elif kind == "handoff_source_id":
                identifier(item, "src_")
            else:
                parse_sync_effect(kind, item)
        if kind in {"hashed_path", "handoff_source_id"}:
            keys = values
        elif kind == "new_active_representation":
            keys = [(item["source_id"], item["content_sha256"], item["derivation_id"]) for item in values]
        elif kind == "citation_rewrite":
            keys = [(item["source_id"], item["content_sha256"], item["raw_path"]) for item in values]
        else:
            keys = [(item["path"] or "", item["code"], item["message"], json.dumps(item["details"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))) for item in values]
        require(keys == sorted(keys), "response sample order")
    obj(data["decision_counts"], {action.value for action in SyncAction})
    for count in data["decision_counts"].values():
        integer(count)
    decisions = array(data["sampled_decisions"])
    require(len(decisions) <= 100, "decision sample limit")
    observed = Counter()
    decision_keys = []
    decision_bytes = 0
    for item in decisions:
        obj(item, {"source_id", "action", "reason", "item"})
        if item["source_id"] is not None:
            identifier(item["source_id"], "src_")
        action = SyncAction(text(item["action"]))
        observed[action.value] += 1
        text(item["reason"])
        if item["item"] is not None:
            inventory_item(item["item"])
        inv = item["item"]
        decision_keys.append((inv["fingerprint"]["path"] if inv else "", item["source_id"] or "", item["action"], item["reason"]))
        # Production budgets the internal decision projection, not the public
        # nested inventory spelling; preserve that exact byte-count contract.
        projection = None if inv is None else {
            **inv["fingerprint"], "media_type": inv["media_type"], "extension": inv["extension"], "sha256": inv["sha256"],
            "url": None if inv["url_descriptor"] is None else inv["url_descriptor"]["url"],
            "description": None if inv["url_descriptor"] is None else inv["url_descriptor"]["description"],
        }
        decision_bytes += len(json.dumps({**item, "item": projection}, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode())
    require(decision_keys == sorted(decision_keys) and decision_bytes <= 32768, "decision sample order/bytes")
    require(all(count <= data["decision_counts"][action] for action, count in observed.items()), "decision count")
    handoff_summaries(data)
    gaps = reference.event_counts["coverage_gap"] > 0
    require(data["status"] == ("complete_with_gaps" if gaps else "complete"), "response status")
    if gaps:
        require(result["ok"] is False and observed_exit_code == 1 and len(result["errors"]) == 1
                and result["errors"][0] == {"code": "source_coverage_gaps", "message": "Synchronization completed with unresolved source coverage gaps.", "path": None, "details": {}}, "unaccounted manifest error")
    else:
        require(result["ok"] is True and observed_exit_code == 0 and not result["errors"], "manifest response failure")
    # Uses production joins after strict count/scalar checks, avoiding bool == int.
    from datetime import datetime, timezone
    PendingSyncResult(command, datetime(2000, 1, 1, tzinfo=timezone.utc), reference, data)
    return ParsedManifestResponse(command, data, reference)


@dataclass(frozen=True)
class CapturedSourceState:
    records: dict[str, product.SourceRecord]
    files: dict[str, bytes]
    corpus_revision: str
    pending_result: PendingSyncResult | None = None
    pending_checkpoint: bytes | None = None


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(SemanticEvidenceError("non-finite JSON")))
    require(type(value) is dict, "JSON object required")
    return value


@boundary
def read_source_state(control, command_id, *, result_id, revision, execution_id=None):
    observation = control.obj(command_id, "command_observation")
    state_id = text(observation["source_state_id"])
    state = strict_json(control.read(state_id, "source_state"))
    obj(state, {"schema_version", "run_id", "execution_id", "command_id", "capture_point", "result_id", "corpus_revision", "records", "files", "pending_result_id"})
    require(type(state["schema_version"]) is int and state["schema_version"] == 1
            and state["run_id"] == control.run_id and state["command_id"] == command_id
            and state["execution_id"] == (execution_id if execution_id is not None else observation["execution_id"])
            and state["capture_point"] == "after_command_before_return"
            and state["result_id"] == result_id, "source-state command binding")
    if result_id is not None:
        identifier(result_id, "sync_")
    identifier(revision)
    require(state["corpus_revision"] == revision, "source-state revision")
    records, files = {}, {}
    for field, kind in (("records", "source_record"), ("files", "file_capture")):
        previous = ""
        artifacts = set()
        for entry in array(state[field]):
            obj(entry, {"path", "artifact_id"})
            logical = path(entry["path"]).as_posix()
            require(logical > previous and text(entry["artifact_id"]) not in artifacts, "source-state inventory order/duplicate")
            previous = logical
            artifacts.add(entry["artifact_id"])
            raw = control.read(entry["artifact_id"], kind)
            if field == "records":
                record = product.SourceRecord.from_dict(strict_json(raw))
                record.to_dict()
                require(logical == f"sources/ledger/{record.source_id}.json" and record.source_id not in records, "source-record path identity")
                records[record.source_id] = record
            else:
                require(logical == "config/extractors.toml" or logical.startswith(("sources/raw/", "sources/extracted/")), "source-state file namespace")
                files[logical] = raw
    require(product.compute_corpus_revision(records.values()) == revision, "captured corpus mismatch")
    require("config/extractors.toml" in files, "captured registry missing")
    pending, checkpoint = None, None
    if state["pending_result_id"] is not None:
        checkpoint = control.read(text(state["pending_result_id"]), "pending_sync_result")
        value = strict_json(checkpoint)
        obj(value, {"schema_version", "command", "generated_at", "reference", "result_data"})
        require(type(value["schema_version"]) is int and value["schema_version"] == 1, "pending decision schema")
        reference = SyncResultReference.from_dict(value["reference"])
        require(reference.result_id == result_id and reference.corpus_revision == revision, "pending decision result identity")
        pending = PendingSyncResult(text(value["command"]), datetime.fromisoformat(text(value["generated_at"])), reference, value["result_data"])
        # Retain producer bytes, not a normalization that erases negative zero,
        # whitespace or equivalent timestamp spellings before the origin join.
    return CapturedSourceState(records, files, revision, pending, checkpoint)


@boundary
def require_active_representation(value, state):
    rep = parse_active_representation(value)
    record = state.records[rep.source_id]
    require(record.active_content_sha256 == rep.content_sha256 and record.active_derivation_id == rep.derivation_id
            and representation_for(record, rep.content_sha256, rep.derivation_id) == rep, "representation is not exact current activation")
    version, derivation = record.versions[rep.content_sha256], record.derivations[rep.derivation_id]
    for logical, sha, size in (("sources/raw/" + rep.raw_path.as_posix(), rep.content_sha256, version.byte_size),
                               (rep.extracted_path.as_posix(), rep.output_sha256, derivation.output_byte_size)):
        raw = state.files[logical]
        require(len(raw) == size and hashlib.sha256(raw).hexdigest() == sha, "active representation captured bytes")
    validate_markdown(state.files[rep.extracted_path.as_posix()], rep.anchors,
                      expected_anchors=tuple(sorted({anchor.kind for anchor in rep.anchors})),
                      max_output_bytes=derivation.output_byte_size)
    return rep


def record_digest(record):
    return hashlib.sha256(_canonical_record_payload(record)).hexdigest()


@boundary
def validate_stream_effects(effects, state):
    seen = set()
    gaps = Counter()
    for effect in effects:
        obj(effect, {"kind", "data"})
        kind, data = effect["kind"], parse_sync_effect(effect["kind"], effect["data"])
        if kind == "new_active_representation":
            rep = require_active_representation(data, state)
            identity = (kind, rep.source_id)
        elif kind == "handoff_source_id":
            record = state.records[data["source_id"]]
            require(record.state is product.SourceState.NEEDS_AGENT and record_digest(record) == data["record_sha256"], "handoff source checkpoint mismatch")
            identity = (kind, record.source_id)
        elif kind == "citation_rewrite":
            record = state.records[data["source_id"]]
            require(record.versions[data["content_sha256"]].raw_path.as_posix() == data["raw_path"], "rewrite retained version mismatch")
            identity = (kind, data["source_id"], data["content_sha256"])
        elif kind == "coverage_gap":
            gaps[json.dumps(data, sort_keys=True)] += 1
            continue
        else:
            continue
        require(identity not in seen, "duplicate source effect")
        seen.add(identity)
    expected = Counter(json.dumps(product._diagnostic_to_dict(d), sort_keys=True) for _, d in _iter_coverage_gaps(state.records, ()))
    require(all(count <= expected[key] for key, count in gaps.items()), "unexplained coverage gap")


@boundary
def validate_response_effects(parsed, effects, state, *, generated_at=None):
    """Join bounded response samples/nested snapshot facts to their authorities."""
    require(parsed.reference.corpus_revision == state.corpus_revision, "response source-state revision")
    handoff_records = state.records
    if parsed.command == "source snapshot-url":
        require(state.pending_result is None and state.pending_checkpoint is None,
                "snapshot origin cannot establish a pending sync checkpoint")
        source_id = parsed.data["snapshot"]["source_id"]
        handoff_records = {source_id: state.records[source_id]}
    expected_items = collect_durable_handoffs(handoff_records, captured_registry(state))
    expected_summaries = [{"handoff_id": item.handoff_id, "kind": item.kind, "source_id": item.source_id,
                           "content_sha256": item.content_sha256, "reason": item.reason} for item in expected_items]
    require(parsed.data["handoffs"] == expected_summaries, "handoff summary differs from captured source state")
    if parsed.command == "source snapshot-url":
        value = parsed.data["snapshot"]
        record = state.records[value["source_id"]]
        require(record.active_content_sha256 == value["content_sha256"], "snapshot source content")
        encoded = record.to_dict()
        require(encoded["versions"][value["content_sha256"]] == value["source_version"], "snapshot source version checkpoint")
        require(value["retrieval"] in value["source_version"]["retrieval_events"], "snapshot retrieval checkpoint")
        result = value["extraction_result"]
        if result is not None:
            require(result["state"] == record.state.value, "snapshot extraction state checkpoint")
            if result["attempt"] is not None:
                require(result["attempt"] == encoded["last_attempt"], "snapshot attempt checkpoint")
            if result["derivation"] is not None:
                require(result["derivation"] == encoded["derivations"].get(result["derivation"]["derivation_id"]), "snapshot derivation checkpoint")
            declared = Counter(json.dumps(item, sort_keys=True) for item in result["diagnostics"])
            retained = Counter(json.dumps(item, sort_keys=True) for item in encoded["diagnostics"])
            require(all(count <= retained[key] for key, count in declared.items()), "snapshot diagnostic checkpoint")
            if result["attempt"] is not None:
                require({item["code"] for item in result["diagnostics"]} <= set(result["attempt"]["diagnostic_codes"]), "snapshot diagnostic attempt")
        if value["active_representation"] is not None:
            require_active_representation(value["active_representation"], state)
        else:
            require(record.active_derivation_id is None, "snapshot hides active derivation")
        return
    fields = {"hashed_path": "hashed_paths", "new_active_representation": "new_active_representations", "citation_rewrite": "citation_rewrites", "handoff_source_id": "handoff_source_ids", "coverage_gap": "coverage_gaps"}
    projections = {kind: Counter() for kind in fields}
    for effect in effects:
        kind, data = effect["kind"], parse_sync_effect(effect["kind"], effect["data"])
        public = data["path"] if kind == "hashed_path" else data["source_id"] if kind == "handoff_source_id" else data
        projections[kind][json.dumps(public, sort_keys=True)] += 1
    for kind, field in fields.items():
        sample = Counter(json.dumps(item, sort_keys=True) for item in parsed.data[field])
        require(all(count <= projections[kind][key] for key, count in sample.items()), "response sample absent from immutable stream")
    expected_gaps = Counter(json.dumps(product._diagnostic_to_dict(d), sort_keys=True) for _, d in _iter_coverage_gaps(state.records, ()))
    require(projections["coverage_gap"] == expected_gaps, "sync does not explain complete captured ledger coverage")
    pending = state.pending_result
    require(pending is not None, "pending decision capture missing")
    require(pending.command == parsed.command and pending.reference == parsed.reference, "pending decision origin binding")
    if generated_at is not None:
        require(pending.generated_at == datetime.fromisoformat(text(generated_at)), "pending decision stream time binding")
    expected = dict(parsed.data)
    if expected["handoffs"]:
        # Production compact_sync_data retains all decision fields verbatim;
        # only the complete handoff array moves behind this digest reference.
        payload = _canonical_json({"schema_version": 1, "handoff_manifest": expected["handoff_manifest"], "handoffs": expected.pop("handoffs")})
        digest = hashlib.sha256(payload).hexdigest()
        expected["handoff_response"] = {"path": f".brain/sync-results/handoffs_{digest}.json", "sha256": digest}
    require(_canonical_json(pending.result_data) == _canonical_json(expected), "response decisions/data differ from captured pending result")
    # This is the exact SyncResultStore.save_pending serialization. Public
    # validation supplies the immutable header time, not the copy's timestamp.
    origin_time = pending.generated_at if generated_at is None else datetime.fromisoformat(text(generated_at))
    expected_checkpoint = {"schema_version": 1, "command": parsed.command,
                           "generated_at": origin_time.isoformat(), "reference": parsed.reference.to_dict(),
                           "result_data": expected}
    require(state.pending_checkpoint == _canonical_json(expected_checkpoint),
            "pending bytes differ from exact producer serialization")


def captured_registry(state):
    document = tomllib.loads(state.files["config/extractors.toml"].decode("utf-8"))
    recipes._reject_unknown_keys(document, recipes._ROOT_KEYS, "registry")
    require(type(document.get("schema_version")) is int and document["schema_version"] == 1, "registry schema")
    extractors = tuple(recipes._parse_extractor(value, index=index) for index, value in enumerate(array(document.get("extractors"))))
    recipes._validate_unique_registry_entries(extractors)
    return recipes.ExtractorRegistry(1, extractors, recipes._canonical_sha256(document))


@boundary
def validate_delivery_sources(effects, items, state):
    expected_records = {}
    for effect in effects:
        if effect["kind"] != "handoff_source_id":
            continue
        data = parse_sync_effect(effect["kind"], effect["data"])
        record = state.records[data["source_id"]]
        require(record.source_id not in expected_records and record_digest(record) == data["record_sha256"], "delivery source digest/duplicate")
        expected_records[record.source_id] = record
    decoded = []
    for item in items:
        typed = _decode_item(item) if type(item) is dict else item
        _validate_item(typed)
        decoded.append(typed)
    require([item.source_id for item in decoded] == list(expected_records), "delivery effect order")
    expected = collect_durable_handoffs(expected_records, captured_registry(state))
    require([handoff_to_dict(item) for item in decoded] == [handoff_to_dict(item) for item in expected], "delivery differs from captured durable sources")


@boundary
def validate_promoted_web_citations(body, logical_path, state, rendered_activation):
    require(type(body) is str, "Markdown must be text")
    document = Path("/__eval_logical_root__") / path(logical_path, prefix="wiki/")
    scan = scan_markdown(body)
    require(not scan.diagnostics, "ambiguous citation Markdown")
    definitions = parse_citation_definitions(body, path=document)
    counts = Counter(item.citation_id for item in definitions)
    definition_lines = {item.definition_line for item in definitions}
    markers = Counter(item.citation_id for item in scan.citation_markers if item.line not in definition_lines)
    require(bool(definitions) and all(count == 1 for count in counts.values())
            and set(markers) == set(counts), "missing/unused/duplicate citation definition")
    sources = [heading for heading in scan.headings if heading.level == 2 and heading.text == "Sources"]
    require(bool(sources) and not any(h.line > sources[-1].line for h in scan.headings), "Sources must be final")
    rendered = require_active_representation(rendered_activation, state)
    require(rendered.raw_path.parts[:3] == ("_web", rendered.source_id, rendered.content_sha256), "rendered source ownership")
    matched = False
    for citation in definitions:
        require(citation.definition_line > sources[-1].line, "citation outside Sources")
        record = state.records[citation.source_id]
        rep = representation_for(record, citation.content_sha256, citation.derivation_id)
        require(rep is not None and citation.anchor in rep.anchors, "citation retained identity/anchor")
        raw_path = "sources/raw/" + rep.raw_path.as_posix()
        extracted_path = rep.extracted_path.as_posix()
        expected_raw = encode_markdown_path(document, Path("/__eval_logical_root__") / raw_path)
        expected_extracted = encode_markdown_path(document, Path("/__eval_logical_root__") / extracted_path) + f"#{citation.anchor.kind}:{citation.anchor.value}"
        require(citation.original_destination == expected_raw and citation.extracted_destination == expected_extracted, "citation destination does not resolve to retained representation")
        if rep.raw_path.parts[0] in {"_web", "_versions"}:
            require(rep.raw_path.parts[1:3] == (rep.source_id, rep.content_sha256), "citation raw namespace owner")
        else:
            require(rep.raw_path == record.current_raw_path and rep.content_sha256 == record.active_content_sha256, "citation live raw owner")
        require(rep.extracted_path.parts[-2:] == (rep.content_sha256, rep.derivation_id + ".md"), "citation extracted owner")
        version, derivation = record.versions[rep.content_sha256], record.derivations[rep.derivation_id]
        for target, sha, size in ((raw_path, rep.content_sha256, version.byte_size), (extracted_path, rep.output_sha256, derivation.output_byte_size)):
            raw = state.files[target]
            require(len(raw) == size and hashlib.sha256(raw).hexdigest() == sha, "citation retained bytes mismatch")
        validate_markdown(state.files[extracted_path], rep.anchors,
                          expected_anchors=tuple(sorted({anchor.kind for anchor in rep.anchors})),
                          max_output_bytes=derivation.output_byte_size)
        matched |= rep == rendered
    require(matched, "no used citation resolves to rendered activation")
