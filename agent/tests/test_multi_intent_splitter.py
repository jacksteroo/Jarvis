"""Unit tests for ``agent.multi_intent_splitter``.

Covers:

- Existing boundary tokens (and / also / ; / ?).
- New boundary tokens (plus / then / & / as well as / em- and en-dash /
  hyphen between clauses / line breaks / "what about" prefixes).
- Quoted-span guard.
- Possessives stay intact.
- Empty / whitespace / non-string input.
- Singleton fallback when no boundary applies.
- Truncation guard for over-long input.
"""
from __future__ import annotations

import pytest

from agent.multi_intent_splitter import (
    MAX_QUERY_CHARS,
    split_multi_intent,
)


# ── Empty / type contract ────────────────────────────────────────────────────


def test_empty_string_returns_empty_list():
    assert split_multi_intent("") == []


def test_whitespace_only_returns_empty_list():
    assert split_multi_intent("   \n\t  ") == []


def test_non_string_raises_type_error():
    with pytest.raises(TypeError):
        split_multi_intent(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        split_multi_intent(42)  # type: ignore[arg-type]


# ── Singleton fallback ───────────────────────────────────────────────────────


def test_no_boundary_returns_singleton():
    assert split_multi_intent("what is on my calendar today") == [
        "what is on my calendar today"
    ]


def test_singleton_strips_outer_whitespace():
    assert split_multi_intent("   hello world   ") == ["hello world"]


# ── Existing boundary tokens ─────────────────────────────────────────────────


def test_splits_on_and():
    assert split_multi_intent("check my calendar and read my email") == [
        "check my calendar",
        "read my email",
    ]


def test_splits_on_also():
    assert split_multi_intent("check my calendar also read my email") == [
        "check my calendar",
        "read my email",
    ]


def test_splits_on_semicolon():
    assert split_multi_intent("check calendar; read email") == [
        "check calendar",
        "read email",
    ]


def test_splits_on_question_mark_between_sentences():
    assert split_multi_intent(
        "what's on my calendar today? Also check my email"
    ) == [
        "what's on my calendar today",
        "check my email",
    ]


# ── Newly added boundary tokens ──────────────────────────────────────────────


def test_splits_on_plus():
    assert split_multi_intent("check email plus read slack") == [
        "check email",
        "read slack",
    ]


def test_splits_on_then():
    assert split_multi_intent("check email then summarize slack") == [
        "check email",
        "summarize slack",
    ]


def test_splits_on_ampersand():
    assert split_multi_intent("check email & read slack") == [
        "check email",
        "read slack",
    ]


def test_splits_on_as_well_as():
    assert split_multi_intent("check email as well as read slack") == [
        "check email",
        "read slack",
    ]


def test_splits_on_em_dash():
    assert split_multi_intent("check my calendar — read my email") == [
        "check my calendar",
        "read my email",
    ]


def test_splits_on_en_dash():
    assert split_multi_intent("check my calendar – read my email") == [
        "check my calendar",
        "read my email",
    ]


def test_splits_on_spaced_hyphen():
    assert split_multi_intent("check my calendar - read my email") == [
        "check my calendar",
        "read my email",
    ]


def test_splits_on_line_break():
    assert split_multi_intent("check email\nread slack") == [
        "check email",
        "read slack",
    ]


def test_splits_on_what_about_prefix():
    assert split_multi_intent("check my calendar, what about my email") == [
        "check my calendar",
        "my email",
    ]


def test_three_way_split():
    assert split_multi_intent(
        "check email and read slack and summarize calendar"
    ) == [
        "check email",
        "read slack",
        "summarize calendar",
    ]


# ── Negative guards ──────────────────────────────────────────────────────────


def test_quoted_span_blocks_split_token():
    # The " and " inside the quotes must not split.
    assert split_multi_intent('reply to "hi and goodbye" thread') == [
        'reply to "hi and goodbye" thread'
    ]


def test_quoted_span_with_real_split_outside():
    assert split_multi_intent(
        'reply to "hi and goodbye" thread and check calendar'
    ) == [
        'reply to "hi and goodbye" thread',
        "check calendar",
    ]


def test_curly_quotes_protect_split_token():
    assert split_multi_intent("reply to “hi and bye” thread") == [
        "reply to “hi and bye” thread"
    ]


def test_possessive_not_split():
    # No whitespace boundary inside "Sara's"; splitter must leave it whole.
    assert split_multi_intent("read Sara's email") == ["read Sara's email"]


def test_hyphenated_word_not_split():
    # " - " requires surrounding spaces; "self-care" stays intact.
    assert split_multi_intent("plan self-care for tomorrow") == [
        "plan self-care for tomorrow"
    ]


def test_unbalanced_quote_does_not_crash():
    # No quote-span match → all boundaries apply normally.
    out = split_multi_intent('check email and "unfinished')
    assert out == ["check email", '"unfinished']


def test_single_fragment_after_drop_returns_singleton():
    # Trailing " and " with empty tail → fragment count drops below 2.
    assert split_multi_intent("check email and ") == ["check email and"]


# ── Truncation guard ─────────────────────────────────────────────────────────


def test_oversize_input_is_truncated():
    huge = "x" * (MAX_QUERY_CHARS + 500) + " and y"
    out = split_multi_intent(huge)
    # Truncated tail strips the " and y"; we get a singleton of x's.
    assert len(out) == 1
    assert out[0] == "x" * MAX_QUERY_CHARS


# ── Real-world phrasings ─────────────────────────────────────────────────────


def test_calendar_plus_email_phrasing():
    assert split_multi_intent("what's on my calendar and any urgent emails") == [
        "what's on my calendar",
        "any urgent emails",
    ]


def test_question_then_followup():
    out = split_multi_intent(
        "what's on my calendar today? plus any urgent emails"
    )
    # The "?" splits first; then " plus " splits the tail.
    assert out == ["what's on my calendar today", "any urgent emails"]


def test_semicolon_with_and_inside_chunks():
    assert split_multi_intent(
        "check email; summarize slack and read calendar"
    ) == [
        "check email",
        "summarize slack",
        "read calendar",
    ]
