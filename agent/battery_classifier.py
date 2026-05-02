"""Pure-Python helpers for Phase 0 Task 4 LLM-assisted battery classification.

The runner in `scripts/classify_battery_results.py` calls the LLM; this module
holds the deterministic pieces (prompt assembly, response parsing, taxonomy
schema) so they're unit-testable without the network.

Privacy: nothing in here makes a network call. Battery records may contain
calendar/people details from `docs/LIFE_CONTEXT.md`; the migration plan
(Decisions Log entry "Phase 0 / Classification approach") explicitly
authorizes sending battery query+response pairs to the judge model for this
single audit step.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Per docs/SEMANTIC_ROUTER_MIGRATION.md Phase 0 Task 4.
TAXONOMY: tuple[str, ...] = (
    "ROUTING_MISS",
    "INTERCEPT_MISS",
    "HALLUCINATION",
    "SYNTHESIS_MISS",
    "TOOL_MISS",
    "CONTEXT_MISS",
    "OVER_INVOCATION",
    "STALE_MEMORY",
    "OTHER",
)

_TOOL_NAME_KEYS = ("name", "function", "tool", "tool_name")


def extract_tool_names(tool_calls: Any) -> list[str]:
    """Best-effort extraction of tool-call names from a logger record.

    `tool_calls` shape varies: Ollama sometimes returns
    ``[{"function": {"name": "..."}}]``, hermes text-extraction yields
    ``[{"name": "...", "arguments": {...}}]``, and intercepts log
    ``[]``. Unknown shapes return an empty list rather than raising.
    """
    if not isinstance(tool_calls, list):
        return []
    names: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if isinstance(fn, dict):
            n = fn.get("name")
            if isinstance(n, str) and n:
                names.append(n)
                continue
        for key in _TOOL_NAME_KEYS:
            n = call.get(key)
            if isinstance(n, str) and n:
                names.append(n)
                break
    return names


def build_judge_prompt(record: dict[str, Any], life_context: str) -> str:
    """Assemble the judge prompt for a single battery record.

    The prompt asks for a strict JSON object so the parser stays simple.
    """
    actual_tools = extract_tool_names(record.get("tool_calls"))
    response = (record.get("response") or "").strip()
    if len(response) > 4000:
        response = response[:4000] + "...[truncated]"

    payload = {
        "battery_id": record.get("battery_id"),
        "category": record.get("category"),
        "query": record.get("query"),
        "expected_intent": record.get("expected_intent"),
        "expected_tools": record.get("expected_tools", []),
        "actual_tools": actual_tools,
        "model": record.get("model"),
        "response": response,
        "battery_notes": record.get("notes"),
        "had_error": record.get("error") is not None,
    }
    schema_hint = (
        '{"success": <bool>, "taxonomy": <one of '
        + "|".join(TAXONOMY)
        + ' or null when success=true>, "reasoning": <≤2 sentences>}'
    )
    return (
        "You are auditing Pepper, a local-first AI life assistant, on a single "
        "battery query. Judge whether Pepper's response is correct against the "
        "ground-truth life context. If incorrect, classify the failure mode.\n\n"
        "Failure taxonomy (pick ONE if success=false):\n"
        "- ROUTING_MISS: wrong tool selected (or no tool when one was needed).\n"
        "- INTERCEPT_MISS: deterministic intercept fired but produced wrong output.\n"
        "- HALLUCINATION: model invented facts not in life context or tool outputs.\n"
        "- SYNTHESIS_MISS: right tool, right data, wrong/poor summarization.\n"
        "- TOOL_MISS: right tool, but tool returned bad/empty data.\n"
        "- CONTEXT_MISS: right tool, but bad context window (missing prior facts).\n"
        "- OVER_INVOCATION: too many tools called, blew context.\n"
        "- STALE_MEMORY: outdated facts pulled into context.\n"
        "- OTHER: none of the above fit.\n\n"
        "Output STRICT JSON, no prose, matching this shape:\n"
        f"{schema_hint}\n\n"
        "=== LIFE_CONTEXT (ground truth) ===\n"
        f"{life_context}\n\n"
        "=== BATTERY RECORD ===\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "Now emit the JSON object."
    )


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

# Loose-recovery regexes for judges that emit near-JSON with unquoted enum
# values or unquoted reasoning strings (qwen2.5 does this often).
_LOOSE_SUCCESS = re.compile(r'"success"\s*:\s*(true|false)', re.IGNORECASE)
_LOOSE_TAXONOMY = re.compile(r'"taxonomy"\s*:\s*"?([A-Za-z_]+)"?')
_LOOSE_REASONING = re.compile(
    r'"reasoning"\s*:\s*"?(.+?)"?\s*[},]?\s*$',
    re.DOTALL,
)


def _loose_recover(blob: str) -> dict[str, Any] | None:
    """Last-resort recovery for near-JSON judge outputs."""
    sm = _LOOSE_SUCCESS.search(blob)
    if not sm:
        return None
    success = sm.group(1).lower() == "true"
    taxonomy: str | None = None
    if not success:
        tm = _LOOSE_TAXONOMY.search(blob)
        if not tm or tm.group(1) not in TAXONOMY:
            return None
        taxonomy = tm.group(1)
    rm = _LOOSE_REASONING.search(blob)
    reasoning = rm.group(1).strip().rstrip('"').strip() if rm else ""
    return {
        "success": success,
        "taxonomy": taxonomy,
        "reasoning": reasoning,
        "parse_error": "loose_recovered",
    }


def parse_judgment(text: str) -> dict[str, Any]:
    """Parse a judge response into a normalized verdict dict.

    Returns ``{"success": bool, "taxonomy": str|None, "reasoning": str,
    "parse_error": str|None}``. Unparseable responses become
    ``success=False, taxonomy="OTHER"`` with the parse error captured so
    downstream tabulation can flag them rather than crashing.
    """
    if not text or not text.strip():
        return _bad("empty response")
    match = _JSON_OBJECT.search(text)
    if not match:
        return _bad("no JSON object found")
    blob = match.group(0)
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError as exc:
        recovered = _loose_recover(blob)
        if recovered is not None:
            return recovered
        return _bad(f"json decode: {exc}")
    if not isinstance(obj, dict):
        return _bad("not a JSON object")

    success = obj.get("success")
    if not isinstance(success, bool):
        return _bad("'success' missing or not bool")

    taxonomy = obj.get("taxonomy")
    if success:
        taxonomy = None
    else:
        if taxonomy not in TAXONOMY:
            return _bad(f"taxonomy {taxonomy!r} not in allowed set")

    reasoning = obj.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    return {
        "success": success,
        "taxonomy": taxonomy,
        "reasoning": reasoning.strip(),
        "parse_error": None,
    }


def _bad(reason: str) -> dict[str, Any]:
    return {
        "success": False,
        "taxonomy": "OTHER",
        "reasoning": "",
        "parse_error": reason,
    }
