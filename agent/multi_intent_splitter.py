"""Multi-intent splitter — Phase 2 utility for the semantic router.

Splits a user message into independent intent fragments so the semantic
classifier can score each one separately. This is a pure, deterministic
splitter — no model calls, no DB access, no logging side effects.

Boundary tokens (per migration plan §"Multi-intent handling")
-------------------------------------------------------------

Existing (ported from ``query_router.route_multi``):
    " and ", " also ", "; ", "?"

Added in this iteration:
    " plus ", " then ", " & ", " as well as ", "what about" prefixes,
    em-dash / en-dash variants, " - " between clauses, line breaks.

Negative split guards
---------------------

- Split tokens that fall inside double-quoted spans (ASCII or curly) are
  ignored, so quoted phrases stay intact.
- Possessives are unaffected by the boundary patterns: every word-shaped
  token requires explicit whitespace separators (e.g. ``\\s+and\\s+``),
  so ``"Sara's"`` and ``"father's"`` are never broken.
- Fragments collapse on whitespace; empty fragments are dropped.
- If fewer than two non-empty fragments survive, the original (stripped)
  query is returned as a singleton.

Out of scope here
-----------------

This module does **not** classify, rank, or filter fragments by intent
quality. The original ``QueryRouter.route_multi`` rejected splits where
no fragment carried a target source or entity target — that policy is a
*routing* concern and stays with the future ``SemanticRouter`` facade,
which can decide per-fragment whether to defer or fold back into the
parent query based on classifier confidence.
"""
from __future__ import annotations

import re

#: Maximum input length the splitter considers. Longer inputs are
#: truncated up-front so the regex pass stays bounded. Pepper's runtime
#: queries are far below this; the cap is a runaway-input guard.
MAX_QUERY_CHARS: int = 4000

# Boundary tokens, longest phrases first so e.g. "as well as" wins over
# the bare " and "/" as " inside it.
_SPLIT_TOKEN_RE = re.compile(
    r"(?:"
    r"\s+as\s+well\s+as\s+"
    r"|\s*,\s*what\s+about\s+"
    r"|\s+what\s+about\s+"
    r"|\s+plus\s+"
    r"|\s+also\s+"
    r"|\s+then\s+"
    r"|\s+and\s+"
    r"|\s*&\s*"
    r"|\s*[—–]+\s*"
    r"|\s+-\s+"
    r"|\s*;\s*"
    r"|\?+(?=\s)"
    r"|\n+"
    r")",
    re.IGNORECASE,
)

# Quoted spans (ASCII straight, curly double, curly single). Split tokens
# inside these are ignored.
_QUOTE_SPAN_RE = re.compile(
    r'"[^"]*"'
    r"|“[^”]*”"
    r"|‘[^’]*’"
)


def split_multi_intent(query: str) -> list[str]:
    """Split ``query`` into a list of intent fragments.

    Returns
    -------
    list[str]
        - ``[]`` for empty / whitespace-only input.
        - ``[query.strip()]`` when no boundary token applies, or every
          candidate boundary lies inside a quoted span, or the split
          yields fewer than two non-empty fragments.
        - Otherwise, the ordered list of stripped fragments.

    Raises
    ------
    TypeError
        If ``query`` is not a string.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    text = query.strip()
    if not text:
        return []
    if len(text) > MAX_QUERY_CHARS:
        text = text[:MAX_QUERY_CHARS].rstrip()

    quote_spans = [m.span() for m in _QUOTE_SPAN_RE.finditer(text)]

    def in_quote(start: int, end: int) -> bool:
        for qs, qe in quote_spans:
            if start >= qs and end <= qe:
                return True
        return False

    fragments: list[str] = []
    last = 0
    for match in _SPLIT_TOKEN_RE.finditer(text):
        if in_quote(match.start(), match.end()):
            continue
        chunk = text[last:match.start()].strip()
        if chunk:
            fragments.append(chunk)
        last = match.end()
    tail = text[last:].strip()
    if tail:
        fragments.append(tail)

    if len(fragments) < 2:
        return [text]
    return fragments
