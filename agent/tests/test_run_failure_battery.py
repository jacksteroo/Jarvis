"""Unit tests for scripts/run_failure_battery.py (Phase 0 Task 3)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_failure_battery as rfb  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_load_battery_round_trip(tmp_path: Path) -> None:
    battery = tmp_path / "b.jsonl"
    _write_jsonl(
        battery,
        [
            {"id": "x-01", "query": "q1", "expected_intent": "i1"},
            {"id": "x-02", "query": "q2"},
        ],
    )
    rows = rfb.load_battery(battery)
    assert len(rows) == 2
    assert rows[0]["id"] == "x-01"
    assert rows[1]["query"] == "q2"


def test_find_chat_turn_picks_latest_for_session(monkeypatch, tmp_path: Path) -> None:
    chat_dir = tmp_path / "chat_turns"
    chat_dir.mkdir()
    today = datetime.now().strftime("%Y-%m-%d")
    path = chat_dir / f"{today}.jsonl"
    _write_jsonl(
        path,
        [
            {"session_id": "other", "model": "x"},
            {"session_id": "abc", "model": "first", "tool_calls": []},
            {"session_id": "abc", "model": "latest", "tool_calls": [{"name": "t"}]},
        ],
    )
    monkeypatch.setattr(rfb, "CHAT_TURN_DIR", chat_dir)
    found = rfb.find_chat_turn("abc")
    assert found is not None
    assert found["model"] == "latest"
    assert found["tool_calls"] == [{"name": "t"}]


def test_find_chat_turn_missing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rfb, "CHAT_TURN_DIR", tmp_path / "nonexistent")
    assert rfb.find_chat_turn("abc") is None


def test_find_chat_turn_no_match(monkeypatch, tmp_path: Path) -> None:
    chat_dir = tmp_path / "chat_turns"
    chat_dir.mkdir()
    today = datetime.now().strftime("%Y-%m-%d")
    _write_jsonl(chat_dir / f"{today}.jsonl", [{"session_id": "other"}])
    monkeypatch.setattr(rfb, "CHAT_TURN_DIR", chat_dir)
    assert rfb.find_chat_turn("missing") is None


class _StubResp:
    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> dict:
        return self._body if isinstance(self._body, dict) else {}


class _StubClient:
    def __init__(self, responses: list[_StubResp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def post(self, url: str, headers: dict, json: dict) -> _StubResp:  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def test_run_battery_writes_combined_record(monkeypatch, tmp_path: Path) -> None:
    battery = tmp_path / "battery.jsonl"
    _write_jsonl(
        battery,
        [
            {
                "id": "t-01",
                "category": "Test",
                "query": "hello?",
                "difficulty": 1,
                "expected_intent": "greeting",
                "expected_tools": [],
                "notes": "n",
            }
        ],
    )
    chat_dir = tmp_path / "chat_turns"
    chat_dir.mkdir()
    today = datetime.now().strftime("%Y-%m-%d")
    captured: dict = {}

    stub = _StubClient([_StubResp(200, {"response": "hi back"})])
    monkeypatch.setattr(rfb.httpx, "Client", lambda timeout: stub)

    def _fake_find(session_id: str) -> dict:
        captured["session_id"] = session_id
        _write_jsonl(
            chat_dir / f"{today}.jsonl",
            [
                {
                    "session_id": session_id,
                    "model": "local/x",
                    "tool_calls": [{"name": "search_memory"}],
                    "latency_ms": 42,
                }
            ],
        )
        return {
            "session_id": session_id,
            "model": "local/x",
            "tool_calls": [{"name": "search_memory"}],
            "latency_ms": 42,
        }

    monkeypatch.setattr(rfb, "find_chat_turn", _fake_find)

    out_path = tmp_path / "out.jsonl"
    summary = rfb.run_battery(
        battery_path=battery,
        api_url="http://example",
        api_key="k",
        out_path=out_path,
        limit=None,
        timeout_s=5.0,
    )
    assert summary["sent"] == 1
    assert summary["failed"] == 0
    assert stub.calls[0]["headers"]["x-api-key"] == "k"
    assert stub.calls[0]["json"]["message"] == "hello?"

    rows = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert len(rows) == 1
    rec = rows[0]
    assert rec["battery_id"] == "t-01"
    assert rec["response"] == "hi back"
    assert rec["model"] == "local/x"
    assert rec["tool_calls"] == [{"name": "search_memory"}]
    assert rec["error"] is None
    assert rec["session_id"].startswith("battery-")
    assert "t-01" in rec["session_id"]


def test_run_battery_records_http_error(monkeypatch, tmp_path: Path) -> None:
    battery = tmp_path / "battery.jsonl"
    _write_jsonl(battery, [{"id": "t-01", "query": "q"}])

    stub = _StubClient([_StubResp(500, "boom")])
    monkeypatch.setattr(rfb.httpx, "Client", lambda timeout: stub)
    monkeypatch.setattr(rfb, "find_chat_turn", lambda sid: None)

    out_path = tmp_path / "out.jsonl"
    summary = rfb.run_battery(
        battery_path=battery,
        api_url="http://example",
        api_key="k",
        out_path=out_path,
        limit=None,
        timeout_s=5.0,
    )
    assert summary["sent"] == 1
    assert summary["failed"] == 1
    rec = json.loads(out_path.read_text().splitlines()[0])
    assert rec["error"].startswith("http_500")
    assert rec["response"] is None
    assert rec["tool_calls"] == []


def test_run_battery_respects_limit(monkeypatch, tmp_path: Path) -> None:
    battery = tmp_path / "battery.jsonl"
    _write_jsonl(
        battery,
        [
            {"id": "t-01", "query": "q1"},
            {"id": "t-02", "query": "q2"},
            {"id": "t-03", "query": "q3"},
        ],
    )
    stub = _StubClient([_StubResp(200, {"response": "ok"}) for _ in range(3)])
    monkeypatch.setattr(rfb.httpx, "Client", lambda timeout: stub)
    monkeypatch.setattr(rfb, "find_chat_turn", lambda sid: None)

    out_path = tmp_path / "out.jsonl"
    summary = rfb.run_battery(
        battery_path=battery,
        api_url="http://example",
        api_key="k",
        out_path=out_path,
        limit=2,
        timeout_s=5.0,
    )
    assert summary["sent"] == 2
    assert len(stub.calls) == 2
