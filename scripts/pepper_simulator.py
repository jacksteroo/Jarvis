#!/usr/bin/env python3
"""
Pepper Simulator — drives Pepper (formerly Pepper) with realistic EA-style
conversations to surface response quality issues over time.

What it does
------------
- Picks a themed multi-turn conversation transcript based on time-of-day
  and a rotation, then sends each turn to Pepper's `/chat` HTTP endpoint
  using the same `session_id` Telegram uses, so Pepper sees them as a
  continuation of the real chat.
- Pauses a few seconds between turns inside a conversation.
- Pauses a few minutes between conversations.
- Logs every send/receive line to `logs/pepper_simulator.log`.

Hard rules baked into every transcript
--------------------------------------
- All status queries are READ-ONLY.
- Any message/email/Slack reply is DRAFT-ONLY — Pepper is told explicitly
  to NOT send anything, just draft and show.
- Never asks Pepper to delete anything, ever.

Themes are inspired by real questions in `logs/pepper.log`:
  identity, calendar (work + family), email triage (gmail/yahoo,
  renewals), draft replies, commitments, family (Susan, Matthew),
  driving/errands, system self-health, start/mid/end-of-day summaries.

Usage
-----
    python scripts/pepper_simulator.py            # run forever
    python scripts/pepper_simulator.py --once     # one conversation, exit
    python scripts/pepper_simulator.py --theme morning_brief
    python scripts/pepper_simulator.py --list     # list themes
    python scripts/pepper_simulator.py --dry-run  # print what it would send

Environment (read from .env or the process env):
    API_KEY                      — Pepper API key (required)
    PORT                         — Pepper port (default 8000)
    PEPPER_SIM_SESSION_ID        — session id to use (default: first
                                   TELEGRAM_ALLOWED_USER_IDS so it lands on
                                   the same path as the real Telegram chat)
    PEPPER_SIM_MIN_GAP_SECS      — min seconds between conversations (default 180)
    PEPPER_SIM_MAX_GAP_SECS      — max seconds between conversations (default 420)
    PEPPER_SIM_TURN_GAP_SECS     — base seconds between turns inside one
                                   conversation (default 8, jittered ±50%)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Iterable

import httpx

# ─── Paths & env ────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "pepper_simulator.log"


def _load_dotenv() -> None:
    """Tiny .env loader so we don't pull in python-dotenv as a dep."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Don't clobber values already set in the real environment
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


PORT = int(_env("PORT", "8000") or "8000")
API_KEY = _env("API_KEY")
if not API_KEY:
    print("error: API_KEY not set in environment or .env", file=sys.stderr)
    sys.exit(2)

_default_sid = (_env("TELEGRAM_ALLOWED_USER_IDS", "") or "").split(",")[0].strip() or "pepper-sim"
SESSION_ID = _env("PEPPER_SIM_SESSION_ID", _default_sid) or "pepper-sim"

MIN_GAP = int(_env("PEPPER_SIM_MIN_GAP_SECS", "180") or "180")
MAX_GAP = int(_env("PEPPER_SIM_MAX_GAP_SECS", "420") or "420")
TURN_GAP = float(_env("PEPPER_SIM_TURN_GAP_SECS", "8") or "8")

CHAT_URL = f"http://localhost:{PORT}/chat"
HTTP_TIMEOUT = 300.0  # heavy queries can take a while

# ─── Logging ────────────────────────────────────────────────────────────────

logger = logging.getLogger("pepper_sim")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
_fh = logging.FileHandler(LOG_FILE)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)
logger.propagate = False


# ─── Themed transcripts ─────────────────────────────────────────────────────
#
# Each theme is a list of user turns. Pepper replies in between.
# Every transcript should be (a) safe — read-only or draft-only, never a
# send/delete, and (b) realistic — phrased the way Jack actually talks
# in logs/pepper.log.
#
# Time-of-day buckets decide which themes are eligible at a given hour.

THEMES: dict[str, list[str]] = {
    # ─── Day rhythm summaries ──────────────────────────────────────────────
    "morning_brief": [
        "good morning Pepper — give me a tight start-of-day brief: top 3 things I should care about today across calendar, email, and messages. read-only, don't send anything.",
        "of those, what's the single most important one and why?",
        "anything from yesterday I committed to that I haven't followed up on yet?",
        "what's on my calendar before noon?",
    ],
    "midday_check": [
        "midday check-in — what's left on my calendar for the rest of today?",
        "any urgent emails that came in this morning I should know about? summarize, don't reply.",
        "anything I said I'd do today that I still haven't done?",
        "anyone waiting on a response from me right now? just list them, don't draft anything yet.",
    ],
    "eod_wrap": [
        "end of day wrap — what did I get through today vs what slipped?",
        "give me a 5-bullet summary of the most important things that happened across email, calendar, and messages today. read-only.",
        "what's the first thing I should look at tomorrow morning?",
        "anything I owe somebody a reply on that's been sitting more than a day? don't send anything, just list them.",
    ],

    # ─── Status / read-only queries ────────────────────────────────────────
    "calendar_status_work": [
        "what's my schedule like for work next week?",
        "any back-to-back stretches I should try to break up?",
        "which of those meetings am I likely the decision-maker in vs just an attendee?",
        "is there anything on the calendar I should probably decline?",
    ],
    "calendar_status_family": [
        "how's my personal and family schedule look this week?",
        "anything Susan is doing with the kids that I should be aware of?",
        "any college tour stuff for Matthew coming up I should plan around?",
        "any conflicts between the family stuff and work stuff this week?",
    ],
    "email_triage": [
        "what's been going on in my personal gmail and yahoo accounts the last few days? summary only, read-only.",
        "anything that looks like a renewal or subscription charge I should review?",
        "anything from a real human being that's been sitting unread more than a day?",
        "for the most important one, draft a short reply for me to review — DO NOT send it, just show me the draft.",
    ],
    "email_renewals": [
        "any subscription renewals or auto-charges hitting in the next 30 days you can see in my inbox? read-only.",
        "anything that looks suspicious or that I probably forgot I signed up for?",
        "for the chatgpt one specifically — when does it renew and is there a cancellation link in any of the emails?",
        "do not click anything or send anything, just summarize what you found.",
    ],
    "comms_followups": [
        "who's currently waiting on a response from me across email, imessage, and slack? read-only summary.",
        "of those, who's been waiting the longest?",
        "for the top 2, draft a quick reply for me to review — DRAFT ONLY, do not send.",
        "any patterns? am I systematically dropping certain types of messages?",
    ],
    "comms_health": [
        "give me a comms health snapshot — anyone important I haven't talked to in a while?",
        "any relationships that look one-sided right now (they reach out, I don't)?",
        "for the top quiet contact, draft a casual check-in message I could send. DRAFT ONLY, don't send.",
    ],

    # ─── Memory / commitments / context ────────────────────────────────────
    "commitments_check": [
        "what have I committed to recently that's still open? read-only.",
        "any of those that have an implicit deadline this week?",
        "is there anything I committed to and then never mentioned again?",
        "don't mark anything resolved — I'll do that myself.",
    ],
    "memory_probe_people": [
        "what do you know about my wife Susan? just tell me what you have on file.",
        "what about Matthew — what's the current status with college tours?",
        "anyone else in the family I should be checking in on this week?",
        "read-only, don't update anything.",
    ],
    "memory_probe_work": [
        "what do you know about my work and what I'm working on there right now?",
        "who are the main people I interact with at work based on calendar and messages?",
        "anything you've picked up about projects I'm currently leading vs just attending?",
        "read-only.",
    ],
    "priority_focus": [
        "if you had to pick one thing for me to focus on today, what would it be and why? base it on calendar, email, and pending commitments.",
        "what's the second priority?",
        "what's something on my plate I should probably drop or push back?",
        "be direct, I want a real opinion.",
    ],

    # ─── Drafting messages (NEVER send) ────────────────────────────────────
    "draft_status_update": [
        "I want to send Susan a quick update on how my day is going. draft something casual and warm. DRAFT ONLY — do not send anything to anyone, just show me the text.",
        "make it shorter and less formal.",
        "now draft a separate one for my work team summarizing where I am on the current project. DRAFT ONLY, don't send.",
    ],
    "draft_meeting_decline": [
        "I want to politely decline a meeting. pick the most decline-able one on my calendar this week and draft a polite decline reply. DRAFT ONLY — do not send. do not actually decline it on the calendar either.",
        "make it warmer.",
        "now draft an alternative — a 'let's reschedule to next week' version. DRAFT ONLY.",
    ],

    # ─── Pepper self / system ──────────────────────────────────────────────
    "system_self": [
        "how's the system memory doing? are you staying healthy and below the limits of your hardware?",
        "any subsystems currently degraded or failing?",
        "what's the slowest part of your stack right now from your perspective?",
        "read-only — don't restart anything.",
    ],

    # ─── Logistics / errands ───────────────────────────────────────────────
    "logistics_driving": [
        "I might need to run some errands later — what's the driving time from home (1401 Aster Ln, Cupertino) to Office Depot in San Jose right now?",
        "what about to the nearest Costco?",
        "any errands I've mentioned recently that I haven't done yet? read-only.",
    ],
}


# Time-of-day buckets — local time. Each bucket lists eligible theme names.
BUCKETS: list[tuple[range, list[str]]] = [
    # 06–10: morning
    (range(6, 10), [
        "morning_brief", "calendar_status_work", "calendar_status_family",
        "priority_focus", "email_triage",
    ]),
    # 10–14: late morning / midday
    (range(10, 14), [
        "midday_check", "calendar_status_work", "comms_followups",
        "commitments_check", "memory_probe_work", "draft_meeting_decline",
    ]),
    # 14–18: afternoon
    (range(14, 18), [
        "email_triage", "comms_followups", "memory_probe_people",
        "logistics_driving", "draft_status_update", "comms_health",
    ]),
    # 18–23: evening
    (range(18, 23), [
        "eod_wrap", "calendar_status_family", "email_renewals",
        "memory_probe_people", "system_self",
    ]),
    # 23–06: overnight — keep it light
    (range(23, 24), ["system_self"]),
    (range(0, 6), ["system_self"]),
]


def themes_for_hour(hour: int) -> list[str]:
    for rng, names in BUCKETS:
        if hour in rng:
            return names
    return list(THEMES.keys())


def pick_theme(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    eligible = themes_for_hour(now.hour)
    # weighted toward variety: shuffle then pick
    return random.choice(eligible)


# ─── HTTP send loop ─────────────────────────────────────────────────────────


async def send_turn(client: httpx.AsyncClient, message: str) -> str:
    payload = {"message": message, "session_id": SESSION_ID}
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}
    r = await client.post(CHAT_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json().get("response", "")


def _truncate(s: str, n: int = 280) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def run_conversation(
    theme_name: str,
    dry_run: bool = False,
    emit_json: bool = False,
    only_turn: int | None = None,
) -> list[dict]:
    """Run one themed conversation. Returns a list of {turn, send, recv} dicts.

    If `only_turn` is set (1-indexed), only that single turn is sent — useful
    for retrying a specific turn after a code fix.
    """
    turns = THEMES[theme_name]
    indices = [only_turn - 1] if only_turn else list(range(len(turns)))
    logger.info(
        "conversation_start theme=%s turns=%d session=%s%s",
        theme_name, len(turns), SESSION_ID,
        f" only_turn={only_turn}" if only_turn else "",
    )
    results: list[dict] = []

    if dry_run:
        for idx in indices:
            t = turns[idx]
            logger.info("DRY turn=%d/%d send=%s", idx + 1, len(turns), _truncate(t))
            results.append({"theme": theme_name, "turn": idx + 1, "send": t, "recv": None, "dry_run": True})
        if emit_json:
            for row in results:
                print(json.dumps(row), flush=True)
        logger.info("conversation_end theme=%s (dry-run)", theme_name)
        return results

    async with httpx.AsyncClient() as client:
        for n, idx in enumerate(indices):
            turn = turns[idx]
            logger.info("turn=%d/%d send=%s", idx + 1, len(turns), _truncate(turn))
            try:
                response = await send_turn(client, turn)
            except Exception as e:
                logger.error("turn_failed theme=%s turn=%d error=%s", theme_name, idx + 1, e)
                row = {"theme": theme_name, "turn": idx + 1, "send": turn, "recv": None, "error": str(e)}
                results.append(row)
                if emit_json:
                    print(json.dumps(row), flush=True)
                return results
            logger.info("turn=%d/%d recv=%s", idx + 1, len(turns), _truncate(response, 400))
            row = {"theme": theme_name, "turn": idx + 1, "send": turn, "recv": response}
            results.append(row)
            if emit_json:
                print(json.dumps(row), flush=True)
            if n < len(indices) - 1:
                gap = max(2.0, TURN_GAP * random.uniform(0.5, 1.5))
                await asyncio.sleep(gap)
    logger.info("conversation_end theme=%s", theme_name)
    return results


async def daemon_loop(forced_theme: str | None = None) -> None:
    logger.info(
        "daemon_start url=%s session=%s gap=%d-%ds",
        CHAT_URL, SESSION_ID, MIN_GAP, MAX_GAP,
    )
    while True:
        theme = forced_theme or pick_theme()
        try:
            await run_conversation(theme)
        except Exception as e:
            logger.error("conversation_crashed theme=%s error=%s", theme, e)
        gap = random.randint(MIN_GAP, MAX_GAP)
        logger.info("sleeping_until_next secs=%d", gap)
        await asyncio.sleep(gap)


# ─── CLI ────────────────────────────────────────────────────────────────────


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Drive Pepper with simulated EA-style conversations.")
    p.add_argument("--once", action="store_true", help="Run one conversation then exit.")
    p.add_argument("--theme", help="Force a specific theme (see --list).")
    p.add_argument("--list", action="store_true", help="List available themes and exit.")
    p.add_argument("--dry-run", action="store_true", help="Print what would be sent without calling Pepper.")
    p.add_argument("--emit-json", action="store_true", help="Print one JSON object per turn to stdout (machine-readable).")
    p.add_argument("--turn", type=int, help="Run only this turn (1-indexed) — used for retries after a fix.")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.list:
        print("Available themes:")
        for name in sorted(THEMES):
            print(f"  {name:30s} ({len(THEMES[name])} turns)")
        print("\nTime-of-day buckets:")
        for rng, names in BUCKETS:
            print(f"  hours {rng.start:02d}-{rng.stop:02d}: {', '.join(names)}")
        return 0

    if args.theme and args.theme not in THEMES:
        print(f"error: unknown theme {args.theme!r}. use --list to see options.", file=sys.stderr)
        return 2

    if args.once or args.turn:
        theme = args.theme or pick_theme()
        asyncio.run(run_conversation(
            theme,
            dry_run=args.dry_run,
            emit_json=args.emit_json,
            only_turn=args.turn,
        ))
        return 0

    asyncio.run(daemon_loop(forced_theme=args.theme))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
