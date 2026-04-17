"""Tests for Phase 6.7 — CommitmentFollowup."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.commitment_followup import CommitmentFollowup, DueCommitment


def _make_pepper(memory_results, hour=8, tz="America/Los_Angeles"):
    mem = MagicMock()
    mem.search_recall = AsyncMock(return_value=memory_results)
    config = MagicMock()
    config.TIMEZONE = tz
    pepper = MagicMock()
    pepper.memory = mem
    pepper.config = config
    return pepper


def _commitment(id_, content, created_at="2026-04-16T12:00:00"):
    return {"id": id_, "content": content, "created_at": created_at}


@pytest.mark.asyncio
async def test_morning_surfaces_today_cue(monkeypatch):
    # Force morning slot by patching datetime.now to return 8am
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 8, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: I'll reply to Sarah today"),
        _commitment(2, "COMMITMENT: [RESOLVED] I'll send the doc"),
    ])
    f = CommitmentFollowup(pepper)
    due = await f.find_due_commitments()

    assert len(due) == 1
    assert due[0].memory_id == 1
    assert due[0].slot == "morning"


@pytest.mark.asyncio
async def test_evening_surfaces_tonight_cue(monkeypatch):
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 22, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: I'll reply tonight"),
        _commitment(2, "COMMITMENT: I'll call her today"),
    ])
    f = CommitmentFollowup(pepper)
    due = await f.find_due_commitments()

    ids = {d.memory_id for d in due}
    assert 1 in ids  # tonight cue
    assert 2 not in ids  # today cue not surfaced in evening slot


@pytest.mark.asyncio
async def test_resolved_skipped(monkeypatch):
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 8, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: [RESOLVED] I'll send the doc today"),
    ])
    f = CommitmentFollowup(pepper)
    due = await f.find_due_commitments()
    assert due == []


@pytest.mark.asyncio
async def test_dedup_within_same_day(monkeypatch):
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 8, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: I'll reply to Sarah today"),
    ])
    f = CommitmentFollowup(pepper)
    first = await f.find_due_commitments()
    second = await f.find_due_commitments()
    assert len(first) == 1
    assert second == []  # already surfaced today


@pytest.mark.asyncio
async def test_unscoped_commitment_surfaces_in_morning_only(monkeypatch):
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 8, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: I'll look into the staging issue"),
    ])
    f = CommitmentFollowup(pepper)
    due = await f.find_due_commitments()
    assert len(due) == 1
    assert due[0].cue == "unscoped"


@pytest.mark.asyncio
async def test_unscoped_not_surfaced_in_evening(monkeypatch):
    from agent import commitment_followup as cf
    import datetime as real_dt

    class FakeDT:
        @staticmethod
        def now(_tz):
            return real_dt.datetime(2026, 4, 16, 22, 0, 0, tzinfo=_tz)

    monkeypatch.setattr(cf, "datetime", FakeDT)

    pepper = _make_pepper([
        _commitment(1, "COMMITMENT: I'll look into the staging issue"),
    ])
    f = CommitmentFollowup(pepper)
    due = await f.find_due_commitments()
    assert due == []


def test_format_single():
    items = [DueCommitment(memory_id=1, text="reply to Sarah", slot="morning", cue="today", created_at="")]
    msg = CommitmentFollowup.format_followup_message(items)
    assert "reply to Sarah" in msg
    assert msg.startswith("Follow-up")


def test_format_multiple():
    items = [
        DueCommitment(memory_id=i, text=f"thing {i}", slot="morning", cue="today", created_at="")
        for i in range(3)
    ]
    msg = CommitmentFollowup.format_followup_message(items)
    assert "3 open" in msg
    assert "thing 0" in msg and "thing 2" in msg


def test_format_empty():
    assert CommitmentFollowup.format_followup_message([]) == ""
