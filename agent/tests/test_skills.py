"""Tests for the lazy-load skill system (agent/skills.py)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agent.skills import (
    Skill,
    parse_frontmatter,
    _load_skill,
    load_skills,
    load_all_skills,
    build_index,
    read_skill_reference,
    sync_repo_skills_to_user_dir,
)


# ── parse_frontmatter ────────────────────────────────────────────────────────

def test_parse_frontmatter_extracts_dict_and_body():
    raw = dedent("""\
        ---
        name: test_skill
        description: A test skill
        version: 1
        references:
          - references/foo.md
        ---

        ## Workflow

        1. Do something.
    """)
    fm, body = parse_frontmatter(raw)
    assert fm["name"] == "test_skill"
    assert fm["description"] == "A test skill"
    assert fm["references"] == ["references/foo.md"]
    assert "Do something" in body


def test_parse_frontmatter_no_frontmatter_returns_empty_dict():
    raw = "Just some content with no frontmatter."
    fm, body = parse_frontmatter(raw)
    assert fm == {}
    assert body == raw


def test_parse_frontmatter_empty_yaml_returns_empty_dict():
    raw = "---\n---\nBody here."
    fm, body = parse_frontmatter(raw)
    assert fm == {}
    assert "Body here" in body


# ── _load_skill ───────────────────────────────────────────────────────────────

def _write_flat_skill(tmp_path: Path, content: str, filename: str = "my_skill.md") -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


def _write_folder_skill(
    tmp_path: Path,
    name: str,
    content: str,
    references: dict[str, str] | None = None,
) -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(content, encoding="utf-8")
    if references:
        (folder / "references").mkdir(exist_ok=True)
        for ref_name, ref_body in references.items():
            (folder / "references" / ref_name).write_text(ref_body, encoding="utf-8")
    return folder


def test_load_skill_valid_flat_file(tmp_path):
    path = _write_flat_skill(tmp_path, dedent("""\
        ---
        name: morning_brief
        description: Daily brief
        version: 2
        ---

        ## Workflow

        1. Greet.
        2. Fetch calendar.
    """))
    skill = _load_skill(path)
    assert skill is not None
    assert skill.name == "morning_brief"
    assert skill.version == 2
    assert skill.root is None  # flat form has no folder root
    assert "Workflow" in skill.content


def test_load_skill_missing_name_returns_none(tmp_path):
    path = _write_flat_skill(tmp_path, dedent("""\
        ---
        description: No name here
        ---
        ## Workflow
        1. Step.
    """))
    assert _load_skill(path) is None


def test_load_skill_empty_body_returns_none(tmp_path):
    path = _write_flat_skill(tmp_path, dedent("""\
        ---
        name: empty_skill
        ---
    """))
    assert _load_skill(path) is None


def test_load_skill_references_only_markdown(tmp_path):
    path = _write_flat_skill(tmp_path, dedent("""\
        ---
        name: demo
        references:
          - references/keep.md
          - scripts/drop.py
          - assets/drop.png
        ---
        ## Workflow
        1. Run.
    """))
    skill = _load_skill(path)
    assert skill is not None
    assert skill.references == ["references/keep.md"]


def test_load_skill_invalid_version_falls_back_to_one(tmp_path):
    path = _write_flat_skill(tmp_path, dedent("""\
        ---
        name: weird
        version: not-a-number
        ---
        ## Workflow
        1. Run.
    """))
    skill = _load_skill(path)
    assert skill is not None
    assert skill.version == 1


# ── load_skills ───────────────────────────────────────────────────────────────

def test_load_skills_returns_empty_for_missing_dir(tmp_path):
    skills = load_skills(skills_dir=tmp_path / "nonexistent")
    assert skills == []


def test_load_skills_loads_flat_files(tmp_path):
    for i in range(3):
        _write_flat_skill(tmp_path, dedent(f"""\
            ---
            name: skill_{i}
            description: Test {i}
            ---
            ## Workflow
            Step {i}.
        """), filename=f"skill_{i}.md")

    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 3
    names = {s.name for s in skills}
    assert names == {"skill_0", "skill_1", "skill_2"}


def test_load_skills_loads_folder_form(tmp_path):
    _write_folder_skill(tmp_path, "alpha", dedent("""\
        ---
        name: alpha
        description: Alpha skill
        ---
        ## Workflow
        Do alpha.
    """))
    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "alpha"
    assert skills[0].root is not None
    assert skills[0].root.name == "alpha"


def test_load_skills_handles_mixed_layouts(tmp_path):
    _write_folder_skill(tmp_path, "folder_skill", dedent("""\
        ---
        name: folder_skill
        ---
        ## Workflow
        Folder.
    """))
    _write_flat_skill(tmp_path, dedent("""\
        ---
        name: flat_skill
        ---
        ## Workflow
        Flat.
    """), filename="flat_skill.md")

    skills = load_skills(skills_dir=tmp_path)
    names = {s.name for s in skills}
    assert names == {"folder_skill", "flat_skill"}


def test_load_skills_folder_wins_on_collision(tmp_path):
    # Folder form runs first; flat with same name should be skipped.
    _write_folder_skill(tmp_path, "dup", dedent("""\
        ---
        name: dup
        description: from folder
        ---
        ## Workflow
        Folder.
    """))
    _write_flat_skill(tmp_path, dedent("""\
        ---
        name: dup
        description: from flat
        ---
        ## Workflow
        Flat.
    """), filename="dup.md")
    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 1
    assert skills[0].description == "from folder"


def test_load_skills_skips_invalid_files(tmp_path):
    _write_flat_skill(tmp_path, dedent("""\
        ---
        name: good_skill
        ---
        ## Workflow
        1. Do it.
    """), filename="good.md")
    _write_flat_skill(tmp_path, "---\ndescription: no name\n---\n## Workflow\n1. Nope.", filename="bad.md")

    skills = load_skills(skills_dir=tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "good_skill"


# ── load_all_skills (user dir overrides repo dir) ─────────────────────────────

def test_load_all_skills_user_overrides_repo(tmp_path):
    user_dir = tmp_path / "user"
    repo_dir = tmp_path / "repo"
    user_dir.mkdir()
    repo_dir.mkdir()

    _write_flat_skill(user_dir, dedent("""\
        ---
        name: shared
        description: from user
        ---
        ## Workflow
        User.
    """), filename="shared.md")
    _write_flat_skill(repo_dir, dedent("""\
        ---
        name: shared
        description: from repo
        ---
        ## Workflow
        Repo.
    """), filename="shared.md")
    _write_flat_skill(repo_dir, dedent("""\
        ---
        name: repo_only
        description: only in repo
        ---
        ## Workflow
        Repo only.
    """), filename="repo_only.md")

    skills = load_all_skills(user_dir=user_dir, repo_dir=repo_dir)
    by_name = {s.name: s for s in skills}
    assert by_name["shared"].description == "from user"
    assert "repo_only" in by_name


# ── build_index ───────────────────────────────────────────────────────────────

def test_build_index_empty_returns_empty_string():
    assert build_index([]) == ""


def test_build_index_lists_skills_alphabetically(tmp_path):
    _write_flat_skill(tmp_path, "---\nname: zebra\ndescription: Z\n---\nbody",
                      filename="zebra.md")
    _write_flat_skill(tmp_path, "---\nname: alpha\ndescription: A\n---\nbody",
                      filename="alpha.md")
    skills = load_skills(skills_dir=tmp_path)
    index = build_index(skills)
    alpha_pos = index.find("- alpha")
    zebra_pos = index.find("- zebra")
    assert alpha_pos != -1 and zebra_pos != -1
    assert alpha_pos < zebra_pos
    assert "skill_view" in index
    assert "skill_search" in index


def test_build_index_truncates_long_descriptions(tmp_path):
    long_desc = "x" * 300
    _write_flat_skill(tmp_path, f"---\nname: long\ndescription: {long_desc}\n---\nbody",
                      filename="long.md")
    skills = load_skills(skills_dir=tmp_path)
    index = build_index(skills)
    for line in index.splitlines():
        if line.startswith("- long"):
            assert len(line) < 200
            assert line.endswith("...")
            break
    else:
        pytest.fail("long skill not found in index")


# ── read_skill_reference ──────────────────────────────────────────────────────

def test_read_skill_reference_returns_declared_md(tmp_path):
    _write_folder_skill(tmp_path, "demo", dedent("""\
        ---
        name: demo
        references:
          - references/extra.md
        ---
        ## Workflow
        Body.
    """), references={"extra.md": "extra body"})
    skills = load_skills(skills_dir=tmp_path)
    skill = skills[0]
    body = read_skill_reference(skill, "references/extra.md")
    assert body == "extra body"


def test_read_skill_reference_rejects_undeclared(tmp_path):
    _write_folder_skill(tmp_path, "demo", dedent("""\
        ---
        name: demo
        ---
        ## Workflow
        Body.
    """), references={"sneaky.md": "sneaky"})
    skills = load_skills(skills_dir=tmp_path)
    skill = skills[0]
    assert read_skill_reference(skill, "references/sneaky.md") is None


def test_read_skill_reference_rejects_path_traversal(tmp_path):
    _write_folder_skill(tmp_path, "demo", dedent("""\
        ---
        name: demo
        references:
          - ../escape.md
        ---
        ## Workflow
        Body.
    """))
    (tmp_path / "escape.md").write_text("escaped", encoding="utf-8")
    skills = load_skills(skills_dir=tmp_path)
    skill = skills[0]
    assert read_skill_reference(skill, "../escape.md") is None


def test_read_skill_reference_returns_none_for_flat_skill(tmp_path):
    _write_flat_skill(tmp_path, "---\nname: flat\n---\nbody", filename="flat.md")
    skills = load_skills(skills_dir=tmp_path)
    assert read_skill_reference(skills[0], "anything.md") is None


# ── Real skills directory ────────────────────────────────────────────────────

def test_real_skills_directory_loads():
    """The five legacy flat skills in skills/ still load."""
    skills = load_skills()
    names = {s.name for s in skills}
    expected = {
        "morning_brief",
        "weekly_review",
        "commitment_check",
        "draft_reply_to_contact",
        "prep_for_meeting",
    }
    assert expected.issubset(names)


def test_real_skills_appear_in_index():
    skills = load_skills()
    index = build_index(skills)
    for name in ("morning_brief", "weekly_review", "commitment_check"):
        assert f"- {name}" in index


# ── sync_repo_skills_to_user_dir ─────────────────────────────────────────────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_sync_creates_flat_and_folder_skills_in_user_dir(tmp_path):
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write(repo / "alpha.md", "---\nname: alpha\nversion: 1\n---\nbody A")
    _write(repo / "beta" / "SKILL.md", "---\nname: beta\nversion: 1\n---\nbody B")

    counts = sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert counts == {"created": 2, "updated": 0, "unchanged": 0}
    assert (user / "alpha" / "SKILL.md").read_text() == "---\nname: alpha\nversion: 1\n---\nbody A"
    assert (user / "beta" / "SKILL.md").read_text() == "---\nname: beta\nversion: 1\n---\nbody B"


def test_sync_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write(repo / "alpha.md", "---\nname: alpha\nversion: 1\n---\nbody")

    sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)
    counts = sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert counts == {"created": 0, "updated": 0, "unchanged": 1}


def test_sync_overwrites_drifted_user_copy(tmp_path):
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write(repo / "alpha.md", "---\nname: alpha\nversion: 1\n---\nfresh body")

    sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)
    # Simulate drift: someone edited the user copy directly
    (user / "alpha" / "SKILL.md").write_text("STALE")

    counts = sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert counts == {"created": 0, "updated": 1, "unchanged": 0}
    assert (user / "alpha" / "SKILL.md").read_text() == "---\nname: alpha\nversion: 1\n---\nfresh body"


def test_sync_preserves_user_only_skills(tmp_path):
    """Skills installed via skill_install (no repo counterpart) must survive sync."""
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write(repo / "alpha.md", "---\nname: alpha\nversion: 1\n---\nrepo skill")
    _write(user / "user_only" / "SKILL.md", "---\nname: user_only\nversion: 1\n---\nlocal install")

    sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert (user / "user_only" / "SKILL.md").exists()
    assert (user / "user_only" / "SKILL.md").read_text() == "---\nname: user_only\nversion: 1\n---\nlocal install"


def test_sync_mirrors_folder_form_assets(tmp_path):
    """Folder-form skills with references/*.md should have those mirrored too."""
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _write(repo / "beta" / "SKILL.md", "---\nname: beta\nversion: 1\n---\nbody")
    _write(repo / "beta" / "references" / "extra.md", "reference content")

    sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert (user / "beta" / "references" / "extra.md").read_text() == "reference content"


def test_sync_skips_silently_when_repo_missing(tmp_path):
    user = tmp_path / "user"
    repo = tmp_path / "nope"  # does not exist

    counts = sync_repo_skills_to_user_dir(user_dir=user, repo_dir=repo)

    assert counts == {"created": 0, "updated": 0, "unchanged": 0}
    assert not user.exists()
