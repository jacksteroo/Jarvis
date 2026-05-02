---
name: relationship_nudge
description: Surface contacts going cold and propose a low-effort reach-out for the most important ones
version: 1
---

## Workflow

1. Call `find_quiet_contacts` with `days: 30` (configurable — start at 30, go to 60 if the user asks for "deeper").

2. Filter aggressively. Not every quiet contact is worth a nudge — most aren't. Drop:
   - Vendors, support contacts, mailing-list senders, transactional addresses
   - One-off contacts the user has only ever exchanged 1–2 messages with
   - Anyone clearly tagged as low-priority in life context or memory

   Keep contacts who appear to be:
   - Personal friends, family
   - Active professional relationships (colleagues, advisors, recurring collaborators)
   - People mentioned positively in life context

3. **Prioritize**. For each surviving candidate, call `get_contact_profile` and `search_memory` with their name. Score by:
   - **Closeness signal**: how often they used to talk, breadth of channels
   - **Recency of relationship**: a friend gone quiet 6 weeks is more urgent than a vendor gone quiet 6 months
   - **Open loops**: any unresolved commitment to or from this person bumps priority

4. Pick the **top 3 candidates** (max). Anything more is overwhelming and turns into a chore the user ignores.

5. For each of the top 3, present:
   - Name + last contact (relative: "haven't talked in 5 weeks")
   - One-line context: who they are / why they matter, anchored in memory or life context — never invented
   - A **proposed nudge**: one sentence the user could send. Specific, low-effort, references something real ("How did the move to Berlin go?"). Not "Hey, long time!"

6. Offer to draft the actual message: "Want me to draft any of these as a real outbound? I'll queue them for your approval."

   - If yes, hand off to the `draft_reply_to_contact` skill workflow (or call its drafter directly via the channel-specific tool).

Don't fabricate context. If you don't have anything specific to anchor the nudge in, say so — "I don't have anything specific to reference for [Name]; want to give me a hook?" — rather than inventing a fake one.
