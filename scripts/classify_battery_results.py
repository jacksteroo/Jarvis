"""Phase 0 Task 4 — LLM-assisted classification of battery results.

Reads the latest `logs/router_audit/battery_run_<ts>.jsonl` produced by
`scripts/run_failure_battery.py`, sends each (query, response, expected,
actual_tools) tuple to a judge model alongside `docs/LIFE_CONTEXT.md`, and
writes per-record verdicts to
`logs/router_audit/battery_classification_<ts>.jsonl`.

Judge model selection:
- Default: Anthropic Opus 4.7 (per migration plan Decisions Log).
- Fallback: local hermes via Ollama if `ANTHROPIC_API_KEY` is unset. Privacy-
  safe — the spec accepts a local judge in offline mode.

Privacy: only this single audit step sends battery query+response pairs to a
non-local judge — explicitly authorized in
`docs/SEMANTIC_ROUTER_MIGRATION.md` Decisions Log. Raw mailbox/iMessage data
is never read here; inputs come solely from the battery JSONL.

Usage::

    .venv/bin/python scripts/classify_battery_results.py \\
        [--battery-run logs/router_audit/battery_run_<ts>.jsonl] \\
        [--judge opus|local] \\
        [--limit N] \\
        [--out logs/router_audit/battery_classification_<ts>.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent.battery_classifier import (  # noqa: E402
    TAXONOMY,
    build_judge_prompt,
    parse_judgment,
)

DEFAULT_AUDIT_DIR = REPO_ROOT / "logs" / "router_audit"
DEFAULT_LIFE_CONTEXT = REPO_ROOT / "docs" / "LIFE_CONTEXT.md"

OPUS_MODEL = "claude-opus-4-7"
LOCAL_JUDGE_MODEL = "hermes3:latest"


def latest_battery_run() -> Path:
    candidates = sorted(DEFAULT_AUDIT_DIR.glob("battery_run_*.jsonl"))
    if not candidates:
        raise SystemExit("no battery_run_*.jsonl found in logs/router_audit/")
    return candidates[-1]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Judge backends
# ---------------------------------------------------------------------------


def _load_env_api_key() -> str | None:
    """Load ANTHROPIC_API_KEY from process env or .env (uncommented only)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
            return v.strip()
    return None


def judge_opus(prompt: str, api_key: str) -> tuple[str, str]:
    """Returns (raw_text, model_id_used)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text, OPUS_MODEL


def judge_local(prompt: str, ollama_url: str, model: str) -> tuple[str, str]:
    resp = httpx.post(
        f"{ollama_url}/api/chat",
        json={
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.0, "num_predict": 600},
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (data.get("message") or {}).get("content", "")
    return text, f"local/{model}"


def classify_record(
    record: dict[str, Any],
    life_context: str,
    judge: str,
    api_key: str | None,
    ollama_url: str,
    local_model: str,
) -> dict[str, Any]:
    prompt = build_judge_prompt(record, life_context)
    t0 = time.perf_counter()
    judge_error: str | None = None
    raw = ""
    judge_model = ""
    try:
        if judge == "opus":
            assert api_key
            raw, judge_model = judge_opus(prompt, api_key)
        else:
            raw, judge_model = judge_local(prompt, ollama_url, local_model)
    except Exception as exc:  # noqa: BLE001 — best-effort judge runner
        judge_error = f"{type(exc).__name__}: {exc}"
    latency_ms = round((time.perf_counter() - t0) * 1000)

    if judge_error:
        verdict = {
            "success": False,
            "taxonomy": "OTHER",
            "reasoning": "",
            "parse_error": f"judge_call_failed: {judge_error}",
        }
    else:
        verdict = parse_judgment(raw)

    return {
        "battery_id": record.get("battery_id"),
        "category": record.get("category"),
        "query": record.get("query"),
        "expected_intent": record.get("expected_intent"),
        "expected_tools": record.get("expected_tools", []),
        "model_under_test": record.get("model"),
        "judge_model": judge_model,
        "judge_latency_ms": latency_ms,
        "judge_raw": raw,
        "verdict": verdict,
    }


def summarize(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(verdicts)
    successes = sum(1 for v in verdicts if v["verdict"]["success"])
    by_taxonomy: dict[str, int] = {t: 0 for t in TAXONOMY}
    parse_errors = 0
    for v in verdicts:
        verdict = v["verdict"]
        if verdict.get("parse_error"):
            parse_errors += 1
        tax = verdict.get("taxonomy")
        if tax in by_taxonomy:
            by_taxonomy[tax] += 1
    routing_fixable = by_taxonomy["ROUTING_MISS"] + by_taxonomy["INTERCEPT_MISS"]
    failures = total - successes
    routing_share = (routing_fixable / failures) if failures else 0.0
    return {
        "total": total,
        "successes": successes,
        "failures": failures,
        "by_taxonomy": by_taxonomy,
        "routing_fixable": routing_fixable,
        "routing_fixable_share_of_failures": round(routing_share, 4),
        "parse_errors": parse_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--battery-run", type=Path, default=None)
    parser.add_argument("--life-context", type=Path, default=DEFAULT_LIFE_CONTEXT)
    parser.add_argument("--judge", choices=("opus", "local", "auto"), default="auto")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--local-model", default=LOCAL_JUDGE_MODEL)
    args = parser.parse_args()

    battery_run = args.battery_run or latest_battery_run()
    print(f"battery_run: {battery_run}", flush=True)
    rows = load_jsonl(battery_run)
    if args.limit is not None:
        rows = rows[: args.limit]
    life_context = args.life_context.read_text(encoding="utf-8")

    api_key = _load_env_api_key()
    if args.judge == "auto":
        judge = "opus" if api_key else "local"
    else:
        judge = args.judge
    if judge == "opus" and not api_key:
        print("ERROR: --judge opus but no ANTHROPIC_API_KEY available", file=sys.stderr)
        return 2

    out_path = args.out or (
        DEFAULT_AUDIT_DIR
        / f"battery_classification_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"judge: {judge}; out: {out_path}; rows: {len(rows)}", flush=True)

    verdicts: list[dict[str, Any]] = []
    started = time.perf_counter()
    with open(out_path, "a", encoding="utf-8") as out_fh:
        for i, record in enumerate(rows, 1):
            v = classify_record(
                record,
                life_context=life_context,
                judge=judge,
                api_key=api_key,
                ollama_url=args.ollama_url,
                local_model=args.local_model,
            )
            verdicts.append(v)
            out_fh.write(json.dumps(v, ensure_ascii=False) + "\n")
            out_fh.flush()
            verdict = v["verdict"]
            mark = "PASS" if verdict["success"] else (verdict.get("taxonomy") or "?")
            err = " (PARSE_ERR)" if verdict.get("parse_error") else ""
            print(
                f"[{i}/{len(rows)}] {v['battery_id']:<18} {v['category']:<14} "
                f"{v['judge_latency_ms']:>6}ms  {mark}{err}",
                flush=True,
            )

    summary = summarize(verdicts)
    summary["duration_s"] = round(time.perf_counter() - started, 1)
    summary["judge"] = judge
    summary["out_path"] = str(out_path)
    summary["battery_run"] = str(battery_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
