"""
Phase 5.3 — MCP Audit Logger.

Every MCP tool call is logged to an append-only audit trail with:
  - timestamp
  - server name and trust level
  - tool name
  - data classification of inputs
  - duration
  - success/failure

Violations (attempted cross-trust routing) are logged at ERROR level
and raise immediately — privacy bugs, not runtime warnings.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Dedicated audit logger — writes to a separate structured log
audit_logger = structlog.get_logger("pepper.mcp_audit")


class DataClassification(str, Enum):
    """Data sensitivity classification for MCP tool inputs.

    Maps to the existing DataSensitivity enum from error_classifier.py
    but specific to MCP routing context.
    """
    RAW_PERSONAL = "raw_personal"     # iMessage bodies, email content, WhatsApp messages
    STRUCTURED = "structured"          # contact names, calendar titles, sender addresses
    SUMMARY = "summary"                # LLM-generated summaries, aggregated stats
    PUBLIC = "public"                  # web search results, general knowledge


# Tools that handle raw personal data — must ONLY run on local MCP servers.
# This is the enforcement point for the privacy invariant.
RAW_PERSONAL_TOOLS = frozenset({
    # iMessage
    "get_recent_imessages", "get_imessage_conversation", "search_imessages",
    # WhatsApp
    "get_recent_whatsapp_chats", "get_whatsapp_chat", "get_whatsapp_messages",
    "search_whatsapp", "get_whatsapp_groups",
    # Email bodies
    "get_recent_emails", "search_emails",
    # Slack message content
    "search_slack", "get_slack_channel_messages",
    # Memory (may contain raw recalled content)
    "search_memory", "save_memory",
})

# Tools that return structured but non-raw data — ok for trusted servers
STRUCTURED_TOOLS = frozenset({
    "get_email_unread_counts",
    "get_contact_profile", "find_quiet_contacts", "search_contacts",
    "get_comms_health_summary", "get_overdue_responses",
    "get_relationship_balance_report",
    "list_calendars",
    "list_slack_channels", "get_slack_deadlines",
})

# Tools that return fully public data — ok for external servers
PUBLIC_TOOLS = frozenset({
    "get_upcoming_events", "get_calendar_events_range",
    "search_web", "get_driving_time", "search_images",
})

# Trust level → maximum allowed data classification
TRUST_ALLOWS = {
    "local": {DataClassification.RAW_PERSONAL, DataClassification.STRUCTURED,
              DataClassification.SUMMARY, DataClassification.PUBLIC},
    "trusted": {DataClassification.STRUCTURED, DataClassification.SUMMARY,
                DataClassification.PUBLIC},
    "external": {DataClassification.SUMMARY, DataClassification.PUBLIC},
}


def classify_tool_data(
    tool_name: str,
    server_trust_level: str | None = None,
) -> DataClassification:
    """Classify the data sensitivity of a tool's inputs/outputs.

    Uses the canonical tool name (without mcp_ prefix) plus, optionally,
    the trust level of the server the tool lives on.

    The trust-level context matters for unknown tools (i.e. tools from
    third-party MCP servers that don't appear in Pepper's hardcoded lists):

    * ``server_trust_level="external"`` → unknown tools are classified as
      PUBLIC.  External MCP servers are expected to expose only non-personal
      tools (GitHub issues, web search, routing, etc.).  Treating them as
      STRUCTURED would block the entire external-MCP integration because
      external servers are only allowed SUMMARY/PUBLIC.

    * Any other trust level (local, trusted, or unspecified) → unknown tools
      default to STRUCTURED.  New internal tools that haven't been explicitly
      classified should not be treated as public until a human confirms they
      are safe to route externally.
    """
    if tool_name in RAW_PERSONAL_TOOLS:
        return DataClassification.RAW_PERSONAL
    if tool_name in STRUCTURED_TOOLS:
        return DataClassification.STRUCTURED
    if tool_name in PUBLIC_TOOLS:
        return DataClassification.PUBLIC
    # Context-sensitive default for unknown tools.
    if server_trust_level == "external":
        return DataClassification.PUBLIC
    return DataClassification.STRUCTURED


class MCPPrivacyViolation(Exception):
    """Raised when an MCP tool call would violate trust boundaries.

    This is a privacy bug, not a runtime warning. It should never
    be caught silently — always surface to the user and log at ERROR.
    """
    def __init__(self, server_name: str, trust_level: str,
                 tool_name: str, data_classification: DataClassification):
        self.server_name = server_name
        self.trust_level = trust_level
        self.tool_name = tool_name
        self.data_classification = data_classification
        # Use .get() to handle unknown trust levels gracefully in the error message
        allowed = TRUST_ALLOWS.get(trust_level, TRUST_ALLOWS["external"])
        super().__init__(
            f"PRIVACY VIOLATION: Tool '{tool_name}' (data: {data_classification.value}) "
            f"cannot run on MCP server '{server_name}' (trust: {trust_level}). "
            f"Trust level '{trust_level}' only allows: "
            f"{', '.join(d.value for d in allowed)}"
        )


# Argument values longer than this threshold on a non-local server are logged
# as a potential raw-content leak.  Search queries and structured payloads are
# short; raw message bodies are long.  We warn but do not block — the threshold
# is intentionally conservative to avoid false-positive rejections on legitimate
# long inputs (e.g. a Markdown code snippet passed to a documentation tool).
_RAW_CONTENT_CHAR_THRESHOLD = 500


def check_trust_boundary(
    server_name: str,
    trust_level: str,
    tool_name: str,
    arguments: dict | None = None,
) -> None:
    """Validate that a tool call respects trust boundaries.

    Raises MCPPrivacyViolation if the tool's data classification exceeds
    what the server's trust level allows.

    Also performs an argument-level privacy scan when ``arguments`` is
    provided: if any string argument value sent to a non-local server
    exceeds _RAW_CONTENT_CHAR_THRESHOLD characters, a WARNING is emitted.
    This catches cases where the LLM passes raw message/email content as
    the value of a nominally "public" argument (e.g. a search query that
    is actually a full email thread).  Values are never logged — only their
    lengths are recorded.

    Unknown trust levels are treated as the most restrictive (external),
    and a warning is logged — this makes misconfiguration safe-by-default.
    """
    data_class = classify_tool_data(tool_name, server_trust_level=trust_level)
    allowed = TRUST_ALLOWS.get(trust_level)
    if allowed is None:
        audit_logger.warning(
            "mcp_unknown_trust_level",
            server=server_name,
            trust_level=trust_level,
            tool=tool_name,
            fallback="treating as external",
        )
        allowed = TRUST_ALLOWS["external"]

    if data_class not in allowed:
        violation = MCPPrivacyViolation(
            server_name=server_name,
            trust_level=trust_level,
            tool_name=tool_name,
            data_classification=data_class,
        )
        audit_logger.error(
            "mcp_privacy_violation",
            server=server_name,
            trust_level=trust_level,
            tool=tool_name,
            data_classification=data_class.value,
        )
        raise violation

    # Argument-level scan: warn if outbound arguments look like raw content.
    # Only applied to non-local servers — local servers are fully trusted.
    if arguments and trust_level != "local":
        oversized = {
            k: len(v)
            for k, v in arguments.items()
            if isinstance(v, str) and len(v) > _RAW_CONTENT_CHAR_THRESHOLD
        }
        if oversized:
            audit_logger.warning(
                "mcp_argument_oversized",
                server=server_name,
                trust_level=trust_level,
                tool=tool_name,
                oversized_arg_lengths=oversized,
                note=(
                    f"Argument value(s) exceed {_RAW_CONTENT_CHAR_THRESHOLD} chars — "
                    "possible raw personal content being sent to a non-local server. "
                    "Ensure only summaries reach non-local MCP tools."
                ),
            )


@dataclass
class AuditEntry:
    """A single MCP audit log entry."""
    timestamp: str
    server_name: str
    trust_level: str
    tool_name: str
    data_classification: str
    duration_ms: int
    success: bool
    error: str | None = None


def log_mcp_call(
    server_name: str,
    trust_level: str,
    tool_name: str,
    duration_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Log an MCP tool call to the audit trail."""
    data_class = classify_tool_data(tool_name, server_trust_level=trust_level)
    entry = AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        server_name=server_name,
        trust_level=trust_level,
        tool_name=tool_name,
        data_classification=data_class.value,
        duration_ms=duration_ms,
        success=success,
        error=error,
    )
    audit_logger.info("mcp_audit", **asdict(entry))
