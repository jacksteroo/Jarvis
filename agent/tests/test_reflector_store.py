"""Unit tests for `agents.reflector.store` — surface guarantees + dataclass invariants.

Mirrors the discipline of `test_traces_repository.py`: validate the
no-mutation surface via `inspect`, validate the dataclass guards via
direct construction. Behaviour against a live DB is exercised by the
integration test in `test_reflector_integration.py` (skipped without
Postgres).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from agents.reflector.store import (
    REFLECTION_EMBEDDING_DIM,
    TIER_DAILY,
    TIER_MONTHLY,
    TIER_WEEKLY,
    Reflection,
    ReflectionRepository,
)


class TestNoMutationSurface:
    forbidden_prefixes = ("update", "delete", "purge", "drop", "truncate", "remove")

    def test_no_method_with_forbidden_prefix(self) -> None:
        members = inspect.getmembers(ReflectionRepository, predicate=inspect.isfunction)
        bad = [
            name
            for name, _ in members
            if any(name.startswith(p) for p in self.forbidden_prefixes)
            and not name.startswith("_")
        ]
        assert bad == [], f"forbidden mutation methods exposed: {bad}"

    def test_only_documented_public_methods(self) -> None:
        public = sorted(
            name
            for name, _ in inspect.getmembers(
                ReflectionRepository, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        )
        # If this list grows, the no-mutation discipline must be re-checked.
        assert public == ["append", "get_by_id", "latest", "query"]


class TestReflectionDataclassGuards:
    def _make(self, **overrides) -> Reflection:
        now = datetime.now(timezone.utc)
        defaults = dict(
            text="i noticed i was tired today.",
            window_start=now - timedelta(hours=24),
            window_end=now,
        )
        defaults.update(overrides)
        return Reflection(**defaults)

    def test_default_tier_is_daily(self) -> None:
        r = self._make()
        assert r.tier == TIER_DAILY

    def test_invalid_tier_rejected(self) -> None:
        with pytest.raises(ValueError, match="tier"):
            self._make(tier="quarterly")

    def test_inverted_window_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="window_end"):
            Reflection(
                text="x",
                window_start=now,
                window_end=now - timedelta(hours=1),
            )

    def test_wrong_dim_embedding_rejected(self) -> None:
        with pytest.raises(ValueError, match="dim"):
            self._make(embedding=[0.0] * 16)

    def test_correct_dim_embedding_accepted(self) -> None:
        r = self._make(embedding=[0.0] * REFLECTION_EMBEDDING_DIM)
        assert r.embedding is not None
        assert len(r.embedding) == REFLECTION_EMBEDDING_DIM

    def test_weekly_and_monthly_tiers_accepted(self) -> None:
        # Schema-level support landed in #39 so #40 doesn't need a migration.
        for tier in (TIER_WEEKLY, TIER_MONTHLY):
            r = self._make(tier=tier)
            assert r.tier == tier

    def test_frozen_cannot_mutate(self) -> None:
        r = self._make()
        with pytest.raises(Exception):  # FrozenInstanceError
            r.text = "different"  # type: ignore[misc]
