"""CapabilityBlockSelector — surfaces the live capability registry block.

The capability block is already embedded in the system prompt by
``build_system_prompt`` (see :mod:`agent.life_context`). This selector exists
so #33 can attach a structured "what sources were available this turn?" record
to traces without re-parsing the rendered system prompt.

It does NOT add a separate string to the prompt — that would duplicate what's
already in the life-context system prompt. ``content`` is the rendered block
for diagnostic / test access; the assembler does not concatenate it again.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.life_context import build_capability_block


class CapabilityBlockSelector:
    name = "capability_block"

    def __init__(self, capability_registry: Any | None = None) -> None:
        self._registry = capability_registry

    def select(self) -> SelectorRecord:
        block = build_capability_block(self._registry)

        available: list[str] = []
        try:
            if self._registry is not None and hasattr(
                self._registry, "get_available_sources"
            ):
                available = list(self._registry.get_available_sources() or [])
        except Exception:
            # Registry probing is fail-soft — provenance reflects what we
            # actually saw, not what we wished for.
            available = []

        provenance = {
            "selector": self.name,
            "available_sources": sorted(available),
            "block_chars": len(block or ""),
            "registry_present": self._registry is not None,
        }
        return SelectorRecord(
            name=self.name,
            content=block,
            provenance=provenance,
        )
