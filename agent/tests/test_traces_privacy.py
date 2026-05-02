"""Privacy regression tests for the trace substrate (Epic 01 #25).

The trace store carries every turn's input, output, assembled context,
and tool-call args verbatim. The privacy invariant is that this row
**never leaves the host machine** by any path:

- No MCP tool that exposes traces may be classified below RAW_PERSONAL.
- The `agent.traces` package may not import any HTTP client, MCP client,
  or other egress surface. The only writer of trace rows is local
  Postgres; the only reader of trace rows is the in-process FastAPI
  route (#24) bound to localhost.

These tests are bolted onto the existing 59-test MCP regression suite
(`test_mcp_privacy.py`) for `pytest -k privacy` discoverability.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import pytest

import agent.traces
from agent.mcp_audit import (
    RAW_PERSONAL_TOOLS,
    DataClassification,
    MCPPrivacyViolation,
    TRUST_ALLOWS,
    check_trust_boundary,
    classify_tool_data,
)

# The set of tool names that MUST be classified RAW_PERSONAL because they
# would surface trace contents. Forward-defends against future MCP
# exposure of the in-process /traces route.
TRACE_TOOL_NAMES = (
    "query_traces",
    "get_trace",
    "search_traces",
    "find_similar_traces",
    "get_trace_by_id",
    "list_traces",
)

# Trust levels that MUST reject RAW_PERSONAL tools.
NON_LOCAL_TRUST_LEVELS = ("trusted", "external")


class TestTraceToolClassification:
    """Every trace-surfacing MCP tool name is classified RAW_PERSONAL."""

    @pytest.mark.parametrize("tool_name", TRACE_TOOL_NAMES)
    def test_classified_raw_personal(self, tool_name: str) -> None:
        assert classify_tool_data(tool_name) == DataClassification.RAW_PERSONAL

    @pytest.mark.parametrize("tool_name", TRACE_TOOL_NAMES)
    def test_in_raw_personal_tools_set(self, tool_name: str) -> None:
        # Belt-and-braces: ensure the constant export and the function agree.
        assert tool_name in RAW_PERSONAL_TOOLS


class TestTraceToolTrustBoundary:
    """RAW_PERSONAL trace tools are rejected on every non-local server."""

    @pytest.mark.parametrize("tool_name", TRACE_TOOL_NAMES)
    @pytest.mark.parametrize("trust_level", NON_LOCAL_TRUST_LEVELS)
    def test_rejected_on_non_local_server(
        self,
        tool_name: str,
        trust_level: str,
    ) -> None:
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary(
                server_name=f"fake-{trust_level}-server",
                trust_level=trust_level,
                tool_name=tool_name,
            )

    @pytest.mark.parametrize("tool_name", TRACE_TOOL_NAMES)
    def test_allowed_on_local_server(self, tool_name: str) -> None:
        # Local servers may receive RAW_PERSONAL — sanity check the
        # complement so the rejection above isn't trivially correct.
        check_trust_boundary(
            server_name="fake-local-server",
            trust_level="local",
            tool_name=tool_name,
        )

    def test_trust_table_excludes_raw_personal_for_non_local(self) -> None:
        # Defends against a careless edit to TRUST_ALLOWS that would
        # silently re-allow RAW_PERSONAL on trusted/external servers.
        for level in NON_LOCAL_TRUST_LEVELS:
            assert DataClassification.RAW_PERSONAL not in TRUST_ALLOWS[level]


class TestTracePackageEgressSurface:
    """The `agent.traces` package contains no egress code path.

    The only sanctioned writers/readers of trace rows are local Postgres
    and the in-process FastAPI route. Importing an HTTP client, MCP
    client, or other off-host transport from `agent.traces.*` would be
    a privacy regression — even if no caller currently uses it.
    """

    # Modules that, if imported anywhere under agent.traces, indicate a
    # potential egress path. SQLAlchemy is allowed (Postgres is local).
    FORBIDDEN_MODULES = frozenset({
        "httpx",
        "requests",
        "aiohttp",
        "urllib.request",
        "urllib3",
        "socket",  # raw network sockets
        "agent.mcp_client",
        "agent.email_tools",
        "agent.slack_tools",
        "agent.send_tools",
        "agent.telegram_bot",
        "agent.whatsapp_tools",
    })

    def _all_traces_modules(self):
        for info in pkgutil.iter_modules(agent.traces.__path__, prefix="agent.traces."):
            yield importlib.import_module(info.name)

    @pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_MODULES))
    def test_no_traces_module_imports_egress(self, forbidden: str) -> None:
        """Inspect each loaded `agent.traces.*` module's source for
        forbidden-module imports.

        We use source inspection rather than `sys.modules` snapshots
        because Python caches imports across tests; static checks catch
        the regression even if the module was loaded eagerly elsewhere.
        """
        offenders: list[str] = []
        for mod in self._all_traces_modules():
            try:
                src = inspect.getsource(mod)
            except (OSError, TypeError):
                continue
            # Look for `import X` / `from X` / `from X.` lines.
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith(
                    (f"import {forbidden}", f"from {forbidden} ", f"from {forbidden}.")
                ):
                    offenders.append(f"{mod.__name__}: {stripped}")
        assert not offenders, (
            f"agent.traces.* must not import {forbidden}; offenders: {offenders}"
        )


class TestTraceReprNeverLeaksRawPersonal:
    """Defense in depth: even if a Trace ends up in a stack trace or
    structured log line, its `__repr__` redacts RAW_PERSONAL columns.
    """

    def test_repr_redacts_input_output(self) -> None:
        from agent.traces import Trace

        secret = "ssn-123-45-6789-and-credit-card-4111111111111111"
        t = Trace(input=secret, output=secret)
        r = repr(t)
        assert secret not in r
        assert "<redacted" in r
