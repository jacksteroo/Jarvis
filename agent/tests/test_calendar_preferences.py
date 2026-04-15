from __future__ import annotations

import json
import os

from agent import accounts
from subsystems.calendar import client as calendar_client
from subsystems.calendar import preferences


def test_get_calendar_excluded_ids_reads_per_account_and_reload(monkeypatch, tmp_path):
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps({
        "calendar": {
            "excluded_calendar_ids_by_account": {
                "default": ["personal-hidden"],
                "work": ["work-hidden"],
            }
        }
    }))

    monkeypatch.setattr(accounts, "_CONFIG_PATH", config_path)
    accounts._load_cached.cache_clear()

    assert accounts.get_calendar_excluded_ids() == {"personal-hidden"}
    assert accounts.get_calendar_excluded_ids("work") == {"work-hidden"}

    original_stat = config_path.stat()
    config_path.write_text(json.dumps({
        "calendar": {
            "excluded_calendar_ids_by_account": {
                "default": ["updated-hidden"],
            }
        }
    }))
    os.utime(config_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1))

    assert accounts.get_calendar_excluded_ids() == {"updated-hidden"}


def test_save_excluded_calendar_ids_persists_under_calendar_config(monkeypatch, tmp_path):
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps({
        "email": {
            "accounts": [{"id": "personal", "label": "Personal", "type": "gmail"}]
        }
    }))

    monkeypatch.setattr(preferences, "CONFIG_PATH", config_path)

    preferences.save_excluded_calendar_ids("work", ["b@example.com", "a@example.com"])

    saved = json.loads(config_path.read_text())
    assert saved["email"]["accounts"][0]["id"] == "personal"
    assert saved["calendar"]["excluded_calendar_ids_by_account"]["work"] == [
        "a@example.com",
        "b@example.com",
    ]


def test_calendar_client_filters_excluded_calendars_by_default(monkeypatch):
    calendars = [
        {"id": "primary@example.com", "summary": "Primary", "primary": True},
        {"id": "hidden@example.com", "summary": "Hidden Shared"},
        {"id": "visible@example.com", "summary": "Visible Shared"},
    ]

    class FakeRequest:
        def execute(self):
            return {"items": calendars}

    class FakeCalendarList:
        def list(self, pageToken=None):
            return FakeRequest()

    class FakeService:
        def calendarList(self):
            return FakeCalendarList()

    monkeypatch.setattr(calendar_client, "get_credentials", lambda account: object())
    monkeypatch.setattr(calendar_client, "build", lambda *args, **kwargs: FakeService())
    monkeypatch.setattr(
        calendar_client,
        "get_excluded_calendar_ids",
        lambda account=None: {"hidden@example.com"},
    )

    client = calendar_client.CalendarClient(account="work")

    assert [calendar["id"] for calendar in client.list_calendars()] == [
        "primary@example.com",
        "visible@example.com",
    ]
    assert [calendar["id"] for calendar in client.list_calendars(include_excluded=True)] == [
        "primary@example.com",
        "hidden@example.com",
        "visible@example.com",
    ]


def test_prompt_shared_calendar_selection_handles_single_non_primary_calendar(monkeypatch, tmp_path):
    config_path = tmp_path / "accounts.json"
    monkeypatch.setattr(preferences, "CONFIG_PATH", config_path)

    prompts = []
    printed = []
    calendars = [
        {"id": "primary@example.com", "summary": "Primary", "primary": True},
        {"id": "shared@example.com", "summary": "Shared"},
    ]

    excluded = preferences.prompt_shared_calendar_selection(
        "work",
        calendars,
        input_fn=lambda prompt: prompts.append(prompt) or "none",
        print_fn=printed.append,
    )

    saved = json.loads(config_path.read_text())
    assert excluded == ["shared@example.com"]
    assert prompts
    assert saved["calendar"]["excluded_calendar_ids_by_account"]["work"] == ["shared@example.com"]
