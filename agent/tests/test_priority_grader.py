"""Tests for Phase 6.7 — PriorityGrader."""
from __future__ import annotations

from agent.priority_grader import PriorityGrader, extract_vips_from_life_context


def test_newsletter_is_ignore():
    g = PriorityGrader()
    assert g.grade({"from": "newsletter@techcrunch.com", "subject": "Weekly digest"}) == "ignore"


def test_noreply_is_ignore():
    g = PriorityGrader()
    assert g.grade({"from": "noreply@github.com", "subject": "Build passed"}) == "ignore"


def test_urgent_keyword_is_urgent():
    g = PriorityGrader()
    tag = g.grade({"from": "boss@acme.com", "subject": "ASAP — need your input"})
    assert tag == "urgent"


def test_plain_work_email_is_defer():
    g = PriorityGrader()
    assert g.grade({"from": "random@example.com", "subject": "heads up"}) == "defer"


def test_vip_without_urgency_is_important():
    g = PriorityGrader(vips=["sarah"])
    assert g.grade({"from": "sarah@home.com", "subject": "dinner?"}) == "important"


def test_vip_with_urgency_is_urgent():
    g = PriorityGrader(vips=["sarah"])
    assert g.grade({"from": "sarah@home.com", "subject": "asap: flight delayed"}) == "urgent"


def test_vip_noise_still_demoted_to_ignore_when_not_matched():
    # Noise short-circuit beats non-VIP, but VIP beats noise
    g = PriorityGrader(vips=["sarah"])
    # sender contains both noise pattern and VIP name
    tag = g.grade({"from": "sarah@noreply.com", "subject": "asap"})
    assert tag == "urgent"  # VIP match wins


def test_quiet_contact_is_important():
    g = PriorityGrader(quiet_contacts=["dave"])
    assert g.grade({"from": "dave@olddomain.com", "subject": "long time"}) == "important"


def test_review_request_signals_important():
    g = PriorityGrader()
    assert g.grade({"from": "colleague@acme.com", "subject": "please review the doc"}) == "important"


def test_event_soon_bumps_vip_to_urgent():
    g = PriorityGrader(vips=["sarah"], upcoming_event_soon=True)
    assert g.grade({"from": "sarah@home.com", "subject": "running late"}) == "urgent"


def test_batch_sorts_by_priority():
    g = PriorityGrader(vips=["sarah"])
    items = [
        {"from": "newsletter@x.com", "subject": "digest"},
        {"from": "boss@acme.com", "subject": "ASAP: fix staging"},
        {"from": "sarah@home.com", "subject": "dinner"},
        {"from": "colleague@acme.com", "subject": "fyi"},
    ]
    result = g.grade_batch(items)
    tags = [t for _, t in result]
    assert tags[0] == "urgent"
    assert tags[1] == "important"
    assert tags[-1] == "ignore"


def test_extract_vips_from_life_context_bullet_list():
    text = """
## Important People

- Sarah Smith: wife
- Mike — best friend
* Dr. Patel: cardiologist

## Other Section
- Random
"""
    vips = extract_vips_from_life_context(text)
    assert "sarah smith" in vips
    assert "mike" in vips
    assert "dr. patel" in vips
    assert "random" not in vips


def test_extract_vips_empty_when_no_section():
    assert extract_vips_from_life_context("# Notes\nJust notes") == []


def test_extract_vips_empty_when_blank():
    assert extract_vips_from_life_context("") == []


# ── Attention-flow integration (iMessage / WhatsApp) ──────────────────────────


def _make_pepper_with_vips(vips):
    """Build a minimal PepperCore whose _make_grader returns a seeded grader."""
    from unittest.mock import patch, MagicMock
    from agent.core import PepperCore
    config = MagicMock()
    config.LIFE_CONTEXT_PATH = "data/life_context.md"
    config.OWNER_NAME = "Test"
    config.TIMEZONE = "UTC"
    config.DEFAULT_LOCAL_MODEL = "x"
    with patch("agent.core.ModelClient"), \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter"), \
         patch("agent.core.build_system_prompt", return_value="system"):
        MockMem.return_value._working = []
        pepper = PepperCore(config)
    pepper._make_grader = lambda: PriorityGrader(vips=vips)
    return pepper


def test_attention_flow_tags_vip_conversation_as_important():
    pepper = _make_pepper_with_vips(vips=["sarah"])
    result = {
        "items": [
            {
                "chat_id": "1",
                "display_name": "Sarah",
                "sender": "Sarah",
                "unread_count": 1,
                "text": "hey",
                "timestamp": "2026-04-17T10:00:00",
                "why": "unread",
            },
            {
                "chat_id": "2",
                "display_name": "Random",
                "sender": "Random",
                "unread_count": 0,
                "text": "hi",
                "timestamp": "2026-04-17T09:00:00",
                "why": "recent",
            },
        ],
        "summary": "original summary",
    }
    out = pepper._apply_priority_tags_to_attention(result, source_label="iMessage")
    # VIP line gets an [important] tag and comes first
    lines = out.split("\n")
    assert any("[important]" in ln and "Sarah" in ln for ln in lines)
    # Non-VIP is not tagged
    assert not any("[important]" in ln and "Random" in ln for ln in lines)


def test_attention_flow_tags_urgent_keyword():
    pepper = _make_pepper_with_vips(vips=[])
    result = {
        "items": [
            {
                "chat_id": "1",
                "display_name": "Colleague",
                "sender": "Colleague",
                "unread_count": 1,
                "text": "ASAP — need this fixed",
                "why": "unread",
            },
        ],
        "summary": "fallback",
    }
    out = pepper._apply_priority_tags_to_attention(result, source_label="WhatsApp")
    assert "[urgent]" in out
    assert "WhatsApp" in out


def test_attention_flow_falls_back_when_no_items():
    pepper = _make_pepper_with_vips(vips=[])
    result = {"summary": "nothing to see"}
    out = pepper._apply_priority_tags_to_attention(result, source_label="iMessage")
    assert out == "nothing to see"
