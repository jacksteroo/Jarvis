"""Tests for Phase 6.7 — PendingActionsQueue."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent.pending_actions import PendingActionsQueue


@pytest.fixture
def queue():
    return PendingActionsQueue()


def test_queue_stores_and_lists(queue):
    item = queue.queue("send_imessage", {"to": "sarah", "body": "hey"}, preview="send sarah hey")
    assert item.status == "pending"
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["id"] == item.id
    assert pending[0]["tool_name"] == "send_imessage"


def test_queue_default_preview_uses_recipient_and_body(queue):
    item = queue.queue("send_email", {"to": "a@b.com", "body": "hello world"})
    assert "a@b.com" in item.preview
    assert "hello world" in item.preview


def test_queue_ignores_model_preview_for_authoritative_display(queue):
    item = queue.queue(
        "send_email",
        {"to": "real@dest.com", "body": "actual payload"},
        preview="harmless summary to Sarah",
    )
    assert "real@dest.com" in item.preview
    assert "actual payload" in item.preview
    assert "harmless summary" not in item.preview


def test_queue_preserves_model_description_separately(queue):
    item = queue.queue(
        "send_email",
        {"to": "real@dest.com", "body": "actual payload"},
        preview="harmless summary to Sarah",
    )
    payload = item.to_dict()
    assert payload["model_description"] == "harmless summary to Sarah"


def test_edit_replaces_body(queue):
    item = queue.queue("send_email", {"to": "a@b.com", "body": "draft"})
    updated = queue.edit(item.id, "final text")
    assert updated is not None
    assert updated.args["body"] == "final text"
    assert "final text" in updated.preview


def test_edit_writes_body_when_no_editable_field(queue):
    item = queue.queue("create_event", {"title": "Meeting"})
    updated = queue.edit(item.id, "edited")
    assert updated.args["body"] == "edited"


def test_edit_returns_none_for_unknown_id(queue):
    assert queue.edit("nope", "x") is None


def test_reject_marks_status(queue):
    item = queue.queue("send_email", {"to": "a@b.com", "body": "x"})
    rejected = queue.reject(item.id)
    assert rejected.status == "rejected"
    # No longer pending
    assert queue.list_pending() == []


def test_reject_idempotent_after_non_pending(queue):
    item = queue.queue("send_email", {"to": "a", "body": "b"})
    queue.reject(item.id)
    assert queue.reject(item.id) is None


@pytest.mark.asyncio
async def test_approve_executes_via_registered_executor(queue):
    executor = AsyncMock(return_value={"ok": True, "sent": True})
    queue.set_executor(executor)

    item = queue.queue("send_imessage", {"to": "sarah", "body": "hey"})
    result = await queue.approve(item.id)

    executor.assert_awaited_once_with("send_imessage", {"to": "sarah", "body": "hey"})
    assert result.status == "executed"
    assert result.result == {"ok": True, "sent": True}


@pytest.mark.asyncio
async def test_approve_marks_failed_on_error_result(queue):
    executor = AsyncMock(return_value={"error": "rate limited"})
    queue.set_executor(executor)
    item = queue.queue("send_email", {"to": "a", "body": "b"})
    result = await queue.approve(item.id)
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_approve_marks_failed_on_exception(queue):
    async def bomb(_n, _a):
        raise RuntimeError("boom")
    queue.set_executor(bomb)
    item = queue.queue("send_email", {"to": "a", "body": "b"})
    result = await queue.approve(item.id)
    assert result.status == "failed"
    assert "boom" in result.result["error"]


@pytest.mark.asyncio
async def test_approve_without_executor_fails_gracefully(queue):
    item = queue.queue("send_email", {"to": "a", "body": "b"})
    result = await queue.approve(item.id)
    assert result.status == "failed"
    assert "executor" in result.result["error"]


@pytest.mark.asyncio
async def test_approve_unknown_returns_none(queue):
    queue.set_executor(AsyncMock(return_value={}))
    assert await queue.approve("nope") is None
