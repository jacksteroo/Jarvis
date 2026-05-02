"""Schema and coverage tests for the Phase 0 failure-seed battery.

The battery (`tests/failure_seed_battery.jsonl`) is the deterministic input
to the router-failure audit. These tests pin its shape so future runs can
regenerate or extend it without silently drifting.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

BATTERY_PATH = Path(__file__).resolve().parent / "failure_seed_battery.jsonl"

EXPECTED_CATEGORIES = {
    "Travel",
    "Family",
    "Health",
    "Partner",
    "Calendar",
    "Communications",
    "Finance",
    "Meal",
    "Proactive",
    "Knowledge",
}

# Mirrors agent.query_router.IntentType. Kept duplicated to avoid pulling
# the agent runtime into the battery test.
VALID_INTENTS = {
    "capability_check",
    "inbox_summary",
    "action_items",
    "person_lookup",
    "conversation_lookup",
    "schedule_lookup",
    "cross_source_triage",
    "general_chat",
    "unknown",
}

REQUIRED_FIELDS = {
    "id",
    "category",
    "query",
    "difficulty",
    "expected_intent",
    "expected_tools",
}


def _load() -> list[dict]:
    with BATTERY_PATH.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_battery_has_one_hundred_rows():
    assert len(_load()) == 100


def test_every_row_has_required_fields():
    for row in _load():
        missing = REQUIRED_FIELDS - row.keys()
        assert not missing, f"{row.get('id')!r} missing fields: {missing}"


def test_ids_and_queries_are_unique():
    rows = _load()
    ids = [r["id"] for r in rows]
    queries = [r["query"] for r in rows]
    assert len(set(ids)) == len(ids), "duplicate id in battery"
    assert len(set(queries)) == len(queries), "duplicate query in battery"


def test_uniform_category_weighting():
    counts = Counter(r["category"] for r in _load())
    assert set(counts) == EXPECTED_CATEGORIES
    assert all(n == 10 for n in counts.values()), counts


def test_difficulty_in_range():
    for row in _load():
        assert row["difficulty"] in (1, 2, 3), row["id"]


def test_expected_intent_is_valid():
    for row in _load():
        assert row["expected_intent"] in VALID_INTENTS, row


def test_expected_tools_is_list_of_strings():
    for row in _load():
        tools = row["expected_tools"]
        assert isinstance(tools, list)
        assert all(isinstance(t, str) and t for t in tools)


def test_query_is_non_empty_string():
    for row in _load():
        q = row["query"]
        assert isinstance(q, str) and q.strip(), row["id"]
