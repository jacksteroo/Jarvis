"""Unit tests for `agents.reflector.rollup` — windowing + pipeline.

Stubs the LLM + embed callables so the pipeline runs without a model
or DB. Covers:
  - weekly window resolution from a Sunday payload (UTC + non-UTC TZ)
  - monthly window resolution from a 1st-of-month payload
  - end-to-end rollup: 7 daily children → 1 weekly with parents linked
  - empty period: 0 children → 'quiet week' single-line reflection
  - duplicate-window collision is logged and skipped
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agents.reflector import rollup as rrollup
from agents.reflector import store as rstore


# ── Window helpers ──────────────────────────────────────────────────────────


class TestWeeklyWindowForPayload:
    def test_sunday_utc_payload_aligns_monday_to_next_monday(self) -> None:
        # 2026-05-03 is a Sunday.
        ws, we = rrollup.weekly_window_for_payload(
            "2026-05-03",
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        )
        assert ws == datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)  # Mon
        assert we == datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)  # next Mon

    def test_sunday_west_coast_payload_aligns_to_local_week(self) -> None:
        tz = ZoneInfo("America/Los_Angeles")  # PDT, UTC-7
        ws, we = rrollup.weekly_window_for_payload(
            "2026-05-03",
            tz=tz,
            now=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc),
        )
        # Local Monday 2026-04-27 00:00 PDT = 07:00Z
        assert ws == datetime(2026, 4, 27, 7, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 4, 7, 0, tzinfo=timezone.utc)

    def test_window_end_clipped_to_now(self) -> None:
        tz = ZoneInfo("UTC")
        # If we trigger Sunday 23:59 and now is mid-Sunday, window_end
        # should clip back to now.
        now = datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc)
        ws, we = rrollup.weekly_window_for_payload("2026-05-03", tz=tz, now=now)
        assert we == now

    def test_malformed_payload_falls_back_to_yesterday(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        ws, we = rrollup.weekly_window_for_payload("not-a-date", tz=tz, now=now)
        # Yesterday in UTC = 2026-05-03 (Sunday); calendar-week
        # window is Mon 2026-04-27 .. Mon 2026-05-04.
        assert ws == datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)

    def test_non_sunday_payload_aligns_to_iso_week(self) -> None:
        # 2026-04-29 is a Wednesday. The calendar-week answer is the
        # Mon..Mon window containing that Wednesday: 2026-04-27 →
        # 2026-05-04. (The previous defensive branch produced a
        # misaligned 7-day window — that branch is gone.)
        ws, we = rrollup.weekly_window_for_payload(
            "2026-04-29",
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc),
        )
        assert ws == datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc)


class TestMonthlyWindowForPayload:
    def test_first_of_month_payload(self) -> None:
        ws, we = rrollup.monthly_window_for_payload(
            "2026-05-01",
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        )
        # Covers all of April 2026.
        assert ws == datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)

    def test_january_wraps_to_december_of_prev_year(self) -> None:
        ws, we = rrollup.monthly_window_for_payload(
            "2027-01-01",
            tz=ZoneInfo("UTC"),
            now=datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc),
        )
        assert ws == datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)

    def test_local_tz_offset_applied(self) -> None:
        tz = ZoneInfo("America/Los_Angeles")  # PDT, UTC-7 in May
        ws, we = rrollup.monthly_window_for_payload(
            "2026-05-01",
            tz=tz,
            now=datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc),
        )
        # April 1 00:00 PDT = 07:00Z; May 1 00:00 PDT = 07:00Z.
        assert ws == datetime(2026, 4, 1, 7, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 1, 7, 0, tzinfo=timezone.utc)


# ── Pipeline tests ──────────────────────────────────────────────────────────


def _daily(
    *,
    text: str,
    window_start: datetime,
) -> rstore.Reflection:
    return rstore.Reflection(
        text=text,
        window_start=window_start,
        window_end=window_start + timedelta(days=1),
        tier=rstore.TIER_DAILY,
    )


def _weekly(
    *,
    text: str,
    window_start: datetime,
) -> rstore.Reflection:
    return rstore.Reflection(
        text=text,
        window_start=window_start,
        window_end=window_start + timedelta(days=7),
        tier=rstore.TIER_WEEKLY,
    )


def _make_session_factory(
    *,
    children: Sequence[rstore.Reflection],
    previous: Optional[rstore.Reflection],
    appended: list[rstore.Reflection],
    raise_duplicate: bool = False,
):
    @asynccontextmanager
    async def _factory():
        with patch.object(rstore, "ReflectionRepository") as RR:
            rr = MagicMock()
            # Rollups read children via window-based query, NOT
            # created_at-based query — that's the whole point of #40's
            # late-backfill correctness fix.
            rr.query_by_window = AsyncMock(return_value=list(children))
            rr.latest = AsyncMock(return_value=previous)

            async def _append(reflection):
                if raise_duplicate:
                    raise rstore.DuplicateReflectionError(
                        reflection.tier, reflection.window_start
                    )
                appended.append(reflection)
                return reflection

            rr.append = AsyncMock(side_effect=_append)
            RR.return_value = rr
            yield None

    return _factory


@pytest.mark.asyncio
class TestWeeklyRollupPipeline:
    async def test_seven_dailies_become_one_weekly(self) -> None:
        # Build 7 dailies covering Mon..Sun in UTC.
        week_start = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        children = [
            _daily(
                text=f"day {i}",
                window_start=week_start + timedelta(days=i),
            )
            for i in range(7)
        ]
        appended: list[rstore.Reflection] = []
        factory = _make_session_factory(
            children=children, previous=None, appended=appended
        )

        async def _chat(*, system_prompt, user_prompt, model, timeout_s):
            return ("the week leaned quiet, with a steady rhythm.", "test-model")

        async def _embed(*, text, model, timeout_s):
            return [0.0] * rstore.REFLECTION_EMBEDDING_DIM

        result = await rrollup.run_weekly_rollup(
            payload="2026-05-03",  # Sunday
            session_factory=factory,
            chat_fn=_chat,
            embed_fn=_embed,
            chat_model="test-model",
            chat_timeout_s=10.0,
            embed_timeout_s=10.0,
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc),
        )

        assert result is not None
        assert len(appended) == 1
        stored = appended[0]
        assert stored.tier == rstore.TIER_WEEKLY
        assert stored.parent_reflection_ids is not None
        assert len(stored.parent_reflection_ids) == 7
        assert stored.window_start == datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
        assert stored.metadata_["child_count"] == 7
        assert stored.metadata_["child_tier"] == rstore.TIER_DAILY

    async def test_quiet_week_produces_short_reflection(self) -> None:
        appended: list[rstore.Reflection] = []
        factory = _make_session_factory(
            children=[], previous=None, appended=appended
        )

        async def _chat(*, system_prompt, user_prompt, model, timeout_s):
            # Emulate what the prompt requests for an empty period.
            return ("a quiet week.", "test-model")

        async def _embed(*, text, model, timeout_s):
            return None

        result = await rrollup.run_weekly_rollup(
            payload="2026-05-03",
            session_factory=factory,
            chat_fn=_chat,
            embed_fn=_embed,
            chat_model="test-model",
            chat_timeout_s=10.0,
            embed_timeout_s=10.0,
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc),
        )

        assert result is not None
        assert appended[0].metadata_["child_count"] == 0
        assert appended[0].parent_reflection_ids is None

    async def test_duplicate_weekly_window_is_skipped(self) -> None:
        appended: list[rstore.Reflection] = []
        factory = _make_session_factory(
            children=[], previous=None, appended=appended, raise_duplicate=True
        )

        async def _chat(*, system_prompt, user_prompt, model, timeout_s):
            return ("a quiet week.", "test-model")

        async def _embed(*, text, model, timeout_s):
            return None

        result = await rrollup.run_weekly_rollup(
            payload="2026-05-03",
            session_factory=factory,
            chat_fn=_chat,
            embed_fn=_embed,
            chat_model="test-model",
            chat_timeout_s=10.0,
            embed_timeout_s=10.0,
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc),
        )
        # Repository raises DuplicateReflectionError → rollup returns None.
        assert result is None
        assert appended == []

    async def test_empty_response_is_skipped(self) -> None:
        appended: list[rstore.Reflection] = []
        factory = _make_session_factory(
            children=[], previous=None, appended=appended
        )

        async def _chat(*, system_prompt, user_prompt, model, timeout_s):
            return ("", "test-model")

        async def _embed(*, text, model, timeout_s):
            return None

        result = await rrollup.run_weekly_rollup(
            payload="2026-05-03",
            session_factory=factory,
            chat_fn=_chat,
            embed_fn=_embed,
            chat_model="test-model",
            chat_timeout_s=10.0,
            embed_timeout_s=10.0,
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc),
        )
        assert result is None
        assert appended == []


@pytest.mark.asyncio
class TestMonthlyRollupPipeline:
    async def test_four_weeklies_become_one_monthly(self) -> None:
        # April 2026 weeklies, in UTC.
        weekly_starts = [
            datetime(2026, 3, 30, 0, 0, tzinfo=timezone.utc),  # week of Mar30-Apr5
            datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc),
        ]
        children = [
            _weekly(text=f"week starting {ws.date()}", window_start=ws)
            for ws in weekly_starts
        ]
        appended: list[rstore.Reflection] = []
        factory = _make_session_factory(
            children=children, previous=None, appended=appended
        )

        async def _chat(*, system_prompt, user_prompt, model, timeout_s):
            return ("April held a steadier tempo than March did.", "test-model")

        async def _embed(*, text, model, timeout_s):
            return None

        result = await rrollup.run_monthly_rollup(
            payload="2026-05-01",  # rollup covers April 2026
            session_factory=factory,
            chat_fn=_chat,
            embed_fn=_embed,
            chat_model="test-model",
            chat_timeout_s=10.0,
            embed_timeout_s=10.0,
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc),
        )

        assert result is not None
        stored = appended[0]
        assert stored.tier == rstore.TIER_MONTHLY
        assert stored.window_start == datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        assert stored.metadata_["child_count"] == 4
        assert stored.metadata_["child_tier"] == rstore.TIER_WEEKLY
