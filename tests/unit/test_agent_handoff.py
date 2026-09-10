from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

import brainlib.commands as commands
from brainlib.commands import CommandServices
from brainlib.contracts import Anchor, SourceState, derivation_id
from brainlib.diagnostics import Diagnostic
from brainlib.extractors.handoff import (
    AgentRegistrationError,
    handoff_id_for,
    handoff_to_dict,
    load_handoff_item,
    register_staged_agent_extraction,
    write_handoff_manifest,
)
from brainlib.extractors.processor import DeterministicSourceProcessor
from brainlib.layout import RepoPaths
from brainlib.ledger import ActivationGuard, LedgerStore, activate_derivation
from tests.conftest import durable_source_id
from tests.helpers_extractors import (
    FIXED_NOW,
    PNG_BYTES,
    handoff_for_job,
    make_job,
    record_for_handoff,
    resolve_test_converter,
    run_brain,
    run_brain_json,
)


SERVICES = CommandServices(
    lambda: DeterministicSourceProcessor(resolve=lambda _spec: None),
    lambda spec: resolve_test_converter(spec).prerequisite_digest,
)
ANCHORS = (Anchor("block", "image-1"),)
NOTE = "faithful diagram transcription"


@pytest.mark.parametrize(
    ("command", "interruption"),
    (("init", None), ("sync", "save_pending"), ("init", "complete_inflight")),
)
def test_thousand_handoffs_survive_delivery_replay_ack_and_later_sync(
    repo_paths, monkeypatch, command, interruption
):
    from brainlib.sync_results import SyncResultStore

    directory = repo_paths.raw / "images"
    directory.mkdir()
    for index in range(1000):
        (directory / f"image-{index:04d}.png").write_bytes(PNG_BYTES)
    with monkeypatch.context() as failures:
        if interruption:

            def stop(*args, **kwargs):
                raise OSError("injected handoff receipt interruption")

            failures.setattr(SyncResultStore, interruption, stop)
        first = run_brain_json(repo_paths.root, command, services=SERVICES)
    assert len(LedgerStore(repo_paths).load_all()) == 1000
    if interruption:
        assert "injected handoff receipt interruption" in first["errors"][0]["message"]
    else:
        assert len(first["data"]["handoffs"]) == 1000, first
    retry = run_brain_json(repo_paths.root, command, services=SERVICES)
    assert len(retry["data"]["handoffs"]) == 1000, retry
    manifest_path = repo_paths.root / retry["data"]["handoff_manifest"]
    items = json.loads(manifest_path.read_text())["items"]
    summaries = [
        {
            key: item[key]
            for key in ("handoff_id", "kind", "source_id", "content_sha256", "reason")
        }
        for item in items
    ]
    assert retry["data"]["handoffs"] == summaries
    assert len(json.dumps(summaries).encode()) > 256 * 1024
    assert len({item["handoff_id"] for item in summaries}) == 1000
    assert (
        repo_paths.root / ".brain/sync-results/pending.json"
    ).stat().st_size < 256 * 1024
    replay = run_brain_json(repo_paths.root, command, services=SERVICES)
    assert replay == retry
    if interruption is None:
        assert retry == first
    receipt = retry["data"]["result_manifest"]["result_id"]
    consumed = run_brain_json(
        repo_paths.root, "source", "consume-sync-result", "--result-id", receipt
    )
    assert consumed["ok"]
    ack = run_brain_json(
        repo_paths.root, "source", "acknowledge-sync-result", "--result-id", receipt
    )
    assert ack["ok"]
    assert SyncResultStore(repo_paths).load_pending() is None
    (directory / "later.png").write_bytes(PNG_BYTES)
    later = run_brain_json(repo_paths.root, "sync", services=SERVICES)
    assert len(later["data"]["handoffs"]) == 1001, later
    assert {item["handoff_id"] for item in summaries} < {
        item["handoff_id"] for item in later["data"]["handoffs"]
    }


@pytest.mark.parametrize(
    "mutation", ("bytes", "path", "symlink", "manifest", "missing")
)
def test_handoff_response_reference_rejects_tampering_before_replay_or_ack(
    repo_with_agent_sources, repo_paths, mutation
):
    initial = run_brain_json(repo_paths.root, "init", services=SERVICES)
    receipt_path = repo_paths.root / ".brain/sync-results/pending.json"
    receipt = json.loads(receipt_path.read_text())
    reference = receipt["result_data"]["handoff_response"]
    artifact = repo_paths.root / reference["path"]
    original = artifact.read_bytes()
    if mutation == "bytes":
        artifact.write_bytes(
            original.replace(b"extractor_processor_unavailable", b"forged")
        )
    elif mutation == "path":
        reference["path"] = "../" + reference["path"]
        receipt_path.write_text(json.dumps(receipt))
    elif mutation == "missing":
        del receipt["result_data"]["handoff_response"]
        receipt_path.write_text(json.dumps(receipt))
    elif mutation == "manifest":
        receipt["result_data"]["handoff_manifest"] = (
            "sources/ledger/handoffs/other.json"
        )
        receipt_path.write_text(json.dumps(receipt))
    else:
        target = artifact.with_suffix(".saved")
        artifact.rename(target)
        artifact.symlink_to(target)
    replay = run_brain_json(repo_paths.root, "init", services=SERVICES)
    assert not replay["ok"]
    assert "handoffs" not in replay["data"]
    ack = run_brain_json(
        repo_paths.root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        initial["data"]["result_manifest"]["result_id"],
    )
    assert not ack["ok"]
    assert receipt_path.is_file()


def register(paths, handoff, staging, record=None, **overrides):
    arguments = dict(
        handoff=handoff,
        record=record or record_for_handoff(handoff, paths),
        staging_path=staging,
        anchors=ANCHORS,
        quality_state="ok",
        note=NOTE,
        paths=paths,
        now=FIXED_NOW,
    )
    arguments.update(overrides)
    return register_staged_agent_extraction(**arguments)


def manifest(paths, handoff, run_id="run-20260904"):
    return write_handoff_manifest(
        paths, run_id=run_id, created_at=FIXED_NOW, items=(handoff,)
    )


def command_register(paths, handoff, staging, *, services=SERVICES):
    return run_brain_json(
        paths.root,
        "source",
        "register-extraction",
        "--handoff-id",
        handoff.handoff_id,
        "--staging-path",
        str(staging),
        "--anchors-json",
        '[{"kind":"block","value":"image-1"}]',
        "--quality-state",
        "ok",
        "--note",
        NOTE,
        services=services,
    )


def prepare_command(paths, handoff):
    manifest(paths, handoff)
    LedgerStore(paths).save(record_for_handoff(handoff, paths))


def test_needs_agent_manifest_freezes_recipe_identity(repo_paths, extraction_handoff):
    path = manifest(repo_paths, extraction_handoff)
    item = json.loads(path.read_text())["items"][0]
    for key in ("extractor_id", "extractor_version", "config_sha256", "agent_revision"):
        assert item[key] == getattr(extraction_handoff, key)
    without_id = {key: value for key, value in item.items() if key != "handoff_id"}
    assert (
        item["handoff_id"]
        == "hnd_"
        + hashlib.sha256(
            json.dumps(without_id, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    with pytest.raises(FrozenInstanceError):
        extraction_handoff.reason = "changed"


def test_registration_derives_identity_only_from_manifest(
    repo_paths, extraction_handoff, staging_markdown
):
    result = register(repo_paths, extraction_handoff, staging_markdown)
    assert result.derivation is not None
    assert result.derivation.extractor_id == extraction_handoff.extractor_id
    assert result.derivation.extractor_version == extraction_handoff.extractor_version
    assert result.derivation.config_sha256 == extraction_handoff.config_sha256
    assert result.derivation.method == "agent"
    assert result.derivation.method_metadata == {
        "handoff_id": extraction_handoff.handoff_id,
        "agent_revision": extraction_handoff.agent_revision,
        "note": NOTE,
    }
    assert LedgerStore(repo_paths).load_all() == {}
    assert staging_markdown.is_file()


def test_same_agent_revision_replays_only_identical_output(
    repo_paths, extraction_handoff, staging_markdown
):
    record = record_for_handoff(extraction_handoff, repo_paths)
    first = register(repo_paths, extraction_handoff, staging_markdown, record)
    activated = activate_derivation(record, first.derivation, now=FIXED_NOW)
    replayed = register(
        repo_paths,
        extraction_handoff,
        staging_markdown,
        activated,
        now=FIXED_NOW + timedelta(days=1),
    )
    assert replayed.derivation == first.derivation
    staging_markdown.write_text(
        '<a id="block:image-1"></a>\n## Changed\nDifferent bytes\n'
    )
    with pytest.raises(AgentRegistrationError, match="output_path_collision"):
        register(repo_paths, extraction_handoff, staging_markdown, activated)


@pytest.mark.parametrize(
    "mutation", ("note", "anchors", "quality", "inactive", "missing", "mtime")
)
def test_replay_preserves_exact_retained_provenance(
    repo_paths, extraction_handoff, staging_markdown, mutation
):
    record = record_for_handoff(extraction_handoff, repo_paths)
    first = register(repo_paths, extraction_handoff, staging_markdown, record)
    activated = activate_derivation(record, first.derivation, now=FIXED_NOW)
    output = repo_paths.root / first.derivation.output_path
    kwargs = {}
    if mutation == "note":
        kwargs["note"] = "different note"
    elif mutation == "anchors":
        kwargs["anchors"] = (Anchor("block", "other"),)
    elif mutation == "quality":
        kwargs["quality_state"] = "warning"
    elif mutation == "inactive":
        activated = replace(activated, active_derivation_id=None)
    elif mutation == "missing":
        output.unlink()
    else:
        os.utime(output, ns=(1, 1))
    with pytest.raises(AgentRegistrationError):
        register(repo_paths, extraction_handoff, staging_markdown, activated, **kwargs)
    if mutation == "missing":
        assert not output.exists()


@pytest.mark.parametrize(
    "case",
    ("sibling", "root", "escape", "file_symlink", "directory_symlink", "missing"),
)
def test_agent_staging_is_scoped_to_exact_handoff_directory(
    repo_paths, extraction_handoff, staging_markdown, case
):
    target = staging_markdown
    if case == "sibling":
        target = staging_markdown.parent.parent / ("hnd_" + "f" * 64) / "result.md"
        target.parent.mkdir()
        target.write_bytes(staging_markdown.read_bytes())
    elif case == "root":
        target = staging_markdown.parent
    elif case == "escape":
        target = (
            staging_markdown.parent / ".." / extraction_handoff.handoff_id / "result.md"
        )
    elif case == "file_symlink":
        target = staging_markdown.with_name("alias.md")
        target.symlink_to(staging_markdown)
    elif case == "directory_symlink":
        real = staging_markdown.parent.with_name("retained")
        staging_markdown.parent.rename(real)
        staging_markdown.parent.symlink_to(real, target_is_directory=True)
    else:
        staging_markdown.unlink()
    with pytest.raises(AgentRegistrationError, match="staging"):
        register(repo_paths, extraction_handoff, target)
    assert not tuple(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize(
    "body",
    (
        b"",
        b"\xff",
        b"no anchors",
        b'<a id="block:image-1"></a>\n',
        b'<a id="block:image-1"></a>\n<a id="block:image-1"></a>\nText',
        b'```html\n<a id="block:image-1"></a>\n```\nText',
    ),
)
def test_invalid_agent_markdown_never_publishes(
    repo_paths, extraction_handoff, staging_markdown, body
):
    staging_markdown.write_bytes(body)
    with pytest.raises(AgentRegistrationError):
        register(repo_paths, extraction_handoff, staging_markdown)
    assert not tuple(repo_paths.extracted.rglob("*.md"))


@pytest.mark.parametrize(
    "overrides",
    (
        {"note": " "},
        {"anchors": ()},
        {"anchors": ANCHORS * 2},
        {"anchors": (Anchor("bad", "1"),)},
        {"quality_state": "failed"},
    ),
)
def test_registration_rejects_invalid_metadata_before_publication(
    repo_paths, extraction_handoff, staging_markdown, overrides
):
    with pytest.raises(AgentRegistrationError):
        register(repo_paths, extraction_handoff, staging_markdown, **overrides)
    assert not tuple(repo_paths.extracted.rglob("*.md"))


def test_registration_rejects_oversized_staging(
    repo_paths, extraction_handoff, staging_markdown, monkeypatch
):
    import brainlib.extractors.handoff as module

    monkeypatch.setattr(module, "MAX_AGENT_STAGING_BYTES", 16)
    with pytest.raises(AgentRegistrationError, match="limit|oversized"):
        register(repo_paths, extraction_handoff, staging_markdown)


def test_registration_requires_matching_needs_agent_attempt(
    repo_paths, extraction_handoff, staging_markdown
):
    record = record_for_handoff(extraction_handoff, repo_paths)
    for changed in (
        replace(record, last_attempt=None),
        replace(record, state=SourceState.PENDING),
        replace(
            record, last_attempt=replace(record.last_attempt, config_sha256="f" * 64)
        ),
    ):
        with pytest.raises(AgentRegistrationError, match="attempt"):
            register(repo_paths, extraction_handoff, staging_markdown, changed)
    assert not tuple(repo_paths.extracted.rglob("*.md"))


def test_agent_revision_bump_changes_handoff_and_derivation_identity(
    extraction_handoff,
):
    fields = handoff_to_dict(extraction_handoff)
    fields.pop("handoff_id")
    fields.update(agent_revision="2", config_sha256="e" * 64)
    assert handoff_id_for(fields) != extraction_handoff.handoff_id
    args = dict(
        source_sha256=extraction_handoff.content_sha256,
        extractor_id=extraction_handoff.extractor_id,
        extractor_version=extraction_handoff.extractor_version,
    )
    assert derivation_id(
        **args, config_sha256=extraction_handoff.config_sha256
    ) != derivation_id(**args, config_sha256="e" * 64)


def test_registration_rejects_web_capture_handoff(
    repo_paths, extraction_handoff, staging_markdown
):
    fields = handoff_to_dict(extraction_handoff)
    fields.pop("handoff_id")
    fields["kind"] = "rendered_web_capture"
    handoff = replace(
        extraction_handoff,
        kind="rendered_web_capture",
        handoff_id=handoff_id_for(fields),
    )
    with pytest.raises(AgentRegistrationError, match="not an extraction handoff"):
        register(repo_paths, handoff, staging_markdown)


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("--extractor-id", "forged"),
        ("--extractor-version", "forged"),
        ("--config-sha256", "f" * 64),
        ("--method", "deterministic"),
        ("--agent-revision", "999"),
        ("--content-sha256", "f" * 64),
    ),
)
def test_cli_has_no_caller_controlled_recipe_flags(repo_root, flag, value):
    result = run_brain(
        repo_root,
        "--json",
        "source",
        "register-extraction",
        "--handoff-id",
        "hnd_" + "b" * 64,
        "--staging-path",
        ".brain/agent-staging/hnd_" + "b" * 64 + "/result.md",
        "--anchors-json",
        '[{"kind":"block","value":"image-1"}]',
        "--quality-state",
        "ok",
        "--note",
        "diagram",
        flag,
        value,
        services=SERVICES,
    )
    assert result.returncode == 2


def test_init_returns_manifest_and_sorted_handoff_ids(repo_with_agent_sources):
    payload = run_brain_json(repo_with_agent_sources, "init", services=SERVICES)
    assert payload["data"]["handoff_manifest"].startswith("sources/ledger/handoffs/")
    summaries = payload["data"]["handoffs"]
    assert summaries and summaries == sorted(
        summaries,
        key=lambda item: (item["source_id"], item["kind"], item["handoff_id"]),
    )
    assert all(
        re.fullmatch(r"hnd_[0-9a-f]{64}", item["handoff_id"]) for item in summaries
    )
    assert (repo_with_agent_sources / payload["data"]["handoff_manifest"]).is_file()


def test_sync_reconstructs_handoff_after_interrupted_manifest_write(
    repo_with_checkpointed_needs_agent_record,
):
    payload = run_brain_json(
        repo_with_checkpointed_needs_agent_record, "sync", services=SERVICES
    )
    assert payload["data"]["handoffs"][0]["source_id"] == durable_source_id(
        repo_with_checkpointed_needs_agent_record
    )


def test_manifest_publication_failure_precedes_staged_sync_result(
    repo_with_agent_sources, monkeypatch
):
    import brainlib.extractors.handoff as module

    original = module.write_handoff_manifest
    captured = []

    def interrupted(*args, **kwargs):
        captured.extend(kwargs["items"])
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, "write_handoff_manifest", interrupted)
    with pytest.raises(KeyboardInterrupt):
        commands.init_sources(repo_with_agent_sources, services=SERVICES)
    paths = RepoPaths.discover(repo_with_agent_sources)
    from brainlib.sync_results import SyncResultStore

    assert SyncResultStore(paths).load_staged() is None
    assert SyncResultStore(paths).load_pending() is None
    assert (
        tuple(LedgerStore(paths).load_all().values())[0].state
        is SourceState.NEEDS_AGENT
    )
    monkeypatch.setattr(module, "write_handoff_manifest", original)
    resumed = commands.init_sources(paths.root, services=SERVICES)
    assert resumed.data["handoffs"][0]["handoff_id"] == captured[0].handoff_id


def test_manifest_includes_all_durable_records_beyond_report_sample(repo_paths):
    for index in range(105):
        job = make_job(repo_paths, f"images/{index}.png", PNG_BYTES, "image/png")
        item = handoff_for_job(
            job,
            kind="extraction",
            reason="complex_image",
            diagnostics=(Diagnostic("agent_required", "Vision judgment required"),),
        )
        LedgerStore(repo_paths).save(record_for_handoff(item, repo_paths))
    result = commands.sync_sources(repo_paths.root, services=SERVICES)
    assert len(result.data["handoff_source_ids"]) <= 100
    assert len(result.data["handoffs"]) == 105


def test_identical_repeated_manifests_and_immutable_run_paths(
    repo_paths, extraction_handoff
):
    first = manifest(repo_paths, extraction_handoff)
    before = first.read_bytes()
    manifest(repo_paths, extraction_handoff)
    manifest(repo_paths, extraction_handoff, "second-run")
    assert (
        load_handoff_item(repo_paths, extraction_handoff.handoff_id)
        == extraction_handoff
    )
    assert first.read_bytes() == before
    with pytest.raises((ValueError, OSError)):
        write_handoff_manifest(
            repo_paths,
            run_id="run-20260904",
            created_at=FIXED_NOW + timedelta(days=1),
            items=(extraction_handoff,),
        )
    assert first.read_bytes() == before
    with pytest.raises(AgentRegistrationError, match="handoff_not_found"):
        load_handoff_item(repo_paths, "hnd_" + "f" * 64)


def test_full_validation_rejects_conflicting_same_handoff_id(repo_with_agent_sources):
    payload = run_brain_json(repo_with_agent_sources, "init", services=SERVICES)
    original = repo_with_agent_sources / payload["data"]["handoff_manifest"]
    document = json.loads(original.read_text())
    document["items"][0]["reason"] = "different"
    original.with_name("conflicting.json").write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    )
    result = run_brain(
        repo_with_agent_sources, "--json", "validate", "--full", services=SERVICES
    )
    assert result.returncode == 1
    assert "handoff_id_collision" in result.stdout


@pytest.mark.parametrize(
    "mutation",
    (
        "id",
        "missing_source",
        "missing_version",
        "extra_field",
        "schema",
        "symlink",
        "duplicate_key",
    ),
)
def test_full_validation_rejects_invalid_manifest_shape_and_links(
    repo_paths, extraction_handoff, mutation
):
    prepare_command(repo_paths, extraction_handoff)
    path = repo_paths.ledger_dir / "handoffs/run-20260904.json"
    doc = json.loads(path.read_text())
    if mutation == "id":
        doc["items"][0]["handoff_id"] = "hnd_" + "f" * 64
    elif mutation in {"missing_source", "missing_version"}:
        fields = doc["items"][0]
        fields["source_id" if mutation == "missing_source" else "content_sha256"] = (
            "src_" if mutation == "missing_source" else ""
        ) + "f" * 64
        fields["handoff_id"] = handoff_id_for(
            {key: value for key, value in fields.items() if key != "handoff_id"}
        )
    elif mutation == "extra_field":
        doc["items"][0]["caller"] = "forged"
    elif mutation == "schema":
        doc["schema_version"] = True
    elif mutation == "symlink":
        alias = path.with_name("alias.json")
        alias.symlink_to(path)
    else:
        path.write_bytes(b'{"schema_version":1,' + path.read_bytes()[1:])
    if mutation not in {"symlink", "duplicate_key"}:
        path.write_text(json.dumps(doc))
    payload = run_brain_json(repo_paths.root, "validate", "--full", services=SERVICES)
    assert not payload["ok"]
    assert "handoff" in json.dumps(payload["errors"])


@pytest.mark.parametrize(
    "change", ("registry", "prerequisite", "version", "raw", "media")
)
def test_registration_checks_current_recipe_and_source_under_lock(
    repo_paths, extraction_handoff, staging_markdown, change
):
    prepare_command(repo_paths, extraction_handoff)
    services = SERVICES
    if change in {"registry", "version"}:
        body = repo_paths.registry.read_text()
        if change == "registry":
            body = body.replace('agent_revision = "1"', 'agent_revision = "2"')
        else:
            body = body.replace(
                'id = "image"\nversion = "1"', 'id = "image"\nversion = "2"'
            )
        repo_paths.registry.write_text(body)
    elif change == "prerequisite":

        def changed_digest(spec):
            assert repo_paths.lock.exists()
            return "f" * 64

        services = replace(services, prerequisite_digest=changed_digest)
    else:
        record = LedgerStore(repo_paths).load(extraction_handoff.source_id)
        LedgerStore(repo_paths).save(
            replace(
                record,
                **(
                    {
                        "current_raw_path": record.current_raw_path.with_name(
                            "renamed.png"
                        ),
                        "previous_raw_paths": (record.current_raw_path,),
                    }
                    if change == "raw"
                    else {"media_type": "image/jpeg"}
                ),
            )
        )
    result = command_register(
        repo_paths, extraction_handoff, staging_markdown, services=services
    )
    assert not result["ok"]
    if change in {"registry", "version", "prerequisite"}:
        assert "handoff_recipe_stale" in json.dumps(result["errors"])
    assert staging_markdown.exists()
    assert not tuple(repo_paths.extracted.rglob("*.md"))


def test_historical_manifest_remains_valid_after_registry_change(
    repo_paths, extraction_handoff
):
    prepare_command(repo_paths, extraction_handoff)
    repo_paths.registry.write_text(
        repo_paths.registry.read_text().replace(
            'agent_revision = "1"', 'agent_revision = "2"'
        )
    )
    store = LedgerStore(repo_paths)
    store.write_summary(store.load_all().values(), generated_at=FIXED_NOW)
    assert run_brain_json(repo_paths.root, "validate", "--full", services=SERVICES)[
        "ok"
    ]


def test_registration_command_checkpoints_and_removes_staging_then_exactly_replays(
    repo_paths, extraction_handoff, staging_markdown
):
    prepare_command(repo_paths, extraction_handoff)
    body = staging_markdown.read_bytes()
    result = command_register(repo_paths, extraction_handoff, staging_markdown)
    assert result["ok"], result
    registration = result["data"]["registration"]
    assert set(registration) == {
        "source_id",
        "content_sha256",
        "derivation_id",
        "output_path",
        "active_representation",
        "corpus_revision",
    }
    record = LedgerStore(repo_paths).load(extraction_handoff.source_id)
    assert record.state is SourceState.OK
    assert record.active_derivation_id == registration["derivation_id"]
    assert not staging_markdown.exists()
    assert not tuple(repo_paths.ledger_dir.glob("*.activation-pending"))
    shard = repo_paths.ledger_dir / (record.source_id + ".json")
    original = shard.read_bytes()
    staging_markdown.write_bytes(body)
    replay = command_register(repo_paths, extraction_handoff, staging_markdown)
    assert replay["ok"] and replay["data"] == result["data"]
    assert shard.read_bytes() == original
    assert not staging_markdown.exists()


@pytest.mark.parametrize(
    "failure",
    ("before", "after", "repeated", "interrupt", "staging_swap", "output_swap"),
)
def test_registration_checkpoint_failure_stays_guarded_and_preserves_staging(
    repo_paths, extraction_handoff, staging_markdown, monkeypatch, failure
):
    prepare_command(repo_paths, extraction_handoff)
    original_save = LedgerStore.save
    active_writes = []

    def fail(store, record):
        if record.active_derivation_id:
            active_writes.append(record)
            guard = ActivationGuard.load(repo_paths, record.source_id)
            assert guard.candidate == record
            with pytest.raises(ValueError, match="activation"):
                LedgerStore(repo_paths).load_all()
            if failure == "before":
                raise OSError("before candidate checkpoint")
            result = original_save(store, record)
            if failure == "interrupt":
                raise KeyboardInterrupt()
            if failure == "staging_swap":
                staging_markdown.write_text("changed during checkpoint")
                return result
            if failure == "output_swap":
                (
                    repo_paths.root
                    / record.derivations[record.active_derivation_id].output_path
                ).write_text("changed during checkpoint")
                return result
            raise OSError("after candidate checkpoint")
        if active_writes and failure == "repeated":
            raise OSError("rollback checkpoint failure")
        return original_save(store, record)

    monkeypatch.setattr(LedgerStore, "save", fail)
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            command_register(repo_paths, extraction_handoff, staging_markdown)
    else:
        result = command_register(repo_paths, extraction_handoff, staging_markdown)
        assert not result["ok"]
    assert active_writes
    assert staging_markdown.exists()
    if failure == "repeated":
        with pytest.raises(ValueError, match="activation"):
            LedgerStore(repo_paths).load_all()
        monkeypatch.setattr(LedgerStore, "save", original_save)
        from brainlib.locking import SourceWriteLock

        with SourceWriteLock.acquire(repo_paths.lock):
            LedgerStore(repo_paths).recover_activation_guards()
    record = LedgerStore(repo_paths).load(extraction_handoff.source_id)
    assert record.active_derivation_id is None
    assert record.state is SourceState.EXTRACTING


def test_registration_cannot_clear_guard_when_rollback_does_not_persist(
    repo_paths, extraction_handoff, staging_markdown, monkeypatch
):
    prepare_command(repo_paths, extraction_handoff)
    original = LedgerStore.save
    candidate_written = False

    def silently_skip_rollback(store, record):
        nonlocal candidate_written
        if record.active_derivation_id:
            candidate_written = True
            original(store, record)
            raise OSError("checkpoint failed after publication")
        if candidate_written:
            return store.paths.ledger_dir / (record.source_id + ".json")
        return original(store, record)

    monkeypatch.setattr(LedgerStore, "save", silently_skip_rollback)
    assert not command_register(repo_paths, extraction_handoff, staging_markdown)["ok"]
    assert staging_markdown.exists()
    with pytest.raises(ValueError, match="activation"):
        LedgerStore(repo_paths).load_all()


def test_unavailable_image_handoff_does_not_invent_complexity(repo_with_agent_sources):
    result = run_brain_json(repo_with_agent_sources, "init", services=SERVICES)
    assert result["data"]["handoffs"][0]["reason"] == "extractor_processor_unavailable"


def test_production_handoff_for_job_rejects_invalid_agent_revision(repo_paths):
    from brainlib.extractors.handoff import handoff_for_job as production_handoff

    job = make_job(repo_paths, "images/diagram.png", PNG_BYTES, "image/png")
    for revision in (None, "0", "01", "", "invalid"):
        changed = replace(
            job, extractor=replace(job.extractor, agent_revision=revision)
        )
        with pytest.raises(AgentRegistrationError, match="revision"):
            production_handoff(
                changed, kind="extraction", reason="complex_image", diagnostics=()
            )


def test_registration_uses_only_staging_and_published_bytes(
    repo_paths, extraction_handoff, staging_markdown
):
    prepare_command(repo_paths, extraction_handoff)
    (repo_paths.raw / extraction_handoff.raw_path).unlink()
    assert command_register(repo_paths, extraction_handoff, staging_markdown)["ok"]


def test_registration_with_bumped_revision_preserves_older_derivation(
    repo_paths, extraction_handoff, staging_markdown
):
    prepare_command(repo_paths, extraction_handoff)
    first = command_register(repo_paths, extraction_handoff, staging_markdown)
    assert first["ok"]
    store = LedgerStore(repo_paths)
    older = store.load(extraction_handoff.source_id)
    old_output = repo_paths.root / first["data"]["registration"]["output_path"]
    old_bytes = old_output.read_bytes()
    repo_paths.registry.write_text(
        repo_paths.registry.read_text().replace(
            'agent_revision = "1"', 'agent_revision = "2"'
        )
    )
    job = make_job(repo_paths, "images/diagram.png", PNG_BYTES, "image/png")
    handoff = handoff_for_job(
        job,
        kind="extraction",
        reason="complex_image",
        diagnostics=extraction_handoff.diagnostics,
    )
    pending = record_for_handoff(handoff, repo_paths)
    store.save(
        replace(
            pending,
            derivations=older.derivations,
            active_derivation_id=older.active_derivation_id,
        )
    )
    manifest(repo_paths, handoff, "revision-two")
    stage = staging_markdown.parent.parent / handoff.handoff_id / "result.md"
    stage.parent.mkdir()
    stage.write_text('<a id="block:image-1"></a>\nUpdated transcription\n')
    second = command_register(repo_paths, handoff, stage)
    assert second["ok"], second
    assert len(store.load(handoff.source_id).derivations) == 2
    assert (
        second["data"]["registration"]["output_path"]
        != first["data"]["registration"]["output_path"]
    )
    assert old_output.read_bytes() == old_bytes


@pytest.mark.parametrize(
    "field", ("extractor_id", "extractor_version", "config_sha256")
)
def test_replay_rejects_retained_recipe_that_disagrees_with_manifest(
    repo_paths, extraction_handoff, staging_markdown, field
):
    record = record_for_handoff(extraction_handoff, repo_paths)
    first = register(repo_paths, extraction_handoff, staging_markdown, record)
    active = activate_derivation(record, first.derivation, now=FIXED_NOW)
    forged = replace(
        first.derivation, **{field: "f" * 64 if field == "config_sha256" else "forged"}
    )
    active = replace(active, derivations={forged.derivation_id: forged})
    with pytest.raises(AgentRegistrationError, match="replay"):
        register(repo_paths, extraction_handoff, staging_markdown, active)


def test_conflicting_numeric_diagnostic_values_are_not_identical_manifests(
    repo_paths, extraction_handoff
):
    fields = handoff_to_dict(extraction_handoff)
    fields.pop("handoff_id")
    fields["diagnostics"][0]["details"] = {"count": 1}
    item = replace(
        extraction_handoff,
        handoff_id=handoff_id_for(fields),
        diagnostics=(
            Diagnostic(
                "agent_required", "Vision judgment required", details={"count": 1}
            ),
        ),
    )
    path = manifest(repo_paths, item)
    document = json.loads(path.read_text())
    document["items"][0]["diagnostics"][0]["details"]["count"] = True
    path.with_name("different.json").write_text(json.dumps(document))
    with pytest.raises(AgentRegistrationError, match="handoff_id_collision"):
        load_handoff_item(repo_paths, item.handoff_id)


def test_registration_recovery_cannot_erase_guard_without_rollback_checkpoint(
    repo_paths, extraction_handoff, staging_markdown, monkeypatch
):
    prepare_command(repo_paths, extraction_handoff)
    original = LedgerStore.save
    candidate_seen = False

    def fail_after_candidate(store, record):
        nonlocal candidate_seen
        if record.active_derivation_id:
            candidate_seen = True
            original(store, record)
            raise OSError("candidate durability proof failed")
        if candidate_seen:
            raise OSError("rollback persistence failed")
        return original(store, record)

    monkeypatch.setattr(LedgerStore, "save", fail_after_candidate)
    assert not command_register(repo_paths, extraction_handoff, staging_markdown)["ok"]
    guard = next(repo_paths.ledger_dir.glob("*.activation-pending"))
    evidence = guard.read_bytes()
    shard = repo_paths.ledger_dir / (extraction_handoff.source_id + ".json")
    candidate_bytes = shard.read_bytes()
    monkeypatch.setattr(LedgerStore, "save", lambda _store, _record: shard)
    recovered = command_register(repo_paths, extraction_handoff, staging_markdown)
    assert not recovered["ok"]
    assert guard.read_bytes() == evidence
    assert shard.read_bytes() == candidate_bytes
    assert staging_markdown.exists()
    with pytest.raises(ValueError, match="activation"):
        LedgerStore(repo_paths).load_all()
