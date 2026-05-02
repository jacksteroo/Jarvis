"""Skill tools — discovery, lazy load, and install.

Lets the model load a skill body on demand (`skill_view`), search trusted
external registries for skills not yet installed (`skill_search`), copy
markdown skills into the user's local install (`skill_install`), and refresh
the anthropics mirror to its pinned SHA (`skill_registry_update`).

The Pepper instance is passed at execution time so the model's calls observe
the live skill list, including skills installed earlier in the same session.
"""

from __future__ import annotations

from agent.skills import Skill, read_skill_reference
from agent.skill_registry import (
    install_skill,
    search_registry,
    update_anthropics_mirror,
)


SKILL_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "skill_view",
            "description": (
                "Load the full instructions for an installed skill. Call this when a "
                "skill in the available-skills index looks relevant to the current task. "
                "Returns the skill's markdown body, or a referenced markdown file when "
                "'ref' is provided."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name as it appears in the available-skills index",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Optional path to a referenced markdown file (e.g. 'references/foo.md')",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "skill_search",
            "description": (
                "Search trusted external skill registries (anthropics, hermes-agent) "
                "for skills not yet installed. Use when no installed skill matches "
                "the current task. Returns name, description, and source for each match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords describing the desired skill (matches name and description)",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["anthropics", "hermes"],
                        "description": "Optionally restrict the search to one registry",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "skill_install",
            "description": (
                "Install a skill from an external registry into Pepper's local skill set. "
                "Markdown only — scripts and binaries are skipped. After install, the skill "
                "appears in the next turn's available-skills index."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name as returned by skill_search",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["anthropics", "hermes"],
                        "description": "Registry the skill comes from",
                    },
                },
                "required": ["name", "source"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "skill_registry_update",
            "description": (
                "Refresh the anthropics/skills mirror to its pinned commit SHA. "
                "Reads PEPPER_ANTHROPIC_SKILLS_SHA from the environment; refuses to "
                "run without one. Hermes-agent skills are read live and need no refresh."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _find_skill(skills: list[Skill], name: str) -> Skill | None:
    for s in skills:
        if s.name == name:
            return s
    return None


async def execute_skill_view(args: dict, skills: list[Skill]) -> dict:
    args = args or {}
    name = (args.get("name") or "").strip()
    ref_raw = args.get("ref") or ""
    ref = ref_raw.strip() or None
    if not name:
        return {"error": "skill_view requires a 'name' argument"}

    skill = _find_skill(skills, name)
    if skill is None:
        return {
            "error": f"skill '{name}' is not installed",
            "hint": "Use skill_search to find skills in external registries, then skill_install to add one.",
        }

    if ref:
        body = read_skill_reference(skill, ref)
        if body is None:
            return {
                "error": f"reference '{ref}' not available for skill '{name}'",
                "available_references": skill.references,
            }
        return {"name": name, "ref": ref, "content": body}

    return {
        "name": name,
        "version": skill.version,
        "description": skill.description,
        "content": skill.content,
        "references": skill.references,
    }


async def execute_skill_search(args: dict) -> dict:
    args = args or {}
    query = (args.get("query") or "").strip()
    source = args.get("source")
    if source and source not in ("anthropics", "hermes"):
        return {"error": f"unknown source '{source}' (expected: anthropics, hermes)"}

    matches = search_registry(query, source=source)
    return {
        "query": query,
        "source": source,
        "count": len(matches),
        "matches": [
            {
                "name": m.name,
                "description": m.description,
                "source": m.source,
            }
            for m in matches[:25]
        ],
        "truncated": len(matches) > 25,
    }


async def execute_skill_install(args: dict) -> dict:
    args = args or {}
    name = (args.get("name") or "").strip()
    source = (args.get("source") or "").strip()
    if not name or not source:
        return {"error": "skill_install requires 'name' and 'source'"}
    return install_skill(name, source)


async def execute_skill_registry_update(args: dict) -> dict:
    return update_anthropics_mirror()


async def execute_skill_tool(name: str, args: dict, skills: list[Skill]) -> dict:
    if name == "skill_view":
        return await execute_skill_view(args, skills)
    if name == "skill_search":
        return await execute_skill_search(args)
    if name == "skill_install":
        return await execute_skill_install(args)
    if name == "skill_registry_update":
        return await execute_skill_registry_update(args)
    return {"error": f"unknown skill tool: {name}"}
