"""Lazy-load skill system.

Skills are markdown files with YAML frontmatter (name, description, version,
optional references). Two layouts are supported:

  skills/<name>/SKILL.md     — folder form, may include sibling references/*.md
  skills/<name>.md           — flat form, kept for back-compat

On every turn, Pepper appends a one-line index of installed skills to the
system prompt. The model decides when a skill is relevant and calls the
`skill_view` tool to load the body (progressive disclosure). The old
trigger-phrase matcher and model-tier upgrade have been removed; see
docs/SKILLS.md for the full rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()

_SKILLS_DIR = Path(__file__).parent.parent / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    version: int
    content: str            # SKILL.md body, frontmatter stripped
    path: Path              # SKILL.md file (or flat .md)
    references: list[str] = field(default_factory=list)  # sibling markdown files
    root: Path | None = None   # folder root for folder-form skills; None for flat


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split raw text into (frontmatter_dict, body).

    Returns ({}, raw) if the file has no valid --- delimiters.
    """
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    fm_text = match.group(1)
    body = match.group(2).strip()

    try:
        import yaml
        fm = yaml.safe_load(fm_text) or {}
    except Exception:
        # Fallback minimal parser: handles `key: scalar` and indented `- item` lists.
        fm = {}
        current_list_key: str | None = None
        for line in fm_text.splitlines():
            stripped = line.rstrip()
            if not stripped:
                continue
            if stripped.lstrip().startswith("- "):
                if current_list_key is not None:
                    item = stripped.lstrip().removeprefix("- ").strip()
                    fm.setdefault(current_list_key, []).append(item)
                continue
            if ":" in stripped and not stripped.startswith(" "):
                current_list_key = None
                k, _, v = stripped.partition(":")
                k = k.strip()
                v = v.strip()
                if v:
                    fm[k] = v
                else:
                    current_list_key = k

    return fm, body


def _load_skill(path: Path, root: Path | None = None) -> Skill | None:
    """Parse and validate a single SKILL.md file. Returns None on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill_read_failed", path=str(path), error=str(e))
        return None

    fm, body = parse_frontmatter(raw)

    name = fm.get("name", "")
    if not name:
        logger.warning("skill_missing_name", path=str(path))
        return None

    if not body:
        logger.warning("skill_empty_body", name=name, path=str(path))
        return None

    references = fm.get("references") or []
    if isinstance(references, str):
        references = [references]
    references = [str(r) for r in references if r and str(r).endswith(".md")]

    try:
        version = int(fm.get("version", 1))
    except (TypeError, ValueError):
        version = 1

    return Skill(
        name=str(name),
        description=str(fm.get("description", "")),
        version=version,
        content=body,
        path=path,
        references=references,
        root=root,
    )


def load_skills(skills_dir: Path | None = None) -> list[Skill]:
    """Load all skills from a directory.

    Walks both `<name>/SKILL.md` (folder form) and top-level `*.md` (flat form).
    Skips files that fail validation with a warning — never crashes startup.
    Local installs override registry skills only at the call site; this loader
    just reads one directory.
    """
    directory = skills_dir or _SKILLS_DIR
    if not directory.exists():
        logger.info("skills_dir_not_found", path=str(directory))
        return []

    skills: list[Skill] = []
    seen_names: set[str] = set()

    # Folder form: <name>/SKILL.md
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        skill = _load_skill(skill_md, root=entry)
        if skill is None:
            logger.warning("skill_load_skipped", path=str(skill_md))
            continue
        if skill.name in seen_names:
            logger.warning("skill_name_collision", name=skill.name, path=str(skill_md))
            continue
        skills.append(skill)
        seen_names.add(skill.name)
        logger.info("skill_loaded", name=skill.name, version=skill.version, layout="folder")

    # Flat form: top-level *.md
    for path in sorted(directory.glob("*.md")):
        skill = _load_skill(path, root=None)
        if skill is None:
            logger.warning("skill_load_skipped", path=str(path))
            continue
        if skill.name in seen_names:
            logger.warning("skill_name_collision", name=skill.name, path=str(path))
            continue
        skills.append(skill)
        seen_names.add(skill.name)
        logger.info("skill_loaded", name=skill.name, version=skill.version, layout="flat")

    logger.info("skills_loaded", count=len(skills))
    return skills


def sync_repo_skills_to_user_dir(
    user_dir: Path | None = None,
    repo_dir: Path | None = None,
) -> dict[str, int]:
    """Mirror repo skills into the user dir so the bind-mounted ~/.pepper sees them.

    The repo (`./skills/`) is the deployment artifact — the canonical source of
    truth shipped with each release. The user dir (`~/.pepper/skills/`) is the
    runtime location bind-mounted into the container. On every boot we copy
    repo → user so a `git pull && docker compose restart` is enough to deploy
    skill changes; nothing manual.

    For each skill in the repo (flat or folder form), writes
    `~/.pepper/skills/<name>/SKILL.md` (always folder form in the user dir).
    Folder-form repo skills also get their sibling files mirrored.

    Idempotent: only writes when content actually differs. Never touches
    user-installed skills that have no repo counterpart (those came from
    `skill_install` against external registries and must be preserved).

    Returns a counter dict: {"created": N, "updated": N, "unchanged": N}.
    """
    repo = repo_dir or _SKILLS_DIR
    user = user_dir or (Path.home() / ".pepper" / "skills")

    counts = {"created": 0, "updated": 0, "unchanged": 0}

    if not repo.exists():
        logger.info("skill_sync_skipped", reason="repo_dir_missing", path=str(repo))
        return counts

    user.mkdir(parents=True, exist_ok=True)

    # Collect (name, source_path, source_root_or_None) tuples from the repo.
    # Folder form first so it wins over a stale flat sibling with the same name.
    sources: list[tuple[str, Path, Path | None]] = []
    seen: set[str] = set()

    for entry in sorted(repo.iterdir()):
        if entry.is_dir():
            skill_md = entry / "SKILL.md"
            if skill_md.exists() and entry.name not in seen:
                sources.append((entry.name, skill_md, entry))
                seen.add(entry.name)

    for path in sorted(repo.glob("*.md")):
        name = path.stem
        if name not in seen:
            sources.append((name, path, None))
            seen.add(name)

    for name, src_md, src_root in sources:
        try:
            target_dir = user / name
            target_md = target_dir / "SKILL.md"
            new_content = src_md.read_text(encoding="utf-8")

            if not target_md.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(new_content, encoding="utf-8")
                counts["created"] += 1
                logger.info("skill_sync_created", name=name)
            elif target_md.read_text(encoding="utf-8") != new_content:
                target_md.write_text(new_content, encoding="utf-8")
                counts["updated"] += 1
                logger.info("skill_sync_updated", name=name)
            else:
                counts["unchanged"] += 1

            # Mirror sibling files for folder-form skills (e.g. references/*.md).
            if src_root is not None:
                for src_file in src_root.rglob("*"):
                    if not src_file.is_file() or src_file == src_md:
                        continue
                    rel = src_file.relative_to(src_root)
                    dst = target_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not dst.exists() or dst.read_bytes() != src_file.read_bytes():
                        dst.write_bytes(src_file.read_bytes())
                        logger.info("skill_sync_asset", name=name, asset=str(rel))
        except OSError as e:
            logger.warning("skill_sync_failed", name=name, error=str(e))

    logger.info("skill_sync_done", **counts)
    return counts


def load_all_skills(
    user_dir: Path | None = None,
    repo_dir: Path | None = None,
) -> list[Skill]:
    """Load installed skills from both ~/.pepper/skills and the repo skills dir.

    User-installed skills (~/.pepper/skills) take precedence over repo skills
    on name collision — local install always wins.
    """
    user = user_dir or (Path.home() / ".pepper" / "skills")
    repo = repo_dir or _SKILLS_DIR

    user_skills = load_skills(user) if user.exists() else []
    repo_skills = load_skills(repo) if repo.exists() else []

    seen = {s.name for s in user_skills}
    merged = list(user_skills)
    for s in repo_skills:
        if s.name in seen:
            logger.info("skill_repo_shadowed_by_user", name=s.name)
            continue
        merged.append(s)
        seen.add(s.name)

    return merged


def build_index(skills: list[Skill]) -> str:
    """Render a compact one-line-per-skill index for system-prompt injection.

    Returns the empty string when no skills are loaded — callers should not
    inject in that case.
    """
    if not skills:
        return ""

    lines = ["Available skills:"]
    for s in sorted(skills, key=lambda x: x.name):
        desc = s.description.strip().replace("\n", " ")
        if len(desc) > 160:
            desc = desc[:157] + "..."
        lines.append(f"- {s.name} — {desc}" if desc else f"- {s.name}")

    lines.append("")
    lines.append(
        "Use skill_view(name) to load a skill's full instructions when one is "
        "relevant. Use skill_search(query) to find skills not yet installed."
    )
    return "\n".join(lines)


def read_skill_reference(skill: Skill, ref: str) -> str | None:
    """Return the contents of a sibling markdown reference, or None.

    Refuses paths that escape the skill folder, non-markdown files, and any
    reference not declared in the skill's frontmatter.
    """
    if skill.root is None:
        return None
    if not ref.endswith(".md"):
        return None
    if ref not in skill.references:
        return None
    target = (skill.root / ref).resolve()
    try:
        target.relative_to(skill.root.resolve())
    except ValueError:
        logger.warning("skill_ref_escape_attempt", skill=skill.name, ref=ref)
        return None
    if not target.exists() or not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("skill_ref_read_failed", skill=skill.name, ref=ref, error=str(e))
        return None
