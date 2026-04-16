"""
Phase 5.3 — MCP Privacy enforcement tests.

These are REGRESSION tests for the privacy invariant:
  - Raw personal data (iMessage, WhatsApp, email bodies, Slack messages)
    can NEVER reach an external or trusted MCP server.
  - Only local MCP servers can receive raw personal data.
  - Trust boundary violations raise MCPPrivacyViolation immediately.

These tests MUST pass. A failure here is a security regression.
"""
import pytest

from agent.mcp_audit import (
    DataClassification,
    MCPPrivacyViolation,
    PUBLIC_TOOLS,
    RAW_PERSONAL_TOOLS,
    STRUCTURED_TOOLS,
    TRUST_ALLOWS,
    check_trust_boundary,
    classify_tool_data,
    log_mcp_call,
)


# ── Data classification ──────────────────────────────────────────────────────


class TestDataClassification:
    """Verify that tools are classified at the correct sensitivity level."""

    def test_imessage_tools_are_raw_personal(self):
        assert classify_tool_data("get_recent_imessages") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("get_imessage_conversation") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("search_imessages") == DataClassification.RAW_PERSONAL

    def test_whatsapp_tools_are_raw_personal(self):
        assert classify_tool_data("get_recent_whatsapp_chats") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("get_whatsapp_chat") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("get_whatsapp_messages") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("search_whatsapp") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("get_whatsapp_groups") == DataClassification.RAW_PERSONAL

    def test_email_tools_are_raw_personal(self):
        assert classify_tool_data("get_recent_emails") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("search_emails") == DataClassification.RAW_PERSONAL

    def test_slack_tools_are_raw_personal(self):
        assert classify_tool_data("search_slack") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("get_slack_channel_messages") == DataClassification.RAW_PERSONAL

    def test_memory_tools_are_raw_personal(self):
        assert classify_tool_data("search_memory") == DataClassification.RAW_PERSONAL
        assert classify_tool_data("save_memory") == DataClassification.RAW_PERSONAL

    def test_structured_tools(self):
        assert classify_tool_data("get_email_unread_counts") == DataClassification.STRUCTURED
        assert classify_tool_data("get_contact_profile") == DataClassification.STRUCTURED
        assert classify_tool_data("find_quiet_contacts") == DataClassification.STRUCTURED
        assert classify_tool_data("list_calendars") == DataClassification.STRUCTURED

    def test_public_tools(self):
        assert classify_tool_data("get_upcoming_events") == DataClassification.PUBLIC
        assert classify_tool_data("search_web") == DataClassification.PUBLIC

    def test_unknown_tool_defaults_to_structured(self):
        """Unknown tools default to STRUCTURED (conservative), not PUBLIC.

        This ensures new tools cannot be routed to external MCP servers until
        they have been explicitly classified as safe.
        """
        assert classify_tool_data("unknown_new_tool") == DataClassification.STRUCTURED
        assert classify_tool_data("some_future_tool") == DataClassification.STRUCTURED


# ── Trust boundary enforcement ───────────────────────────────────────────────


class TestTrustBoundary:
    """Verify that trust boundaries are enforced correctly."""

    # --- LOCAL servers can access everything ---

    def test_local_allows_raw_personal(self):
        """Local MCP servers CAN access raw personal data."""
        check_trust_boundary("local-server", "local", "get_recent_imessages")
        check_trust_boundary("local-server", "local", "search_emails")
        check_trust_boundary("local-server", "local", "get_whatsapp_chat")

    def test_local_allows_structured(self):
        check_trust_boundary("local-server", "local", "get_email_unread_counts")

    def test_local_allows_public(self):
        check_trust_boundary("local-server", "local", "get_upcoming_events")

    # --- TRUSTED servers cannot access raw personal data ---

    def test_trusted_blocks_raw_personal_imessage(self):
        """Trusted MCP servers CANNOT access iMessage content."""
        with pytest.raises(MCPPrivacyViolation) as exc_info:
            check_trust_boundary("notion-server", "trusted", "get_recent_imessages")
        assert "PRIVACY VIOLATION" in str(exc_info.value)

    def test_trusted_blocks_raw_personal_whatsapp(self):
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("notion-server", "trusted", "get_whatsapp_chat")

    def test_trusted_blocks_raw_personal_email(self):
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("notion-server", "trusted", "search_emails")

    def test_trusted_blocks_raw_personal_slack(self):
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("notion-server", "trusted", "search_slack")

    def test_trusted_blocks_raw_personal_memory(self):
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("notion-server", "trusted", "search_memory")

    def test_trusted_allows_structured(self):
        """Trusted servers CAN access structured data."""
        check_trust_boundary("notion-server", "trusted", "get_email_unread_counts")
        check_trust_boundary("notion-server", "trusted", "get_contact_profile")

    def test_trusted_allows_public(self):
        check_trust_boundary("notion-server", "trusted", "get_upcoming_events")

    # --- EXTERNAL servers cannot access raw personal or structured data ---

    def test_external_blocks_raw_personal(self):
        """External MCP servers CANNOT access any personal data."""
        for tool in RAW_PERSONAL_TOOLS:
            with pytest.raises(MCPPrivacyViolation):
                check_trust_boundary("github-server", "external", tool)

    def test_external_blocks_structured(self):
        """External servers CANNOT access structured personal data."""
        for tool in STRUCTURED_TOOLS:
            with pytest.raises(MCPPrivacyViolation):
                check_trust_boundary("github-server", "external", tool)

    def test_external_allows_public(self):
        """External servers CAN access public data."""
        check_trust_boundary("github-server", "external", "get_upcoming_events")
        check_trust_boundary("github-server", "external", "search_web")

    # --- Comprehensive: every raw personal tool must fail on trusted + external ---

    def test_all_raw_personal_blocked_on_trusted(self):
        """REGRESSION: every tool in RAW_PERSONAL_TOOLS must fail on trusted."""
        for tool in RAW_PERSONAL_TOOLS:
            with pytest.raises(MCPPrivacyViolation):
                check_trust_boundary("any-trusted", "trusted", tool)

    def test_all_raw_personal_blocked_on_external(self):
        """REGRESSION: every tool in RAW_PERSONAL_TOOLS must fail on external."""
        for tool in RAW_PERSONAL_TOOLS:
            with pytest.raises(MCPPrivacyViolation):
                check_trust_boundary("any-external", "external", tool)

    def test_all_structured_blocked_on_external(self):
        """REGRESSION: every tool in STRUCTURED_TOOLS must fail on external."""
        for tool in STRUCTURED_TOOLS:
            with pytest.raises(MCPPrivacyViolation):
                check_trust_boundary("any-external", "external", tool)


# ── Trust level configuration ────────────────────────────────────────────────


class TestToolSetConsistency:
    """Verify the tool classification sets are internally consistent."""

    def test_tool_sets_are_disjoint(self):
        """No tool can appear in multiple classification sets."""
        raw_and_structured = RAW_PERSONAL_TOOLS & STRUCTURED_TOOLS
        raw_and_public = RAW_PERSONAL_TOOLS & PUBLIC_TOOLS
        structured_and_public = STRUCTURED_TOOLS & PUBLIC_TOOLS

        assert raw_and_structured == frozenset(), (
            f"Tools in both RAW_PERSONAL and STRUCTURED: {raw_and_structured}"
        )
        assert raw_and_public == frozenset(), (
            f"Tools in both RAW_PERSONAL and PUBLIC: {raw_and_public}"
        )
        assert structured_and_public == frozenset(), (
            f"Tools in both STRUCTURED and PUBLIC: {structured_and_public}"
        )

    def test_all_tool_sets_exported(self):
        """PUBLIC_TOOLS is exported and importable from mcp_audit."""
        assert PUBLIC_TOOLS is not None
        assert len(PUBLIC_TOOLS) > 0


class TestTrustConfig:
    """Verify the trust level configuration is correct."""

    def test_local_allows_all_classifications(self):
        assert TRUST_ALLOWS["local"] == {
            DataClassification.RAW_PERSONAL,
            DataClassification.STRUCTURED,
            DataClassification.SUMMARY,
            DataClassification.PUBLIC,
        }

    def test_trusted_excludes_raw_personal(self):
        assert DataClassification.RAW_PERSONAL not in TRUST_ALLOWS["trusted"]
        assert DataClassification.STRUCTURED in TRUST_ALLOWS["trusted"]
        assert DataClassification.SUMMARY in TRUST_ALLOWS["trusted"]
        assert DataClassification.PUBLIC in TRUST_ALLOWS["trusted"]

    def test_external_only_summary_and_public(self):
        assert TRUST_ALLOWS["external"] == {
            DataClassification.SUMMARY,
            DataClassification.PUBLIC,
        }
        assert DataClassification.RAW_PERSONAL not in TRUST_ALLOWS["external"]
        assert DataClassification.STRUCTURED not in TRUST_ALLOWS["external"]


# ── MCPPrivacyViolation exception ────────────────────────────────────────────


class TestPrivacyViolationException:
    """Verify the exception carries useful information."""

    def test_violation_has_server_info(self):
        exc = MCPPrivacyViolation(
            "my-server", "external", "search_imessages",
            DataClassification.RAW_PERSONAL,
        )
        assert exc.server_name == "my-server"
        assert exc.trust_level == "external"
        assert exc.tool_name == "search_imessages"
        assert exc.data_classification == DataClassification.RAW_PERSONAL

    def test_violation_message_is_actionable(self):
        exc = MCPPrivacyViolation(
            "github", "external", "get_recent_emails",
            DataClassification.RAW_PERSONAL,
        )
        msg = str(exc)
        assert "PRIVACY VIOLATION" in msg
        assert "get_recent_emails" in msg
        assert "github" in msg
        assert "external" in msg


# ── Audit logging ────────────────────────────────────────────────────────────


class TestAuditLogging:
    """Verify audit log entries are generated correctly."""

    def test_log_mcp_call_succeeds(self):
        """log_mcp_call should not raise for valid inputs."""
        log_mcp_call(
            server_name="test",
            trust_level="local",
            tool_name="get_upcoming_events",
            duration_ms=42,
            success=True,
        )

    def test_log_mcp_call_with_error(self):
        log_mcp_call(
            server_name="test",
            trust_level="external",
            tool_name="some_tool",
            duration_ms=0,
            success=False,
            error="Connection refused",
        )


class TestUnknownTrustLevel:
    """Verify unknown trust levels are handled safely."""

    def test_unknown_trust_level_blocks_raw_personal(self):
        """Unknown trust level must block raw personal data (treated as external)."""
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("server", "unknown_level", "get_recent_imessages")

    def test_unknown_trust_level_blocks_structured(self):
        """Unknown trust level must block structured data (treated as external)."""
        with pytest.raises(MCPPrivacyViolation):
            check_trust_boundary("server", "unknown_level", "get_email_unread_counts")

    def test_unknown_trust_level_allows_public(self):
        """Unknown trust level allows public tools (treated as external)."""
        # Should not raise
        check_trust_boundary("server", "unknown_level", "search_web")


class TestDataClassificationCoverage:
    """Additional coverage for classify_tool_data."""

    def test_calendar_range_is_public(self):
        assert classify_tool_data("get_calendar_events_range") == DataClassification.PUBLIC

    def test_driving_time_is_public(self):
        assert classify_tool_data("get_driving_time") == DataClassification.PUBLIC

    def test_search_images_is_public(self):
        assert classify_tool_data("search_images") == DataClassification.PUBLIC

    def test_search_contacts_is_structured(self):
        assert classify_tool_data("search_contacts") == DataClassification.STRUCTURED

    def test_comms_health_summary_is_structured(self):
        assert classify_tool_data("get_comms_health_summary") == DataClassification.STRUCTURED

    def test_overdue_responses_is_structured(self):
        assert classify_tool_data("get_overdue_responses") == DataClassification.STRUCTURED


# ── MCP Server (5.4) access control ─────────────────────────────────────────


class TestMCPServerAccessControl:
    """Verify Pepper-as-MCP-server never exposes raw personal data tools."""

    def test_never_expose_list(self):
        from agent.mcp_server import NEVER_EXPOSE
        # Every raw personal data tool must be in NEVER_EXPOSE
        for tool in RAW_PERSONAL_TOOLS:
            assert tool in NEVER_EXPOSE, f"{tool} must be in NEVER_EXPOSE"

    def test_write_side_effect_tools_never_exposed(self):
        """Tools with write side-effects must never be exposed to external clients."""
        from agent.mcp_server import NEVER_EXPOSE
        write_tools = {"update_life_context", "mark_commitment_complete"}
        for tool in write_tools:
            assert tool in NEVER_EXPOSE, (
                f"Write-side-effect tool '{tool}' must be in NEVER_EXPOSE"
            )

    def test_default_allowed_is_safe(self):
        from agent.mcp_server import DEFAULT_ALLOWED_TOOLS, NEVER_EXPOSE
        # No default allowed tool should be in NEVER_EXPOSE
        overlap = DEFAULT_ALLOWED_TOOLS & NEVER_EXPOSE
        assert overlap == set(), f"Unsafe tools in default allowlist: {overlap}"

    def test_access_config_strips_never_expose(self):
        """Even if config allows a NEVER_EXPOSE tool, it gets stripped."""
        from agent.mcp_server import NEVER_EXPOSE
        # Simulate: _load_access_config with a config that includes blocked tools
        requested = {"search_memory", "get_upcoming_events", "get_recent_imessages",
                     "mark_commitment_complete"}
        safe = requested - NEVER_EXPOSE
        assert "get_recent_imessages" not in safe
        assert "search_memory" not in safe      # memory is in NEVER_EXPOSE
        assert "mark_commitment_complete" not in safe  # write side-effect
        assert "get_upcoming_events" in safe

    def test_never_expose_and_raw_personal_are_consistent(self):
        """RAW_PERSONAL_TOOLS must be a subset of NEVER_EXPOSE."""
        from agent.mcp_server import NEVER_EXPOSE
        missing = RAW_PERSONAL_TOOLS - NEVER_EXPOSE
        assert missing == frozenset(), (
            f"RAW_PERSONAL_TOOLS entries missing from NEVER_EXPOSE: {missing}"
        )
