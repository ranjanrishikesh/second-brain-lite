import shutil
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable
import re

import pytest

from tests.helpers import (
    FIXED_NOW as FIXED_NOW,
    make_retrieval_metadata as make_retrieval_metadata,
    make_source_record as make_source_record,
    write_bytes as write_bytes,
)

sys.modules.setdefault("conftest", sys.modules[__name__])

if TYPE_CHECKING:
    from brainlib.extractors.handoff import HandoffItem
    from brainlib.extractors.processor import DeterministicSourceProcessor, Job
    from brainlib.layout import RepoPaths


def _require_fixture_receipt_parents(root: Path, relative: Path, *, create: bool = False) -> None:
    """Check each directory before advancing; never traverse a directory link."""
    directories = [root]
    for component in relative.parts:
        directories.append(directories[-1] / component)
    for directory in directories:
        try:
            if create:
                directory.mkdir(exist_ok=True)
            metadata = directory.lstat()
        except OSError as error:
            raise ValueError(f"unsafe fixture receipt ancestor: {directory}") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"unsafe fixture receipt ancestor: {directory}")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "second-brain-lite"
    fixture_receipt = Path(
        "tests/fixtures/wiki/scenarios/workflow/answer/repo/.brain/sync-results/"
        "sync_fcdc3f71961f03ac1de80bc4c7408f91195c067be42e6d34d7c1c77e24e65f70.jsonl"
    )
    _require_fixture_receipt_parents(source_root, fixture_receipt.parent)
    source_receipt = source_root / fixture_receipt
    try:
        receipt_mode = source_receipt.lstat().st_mode
    except OSError as error:
        raise ValueError(f"unsafe fixture receipt leaf: {source_receipt}") from error
    if not (stat.S_ISREG(receipt_mode) or stat.S_ISLNK(receipt_mode)):
        raise ValueError(f"unsafe fixture receipt leaf: {source_receipt}")

    def ignore_live_state(directory: str, names: list[str]) -> set[str]:
        ignored = {
            name
            for name in names
            if name
            in {
                ".git",
                ".context",
                ".brain",
                ".pytest_cache",
                "__pycache__",
                ".coverage",
            }
        }
        if Path(directory) == source_root:
            ignored.update({"sources", "wiki"} & set(names))
        return ignored

    shutil.copytree(
        source_root,
        destination,
        symlinks=True,
        ignore=ignore_live_state,
    )
    copied_receipt = destination / fixture_receipt
    _require_fixture_receipt_parents(destination, fixture_receipt.parent, create=True)
    shutil.copy2(source_receipt, copied_receipt, follow_symlinks=False)
    for relative in (
        "sources/raw/_versions",
        "sources/raw/_web",
        "sources/extracted",
        "sources/ledger",
        "wiki/pages",
        "wiki/questions",
    ):
        directory = destination / relative
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").touch()
    (destination / "sources/ledger.md").write_text(
        "# Source Ledger\n\nNot initialized. Run `./brain init`.\n", encoding="utf-8"
    )
    (destination / "wiki/index.md").write_text(
        "# Second Brain Lite\n\n## Pages\n\n## Questions\n", encoding="utf-8"
    )
    return destination


def _fixture_resolve(extractor):
    """Resolve only the deterministic fixture adapters; never probe the host."""

    from dataclasses import replace

    from tests.helpers_extractors import resolve_test_converter

    if extractor.extractor_id == "image":
        return None
    converter_id = {
        "html": "builtin.html",
        "pptx": "libreoffice",
        "xlsx": "libreoffice",
    }.get(extractor.extractor_id, extractor.preferred.converter_id)
    converter = next(
        item
        for item in (extractor.preferred, *extractor.fallbacks)
        if item.converter_id == converter_id
    )
    return resolve_test_converter(replace(extractor, preferred=converter))


def _fixture_prerequisite_digest(extractor) -> str:
    import hashlib

    resolved = _fixture_resolve(extractor)
    if resolved is not None:
        return resolved.prerequisite_digest
    return hashlib.sha256(b"converter-v1\0unavailable\0image").hexdigest()


def _fixture_command_runner(
    argv,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    pass_fds=(),
):
    """Produce fixed outputs for registered optional adapters in the matrix."""

    del cwd, timeout_seconds, max_output_bytes
    from brainlib.extractors.processor import CommandExecution

    assert len(pass_fds) == 1
    command = argv[0]
    if command == "pdftotext":
        source, output = Path(argv[-2]), Path(argv[-1])
        if b"/Encrypt" in source.read_bytes():
            return CommandExecution(1, b"", b"encrypted fixture")
        output.write_text("Fixture PDF page\n", encoding="utf-8")
        return CommandExecution(0, b"", b"")
    if command == "pandoc":
        import zipfile

        output_index = argv.index("--output")
        source, output = Path(argv[output_index - 1]), Path(argv[output_index + 1])
        try:
            with zipfile.ZipFile(source) as document:
                document.read("word/document.xml")
        except (KeyError, OSError, zipfile.BadZipFile):
            return CommandExecution(1, b"", b"malformed fixture")
        output.write_text("Fixture document section\n", encoding="utf-8")
        return CommandExecution(0, b"", b"")
    if command == "soffice":
        format_index = argv.index("--convert-to")
        outdir_index = argv.index("--outdir")
        output = Path(argv[outdir_index + 1])
        source = Path(argv[outdir_index + 2])
        assert output.is_dir() and source.read_bytes().startswith(b"PK\x03\x04")
        if argv[format_index + 1] == "txt":
            (output / "fixture.txt").write_text(
                "Fixture presentation slide\n", encoding="utf-8"
            )
        elif argv[format_index + 1] == "csv":
            (output / "Fixture sheet.csv").write_text(
                "Name,Value\nFixture,1\n", encoding="utf-8"
            )
        else:
            raise AssertionError(f"unexpected LibreOffice fixture format: {argv!r}")
        return CommandExecution(0, b"", b"")
    raise AssertionError(f"unexpected fixture converter command: {argv!r}")


@pytest.fixture
def fixture_services():
    from brainlib.commands import CommandServices
    from brainlib.extractors.processor import DeterministicSourceProcessor

    return CommandServices(
        processor_factory=lambda: DeterministicSourceProcessor(
            run=_fixture_command_runner,
            resolve=_fixture_resolve,
        ),
        prerequisite_digest=_fixture_prerequisite_digest,
    )


@pytest.fixture
def initialized_repo(repo_root: Path, fixture_services) -> Path:
    """Start each matrix case from an acknowledged, empty template copy."""

    from tests.helpers_extractors import run_brain_json

    fixture_content = repo_root / "sources/raw/fixtures"
    shutil.rmtree(fixture_content, ignore_errors=True)
    initialized = run_brain_json(repo_root, "init", services=fixture_services)
    assert initialized["ok"], initialized
    reference = initialized["data"]["result_manifest"]
    acknowledgement = run_brain_json(
        repo_root,
        "source",
        "acknowledge-sync-result",
        "--result-id",
        reference["result_id"],
        services=fixture_services,
    )
    assert acknowledgement["ok"], acknowledgement
    return repo_root


def install_fixture(repo_root: Path, relative: str) -> Path:
    """Copy exactly one versioned generator fixture into the test corpus."""

    source = Path(__file__).resolve().parent / "fixtures/extractors" / relative
    assert source.is_file(), source
    destination = repo_root / "sources/raw/fixtures" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def record_for_fixture(repo_root: Path, name: str) -> dict:
    """Return the one canonical ledger shard for a fixture raw path."""

    import json

    matches = []
    for path in sorted((repo_root / "sources/ledger").glob("src_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("current_raw_path") == f"fixtures/{name}":
            matches.append(record)
    assert len(matches) == 1, f"expected one ledger record for fixtures/{name}"
    return matches[0]


def active_representation(record: dict) -> dict:
    """Adapt a ledger shard's active derivation to the matrix representation view."""

    identifier = record["active_derivation_id"]
    assert isinstance(identifier, str) and identifier
    derivation = record["derivations"][identifier]
    assert isinstance(derivation, dict)
    return {**derivation, "extracted_path": derivation["output_path"]}


@pytest.fixture
def repo_paths(repo_root: Path) -> "RepoPaths":
    # Import RepoPaths inside the fixture so Task 1's initially failing test can
    # be written before Task 4 creates brainlib.layout.
    from brainlib.layout import RepoPaths

    return RepoPaths.discover(repo_root)


@pytest.fixture
def streaming_rg_recorder():
    from tests.helpers_knowledge import ContentAwareRgRecorder

    return ContentAwareRgRecorder()


@pytest.fixture
def active_ledger_factory(repo_paths):
    from tests.helpers_knowledge import make_active_ledger

    return lambda count: make_active_ledger(repo_paths, count, matching_numbers=set(range(count)))


@pytest.fixture
def one_active_ledger(active_ledger_factory):
    return active_ledger_factory(1)


@pytest.fixture
def recursive_json_decoder(monkeypatch):
    # Exercise the stdlib's recursive fallback parser as well as the installed
    # C accelerator, which can handle deeper JSON on newer Python versions.
    import json
    import json.scanner

    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    monkeypatch.setattr(json, "_default_decoder", decoder)
    previous = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        yield
    finally:
        sys.setrecursionlimit(previous)


@pytest.fixture
def scenario_repo(repo_root: Path) -> Callable[[str], object]:
    """Install one complete committed knowledge-scenario overlay per test."""

    from brainlib.layout import RepoPaths
    from brainlib.ledger import LedgerStore
    from tests.helpers_knowledge import KnowledgeScenario, restore_scenario_metadata

    installed = False

    def install(name: str) -> KnowledgeScenario:
        nonlocal installed
        if installed:
            raise RuntimeError("install exactly one knowledge scenario per test")
        if not re.fullmatch(r"[a-z0-9-]+(?:/[a-z0-9-]+)*", name):
            raise ValueError("invalid scenario name")
        overlay = repo_root / "tests/fixtures/wiki/scenarios" / name / "repo"
        if not (overlay / "sources/ledger").is_dir() or not (overlay / "wiki").is_dir():
            raise FileNotFoundError(f"incomplete knowledge scenario: {name}")
        shutil.copytree(overlay, repo_root, dirs_exist_ok=True, symlinks=True)
        paths = RepoPaths.discover(repo_root)
        ledger = LedgerStore(paths)
        records = ledger.load_all()
        restore_scenario_metadata(paths, records)
        installed = True
        return KnowledgeScenario(repo_root, paths, ledger)

    return install


@pytest.fixture(autouse=True)
def close_staged_test_inputs():
    from tests.helpers_extractors import STAGED_TEST_INPUTS

    yield
    while STAGED_TEST_INPUTS:
        STAGED_TEST_INPUTS.pop().close()


@pytest.fixture
def jobs(repo_paths: "RepoPaths") -> list["Job"]:
    from tests.helpers_extractors import make_jobs

    return make_jobs(repo_paths)


@pytest.fixture
def processor() -> "DeterministicSourceProcessor":
    from brainlib.extractors.processor import DeterministicSourceProcessor

    return DeterministicSourceProcessor()


@pytest.fixture
def pdf_job(repo_paths: "RepoPaths") -> "Job":
    from tests.helpers_extractors import make_job

    return make_job(
        repo_paths,
        "papers/sample.pdf",
        b"%PDF-1.4\nfixture\n%%EOF\n",
        "application/pdf",
    )


@pytest.fixture
def pdf_resolved(pdf_job: "Job"):
    from brainlib.registry import ResolvedConverter

    assert pdf_job.extractor.preferred is not None
    return ResolvedConverter(
        converter=pdf_job.extractor.preferred,
        detected_version="fixture-1",
        prerequisite_digest=pdf_job.context.prerequisite_digest,
    )


@pytest.fixture
def recording_run():
    from tests.helpers_extractors import RecordingRun

    return RecordingRun(markdown='<a id="page:1"></a>\nPage one\n')


@pytest.fixture
def extraction_handoff(repo_paths: "RepoPaths") -> "HandoffItem":
    from brainlib.diagnostics import Diagnostic
    from tests.helpers_extractors import PNG_BYTES, handoff_for_job, make_job

    job = make_job(repo_paths, "images/diagram.png", PNG_BYTES, "image/png")
    return handoff_for_job(
        job,
        kind="extraction",
        reason="complex_image",
        diagnostics=(Diagnostic("agent_required", "Vision judgment required"),),
    )


@pytest.fixture
def staging_markdown(
    repo_paths: "RepoPaths", extraction_handoff: "HandoffItem"
) -> Path:
    path = (
        repo_paths.root
        / ".brain/agent-staging"
        / extraction_handoff.handoff_id
        / "result.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<a id="block:image-1"></a>\n## Image 1\nDiagram text\n', encoding="utf-8"
    )
    return path


@pytest.fixture
def repo_with_agent_sources(repo_root: Path) -> Path:
    from tests.helpers_extractors import PNG_BYTES

    target = repo_root / "sources/raw/images/diagram.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_BYTES)
    return repo_root


@pytest.fixture
def repo_with_checkpointed_needs_agent_record(repo_root: Path) -> Path:
    from brainlib.diagnostics import Diagnostic
    from brainlib.layout import RepoPaths
    from brainlib.ledger import LedgerStore
    from tests.helpers_extractors import (
        PNG_BYTES,
        handoff_for_job,
        make_job,
        record_for_handoff,
    )

    paths = RepoPaths.discover(repo_root)
    job = make_job(paths, "images/diagram.png", PNG_BYTES, "image/png")
    handoff = handoff_for_job(
        job,
        kind="extraction",
        reason="complex_image",
        diagnostics=(Diagnostic("agent_required", "Vision judgment required"),),
    )
    LedgerStore(paths).save(record_for_handoff(handoff, paths))
    return repo_root


def durable_source_id(repo_root: Path) -> str:
    import json

    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((repo_root / "sources/ledger").glob("src_*.json"))
    ]
    assert len(documents) == 1
    return str(documents[0]["source_id"])


@pytest.fixture(autouse=True)
def web_tests_prohibit_public_network(request, monkeypatch):
    if request.path.name not in {"test_web_capture.py", "test_url_snapshot_flow.py"}:
        return
    import socket

    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect

    def resolve(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1"}, "public DNS is forbidden in tests"
        return original_resolve(host, *args, **kwargs)

    def connect(sock, address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, (
            "public transport is forbidden in tests"
        )
        return original_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def local_web_server():
    from tests.helpers_extractors import LocalWebServer

    with LocalWebServer() as url:
        yield url


@pytest.fixture
def descriptor_record(repo_paths):
    from tests.helpers_extractors import make_descriptor_record

    return make_descriptor_record(repo_paths)


@pytest.fixture
def captured_descriptor_record(descriptor_record):
    return descriptor_record


@pytest.fixture
def recording_transport(repo_paths):
    from tests.helpers_extractors import mock_web_transport

    return mock_web_transport(repo_paths)


@pytest.fixture
def captured_ok_descriptor_record(descriptor_record, recording_transport, repo_paths):
    from brainlib.ledger import LedgerStore
    from brainlib.sources.web import ApprovalClaim
    from tests.helpers_extractors import run_snapshot_fixture

    run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_first", "first capture", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
    )
    return LedgerStore(repo_paths).load(descriptor_record.source_id)


@pytest.fixture
def processor_spy():
    from unittest.mock import Mock
    from brainlib.sync import SourceProcessor

    return Mock(spec=SourceProcessor)


@pytest.fixture
def records_with_approval_event(descriptor_record, recording_transport, repo_paths):
    from brainlib.ledger import LedgerStore
    from brainlib.sources.web import ApprovalClaim
    from tests.helpers_extractors import run_snapshot_fixture

    run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_existing", "one URL", "user approved"),
        paths=repo_paths,
        transport=recording_transport,
    )
    recording_transport.reset_mock()
    return LedgerStore(repo_paths).load_all()


@pytest.fixture
def rendered_staging_file(repo_paths):
    path = repo_paths.root / ".brain/web-staging/rendered.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"<html><body><h1>Rendered fact</h1><p>Faithful DOM</p></body></html>"
    )
    return path


@pytest.fixture
def descriptor_with_render_handoff(descriptor_record, repo_paths):
    from brainlib.extractors.handoff import (
        collect_durable_handoffs,
        publish_handoff_data,
    )
    from brainlib.ledger import LedgerStore
    from brainlib.registry import ExtractorRegistry
    from brainlib.sources.web import ApprovalClaim
    from tests.helpers_extractors import (
        FIXED_NOW,
        ROUTES,
        _WEB_HANDOFF_PATHS,
        mock_web_transport,
        run_snapshot_fixture,
    )

    run_snapshot_fixture(
        descriptor_record,
        approval=ApprovalClaim("evt_shell", "one URL", "user approved"),
        paths=repo_paths,
        transport=mock_web_transport(
            repo_paths, ROUTES["/render-shell"][2], "text/html", "shell.html"
        ),
    )
    records = LedgerStore(repo_paths).load_all()
    publish_handoff_data(
        repo_paths,
        items=collect_durable_handoffs(
            records, ExtractorRegistry.load(repo_paths.registry)
        ),
        now=FIXED_NOW,
    )
    _WEB_HANDOFF_PATHS[descriptor_record.source_id] = repo_paths
    return records[descriptor_record.source_id]


@pytest.fixture
def render_handoff(repo_paths):
    from brainlib.diagnostics import Diagnostic
    from tests.helpers_extractors import handoff_for_job, make_job

    job = make_job(repo_paths, "fixture.html", b"<html>render</html>", "text/html")
    return handoff_for_job(
        job,
        kind="rendered_web_capture",
        reason="web_rendering_required",
        diagnostics=(Diagnostic("web_rendering_required", "Rendering required"),),
    )
