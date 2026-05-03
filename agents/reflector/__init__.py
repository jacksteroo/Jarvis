"""Reflector archetype — daily reflection over the previous 24h of traces.

First inhabitant of `agents/` per ADR-0004 + ADR-0006. Reads traces
read-only via `agent.traces.repository`, generates a short note **to
herself** (first-person, never audience-shaped), and writes it to the
`reflections` table.

The trigger is a Postgres `LISTEN/NOTIFY` channel
(`reflector_trigger`) that core's APScheduler fires once per day. The
listener is in `listener.py`; the reflection step is in `main.py`;
the prompt and parsing are in `prompt.py`; the persistence shape is
in `store.py`.

#40 (weekly + monthly rollups) extends this archetype with hierarchy
columns already present in the `reflections` table (`tier`,
`parent_reflection_ids`).
"""
