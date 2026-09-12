from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import brainlib.registry as registry_module
from conftest import write_bytes
from brainlib.commands import doctor
from brainlib.registry import (
    ConverterSpec,
    ExtractorRegistry,
    build_converter_argv,
    detect_converter_version,
    effective_extractor_version,
    prerequisite_digest,
    resolve_converter,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("text", True),
        ("builtin.text", True),
        ("python.python-docx", True),
        ("impossible/id", False),
        ("Upper", False),
        ("nul\0id", False),
        ("", False),
        (7, False),
    ),
)
def test_normalized_identifier_predicate_exposes_the_registry_grammar(
    value: object, expected: bool
) -> None:
    predicate = getattr(registry_module, "is_normalized_identifier", None)

    assert callable(predicate)
    assert predicate(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("builtin.text", True),
        ("builtin.http-capture", True),
        ("builtin.not-implemented", False),
        ("external.converter", False),
        (7, False),
    ),
)
def test_committed_builtin_predicate_exposes_permanent_producer_set(
    value: object, expected: bool
) -> None:
    predicate = getattr(registry_module, "is_committed_builtin_converter_id", None)

    assert callable(predicate)
    assert predicate(value) is expected


def _converter_values(converter: ConverterSpec) -> dict[str, object]:
    return {
        "id": converter.converter_id,
        "executable": converter.executable,
        "argv": converter.argv_template,
        "version_args": converter.version_args,
        "python_distribution": converter.python_distribution,
        "install": dict(converter.install_recipes),
    }


def test_html_recipe_can_emit_a_versioned_rendered_capture_handoff(repo_paths):
    registry = ExtractorRegistry.load(repo_paths.registry)
    html = registry.select("text/html", ".html")
    assert html.agent_fallback is True
    assert html.agent_revision == "1"
    assert html.expected_anchors == ("section",)
    assert html.preferred.converter_id == "pandoc"
    assert tuple(c.converter_id for c in html.fallbacks) == ("builtin.html",)
    assert registry.select("text/plain", ".txt").agent_fallback is False
    assert registry.select("application/pdf", ".pdf").agent_revision == "1"


def _extractor_values(registry: ExtractorRegistry) -> list[dict[str, object]]:
    return [
        {
            "id": extractor.extractor_id,
            "version": extractor.extractor_version,
            "timeout_seconds": extractor.timeout_seconds,
            "max_output_bytes": extractor.max_output_bytes,
            "mimes": extractor.media_types,
            "extensions": extractor.extensions,
            "mode": extractor.mode.value,
            "output_suffix": extractor.output_suffix,
            "anchors": extractor.expected_anchors,
            "agent_fallback": extractor.agent_fallback,
            "agent_revision": extractor.agent_revision,
            "preferred": _converter_values(extractor.preferred),
            "fallbacks": tuple(
                _converter_values(converter) for converter in extractor.fallbacks
            ),
        }
        for extractor in registry.extractors
    ]


class _FakeWindowsJobApi:
    def __init__(
        self,
        *,
        configure_error: OSError | None = None,
        assign_error: OSError | None = None,
    ) -> None:
        self.configure_error = configure_error
        self.assign_error = assign_error
        self.calls: list[tuple[object, ...]] = []

    def create_job(self) -> int:
        self.calls.append(("create",))
        return 101

    def configure_kill_on_close(self, job_handle: int) -> None:
        self.calls.append(("configure-kill-on-close", job_handle))
        if self.configure_error is not None:
            raise self.configure_error

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        self.calls.append(("assign", job_handle, process_handle))
        if self.assign_error is not None:
            raise self.assign_error

    def resume_process(self, process_handle: int) -> None:
        self.calls.append(("resume", process_handle))

    def close_handle(self, job_handle: int) -> None:
        self.calls.append(("close", job_handle))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int, *, timeout: float = 1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.01)
    return not _pid_exists(pid)


def _terminate_controlled_probe_group(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    direct_pid, descendant_pid = (int(value) for value in pid_path.read_text().split())
    for pid in (direct_pid, descendant_pid):
        try:
            process_group = os.getpgid(pid)
        except OSError:
            continue
        if process_group != direct_pid:
            continue
        try:
            os.killpg(direct_pid, signal.SIGKILL)
        except OSError:
            pass
        return


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_controlled_probe_cleanup_does_not_signal_reused_recorded_pids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    direct_pid = 41_001
    descendant_pid = 41_002
    pid_path = tmp_path / "reused-probe-pids"
    pid_path.write_text(f"{direct_pid} {descendant_pid}")
    reused_process_groups = {
        direct_pid: 51_001,
        descendant_pid: 51_002,
    }
    signals: list[tuple[str, int, int]] = []

    def reused_process_group(pid: int) -> int:
        return reused_process_groups[pid]

    def record_group_signal(process_group: int, signal_number: int) -> None:
        signals.append(("group", process_group, signal_number))
        raise ProcessLookupError(process_group)

    def record_process_signal(pid: int, signal_number: int) -> None:
        signals.append(("process", pid, signal_number))

    monkeypatch.setattr(os, "getpgid", reused_process_group)
    monkeypatch.setattr(os, "killpg", record_group_signal)
    monkeypatch.setattr(os, "kill", record_process_signal)

    _terminate_controlled_probe_group(pid_path)

    assert signals == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_controlled_probe_cleanup_signals_group_after_live_descendant_verifies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    direct_pid = 42_001
    descendant_pid = 42_002
    pid_path = tmp_path / "live-probe-pids"
    pid_path.write_text(f"{direct_pid} {descendant_pid}")
    verified_process_groups: set[int] = set()
    signals: list[tuple[str, int, int]] = []

    def recorded_process_group(pid: int) -> int:
        if pid == direct_pid:
            raise ProcessLookupError(pid)
        verified_process_groups.add(direct_pid)
        return direct_pid

    def record_group_signal(process_group: int, signal_number: int) -> None:
        assert process_group in verified_process_groups
        signals.append(("group", process_group, signal_number))

    def record_process_signal(pid: int, signal_number: int) -> None:
        signals.append(("process", pid, signal_number))

    monkeypatch.setattr(os, "getpgid", recorded_process_group)
    monkeypatch.setattr(os, "killpg", record_group_signal)
    monkeypatch.setattr(os, "kill", record_process_signal)

    _terminate_controlled_probe_group(pid_path)

    assert signals == [("group", direct_pid, signal.SIGKILL)]


def test_parsed_registry_exactly_matches_the_approved_allowlist(
    repo_root: Path,
) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")

    assert registry.schema_version == 1
    assert _extractor_values(registry) == [
        {
            "id": "text",
            "version": "2",
            "timeout_seconds": 30,
            "max_output_bytes": 268435456,
            "mimes": ("text/plain", "text/markdown"),
            "extensions": (".txt", ".md", ".markdown"),
            "mode": "python",
            "output_suffix": ".md",
            "anchors": ("line",),
            "agent_fallback": False,
            "agent_revision": None,
            "preferred": {
                "id": "builtin.text",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": None,
                "install": {},
            },
            "fallbacks": (),
        },
        {
            "id": "tabular",
            "version": "1",
            "timeout_seconds": 60,
            "max_output_bytes": 268435456,
            "mimes": ("text/csv", "text/tab-separated-values"),
            "extensions": (".csv", ".tsv"),
            "mode": "python",
            "output_suffix": ".md",
            "anchors": ("row",),
            "agent_fallback": False,
            "agent_revision": None,
            "preferred": {
                "id": "builtin.tabular",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": None,
                "install": {},
            },
            "fallbacks": (),
        },
        {
            "id": "json",
            "version": "1",
            "timeout_seconds": 60,
            "max_output_bytes": 268435456,
            "mimes": (
                "application/json",
                "application/ld+json",
                "application/x-ndjson",
            ),
            "extensions": (".json", ".jsonl", ".ndjson"),
            "mode": "python",
            "output_suffix": ".md",
            "anchors": ("block",),
            "agent_fallback": False,
            "agent_revision": None,
            "preferred": {
                "id": "builtin.json",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": None,
                "install": {},
            },
            "fallbacks": (),
        },
        {
            "id": "html",
            "version": "1",
            "timeout_seconds": 120,
            "max_output_bytes": 268435456,
            "mimes": ("text/html", "application/xhtml+xml"),
            "extensions": (".html", ".htm", ".xhtml"),
            "mode": "command",
            "output_suffix": ".md",
            "anchors": ("section",),
            "agent_fallback": True,
            "agent_revision": "1",
            "preferred": {
                "id": "pandoc",
                "executable": "pandoc",
                "argv": (
                    "--from=html",
                    "--to=gfm",
                    "--wrap=none",
                    "{input}",
                    "--output",
                    "{output}",
                ),
                "version_args": ("--version",),
                "python_distribution": None,
                "install": {
                    "macos": ("brew", "install", "pandoc"),
                    "linux": ("sudo", "apt-get", "install", "pandoc"),
                    "windows": (
                        "winget",
                        "install",
                        "--id",
                        "JohnMacFarlane.Pandoc",
                        "-e",
                    ),
                },
            },
            "fallbacks": (
                {
                    "id": "builtin.html",
                    "executable": None,
                    "argv": ("{input}", "{output}"),
                    "version_args": (),
                    "python_distribution": None,
                    "install": {},
                },
            ),
        },
        {
            "id": "pdf",
            "version": "3",
            "timeout_seconds": 120,
            "max_output_bytes": 536870912,
            "mimes": ("application/pdf",),
            "extensions": (".pdf",),
            "mode": "command",
            "output_suffix": ".md",
            "anchors": ("page",),
            "agent_fallback": True,
            "agent_revision": "1",
            "preferred": {
                "id": "poppler.pdftotext",
                "executable": "pdftotext",
                "argv": ("-layout", "-enc", "UTF-8", "{input}", "{output}"),
                "version_args": ("-v",),
                "python_distribution": None,
                "install": {
                    "macos": ("brew", "install", "poppler"),
                    "linux": ("sudo", "apt-get", "install", "poppler-utils"),
                    "windows": (
                        "winget",
                        "install",
                        "--id",
                        "oschwartz10612.Poppler",
                        "-e",
                    ),
                },
            },
            "fallbacks": (
                {
                    "id": "python.pymupdf",
                    "executable": None,
                    "argv": ("{input}", "{output}"),
                    "version_args": (),
                    "python_distribution": "PyMuPDF",
                    "install": {
                        "macos": ("python3", "-m", "pip", "install", "PyMuPDF"),
                        "linux": ("python3", "-m", "pip", "install", "PyMuPDF"),
                        "windows": (
                            "py",
                            "-3",
                            "-m",
                            "pip",
                            "install",
                            "PyMuPDF",
                        ),
                    },
                },
            ),
        },
        {
            "id": "docx",
            "version": "1",
            "timeout_seconds": 120,
            "max_output_bytes": 268435456,
            "mimes": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            "extensions": (".docx",),
            "mode": "command",
            "output_suffix": ".md",
            "anchors": ("section",),
            "agent_fallback": False,
            "agent_revision": None,
            "preferred": {
                "id": "pandoc",
                "executable": "pandoc",
                "argv": (
                    "--from=docx",
                    "--to=gfm",
                    "--wrap=none",
                    "{input}",
                    "--output",
                    "{output}",
                ),
                "version_args": ("--version",),
                "python_distribution": None,
                "install": {
                    "macos": ("brew", "install", "pandoc"),
                    "linux": ("sudo", "apt-get", "install", "pandoc"),
                    "windows": (
                        "winget",
                        "install",
                        "--id",
                        "JohnMacFarlane.Pandoc",
                        "-e",
                    ),
                },
            },
            "fallbacks": (
                {
                    "id": "python.python-docx",
                    "executable": None,
                    "argv": ("{input}", "{output}"),
                    "version_args": (),
                    "python_distribution": "python-docx",
                    "install": {
                        "macos": (
                            "python3",
                            "-m",
                            "pip",
                            "install",
                            "python-docx",
                        ),
                        "linux": (
                            "python3",
                            "-m",
                            "pip",
                            "install",
                            "python-docx",
                        ),
                        "windows": (
                            "py",
                            "-3",
                            "-m",
                            "pip",
                            "install",
                            "python-docx",
                        ),
                    },
                },
            ),
        },
        {
            "id": "pptx",
            "version": "1",
            "timeout_seconds": 180,
            "max_output_bytes": 268435456,
            "mimes": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            "extensions": (".pptx",),
            "mode": "python",
            "output_suffix": ".md",
            "anchors": ("slide",),
            "agent_fallback": True,
            "agent_revision": "1",
            "preferred": {
                "id": "python.python-pptx",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": "python-pptx",
                "install": {
                    "macos": ("python3", "-m", "pip", "install", "python-pptx"),
                    "linux": ("python3", "-m", "pip", "install", "python-pptx"),
                    "windows": (
                        "py",
                        "-3",
                        "-m",
                        "pip",
                        "install",
                        "python-pptx",
                    ),
                },
            },
            "fallbacks": (
                {
                    "id": "libreoffice",
                    "executable": "soffice",
                    "argv": (
                        "--headless",
                        "--convert-to",
                        "txt",
                        "--outdir",
                        "{output}",
                        "{input}",
                    ),
                    "version_args": ("--version",),
                    "python_distribution": None,
                    "install": {
                        "macos": ("brew", "install", "--cask", "libreoffice"),
                        "linux": ("sudo", "apt-get", "install", "libreoffice"),
                        "windows": (
                            "winget",
                            "install",
                            "--id",
                            "TheDocumentFoundation.LibreOffice",
                            "-e",
                        ),
                    },
                },
            ),
        },
        {
            "id": "xlsx",
            "version": "1",
            "timeout_seconds": 180,
            "max_output_bytes": 536870912,
            "mimes": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            "extensions": (".xlsx",),
            "mode": "python",
            "output_suffix": ".md",
            "anchors": ("sheet", "row"),
            "agent_fallback": False,
            "agent_revision": None,
            "preferred": {
                "id": "python.openpyxl",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": "openpyxl",
                "install": {
                    "macos": ("python3", "-m", "pip", "install", "openpyxl"),
                    "linux": ("python3", "-m", "pip", "install", "openpyxl"),
                    "windows": (
                        "py",
                        "-3",
                        "-m",
                        "pip",
                        "install",
                        "openpyxl",
                    ),
                },
            },
            "fallbacks": (
                {
                    "id": "libreoffice",
                    "executable": "soffice",
                    "argv": (
                        "--headless",
                        "--convert-to",
                        "csv",
                        "--outdir",
                        "{output}",
                        "{input}",
                    ),
                    "version_args": ("--version",),
                    "python_distribution": None,
                    "install": {
                        "macos": ("brew", "install", "--cask", "libreoffice"),
                        "linux": ("sudo", "apt-get", "install", "libreoffice"),
                        "windows": (
                            "winget",
                            "install",
                            "--id",
                            "TheDocumentFoundation.LibreOffice",
                            "-e",
                        ),
                    },
                },
            ),
        },
        {
            "id": "image",
            "version": "1",
            "timeout_seconds": 180,
            "max_output_bytes": 268435456,
            "mimes": ("image/png", "image/jpeg", "image/tiff", "image/webp"),
            "extensions": (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"),
            "mode": "command",
            "output_suffix": ".md",
            "anchors": ("block",),
            "agent_fallback": True,
            "agent_revision": "1",
            "preferred": {
                "id": "tesseract",
                "executable": "tesseract",
                "argv": ("{input}", "stdout", "-l", "eng"),
                "version_args": ("--version",),
                "python_distribution": None,
                "install": {
                    "macos": ("brew", "install", "tesseract"),
                    "linux": ("sudo", "apt-get", "install", "tesseract-ocr"),
                    "windows": (
                        "winget",
                        "install",
                        "--id",
                        "UB-Mannheim.TesseractOCR",
                        "-e",
                    ),
                },
            },
            "fallbacks": (),
        },
        {
            "id": "webpage",
            "version": "1",
            "timeout_seconds": 120,
            "max_output_bytes": 536870912,
            "mimes": ("application/x.second-brain-url-descriptor",),
            "extensions": (".url.md",),
            "mode": "web_capture",
            "output_suffix": ".md",
            "anchors": ("section",),
            "agent_fallback": True,
            "agent_revision": "1",
            "preferred": {
                "id": "builtin.http-capture",
                "executable": None,
                "argv": ("{input}", "{output}"),
                "version_args": (),
                "python_distribution": None,
                "install": {},
            },
            "fallbacks": (),
        },
    ]


def test_registry_covers_all_core_families(repo_root: Path) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")
    covered = {
        media_type for item in registry.extractors for media_type in item.media_types
    }
    assert {
        "text/plain",
        "text/markdown",
        "text/html",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
    } <= covered
    assert [item.extractor_id for item in registry.extractors] == [
        "text",
        "tabular",
        "json",
        "html",
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "image",
        "webpage",
    ]
    assert all(item.output_suffix == ".md" for item in registry.extractors)
    agent_extractors = [item for item in registry.extractors if item.agent_fallback]
    assert {item.extractor_id for item in agent_extractors} == {
        "html",
        "pdf",
        "pptx",
        "image",
        "webpage",
    }
    assert all(item.agent_revision == "1" for item in agent_extractors)
    assert all(
        item.agent_revision is None
        for item in registry.extractors
        if not item.agent_fallback
    )


def test_converter_argv_preserves_metacharacters_as_arguments(tmp_path: Path) -> None:
    converter = ConverterSpec(
        converter_id="poppler",
        executable="pdftotext",
        argv_template=("-layout", "{input}", "{output}"),
        version_args=("-v",),
        python_distribution=None,
        install_recipes={},
    )

    argv = build_converter_argv(
        converter,
        input_path=tmp_path / "a;$(bad).pdf",
        output_path=tmp_path / "out.md",
    )

    assert argv == (
        "pdftotext",
        "-layout",
        str(tmp_path / "a;$(bad).pdf"),
        str(tmp_path / "out.md"),
    )


def test_extractor_config_digest_is_scoped_to_one_entry(
    repo_root: Path, tmp_path: Path
) -> None:
    original_path = repo_root / "config/extractors.toml"
    original = ExtractorRegistry.load(original_path)
    changed_path = tmp_path / "extractors.toml"
    changed_path.write_text(
        original_path.read_text().replace(
            'id = "pdf"\nversion = "3"\ntimeout_seconds = 120',
            'id = "pdf"\nversion = "3"\ntimeout_seconds = 121',
            1,
        )
    )
    changed = ExtractorRegistry.load(changed_path)
    before = {item.extractor_id: item.config_sha256 for item in original.extractors}
    after = {item.extractor_id: item.config_sha256 for item in changed.extractors}

    assert before["pdf"] != after["pdf"]
    assert before["text"] == after["text"]


def test_agent_revision_is_approval_gated_and_scoped_into_config_digest(
    repo_root: Path, tmp_path: Path
) -> None:
    original_path = repo_root / "config/extractors.toml"
    original = ExtractorRegistry.load(original_path)
    changed_path = tmp_path / "extractors.toml"
    source = original_path.read_text()
    image_start = source.index('[[extractors]]\nid = "image"')
    webpage_start = source.index('[[extractors]]\nid = "webpage"')
    image_entry = source[image_start:webpage_start].replace(
        'agent_revision = "1"', 'agent_revision = "2"', 1
    )
    changed_path.write_text(source[:image_start] + image_entry + source[webpage_start:])
    changed = ExtractorRegistry.load(changed_path)
    before = {item.extractor_id: item.config_sha256 for item in original.extractors}
    after = {item.extractor_id: item.config_sha256 for item in changed.extractors}

    assert (
        next(
            item for item in changed.extractors if item.extractor_id == "image"
        ).agent_revision
        == "2"
    )
    assert before["image"] != after["image"]
    assert before["pdf"] == after["pdf"]


@pytest.mark.parametrize(
    ("agent_fallback", "agent_revision"),
    ((True, None), (False, "1"), (True, "0"), (True, "01"), (True, "recipe-v1")),
)
def test_agent_revision_contract_is_strict(
    repo_root: Path,
    tmp_path: Path,
    agent_fallback: bool,
    agent_revision: str | None,
) -> None:
    source = (repo_root / "config/extractors.toml").read_text()
    entry = source[
        source.index('[[extractors]]\nid = "text"') : source.index(
            '[[extractors]]\nid = "tabular"'
        )
    ]
    entry = entry.replace(
        "agent_fallback = false",
        f"agent_fallback = {str(agent_fallback).lower()}",
    )
    if agent_revision is not None:
        entry = entry.replace(
            "agent_fallback = " + str(agent_fallback).lower(),
            "agent_fallback = "
            + str(agent_fallback).lower()
            + f'\nagent_revision = "{agent_revision}"',
        )
    path = tmp_path / "extractors.toml"
    path.write_text("schema_version = 1\n\n" + entry)

    with pytest.raises(ValueError, match="agent_revision"):
        ExtractorRegistry.load(path)


def test_detected_converter_version_changes_prerequisite_digest(
    repo_root: Path,
) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")
    pdf = next(item for item in registry.extractors if item.extractor_id == "pdf")
    pdf = replace(pdf, fallbacks=())
    versions = {"pdftotext": "pdftotext version 24.02"}

    def run(
        argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int
    ) -> str:
        assert timeout_seconds == 5
        assert max_output_bytes == 16_384
        return versions[Path(argv[0]).name]

    before = prerequisite_digest(pdf, run=run)
    versions["pdftotext"] = "pdftotext version 24.03"

    assert prerequisite_digest(pdf, run=run) != before


def test_resolution_returns_the_converter_bound_into_the_digest(
    repo_root: Path,
) -> None:
    html = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "html"
    )

    def missing_preferred(
        argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int
    ) -> str:
        raise FileNotFoundError(argv[0])

    resolved = resolve_converter(html, run=missing_preferred)

    assert resolved is not None
    assert resolved.converter.converter_id == "builtin.html"
    assert resolved.detected_version == "builtin:builtin.html:1"
    assert (
        resolved.prerequisite_digest
        == hashlib.sha256(
            b"converter-v1\0builtin.html\0builtin:builtin.html:1"
        ).hexdigest()
    )
    assert resolved.prerequisite_digest == prerequisite_digest(
        html, run=missing_preferred
    )


@pytest.mark.parametrize(
    ("converter_id", "expected"),
    (
        ("builtin.text", "builtin:builtin.text:7"),
        ("builtin.tabular", "builtin:builtin.tabular:7"),
        ("builtin.json", "builtin:builtin.json:7"),
        ("builtin.html", "builtin:builtin.html:7"),
        ("builtin.http-capture", "builtin:builtin.http-capture:7"),
    ),
)
def test_every_committed_builtin_has_a_stable_version(
    converter_id: str, expected: str
) -> None:
    converter = ConverterSpec(
        converter_id=converter_id,
        executable=None,
        argv_template=("{input}", "{output}"),
        version_args=(),
        python_distribution=None,
        install_recipes={},
    )

    assert detect_converter_version(converter, extractor_version="7") == expected


def test_uncommitted_builtin_id_is_rejected_during_registry_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "extractors.toml"
    path.write_text(
        """schema_version = 1
[[extractors]]
id = 'bad'
version = '1'
timeout_seconds = 1
max_output_bytes = 1
mimes = ['application/x-bad']
extensions = ['.bad']
mode = 'python'
output_suffix = '.md'
anchors = ['block']
agent_fallback = false
[extractors.preferred]
id = 'builtin.not-implemented'
argv = ['{input}', '{output}']
"""
    )

    with pytest.raises(ValueError, match="committed builtin"):
        ExtractorRegistry.load(path)


@pytest.mark.parametrize(
    "bad_argv",
    (
        ["bash", "-c", "tool"],
        ["cmd", "/c", "tool"],
        ["powershell", "-Command", "tool"],
        ["{input}", "|", "other"],
        ["{input}", ">", "{output}"],
        ["{input}", "&&", "other"],
        ["$(bad)", "{input}"],
        ["{unknown}", "{input}"],
    ),
)
def test_other_shell_tokens_are_rejected(tmp_path: Path, bad_argv: list[str]) -> None:
    path = tmp_path / "extractors.toml"
    rendered_argv = ", ".join(repr(item) for item in bad_argv)
    path.write_text(
        "[[extractors]]\n"
        "id = 'bad'\n"
        "version = '1'\n"
        "mimes = ['application/pdf']\n"
        "extensions = ['.pdf']\n"
        "mode = 'command'\n"
        "output_suffix = '.md'\n"
        "timeout_seconds = 1\n"
        "max_output_bytes = 1\n"
        "anchors = ['page']\n"
        "agent_fallback = false\n"
        "[extractors.preferred]\n"
        "id = 'bad'\n"
        "executable = 'safe-tool'\n"
        f"argv = [{rendered_argv}]\n"
        "version_args = ['--version']\n"
    )

    with pytest.raises(ValueError, match="shell"):
        ExtractorRegistry.load(path)


def test_invalid_template_with_shell_syntax_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "extractors.toml"
    path.write_text(
        """[[extractors]]
id = 'bad'
version = '1'
mimes = ['application/pdf']
extensions = ['.pdf']
mode = 'command'
output_suffix = '.md'
timeout_seconds = 1
max_output_bytes = 1
anchors = ['page']
[extractors.preferred]
id = 'bad'
executable = 'sh'
argv = ['sh', '-c', 'pdftotext {input} {output}']
version_args = ['--version']
"""
    )

    with pytest.raises(ValueError, match="shell"):
        ExtractorRegistry.load(path)


@pytest.mark.parametrize(
    "scope",
    ("root", "extractor", "converter", "install"),
)
def test_unknown_toml_keys_are_rejected(
    repo_root: Path, tmp_path: Path, scope: str
) -> None:
    source = (repo_root / "config/extractors.toml").read_text()
    if scope == "root":
        source = "unknown = true\n" + source
    elif scope == "extractor":
        source = source.replace('id = "text"', 'id = "text"\nunknown = true', 1)
    elif scope == "converter":
        source = source.replace(
            'id = "builtin.text"', 'id = "builtin.text"\nunknown = true', 1
        )
    else:
        source = source.replace(
            'macos = ["brew", "install", "pandoc"]',
            'unknown = ["no"]\nmacos = ["brew", "install", "pandoc"]',
            1,
        )
    path = tmp_path / "extractors.toml"
    path.write_text(source)

    with pytest.raises(ValueError, match="unknown"):
        ExtractorRegistry.load(path)


def test_selection_prefers_exact_mime_then_longest_extension(repo_root: Path) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")

    by_mime = registry.select(
        detected_media_type="text/plain", path=Path("capture.url.md")
    )
    by_extension = registry.select(
        detected_media_type="application/octet-stream",
        path=Path("capture.url.md"),
    )

    assert by_mime is not None and by_mime.extractor_id == "text"
    assert by_extension is not None and by_extension.extractor_id == "webpage"
    assert by_extension.extensions == (".url.md",)


def test_registry_contracts_are_immutable(repo_root: Path) -> None:
    registry = ExtractorRegistry.load(repo_root / "config/extractors.toml")
    html = next(item for item in registry.extractors if item.extractor_id == "html")

    assert isinstance(registry.extractors, tuple)
    assert isinstance(html.media_types, tuple)
    assert isinstance(html.preferred.argv_template, tuple)
    with pytest.raises(TypeError):
        html.preferred.install_recipes["macos"] = ("poison",)  # type: ignore[index]


def test_version_probe_uses_literal_argv_and_normalizes_output() -> None:
    converter = ConverterSpec(
        converter_id="tool",
        executable="safe-tool",
        argv_template=("{input}", "{output}"),
        version_args=("--version",),
        python_distribution=None,
        install_recipes={},
    )
    calls: list[tuple[tuple[str, ...], int, int]] = []

    def run(
        argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int
    ) -> str:
        calls.append((argv, timeout_seconds, max_output_bytes))
        return "  safe-tool\t1.2.3\n"

    assert (
        detect_converter_version(converter, extractor_version="7", run=run)
        == "safe-tool 1.2.3"
    )
    assert calls == [(("safe-tool", "--version"), 5, 16_384)]


def test_default_version_probe_kills_and_reaps_on_output_overflow(
    repo_root: Path, tmp_path: Path
) -> None:
    pid_path = tmp_path / "probe.pid"
    script = write_bytes(
        tmp_path / "overflow_probe.py",
        (
            "import os\n"
            "import time\n"
            f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "payload = b'x' * 20000\n"
            "while payload:\n"
            "    written = os.write(1, payload)\n"
            "    payload = payload[written:]\n"
            "time.sleep(3)\n"
        ).encode(),
    )
    converter = ConverterSpec(
        converter_id="controlled-overflow",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(str(script),),
        python_distribution=None,
        install_recipes={},
    )
    pdf = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "pdf"
    )
    pdf = replace(pdf, preferred=converter, fallbacks=())

    started = time.monotonic()
    resolved = resolve_converter(pdf)
    elapsed = time.monotonic() - started

    assert pid_path.is_file()
    with pytest.raises(ChildProcessError):
        os.waitpid(int(pid_path.read_text()), os.WNOHANG)
    assert resolved is None
    assert elapsed < 2


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize(
    ("exit_code", "expected_version"),
    ((0, "controlled-version 1"), (9, None)),
)
def test_default_version_probe_terminates_redirected_descendants_after_exit(
    repo_root: Path,
    tmp_path: Path,
    exit_code: int,
    expected_version: str | None,
) -> None:
    pid_path = tmp_path / f"probe-pids-{exit_code}"
    probe = write_bytes(
        tmp_path / f"redirected_descendant_probe_{exit_code}.py",
        (
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            f"pid_path = {str(pid_path)!r}\n"
            "descendant = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "with open(pid_path, 'w', encoding='utf-8') as target:\n"
            "    target.write(f'{os.getpid()} {descendant.pid}')\n"
            "print('controlled-version 1', flush=True)\n"
            f"raise SystemExit({exit_code})\n"
        ).encode(),
    )
    converter = ConverterSpec(
        converter_id=f"controlled-redirected-descendant-{exit_code}",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(str(probe),),
        python_distribution=None,
        install_recipes={},
    )
    pdf = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "pdf"
    )
    pdf = replace(pdf, preferred=converter, fallbacks=())

    cleanup_required = True
    try:
        resolved = resolve_converter(pdf)
        direct_pid, descendant_pid = (
            int(value) for value in pid_path.read_text().split()
        )
        actual_version = None if resolved is None else resolved.detected_version

        assert actual_version == expected_version
        with pytest.raises(ChildProcessError):
            os.waitpid(direct_pid, os.WNOHANG)
        assert _wait_for_pid_exit(descendant_pid)
        cleanup_required = False
    finally:
        if cleanup_required:
            _terminate_controlled_probe_group(pid_path)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_default_version_probe_terminates_inherited_stdout_process_group(
    repo_root: Path, tmp_path: Path
) -> None:
    pid_path = tmp_path / "probe-pids"
    probe = write_bytes(
        tmp_path / "inherited_stdout_probe.py",
        (
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            f"pid_path = {str(pid_path)!r}\n"
            "descendant = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "    stdout=sys.stdout,\n"
            "    stderr=sys.stderr,\n"
            ")\n"
            "with open(pid_path, 'w', encoding='utf-8') as target:\n"
            "    target.write(f'{os.getpid()} {descendant.pid}')\n"
            "os.write(1, b'controlled-version 1\\n')\n"
        ).encode(),
    )
    controller = write_bytes(
        tmp_path / "probe_controller.py",
        (
            "import sys\n"
            "from dataclasses import replace\n"
            "from pathlib import Path\n"
            "root = Path(sys.argv[1])\n"
            "sys.path.insert(0, str(root))\n"
            "from brainlib.registry import ConverterSpec, ExtractorRegistry, resolve_converter\n"
            "converter = ConverterSpec(\n"
            "    converter_id='controlled-inherited-stdout',\n"
            "    executable=sys.executable,\n"
            "    argv_template=('{input}', '{output}'),\n"
            "    version_args=(sys.argv[2],),\n"
            "    python_distribution=None,\n"
            "    install_recipes={},\n"
            ")\n"
            "pdf = next(\n"
            "    item for item in ExtractorRegistry.load(root / 'config/extractors.toml').extractors\n"
            "    if item.extractor_id == 'pdf'\n"
            ")\n"
            "resolved = resolve_converter(replace(pdf, preferred=converter, fallbacks=()))\n"
            "print('unavailable' if resolved is None else 'available', flush=True)\n"
        ).encode(),
    )
    controller_process = subprocess.Popen(
        [sys.executable, str(controller), str(repo_root), str(probe)],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    started = time.monotonic()
    cleanup_required = True
    try:
        try:
            stdout, stderr = controller_process.communicate(timeout=6.5)
        except subprocess.TimeoutExpired:
            os.killpg(controller_process.pid, signal.SIGKILL)
            controller_process.wait(timeout=2)
            pytest.fail("version resolution exceeded its five-second deadline")

        elapsed = time.monotonic() - started
        assert controller_process.returncode == 0, stderr
        assert stdout.strip() == "unavailable"
        assert elapsed < 6
        direct_pid, descendant_pid = (
            int(value) for value in pid_path.read_text().split()
        )
        assert _wait_for_pid_exit(direct_pid)
        assert _wait_for_pid_exit(descendant_pid)
        cleanup_required = False
    finally:
        if controller_process.poll() is None:
            os.killpg(controller_process.pid, signal.SIGKILL)
            controller_process.wait(timeout=2)
        if cleanup_required:
            _terminate_controlled_probe_group(pid_path)


def test_default_version_probe_cleans_up_when_reader_start_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = write_bytes(
        tmp_path / "long_probe.py",
        b"import time\ntime.sleep(30)\n",
    )
    converter = ConverterSpec(
        converter_id="controlled-thread-start-failure",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(str(script),),
        python_distribution=None,
        install_recipes={},
    )
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        return process

    def fail_reader_start(_thread: object) -> None:
        raise RuntimeError("controlled reader startup failure")

    monkeypatch.setattr("brainlib.registry.subprocess.Popen", tracking_popen)
    monkeypatch.setattr("brainlib.registry.threading.Thread.start", fail_reader_start)

    process: subprocess.Popen[bytes] | None = None
    try:
        with pytest.raises(RuntimeError, match="controlled reader startup failure"):
            detect_converter_version(converter, extractor_version="1")

        assert len(processes) == 1
        process = processes[0]
        assert process.poll() is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert process.stdout is not None and process.stdout.closed
    finally:
        if process is None and processes:
            process = processes[0]
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()


def test_default_version_probe_does_not_require_nonblocking_pipe_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = write_bytes(
        tmp_path / "portable_probe.py",
        b"print('portable-version 1')\n",
    )
    converter = ConverterSpec(
        converter_id="controlled-blocking-reader",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(str(probe),),
        python_distribution=None,
        install_recipes={},
    )

    def unsupported_set_blocking(*_args: object) -> None:
        raise AssertionError("Windows Python 3.11 pipes do not support set_blocking")

    monkeypatch.setattr(registry_module.os, "set_blocking", unsupported_set_blocking)

    assert (
        detect_converter_version(converter, extractor_version="1")
        == "portable-version 1"
    )


def test_bounded_wait_timeout_transfers_process_to_daemon_reaper() -> None:
    reaped = threading.Event()

    class DelayedFakeProcess:
        def __init__(self) -> None:
            self.wait_timeouts: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(["controlled-probe"], timeout)
            reaped.set()
            return -9

    process = DelayedFakeProcess()

    registry_module._bounded_wait_or_reap_later(  # type: ignore[attr-defined]
        process,  # type: ignore[arg-type]
        deadline=time.monotonic() + 0.1,
    )

    assert reaped.wait(timeout=1)
    assert len(process.wait_timeouts) == 2
    assert process.wait_timeouts[0] is not None
    assert 0 <= process.wait_timeouts[0] <= 0.1
    assert process.wait_timeouts[1] is None


def test_windows_job_sets_kill_on_close_assigns_process_and_closes() -> None:
    api = _FakeWindowsJobApi()

    owner = registry_module._WindowsProbeTree.create(  # type: ignore[attr-defined]
        api,
        creation_flags=516,
    )
    assert owner.popen_options == {"creationflags": 516}

    owner.attach(SimpleNamespace(_handle=202))
    owner.terminate_tree()
    owner.terminate_tree()

    assert api.calls == [
        ("create",),
        ("configure-kill-on-close", 101),
        ("assign", 101, 202),
        ("resume", 202),
        ("close", 101),
    ]


def test_windows_job_setup_failure_closes_handle_and_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    api = _FakeWindowsJobApi(configure_error=OSError("controlled setup failure"))

    def create_failing_owner() -> object:
        return registry_module._WindowsProbeTree.create(  # type: ignore[attr-defined]
            api,
            creation_flags=512,
        )

    monkeypatch.setattr(
        registry_module,
        "_create_probe_tree",
        create_failing_owner,
        raising=False,
    )
    converter = ConverterSpec(
        converter_id="controlled-job-setup-failure",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(
            str(write_bytes(tmp_path / "must_not_run.py", b"print('must not run')\n")),
        ),
        python_distribution=None,
        install_recipes={},
    )
    pdf = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "pdf"
    )

    assert resolve_converter(replace(pdf, preferred=converter, fallbacks=())) is None
    assert api.calls == [
        ("create",),
        ("configure-kill-on-close", 101),
        ("close", 101),
    ]


def test_windows_job_assignment_failure_cleans_child_stream_and_handle(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, tmp_path: Path
) -> None:
    api = _FakeWindowsJobApi(assign_error=OSError("controlled assignment failure"))
    owner = registry_module._WindowsProbeTree.create(  # type: ignore[attr-defined]
        api,
        creation_flags=0,
    )
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracking_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        process._handle = 202  # type: ignore[attr-defined]
        processes.append(process)
        return process

    monkeypatch.setattr(registry_module, "_create_probe_tree", lambda: owner)
    monkeypatch.setattr(registry_module.subprocess, "Popen", tracking_popen)
    converter = ConverterSpec(
        converter_id="controlled-job-assignment-failure",
        executable=sys.executable,
        argv_template=("{input}", "{output}"),
        version_args=(
            str(
                write_bytes(
                    tmp_path / "long_assignment_probe.py",
                    b"import time\ntime.sleep(30)\n",
                )
            ),
        ),
        python_distribution=None,
        install_recipes={},
    )
    pdf = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "pdf"
    )

    process: subprocess.Popen[bytes] | None = None
    try:
        assert (
            resolve_converter(replace(pdf, preferred=converter, fallbacks=())) is None
        )
        process = processes[0]
        assert process.poll() is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert process.stdout is not None and process.stdout.closed
        assert api.calls == [
            ("create",),
            ("configure-kill-on-close", 101),
            ("assign", 101, 202),
            ("close", 101),
        ]
    finally:
        owner.terminate_tree()
        if process is None and processes:
            process = processes[0]
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()


def test_unavailable_prerequisite_and_effective_version_have_exact_identity(
    repo_root: Path,
) -> None:
    pdf = next(
        item
        for item in ExtractorRegistry.load(
            repo_root / "config/extractors.toml"
        ).extractors
        if item.extractor_id == "pdf"
    )
    pdf = replace(pdf, fallbacks=())

    def missing(
        argv: tuple[str, ...], *, timeout_seconds: int, max_output_bytes: int
    ) -> str:
        raise FileNotFoundError(argv[0])

    digest = prerequisite_digest(pdf, run=missing)

    assert digest == hashlib.sha256(b"converter-v1\0unavailable\0pdf").hexdigest()
    assert effective_extractor_version(pdf, digest) == f"3+{digest}"


def test_doctor_validates_registry_without_running_version_probes(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("doctor must not probe converters")

    monkeypatch.setattr("brainlib.registry._run_version_probe", forbidden)

    result = doctor(repo_root)

    assert result.ok is True
    assert result.data["extractor_registry"] == {
        "path": "config/extractors.toml",
        "present": True,
        "valid": True,
    }
    assert "extractor_registry_invalid" not in {
        warning.code for warning in result.warnings
    }


def test_doctor_discovers_registry_from_nested_repository_directory(
    repo_root: Path,
) -> None:
    nested = repo_root / "sources/raw/notes"
    nested.mkdir(parents=True)

    result = doctor(nested)

    assert result.data["extractor_registry"] == {
        "path": "config/extractors.toml",
        "present": True,
        "valid": True,
    }
    assert "extractor_registry_missing" not in {
        warning.code for warning in result.warnings
    }


def test_doctor_reports_invalid_registry_with_isolated_diagnostics(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config/extractors.toml"
    config.parent.mkdir(parents=True)
    config.write_text("schema_version = 1\nunknown = true\n")

    first = doctor(tmp_path)
    config.write_text("schema_version = 1\nextractors = []\n")
    second = doctor(tmp_path)

    assert first.data["extractor_registry"] == {
        "path": "config/extractors.toml",
        "present": True,
        "valid": False,
    }
    assert [warning.code for warning in first.warnings] == [
        "extractor_registry_invalid"
    ]
    assert second.data["extractor_registry"] == {
        "path": "config/extractors.toml",
        "present": True,
        "valid": True,
    }
    assert second.warnings == ()
