"""Tests for the skill tool dispatchers (agent/skill_tools.py)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent import skill_registry
from agent.skills import Skill, load_skills
from agent.skill_tools import (
    SKILL_TOOLS,
    execute_skill_view,
    execute_skill_search,
    execute_skill_install,
    execute_skill_tool,
)


def _make_skill(name: str, content: str = "## Workflow\n1. Run.", references=None) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        version=1,
        content=content,
        path=Path(f"/fake/{name}/SKILL.md"),
        references=references or [],
        root=Path(f"/fake/{name}") if references else None,
    )


def _write_folder_skill(root: Path, name: str, body: str = "Body") -> None:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        dedent(f"""\
        ---
        name: {name}
        description: {name} description
        ---
        ## Workflow
        {body}
        """),
        encoding="utf-8",
    )


# ── Tool schemas ─────────────────────────────────────────────────────────────

def test_skill_tools_have_expected_names():
    names = {t["function"]["name"] for t in SKILL_TOOLS}
    assert names == {"skill_view", "skill_search", "skill_install", "skill_registry_update"}


def test_skill_tools_side_effects_flagged_correctly():
    by_name = {t["function"]["name"]: t for t in SKILL_TOOLS}
    assert by_name["skill_view"]["side_effects"] is False
    assert by_name["skill_search"]["side_effects"] is False
    assert by_name["skill_install"]["side_effects"] is True
    assert by_name["skill_registry_update"]["side_effects"] is True


# ── skill_view ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_view_returns_body():
    skills = [_make_skill("alpha", content="## Workflow\nDo alpha.")]
    result = await execute_skill_view({"name": "alpha"}, skills)
    assert "error" not in result
    assert result["name"] == "alpha"
    assert "Do alpha" in result["content"]


@pytest.mark.asyncio
async def test_skill_view_unknown_skill_returns_error_with_hint():
    result = await execute_skill_view({"name": "ghost"}, [])
    assert "error" in result
    assert "skill_search" in result.get("hint", "")


@pytest.mark.asyncio
async def test_skill_view_missing_name_returns_error():
    result = await execute_skill_view({}, [])
    assert "error" in result


@pytest.mark.asyncio
async def test_skill_view_unknown_ref_returns_available_list(tmp_path):
    _write_folder_skill(tmp_path, "demo")
    folder = tmp_path / "demo"
    (folder / "SKILL.md").write_text(
        dedent("""\
        ---
        name: demo
        description: demo
        references:
          - references/known.md
        ---
        ## Workflow
        Body.
        """),
        encoding="utf-8",
    )
    skills = load_skills(skills_dir=tmp_path)
    result = await execute_skill_view({"name": "demo", "ref": "references/missing.md"}, skills)
    assert "error" in result
    assert result["available_references"] == ["references/known.md"]


# ── skill_search ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_search_returns_truncation_flag(tmp_path, monkeypatch):
    # Create 30 skills so we hit the 25 cap.
    for i in range(30):
        folder = tmp_path / f"skill_{i:02d}"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: skill_{i:02d}\ndescription: number {i}\n---\nbody",
            encoding="utf-8",
        )

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(tmp_path))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")

    result = await execute_skill_search({"query": "skill"})
    assert result["count"] == 30
    assert len(result["matches"]) == 25
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_skill_search_invalid_source_returns_error():
    result = await execute_skill_search({"query": "foo", "source": "totally-fake"})
    assert "error" in result


# ── skill_install ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_install_requires_name_and_source():
    result = await execute_skill_install({})
    assert "error" in result


@pytest.mark.asyncio
async def test_skill_install_end_to_end(tmp_path, monkeypatch):
    src_root = tmp_path / "hermes_src"
    dest_root = tmp_path / "user_skills"
    _write_folder_skill(src_root, "calendar_helper")

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(src_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", dest_root)

    result = await execute_skill_install({"name": "calendar_helper", "source": "hermes"})
    assert result["ok"] is True
    assert (dest_root / "calendar_helper" / "SKILL.md").exists()


# ── execute_skill_tool dispatcher ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatcher_routes_known_tools():
    result = await execute_skill_tool("skill_view", {"name": "ghost"}, [])
    assert "error" in result  # delegates correctly


@pytest.mark.asyncio
async def test_dispatcher_unknown_tool_returns_error():
    result = await execute_skill_tool("not_a_skill_tool", {}, [])
    assert "error" in result
