"""Outbound send tools (email / iMessage / WhatsApp).

Two surfaces:

1. **Draft tools** (`draft_email_reply`, `draft_email_new`, `draft_imessage`,
   `draft_whatsapp`) — exposed to the LLM. They never send. Each one queues
   a `PendingAction` whose `tool_name` is the corresponding `send_*` and waits
   for explicit user approval (Telegram inline buttons or the web UI).

2. **Send executors** (`execute_send_email`, `execute_send_imessage`,
   `execute_send_whatsapp`) — invoked only by `PendingActionsQueue.approve`.
   These are deliberately NOT registered in the LLM-visible tool registry, so
   the model cannot bypass approval.

Cross-cutting:

- `RateLimiter` enforces a simple per-channel min-interval (defense in depth;
  the approval gate is the primary control).
- Every send is appended to `AuditLog` with status (`sent` / `failed`).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import structlog

from agent.models import AuditLog

logger = structlog.get_logger()


# ── Rate limiter ───────────────────────────────────────────────────────────

class RateLimiter:
    """Per-key min-interval rate limiter. Process-local; fine for a single user."""

    def __init__(self, min_interval_seconds: dict[str, float]):
        # e.g. {"email": 2.0, "imessage": 1.0, "whatsapp": 1.0}
        self._intervals = min_interval_seconds
        self._last_call: dict[str, float] = {}

    def check(self, key: str) -> Optional[float]:
        """Return the seconds remaining if rate-limited, else None."""
        interval = self._intervals.get(key)
        if not interval:
            return None
        last = self._last_call.get(key, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < interval:
            return interval - elapsed
        return None

    def record(self, key: str) -> None:
        self._intervals.get(key)  # no-op when key unknown; record anyway for visibility
        self._last_call[key] = time.monotonic()


_rate_limiter = RateLimiter({"email": 2.0, "imessage": 1.0, "whatsapp": 1.0})


# ── Audit log ──────────────────────────────────────────────────────────────

DbFactory = Callable[[], Any]  # async context manager factory


async def _audit(db_factory: Optional[DbFactory], event_type: str, details: str) -> None:
    if not db_factory:
        return
    try:
        async with db_factory() as session:
            session.add(AuditLog(event_type=event_type, details=details[:4000]))
            await session.commit()
    except Exception as exc:
        logger.warning("send_audit_failed", event=event_type, error=str(exc))


# ── Send executors (private surface — only the queue calls these) ──────────

async def execute_send_email(args: dict, db_factory: Optional[DbFactory] = None) -> dict:
    """Send a drafted email. Routes by `account` to Gmail or IMAP/SMTP."""
    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    account = (args.get("account") or "default").strip()
    in_reply_to = (args.get("in_reply_to_id") or "").strip() or None
    cc = (args.get("cc") or "").strip() or None
    bcc = (args.get("bcc") or "").strip() or None

    if not to:
        return {"error": "send_email: 'to' is required"}
    if not body:
        return {"error": "send_email: 'body' is required"}

    wait = _rate_limiter.check("email")
    if wait:
        return {"error": f"send_email rate limited; retry in {wait:.1f}s"}

    try:
        provider = _detect_email_provider(account)
        if provider == "gmail":
            from subsystems.communications.gmail_client import GmailClient
            client = GmailClient(account)
            sent = await _to_thread(
                client.send_message, to, subject, body, in_reply_to, cc, bcc
            )
        else:
            from subsystems.communications.imap_client import ImapClient
            client = ImapClient(account)
            sent = await _to_thread(
                client.send_message, to, subject, body, in_reply_to, cc, bcc
            )
        _rate_limiter.record("email")
        await _audit(
            db_factory,
            "send_email",
            f"account={account} to={to} subject={subject!r} bytes={len(body)} id={sent.get('id')}",
        )
        return {"ok": True, "channel": "email", "account": account, "id": sent.get("id"), "thread_id": sent.get("threadId")}
    except Exception as exc:
        await _audit(db_factory, "send_email_failed", f"account={account} to={to} error={exc}")
        logger.error("send_email_failed", account=account, error=str(exc))
        return {"error": f"send_email failed: {exc}"}


async def execute_send_imessage(args: dict, db_factory: Optional[DbFactory] = None) -> dict:
    """Send a drafted iMessage."""
    to = (args.get("to") or "").strip()
    body = args.get("body") or ""
    chat_guid = (args.get("chat_guid") or "").strip() or None

    if not body:
        return {"error": "send_imessage: 'body' is required"}
    if not to and not chat_guid:
        return {"error": "send_imessage: either 'to' or 'chat_guid' is required"}

    wait = _rate_limiter.check("imessage")
    if wait:
        return {"error": f"send_imessage rate limited; retry in {wait:.1f}s"}

    try:
        from subsystems.communications.imessage_client import IMessageClient
        client = IMessageClient()
        sent = await client.send(to, body, chat_guid=chat_guid)
        _rate_limiter.record("imessage")
        await _audit(
            db_factory,
            "send_imessage",
            f"to={(chat_guid or to)} bytes={len(body)}",
        )
        return {"ok": True, "channel": "imessage", **sent}
    except Exception as exc:
        await _audit(db_factory, "send_imessage_failed", f"to={(chat_guid or to)} error={exc}")
        logger.error("send_imessage_failed", to=(chat_guid or to)[:40], error=str(exc))
        return {"error": f"send_imessage failed: {exc}"}


async def execute_send_whatsapp(args: dict, db_factory: Optional[DbFactory] = None) -> dict:
    """Send a drafted WhatsApp message via the local Node.js bridge."""
    chat_id = (args.get("chat_id") or args.get("to") or "").strip()
    body = args.get("body") or args.get("message") or ""
    reply_to = (args.get("reply_to") or "").strip() or None

    if not chat_id:
        return {"error": "send_whatsapp: 'chat_id' is required (phone digits or JID)"}
    if not body:
        return {"error": "send_whatsapp: 'body' is required"}

    wait = _rate_limiter.check("whatsapp")
    if wait:
        return {"error": f"send_whatsapp rate limited; retry in {wait:.1f}s"}

    try:
        from subsystems.communications.whatsapp_client import WhatsAppClient
        client = WhatsAppClient()
        sent = await client.send(chat_id, body, reply_to=reply_to)
        _rate_limiter.record("whatsapp")
        await _audit(
            db_factory,
            "send_whatsapp",
            f"chat_id={chat_id} bytes={len(body)} id={sent.get('id')}",
        )
        return {"ok": True, "channel": "whatsapp", **sent}
    except Exception as exc:
        await _audit(db_factory, "send_whatsapp_failed", f"chat_id={chat_id} error={exc}")
        logger.error("send_whatsapp_failed", chat_id=chat_id[:32], error=str(exc))
        return {"error": f"send_whatsapp failed: {exc}"}


# ── LLM-visible draft tools ────────────────────────────────────────────────
#
# These never send directly — they always queue. The shape mirrors what the
# corresponding executor expects so approval can dispatch with no translation.

SEND_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "draft_email_reply",
            "description": (
                "Queue a drafted email REPLY for the user to approve. Use this for any reply to "
                "an email that came in. Threading (In-Reply-To/References + Gmail threadId) is "
                "handled automatically when 'in_reply_to_id' is supplied. Never sends without "
                "explicit user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account": {"type": "string", "description": "Account name (e.g. 'default', 'work'). Default: 'default'."},
                    "to": {"type": "string", "description": "Recipient email. May include comma-separated addresses."},
                    "subject": {"type": "string", "description": "Subject line. For replies, use 'Re: <original subject>'."},
                    "body": {"type": "string", "description": "Plain-text body."},
                    "in_reply_to_id": {"type": "string", "description": "ID of the parent message you are replying to (Gmail message id or IMAP uid)."},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_email_new",
            "description": (
                "Queue a NEW (non-reply) email for the user to approve. Use draft_email_reply "
                "instead whenever you have a parent message id available. Never sends without "
                "explicit user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account": {"type": "string"},
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_imessage",
            "description": (
                "Queue an outgoing iMessage for the user to approve. Supply 'to' for 1:1 (phone "
                "or email) OR 'chat_guid' for an existing group chat. Never sends without "
                "explicit user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number or email. Required for 1:1 chats."},
                    "chat_guid": {"type": "string", "description": "Existing chat GUID (use for group chats; from get_recent_imessages)."},
                    "body": {"type": "string", "description": "Message text."},
                },
                "required": ["body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_whatsapp",
            "description": (
                "Queue an outgoing WhatsApp message for the user to approve. 'chat_id' may be a "
                "phone number, a user JID (...@c.us), or a group JID (...@g.us) — use the value "
                "from get_recent_whatsapp_chats. Requires the local WhatsApp send bridge to be "
                "running. Never sends without explicit user approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "Phone digits, user JID, or group JID."},
                    "body": {"type": "string"},
                    "reply_to": {"type": "string", "description": "Optional: bridge message id to quote."},
                },
                "required": ["chat_id", "body"],
            },
        },
    },
]


def queue_draft(
    pending_actions,
    *,
    send_tool_name: str,
    send_args: dict,
    preview_subject: str = "",
) -> dict:
    """Drop a draft into the pending-actions queue. Returns the LLM-facing result."""
    action = pending_actions.queue(send_tool_name, send_args, preview_subject)
    return {
        "ok": True,
        "queued": True,
        "channel": send_tool_name.removeprefix("send_"),
        "action_id": action.id,
        "preview": action.preview,
        "message": (
            "Draft queued for your approval. Approve, edit, or reject it from "
            "Telegram or the Pepper status panel."
        ),
    }


async def execute_draft_tool(name: str, args: dict, *, pending_actions) -> dict:
    """Dispatcher invoked by PepperCore for any draft_* tool name."""
    if name == "draft_email_reply":
        return queue_draft(
            pending_actions,
            send_tool_name="send_email",
            send_args={
                "account": args.get("account") or "default",
                "to": args.get("to", ""),
                "subject": args.get("subject", ""),
                "body": args.get("body", ""),
                "in_reply_to_id": args.get("in_reply_to_id", ""),
                "cc": args.get("cc", ""),
                "bcc": args.get("bcc", ""),
            },
            preview_subject=args.get("subject", ""),
        )
    if name == "draft_email_new":
        return queue_draft(
            pending_actions,
            send_tool_name="send_email",
            send_args={
                "account": args.get("account") or "default",
                "to": args.get("to", ""),
                "subject": args.get("subject", ""),
                "body": args.get("body", ""),
                "cc": args.get("cc", ""),
                "bcc": args.get("bcc", ""),
            },
            preview_subject=args.get("subject", ""),
        )
    if name == "draft_imessage":
        return queue_draft(
            pending_actions,
            send_tool_name="send_imessage",
            send_args={
                "to": args.get("to", ""),
                "chat_guid": args.get("chat_guid", ""),
                "body": args.get("body", ""),
            },
        )
    if name == "draft_whatsapp":
        return queue_draft(
            pending_actions,
            send_tool_name="send_whatsapp",
            send_args={
                "chat_id": args.get("chat_id", ""),
                "body": args.get("body", ""),
                "reply_to": args.get("reply_to", ""),
            },
        )
    return {"error": f"unknown draft tool: {name}"}


# ── Helpers ────────────────────────────────────────────────────────────────

def _detect_email_provider(account_name: str) -> str:
    """Heuristic: a Google OAuth token at the expected path means Gmail."""
    from subsystems.google_auth import token_path
    path = token_path(None if account_name in ("default", "personal") else account_name)
    return "gmail" if path.exists() else "imap"


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)


DRAFT_TOOL_NAMES = frozenset({
    "draft_email_reply", "draft_email_new", "draft_imessage", "draft_whatsapp",
})

SEND_TOOL_NAMES = frozenset({"send_email", "send_imessage", "send_whatsapp"})
