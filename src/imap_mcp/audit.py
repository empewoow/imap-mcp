"""Optional audit log of what the MCP client read.

When IMAP_MCP_AUDIT_LOG names a file, every mail tool call appends one JSON
line: time, tool, arguments, and the messages it returned (account, folder, id,
sender, subject, date). Message bodies and snippets are never logged.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import suppress
from pathlib import Path

_FIELDS = ("account", "folder", "id", "from", "subject", "date", "error")


def record(tool: str, args: dict, result: object) -> None:
    """Append one line for this call; a no-op when the log is not configured."""
    path = os.environ.get("IMAP_MCP_AUDIT_LOG")
    if not path:
        return
    rows = result if isinstance(result, list) else [result]
    entry = {
        "time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "tool": tool,
        "args": args,
        "messages": [{k: r[k] for k in _FIELDS if k in r} for r in rows if isinstance(r, dict)],
    }
    # Never let a logging problem break a read.
    with suppress(Exception), Path(path).expanduser().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
