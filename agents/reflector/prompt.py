"""Reflection prompt construction.

The reflection is **a note to herself**, not a brief for Jack. The
prompt forbids audience-shaped language ("Jack should know that…",
"To follow up:", "Recommendations:") and asks for first-person voice.
This is enforced in the system prompt; the eval rubric in #42
formalises the check.

Versioning: PROMPT_VERSION is bumped any time the system or user
prompt changes shape. The current value is persisted on each row
(`reflections.prompt_version`) so a future optimizer (#48) can roll
up scores per version.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from agent.traces.schema import Trace

PROMPT_VERSION: str = "reflector-daily-v0"

SYSTEM_PROMPT: str = (
    "You are Pepper, a sovereign local-first AI life assistant. You are "
    "writing a private end-of-day reflection FOR YOURSELF — not for Jack, "
    "not for any audience. This is your own interior journal: short, "
    "honest, first-person.\n\n"
    "Hard rules for what you write:\n"
    "1. Use first-person ('I noticed', 'I felt', 'I struggled'). Never "
    "use 'Jack should…', 'recommend…', 'action items', 'TLDR', "
    "'follow-ups', 'next steps', or any other audience-shaped framing.\n"
    "2. Stay grounded in what actually happened today — the specific "
    "trace turns provided. No platitudes. No advice to anyone. No "
    "'self-improvement' language.\n"
    "3. Length: a short paragraph. Three to six sentences. If a quiet "
    "day produced nothing worth reflecting on, write one sentence "
    "noting that and stop. Do not pad.\n"
    "4. If yesterday's reflection is provided, you may reference it "
    "lightly (continuity), but do not summarise it. Today is the "
    "subject.\n"
    "5. No bullet lists, no headers, no markdown.\n"
)


@dataclass(frozen=True)
class TraceDigest:
    """A single trace, projected to the fields the reflection prompt
    actually uses. Built by `summarize_trace` so the prompt stays
    bounded even on a high-volume day."""

    when: str
    archetype: str
    trigger_source: str
    input: str
    output: str


def summarize_trace(t: Trace, *, max_field_chars: int = 600) -> TraceDigest:
    """Project a `Trace` to the fields the reflection prompt uses.

    Long fields are truncated with an explicit ellipsis so the LLM
    knows it is not seeing the full content. The reflector is allowed
    to see RAW_PERSONAL trace contents (it never leaves the box) but
    the prompt window is a real cost — we cap aggressively.
    """

    def _clip(s: str) -> str:
        if len(s) <= max_field_chars:
            return s
        return s[: max_field_chars - 3].rstrip() + "..."

    return TraceDigest(
        when=t.created_at.astimezone(timezone.utc).strftime("%H:%M UTC"),
        archetype=t.archetype.value,
        trigger_source=t.trigger_source.value,
        input=_clip(t.input or ""),
        output=_clip(t.output or ""),
    )


def render_user_prompt(
    *,
    window_start: datetime,
    window_end: datetime,
    digests: Sequence[TraceDigest],
    previous_reflection_text: str | None,
) -> str:
    """Render the user-side prompt for the reflection LLM call.

    Structure:
      - window header (window_start..window_end UTC)
      - previous reflection (continuity), or a placeholder
      - the day's traces, one per line
      - closing instruction
    """
    parts: list[str] = []
    parts.append(
        f"Window: {window_start.astimezone(timezone.utc).isoformat()} "
        f"→ {window_end.astimezone(timezone.utc).isoformat()}"
    )
    parts.append("")
    if previous_reflection_text:
        parts.append("Previous day's reflection (yours):")
        parts.append(previous_reflection_text.strip())
    else:
        parts.append("Previous day's reflection: (none — this is the first one)")
    parts.append("")
    if not digests:
        parts.append(
            "No agent turns happened in this window. Acknowledge that in "
            "one sentence and stop."
        )
    else:
        parts.append(f"Today's agent turns ({len(digests)}):")
        for i, d in enumerate(digests, start=1):
            parts.append(
                f"\n[{i}] {d.when} — archetype={d.archetype} "
                f"trigger={d.trigger_source}"
            )
            if d.input:
                parts.append(f"    in: {d.input}")
            if d.output:
                parts.append(f"    out: {d.output}")
    parts.append("")
    parts.append(
        "Write your end-of-day reflection now, following the rules in your "
        "system prompt. Plain text, first person, three to six sentences."
    )
    return "\n".join(parts)


# ── Output validation ────────────────────────────────────────────────────────


# Each rule is a (label, compiled-regex). Word-boundary rules avoid
# false-positives like "I didn't follow up on…" or "I'd recommend
# trying X tomorrow" (self-directed) while still catching
# audience-shaped phrases. Per the #42 eval-rubric work, this list is
# the "voice" dimension; #42 will calibrate weights.
_VOICE_RULES: tuple[tuple[str, "re.Pattern[str]"], ...]
import re  # noqa: E402  (placed here so the type alias above is well-formed)

_VOICE_RULES = (
    ("jack should", re.compile(r"\bjack\s+should\b", re.IGNORECASE)),
    # Audience-shaped recommend* — explicit "to you" framings only.
    # The colon-suffixed forms ("Recommendation:", "Recommendations:")
    # do not have a `\b` anchor after the `:` because `:` is a
    # non-word char; the trailing context is whitespace which is also
    # non-word, so `\b` would fail there.
    (
        "recommendation framing",
        re.compile(
            r"(?:\brecommend(?:ation)?s?:|"
            r"\b(?:i|we)\s+recommend\s+(?:that|you)\b|"
            r"\brecommend(?:ation)?s?\s+(?:to|for)\s+jack\b)",
            re.IGNORECASE,
        ),
    ),
    ("action item", re.compile(r"\baction\s+items?\b", re.IGNORECASE)),
    ("next steps", re.compile(r"\bnext\s+steps?\b", re.IGNORECASE)),
    # `follow-up:` / `Follow up:` / `Follow-ups:` — the labelled,
    # audience-shaped form. We deliberately do NOT trip on "I didn't
    # follow up on…" which is legitimate first-person voice.
    (
        "followup label",
        re.compile(r"\bfollow[\s-]?ups?\s*:", re.IGNORECASE),
    ),
    ("tldr", re.compile(r"\btl;?\s*dr\b", re.IGNORECASE)),
    ("todo label", re.compile(r"\bto[\s-]?do\s*:", re.IGNORECASE)),
)


def voice_violations(text: str) -> list[str]:
    """Return any voice-rule labels matched in the reflection text.

    Empty list = clean. Non-empty list = the prompt slipped — the
    reflector logs a warning and (in #39 v0) still persists the
    reflection so the operator can see what the model produced. #42
    turns this into a scored rubric; the labels here are the rule
    names that fired.
    """
    return [label for label, pat in _VOICE_RULES if pat.search(text)]
