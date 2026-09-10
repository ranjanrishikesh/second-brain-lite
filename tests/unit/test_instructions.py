import io
import json
import os
from pathlib import Path
import signal

import pytest

import brainlib.instructions as instructions
from brainlib.cli import main
from brainlib.instructions import (
    FrontmatterError,
    load_canonical_skill,
    load_skill,
    parse_scalar_frontmatter,
)
from brainlib.layout import RepoPaths
from brainlib.validators import validate_instruction_architecture


def break_instruction_topology(repo_root: Path) -> RepoPaths:
    canonical = repo_root / ".agents/skills/brain-answer/SKILL.md"
    if canonical.is_file():
        canonical.unlink()
    claude_link = repo_root / ".claude/skills/brain-answer"
    if claude_link.is_symlink() or claude_link.exists():
        claude_link.unlink()
    return RepoPaths.discover(repo_root)


def test_parse_scalar_frontmatter_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: one\nname: two\n---\nBody\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="duplicate key: name"):
        parse_scalar_frontmatter(path)


def test_parse_scalar_frontmatter_rejects_quoted_empty_scalar(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: valid\ndescription: \"\"\n---\nBody\n", encoding="utf-8"
    )
    with pytest.raises(FrontmatterError, match="only nonempty scalar"):
        parse_scalar_frontmatter(path)


def test_load_skill_requires_matching_directory_name(tmp_path: Path) -> None:
    skill_dir = tmp_path / "brain-answer"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text("---\nname: wrong\ndescription: x\n---\nBody\n", encoding="utf-8")
    with pytest.raises(FrontmatterError, match="must match directory"):
        load_skill(tmp_path, path)


def test_load_canonical_skill_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    external = tmp_path / "brain-answer"
    external.mkdir()
    skill = external / "SKILL.md"
    skill.write_text(
        "---\nname: brain-answer\ndescription: x\n---\nExternal body\n",
        encoding="utf-8",
    )

    with pytest.raises(FrontmatterError, match="canonical skill path is invalid"):
        load_canonical_skill(root, root / "../brain-answer/SKILL.md")


def test_load_canonical_skill_rejects_fifo_substituted_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    skill = root / ".agents/skills/brain-answer/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: brain-answer\ndescription: x\n---\nBody\n", encoding="utf-8"
    )
    original_open = os.open
    replaced = False
    timed_out = False

    def substitute_fifo(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "SKILL.md" and not replaced:
            skill.unlink()
            os.mkfifo(skill)
            replaced = True
        return original_open(path, flags, *args, **kwargs)

    def timeout(_signum, _frame):
        nonlocal timed_out
        timed_out = True
        raise TimeoutError("opening substituted FIFO blocked")

    monkeypatch.setattr(instructions.os, "open", substitute_fifo)
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        with pytest.raises(FrontmatterError):
            load_canonical_skill(root, skill)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert not timed_out


def test_load_canonical_skill_closes_descriptor_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    skill = root / ".agents/skills/brain-answer/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: brain-answer\ndescription: x\n---\nBody\n", encoding="utf-8"
    )
    original_open = os.open
    original_close = os.close
    original_fstat = os.fstat
    opened: set[int] = set()
    closed: set[int] = set()
    failed = False

    def track_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def track_close(descriptor):
        closed.add(descriptor)
        return original_close(descriptor)

    def fail_first_fstat(descriptor):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(instructions.os, "open", track_open)
    monkeypatch.setattr(instructions.os, "close", track_close)
    monkeypatch.setattr(instructions.os, "fstat", fail_first_fstat)

    with pytest.raises(FrontmatterError, match="cannot safely read canonical skill"):
        load_canonical_skill(root, skill)

    assert opened <= closed


def test_validator_requires_canonical_skills_and_relative_claude_links(
    repo_root: Path,
) -> None:
    report = validate_instruction_architecture(break_instruction_topology(repo_root))
    assert {issue.code for issue in report.issues} >= {
        "missing_canonical_skill",
        "invalid_claude_skill_link",
    }


def test_validator_rejects_canonical_skill_symlink_to_external_file(
    repo_root: Path,
) -> None:
    canonical = repo_root / ".agents/skills/brain-answer/SKILL.md"
    external = repo_root.parent / "external-brain-answer-skill.md"
    external.write_text(
        "---\nname: brain-answer\ndescription: x\n---\nExternal body\n",
        encoding="utf-8",
    )
    canonical.unlink()
    canonical.symlink_to(external)

    report = validate_instruction_architecture(RepoPaths.discover(repo_root))

    assert "invalid_skill" in {issue.code for issue in report.issues}


def test_validate_command_appends_instruction_architecture_report(
    repo_root: Path,
) -> None:
    break_instruction_topology(repo_root)
    stdout = io.StringIO()
    returncode = main(
        ["--json", "validate"],
        cwd=repo_root,
        stdout=stdout,
        stderr=io.StringIO(),
    )
    payload = json.loads(stdout.getvalue())
    report = next(
        item
        for item in payload["data"]["reports"]
        if item["checks"] == ["instruction-architecture"]
    )
    assert returncode == 1
    assert {item["code"] for item in report["issues"]} >= {
        "missing_canonical_skill",
        "invalid_claude_skill_link",
    }


def test_validator_rejects_model_pins_and_cross_client_description_drift(
    repo_root: Path,
) -> None:
    codex = repo_root / ".codex/agents/source-researcher.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(
        'name = "source-researcher"\n'
        'description = "Codex description"\n'
        'model = "pinned"\n'
        'developer_instructions = "Read docs/brain/agent-briefs/source-researcher.md"\n',
        encoding="utf-8",
    )
    claude = repo_root / ".claude/agents/source-researcher.md"
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text(
        "---\nname: source-researcher\ndescription: Claude description\n---\n"
        "Read `docs/brain/agent-briefs/source-researcher.md`.\n",
        encoding="utf-8",
    )
    report = validate_instruction_architecture(RepoPaths.discover(repo_root))
    assert "invalid_agent_adapter" in {issue.code for issue in report.issues}
