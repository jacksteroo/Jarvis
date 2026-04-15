from __future__ import annotations

import httpx
import structlog
from dataclasses import dataclass, field

logger = structlog.get_logger()

SUBSYSTEM_PORTS = {
    "calendar": 8100,
    "communications": 8101,
    "knowledge": 8102,
    "health": 8103,
    "finance": 8104,
    "people": 8001,
}


@dataclass
class SubsystemInfo:
    name: str
    base_url: str
    status: str = "unknown"  # "ok", "degraded", "down"
    tools: list = field(default_factory=list)


class ToolRouter:
    def __init__(self):
        self._subsystems: dict[str, SubsystemInfo] = {}
        # Register defaults
        for name, port in SUBSYSTEM_PORTS.items():
            self.register_subsystem(name, f"http://localhost:{port}")

    def register_subsystem(self, name: str, base_url: str) -> None:
        self._subsystems[name] = SubsystemInfo(name=name, base_url=base_url)

    async def check_health(self) -> dict[str, str]:
        """Ping all subsystems. Returns {name: status}."""
        results = {}
        async with httpx.AsyncClient(timeout=3.0) as client:
            for name, info in self._subsystems.items():
                try:
                    resp = await client.get(f"{info.base_url}/health")
                    info.status = "ok" if resp.status_code == 200 else "degraded"
                except Exception:
                    info.status = "down"
                results[name] = info.status
        return results

    async def list_available_tools(self) -> list[dict]:
        """Fetch tool definitions from all healthy subsystems."""
        tools = []
        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, info in self._subsystems.items():
                if info.status == "down":
                    continue
                try:
                    resp = await client.get(f"{info.base_url}/tools")
                    if resp.status_code == 200:
                        subsystem_tools = resp.json()
                        # Tag each tool with subsystem name for routing
                        for tool in subsystem_tools:
                            tool["_subsystem"] = name
                        tools.extend(subsystem_tools)
                        info.tools = subsystem_tools
                except Exception as e:
                    logger.warning("tool_fetch_failed", subsystem=name, error=str(e))
        return tools

    async def call_tool(self, subsystem: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a subsystem. Returns result dict."""
        info = self._subsystems.get(subsystem)
        if not info or info.status == "down":
            return {"error": f"Subsystem '{subsystem}' is unavailable", "subsystem": subsystem}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{info.base_url}/tools/{tool_name}",
                    json={"arguments": arguments},
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "tool_call_failed",
                subsystem=subsystem,
                tool=tool_name,
                status=e.response.status_code,
            )
            return {
                "error": f"Tool call failed: {e.response.status_code}",
                "subsystem": subsystem,
            }
        except Exception as e:
            logger.error(
                "tool_call_error", subsystem=subsystem, tool=tool_name, error=str(e)
            )
            return {"error": str(e), "subsystem": subsystem}

    def get_status(self) -> dict:
        return {name: info.status for name, info in self._subsystems.items()}
