from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from brainlib.cli import main
from brainlib.registry import ExtractorRegistry
from tests.helpers_extractors import run_brain, run_brain_json


def test_doctor_reports_missing_optional_tool_without_installing(
    repo_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("brainlib.commands.shutil.which", lambda _: None)
    stdout = io.StringIO()
    assert main(["--json", "doctor"], cwd=repo_root, stdout=stdout) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["command"] == "doctor"
    assert payload["ok"] is True
    assert payload["data"]["capabilities"]["pdftotext"]["available"] is False
    assert payload["data"]["capabilities"]["pdftotext"]["install_recipes"]


def test_capabilities_probe_versions_and_copy_install_recipes(
    repo_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brainlib.capabilities import probe_capabilities

    monkeypatch.setattr(
        "brainlib.capabilities.shutil.which", lambda name: f"/tools/{name}"
    )
    monkeypatch.setattr(
        "brainlib.capabilities.importlib.util.find_spec", lambda name: None
    )

    def version(argv, *, timeout_seconds, max_output_bytes):
        assert argv[1:] in {("--version",), ("-v",)}
        assert timeout_seconds == 5
        assert max_output_bytes == 16384
        return f"{argv[0]}  24.0\n"

    registry = ExtractorRegistry.load(repo_paths.registry)
    capabilities = probe_capabilities(registry, run=version)
    pdf = capabilities["poppler.pdftotext"]
    assert pdf.available is True
    assert pdf.detected_version == "pdftotext 24.0"
    assert pdf.install_recipes["macos"] == ("brew", "install", "poppler")
    assert capabilities["python.python-docx"].available is False
    assert capabilities["builtin.text"].available is True


def test_python_capability_requires_importable_module_and_distribution_version(
    repo_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brainlib.capabilities import probe_capabilities

    monkeypatch.setattr("brainlib.capabilities.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "brainlib.capabilities.importlib.util.find_spec",
        lambda name: object() if name == "docx" else None,
    )
    monkeypatch.setattr(
        "brainlib.registry.metadata.version", lambda distribution: "1.2.3"
    )
    capabilities = probe_capabilities(
        ExtractorRegistry.load(repo_paths.registry),
        run=lambda *args, **kwargs: pytest.fail("missing executables must not run"),
    )
    assert capabilities["python.python-docx"].detected_version == "1.2.3"
    assert capabilities["python.python-docx"].available is True
    assert capabilities["python.openpyxl"].available is False


@pytest.mark.parametrize("command", ["init", "sync"])
@pytest.mark.parametrize("workers", ["0", "17", "nope"])
def test_cli_rejects_invalid_worker_count(
    repo_root: Path, command: str, workers: str
) -> None:
    result = run_brain(repo_root, "--json", command, "--max-workers", workers)
    assert result.returncode == 2
    assert "1..16" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("command", ["init", "sync"])
def test_cli_accepts_worker_count(repo_root: Path, command: str) -> None:
    payload = run_brain_json(repo_root, command, "--max-workers", "2")
    assert payload["ok"] is True
