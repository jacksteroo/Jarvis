---
name: draft_reply_to_contact
description: Draft a reply to a specific person using recent message history for context
triggers:
  - draft a reply
  - draft reply
  - write a reply
  - reply to
  - respond to
  - write back to
  - help me reply
  - draft a message to
  - write a message to
tools:
  - get_imessage_conversation
  - get_recent_whatsapp_chats
  - get_whatsapp_chat
  - search_emails
  - get_contact_profile
  - search_memory
  - queue_outbound_action
model: frontier
version: 2
---

## Workflow

1. Identify the contact from the user's request. If unclear, ask once: "Who are you replying to?"

2. Call `get_contact_profile` with the contact's name to see their dominant channel and last contact time.

3. Based on dominant channel, fetch recent context:
   - iMessage: call `get_imessage_conversation` (limit 10 messages)
   - WhatsApp: `get_whatsapp_chat` requires a numeric `chat_id`, not a name.
     First call `get_recent_whatsapp_chats` (limit 30) to find the chat whose
     name matches the contact. Extract its `chat_id`, then call `get_whatsapp_chat`
     with that `chat_id` (limit 10 messages). If no matching chat is found, say so.
   - Email: call `search_emails` with `from:[contact name]` (limit 5)
   - If dominant channel is unclear, try iMessage first, then email.

4. Call `search_memory` with the contact's name to surface any relevant history, commitments, or context.

5. Draft the reply:
   - Match the tone of the existing conversation (casual vs. formal)
   - Reference specific things from the thread — do not write a generic message
   - Address the most recent unanswered question or request
   - Keep it concise — match the length of their typical messages
   - Present the draft clearly labeled: "Draft reply:"

6. Call `queue_outbound_action` with:
   - `tool_name`: the appropriate send tool for the channel
     (e.g. `mcp_imessage_send_message`, `mcp_whatsapp_send_message`, or `mcp_gmail_send_email`)
   - `args`: the full arguments object needed to send (to/recipient, body/message, etc.)
   - `preview`: a one-line summary like "Reply to Sarah via iMessage: <first 80 chars of draft>"
   This enqueues the draft for your explicit approval in the Pepper status panel — nothing is sent until you approve.

7. After queuing, confirm: "Draft queued for your review. You can approve, edit, or reject it from the status panel. Want me to adjust anything before you approve?"

Never fabricate conversation history. Only draft based on what the tools return.
Never send directly — always use queue_outbound_action so you keep full control.
