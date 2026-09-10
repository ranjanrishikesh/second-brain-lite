"""Validate that each client consumes the same instruction architecture."""

from __future__ import annotations

from pathlib import Path
import tomllib

from brainlib.diagnostics import ValidationIssue, ValidationReport
from brainlib.instructions import (
    FrontmatterError,
    load_canonical_skill,
    parse_scalar_frontmatter,
)
from brainlib.layout import RepoPaths


SKILLS = (
    "brain-initialize",
    "brain-answer",
    "brain-web-research",
    "brain-wiki-maintenance",
    "brain-validate",
)
AGENTS = ("source-researcher", "source-ingester", "wiki-curator", "brain-auditor")


def validate_instruction_architecture(paths: RepoPaths) -> ValidationReport:
    """Report missing or drifting skills, briefs, and client adapters."""

    issues: list[ValidationIssue] = []
    for name in SKILLS:
        canonical = paths.root / ".agents" / "skills" / name / "SKILL.md"
        if not canonical.exists() and not canonical.is_symlink():
            issues.append(
                ValidationIssue(
                    "error",
                    "missing_canonical_skill",
                    f"missing {canonical.relative_to(paths.root)}",
                )
            )
        else:
            try:
                load_canonical_skill(paths.root, canonical)
            except FrontmatterError as error:
                issues.append(ValidationIssue("error", "invalid_skill", str(error)))

        claude_link = paths.root / ".claude" / "skills" / name
        expected = Path("..") / ".." / ".agents" / "skills" / name
        if not claude_link.is_symlink() or claude_link.readlink() != expected:
            issues.append(
                ValidationIssue(
                    "error",
                    "invalid_claude_skill_link",
                    f"{claude_link.relative_to(paths.root)} must link to {expected}",
                )
            )

    for name in AGENTS:
        _validate_agent(paths, name, issues)

    return ValidationReport(
        checks=("instruction-architecture",),
        issues=tuple(issues),
        corpus_revision=None,
    )


def _validate_agent(
    paths: RepoPaths, name: str, issues: list[ValidationIssue]
) -> None:
    brief = paths.root / "docs" / "brain" / "agent-briefs" / f"{name}.md"
    if not brief.is_file():
        issues.append(
            ValidationIssue(
                "error",
                "missing_agent_brief",
                f"missing {brief.relative_to(paths.root)}",
            )
        )

    brief_reference = f"docs/brain/agent-briefs/{name}.md"
    codex_relative = Path(".codex/agents") / f"{name}.toml"
    codex = paths.root / codex_relative
    claude_relative = Path(".claude/agents") / f"{name}.md"
    claude = paths.root / claude_relative
    codex_value: dict[str, object] | None = None

    if not codex.is_file():
        issues.append(
            ValidationIssue(
                "error", "missing_agent_adapter", f"missing {codex_relative}"
            )
        )
    else:
        try:
            raw_codex = codex.read_text(encoding="utf-8")
            parsed = tomllib.loads(raw_codex)
            if not isinstance(parsed, dict):
                raise ValueError("adapter must be a TOML table")
            codex_value = parsed
            allowed = {"name", "description", "sandbox_mode", "developer_instructions"}
            if codex_value.keys() - allowed:
                raise ValueError("unknown or model-specific key")
            if codex_value.get("name") != name or not isinstance(
                codex_value.get("description"), str
            ):
                raise ValueError("name/description mismatch")
            instructions = codex_value.get("developer_instructions")
            if (
                "model" in codex_value
                or not isinstance(instructions, str)
                or brief_reference not in instructions
            ):
                raise ValueError("model pin or shared-brief route violation")
            if len(raw_codex.splitlines()) > 12:
                raise ValueError("adapter exceeds 12 lines")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
            codex_value = None
            issues.append(
                ValidationIssue(
                    "error", "invalid_agent_adapter", f"{codex_relative}: {error}"
                )
            )

    if not claude.is_file():
        issues.append(
            ValidationIssue(
                "error", "missing_agent_adapter", f"missing {claude_relative}"
            )
        )
        return

    try:
        raw_claude = claude.read_text(encoding="utf-8")
        metadata, body = parse_scalar_frontmatter(claude)
        if set(metadata) != {"name", "description"} or metadata.get("name") != name:
            raise ValueError("frontmatter must contain only matching name and description")
        if brief_reference not in body:
            raise ValueError("body does not route to shared brief")
        if codex_value is not None and codex_value.get("description") != metadata["description"]:
            raise ValueError("Codex/Claude descriptions differ")
        if len(raw_claude.splitlines()) > 12:
            raise ValueError("adapter exceeds 12 lines")
    except (OSError, UnicodeError, FrontmatterError, ValueError) as error:
        issues.append(
            ValidationIssue(
                "error", "invalid_agent_adapter", f"{claude_relative}: {error}"
            )
        )
