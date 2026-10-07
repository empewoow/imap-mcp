"""Unified read-only email MCP server. Reads across all configured IMAP
mailboxes and exposes list/get/search tools to an MCP client."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import accounts as acct
from . import audit, imap

mcp = MCPServer("imap-mcp", version="0.1.0")


def _parse_since(since: str | None) -> dt.date | None:
    if not since:
        return None
    return dt.date.fromisoformat(since)  # raises ValueError on bad input


def _enabled(account: str) -> acct.Account:
    """The named account, refusing disabled ones."""
    a = acct.get_account(account)
    if not a.enabled:
        raise PermissionError(f"account {account!r} is disabled")
    return a


def _targets(account: str | None) -> list[acct.Account]:
    if account:
        a = acct.get_account(account)
        return [a] if a.enabled else []
    return acct.enabled_accounts()


@mcp.tool()
def list_accounts() -> list[dict]:
    """List configured mail accounts and whether each is reachable right now.

    Performs a live IMAP login per enabled account. Returns status so the caller
    knows which mailboxes are queryable.
    """
    out = []
    for a in acct.all_accounts():
        entry = {"account": a.key, "label": a.label, "email": a.email, "enabled": a.enabled}
        if not a.enabled:
            entry["status"] = "disabled"
        elif not a.password():
            entry["status"] = "no-credential"
        else:
            try:
                with imap.open_box(a):
                    entry["status"] = "ok"
            except Exception as e:  # noqa: BLE001 -- report, don't crash
                entry["status"] = f"unreachable: {type(e).__name__}"
        out.append(entry)
    return out


@mcp.tool()
def list_folders(account: str) -> list[str]:
    """List the selectable folders of one account (INBOX, Sent, Archive, ...).

    Use the names as `folder` in list_emails, search_emails, and get_email.
    """
    folders = imap.list_folders(_enabled(account))
    audit.record("list_folders", {"account": account}, folders)
    return folders


@mcp.tool()
def list_emails(
    account: str | None = None,
    folder: str = "INBOX",
    since: str | None = None,
    unread_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    """List recent emails in one folder, newest first.

    account: account key (see list_accounts). Omit to merge ALL enabled accounts.
    folder: folder name (see list_folders), default INBOX.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    unread_only: only unseen messages.
    limit: max rows (per account when merging).
    """
    since_d = _parse_since(since)
    rows: list[dict] = []
    for a in _targets(account):
        try:
            rows.extend(
                imap.fetch_rows(
                    a, folder=folder, since=since_d, unread_only=unread_only, limit=limit
                )
            )
        except Exception as e:  # noqa: BLE001 -- per-account fail-safe
            rows.append({"account": a.key, "error": f"{type(e).__name__}: {e}"})
    rows.sort(key=imap.date_key, reverse=True)
    args = {"account": account, "folder": folder, "since": since, "unread_only": unread_only}
    audit.record("list_emails", args | {"limit": limit}, rows)
    return rows


@mcp.tool()
def search_emails(
    query: str,
    account: str | None = None,
    folder: str | None = None,
    since: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search emails across from/subject/body text, newest first.

    query: free text; matched against sender, subject, and body (IMAP OR).
    account: omit to search ALL enabled accounts.
    folder: folder name (see list_folders). Omit to search ALL folders.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    limit: max rows (per account when merging).
    """
    since_d = _parse_since(since)
    rows: list[dict] = []
    for a in _targets(account):
        try:
            rows.extend(imap.fetch_rows(a, folder=folder, query=query, since=since_d, limit=limit))
        except Exception as e:  # noqa: BLE001
            rows.append({"account": a.key, "error": f"{type(e).__name__}: {e}"})
    rows.sort(key=imap.date_key, reverse=True)
    args = {"query": query, "account": account, "folder": folder, "since": since}
    audit.record("search_emails", args | {"limit": limit}, rows)
    return rows


@mcp.tool()
def get_email(account: str, id: str, folder: str = "INBOX") -> dict:
    """Fetch one full email (plain-text body preferred) by account + message id.

    id and folder are the values returned in list_emails/search_emails rows
    (IMAP UIDs are per folder). Reading does not mark the message as read.
    """
    a = _enabled(account)
    imap.check_uid(id)
    msg = imap.fetch_one(a, id, folder)
    if msg is None:
        msg = {"account": account, "folder": folder, "id": id, "error": f"not found in {folder}"}
    audit.record("get_email", {"account": account, "id": id, "folder": folder}, msg)
    return msg


@mcp.tool()
def save_attachment(account: str, id: str, index: int, folder: str = "INBOX") -> dict:
    """Save one attachment of an email to the local attachment directory.

    id, folder: as returned in list_emails/search_emails rows. index: the
    attachment's index from get_email's "attachments" list. Returns the saved
    file's path; read it with your own file tools. Only works when
    IMAP_MCP_ATTACHMENT_DIR is set. Nothing is opened or executed, and the
    mailbox is not changed.
    """
    args = {"account": account, "id": id, "index": index, "folder": folder}
    dest = os.environ.get("IMAP_MCP_ATTACHMENT_DIR")
    if not dest:
        out = {"error": "attachment saving is disabled (IMAP_MCP_ATTACHMENT_DIR is not set)"}
    else:
        try:
            a = _enabled(account)
            out = imap.save_attachment(a, id, index, folder, Path(dest).expanduser())
            if out is None:
                out = {"error": f"not found in {folder}"}
        except Exception as e:  # noqa: BLE001 -- report and audit every failure
            out = {"error": f"{type(e).__name__}: {e}"}
    out = {"account": account, "folder": folder, "id": id} | out
    audit.record("save_attachment", args, out)
    return out


def main() -> None:
    mcp.run()
