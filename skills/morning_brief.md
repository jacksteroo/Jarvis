---
name: morning_brief
description: Comprehensive morning brief — calendar, inbox snapshot, open loops, and pending commitments
version: 2
---

## Workflow

1. Open with today's date in a natural one-line greeting — no filler.

2. Call `get_upcoming_events` with `days: 1` to surface today's calendar.
   - List each event on its own line: time — title — location (if any) — attendees count if useful.
   - Flag back-to-back blocks (no gap between events) — those are buffer-free zones the user should know about up front.
   - If no events: "Clear calendar today."

3. **Travel time for the first meeting.** If the first event of the day has a physical location (not Zoom/Meet/Teams/phone) and starts within the next 4 hours, call `get_driving_time` from "home" (or the user's last known location if surfaced in life context) to that location. Surface the result inline with the event: "9am — Coffee w/ Jordan @ Sightglass — 18 min drive, leave by 8:42." Skip silently if the location is virtual or you can't resolve coordinates.

4. Call `get_overdue_responses` to surface unread iMessage/WhatsApp threads waiting more than 48 hours. List up to 3 — these are the most actionable inbox items.

5. Call `search_memory` with query `"open loop pending waiting unresolved"` (limit 5). Surface items that still need attention — skip anything obviously stale or marked `[RESOLVED]`.

6. Call `search_memory` with query `"commitment promised will follow up"` (limit 5). Show items that do NOT start with `[RESOLVED]`. Skip the section entirely if all are resolved or none exist.

7. Call `get_email_unread_counts` for an inbox snapshot. One line per account, skipping any with 0 unread. If totals are huge (>50), say so as a single number rather than enumerating.

8. Call `get_comms_health_summary` with `quiet_days: 14` for relationship signals. Surface at most 2 — pick the most actionable (e.g., a close contact gone quiet, not a vendor). Skip if no signals.

## Synthesis

- **Lead with the single most important thing**: an imminent meeting needing prep, an overdue commitment, the leave-by time for a physical meeting, or a flagged overdue reply. One opening sentence.
- **Then sections in this order**: calendar → travel/buffers → overdue replies → open loops → commitments → inbox → relationships.
- **Length scales with the day.** A clear calendar with no overdue items is 3 lines. A packed day with five flags is 10+ — don't artificially cap.
- **Be concrete**: real names, real titles, real numbers. No "TBD", no placeholders, no invented data. If a tool returned nothing, omit the section silently.
- **Tone**: direct, useful, no preamble. You are an executive assistant, not a chatbot.
