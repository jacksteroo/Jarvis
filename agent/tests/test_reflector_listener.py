"""Unit tests for `agents.reflector.listener` URL conversion + lifecycle.

Live LISTEN/NOTIFY behaviour is exercised in the integration tier
(skipped without Postgres). Here we cover the deterministic helpers.
"""
from __future__ import annotations

import asyncio

from agents.reflector.listener import _to_libpq_url


class TestToLibpqUrl:
    def test_strips_asyncpg_prefix(self) -> None:
        assert (
            _to_libpq_url("postgresql+asyncpg://u:p@h:5432/d")
            == "postgresql://u:p@h:5432/d"
        )

    def test_passthrough_when_no_prefix(self) -> None:
        assert (
            _to_libpq_url("postgresql://u:p@h:5432/d")
            == "postgresql://u:p@h:5432/d"
        )

    def test_other_dialect_passthrough(self) -> None:
        # Unrelated dialect: we don't try to be smart, we just hand it
        # to asyncpg and let asyncpg reject it. Listed here so a future
        # contributor doesn't add over-aggressive parsing.
        assert _to_libpq_url("sqlite:///:memory:") == "sqlite:///:memory:"


class TestListenerLifecycleSimulated:
    """Without a live Postgres we can still simulate the stop event
    propagation: a long-running listener that respects `stop` should
    exit promptly when the event is set."""

    async def test_stop_event_breaks_loop(self) -> None:
        # We can't exercise asyncpg here, but we can verify the
        # asyncio.Event contract that the listener relies on.
        stop = asyncio.Event()

        async def waits() -> None:
            await stop.wait()

        async def setter() -> None:
            await asyncio.sleep(0.01)
            stop.set()

        await asyncio.wait_for(asyncio.gather(waits(), setter()), timeout=1.0)
        assert stop.is_set()
