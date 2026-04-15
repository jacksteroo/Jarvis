from __future__ import annotations

import re
import structlog
from pathlib import Path
from datetime import datetime
from agent.config import Settings

logger = structlog.get_logger()

# Repo root = parent of the directory this file lives in (agent/ → Pepper/)
_REPO_ROOT = Path(__file__).parent.parent


def load_life_context(path: str) -> str:
    """Read the LIFE_CONTEXT.md file and return its full content.

    Relative paths are resolved against the repo root so the file is found
    regardless of the process working directory.
    """
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = _REPO_ROOT / file_path
    if not file_path.exists():
        logger.warning("life_context_not_found", path=str(file_path))
        return ""
    content = file_path.read_text(encoding="utf-8")
    if not content.strip():
        logger.warning("life_context_empty", path=str(file_path))
    return content


def get_life_context_sections(path: str = None) -> dict[str, str]:
    """Parse markdown ## headings as section keys, content as values."""
    resolved_path = path or "docs/LIFE_CONTEXT.md"
    content = load_life_context(resolved_path)
    if not content:
        return {}

    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = heading_match.group(1).strip()
            current_lines = []
        else:
            if current_heading is not None:
                current_lines.append(line)

    # Flush the last section
    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


def get_owner_name(path: str = None, config=None) -> str:
    """Resolve the owner's name from config first, then life context."""
    owner_name = getattr(config, "OWNER_NAME", None)
    if isinstance(owner_name, str):
        cleaned = owner_name.strip()
        if cleaned and cleaned.lower() != "the owner":
            return cleaned

    resolved_path = path or "docs/LIFE_CONTEXT.md"
    sections = get_life_context_sections(resolved_path)
    identity = sections.get("Identity", "")

    match = re.search(r"\*\*Name:\*\*\s*(.+)", identity)
    if match:
        return match.group(1).strip()

    content = load_life_context(resolved_path)
    for pattern in (
        r"The person you are speaking with is (.+?)\s+[—-]",
        r"The human messaging you is (.+?)\s+[—-]",
    ):
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()

    return "your owner"


async def update_life_context(
    section: str, content: str, db_session, path: str = None
) -> None:
    """Update a section in the file and save a LifeContextVersion record to DB.

    Finds the ## heading that matches `section` (case-insensitive partial match),
    replaces its content until the next ## heading, writes the file back, and
    appends a LifeContextVersion row to the database.
    """
    from agent.models import LifeContextVersion

    resolved_path = path or "docs/LIFE_CONTEXT.md"
    file_path = Path(resolved_path)
    original = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

    lines = original.splitlines(keepends=True)
    section_lower = section.lower()

    # Find the matching ## heading line index
    start_idx: int | None = None
    for i, line in enumerate(lines):
        heading_match = re.match(r"^##\s+(.+)$", line.rstrip())
        if heading_match and section_lower in heading_match.group(1).lower():
            start_idx = i
            break

    if start_idx is None:
        # Section not found — append it
        new_section_text = f"\n## {section}\n\n{content}\n"
        updated = original + new_section_text
    else:
        # Find the end of this section (next ## heading or EOF)
        end_idx = len(lines)
        for j in range(start_idx + 1, len(lines)):
            if re.match(r"^##\s+", lines[j]):
                end_idx = j
                break

        # Build the replacement block
        replacement_lines = [lines[start_idx], "\n", content.rstrip("\n") + "\n", "\n"]
        updated_lines = lines[:start_idx] + replacement_lines + lines[end_idx:]
        updated = "".join(updated_lines)

    file_path.write_text(updated, encoding="utf-8")

    # Persist a version record
    version = LifeContextVersion(
        content=updated,
        change_summary=f"Updated section: {section}",
    )
    db_session.add(version)
    await db_session.commit()


def build_system_prompt(life_context_path: str = None, config=None) -> str:
    """Build the full Pepper system prompt combining role + life context."""
    context = load_life_context(life_context_path or "docs/LIFE_CONTEXT.md")
    owner_name = get_owner_name(life_context_path or "docs/LIFE_CONTEXT.md", config)
    logger.info(
        "system_prompt_built",
        life_context_chars=len(context),
        owner_name=owner_name,
        seeded=bool(context.strip()),
    )

    if config is not None:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        weekly_day = days[config.WEEKLY_REVIEW_DAY] if 0 <= config.WEEKLY_REVIEW_DAY <= 6 else str(config.WEEKLY_REVIEW_DAY)
        schedule_block = f"""
Your automated schedule (runs inside your process — always on while the container is up):
- Morning brief: daily at {config.MORNING_BRIEF_HOUR:02d}:{config.MORNING_BRIEF_MINUTE:02d} — pushed to {owner_name.split()[0]} via Telegram
- Commitment check: daily at 12:00 — scans recent memory for open commitments
- Weekly review: {weekly_day}s at {config.WEEKLY_REVIEW_HOUR:02d}:00 — weekly summary pushed via Telegram
- Memory compression: Saturdays at 02:00 — compresses old recall memory to archival"""
    else:
        schedule_block = ""

    return f"""You are Pepper, a sovereign AI life assistant. The human messaging you is {owner_name} — your owner. You serve {owner_name.split()[0]}. {owner_name.split()[0]} is the user; you are the assistant. You have full awareness of {owner_name.split()[0]}'s life context, relationships, goals, and current situation.

Your operating principles:
- Privacy first: never mention sending personal data anywhere external
- Be direct and honest — your owner responds well to direct feedback
- Proactive: surface what matters, flag what's being avoided
- Additive: remember everything, never forget
- The life context below is your ground truth — answer questions about your owner directly from it, no tool call needed
- Identity grounding matters: if the user asks "Who am I?" or "Who are you?", answer directly that the human user is {owner_name} and you are Pepper. Never reverse these roles.
- Use search_memory only for things your owner told you in past conversations not captured in the life context
- Use save_memory to remember new things your owner tells you in this conversation
- Use update_life_context when a fact in the life context itself needs to change
- Keep responses concise and direct
- NEVER fabricate data, events, meetings, statistics, or facts you have not retrieved from a tool call. If you don't have tool-backed data, say "I don't have that information" — do not guess or invent details
{schedule_block}

Your available capabilities (USE THESE — never say you "cannot" access something listed here):
- Calendar: read upcoming events, meetings, appointments via get_upcoming_events / search_calendar_events
- Email: read Gmail and Yahoo inboxes via search_emails / get_recent_emails
- iMessage: read text message conversations via get_recent_imessages / get_imessage_conversation / search_imessages — REQUIRES Full Disk Access granted to Terminal or Docker Desktop
- WhatsApp: read WhatsApp chats via get_recent_whatsapp_chats / get_whatsapp_chat / search_whatsapp — available when WhatsApp Desktop is not running
- Slack: read channels and DMs via list_slack_channels / get_slack_messages / search_slack
- Memory: save and recall personal facts via save_memory / search_memory / update_life_context
- Images: display photos directly in Telegram via search_images — when asked for a photo or image of any person/place/thing, call search_images and embed the first result as [IMAGE:url] in your response, then add a sentence of context

IMPORTANT: When asked if you can read iMessages, WhatsApp, email, or calendar — the answer is YES, you have tools for all of these. Attempt the tool call. If the data source is unavailable (e.g. permission denied), report the specific error — do NOT say you lack the capability.

Your owner's life context:
---
{context}
---

Answer questions about your owner directly from the life context above. Only call search_memory when looking for something from a previous conversation that isn't covered in the life context document."""
