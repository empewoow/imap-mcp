import datetime as dt
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, imap


class FakeFolders:
    """Stands in for MailBox.folder; records every select and its mode."""

    def __init__(self, box):
        self.box = box
        self.selected = []

    def set(self, folder, readonly=False):
        if folder in FakeMailBox.failing:
            raise RuntimeError(f"cannot select {folder}")
        self.selected.append((folder, readonly))
        self.box.current = folder
        return ("OK", [])

    def list(self):
        return [SimpleNamespace(name=n, flags=f) for n, f in FakeMailBox.folders]


class FakeMailBox:
    """Stands in for imap_tools.MailBox; records login/fetch args, no network."""

    instances = []
    messages = []
    folders = [("INBOX", ())]
    failing = set()

    def __init__(self, host, port=993):
        self.host = host
        self.port = port
        self.login_args = None
        self.fetch_kwargs = None
        self.logged_out = False
        self.current = None
        self.fetches = []
        self.folder = FakeFolders(self)
        FakeMailBox.instances.append(self)

    def login(self, email, password, initial_folder="INBOX"):
        self.login_args = (email, password, initial_folder)
        return self

    def fetch(self, criteria, **kwargs):
        self.fetch_criteria = criteria
        self.fetch_kwargs = kwargs
        msgs = FakeMailBox.messages
        if isinstance(msgs, dict):  # per-folder messages
            msgs = msgs.get(self.current, [])
        if kwargs.get("headers_only"):
            msgs = [SimpleNamespace(**{**vars(m), "text": "", "html": ""}) for m in msgs]
        self.fetches.append((self.current, kwargs))
        return iter(msgs)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def fake_mailbox(monkeypatch):
    FakeMailBox.instances = []
    FakeMailBox.messages = []
    FakeMailBox.folders = [("INBOX", ())]
    FakeMailBox.failing = set()
    monkeypatch.setattr(imap, "MailBox", FakeMailBox)


def fake_msg(**overrides):
    base = dict(
        uid="42",
        from_="sender@example.com",
        to=("rcpt@example.com",),
        cc=(),
        subject="Hello",
        date=dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC),
        flags=("\\Seen",),
        text="line one\nline two",
        html="",
        headers={"message-id": ["<abc@example.com>"]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def msg_on(uid, day, mid=None):
    """A message dated 2026-07-<day> with its own Message-ID."""
    return fake_msg(
        uid=uid,
        date=dt.datetime(2026, 7, day, tzinfo=dt.UTC),
        headers={"message-id": [mid or f"<{uid}@example.com>"]},
    )


def test_open_box_requires_password(config):
    alpha = accounts.get_account("alpha")
    with pytest.raises(RuntimeError, match="ALPHA_PW"), imap.open_box(alpha):
        pass


def test_open_box_logs_in_and_out(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "s3cret")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args == ("alpha@example.com", "s3cret", None)
        assert box.folder.selected == [("INBOX", True)]
        assert (box.host, box.port) == ("imap.example.com", 1993)
        assert not box.logged_out
    assert box.logged_out


def test_open_box_strips_spaces_for_gmail_only(config, monkeypatch):
    monkeypatch.setenv("BETA_PW", "abcd efgh ijkl mnop")
    with imap.open_box(accounts.get_account("gmailish")) as box:
        assert box.login_args[1] == "abcdefghijklmnop"

    monkeypatch.setenv("ALPHA_PW", "pass with spaces")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args[1] == "pass with spaces"


def test_open_box_logs_out_when_select_fails(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.failing = {"Nope"}
    with pytest.raises(RuntimeError), imap.open_box(accounts.get_account("alpha"), "Nope"):
        pass
    assert FakeMailBox.instances[-1].logged_out


def test_open_box_without_folder_selects_nothing(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    with imap.open_box(accounts.get_account("alpha"), folder=None) as box:
        assert box.folder.selected == []


def test_list_folders_skips_unselectable(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [
        ("INBOX", ()),
        ("[Gmail]", ("\\Noselect",)),
        ("Sent", ("\\Sent",)),
        ("Gone", ("\\NonExistent",)),
    ]
    assert imap.list_folders(accounts.get_account("alpha")) == ["INBOX", "Sent"]


def test_list_folders_flags_are_case_insensitive_and_names_unique(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [
        ("INBOX", ()),
        ("INBOX", ()),
        ("Parent", ("\\NoSelect",)),
        ("Ghost", ("\\NONEXISTENT",)),
    ]
    assert imap.list_folders(accounts.get_account("alpha")) == ["INBOX"]


def test_date_key_orders_by_real_moment():
    rows = [
        {"id": "late", "date": "2026-07-01T08:30:00+00:00"},
        {"id": "early", "date": "2026-07-01T09:00:00+02:00"},  # 07:00 UTC
        {"id": "naive", "date": "1900-01-01T00:00:00"},
        {"id": "none", "date": None},
    ]
    rows.sort(key=imap.date_key, reverse=True)
    assert [r["id"] for r in rows] == ["late", "early", "naive", "none"]


def test_snippet_collapses_newlines_and_truncates():
    msg = fake_msg(text="a\nb\nc" + "x" * 500)
    snip = imap._snippet(msg)
    assert snip.startswith("a b c")
    assert len(snip) == 200


def test_snippet_falls_back_to_html():
    msg = fake_msg(text="", html="<p>hi</p>")
    assert imap._snippet(msg) == "<p>hi</p>"


def test_row_shape():
    msg = fake_msg(flags=())
    assert imap.row(msg, "alpha", "Sent") == {
        "account": "alpha",
        "folder": "Sent",
        "id": "42",
        "message_id": "<abc@example.com>",
        "from": "sender@example.com",
        "to": ["rcpt@example.com"],
        "subject": "Hello",
        "date": "2026-07-30T12:00:00+00:00",
        "unread": True,
        "snippet": "line one line two",
    }


def test_row_seen_flag_means_read():
    assert imap.row(fake_msg(), "alpha")["unread"] is False


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(query=None, since=None, unread_only=False), "(ALL)"),
        (dict(query=None, since=dt.date(2026, 7, 1), unread_only=False), "(SINCE 1-Jul-2026)"),
        (dict(query=None, since=None, unread_only=True), "(UNSEEN)"),
    ],
)
def test_criteria_simple(kwargs, expected):
    assert str(imap._criteria(**kwargs)) == expected


def test_criteria_query_matches_from_subject_body():
    crit = str(imap._criteria("invoice", None, False))
    for token in ("FROM", "SUBJECT", "TEXT", "invoice"):
        assert token in crit


def test_criteria_combines_with_and():
    crit = str(imap._criteria("invoice", dt.date(2026, 7, 1), True))
    for token in ("invoice", "SINCE 1-Jul-2026", "UNSEEN"):
        assert token in crit


def test_fetch_rows_never_marks_seen(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg()]
    rows = imap.fetch_rows(accounts.get_account("alpha"), limit=7)

    assert [r["id"] for r in rows] == ["42"]
    assert (rows[0]["account"], rows[0]["folder"]) == ("alpha", "INBOX")
    box = FakeMailBox.instances[-1]
    assert box.folder.selected == [("INBOX", True)]
    kwargs = box.fetch_kwargs
    assert kwargs["mark_seen"] is False
    assert kwargs["reverse"] is True
    assert kwargs["limit"] == 7


def test_fetch_rows_all_folders_merges_newest_first(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [("INBOX", ()), ("Box", ("\\Noselect",)), ("Sent", ())]
    FakeMailBox.messages = {
        "INBOX": [msg_on("1", 1)],
        "Sent": [msg_on("7", 3), msg_on("8", 2)],
    }
    rows = imap.fetch_rows(accounts.get_account("alpha"), folder=None, limit=2)

    assert [(r["folder"], r["id"]) for r in rows] == [("Sent", "7"), ("Sent", "8")]
    box = FakeMailBox.instances[-1]
    # headers pass over every folder, then a body pass only where rows survived
    assert box.folder.selected == [("INBOX", True), ("Sent", True), ("Sent", True)]
    assert [kw.get("headers_only") for _, kw in box.fetches] == [True, True, None]
    assert all(kw["mark_seen"] is False for _, kw in box.fetches)
    assert rows[0]["snippet"] == "line one line two"


def test_fetch_rows_all_folders_skips_virtual_and_spam(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [
        ("INBOX", ()),
        ("[Gmail]/All Mail", ("\\All",)),
        ("[Gmail]/Starred", ("\\Flagged",)),
        ("[Gmail]/Important", ("\\Important",)),
        ("Spam", ("\\Junk",)),
        ("Trash", ("\\Trash",)),
    ]
    imap.fetch_rows(accounts.get_account("alpha"), folder=None)
    picked = [f for f, _ in FakeMailBox.instances[-1].folder.selected]
    assert "INBOX" in picked and "Trash" in picked
    assert not {"[Gmail]/All Mail", "[Gmail]/Starred", "[Gmail]/Important", "Spam"} & set(picked)


def test_fetch_rows_all_folders_drops_duplicate_message_ids(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [("INBOX", ()), ("Archive", ())]
    FakeMailBox.messages = {
        "INBOX": [msg_on("1", 2, mid="<same@x>")],
        "Archive": [msg_on("9", 2, mid="<same@x>"), msg_on("5", 1)],
    }
    rows = imap.fetch_rows(accounts.get_account("alpha"), folder=None)
    assert [(r["folder"], r["id"]) for r in rows] == [("INBOX", "1"), ("Archive", "5")]


def test_fetch_rows_folder_failure_is_isolated(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.folders = [("INBOX", ()), ("Broken", ())]
    FakeMailBox.failing = {"Broken"}
    FakeMailBox.messages = {"INBOX": [fake_msg()]}
    rows = imap.fetch_rows(accounts.get_account("alpha"), folder=None)

    assert [r["id"] for r in rows if "error" not in r] == ["42"]
    assert [r for r in rows if "error" in r] == [
        {"account": "alpha", "folder": "Broken", "error": "RuntimeError: cannot select Broken"}
    ]


def test_fetch_rows_single_folder_failure_raises(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.failing = {"Nope"}
    with pytest.raises(RuntimeError, match="Nope"):
        imap.fetch_rows(accounts.get_account("alpha"), folder="Nope")


def test_fetch_one_full_message(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = [fake_msg()]
    msg = imap.fetch_one(accounts.get_account("alpha"), "42", "Sent")

    assert msg == {
        "account": "alpha",
        "folder": "Sent",
        "id": "42",
        "from": "sender@example.com",
        "to": ["rcpt@example.com"],
        "cc": [],
        "subject": "Hello",
        "date": "2026-07-30T12:00:00+00:00",
        "unread": False,
        "message_id": "<abc@example.com>",
        "body": "line one\nline two",
    }
    box = FakeMailBox.instances[-1]
    assert box.folder.selected == [("Sent", True)]
    assert box.fetch_kwargs["mark_seen"] is False


def test_fetch_one_not_found(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    FakeMailBox.messages = []
    assert imap.fetch_one(accounts.get_account("alpha"), "42") is None
