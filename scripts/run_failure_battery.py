"""Run the synthetic failure-seed battery against the live Pepper instance.

Phase 0 Task 3 of docs/SEMANTIC_ROUTER_MIGRATION.md. Drives every query
in tests/failure_seed_battery.jsonl through Pepper's HTTP /chat endpoint
(captures response text + latency), then joins each turn against
logs/chat_turns/<date>.jsonl (captures tool_calls + model recorded by
chat_turn_logger). Combined records are written to
logs/router_audit/battery_run_<timestamp>.jsonl for Task 4 (LLM-assisted
classification).

Privacy: stays local — talks only to http://localhost:8000 and reads
local JSONL. No external network.

Usage::

    .venv/bin/python scripts/run_failure_battery.py \\
        [--battery tests/failure_seed_battery.jsonl] \\
        [--api-url http://localhost:8000] \\
        [--limit N] \\
        [--out logs/router_audit/battery_run_<ts>.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BATTERY = REPO_ROOT / "tests" / "failure_seed_battery.jsonl"
DEFAULT_AUDIT_DIR = REPO_ROOT / "logs" / "router_audit"
CHAT_TURN_DIR = REPO_ROOT / "logs" / "chat_turns"


def load_battery(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def find_chat_turn(session_id: str) -> dict[str, Any] | None:
    """Locate the chat_turn_logger row for this session in today's file."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = CHAT_TURN_DIR / f"{today}.jsonl"
    if not path.exists():
        return None
    match: dict[str, Any] | None = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("session_id") == session_id:
                match = row
    return match


def run_battery(
    battery_path: Path,
    api_url: str,
    api_key: str,
    out_path: Path,
    limit: int | None,
    timeout_s: float,
) -> dict[str, Any]:
    rows = load_battery(battery_path)
    if limit is not None:
        rows = rows[:limit]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    sent = 0
    failed = 0
    started_run = time.perf_counter()
    headers = {"Content-Type": "application/json", "x-api-key": api_key}

    with (
        httpx.Client(timeout=timeout_s) as client,
        open(out_path, "a", encoding="utf-8") as out_fh,
    ):
        for entry in rows:
            session_id = f"battery-{run_id}-{entry['id']}-{uuid.uuid4().hex[:6]}"
            payload = {"message": entry["query"], "session_id": session_id}
            t0 = time.perf_counter()
            response_text: str | None = None
            error: str | None = None
            try:
                resp = client.post(
                    f"{api_url}/chat",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    response_text = resp.json().get("response")
                else:
                    error = f"http_{resp.status_code}: {resp.text[:200]}"
            except Exception as exc:  # noqa: BLE001 — best-effort runner
                error = f"exception: {exc}"
            latency_ms = round((time.perf_counter() - t0) * 1000)

            turn = find_chat_turn(session_id) if response_text is not None else None
            tool_calls = (turn or {}).get("tool_calls", [])
            model = (turn or {}).get("model")
            logger_latency = (turn or {}).get("latency_ms")

            record = {
                "battery_id": entry["id"],
                "category": entry.get("category"),
                "query": entry["query"],
                "difficulty": entry.get("difficulty"),
                "expected_intent": entry.get("expected_intent"),
                "expected_tools": entry.get("expected_tools", []),
                "notes": entry.get("notes"),
                "session_id": session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "response": response_text,
                "tool_calls": tool_calls,
                "model": model,
                "latency_ms": latency_ms,
                "logger_latency_ms": logger_latency,
                "error": error,
            }
            out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            out_fh.flush()
            sent += 1
            if error is not None:
                failed += 1
            cat = entry.get("category", "?")
            print(
                f"[{sent}/{len(rows)}] {entry['id']} {cat:<14} "
                f"{latency_ms}ms{' ERR' if error else ''}",
                flush=True,
            )

    return {
        "run_id": run_id,
        "battery_path": str(battery_path),
        "out_path": str(out_path),
        "sent": sent,
        "failed": failed,
        "duration_s": round(time.perf_counter() - started_run, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--battery", type=Path, default=DEFAULT_BATTERY)
    parser.add_argument("--api-url", default=os.environ.get("PEPPER_API_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("PEPPER_API_KEY"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if not args.api_key:
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("API_KEY="):
                    args.api_key = line.split("=", 1)[1].strip()
                    break
    if not args.api_key:
        print("ERROR: no API key (set PEPPER_API_KEY or .env API_KEY)", file=sys.stderr)
        return 2

    out_path = args.out or (
        DEFAULT_AUDIT_DIR
        / f"battery_run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )

    summary = run_battery(
        battery_path=args.battery,
        api_url=args.api_url,
        api_key=args.api_key,
        out_path=out_path,
        limit=args.limit,
        timeout_s=args.timeout,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
