"""Unit tests for `agents.reflector.prompt` — voice rules + prompt shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.error_classifier import DataSensitivity
from agent.traces.schema import Archetype, Trace, TriggerSource
from agents.reflector.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
    summarize_trace,
    voice_violations,
)


def _trace(input_: str = "hi", output_: str = "hello", **overrides) -> Trace:
    """Construct a minimal Trace for prompt tests."""
    base = dict(
        trigger_source=TriggerSource.USER,
        archetype=Archetype.ORCHESTRATOR,
        input=input_,
        output=output_,
        data_sensitivity=DataSensitivity.LOCAL_ONLY,
    )
    base.update(overrides)
    return Trace(**base)


class TestSystemPrompt:
    def test_system_prompt_forbids_jack_should(self) -> None:
        # The forbidden phrase appears in the system prompt as a NEGATIVE
        # example ("never use 'Jack should…'"). The test confirms the
        # negative framing is present, not that the phrase is absent.
        assert "Jack should" in SYSTEM_PROMPT
        assert "never use" in SYSTEM_PROMPT.lower()
        assert "first-person" in SYSTEM_PROMPT.lower()

    def test_prompt_version_is_pinned(self) -> None:
        assert PROMPT_VERSION == "reflector-daily-v0"


class TestSummarizeTrace:
    def test_clips_long_input(self) -> None:
        t = _trace(input_="x" * 5000)
        d = summarize_trace(t, max_field_chars=100)
        assert len(d.input) <= 100
        assert d.input.endswith("...")

    def test_short_input_not_clipped(self) -> None:
        t = _trace(input_="hello there")
        d = summarize_trace(t, max_field_chars=100)
        assert d.input == "hello there"

    def test_carries_metadata(self) -> None:
        t = _trace(
            archetype=Archetype.REFLECTOR,
            trigger_source=TriggerSource.SCHEDULER,
        )
        d = summarize_trace(t)
        assert d.archetype == "reflector"
        assert d.trigger_source == "scheduler"


class TestRenderUserPrompt:
    def test_empty_window_says_no_turns(self) -> None:
        now = datetime.now(timezone.utc)
        prompt = render_user_prompt(
            window_start=now - timedelta(hours=24),
            window_end=now,
            digests=[],
            previous_reflection_text=None,
        )
        assert "No agent turns" in prompt
        assert "(none — this is the first one)" in prompt

    def test_continuity_includes_previous_reflection(self) -> None:
        now = datetime.now(timezone.utc)
        digests = [summarize_trace(_trace())]
        prompt = render_user_prompt(
            window_start=now - timedelta(hours=24),
            window_end=now,
            digests=digests,
            previous_reflection_text="I noticed I was tired yesterday.",
        )
        assert "Previous day's reflection (yours):" in prompt
        assert "I noticed I was tired yesterday." in prompt

    def test_traces_rendered_chronologically(self) -> None:
        now = datetime.now(timezone.utc)
        digests = [summarize_trace(_trace(input_="first")), summarize_trace(_trace(input_="second"))]
        prompt = render_user_prompt(
            window_start=now - timedelta(hours=24),
            window_end=now,
            digests=digests,
            previous_reflection_text=None,
        )
        # Caller is responsible for sorting; the prompt renders in the
        # order it receives. Just confirm both inputs appear.
        assert "in: first" in prompt
        assert "in: second" in prompt
        assert prompt.index("first") < prompt.index("second")

    def test_window_header_is_iso_utc(self) -> None:
        start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        prompt = render_user_prompt(
            window_start=start,
            window_end=end,
            digests=[],
            previous_reflection_text=None,
        )
        assert "2026-05-01T12:00:00+00:00" in prompt
        assert "2026-05-02T12:00:00+00:00" in prompt


class TestVoiceViolations:
    @pytest.mark.parametrize(
        "text,expected_label",
        [
            ("Jack should consider going to bed earlier.", "jack should"),
            ("TLDR: he was tired.", "tldr"),
            ("TL;DR — busy day.", "tldr"),
            ("Recommendations: nap more.", "recommendation framing"),
            ("I recommend that you take a walk.", "recommendation framing"),
            ("Action item: schedule a call.", "action item"),
            ("Action items:\n- thing", "action item"),
            ("Next steps: review the doc.", "next steps"),
            ("Next step: review.", "next steps"),
            ("Follow-up: check on this thursday.", "followup label"),
            ("Followups:\n- thing", "followup label"),
            ("To-do: \n- thing", "todo label"),
            ("TODO:\nthing", "todo label"),
        ],
    )
    def test_audience_phrases_flagged(self, text: str, expected_label: str) -> None:
        v = voice_violations(text)
        assert expected_label in v, f"expected {expected_label!r} in violations: {v!r}"

    @pytest.mark.parametrize(
        "text",
        [
            # First-person continuous voice — must NOT trip the new
            # word-boundary rules even though earlier substring rules
            # would have falsely flagged these.
            "I noticed I was tired today, but the calendar still got handled.",
            "I didn't follow up on the email I wanted to.",
            "I'd recommend trying that route again.",
            "I felt I should just rest.",
            "Tomorrow feels like a quieter day.",
            "I noticed how busy the followups felt today.",
        ],
    )
    def test_first_person_does_not_trip(self, text: str) -> None:
        assert voice_violations(text) == [], (
            f"unexpected violations on first-person voice: {voice_violations(text)!r}"
        )

    def test_case_insensitive(self) -> None:
        assert "jack should" in voice_violations("JACK SHOULD relax")
        assert "tldr" in voice_violations("tldr nothing happened")
