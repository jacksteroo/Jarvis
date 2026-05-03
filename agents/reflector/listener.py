"""Postgres `LISTEN/NOTIFY` client for the reflector triggers.

The reflector subscribes to one or more channels — daily at minimum
(`reflector_trigger`), plus weekly + monthly rollup channels added in
#40 (`reflector_weekly_trigger`, `reflector_monthly_trigger`). Pepper
Core's APScheduler fires one NOTIFY per cadence:

- daily: 23:55 local (Epic 01 #23)
- weekly: Sunday 23:55 local (#40)
- monthly: day-1 00:05 local (#40)

The connection is opened with raw asyncpg, NOT through SQLAlchemy:
LISTEN holds an idle connection forever, which is incompatible with
the pool. The SQLAlchemy URL passed to the reflector uses the
`+asyncpg` driver prefix; we strip it here to get a libpq-equivalent
URL asyncpg accepts directly.

Failure modes:
- Connection drops → the listener task raises; the runner sees the
  exception and surfaces it in logs. Docker-compose `restart:
  unless-stopped` brings the process back. ADR-0006's tripwire — if
  this happens more than once per minute, revisit supervision.
- Notify payload is malformed → we log and ignore; the reflector
  uses its own clock for the window, not the payload, so a bad
  payload only loses the trigger for that cadence.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import asyncpg
import structlog

logger = structlog.get_logger(__name__)


def _to_libpq_url(sqlalchemy_url: str) -> str:
    """`postgresql+asyncpg://...` → `postgresql://...`.

    Other shapes pass through. asyncpg accepts the bare libpq form.
    """
    prefix = "postgresql+asyncpg://"
    if sqlalchemy_url.startswith(prefix):
        return "postgresql://" + sqlalchemy_url[len(prefix) :]
    return sqlalchemy_url


async def listen_for_triggers(
    *,
    sqlalchemy_url: str,
    channels: Iterable[str],
    stop: asyncio.Event,
) -> AsyncIterator[tuple[str, str]]:
    """Yield each NOTIFY as (channel, payload) until `stop` is set.

    Opens a single dedicated asyncpg connection and registers a
    listener for every channel in `channels`. Yields a tuple so the
    caller can dispatch by cadence (daily / weekly / monthly).

    Exits cleanly when `stop` is set (the runner sets it on
    SIGTERM/SIGINT) or when the connection drops; re-establishing is
    the operator's responsibility (docker `restart: unless-stopped`).
    """
    libpq_url = _to_libpq_url(sqlalchemy_url)
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    channel_list = list(channels)
    if not channel_list:
        raise ValueError("listen_for_triggers requires at least one channel")

    def _on_notify(
        connection: asyncpg.Connection,
        pid: int,
        ch: str,
        payload: str,
    ) -> None:
        # asyncpg invokes this callback synchronously from the
        # connection's event loop; queue.put_nowait is safe.
        try:
            queue.put_nowait((ch, payload))
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            logger.warning("reflector_listener_queue_full", channel=ch)

    conn = await asyncpg.connect(libpq_url)
    for ch in channel_list:
        await conn.add_listener(ch, _on_notify)
    logger.info("reflector_listener_started", channels=channel_list)

    try:
        while not stop.is_set():
            stop_task = asyncio.create_task(stop.wait(), name="listener-stop")
            payload_task = asyncio.create_task(queue.get(), name="listener-payload")
            done, pending = await asyncio.wait(
                {stop_task, payload_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if stop_task in done:
                break
            ch, payload = payload_task.result()
            yield ch, payload
    finally:
        for ch in channel_list:
            try:
                await conn.remove_listener(ch, _on_notify)
            except Exception:  # pragma: no cover — best-effort
                pass
        await conn.close()
        logger.info("reflector_listener_stopped", channels=channel_list)
