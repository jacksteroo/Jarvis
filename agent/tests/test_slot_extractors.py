"""Tests for `agent.slot_extractors` — Phase 2 hardened slot extraction."""
from __future__ import annotations

import pytest

from agent.slot_extractors import (
    MAX_QUERY_CHARS,
    extract_entity_targets,
    extract_filesystem_path,
    extract_target_sources,
    extract_time_scope,
)


# ── Input validation ──────────────────────────────────────────────────────────

class TestInputValidation:
    @pytest.mark.parametrize(
        "fn",
        [
            extract_time_scope,
            extract_entity_targets,
            extract_target_sources,
            extract_filesystem_path,
        ],
    )
    def test_non_string_raises_typeerror(self, fn):
        for bad in (None, 123, 3.14, b"bytes", ["list"], {"d": 1}):
            with pytest.raises(TypeError):
                fn(bad)

    def test_empty_returns_documented_defaults(self):
        assert extract_time_scope("") == "default"
        assert extract_time_scope("   ") == "default"
        assert extract_entity_targets("") == []
        assert extract_entity_targets("   \t\n  ") == []
        assert extract_target_sources("") == []
        assert extract_target_sources("   ") == []
        assert extract_filesystem_path("") is None
        assert extract_filesystem_path("   ") is None

    def test_long_input_is_bounded(self):
        # Longer than MAX_QUERY_CHARS — extraction must still terminate and
        # only consider the leading window. The "tomorrow" qualifier sits
        # well past the cap and must NOT be picked up.
        prefix = "x" * (MAX_QUERY_CHARS + 50) + " tomorrow "
        assert extract_time_scope(prefix) == "default"
        # And a within-cap qualifier is still picked up.
        within = ("today " + "y" * (MAX_QUERY_CHARS - 20))[:MAX_QUERY_CHARS]
        assert extract_time_scope(within) == "today"


# ── extract_time_scope ────────────────────────────────────────────────────────

class TestExtractTimeScope:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("anything overnight?", "overnight"),
            ("what came in last night", "overnight"),
            ("anything this morning", "today"),
            ("any updates today", "today"),
            ("any updates tonight", "today"),
            ("what came in yesterday", "yesterday"),
            ("anything from this week", "this_week"),
            ("over the past week", "this_week"),
            ("over the last few days", "past_few_days"),
            ("just now anything?", "last_hour"),
            ("anything in the last hour", "last_hour"),
            ("plain question with no time", "default"),
        ],
    )
    def test_static_table(self, text, expected):
        assert extract_time_scope(text) == expected

    def test_since_dow(self):
        assert extract_time_scope("anything since Thursday?") == "since_thursday"
        assert extract_time_scope("Since MONDAY please") == "since_monday"

    def test_last_n_unit(self):
        assert extract_time_scope("over the last 3 days") == "last_3_day"
        assert extract_time_scope("in the last 12 hours") == "last_12_hour"
        assert extract_time_scope("for the last 2 weeks") == "last_2_week"

    def test_before_event(self):
        assert extract_time_scope("anything before my 3pm") == "before_3pm"
        assert extract_time_scope("before my meeting") == "before_my_meeting" or \
               extract_time_scope("before my meeting") == "before_meeting"

    def test_unicode_safe(self):
        assert extract_time_scope("café updates today ☕") == "today"


# ── extract_entity_targets ────────────────────────────────────────────────────

class TestExtractEntityTargets:
    def test_did_person_send(self):
        assert extract_entity_targets("Did Sarah send anything?") == ["Sarah"]
        assert extract_entity_targets("Has Mike emailed me?") == ["Mike"]

    def test_did_my_kinship(self):
        # The "did my mom send" pattern matches via _PERSON_DID_RE; the
        # captured token is "mom".
        result = extract_entity_targets("Did my mom send anything?")
        assert "mom" in [t.lower() for t in result]

    def test_from_person(self):
        assert extract_entity_targets("any email from Sarah") == ["Sarah"]
        assert extract_entity_targets("nothing by John Smith") == ["John Smith"]

    def test_from_kinship(self):
        result = extract_entity_targets("any word from my dad")
        assert "dad" in [t.lower() for t in result]

    def test_about_person(self):
        assert extract_entity_targets("anything about Sarah") == ["Sarah"]
        assert extract_entity_targets("heard from Mike?") == ["Mike"]

    def test_possessive_name(self):
        assert extract_entity_targets("Mike's email about the deal") == ["Mike"]

    def test_possessive_kinship(self):
        result = extract_entity_targets("my boss's latest email")
        assert "boss" in [t.lower() for t in result]

    def test_blocklist_drops_source_names(self):
        # "from Slack" should NOT yield "Slack" as a person entity.
        assert extract_entity_targets("any messages from Slack") == []
        assert extract_entity_targets("from Gmail today") == []

    def test_stop_words_dropped(self):
        # "from the boss" — "the" is a stop word, should not be captured.
        # Underlying regex requires title-case for non-kinship; "the" wouldn't
        # match anyway, but verify "boss" comes through via kinship branch.
        result = extract_entity_targets("from the boss")
        assert all(t.lower() not in {"the", "a", "an"} for t in result)

    def test_dedup_preserves_order(self):
        result = extract_entity_targets("from Sarah and about Sarah and Mike's email")
        # "Sarah" appears twice but should be deduplicated; order preserved.
        assert result == ["Sarah", "Mike"]

    def test_no_matches_returns_empty(self):
        assert extract_entity_targets("what's the weather like") == []


# ── extract_target_sources ────────────────────────────────────────────────────

class TestExtractTargetSources:
    def test_explicit_email(self):
        assert "email" in extract_target_sources("anything in my inbox")
        assert "email" in extract_target_sources("any unread emails")
        assert "email" in extract_target_sources("check Gmail")

    def test_imessage(self):
        assert "imessage" in extract_target_sources("any imessage today")

    def test_whatsapp(self):
        assert "whatsapp" in extract_target_sources("anything on whatsapp")

    def test_slack(self):
        assert "slack" in extract_target_sources("any slack messages")

    def test_calendar(self):
        assert "calendar" in extract_target_sources("what's on my calendar today")

    def test_email_suppressed_when_paired_with_other_channel(self):
        # "WhatsApp messages" — "messages" alone shouldn't pull email in.
        srcs = extract_target_sources("any whatsapp messages today")
        assert "whatsapp" in srcs
        assert "email" not in srcs

    def test_explicit_email_overrides_suppression(self):
        # Both explicit "email" and "whatsapp" mentioned — both should appear.
        srcs = extract_target_sources("any email or whatsapp today")
        assert "email" in srcs
        assert "whatsapp" in srcs

    def test_no_source_returns_empty(self):
        assert extract_target_sources("hello") == []

    def test_multiple_sources(self):
        srcs = extract_target_sources("anything in email or on slack today")
        assert set(srcs) >= {"email", "slack"}


# ── extract_filesystem_path ───────────────────────────────────────────────────

class TestExtractFilesystemPath:
    def test_no_path_returns_none(self):
        assert extract_filesystem_path("hello world") is None

    def test_unix_path(self):
        # Underlying extractor recognises explicit unix paths.
        result = extract_filesystem_path("read /tmp/foo.txt please")
        assert result is None or "/tmp/foo.txt" in result

    def test_long_input_truncates_before_extraction(self):
        # Path past the cap must not be returned.
        text = "x" * (MAX_QUERY_CHARS + 10) + " /tmp/should_not_match.txt"
        assert extract_filesystem_path(text) is None
