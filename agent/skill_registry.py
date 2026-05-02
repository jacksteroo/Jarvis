"""Skill registry — browse and install skills from trusted external sources.

Two registries are supported:

  anthropics — git mirror of anthropics/skills, pinned by SHA via
               PEPPER_ANTHROPIC_SKILLS_SHA in .env. Mirrored to
               ~/.pepper/skill-registry/anthropics/ and refreshed manually.

  hermes     — read live from PEPPER_HERMES_SKILLS_PATH (default:
               /Users/jack/Developer/nousresearch/hermes-agent/skills).
               Skipped silently if the path does not exist.

Discovery is substring match on name+description for v1. The skill set is
small enough that this is sufficient; a pgvector layer can be added later
without changing the tool surface.

Install enforces markdown-only: SKILL.md plus any references/*.md declared
in frontmatter. Anything else (scripts/, assets/, executables) is skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

from agent.skills import parse_frontmatter

logger = structlog.get_logger()

USER_SKILLS_DIR = Path.home() / ".pepper" / "skills"
REGISTRY_DIR = Path.home() / ".pepper" / "skill-registry"
ANTHROPICS_MIRROR = REGISTRY_DIR / "anthropics"

DEFAULT_ANTHROPIC_REPO = "https://github.com/anthropics/skills.git"
DEFAULT_HERMES_PATH = Path("/Users/jack/Developer/nousresearch/hermes-agent/skills")

VALID_SOURCES = ("anthropics", "hermes")


@dataclass
class RegistrySkill:
    name: str
    description: str
    source: str           # "anthropics" | "hermes"
    root: Path            # folder containing SKILL.md
    references: list[str]


# ── Registry walking ─────────────────────────────────────────────────────────


def _walk_registry(root: Path, source: str) -> list[RegistrySkill]:
    """Walk a registry root, returning every parseable SKILL.md folder.

    Skips any path under a dot-prefixed directory (e.g. `.git/`, `.cache/`)
    so the anthropics mirror's git internals never produce phantom skills.
    """
    if not root.exists():
        return []

    skills: list[RegistrySkill] = []
    for skill_md in root.rglob("SKILL.md"):
        try:
            rel_parts = skill_md.relative_to(root).parts
        except ValueError:
            continue
        if any(p.startswith(".") for p in rel_parts):
            continue
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("registry_skill_read_failed", path=str(skill_md), error=str(e))
            continue

        fm, body = parse_frontmatter(raw)
        name = fm.get("name", "")
        if not name or not body:
            continue

        references = fm.get("references") or []
        if isinstance(references, str):
            references = [references]
        references = [str(r) for r in references if r and str(r).endswith(".md")]

        skills.append(
            RegistrySkill(
                name=str(name),
                description=str(fm.get("description", "")),
                source=source,
                root=skill_md.parent,
                references=references,
            )
        )

    return skills


def list_registry_skills() -> list[RegistrySkill]:
    """List every skill across all configured registries."""
    hermes_path = Path(os.environ.get("PEPPER_HERMES_SKILLS_PATH", str(DEFAULT_HERMES_PATH)))

    skills: list[RegistrySkill] = []
    skills.extend(_walk_registry(ANTHROPICS_MIRROR, "anthropics"))
    skills.extend(_walk_registry(hermes_path, "hermes"))
    return skills


def search_registry(query: str, source: str | None = None) -> list[RegistrySkill]:
    """Substring search over registry skill names and descriptions.

    Returns matches ranked by name-match first, then description-match.
    Empty query returns the full registry.
    """
    all_skills = list_registry_skills()
    if source:
        all_skills = [s for s in all_skills if s.source == source]

    if not query:
        return all_skills

    q = query.lower().strip()
    name_hits: list[RegistrySkill] = []
    desc_hits: list[RegistrySkill] = []
    for s in all_skills:
        if q in s.name.lower():
            name_hits.append(s)
        elif q in s.description.lower():
            desc_hits.append(s)
    return name_hits + desc_hits


# ── Install ──────────────────────────────────────────────────────────────────


def _is_safe_skill_name(name: str) -> bool:
    """Reject names that would let a malicious frontmatter escape the install dir.

    A skill name is attacker-influenced (it comes from registry SKILL.md
    frontmatter) and is used as the leaf folder in `USER_SKILLS_DIR / name`.
    Permit only names that are a single safe path component.
    """
    if not name:
        return False
    if name in (".", ".."):
        return False
    if name.startswith("."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


def install_skill(name: str, source: str) -> dict:
    """Copy a registry skill into ~/.pepper/skills/<name>/.

    Markdown-only: copies SKILL.md and any references/*.md listed in
    frontmatter. Symlinks in the source are refused. The destination is
    rebuilt from scratch so stale references from a previous install never
    linger. Returns a result dict suitable for tool output.
    """
    if source not in VALID_SOURCES:
        return {"error": f"unknown source '{source}' (expected: {', '.join(VALID_SOURCES)})"}
    if not _is_safe_skill_name(name):
        return {"error": f"invalid skill name '{name}' (must be a single safe path component)"}

    candidates = [s for s in list_registry_skills() if s.name == name and s.source == source]
    if not candidates:
        return {"error": f"skill '{name}' not found in source '{source}'"}
    if len(candidates) > 1:
        logger.warning("registry_duplicate_skill", name=name, source=source, count=len(candidates))
    skill = candidates[0]

    src_skill_md = skill.root / "SKILL.md"
    if src_skill_md.is_symlink():
        return {"error": f"refusing symlinked SKILL.md for '{name}'"}

    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = USER_SKILLS_DIR / name
    if dest.exists():
        # Rebuild from scratch so a previous version's stale references don't linger.
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    dest_skill_md = dest / "SKILL.md"
    shutil.copy2(src_skill_md, dest_skill_md)

    refs_copied: list[str] = []
    refs_skipped: list[str] = []
    for ref in skill.references:
        if (skill.root / ref).is_symlink():
            refs_skipped.append(ref)
            continue
        src_ref = (skill.root / ref).resolve()
        try:
            src_ref.relative_to(skill.root.resolve())
        except ValueError:
            refs_skipped.append(ref)
            continue
        if not src_ref.exists() or not src_ref.is_file() or src_ref.suffix != ".md":
            refs_skipped.append(ref)
            continue
        dest_ref = dest / ref
        # Defence-in-depth: even though the source check normalizes `..`
        # textually, also confirm the destination resolves inside dest.
        try:
            dest_ref.resolve().relative_to(dest.resolve())
        except ValueError:
            refs_skipped.append(ref)
            continue
        dest_ref.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_ref, dest_ref)
        refs_copied.append(ref)

    logger.info(
        "skill_installed",
        name=name,
        source=source,
        dest=str(dest),
        references=refs_copied,
        skipped=refs_skipped,
    )

    return {
        "ok": True,
        "name": name,
        "source": source,
        "dest": str(dest),
        "references_copied": refs_copied,
        "references_skipped": refs_skipped,
    }


# ── Anthropics mirror management ─────────────────────────────────────────────


def _git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def update_anthropics_mirror() -> dict:
    """Clone or update the anthropics/skills mirror to the pinned SHA.

    Reads PEPPER_ANTHROPIC_SKILLS_REPO and PEPPER_ANTHROPIC_SKILLS_SHA from
    the environment. Without a SHA the function refuses to update — pinning
    is required for trust.
    """
    repo = os.environ.get("PEPPER_ANTHROPIC_SKILLS_REPO", DEFAULT_ANTHROPIC_REPO)
    sha = os.environ.get("PEPPER_ANTHROPIC_SKILLS_SHA", "").strip()
    if not sha:
        return {
            "error": (
                "PEPPER_ANTHROPIC_SKILLS_SHA not set; refusing to update without a "
                "pinned commit (set it in .env)"
            ),
        }

    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)

    if not (ANTHROPICS_MIRROR / ".git").exists():
        if ANTHROPICS_MIRROR.exists():
            shutil.rmtree(ANTHROPICS_MIRROR)
        rc, out, err = _git("clone", "--filter=blob:none", "--", repo, str(ANTHROPICS_MIRROR))
        if rc != 0:
            return {"error": f"clone failed: {err or out}"}

    rc, _, err = _git("fetch", "origin", cwd=ANTHROPICS_MIRROR)
    if rc != 0:
        # Don't leave a stale tree behind: a fetch failure means we cannot
        # guarantee the SHA pin is honored. Wipe so the next call re-clones.
        shutil.rmtree(ANTHROPICS_MIRROR, ignore_errors=True)
        return {"error": f"fetch failed: {err}"}

    rc, _, err = _git("checkout", "--detach", sha, "--", cwd=ANTHROPICS_MIRROR)
    if rc != 0:
        # Same reasoning: a checkout failure leaves the tree on whatever
        # ref clone left (usually main). That breaks the pinning guarantee.
        shutil.rmtree(ANTHROPICS_MIRROR, ignore_errors=True)
        return {"error": f"checkout {sha} failed: {err}"}

    skills_count = sum(1 for _ in ANTHROPICS_MIRROR.rglob("SKILL.md"))
    logger.info("anthropics_mirror_updated", sha=sha, skills_count=skills_count)
    return {
        "ok": True,
        "repo": repo,
        "sha": sha,
        "path": str(ANTHROPICS_MIRROR),
        "skills_count": skills_count,
    }
