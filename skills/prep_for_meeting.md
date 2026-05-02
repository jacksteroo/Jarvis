---
name: prep_for_meeting
description: Pre-meeting intelligence — attendees, recent context, open threads, and what to raise
version: 2
---

## Workflow

1. **Identify the meeting**:
   - If a meeting name or time is mentioned, use it.
   - Otherwise call `get_upcoming_events` with `days: 1` and ask: "Which of these is the meeting you're prepping for?"

2. **Buffer check** (cheap, high-value): from the same calendar fetch, note what's immediately *before* and *after* this meeting. The user often needs to know "do I have 10 minutes after to grab coffee with you" or "I'm coming in hot from another call." State it: "Buffer: 30 min before, back-to-back after with [next event]."

3. **Attendees**: extract from the event. Skip generic room names, dial-in bridges, and the user themselves. For each key attendee:
   - Call `get_contact_profile` to see dominant channel and last contact time.

4. **Recent context per attendee** (last 7 days, dominant channel from step 3):
   - iMessage (or unknown): `get_imessage_conversation` (limit 8)
   - WhatsApp: `get_recent_whatsapp_chats` (limit 20) → find matching chat by name → `get_whatsapp_chat` with that `chat_id` (limit 8). Fall back to iMessage if no match.
   - Email: `search_emails` with `from:[name] OR to:[name]` (limit 5)
   - Slack: `search_slack` with the person's name (limit 5)
   - Summarize in 1–2 bullets per person: what was last discussed, any open asks.

5. **Prior meetings with the same people** (highest-signal context — don't skip): call `get_calendar_events_range` for the last 60 days and scan titles/descriptions for events that include any of the same attendees. Note any patterns: "You met with Jordan + Alex 3 times in the last 6 weeks — last one was the pricing review on Apr 12." Skip silently if nothing meaningful surfaces.

6. **Memory sweep**: call `search_memory` with the meeting topic AND each key attendee's name to surface:
   - Prior commitments made to these people (especially anything still open)
   - Previous meeting outcomes or decisions
   - Flagged concerns, sensitivities, or open loops

7. **Shared docs**: if the event description references a file path, link, or doc name, and the path is local, call `inspect_local_path` to preview it. Do not chase external URLs without explicit user request.

## Synthesis

- **Context** (2–3 sentences): what this meeting is about, its history, and why it matters now.
- **Buffer**: one line on what's before and after.
- **People** (1 bullet per attendee): the single most relevant signal — last interaction, open ask, or known sensitivity.
- **Open threads**: anything promised to these people or left unresolved last time.
- **Suggested agenda items** (2–3): concrete things to raise, resolve, or decide. Each one anchored in something specific from the data above — never generic.

Close with: "Anything specific you want me to dig into before you head in?"

Do not fabricate attendee names, history, or commitments. Only surface what the tools return. If the data is thin, say so plainly — "I don't have much context on this one" beats invented filler.
