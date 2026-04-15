"""Email tool definitions and helpers for Pepper core.

Follows the same pattern as calendar_tools.py:
  - EMAIL_TOOLS: Anthropic tool-schema list for the LLM
  - execute_* functions called by PepperCore._execute_tool
  - maybe_get_email_context for proactive injection

Accounts:
  "personal" → personal Google account (shared Gmail + Calendar OAuth2)
  "work"     → work Google account (shared Gmail + Calendar OAuth2)
  "yahoo"    → Yahoo Mail (IMAP app password)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

logger = structlog.get_logger()

_ACTION_ITEM_TRIGGERS = (
    "action item",
    "action items",
    "follow up",
    "follow-up",
    "todo",
    "to do",
    "need to reply",
    "needs a reply",
    "needs reply",
    "need a response",
    "needs a response",
    "what do i owe",
    "what am i missing",
    "what needs my attention",
)

_ACTION_PATTERNS: tuple[tuple[str, int, str], ...] = (
    ("action required", 4, "explicit action required"),
    ("please respond", 4, "asks for a response"),
    ("please reply", 4, "asks for a reply"),
    ("can you", 3, "asks you to do something"),
    ("could you", 3, "asks you to do something"),
    ("please review", 3, "requests review"),
    ("please confirm", 3, "requests confirmation"),
    ("need your", 3, "needs your input"),
    ("needs your", 3, "needs your input"),
    ("let me know", 2, "awaiting your answer"),
    ("reply", 2, "mentions a reply"),
    ("respond", 2, "mentions a response"),
    ("follow up", 2, "mentions follow-up"),
    ("follow-up", 2, "mentions follow-up"),
    ("deadline", 2, "mentions a deadline"),
    ("due", 2, "mentions timing"),
    ("eod", 2, "mentions timing"),
    ("today", 1, "time-sensitive wording"),
    ("tomorrow", 1, "time-sensitive wording"),
    ("urgent", 3, "marked urgent"),
    ("asap", 3, "marked ASAP"),
    ("approve", 2, "requests approval"),
    ("approval", 2, "requests approval"),
    ("confirm", 2, "requests confirmation"),
    ("review", 2, "requests review"),
    ("send", 1, "asks for a send/follow-up"),
    ("schedule", 1, "mentions scheduling"),
    ("availability", 1, "mentions scheduling"),
)

_LOW_SIGNAL_PATTERNS: tuple[tuple[str, int], ...] = (
    ("newsletter", -3),
    ("unsubscribe", -3),
    ("sale", -2),
    ("discount", -2),
    ("receipt", -1),
    ("order shipped", -1),
    ("tracking", -1),
)

def _imap_account_ids() -> list[str]:
    from agent.accounts import get_imap_account_ids
    return get_imap_account_ids()


def _email_label(account_id: str) -> str:
    from agent.accounts import get_email_label
    return get_email_label(account_id)


def _discover_gmail_accounts() -> list[str]:
    """Return all Google accounts that have a shared Gmail+Calendar token."""
    from subsystems.google_auth import list_authorized_accounts

    accounts = []
    for account in list_authorized_accounts():
        accounts.append("personal" if account == "default" else account)
    return accounts


def _get_gmail_client(account_name: str):
    from subsystems.communications.gmail_client import GmailClient
    return GmailClient(account_name)


def _get_imap_client(account_name: str):
    from subsystems.communications.imap_client import ImapClient
    return ImapClient(account_name)


def _get_client(account_name: str):
    from agent.accounts import get_google_auth_account

    auth_account = get_google_auth_account(account_name)
    gmail_accounts = set(_discover_gmail_accounts())
    if auth_account in gmail_accounts:
        return _get_gmail_client(auth_account)
    if account_name in _imap_account_ids():
        return _get_imap_client(account_name)
    raise ValueError(
        "Unknown email account: "
        f"'{account_name}'"
        + (
            f" (mapped to Google auth account '{auth_account}')"
            if auth_account != account_name
            else ""
        )
        + f". Authorized Google mail accounts: {sorted(gmail_accounts)}"
    )


def _build_account_description() -> str:
    """Build dynamic account list for tool descriptions."""
    from agent.accounts import get_email_accounts
    accounts = get_email_accounts()
    if not accounts:
        return "'all' to check all accounts."
    parts = [f"'{a['id']}' ({a['label']})" for a in accounts]
    return ", ".join(parts) + ", or 'all' to check all accounts."


def _get_email_accounts() -> list[dict[str, Any]]:
    from agent.accounts import get_email_accounts

    return get_email_accounts()


def detect_email_account_scope(user_message: str) -> str:
    """Return a configured account id mentioned in the query, else 'all'."""
    lower = user_message.lower()
    matched: list[str] = []
    for account in _get_email_accounts():
        account_id = str(account.get("id", "")).strip()
        if not account_id:
            continue
        candidates = {
            account_id.lower(),
            str(account.get("label", "")).strip().lower(),
            account_id.replace("_", " ").lower(),
        }
        if account_id == "personal":
            candidates.add("default")
        for candidate in candidates:
            if candidate and candidate in lower:
                matched.append(account_id)
                break
    matched = list(dict.fromkeys(matched))
    return matched[0] if len(matched) == 1 else "all"


def is_email_action_items_query(user_message: str) -> bool:
    lower = user_message.lower()
    if not any(t in lower for t in _EMAIL_TRIGGERS):
        return False
    return any(t in lower for t in _ACTION_ITEM_TRIGGERS)


def _email_text(msg: dict[str, Any]) -> str:
    parts = [
        msg.get("subject", ""),
        msg.get("snippet", ""),
        msg.get("from", ""),
    ]
    return " ".join(part for part in parts if part).lower()


def _score_actionability(msg: dict[str, Any]) -> tuple[int, list[str]]:
    text = _email_text(msg)
    from_addr = (msg.get("from") or "").lower()
    score = 0
    reasons: list[str] = []

    if msg.get("unread"):
        score += 2
        reasons.append("unread")

    for phrase, weight, reason in _ACTION_PATTERNS:
        if phrase in text:
            score += weight
            if reason not in reasons:
                reasons.append(reason)

    for phrase, penalty in _LOW_SIGNAL_PATTERNS:
        if phrase in text:
            score += penalty

    if "no-reply" in from_addr or "noreply" in from_addr:
        score -= 2
    if "mailer-daemon" in from_addr:
        score -= 3

    return score, reasons[:3]


def _clean_sender(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value[:80]


def _clean_subject(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "(no subject)").strip()
    return value[:100]


def _format_action_item(msg: dict[str, Any], reasons: list[str]) -> str:
    account = _email_label(msg.get("account", "")) if msg.get("account") else ""
    sender = _clean_sender(msg.get("from", ""))
    subject = _clean_subject(msg.get("subject", ""))
    unread = " [UNREAD]" if msg.get("unread") else ""
    reason_text = ", ".join(reasons) if reasons else "worth reviewing"
    prefix = f"[{account}] " if account else ""
    return f"{prefix}{subject}{unread} — from {sender}. Why: {reason_text}."


EMAIL_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_recent_emails",
            "description": (
                "Fetch recent email headers and snippets from one or all email accounts. "
                "Use when asked about inbox, unread emails, recent messages, or what emails came in. "
                "Accounts: " + _build_account_description()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account": {
                        "type": "string",
                        "description": "Which account to check: " + _build_account_description(),
                        "default": "all",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max number of emails to return per account (default 10, max 30)",
                        "default": 10,
                    },
                    "hours": {
                        "type": "integer",
                        "description": (
                            "Look back this many hours (default 24). "
                            "Convert time ranges to hours before passing: "
                            "1 day = 24, 1 week = 168, 2 weeks = 336, 1 month = 720."
                        ),
                        "default": 24,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "search_emails",
            "description": (
                "Search emails by keyword, sender, or subject across one or all accounts. "
                "For Gmail accounts, supports Gmail search syntax (e.g. 'from:boss@co.com', "
                "'subject:invoice', 'has:attachment'). For Yahoo, searches by subject and sender."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (keyword, sender email, or Gmail search syntax)",
                    },
                    "account": {
                        "type": "string",
                        "description": "Which account to search: " + _build_account_description(),
                        "default": "all",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results per account (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "get_email_unread_counts",
            "description": (
                "Get unread email counts across all connected accounts. "
                "Use when asked 'how many unread emails do I have' or 'check my inbox'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

def _all_accounts() -> list[str]:
    """Return all configured email accounts from accounts.json."""
    from agent.accounts import get_email_account_ids
    ids = get_email_account_ids()
    if ids:
        return ids
    # Fallback: discover from auth files if accounts.json not configured
    from pathlib import Path

    config_dir = Path.home() / ".config" / "pepper"
    gmail = _discover_gmail_accounts()
    imap = _imap_account_ids() if (
        (config_dir / "yahoo_credentials.json").exists()
        or (config_dir / "email_credentials.json").exists()
    ) else []
    return gmail + imap


def _format_email(msg: dict[str, Any], include_account: bool = True) -> str:
    date = msg.get("date", "")
    from_addr = msg.get("from", "")
    subject = msg.get("subject", "(no subject)")
    snippet = msg.get("snippet", "")
    unread = msg.get("unread", False)
    account = msg.get("account", "")

    unread_marker = " [UNREAD]" if unread else ""
    parts = [f"{date} — {from_addr} — {subject}{unread_marker}"]
    if snippet:
        parts.append(f"  Preview: {snippet[:120]}")
    if include_account and account:
        parts.append(f"  Account: {account}")
    return "\n".join(parts)


async def execute_get_recent_emails(args: dict) -> dict:
    account = args.get("account", "all")
    count = min(int(args.get("count", 10)), 30)
    hours = int(args.get("hours", 24))

    accounts_to_check = _all_accounts() if account == "all" else [account]
    all_emails = []
    errors = []

    for acct in accounts_to_check:
        try:
            client = _get_client(acct)
            msgs = await asyncio.to_thread(client.get_recent_messages, count, hours)
            for msg in msgs:
                msg["account"] = acct
            all_emails.extend(msgs)
        except FileNotFoundError:
            errors.append(f"{acct}: not configured (run setup_auth.py)")
        except Exception as e:
            logger.warning("email_fetch_failed", account=acct, error=str(e))
            errors.append(f"{acct}: {e}")

    if not all_emails and errors:
        return {"error": "; ".join(errors)}

    formatted = [_format_email(m, include_account=(account == "all")) for m in all_emails]
    result: dict = {
        "items": all_emails,
        "emails": formatted,
        "count": len(all_emails),
        "hours": hours,
        "summary": f"{len(all_emails)} email(s) in the last {hours} hour(s).",
    }
    if errors:
        result["warnings"] = errors
    return result


async def execute_search_emails(args: dict) -> dict:
    query = args.get("query", "")
    account = args.get("account", "all")
    count = min(int(args.get("count", 10)), 30)

    if not query:
        return {"error": "query is required"}

    accounts_to_check = _all_accounts() if account == "all" else [account]
    all_emails = []
    errors = []

    for acct in accounts_to_check:
        try:
            client = _get_client(acct)
            msgs = await asyncio.to_thread(client.search_messages, query, count)
            for msg in msgs:
                msg["account"] = acct
            all_emails.extend(msgs)
        except FileNotFoundError:
            errors.append(f"{acct}: not configured (run setup_auth.py)")
        except Exception as e:
            logger.warning("email_search_failed", account=acct, error=str(e))
            errors.append(f"{acct}: {e}")

    if not all_emails and errors:
        return {"error": "; ".join(errors)}

    formatted = [_format_email(m, include_account=(account == "all")) for m in all_emails]
    result: dict = {
        "items": all_emails,
        "emails": formatted,
        "count": len(all_emails),
        "query": query,
        "summary": f"Found {len(all_emails)} email(s) matching '{query}'.",
    }
    if errors:
        result["warnings"] = errors
    return result


async def execute_get_email_unread_counts(args: dict) -> dict:
    counts = {}
    errors = []

    for acct in _all_accounts():
        try:
            client = _get_client(acct)
            n = await asyncio.to_thread(client.get_unread_count)
            counts[acct] = n
        except FileNotFoundError:
            errors.append(f"{acct}: not configured")
        except Exception as e:
            logger.warning("unread_count_failed", account=acct, error=str(e))
            errors.append(f"{acct}: {e}")

    total = sum(counts.values())
    result: dict = {
        "counts": counts,
        "total_unread": total,
        "summary": f"{total} unread email(s) across {len(counts)} account(s).",
    }
    if errors:
        result["warnings"] = errors
    return result


async def execute_get_email_action_items(args: dict) -> dict:
    account = args.get("account", "all")
    hours = int(args.get("hours", 168))
    count = min(int(args.get("count", 8)), 30)

    recent = await execute_get_recent_emails(
        {"account": account, "count": count, "hours": hours}
    )
    if "error" in recent:
        return recent

    scored: list[dict[str, Any]] = []
    for msg in recent.get("items", []):
        score, reasons = _score_actionability(msg)
        if score < 3:
            continue
        scored.append(
            {
                "account": msg.get("account", ""),
                "from": _clean_sender(msg.get("from", "")),
                "subject": _clean_subject(msg.get("subject", "")),
                "unread": bool(msg.get("unread")),
                "score": score,
                "reasons": reasons,
                "formatted": _format_action_item(msg, reasons),
            }
        )

    scored.sort(
        key=lambda item: (
            item["score"],
            1 if item["unread"] else 0,
        ),
        reverse=True,
    )
    top = scored[:5]

    result: dict[str, Any] = {
        "action_items": top,
        "count": len(top),
        "scanned_count": len(recent.get("items", [])),
        "summary": (
            f"Found {len(top)} likely email action item(s) in the last {hours} hour(s)."
            if top
            else f"No obvious email action items found in the last {hours} hour(s)."
        ),
    }
    if recent.get("warnings"):
        result["warnings"] = recent["warnings"]
    return result


_EMAIL_TRIGGERS = (
    "email", "inbox", "gmail", "yahoo", "mail",
    "unread", "message", "messages",
    "did i get", "any emails", "check my email",
    "from my", "sent me",
)


async def maybe_get_email_context(user_message: str) -> str:
    """Proactively inject unread counts when the query is email-related."""
    lower = user_message.lower()
    if not any(t in lower for t in _EMAIL_TRIGGERS):
        return ""

    try:
        result = await execute_get_email_unread_counts({})
        if "error" in result or not result.get("counts"):
            return ""
        counts = result["counts"]
        lines = ["Email unread counts:"]
        for acct, n in counts.items():
            lines.append(f"  {acct}: {n} unread")
        scope = detect_email_account_scope(user_message)
        if is_email_action_items_query(user_message):
            action_items = await execute_get_email_action_items(
                {"account": scope, "count": 8, "hours": 168}
            )
            if action_items.get("action_items"):
                lines.append("")
                lines.append("Likely email action items from recent inbox messages:")
                for item in action_items["action_items"]:
                    lines.append(f"  - {item['formatted']}")
            else:
                lines.append("")
                lines.append(
                    "No obvious email action items surfaced from recent subject lines/snippets."
                )
        elif scope != "all":
            recent = await execute_get_recent_emails(
                {"account": scope, "count": 5, "hours": 72}
            )
            if recent.get("items"):
                label = _email_label(scope) if scope != "all" else "connected accounts"
                lines.append("")
                lines.append(f"Recent emails from {label}:")
                for msg in recent["items"][:5]:
                    sender = _clean_sender(msg.get("from", ""))
                    subject = _clean_subject(msg.get("subject", ""))
                    unread = " [UNREAD]" if msg.get("unread") else ""
                    lines.append(f"  - {subject}{unread} — from {sender}")
            else:
                lines.append("")
                lines.append("No recent emails surfaced for that account.")
        logger.debug("email_context_injected", total=result["total_unread"])
        return "\n".join(lines)
    except Exception as e:
        logger.warning("email_proactive_failed", error=str(e))
        return ""
