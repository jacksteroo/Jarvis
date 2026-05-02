"""Unit tests for agent.battery_classifier (Phase 0 Task 4)."""

from __future__ import annotations

import json

import pytest

from agent.battery_classifier import (
    TAXONOMY,
    build_judge_prompt,
    extract_tool_names,
    parse_judgment,
)


def test_taxonomy_matches_migration_plan():
    # Frozen by docs/SEMANTIC_ROUTER_MIGRATION.md Phase 0 Task 4. Adding or
    # removing values requires a corresponding plan edit.
    assert TAXONOMY == (
        "ROUTING_MISS",
        "INTERCEPT_MISS",
        "HALLUCINATION",
        "SYNTHESIS_MISS",
        "TOOL_MISS",
        "CONTEXT_MISS",
        "OVER_INVOCATION",
        "STALE_MEMORY",
        "OTHER",
    )


def test_extract_tool_names_handles_known_shapes():
    calls = [
        {"function": {"name": "search_memory"}, "arguments": {"q": "x"}},
        {"name": "get_calendar_events_range", "arguments": {}},
        {"tool": "list_emails"},
        {"function": "raw_string"},  # malformed: function not dict
        "not-a-dict",
        {"args": {}},  # no name field at all
    ]
    assert extract_tool_names(calls) == [
        "search_memory",
        "get_calendar_events_range",
        "list_emails",
        "raw_string",
    ]


def test_extract_tool_names_returns_empty_for_non_list():
    assert extract_tool_names(None) == []
    assert extract_tool_names("calls") == []
    assert extract_tool_names({}) == []


def test_build_judge_prompt_contains_required_sections():
    record = {
        "battery_id": "travel-01",
        "category": "Travel",
        "query": "When does Matthew fly?",
        "expected_intent": "schedule_lookup",
        "expected_tools": ["search_memory"],
        "tool_calls": [{"function": {"name": "search_memory"}}],
        "model": "local/hermes3:latest",
        "response": "He flies June 22.",
        "notes": "Anchored in LIFE_CONTEXT.",
        "error": None,
    }
    prompt = build_judge_prompt(record, life_context="GROUND_TRUTH_HERE")
    assert "GROUND_TRUTH_HERE" in prompt
    assert "BATTERY RECORD" in prompt
    assert "ROUTING_MISS" in prompt
    assert "STRICT JSON" in prompt
    # Actual tools must be derived (not "expected_tools" verbatim) so the judge
    # can see what Pepper really called.
    assert '"actual_tools"' in prompt
    assert "search_memory" in prompt


def test_build_judge_prompt_truncates_long_response():
    record = {
        "battery_id": "x",
        "query": "q",
        "tool_calls": [],
        "response": "A" * 5000,
    }
    prompt = build_judge_prompt(record, life_context="LC")
    assert "...[truncated]" in prompt
    assert prompt.count("A") <= 4100  # truncated chunk plus a few stray As


def test_parse_judgment_success():
    raw = '{"success": true, "taxonomy": null, "reasoning": "matches LC"}'
    v = parse_judgment(raw)
    assert v == {
        "success": True,
        "taxonomy": None,
        "reasoning": "matches LC",
        "parse_error": None,
    }


def test_parse_judgment_failure_with_taxonomy():
    raw = '{"success": false, "taxonomy": "ROUTING_MISS", "reasoning": "wrong tool"}'
    v = parse_judgment(raw)
    assert v["success"] is False
    assert v["taxonomy"] == "ROUTING_MISS"
    assert v["parse_error"] is None


def test_parse_judgment_strips_prose_around_json():
    raw = "Here's my verdict:\n" + json.dumps(
        {"success": False, "taxonomy": "HALLUCINATION", "reasoning": "invented"}
    ) + "\nDone."
    v = parse_judgment(raw)
    assert v["success"] is False
    assert v["taxonomy"] == "HALLUCINATION"


def test_parse_judgment_invalid_taxonomy_marks_parse_error():
    raw = '{"success": false, "taxonomy": "MADE_UP", "reasoning": "x"}'
    v = parse_judgment(raw)
    assert v["parse_error"] is not None
    assert v["taxonomy"] == "OTHER"


def test_parse_judgment_missing_success_marks_parse_error():
    raw = '{"taxonomy": "OTHER", "reasoning": "x"}'
    v = parse_judgment(raw)
    assert v["parse_error"] is not None


def test_parse_judgment_empty_text():
    v = parse_judgment("")
    assert v["parse_error"] == "empty response"
    assert v["success"] is False
    assert v["taxonomy"] == "OTHER"


def test_parse_judgment_no_json_object():
    v = parse_judgment("the model refused to answer")
    assert v["parse_error"] == "no JSON object found"


def test_parse_judgment_bad_json():
    v = parse_judgment("{not really json}")
    assert v["parse_error"] is not None
    assert v["parse_error"].startswith("json decode")


def test_parse_judgment_loose_recovers_unquoted_taxonomy():
    # Real qwen2.5 output shape: bare enum + unquoted reasoning.
    raw = '{"success": false, "taxonomy": ROUTING_MISS, "reasoning": "wrong tool"}'
    v = parse_judgment(raw)
    assert v["success"] is False
    assert v["taxonomy"] == "ROUTING_MISS"
    assert v["parse_error"] == "loose_recovered"


def test_parse_judgment_loose_recovers_unquoted_reasoning():
    raw = '{"success": true, "taxonomy": null, "reasoning": Pepper got it right.}'
    v = parse_judgment(raw)
    assert v["success"] is True
    assert v["taxonomy"] is None
    assert "Pepper got it right" in v["reasoning"]
    assert v["parse_error"] == "loose_recovered"


def test_parse_judgment_loose_rejects_unknown_taxonomy():
    raw = '{"success": false, "taxonomy": MADE_UP, "reasoning": x}'
    v = parse_judgment(raw)
    # Loose recovery refuses an out-of-set taxonomy → falls through to _bad.
    assert v["parse_error"] is not None
    assert v["parse_error"] != "loose_recovered"


@pytest.mark.parametrize(
    "tax",
    list(TAXONOMY),
)
def test_parse_judgment_accepts_every_taxonomy_value(tax):
    raw = json.dumps({"success": False, "taxonomy": tax, "reasoning": "r"})
    v = parse_judgment(raw)
    assert v["taxonomy"] == tax
    assert v["parse_error"] is None


def test_summarize_aggregates_taxonomy_and_routing_share():
    from scripts.classify_battery_results import summarize

    verdicts = [
        {"verdict": {"success": True, "taxonomy": None}},
        {"verdict": {"success": False, "taxonomy": "ROUTING_MISS"}},
        {"verdict": {"success": False, "taxonomy": "INTERCEPT_MISS"}},
        {"verdict": {"success": False, "taxonomy": "HALLUCINATION"}},
        {"verdict": {"success": False, "taxonomy": "OTHER", "parse_error": "x"}},
    ]
    s = summarize(verdicts)
    assert s["total"] == 5
    assert s["successes"] == 1
    assert s["failures"] == 4
    assert s["routing_fixable"] == 2
    assert s["routing_fixable_share_of_failures"] == 0.5
    assert s["parse_errors"] == 1
    assert s["by_taxonomy"]["HALLUCINATION"] == 1


def test_summarize_handles_zero_failures():
    from scripts.classify_battery_results import summarize

    s = summarize([{"verdict": {"success": True, "taxonomy": None}}])
    assert s["routing_fixable_share_of_failures"] == 0.0
