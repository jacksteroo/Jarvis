"""Phase 6.7 — Draft-and-queue for outbound actions.

All outbound writes (email send, message send, event create) go through this
queue rather than executing directly. Queued drafts are surfaced via the web UI
(and eventually Telegram) with explicit approve / edit / reject controls.

This is a complement to the MCP per-action write gate from Phase 5: that gate
requires user confirmation inside a single chat turn. The pending-actions
queue persists proposed writes across turns so a user can review and edit them
asynchronously from the web UI or a separate channel.

Design notes:
  - In-memory for now. Actions do not survive a Pepper restart. If/when long-
    lived drafts become a hard requirement, swap the dict for a DB-backed
    store — the public API is intentionally stable.
  - No TTL. A stale draft is better than a lost one; the user can dismiss it.
  - The queue does not execute anything itself — an executor callback passed
    to `approve()` does the dispatch. This keeps policy (what to run, how)
    separate from the queue (what's pending, who approved it).
"""
from __future__ import annotations

import time
import uuid
import structlog
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = structlog.get_logger()


Executor = Callable[[str, dict], Awaitable[dict]]


@dataclass
class PendingAction:
    id: str
    tool_name: str
    args: dict
    # `preview` is always server-derived from args and is the trusted display
    # string. `model_description` is the optional free-text summary the model
    # supplied at queue time — advisory only, shown as "model says:" so the
    # operator sees it but does not confuse it with the real payload.
    preview: str
    model_description: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"   # pending | approved | rejected | executed | failed
    result: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "args": self.args,
            "preview": self.preview,
            "model_description": self.model_description,
            "created_at": self.created_at,
            "status": self.status,
            "result": self.result,
        }


class PendingActionsQueue:
    """In-memory queue of proposed outbound actions awaiting user decision."""

    def __init__(self, executor: Optional[Executor] = None) -> None:
        self._items: dict[str, PendingAction] = {}
        self._executor = executor

    def set_executor(self, executor: Executor) -> None:
        """Wire the tool dispatcher that `approve()` uses to actually run the action."""
        self._executor = executor

    # ── Writes ─────────────────────────────────────────────────────────────────

    def queue(self, tool_name: str, args: dict, preview: str = "") -> PendingAction:
        """Enqueue a new action. Returns the stored PendingAction.

        The caller-supplied `preview` string (typically from the model) is
        stored as `model_description` but is NOT used as the authoritative
        display string. The trusted `preview` field is always derived from
        the actual args, so a hallucinated or adversarial tool call cannot
        present a benign summary while hiding a different payload.
        """
        action_id = uuid.uuid4().hex[:12]
        item = PendingAction(
            id=action_id,
            tool_name=tool_name,
            args=dict(args),
            preview=self._default_preview(tool_name, args),
            model_description=(preview or "").strip(),
        )
        self._items[action_id] = item
        logger.info(
            "pending_action_queued",
            id=action_id,
            tool=tool_name,
            preview=item.preview[:160],
        )
        return item

    def edit(self, action_id: str, edited_body: str) -> Optional[PendingAction]:
        """Replace the 'body' / primary content of a pending action.

        Edits the most common field names used by drafted writes: `body`, `text`,
        `message`, `content`. If none of those are present, no-op returns None.
        """
        item = self._items.get(action_id)
        if not item or item.status != "pending":
            return None
        for key in ("body", "text", "message", "content"):
            if key in item.args:
                item.args[key] = edited_body
                item.preview = self._default_preview(item.tool_name, item.args)
                logger.info("pending_action_edited", id=action_id, field=key)
                return item
        # No editable field — store under 'body' so approve() still has content
        item.args["body"] = edited_body
        item.preview = self._default_preview(item.tool_name, item.args)
        logger.info("pending_action_edited", id=action_id, field="body (new)")
        return item

    def reject(self, action_id: str) -> Optional[PendingAction]:
        item = self._items.get(action_id)
        if not item or item.status != "pending":
            return None
        item.status = "rejected"
        logger.info("pending_action_rejected", id=action_id, tool=item.tool_name)
        return item

    async def approve(self, action_id: str) -> Optional[PendingAction]:
        """Execute the pending action via the registered executor.

        Returns the updated item (status = executed | failed) or None if not found.
        """
        item = self._items.get(action_id)
        if not item or item.status != "pending":
            return None
        if not self._executor:
            logger.warning("pending_action_no_executor", id=action_id)
            item.status = "failed"
            item.result = {"error": "no executor wired"}
            return item
        item.status = "approved"
        try:
            result = await self._executor(item.tool_name, item.args)
            item.result = result
            # A response with approval_required=True means the executor hit a
            # secondary approval gate instead of actually running. That's not
            # success: the write did not go out. Treat it as failure so the
            # UI keeps the draft visible and surfaces the problem.
            if isinstance(result, dict) and "error" in result:
                item.status = "failed"
            elif isinstance(result, dict) and result.get("approval_required"):
                item.status = "failed"
                # Normalize to an error shape for UI + API consumers.
                item.result = {
                    "error": "write blocked by secondary approval gate",
                    "raw": result,
                }
            else:
                item.status = "executed"
            logger.info(
                "pending_action_executed",
                id=action_id,
                tool=item.tool_name,
                status=item.status,
            )
        except Exception as e:
            item.result = {"error": str(e)}
            item.status = "failed"
            logger.warning("pending_action_failed", id=action_id, error=str(e))
        return item

    # ── Reads ──────────────────────────────────────────────────────────────────

    def get(self, action_id: str) -> Optional[PendingAction]:
        return self._items.get(action_id)

    def list_pending(self) -> list[dict]:
        return [i.to_dict() for i in self._items.values() if i.status == "pending"]

    def list_all(self) -> list[dict]:
        return [i.to_dict() for i in self._items.values()]

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _default_preview(tool_name: str, args: dict) -> str:
        recipient = (
            args.get("to") or args.get("recipient") or args.get("channel")
            or args.get("chat_id") or args.get("address") or ""
        )
        body = (
            args.get("body") or args.get("text") or args.get("message")
            or args.get("content") or args.get("subject") or ""
        )
        body_preview = (body[:140] + "…") if len(body) > 140 else body
        if recipient and body_preview:
            return f"{tool_name} → {recipient}: {body_preview}"
        if recipient:
            return f"{tool_name} → {recipient}"
        if body_preview:
            return f"{tool_name}: {body_preview}"
        return tool_name
