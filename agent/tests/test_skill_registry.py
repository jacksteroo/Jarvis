"""Tests for the skill registry (agent/skill_registry.py).

Network-dependent paths (git fetch/clone) are not exercised here; we test
walking, searching, and installing against on-disk fixtures.
"""

from __future__ import annotations

from agent import skill_registry
from agent.skill_registry import (
    _walk_registry,
    install_skill,
    search_registry,
    update_anthropics_mirror,
)


def _make_skill_folder(
    root: Path,
    name: str,
    description: str = "",
    references: dict[str, str] | None = None,
    extras: dict[str, str] | None = None,
) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", f"description: {description}"]
    if references:
        lines.append("references:")
        for ref_name in references:
            lines.append(f"  - references/{ref_name}")
    lines += ["---", "## Workflow", f"Body for {name}.", ""]
    (folder / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    if references:
        (folder / "references").mkdir(exist_ok=True)
        for ref_name, ref_body in references.items():
            (folder / "references" / ref_name).write_text(ref_body, encoding="utf-8")
    if extras:
        for extra_name, extra_body in extras.items():
            extra_path = folder / extra_name
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            extra_path.write_text(extra_body, encoding="utf-8")
    return folder


# ── _walk_registry ───────────────────────────────────────────────────────────

def test_walk_registry_returns_empty_for_missing_root(tmp_path):
    assert _walk_registry(tmp_path / "missing", "anthropics") == []


def test_walk_registry_finds_nested_skills(tmp_path):
    _make_skill_folder(tmp_path / "category_a", "skill_one", "First skill")
    _make_skill_folder(tmp_path / "category_b" / "subcat", "skill_two", "Second skill")
    skills = _walk_registry(tmp_path, "anthropics")
    names = {s.name for s in skills}
    assert names == {"skill_one", "skill_two"}
    for s in skills:
        assert s.source == "anthropics"


def test_walk_registry_skips_unparseable_skill_md(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\ndescription: no name\n---\nBody", encoding="utf-8")
    assert _walk_registry(tmp_path, "anthropics") == []


# ── search_registry ──────────────────────────────────────────────────────────

def test_search_registry_substring_match(tmp_path, monkeypatch):
    _make_skill_folder(tmp_path, "github_auth", "Authenticate with GitHub via OAuth")
    _make_skill_folder(tmp_path, "email_triage", "Sort an inbox by importance")

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(tmp_path))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthropics")

    matches = search_registry("github")
    assert any(m.name == "github_auth" for m in matches)
    assert all(m.name != "email_triage" for m in matches)


def test_search_registry_name_match_ranks_first(tmp_path, monkeypatch):
    _make_skill_folder(tmp_path, "alpha", "Some other description")
    _make_skill_folder(tmp_path, "beta", "References alpha workflow indirectly")

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(tmp_path))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthropics")

    matches = search_registry("alpha")
    assert matches[0].name == "alpha"


def test_search_registry_source_filter(tmp_path, monkeypatch):
    anthr_root = tmp_path / "anthropics"
    hermes_root = tmp_path / "hermes"
    anthr_root.mkdir()
    hermes_root.mkdir()
    _make_skill_folder(anthr_root, "anthr_skill", "From anthropics")
    _make_skill_folder(hermes_root, "hermes_skill", "From hermes")

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(hermes_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", anthr_root)

    only_hermes = search_registry("", source="hermes")
    assert {m.name for m in only_hermes} == {"hermes_skill"}

    only_anthr = search_registry("", source="anthropics")
    assert {m.name for m in only_anthr} == {"anthr_skill"}


# ── install_skill ────────────────────────────────────────────────────────────

def test_install_skill_copies_markdown_only(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "user_skills"
    src_root.mkdir()

    _make_skill_folder(
        src_root,
        "demo",
        "Demo skill",
        references={"extra.md": "ref body"},
        extras={
            "scripts/danger.py": "import os; os.system('rm -rf /')",
            "assets/image.png": "binary",
            "references/sneaky.py": "print('boom')",
        },
    )

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(src_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", dest_root)

    result = install_skill("demo", "hermes")
    assert result["ok"] is True

    assert (dest_root / "demo" / "SKILL.md").exists()
    assert (dest_root / "demo" / "references" / "extra.md").exists()
    # Non-markdown extras must NOT be copied:
    assert not (dest_root / "demo" / "scripts" / "danger.py").exists()
    assert not (dest_root / "demo" / "assets" / "image.png").exists()
    assert not (dest_root / "demo" / "references" / "sneaky.py").exists()


def test_install_skill_unknown_source_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", tmp_path / "user_skills")
    result = install_skill("anything", "nope")
    assert "error" in result


def test_install_skill_rejects_path_traversal_in_name(tmp_path, monkeypatch):
    """A malicious SKILL.md whose `name` contains `..` must not write outside dest."""
    src_root = tmp_path / "src"
    user_skills = tmp_path / "user_skills"
    src_root.mkdir()
    # Place a SKILL.md with a malicious `name` field on disk so it's a real
    # candidate from list_registry_skills.
    folder = src_root / "harmless_folder"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: ../../escape\ndescription: bad\n---\n## Workflow\nbody",
        encoding="utf-8",
    )
    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(src_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", user_skills)

    result = install_skill("../../escape", "hermes")
    assert "error" in result
    # Nothing should have been written outside user_skills
    assert not (tmp_path / "escape").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_install_skill_rejects_dotfile_name(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    src_root.mkdir()
    folder = src_root / "looks_normal"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: .hidden\ndescription: x\n---\n## Workflow\nbody",
        encoding="utf-8",
    )
    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(src_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", tmp_path / "user_skills")
    result = install_skill(".hidden", "hermes")
    assert "error" in result


def test_walk_registry_skips_dotfile_dirs(tmp_path):
    """A SKILL.md hiding inside `.git/` or any dot-prefixed dir must be ignored."""
    git_dir = tmp_path / ".git" / "evil"
    git_dir.mkdir(parents=True)
    (git_dir / "SKILL.md").write_text(
        "---\nname: phantom\ndescription: planted\n---\n## Workflow\nx",
        encoding="utf-8",
    )
    skills = _walk_registry(tmp_path, "anthropics")
    assert skills == []


def test_install_skill_missing_skill_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", tmp_path / "user_skills")
    result = install_skill("ghost", "hermes")
    assert "error" in result


def test_install_skill_idempotent_overwrite(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    dest_root = tmp_path / "user_skills"
    src_root.mkdir()
    _make_skill_folder(src_root, "demo", "Demo")

    monkeypatch.setenv("PEPPER_HERMES_SKILLS_PATH", str(src_root))
    monkeypatch.setattr(skill_registry, "ANTHROPICS_MIRROR", tmp_path / "no_anthr")
    monkeypatch.setattr(skill_registry, "USER_SKILLS_DIR", dest_root)

    install_skill("demo", "hermes")
    result2 = install_skill("demo", "hermes")
    assert result2["ok"] is True


# ── update_anthropics_mirror (env-only checks; no real git) ──────────────────

def test_update_anthropics_mirror_requires_sha(monkeypatch):
    monkeypatch.delenv("PEPPER_ANTHROPIC_SKILLS_SHA", raising=False)
    result = update_anthropics_mirror()
    assert "error" in result
    assert "PEPPER_ANTHROPIC_SKILLS_SHA" in result["error"]
