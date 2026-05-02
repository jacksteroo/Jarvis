"""Unit tests for PepperCore.reaction_to_signal — the mapping from
Telegram message-reaction emojis to the migration's success_signal
buckets. Pure function; no DB / network required."""

from agent.core import PepperCore


def test_thumbs_up_maps_to_confirmed():
    assert PepperCore.reaction_to_signal(["👍"]) == "confirmed"


def test_thumbs_down_maps_to_abandoned():
    assert PepperCore.reaction_to_signal(["👎"]) == "abandoned"


def test_negative_dominates_when_mixed():
    # User reacted with both 👍 and 👎 — the correction wins because it's
    # a stronger signal than the polite-positive reflex.
    assert PepperCore.reaction_to_signal(["👍", "👎"]) == "abandoned"


def test_unmapped_emoji_returns_none():
    # 🤔 is intentionally not mapped — we don't want to corrupt
    # success_signal with ambiguous reactions.
    assert PepperCore.reaction_to_signal(["🤔"]) is None


def test_empty_input_returns_none():
    assert PepperCore.reaction_to_signal([]) is None


def test_first_positive_wins():
    assert PepperCore.reaction_to_signal(["❤️", "🔥"]) == "confirmed"


def test_unmapped_plus_positive_yields_confirmed():
    assert PepperCore.reaction_to_signal(["🤔", "👍"]) == "confirmed"
