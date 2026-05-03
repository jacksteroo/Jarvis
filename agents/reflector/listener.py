"""Postgres `LISTEN/NOTIFY` client for the reflector trigger.

The reflector subscribes to a single channel (default
`reflector_trigger`). Pepper Core's APScheduler fires one NOTIFY per
day at end-of-day with a date-string payload (see
`agent/scheduler.py:fire_reflector_trigger`).

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
  payload only loses the trigger for that day.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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
    channel: str,
    stop: asyncio.Event,
) -> AsyncIterator[str]:
    """Yield each NOTIFY payload received on `channel` until `stop` is set.

    Opens a dedicated asyncpg connection, registers a listener, and
    streams payloads. The function exits cleanly when `stop` is set
    (the runner sets it on SIGTERM/SIGINT) or when the connection
    drops. Re-establishing on drop is the operator's responsibility
    (docker `restart: unless-stopped`).
    """
    libpq_url = _to_libpq_url(sqlalchemy_url)
    queue: asyncio.Queue[str] = asyncio.Queue()

    def _on_notify(
        connection: asyncpg.Connection,
        pid: int,
        ch: str,
        payload: str,
    ) -> None:
        # Callbacks run in the asyncpg loop; queue.put_nowait is
        # async-safe because asyncpg notify dispatch is sync.
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            logger.warning("reflector_listener_queue_full", channel=ch)

    conn = await asyncpg.connect(libpq_url)
    await conn.add_listener(channel, _on_notify)
    logger.info("reflector_listener_started", channel=channel)

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
            payload = payload_task.result()
            yield payload
    finally:
        try:
            await conn.remove_listener(channel, _on_notify)
        except Exception:  # pragma: no cover — best-effort
            pass
        await conn.close()
        logger.info("reflector_listener_stopped", channel=channel)
