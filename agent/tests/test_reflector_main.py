"""Unit tests for `agents.reflector.main` reflection-pass logic.

Stubs the LLM + embed HTTP calls and the Postgres session factory so
the test runs without a DB or a model. The tests cover:

  - LLM-empty-response is treated as 'skip' (returns None)
  - voice violations are logged but the row is still persisted, and
    the violation labels are written to `metadata_.voice_violations`
  - the previous reflection is included as continuity
  - the embedding-failure path persists with a NULL embedding
  - `_window_for_payload` derives the correct local-day UTC window
  - a duplicate-window collision is logged-and-skipped (returns None)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from agent.error_classifier import DataSensitivity
from agent.traces.schema import Archetype, Trace, TriggerSource
from agents._shared.config import AgentRuntimeConfig
from agents.reflector import main as rmain
from agents.reflector import store as rstore


def _make_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        archetype="reflector",
        postgres_url="postgresql+asyncpg://test/test",
        log_level="DEBUG",
        notify_channel="reflector_trigger",
    )


def _trace(input_: str = "hi", output_: str = "hello", **overrides) -> Trace:
    base = dict(
        trigger_source=TriggerSource.USER,
        archetype=Archetype.ORCHESTRATOR,
        input=input_,
        output=output_,
        data_sensitivity=DataSensitivity.LOCAL_ONLY,
    )
    base.update(overrides)
    return Trace(**base)


class _StubSession:
    def __init__(
        self,
        *,
        traces: list[Trace] | None = None,
        previous: rstore.Reflection | None = None,
        appended: list[rstore.Reflection] | None = None,
    ) -> None:
        self.traces = traces or []
        self.previous = previous
        self.appended = appended if appended is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    # SQLAlchemy interface stubs
    def add(self, row):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass


def _stub_session_factory(
    *,
    traces: list[Trace],
    previous: rstore.Reflection | None,
    appended_sink: list[rstore.Reflection],
):
    """Returns a callable that yields a context-managed stub session.

    Patches `TraceRepository.query` and
    `ReflectionRepository.{latest, append}` to read/write from the
    in-memory backing.
    """

    @asynccontextmanager
    async def _factory():
        session = _StubSession(
            traces=traces,
            previous=previous,
            appended=appended_sink,
        )
        # Patch the two repository classes to use this stub session.
        with patch.object(rmain, "TraceRepository") as TR, patch.object(
            rstore, "ReflectionRepository"
        ) as RR:
            tr_instance = MagicMock()
            tr_instance.query = AsyncMock(return_value=traces)
            TR.return_value = tr_instance

            rr_instance = MagicMock()
            rr_instance.latest = AsyncMock(return_value=previous)

            async def _fake_append(reflection):
                appended_sink.append(reflection)
                return reflection

            rr_instance.append = AsyncMock(side_effect=_fake_append)
            RR.return_value = rr_instance

            yield session

    return _factory


@pytest.mark.asyncio
class TestRunOneReflection:
    async def test_empty_response_is_skipped(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[],
            previous=None,
            appended_sink=appended,
        )

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(return_value=("", "test-model")),
            ),
            patch.object(
                rmain,
                "_embed_reflection",
                AsyncMock(return_value=None),
            ),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )
        assert out is None
        assert appended == []

    async def test_non_empty_response_is_persisted(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[_trace(input_="how is the day", output_="quiet")],
            previous=None,
            appended_sink=appended,
        )

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(
                    return_value=(
                        "I felt unhurried today; one short check-in and "
                        "nothing else.",
                        "test-model",
                    )
                ),
            ),
            patch.object(
                rmain,
                "_embed_reflection",
                AsyncMock(return_value=[0.0] * rstore.REFLECTION_EMBEDDING_DIM),
            ),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        assert out is not None
        assert len(appended) == 1
        stored = appended[0]
        assert stored.tier == rstore.TIER_DAILY
        assert stored.trace_count == 1
        assert stored.model_used == "test-model"
        assert stored.embedding is not None
        assert len(stored.embedding) == rstore.REFLECTION_EMBEDDING_DIM
        assert stored.embedding_model_version == rstore.REFLECTION_EMBEDDING_MODEL_DEFAULT

    async def test_voice_violation_persists_but_records(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[_trace()],
            previous=None,
            appended_sink=appended,
        )

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(
                    return_value=(
                        "TLDR: jack should rest more.",
                        "test-model",
                    )
                ),
            ),
            patch.object(
                rmain,
                "_embed_reflection",
                AsyncMock(return_value=None),
            ),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        # v0 still persists — the operator should see what the model
        # produced. The rule labels go into `metadata_.voice_violations`
        # so #42's scoring tool can read them without re-running the
        # regex.
        assert out is not None
        assert len(appended) == 1
        violations = appended[0].metadata_.get("voice_violations") or []
        assert "tldr" in violations
        assert "jack should" in violations

    async def test_previous_reflection_is_used_for_continuity(self) -> None:
        cfg = _make_config()
        now = datetime.now(timezone.utc)
        previous = rstore.Reflection(
            text="Yesterday I was tired.",
            window_start=now - timedelta(hours=48),
            window_end=now - timedelta(hours=24),
        )
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[],
            previous=previous,
            appended_sink=appended,
        )

        seen_prompt: dict[str, Any] = {}

        async def _capture(**kwargs):
            seen_prompt["user_prompt"] = kwargs["user_prompt"]
            return ("today felt different.", "test-model")

        with (
            patch.object(rmain, "_generate_reflection_text", AsyncMock(side_effect=_capture)),
            patch.object(rmain, "_embed_reflection", AsyncMock(return_value=None)),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        assert out is not None
        assert "Yesterday I was tired." in seen_prompt["user_prompt"]
        assert appended[-1].previous_reflection_id == previous.reflection_id

    async def test_runaway_text_is_truncated_before_persist(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[_trace()], previous=None, appended_sink=appended
        )

        # 64 KB of model output — a runaway local model. We persist
        # only the first MAX_REFLECTION_TEXT_CHARS, with an ellipsis
        # marker, and record the original length in metadata.
        runaway = "i felt productive today. " * 4000
        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(return_value=(runaway, "test-model")),
            ),
            patch.object(rmain, "_embed_reflection", AsyncMock(return_value=None)),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        assert out is not None
        stored = appended[0]
        assert len(stored.text) <= rmain.MAX_REFLECTION_TEXT_CHARS
        assert stored.text.endswith("...")
        assert stored.metadata_["original_text_len"] == len(runaway)

    async def test_truncation_recorded_in_metadata(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        # Build exactly MAX_TRACES_PER_REFLECTION traces — repository
        # cap fires, truncation flag should be recorded.
        n = rmain.MAX_TRACES_PER_REFLECTION
        traces = [_trace(input_=f"t{i}") for i in range(n)]
        factory = _stub_session_factory(
            traces=traces, previous=None, appended_sink=appended
        )

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(return_value=("today was busy.", "test-model")),
            ),
            patch.object(rmain, "_embed_reflection", AsyncMock(return_value=None)),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        assert out is not None
        assert appended[0].metadata_.get("trace_truncated") is True

    async def test_duplicate_window_is_skipped(self) -> None:
        cfg = _make_config()

        # Custom factory that raises DuplicateReflectionError on append.
        @asynccontextmanager
        async def _factory():
            with patch.object(rmain, "TraceRepository") as TR, patch.object(
                rstore, "ReflectionRepository"
            ) as RR:
                tr = MagicMock()
                tr.query = AsyncMock(return_value=[_trace()])
                TR.return_value = tr

                rr = MagicMock()
                rr.latest = AsyncMock(return_value=None)

                async def _raise(reflection):
                    raise rstore.DuplicateReflectionError(
                        reflection.tier, reflection.window_start
                    )

                rr.append = AsyncMock(side_effect=_raise)
                RR.return_value = rr
                yield None

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(return_value=("today was quiet.", "test-model")),
            ),
            patch.object(rmain, "_embed_reflection", AsyncMock(return_value=None)),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=_factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        # Duplicate-window path returns None — caller logs and waits
        # for the next NOTIFY rather than retrying.
        assert out is None

    async def test_embedding_failure_does_not_block_persist(self) -> None:
        cfg = _make_config()
        appended: list[rstore.Reflection] = []
        factory = _stub_session_factory(
            traces=[_trace()],
            previous=None,
            appended_sink=appended,
        )

        with (
            patch.object(
                rmain,
                "_generate_reflection_text",
                AsyncMock(return_value=("today was quiet.", "test-model")),
            ),
            patch.object(rmain, "_embed_reflection", AsyncMock(return_value=None)),
        ):
            out = await rmain._run_one_reflection(
                config=cfg,
                session_factory=factory,
                payload="2026-05-01",
                tz=ZoneInfo("UTC"),
                now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
            )

        assert out is not None
        assert len(appended) == 1
        # Embedding-null path: the model_version stays NULL too.
        assert appended[0].embedding is None
        assert appended[0].embedding_model_version is None


class TestValidators:
    """Boot-time validation must refuse off-box Ollama URLs and
    malformed Postgres identifiers — these are the privacy-critical
    invariants the reflector enforces on startup."""

    def test_localhost_url_accepted(self) -> None:
        rmain._validate_ollama_url("http://localhost:11434")
        rmain._validate_ollama_url("http://127.0.0.1:11434")
        rmain._validate_ollama_url("http://host.docker.internal:11434")

    @pytest.mark.parametrize(
        "url",
        [
            "http://attacker.example.com:11434",
            "https://api.openai.com/v1",
            "http://example.org",
            "ftp://localhost",
        ],
    )
    def test_off_box_url_rejected(self, url: str) -> None:
        with pytest.raises(rmain.ReflectorConfigError):
            rmain._validate_ollama_url(url)

    @pytest.mark.parametrize(
        "channel",
        ["reflector_trigger", "monitor_trigger", "ABC_123"],
    )
    def test_valid_channel_accepted(self, channel: str) -> None:
        assert rmain._validate_notify_channel(channel) == channel

    @pytest.mark.parametrize(
        "channel",
        [
            "1leadingdigit",
            "with space",
            "drop;table",
            "x" * 100,
            "",
        ],
    )
    def test_invalid_channel_rejected(self, channel: str) -> None:
        with pytest.raises(rmain.ReflectorConfigError):
            rmain._validate_notify_channel(channel)


@pytest.mark.asyncio
class TestRunBootValidation:
    """Boot-time refusal to subscribe a daily channel onto a rollup
    channel — closes the env-override foot-gun the #40 review found."""

    @pytest.mark.parametrize(
        "colliding",
        [rmain.WEEKLY_CHANNEL, rmain.MONTHLY_CHANNEL],
    )
    async def test_daily_channel_collision_with_rollup_is_refused(
        self, colliding: str
    ) -> None:
        cfg = rmain.AgentRuntimeConfig(
            archetype="reflector",
            postgres_url="postgresql+asyncpg://test/test",
            log_level="DEBUG",
            notify_channel=colliding,
        )
        with pytest.raises(rmain.ReflectorConfigError, match="collides"):
            await rmain.run(cfg)


class TestWindowForPayload:
    def test_well_formed_utc_payload(self) -> None:
        ws, we = rmain._window_for_payload(
            "2026-05-01",
            tz=ZoneInfo("UTC"),
            now=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        )
        assert ws == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)

    def test_west_coast_payload_aligns_to_local_day(self) -> None:
        # 2026-05-01 in America/Los_Angeles is 07:00Z 2026-05-01 →
        # 07:00Z 2026-05-02 (PDT, UTC-7).
        tz = ZoneInfo("America/Los_Angeles")
        ws, we = rmain._window_for_payload(
            "2026-05-01",
            tz=tz,
            now=datetime(2026, 5, 2, 23, 0, tzinfo=timezone.utc),
        )
        assert ws == datetime(2026, 5, 1, 7, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 2, 7, 0, tzinfo=timezone.utc)

    def test_window_end_clipped_to_now(self) -> None:
        # If the trigger fires at the END of the local day, `local_end`
        # is in the future relative to `now`. The function clips
        # window_end to `now` so we don't query traces that don't yet
        # exist.
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 1, 23, 30, tzinfo=timezone.utc)
        ws, we = rmain._window_for_payload("2026-05-01", tz=tz, now=now)
        assert ws == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        assert we == now

    def test_malformed_payload_falls_back_to_yesterday_local(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        ws, we = rmain._window_for_payload("not-a-date", tz=tz, now=now)
        # Fallback: yesterday in `tz`.
        assert ws == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)

    def test_empty_payload_falls_back_to_yesterday_local(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc)
        ws, we = rmain._window_for_payload("", tz=tz, now=now)
        assert ws == datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        assert we == datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)
