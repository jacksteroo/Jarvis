---
name: end_of_day
description: Close out the day — resolve commitments, capture what happened, and tee up tomorrow's first action
version: 1
---

## Workflow

1. Open with the date and a one-line read on the day from the calendar (use `get_calendar_events_range` for today). "Heavy day — six meetings" or "Two calls and focus time."

2. **What got done.** Ask the user briefly: "What's worth capturing from today?" — but don't wait if they don't answer; pull signals yourself:
   - `search_memory` query `"today decided shipped completed met with"` (limit 8) — anything memory captured during the day.
   - From today's calendar, list the meetings that happened. The user can tell you which mattered.

3. **Resolve commitments.** Call `search_memory` query `"commitment promised will follow up"` (limit 10). For each open item:
   - If today's events or messages show it was clearly handled (e.g., "sent the intro to Sarah this afternoon"), call `mark_commitment_complete` to close it.
   - If still open, list it with one line — these become tomorrow's hot list.

4. **Inbox close-out.** Call `get_overdue_responses` and `get_email_unread_counts`. If anything is still flagged after a full work day, call it out — not to alarm, just so the user can decide whether to handle it tonight or tomorrow morning.

5. **Capture decisions worth remembering.** If the user mentioned anything that sounds like a decision, preference shift, or new fact about a person/project, call `save_memory` to file it. Be conservative — only capture things that will matter later. Format: "DECISION: [what], because [why], on [date]."

6. **Tomorrow's first action.** Look at tomorrow's calendar (`get_upcoming_events` with `days: 1`). Identify the single most important thing for the morning — usually the first meeting that needs prep, or an overdue commitment that should be handled before anything else. Surface it as: "First thing tomorrow: [action]."

7. Close with a clean handoff line: "Day closed. Tomorrow opens with [first thing]. Anything else you want me to capture before you log off?"

Don't fabricate. If a commitment's status is unclear, leave it open — `mark_commitment_complete` is irreversible from the user's perspective.
