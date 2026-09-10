from pathlib import Path
import tomllib

from brainlib.instructions import parse_scalar_frontmatter


ROOT = Path(__file__).resolve().parents[2]
SKILLS = (
    "brain-initialize",
    "brain-answer",
    "brain-web-research",
    "brain-wiki-maintenance",
    "brain-validate",
)
AGENTS = ("source-researcher", "source-ingester", "wiki-curator", "brain-auditor")


def test_claude_skill_links_are_relative_and_resolve_to_canonical_skills() -> None:
    for name in SKILLS:
        link = ROOT / ".claude/skills" / name
        assert link.is_symlink()
        assert link.readlink() == Path("../../.agents/skills") / name
        assert link.resolve() == (ROOT / ".agents/skills" / name).resolve()


def test_codex_agents_only_route_to_shared_briefs() -> None:
    for name in AGENTS:
        path = ROOT / ".codex/agents" / f"{name}.toml"
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        assert value["name"] == name
        assert value["description"] == CLAUDE_DESCRIPTIONS[name]
        assert f"docs/brain/agent-briefs/{name}.md" in value["developer_instructions"]
        assert "model" not in value
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 12


def test_claude_agents_only_route_to_shared_briefs() -> None:
    for name in AGENTS:
        path = ROOT / ".claude/agents" / f"{name}.md"
        metadata, body = parse_scalar_frontmatter(path)
        text = path.read_text(encoding="utf-8")
        assert metadata == {"name": name, "description": CLAUDE_DESCRIPTIONS[name]}
        assert f"docs/brain/agent-briefs/{name}.md" in text
        assert "model:" not in text
        assert body.strip()
        assert len(text.splitlines()) <= 12


CLAUDE_DESCRIPTIONS = {
    "source-researcher": "Searches the active local corpus in three passes and returns a cited evidence packet without writing files.",
    "source-ingester": "Creates and registers faithful searchable representations for exact source-ingestion handoffs.",
    "wiki-curator": "Stages and atomically applies grounded Q&A and wiki changes with immutable citations and reciprocal links.",
    "brain-auditor": "Independently audits coverage, evidence, citations, graph integrity, routing, and approvals without writing files.",
}
