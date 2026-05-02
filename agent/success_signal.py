"""Heuristic ``success_signal`` derivation for the routing_events table.

Phase 1 Task 5 of docs/SEMANTIC_ROUTER_MIGRATION.md. Each turn's quality is
inferred (no user labelling) by comparing it to the next turn in the same
session. The four states match the spec exactly:

- ``re_asked``  — follow-up within 30 min, keyword overlap ≥ 50%
                  (signals the router missed; user repeated themselves)
- ``confirmed`` — follow-up within 30 min, keyword overlap < 30%
                  (user moved on cleanly; implicit success)
- ``abandoned`` — no follow-up within 60 min AND response < 50 chars OR
                  contained refusal/error markers
- ``unknown``   — none of the above; row decided after the 60-min window
                  closed without a clear signal

Pure functions live here so they can be unit-tested in isolation. The
orchestration that walks routing_events and writes the signal is in
``PepperCore._process_success_signals``; it runs as a background task off
the chat-response critical path so latency is unaffected.

Privacy: all inputs are already on-disk in the JSONL turn log; nothing
new leaves the machine.
"""

from __future__ import annotations

import re
from typing import Optional

# --- thresholds (per spec — tunable later via shadow data) -----------------
RE_ASK_WINDOW_MIN = 30
ABANDON_WINDOW_MIN = 60
RE_ASK_OVERLAP_THRESHOLD = 0.50
CONFIRM_OVERLAP_THRESHOLD = 0.30
SHORT_RESPONSE_CHARS = 50

# Refusal/error markers, lowercased substring match. Pulled from the existing
# error_classifier vocabulary plus common LLM refusal phrasings observed in
# logs/chat_turns/*.jsonl.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "i can't",
    "i cannot",
    "i'm unable",
    "i am unable",
    "unable to",
    "couldn't",
    "could not",
    "no result",
    "no results",
    "not available",
    "no data",
    "error:",
    "failed to",
    "sorry",
    "apolog",  # apologize / apologise
    "not enough information",
    "n/a",
)

# Stopwords kept tiny on purpose — over-pruning destroys overlap signal on
# short queries like "what about tomorrow?" vs "what about next week?". This
# list mirrors the bare-minimum English function words; everything else
# carries enough semantic weight to count as a keyword.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "do", "does", "did", "have", "has", "had", "of", "in", "on", "at",
        "to", "for", "with", "by", "from", "about", "as", "and", "or", "but",
        "if", "then", "than", "so", "this", "that", "these", "those", "it",
        "its", "i", "me", "my", "we", "us", "you", "your", "he", "she",
        "they", "them", "what", "which", "who", "whom", "whose", "how",
        "when", "where", "why", "can", "could", "would", "should", "will",
        "shall", "may", "might", "must",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercased alphanumeric tokens, stopwords removed, length ≥ 2."""
    if not text:
        return set()
    return {
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if len(tok) >= 2 and tok not in _STOPWORDS
    }


def keyword_overlap(a: str, b: str) -> float:
    """Jaccard similarity over content tokens; 0.0 when either side is empty.

    Symmetric in (a, b). Stopwords are excluded so two semantically-empty
    queries like "what is it" and "what about that" don't appear identical.
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


def has_refusal_or_error_markers(text: str) -> bool:
    """True if the response text contains any known refusal or error phrasing."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def derive_followup_signal(
    prior_query: str,
    current_query: str,
    minutes_between: float,
) -> Optional[str]:
    """Return ``"re_asked"`` / ``"confirmed"`` / ``None`` from a follow-up turn.

    Only meaningful when ``minutes_between <= RE_ASK_WINDOW_MIN``; outside
    that window we return ``None`` so the caller can fall back to the
    abandoned-or-unknown path.
    """
    if minutes_between > RE_ASK_WINDOW_MIN:
        return None
    overlap = keyword_overlap(prior_query, current_query)
    if overlap >= RE_ASK_OVERLAP_THRESHOLD:
        return "re_asked"
    if overlap < CONFIRM_OVERLAP_THRESHOLD:
        return "confirmed"
    return None  # 30%–50%: ambiguous, leave NULL


def derive_terminal_signal(
    response_text: Optional[str],
    minutes_since: float,
) -> Optional[str]:
    """Return ``"abandoned"`` / ``"unknown"`` / ``None`` for a row with no follow-up.

    Only fires after the 60-min abandonment window has closed; before that
    we cannot tell if a follow-up is still coming.
    """
    if minutes_since <= ABANDON_WINDOW_MIN:
        return None
    if response_text is None:
        return None  # need the response to decide; let a later sweep retry
    short = len(response_text) < SHORT_RESPONSE_CHARS
    if short or has_refusal_or_error_markers(response_text):
        return "abandoned"
    return "unknown"


__all__ = [
    "RE_ASK_WINDOW_MIN",
    "ABANDON_WINDOW_MIN",
    "RE_ASK_OVERLAP_THRESHOLD",
    "CONFIRM_OVERLAP_THRESHOLD",
    "SHORT_RESPONSE_CHARS",
    "keyword_overlap",
    "has_refusal_or_error_markers",
    "derive_followup_signal",
    "derive_terminal_signal",
]
