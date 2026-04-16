"""
Phase 5 — MCP Client tests.

Tests config loading, tool discovery, and the MCPClient interface.
"""
import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from agent.mcp_client import (
    MCPClient,
    MCPServerConfig,
    MCPToolInfo,
    load_mcp_config,
)


# ── Config loading ───────────────────────────────────────────────────────────


def test_load_config_empty_file():
    """Empty config yields no servers."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"servers": []}, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)
    assert configs == []


def test_load_config_missing_file():
    """Missing config file yields no servers."""
    configs = load_mcp_config("/nonexistent/path.yaml")
    assert configs == []


def test_load_config_valid_server():
    """Valid server entry is parsed correctly."""
    data = {
        "servers": [
            {
                "name": "test-server",
                "command": "npx",
                "args": ["-y", "test-pkg"],
                "trust_level": "external",
                "env": {"API_KEY": "test123"},
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "test-server"
    assert configs[0].command == "npx"
    assert configs[0].args == ["-y", "test-pkg"]
    assert configs[0].trust_level == "external"
    assert configs[0].env == {"API_KEY": "test123"}


def test_load_config_env_interpolation():
    """Environment variables in env values are interpolated."""
    os.environ["TEST_MCP_TOKEN"] = "secret_value"
    data = {
        "servers": [
            {
                "name": "test",
                "command": "test",
                "env": {"TOKEN": "${TEST_MCP_TOKEN}"},
                "trust_level": "local",
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)
    del os.environ["TEST_MCP_TOKEN"]

    assert configs[0].env["TOKEN"] == "secret_value"


def test_load_config_invalid_trust_level():
    """Invalid trust_level raises ValueError."""
    with pytest.raises(ValueError, match="Invalid trust_level"):
        MCPServerConfig(name="bad", command="test", trust_level="invalid")


def test_load_config_default_trust_level():
    """Default trust_level is 'external' (conservative)."""
    data = {
        "servers": [
            {"name": "no-trust", "command": "test"}
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert configs[0].trust_level == "external"


def test_load_config_multiple_servers():
    """Multiple servers are all loaded."""
    data = {
        "servers": [
            {"name": "s1", "command": "cmd1", "trust_level": "local"},
            {"name": "s2", "command": "cmd2", "trust_level": "trusted"},
            {"name": "s3", "command": "cmd3", "trust_level": "external"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 3
    assert {c.trust_level for c in configs} == {"local", "trusted", "external"}


def test_load_config_skips_entry_missing_name():
    """Config entry without 'name' is skipped, not crashed."""
    data = {
        "servers": [
            {"command": "test", "trust_level": "local"},         # missing name
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_skips_entry_missing_command():
    """Config entry without 'command' is skipped, not crashed."""
    data = {
        "servers": [
            {"name": "no-cmd", "trust_level": "local"},          # missing command
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_skips_invalid_trust_level():
    """Config entry with invalid trust_level is skipped gracefully."""
    data = {
        "servers": [
            {"name": "bad-trust", "command": "cmd", "trust_level": "superadmin"},
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_coerces_args_to_strings():
    """Integer args in YAML are coerced to strings."""
    data = {
        "servers": [
            {"name": "test", "command": "node", "args": ["-p", 8080], "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert configs[0].args == ["-p", "8080"]
    assert all(isinstance(a, str) for a in configs[0].args)


# ── MCPClient interface ─────────────────────────────────────────────────────


def test_client_no_config():
    """Client with no config file has empty tools."""
    client = MCPClient(config_path="/nonexistent.yaml")
    assert client.get_tools() == []


def test_tool_info_lookup():
    """get_tool_info returns correct info for registered tools."""
    client = MCPClient()
    # Manually register a tool
    info = MCPToolInfo(
        name="test_tool",
        description="A test tool",
        input_schema={"type": "object"},
        server_name="test-server",
        trust_level="local",
    )
    client._tool_index["mcp_test-server_test_tool"] = info

    assert client.get_tool_info("mcp_test-server_test_tool") == info
    assert client.get_tool_info("nonexistent") is None


def test_get_tools_format():
    """get_tools returns Anthropic function-calling format with MCP metadata."""
    client = MCPClient()
    info = MCPToolInfo(
        name="my_tool",
        description="Does something",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        server_name="github",
        trust_level="external",
    )
    client._tool_index["mcp_github_my_tool"] = info

    tools = client.get_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "mcp_github_my_tool"
    assert "[MCP/github]" in tool["function"]["description"]
    assert tool["_mcp"] is True
    assert tool["_mcp_server"] == "github"
    assert tool["_mcp_tool"] == "my_tool"
    assert tool["_trust_level"] == "external"


@pytest.mark.asyncio
async def test_call_tool_server_not_found():
    """Calling a tool on a non-existent server returns an error."""
    client = MCPClient()
    result = await client.call_tool("nonexistent", "some_tool", {})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_server_not_connected():
    """Calling a tool on a server that is not connected returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["offline"] = MCPServerConnection(
        config=MCPServerConfig(name="offline", command="test"),
        status="disconnected",
    )
    result = await client.call_tool("offline", "some_tool", {})
    assert "error" in result
    assert "disconnected" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_server_error_status():
    """Calling a tool on a server in error state returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["broken"] = MCPServerConnection(
        config=MCPServerConfig(name="broken", command="test"),
        status="error",
    )
    result = await client.call_tool("broken", "some_tool", {})
    assert "error" in result
    assert "error" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_no_session():
    """Calling a tool on a server with connected status but no session returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["nosession"] = MCPServerConnection(
        config=MCPServerConfig(name="nosession", command="test"),
        status="connected",
        session=None,
    )
    result = await client.call_tool("nosession", "some_tool", {})
    assert "error" in result
    assert "no active session" in result["error"]


@pytest.mark.asyncio
async def test_client_health_returns_server_statuses():
    """check_health returns status for all registered servers."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["s1"] = MCPServerConnection(
        config=MCPServerConfig(name="s1", command="test"),
        status="connected",
    )
    client._servers["s2"] = MCPServerConnection(
        config=MCPServerConfig(name="s2", command="test"),
        status="error",
    )
    health = await client.check_health()
    assert health == {"s1": "connected", "s2": "error"}


@pytest.mark.asyncio
async def test_client_health_empty():
    """Health check with no servers returns empty dict."""
    client = MCPClient()
    health = await client.check_health()
    assert health == {}


def test_get_tools_empty_schema_gets_default():
    """get_tools uses a default empty schema when tool has no input_schema."""
    client = MCPClient()
    info = MCPToolInfo(
        name="bare_tool",
        description="No schema",
        input_schema={},
        server_name="local",
        trust_level="local",
    )
    client._tool_index["mcp_local_bare_tool"] = info
    tools = client.get_tools()
    assert len(tools) == 1
    params = tools[0]["function"]["parameters"]
    # Should fall back to {"type": "object", "properties": {}}
    assert params == {"type": "object", "properties": {}}


# ── Rate limiting ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_not_applied_to_local_servers():
    """Local servers bypass rate limiting entirely."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    # Saturate with calls (well beyond the rate limit)
    for _ in range(100):
        client._call_times.setdefault("localsvr", []).append(0.0)  # old timestamps
    client._servers["localsvr"] = MCPServerConnection(
        config=MCPServerConfig(name="localsvr", command="test", trust_level="local"),
        status="connected",
        session=object(),  # non-None placeholder
    )
    # Rate limit should not trigger for local
    result = client._check_rate_limit("localsvr", "local")
    assert result is None


def test_rate_limit_blocks_external_at_threshold():
    """External servers are blocked when call count reaches the rate limit."""
    client = MCPClient()
    now = time.monotonic()
    # Fill up to exactly the rate limit within the window
    client._call_times["ext"] = [now - 1.0] * client._EXTERNAL_RATE_LIMIT
    result = client._check_rate_limit("ext", "external")
    assert result is not None
    assert "Rate limit exceeded" in result


def test_rate_limit_allows_external_below_threshold():
    """External servers are not blocked below the rate limit."""
    client = MCPClient()
    now = time.monotonic()
    # One below the limit
    client._call_times["ext"] = [now - 1.0] * (client._EXTERNAL_RATE_LIMIT - 1)
    result = client._check_rate_limit("ext", "external")
    assert result is None


def test_rate_limit_expires_old_calls():
    """Calls older than the rate window do not count against the limit."""
    client = MCPClient()
    # All calls are expired (2x the window ago)
    client._call_times["ext"] = [0.0] * 100  # ancient timestamps
    result = client._check_rate_limit("ext", "external")
    assert result is None
