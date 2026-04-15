#!/usr/bin/env python3
"""
Pepper Eval — given a recent /chat round-trip in pepper-agent's docker logs,
classify it as grounded or hallucinated.

Detection layers (strongest first):
  1. Tool-call evidence — count `tool_call` log lines between `api_chat_in`
     and `api_chat_out` for a session_id. Zero tool calls on a question that
     requires data = strong hallucination signal.
  2. Placeholder regex — `[Commitment XYZ]`, `[Name]`, `[Date]`, etc.
     Almost zero false positives.
  3. Concreteness check — does the response contain at least one specific
     entity (digit run, date-ish token, capitalized multi-word name)? If
     not, and the question requires data, suspicious.

Usage:
  python scripts/pepper_eval.py --since 10m
  python scripts/pepper_eval.py --since 10m --session 438817475
  python scripts/pepper_eval.py --since 10m --json

Exits 0 if no hallucinations found, 1 if any were found (so callers can
chain it: `pepper_eval.py && echo clean || echo gaps`).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
KV_RE = re.compile(r"(\w+)=(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"|(\S+))")
PLACEHOLDER_RE = re.compile(r"\[(?:[A-Z][A-Za-z0-9]*\s?){1,4}[A-Z0-9]{2,}\]")  # [Commitment XYZ], [Name ABC]
SOFT_PLACEHOLDER_RE = re.compile(r"\[(?:Name|Date|Time|Project|Commitment|Person|Place|Address|Email|Subject|Topic)[^\]]*\]", re.I)

# Phrases that imply data was used, when no tool was actually called.
GROUNDING_PRETENSE = [
    "according to your calendar",
    "in your recent emails",
    "based on your schedule",
    "looking at your inbox",
    "based on my analysis",
    "from what i can see in your",
]

# Trigger words in a question that *require* a tool call to answer honestly.
DATA_TRIGGERS = [
    "calendar", "schedule", "meeting", "email", "inbox", "gmail", "yahoo",
    "imessage", "message", "slack", "whatsapp", "renewal", "subscription",
    "commitment", "follow up", "follow-up", "drive to", "driving time",
    "directions", "address", "contact", "memory", "remember", "recall",
    "what do you know about", "today", "tomorrow", "this week", "next week",
    "yesterday", "last week",
]


@dataclass
class Turn:
    session_id: str
    ts_in: str
    ts_out: str | None
    question: str
    response: str
    n_tools: int
    tool_names: list[str] = field(default_factory=list)

    @property
    def needs_data(self) -> bool:
        q = self.question.lower()
        return any(t in q for t in DATA_TRIGGERS)

    def evaluate(self) -> dict:
        verdicts = []
        confidence = 0  # 0=clean, +N = hallucination evidence

        # Graceful refusal short-circuit: if the response is honestly admitting
        # it doesn't have the data, that's NOT a hallucination — it's the
        # behavior we WANT.
        rl_low = self.response.lower()
        graceful_refusal_markers = [
            "i started to answer that with template placeholders",
            "i don't see anything specific",
            "i don't have specific",
            "i'm holding back rather than make something up",
            "no upcoming events",
            "no events found",
            "i don't have access",
            "i'm not sure",
        ]
        if any(m in rl_low for m in graceful_refusal_markers):
            return {
                "session_id": self.session_id,
                "ts_in": self.ts_in,
                "question": self.question,
                "response_preview": self.response[:200],
                "n_tools": self.n_tools,
                "tool_names": self.tool_names,
                "needs_data": self.needs_data,
                "verdicts": ["graceful_refusal"],
                "hallucinated": False,
                "confidence": 0,
            }

        if PLACEHOLDER_RE.search(self.response) or SOFT_PLACEHOLDER_RE.search(self.response):
            verdicts.append("placeholder_text")
            confidence += 3

        if self.needs_data and self.n_tools == 0:
            verdicts.append("data_question_no_tool_call")
            confidence += 3

        rl = self.response.lower()
        for phrase in GROUNDING_PRETENSE:
            if phrase in rl and self.n_tools == 0:
                verdicts.append(f"pretends_grounded:{phrase!r}")
                confidence += 2
                break

        # Concreteness: at least one digit run of length >= 2 or month name
        has_concrete = bool(re.search(r"\d{2,}", self.response)) or bool(
            re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*", self.response)
        )
        if self.needs_data and not has_concrete:
            verdicts.append("no_concrete_entities")
            confidence += 1

        return {
            "session_id": self.session_id,
            "ts_in": self.ts_in,
            "question": self.question,
            "response_preview": self.response[:200],
            "n_tools": self.n_tools,
            "tool_names": self.tool_names,
            "needs_data": self.needs_data,
            "verdicts": verdicts,
            "hallucinated": confidence >= 3,
            "confidence": confidence,
        }


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def _parse_kv(line: str) -> dict[str, str]:
    out = {}
    for m in KV_RE.finditer(line):
        key = m.group(1)
        val = m.group(2) or m.group(3) or m.group(4) or ""
        out[key] = val
    return out


def _read_docker_logs(since: str) -> list[str]:
    cmd = ["docker", "logs", "--since", since, "pepper-agent"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    raw = (proc.stdout or "") + (proc.stderr or "")
    return [_strip_ansi(line) for line in raw.splitlines()]


def parse_turns(lines: list[str], session_filter: str | None = None) -> list[Turn]:
    """Walk lines top-to-bottom, pairing api_chat_in→api_chat_out and
    counting tool_call lines in between."""
    turns: list[Turn] = []
    pending: dict[str, dict] = {}  # session_id -> {ts_in, question, n_tools, tool_names}

    for line in lines:
        # Find the structlog event name (first bare word after timestamp/level)
        if "api_chat_in" in line:
            kv = _parse_kv(line)
            sid = kv.get("session_id", "")
            if session_filter and sid != session_filter:
                continue
            ts = line[:19]
            pending[sid] = {
                "ts_in": ts,
                "question": kv.get("text", ""),
                "n_tools": 0,
                "tool_names": [],
            }
        elif "tool_call" in line and "n_tools" not in line:
            kv = _parse_kv(line)
            name = kv.get("name", "?")
            for sid, p in pending.items():
                p["n_tools"] += 1
                p["tool_names"].append(name)
        elif "api_chat_out" in line:
            kv = _parse_kv(line)
            sid = kv.get("session_id", "")
            if sid in pending:
                p = pending.pop(sid)
                turns.append(Turn(
                    session_id=sid,
                    ts_in=p["ts_in"],
                    ts_out=line[:19],
                    question=p["question"],
                    response=kv.get("text", ""),
                    n_tools=p["n_tools"],
                    tool_names=p["tool_names"],
                ))
    return turns


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="10m", help="docker logs --since window (e.g. 10m, 1h)")
    p.add_argument("--session", help="restrict to a specific session_id")
    p.add_argument("--json", action="store_true", help="emit JSON instead of human report")
    args = p.parse_args(list(argv) if argv is not None else None)

    lines = _read_docker_logs(args.since)
    turns = parse_turns(lines, session_filter=args.session)
    results = [t.evaluate() for t in turns]
    hallucinations = [r for r in results if r["hallucinated"]]

    if args.json:
        print(json.dumps({
            "total_turns": len(results),
            "hallucinated": len(hallucinations),
            "results": results,
        }, indent=2))
    else:
        print(f"=== pepper_eval: {len(results)} turn(s), {len(hallucinations)} hallucinated ===\n")
        for r in results:
            mark = "🚨" if r["hallucinated"] else "✓ "
            print(f"{mark} [{r['ts_in']}] tools={r['n_tools']} {r['tool_names']}")
            print(f"   Q: {r['question'][:180]}")
            print(f"   A: {r['response_preview']}")
            if r["verdicts"]:
                print(f"   verdicts: {', '.join(r['verdicts'])}")
            print()

    return 1 if hallucinations else 0


if __name__ == "__main__":
    raise SystemExit(main())
