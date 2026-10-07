import json

from imap_mcp import audit, imap, server


def test_record_is_noop_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAP_MCP_AUDIT_LOG", raising=False)
    audit.record("get_email", {}, {"id": "1"})
    assert list(tmp_path.iterdir()) == []


def test_record_logs_metadata_never_bodies(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("IMAP_MCP_AUDIT_LOG", str(log))
    msg = {
        "account": "alpha",
        "folder": "Sent",
        "id": "42",
        "from": "a@example.com",
        "subject": "Hi",
        "date": "2026-07-30T12:00:00+00:00",
        "snippet": "secret snippet",
        "body": "secret body",
    }
    audit.record("get_email", {"account": "alpha", "id": "42"}, msg)
    audit.record("search_emails", {"query": "x"}, [msg, {"account": "beta", "error": "E"}])

    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["tool"] for e in lines] == ["get_email", "search_emails"]
    assert lines[0]["args"] == {"account": "alpha", "id": "42"}
    assert lines[0]["messages"] == [
        {
            "account": "alpha",
            "folder": "Sent",
            "id": "42",
            "from": "a@example.com",
            "subject": "Hi",
            "date": "2026-07-30T12:00:00+00:00",
        }
    ]
    assert lines[1]["messages"][1] == {"account": "beta", "error": "E"}
    assert "secret" not in log.read_text(encoding="utf-8")


def test_record_survives_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_MCP_AUDIT_LOG", str(tmp_path))  # a directory, not a file
    audit.record("get_email", {}, {"id": "1"})  # must not raise


def test_tools_write_audit_lines(config, tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("IMAP_MCP_AUDIT_LOG", str(log))
    row = {"account": "alpha", "folder": "INBOX", "id": "1", "date": "2026-07-01"}
    monkeypatch.setattr(imap, "fetch_rows", lambda a, **kw: [dict(row, account=a.key)])
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid, folder: dict(row, id=uid))
    monkeypatch.setattr(imap, "list_folders", lambda a: ["INBOX"])

    server.list_folders("alpha")
    server.list_emails(account="alpha")
    server.search_emails("invoice", account="alpha")
    server.get_email("alpha", "7")

    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["tool"] for e in lines] == [
        "list_folders",
        "list_emails",
        "search_emails",
        "get_email",
    ]
    assert lines[2]["args"]["query"] == "invoice"
    assert lines[3]["messages"][0]["id"] == "7"
