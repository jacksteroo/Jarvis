"""
Phase 2 — Hardened slot extractors for the semantic router.

Pure, deterministic functions that extract structured slots from a user
message: time scope, target sources, entity targets (people), and an
optional filesystem path.

Ported from `agent/query_router.py` and hardened for production:

- Strict input typing — non-string input raises TypeError.
- Bounded input length — queries longer than `MAX_QUERY_CHARS` are
  truncated before regex matching to keep extraction O(n).
- Deterministic empty-input contract — empty / whitespace-only input
  returns the documented empty defaults, never raises.
- Unicode-safe — input is normalised before pattern matching.
- Stateless — every call is independent; callers may share or cache.

These helpers are consumed by `SemanticRouter` (Phase 2+) for slot
extraction. Intent classification itself is the embedding classifier's
job; slots stay explicit because their patterns are stable and cheap.

The legacy `QueryRouter` keeps its own copies of these helpers (private
`_*` names) until Phase 3 cutover archives it; the regex tables here are
copied verbatim from there so behaviour matches during shadow mode.
"""
from __future__ import annotations

import re

from agent.local_filesystem_tools import extract_path_from_text
from agent.query_intents import (
    CALENDAR_QUERY_TERMS,
    EMAIL_QUERY_TERMS,
    IMESSAGE_QUERY_TERMS,
    NON_EMAIL_CHANNEL_TERMS,
    SLACK_QUERY_TERMS,
    WHATSAPP_QUERY_TERMS,
    contains_any,
    normalize_user_text,
)

# ── Public constants ──────────────────────────────────────────────────────────

#: Maximum input length the extractors will consider. Longer inputs are
#: truncated to this many leading characters before extraction. Pepper-side
#: prompts are far below this; the cap is purely a runaway-input guard.
MAX_QUERY_CHARS: int = 2000

# ── Time scope patterns ───────────────────────────────────────────────────────

_TIME_SCOPE_TABLE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("overnight", "last night"), "overnight"),
    (("this morning",), "today"),
    (("today", "tonight"), "today"),
    (("yesterday",), "yesterday"),
    (("this week", "past week", "last week"), "this_week"),
    (("past few days", "last few days", "over the last few days", "couple of days"), "past_few_days"),
    (("last hour", "past hour", "just now", "recently"), "last_hour"),
)

_SINCE_DOW_RE = re.compile(
    r"\bsince\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_LAST_N_RE = re.compile(
    r"\b(?:in|over|for)\s+the\s+last\s+(\d+)\s+(hour|hours|day|days|week|weeks)\b",
    re.IGNORECASE,
)
_BEFORE_EVENT_RE = re.compile(
    r"\bbefore\s+my\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|meeting|call|appointment|next\s+\w+)\b",
    re.IGNORECASE,
)

# ── Entity (person) patterns ──────────────────────────────────────────────────

_PERSON_DID_RE = re.compile(
    r"\b(did|has|have)\s+(?:my\s+)?(\w+(?:\s+\w+)?)\s+"
    r"(send|sent|message[ds]?|email[ds]?|text(?:ed)?|call(?:ed)?|reach(?:ed)?|reply|replied|respond(?:ed)?|write|written|get|gotten|hear|heard)",
    re.IGNORECASE,
)

_KINSHIP_PAT = (
    r"(?:mom|dad|mother|father|sister|brother|wife|husband"
    r"|son|daughter|grandma|grandpa|grandmother|grandfather"
    r"|aunt|uncle|boss|manager|partner)"
)

_FROM_PERSON_RE = re.compile(
    r"\b(from|by)\s+(?:my\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_FROM_KINSHIP_RE = re.compile(
    r"\b(?:from|by)\s+(?:my\s+)?" + _KINSHIP_PAT + r"\b",
    re.IGNORECASE,
)
_ABOUT_PERSON_RE = re.compile(
    r"\b(about|regarding|hear from|word from|heard from)\s+(?:my\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b"
)
_ABOUT_KINSHIP_RE = re.compile(
    r"\b(?:about|regarding|hear from|word from|heard from)\s+(?:my\s+)?" + _KINSHIP_PAT + r"\b",
    re.IGNORECASE,
)
_POSSESSIVE_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+)'s\s+"
    r"(email|emails|message|messages|thread|threads|reply|text|texts|note|notes|latest|recent|last)\b"
)
_POSSESSIVE_KINSHIP_RE = re.compile(
    r"\b(?:my\s+)?" + _KINSHIP_PAT + r"['’]s\s+"
    r"(email|emails|message|messages|thread|threads|reply|text|texts|note|notes|latest|recent|last)\b",
    re.IGNORECASE,
)

_SOURCE_NAME_BLOCKLIST = frozenset({
    "slack", "gmail", "yahoo", "whatsapp", "imessage", "telegram", "email",
    "sms", "calendar", "google", "facebook", "instagram", "twitter", "linkedin",
    "notion", "github", "jira", "linear",
})

_STOP_WORDS = frozenset({
    "i", "me", "my", "you", "your", "we", "our", "they", "it", "the",
    "a", "an", "this", "that", "these", "those",
    "sent", "send", "text", "texted", "replied", "reply", "called", "messaged",
    "emailed", "heard", "hear", "gotten", "wrote", "write",
})

# ── Target source override ────────────────────────────────────────────────────

_EXPLICIT_EMAIL_TERMS: tuple[str, ...] = (
    "email", "emails", "gmail", "yahoo", "inbox", "unread",
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _coerce_text(text: str) -> str:
    """Validate input and truncate to ``MAX_QUERY_CHARS``.

    Raises ``TypeError`` for non-string input. Empty / whitespace-only input
    is returned unchanged so callers get the documented empty defaults.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"slot extractor expected str, got {type(text).__name__}"
        )
    if len(text) > MAX_QUERY_CHARS:
        return text[:MAX_QUERY_CHARS]
    return text


# ── Public API ────────────────────────────────────────────────────────────────

def extract_time_scope(text: str) -> str:
    """Return a stable time-scope token for the query.

    Tokens:
      - ``"default"`` — no time qualifier detected
      - ``"overnight" | "today" | "yesterday" | "this_week"``
      - ``"past_few_days" | "last_hour"``
      - ``"since_<dow>"`` — e.g. ``"since_thursday"``
      - ``"last_<n>_<unit>"`` — e.g. ``"last_3_day"``
      - ``"before_<anchor>"`` — e.g. ``"before_3pm"``, ``"before_meeting"``
    """
    text = _coerce_text(text)
    if not text.strip():
        return "default"

    normalized = normalize_user_text(text)
    for phrases, scope in _TIME_SCOPE_TABLE:
        if any(p in normalized for p in phrases):
            return scope

    m = _SINCE_DOW_RE.search(text)
    if m:
        return f"since_{m.group(1).lower()}"

    m = _LAST_N_RE.search(text)
    if m:
        n = m.group(1)
        unit = m.group(2).lower().rstrip("s")
        return f"last_{n}_{unit}"

    m = _BEFORE_EVENT_RE.search(text)
    if m:
        anchor = m.group(1).lower().replace(" ", "_")
        return f"before_{anchor}"

    return "default"


def extract_entity_targets(text: str) -> list[str]:
    """Return a deduplicated, ordered list of likely person/contact entities.

    Drops known data-source names (Slack, Gmail, …) and conversational stop
    words. Title-case patterns stay case-sensitive so common words like
    "the" or "any" do not slip through.
    """
    text = _coerce_text(text)
    if not text.strip():
        return []

    targets: list[str] = []

    def _add(name: str) -> None:
        n = name.strip()
        if n and n.lower() not in _STOP_WORDS and n.lower() not in _SOURCE_NAME_BLOCKLIST:
            targets.append(n)

    for m in _PERSON_DID_RE.finditer(text):
        _add(m.group(2))

    for m in _FROM_PERSON_RE.finditer(text):
        _add(m.group(2))

    for m in _FROM_KINSHIP_RE.finditer(text):
        word = m.group(0).split()[-1]
        _add(word)

    for m in _ABOUT_PERSON_RE.finditer(text):
        _add(m.group(2))

    for m in _ABOUT_KINSHIP_RE.finditer(text):
        word = m.group(0).split()[-1]
        _add(word)

    for m in _POSSESSIVE_NAME_RE.finditer(text):
        _add(m.group(1))

    for m in _POSSESSIVE_KINSHIP_RE.finditer(text):
        for tok in m.group(0).split():
            if "'" in tok or "’" in tok:
                _add(tok.split("'")[0].split("’")[0])
                break

    return list(dict.fromkeys(targets))


def extract_target_sources(text: str) -> list[str]:
    """Return the data sources implied by the query.

    Possible values: ``"email"``, ``"imessage"``, ``"whatsapp"``, ``"slack"``,
    ``"calendar"``. Order matches detection order; empty list when the query
    does not mention any source.

    Email-suppression rule: when a non-email channel is named alongside
    broad "mail/messages" terms, email is suppressed unless the query
    explicitly says ``email``, ``gmail``, ``yahoo``, ``inbox``, or ``unread``.
    """
    text = _coerce_text(text)
    if not text.strip():
        return []

    normalized = normalize_user_text(text)
    sources: list[str] = []

    if contains_any(normalized, EMAIL_QUERY_TERMS):
        has_non_email = contains_any(normalized, NON_EMAIL_CHANNEL_TERMS)
        has_explicit_email = contains_any(normalized, _EXPLICIT_EMAIL_TERMS)
        if has_explicit_email or not has_non_email:
            sources.append("email")

    if contains_any(normalized, IMESSAGE_QUERY_TERMS):
        sources.append("imessage")

    if contains_any(normalized, WHATSAPP_QUERY_TERMS):
        sources.append("whatsapp")

    if contains_any(normalized, SLACK_QUERY_TERMS):
        sources.append("slack")

    if contains_any(normalized, CALENDAR_QUERY_TERMS):
        sources.append("calendar")

    return sources


def extract_filesystem_path(text: str) -> str | None:
    """Return a filesystem path mentioned in the query, or ``None``.

    Thin wrapper over ``local_filesystem_tools.extract_path_from_text`` that
    enforces the same input-validation contract as the other extractors.
    """
    text = _coerce_text(text)
    if not text.strip():
        return None
    return extract_path_from_text(text)


__all__ = [
    "MAX_QUERY_CHARS",
    "extract_time_scope",
    "extract_entity_targets",
    "extract_target_sources",
    "extract_filesystem_path",
]
