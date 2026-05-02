---
name: weekly_review
description: Weekly review — what happened, what's still open, and what needs attention next week
version: 2
---

## Workflow

1. Open with the current week label (e.g., "Week of April 14") and a one-sentence shape-of-the-week framing — calm, busy, heavy on meetings, heavy on travel, etc. Derive this honestly from step 2 data.

2. **What actually happened this week.** Call `get_calendar_events_range` for the past 7 days — this is the ground truth for what the user did. Pair it with `search_memory` (query: `"shipped completed decided launched met with"`, limit 10) for things memory captured beyond the calendar. Summarize in 2–4 bullets: decisions made, things shipped, people met. Use real names and projects.

3. **Last week comparison (optional but high-value).** Call `get_calendar_events_range` for days 8–14 ago (the prior week). One sentence on the shift: "Lighter than last week," "Same cadence," "Three more meetings than last week, mostly with the new vendor." Skip if data is too thin to compare honestly.

4. **What's still open.** Call `search_memory` with query `"commitment promised open loop unresolved waiting"` (limit 10). Skip anything starting with `[RESOLVED]`. Group:
   - **Overdue**: should have happened this week
   - **Carry-forward**: still valid, no urgency yet

5. **Pressure for next week.** Call `get_upcoming_events` with `days: 7` AND `get_slack_deadlines` to surface deadline-adjacent events and Slack-mentioned deadlines. Highlight:
   - Reviews, presentations, travel, board/investor touchpoints
   - Conflicts or back-to-back blocks worth flagging
   - Anything with prep work that isn't done yet

6. **Relationship maintenance.** Call `get_comms_health_summary` with `quiet_days: 21`. If a personally important contact (not vendor/list) has gone quiet, flag one — this is something a good EA notices, not just calendars.

## Synthesis

- **Section order**: framing → what happened → comparison → still open → next week pressure → relationship signal.
- End with **1–2 forward-looking priorities**:
  - The single most important thing for next week
  - The risk or open loop that needs resolution before Monday

Keep it tight but don't artificially cap — a busy week deserves more bullets than a quiet one. Be direct. No filler. If a section has nothing real, omit it.
