"""LifeContextSelector — wraps :func:`agent.life_context.build_system_prompt`.

The selector is intentionally thin: ``build_system_prompt`` already does the
heavy lifting (load soul + life context + capability block + schedule). The
selector adds a JSON-serializable provenance record that names which life
context sections were available so #33 can attribute prompt chunks back to
sources for trace analysis.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.life_context import (
    build_system_prompt,
    get_life_context_sections,
    get_owner_name,
)


class LifeContextSelector:
    """Build the base system prompt from soul + capabilities + life context.

    The selector caches the rendered system prompt across turns — same as
    the previous ``self._system_prompt`` attribute on Pepper — because the
    underlying file rarely changes mid-session. Callers must invalidate by
    calling :meth:`refresh` after a successful ``update_life_context`` write.
    """

    name = "life_context"

    def __init__(
        self,
        life_context_path: str,
        config: Any,
        capability_registry: Any | None = None,
    ) -> None:
        self._life_context_path = life_context_path
        self._config = config
        self._capability_registry = capability_registry
        self._cached_prompt: str | None = None
        self._cached_sections: dict[str, str] | None = None

    def refresh(self) -> None:
        """Drop cached prompt + sections so the next ``select`` call rebuilds.

        Called after the model rewrites ``life_context.md`` so subsequent
        turns pick up the new content.
        """
        self._cached_prompt = None
        self._cached_sections = None

    def prime(self, prompt: str) -> None:
        """Inject a pre-built system prompt, bypassing the lazy rebuild.

        Used by ``PepperCore.initialize`` so the assembler shares the exact
        string that's pinned to ``self._system_prompt`` for backwards-compat
        introspection. Also gives unit tests that set ``_system_prompt``
        directly (without invoking ``initialize``) a way to keep the
        assembler in sync without re-reading the life-context file.
        """
        self._cached_prompt = prompt
        # Sections cache stays None so the first reader still parses the file
        # — sections aren't reachable from a primed prompt string alone.

    def select(self) -> SelectorRecord:
        if self._cached_prompt is None:
            self._cached_prompt = build_system_prompt(
                self._life_context_path,
                self._config,
                self._capability_registry,
            )
            self._cached_sections = get_life_context_sections(
                self._life_context_path
            )

        sections = self._cached_sections or {}
        owner = ""
        try:
            owner = get_owner_name(self._life_context_path, self._config)
        except Exception:
            # get_owner_name does its own fallback; defence in depth.
            owner = ""

        provenance = {
            "selector": self.name,
            "life_context_path": self._life_context_path,
            "owner_name": owner,
            "sections_loaded": sorted(sections.keys()),
            "section_count": len(sections),
            "system_prompt_chars": len(self._cached_prompt or ""),
        }
        return SelectorRecord(
            name=self.name,
            content=self._cached_prompt or "",
            provenance=provenance,
        )
