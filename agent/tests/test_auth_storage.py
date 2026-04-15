from __future__ import annotations

import json

from subsystems import google_auth
from subsystems.communications import imap_client


def test_google_token_path_normalizes_personal(monkeypatch, tmp_path):
    monkeypatch.setattr(google_auth, "CONFIG_DIR", tmp_path)

    assert google_auth.token_path(None) == tmp_path / "google_token.json"
    assert google_auth.token_path("personal") == tmp_path / "google_token.json"
    assert google_auth.token_path("default") == tmp_path / "google_token.json"
    assert google_auth.token_path("work") == tmp_path / "google_token_work.json"


def test_list_authorized_google_accounts_uses_shared_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(google_auth, "CONFIG_DIR", tmp_path)
    (tmp_path / "google_token.json").write_text("{}")
    (tmp_path / "google_token_work.json").write_text("{}")

    assert google_auth.list_authorized_accounts() == ["default", "work"]


def test_google_get_credentials_reauths_when_token_file_lacks_gmail_scope(monkeypatch, tmp_path):
    shared_token = tmp_path / "google_token.json"
    shared_token.write_text(json.dumps({
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
    }))
    credentials_path = tmp_path / "google_credentials.json"
    credentials_path.write_text("{}")

    monkeypatch.setattr(google_auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(google_auth, "CREDENTIALS_PATH", credentials_path)

    class FakeExistingCreds:
        valid = True
        expired = False
        refresh_token = "refresh-token"

        def has_scopes(self, scopes):
            return True

    class FakeNewCreds:
        valid = True
        expired = False
        refresh_token = "refresh-token"

        def has_scopes(self, scopes):
            return True

        def to_json(self):
            return json.dumps({"scopes": google_auth.GOOGLE_SCOPES})

    fake_existing = FakeExistingCreds()
    fake_new = FakeNewCreds()

    def fake_from_authorized_user_file(filename, scopes):
        return fake_existing

    monkeypatch.setattr(
        google_auth.Credentials,
        "from_authorized_user_file",
        staticmethod(fake_from_authorized_user_file),
    )

    class FakeFlow:
        def run_local_server(self, port=0, open_browser=True):
            return fake_new

    flow_calls = []

    def fake_from_client_secrets_file(filename, scopes):
        flow_calls.append((filename, tuple(scopes)))
        return FakeFlow()

    monkeypatch.setattr(
        google_auth.InstalledAppFlow,
        "from_client_secrets_file",
        staticmethod(fake_from_client_secrets_file),
    )

    creds = google_auth.get_credentials()

    assert creds is fake_new
    assert flow_calls == [(str(credentials_path), tuple(google_auth.GOOGLE_SCOPES))]
    assert json.loads(shared_token.read_text())["scopes"] == google_auth.GOOGLE_SCOPES


def test_imap_load_credentials_prefers_new_yahoo_path(monkeypatch, tmp_path):
    new_path = tmp_path / "yahoo_credentials.json"
    legacy_path = tmp_path / "email_credentials.json"
    new_path.write_text(json.dumps({"yahoo": {"email": "new@example.com", "password": "new"}}))
    legacy_path.write_text(json.dumps({"yahoo": {"email": "old@example.com", "password": "old"}}))

    monkeypatch.setattr(imap_client, "YAHOO_CREDENTIALS_PATH", new_path)
    monkeypatch.setattr(imap_client, "LEGACY_CREDENTIALS_PATH", legacy_path)

    creds = imap_client.load_credentials("yahoo")
    assert creds["email"] == "new@example.com"


def test_imap_load_credentials_falls_back_to_legacy_path(monkeypatch, tmp_path):
    new_path = tmp_path / "yahoo_credentials.json"
    legacy_path = tmp_path / "email_credentials.json"
    legacy_path.write_text(json.dumps({"yahoo": {"email": "legacy@example.com", "password": "pw"}}))

    monkeypatch.setattr(imap_client, "YAHOO_CREDENTIALS_PATH", new_path)
    monkeypatch.setattr(imap_client, "LEGACY_CREDENTIALS_PATH", legacy_path)

    creds = imap_client.load_credentials("yahoo")
    assert creds["email"] == "legacy@example.com"
