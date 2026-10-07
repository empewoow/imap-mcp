"""Read-only IMAP helpers built on imap-tools.

Every folder is selected with EXAMINE (readonly=True), so the server itself
rejects any change to the mailbox. Every fetch also uses mark_seen=False, so
imap-tools issues BODY.PEEK and reading a message never sets the \\Seen flag.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from imap_tools import AND, OR, MailBox

from .accounts import Account

# Folders with these flags cannot be selected (containers, placeholders).
# IMAP flags are case-insensitive, so all flag checks compare lowercase.
_UNSELECTABLE = {"\\noselect", "\\nonexistent"}

# Skipped when searching all folders: virtual folders that repeat messages from
# elsewhere (Gmail All Mail, Starred, Important) and spam.
_SKIP_IN_ALL = {"\\all", "\\flagged", "\\important", "\\junk"}

_EPOCH = dt.datetime.min.replace(tzinfo=dt.UTC)

# Characters kept in saved attachment file names; everything else becomes "_".
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._() -]")


@contextmanager
def open_box(account: Account, folder: str | None = "INBOX") -> Iterator[MailBox]:
    """Yield a logged-in mailbox with `folder` selected read-only (None selects
    nothing). Raises on failure."""
    pw = account.password()
    if not pw:
        raise RuntimeError(f"no password for {account.key} (env {account.password_env} unset)")
    # Google shows app passwords with spaces for readability; IMAP wants them bare.
    if account.host.endswith("gmail.com"):
        pw = pw.replace(" ", "")
    box = MailBox(account.host, port=account.port)
    box.login(account.email, pw, initial_folder=None)
    try:
        if folder is not None:
            box.folder.set(folder, readonly=True)
        yield box
    finally:
        with suppress(Exception):
            box.logout()


def _folders(box: MailBox, skip: set[str] = frozenset()) -> list[str]:
    """Selectable folder names, without duplicates, minus folders flagged in `skip`."""
    names = []
    for f in box.folder.list():
        flags = {fl.lower() for fl in f.flags}
        if not flags & (_UNSELECTABLE | skip):
            names.append(f.name)
    return list(dict.fromkeys(names))


def list_folders(account: Account) -> list[str]:
    """Names of all selectable folders in the account."""
    with open_box(account, folder=None) as box:
        return _folders(box)


def date_key(r: dict) -> dt.datetime:
    """Sort key for rows: the real moment in UTC, not the ISO string, so mixed
    UTC offsets order correctly. Rows without a date sort last."""
    if not r.get("date"):
        return _EPOCH
    d = dt.datetime.fromisoformat(r["date"])
    return d if d.tzinfo else d.replace(tzinfo=dt.UTC)


def _snippet(msg, length: int = 200) -> str:
    body = (msg.text or msg.html or "").strip().replace("\r", " ").replace("\n", " ")
    return body[:length]


def _message_id(msg) -> str | None:
    return (msg.headers.get("message-id") or (None,))[0]


def row(msg, account_key: str, folder: str = "INBOX") -> dict:
    """Compact metadata row for list/search results."""
    return {
        "account": account_key,
        "folder": folder,
        "id": msg.uid,
        "message_id": _message_id(msg),
        "from": msg.from_,
        "to": list(msg.to),
        "subject": msg.subject,
        "date": msg.date.isoformat() if msg.date else None,
        "unread": "\\Seen" not in msg.flags,
        "snippet": _snippet(msg),
    }


def _criteria(query: str | None, since: dt.date | None, unread_only: bool):
    parts = []
    if query:
        parts.append(OR(from_=query, subject=query, text=query))
    if since:
        parts.append(AND(date_gte=since))
    if unread_only:
        parts.append(AND(seen=False))
    if not parts:
        return AND(all=True)
    return AND(*parts) if len(parts) > 1 else parts[0]


def _fetch_folder(
    box: MailBox, account_key: str, folder: str, crit, limit: int, headers_only: bool = False
) -> list[dict]:
    box.folder.set(folder, readonly=True)
    msgs = box.fetch(
        crit,
        limit=limit,
        reverse=True,  # newest first
        mark_seen=False,  # BODY.PEEK -- never touch \Seen
        bulk=True,
        headers_only=headers_only,
    )
    return [row(m, account_key, folder) for m in msgs]


def _fill_snippets(box: MailBox, rows: list[dict]) -> None:
    """Fetch bodies only for the rows that survived the trim, to set snippets."""
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_folder[r["folder"]].append(r)
    for folder, folder_rows in by_folder.items():
        with suppress(Exception):  # rows keep an empty snippet on failure
            box.folder.set(folder, readonly=True)
            uids = [r["id"] for r in folder_rows]
            msgs = box.fetch(AND(uid=uids), mark_seen=False, bulk=True)
            snippets = {m.uid: _snippet(m) for m in msgs}
            for r in folder_rows:
                r["snippet"] = snippets.get(r["id"], "")


def _dedupe(rows: list[dict]) -> list[dict]:
    """Drop repeats of the same message (same Message-ID) from other folders."""
    seen: set[str] = set()
    out = []
    for r in rows:
        mid = r.get("message_id")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        out.append(r)
    return out


def fetch_rows(
    account: Account,
    *,
    folder: str | None = "INBOX",
    query: str | None = None,
    since: dt.date | None = None,
    unread_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    """Rows from one folder, or from every searchable folder when folder is None.

    Across folders only headers are fetched first; after merging, removing
    duplicates and keeping the newest `limit` rows, bodies are fetched for just
    those rows. A folder that fails comes back as an error row and the other
    folders still answer.
    """
    crit = _criteria(query, since, unread_only)
    with open_box(account, folder=None) as box:
        if folder is not None:
            return _fetch_folder(box, account.key, folder, crit, limit)
        rows: list[dict] = []
        errors: list[dict] = []
        for name in _folders(box, skip=_SKIP_IN_ALL):
            try:
                rows.extend(_fetch_folder(box, account.key, name, crit, limit, headers_only=True))
            except Exception as e:  # noqa: BLE001 -- per-folder fail-safe
                errors.append(
                    {"account": account.key, "folder": name, "error": f"{type(e).__name__}: {e}"}
                )
        rows.sort(key=date_key, reverse=True)
        rows = _dedupe(rows)[:limit]
        _fill_snippets(box, rows)
    return rows + errors


def _attachment_list(msg) -> list[dict]:
    return [
        {"index": i, "filename": a.filename, "content_type": a.content_type, "size": a.size}
        for i, a in enumerate(msg.attachments)
    ]


def check_uid(uid: str) -> None:
    """Reject anything but a single numeric UID (no ranges, lists or `*`)."""
    if not re.fullmatch(r"[0-9]+", uid):
        raise ValueError(f"id must be a single numeric IMAP UID, got {uid!r}")


def _fetch_message(account: Account, uid: str, folder: str):
    check_uid(uid)
    with open_box(account, folder=folder) as box:
        return next(iter(box.fetch(AND(uid=uid), mark_seen=False, bulk=False, limit=1)), None)


def _name_part(text: str, max_len: int) -> str:
    """One piece of a file name: unsafe characters (including / and \\) become _."""
    return _UNSAFE_NAME_CHARS.sub("_", text)[:max_len]


def safe_filename(name: str | None, max_len: int = 120) -> str:
    """A plain file name: no directories, no characters outside a small safe
    set, at most max_len characters with the extension kept."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip(" .")
    if not base:
        return "attachment"
    if len(base) > max_len:
        suffix = Path(base).suffix[:16]
        base = base[: max_len - len(suffix)].rstrip(" .") + suffix
    return base


def save_attachment(
    account: Account, uid: str, index: int, folder: str, dest_dir: Path
) -> dict | None:
    """Write attachment `index` of a message into dest_dir. Returns None if the
    message is not found; raises IndexError for a bad index.

    The file name is built from account, folder, uid and index plus the cleaned
    original name, and is checked to resolve inside dest_dir. Nothing is opened
    or executed.
    """
    m = _fetch_message(account, uid, folder)
    if m is None:
        return None
    attachments = list(m.attachments)
    if not 0 <= index < len(attachments):
        raise IndexError(
            f"message has {len(attachments)} attachment(s); index {index} is out of range"
        )
    att = attachments[index]
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{_name_part(account.key, 40)}-{_name_part(folder, 60)}-{m.uid}-{index}"
    # Not resolved: the name has no separators, and a link at that name must
    # be replaced by os.replace below, not followed.
    path = dest_dir / f"{prefix}-{safe_filename(att.filename, 80)}"
    if path.parent != dest_dir:
        raise ValueError("attachment path escapes the attachment directory")
    # Write a temp file and rename it over the target, so an existing link at
    # that name is replaced instead of written through.
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(att.payload)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    return {
        "account": account.key,
        "folder": folder,
        "id": m.uid,
        "index": index,
        "filename": att.filename,
        "content_type": att.content_type,
        "size": len(att.payload),
        "path": str(path),
    }


def fetch_one(account: Account, uid: str, folder: str = "INBOX") -> dict | None:
    """Full message by UID in `folder`. Returns None if not found."""
    m = _fetch_message(account, uid, folder)
    if m is None:
        return None
    return {
        "account": account.key,
        "folder": folder,
        "id": m.uid,
        "from": m.from_,
        "to": list(m.to),
        "cc": list(m.cc),
        "subject": m.subject,
        "date": m.date.isoformat() if m.date else None,
        "unread": "\\Seen" not in m.flags,
        "message_id": _message_id(m),
        "body": (m.text or m.html or "").strip(),
        "attachments": _attachment_list(m),
    }
