"""
LINE Summary MCP Server.
3 tools: line_list_chats, line_get_history, line_get_contacts.

SECURITY: Key cached in process memory only -- never returned by any tool.
"""
import glob
import json
import os
import re
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from db_reader import DbReader
from key_extractor import extract_key

mcp = FastMCP("line-summary")

_DEFAULTS = {
    "db_path": "",
    "output_dir": os.path.expanduser("~/line-summary/output"),
    "media_mode": "placeholder",
    "url_extraction": True,
    "timezone": "Asia/Taipei",
}

_reader: DbReader | None = None


def _load_settings() -> dict:
    try:
        with open("settings.json", encoding="utf-8") as f:
            return {**_DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        return dict(_DEFAULTS)


_MAIN_EDB_RE = re.compile(r'^[0-9a-f]+$')


def _find_edb_path(data_dir: str | None = None) -> str | None:
    if data_dir is None:
        data_dir = os.path.join(
            os.path.expandvars("%LOCALAPPDATA%"), "LINE", "Data", "db"
        )
    all_edb = [
        p for p in glob.glob(os.path.join(data_dir, "*.edb"))
        if not (p.endswith("-shm") or p.endswith("-wal"))
    ]
    mains = [p for p in all_edb
             if _MAIN_EDB_RE.match(os.path.splitext(os.path.basename(p))[0])]
    candidates = mains if mains else all_edb
    return max(candidates, key=os.path.getsize) if candidates else None


def _parse_iso8601(value: str) -> int:
    """Parse ISO 8601 with explicit timezone to Unix seconds."""
    if not isinstance(value, str) or not any(c in value for c in ('+', 'Z')):
        raise ValueError(
            f"Invalid ISO 8601 (must include timezone offset): '{value}'"
        )
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f"Invalid ISO 8601 format: '{value}'")
    if dt.tzinfo is None:
        raise ValueError(f"Missing timezone in: '{value}'")
    return int(dt.timestamp())


def _get_reader() -> DbReader:
    global _reader
    if _reader is not None:
        return _reader
    settings = _load_settings()
    db_path = settings["db_path"] or _find_edb_path()
    if not db_path:
        raise RuntimeError("LINE .edb not found. Set db_path in settings.json.")
    key = extract_key(db_path, require_consent=True)
    if not key:
        raise RuntimeError("User declined or key extraction failed.")
    _reader = DbReader(db_path, key)
    return _reader


@mcp.tool()
def line_list_chats(
    query: str = "",
    chat_type: str = "",
    limit: int = 50,
) -> list[dict]:
    """List ALL LINE chats: personal, group, multi-person, official account, open chat.

    Args:
        query: Fuzzy match on chat name (optional)
        chat_type: Filter by type -- "personal", "group", "multi", "official", "open"
                   Leave empty to return all types.
        limit: Max results (default 50)

    Returns: [{chat_id, name, type, type_label, member_count, last_message_at}]
    type_label is human-readable: 個人/群組/多人聊天/官方帳號/開放聊天室
    """
    return _get_reader().list_chats(query=query, chat_type=chat_type, limit=limit)


@mcp.tool()
def line_get_history(
    chat_id: str,
    since: str,
    until: str,
    limit: int = 500,
) -> list[dict]:
    """Get LINE chat message history.

    Args:
        chat_id: From line_list_chats
        since: ISO 8601 with timezone, e.g. 2026-06-15T00:00:00+08:00
        until: ISO 8601 with timezone
        limit: Max messages (default 500)

    Returns: [{type, sender, content, urls, sent_at, ...}]
    """
    return _get_reader().get_history(
        chat_id=chat_id,
        since_ts=_parse_iso8601(since),
        until_ts=_parse_iso8601(until),
        limit=limit,
    )


@mcp.tool()
def line_get_contacts(query: str = "") -> list[dict]:
    """Get LINE contacts for name resolution.
    Returns: [{contact_id, display_name}]
    """
    return _get_reader().get_contacts(query=query)


if __name__ == "__main__":
    mcp.run()
