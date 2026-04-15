import pytest
from agent.life_context import build_system_prompt, get_life_context_sections, get_owner_name, load_life_context


def test_load_life_context_returns_string():
    content = load_life_context("docs/LIFE_CONTEXT.md")
    assert isinstance(content, str)
    assert len(content) > 100


def test_load_life_context_missing_file():
    content = load_life_context("docs/nonexistent.md")
    assert content == "" or "not found" in content.lower()


def test_get_life_context_sections_returns_dict():
    sections = get_life_context_sections("docs/LIFE_CONTEXT.md")
    assert isinstance(sections, dict)
    assert len(sections) > 0


def test_get_life_context_sections_has_expected_keys():
    sections = get_life_context_sections("docs/LIFE_CONTEXT.md")
    keys_lower = [k.lower() for k in sections.keys()]
    assert any(
        "who" in k or "family" in k or "pattern" in k or "responsible" in k
        for k in keys_lower
    )


def test_build_system_prompt_contains_pepper():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert "Pepper" in prompt


def test_build_system_prompt_contains_life_context_content():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert len(prompt) > 500


def test_build_system_prompt_has_privacy_directive():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    # Should remind Pepper about privacy
    assert "privacy" in prompt.lower() or "personal data" in prompt.lower()


def test_get_owner_name_reads_identity_section():
    assert get_owner_name("docs/LIFE_CONTEXT.md") == "Jack Chan"
