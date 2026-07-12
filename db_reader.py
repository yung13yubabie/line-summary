"""
Reads LINE PC wxSQLite3-encrypted .edb.

ENGINE (confirmed 2026-07-11): LINE uses **wxSQLite3**. Decryption goes through
apsw + SQLite3MultipleCiphers with scheme **aes128cbc** + passphrase — NOT Zetetic
sqlcipher3 (incompatible; the old path failed HMAC on every key). See FINDINGS.md.

SCHEMA (confirmed 2026-07-11 against LINE 26.3, see spike/real_schema.md):
Tables are `_`-prefixed. Chat names are resolved across _groupChat / _contact /
_room / _squareChat. Message rows live in _message keyed by _chatId, typed by
_contentType. The old chat/message/contact + sender_id/sent_at guesses were wrong.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

# -- Cipher config: CONFIRMED via phase0.py against LINE 26.3 ------------------
_CIPHER_SCHEME = "aes128cbc"
_KEY_MODE = "pass"

# -- Real tables ---------------------------------------------------------------
_T_CHAT = "_chat"
_T_MESSAGE = "_message"
_T_CONTACT = "_contact"
_T_GROUP = "_groupChat"
_T_ROOM = "_room"
_T_SQUARE = "_squareChat"
_T_SQUARE_MEMBER = "_squareMember"

# LINE message _contentType codes (protocol; refine from real distribution).
_CONTENT_TYPE: dict[int, str] = {
    0: "text", 1: "image", 2: "video", 3: "audio",
    6: "location", 7: "sticker", 13: "contact", 14: "file", 16: "link",
}

_URL_RE = re.compile(r'https?://[^\s、-￿]+')
_TZ_TAIPEI = timezone(timedelta(hours=8))


def _dict_row(cursor: Any, row: tuple) -> dict:
    """apsw rowtrace: map each row to a dict keyed by column name."""
    return {d[0]: v for d, v in zip(cursor.getdescription(), row)}


def _open_encrypted(db_path: str, key: str) -> Any:
    """Open a wxSQLite3-encrypted .edb read-only via apsw + SQLite3MultipleCiphers."""
    import apsw
    conn = apsw.Connection(db_path, flags=apsw.SQLITE_OPEN_READONLY)
    conn.execute(f"PRAGMA cipher='{_CIPHER_SCHEME}';")
    if _KEY_MODE == "hex":
        conn.execute(f"PRAGMA hexkey='{key}';")
    else:
        conn.execute(f"PRAGMA key='{key}';")
    conn.setrowtrace(_dict_row)
    return conn


def probe_key(db_path: str, key: str) -> bool:
    """Return True if key decrypts the .edb under the configured wxSQLite3 scheme.
    Called by key_extractor to validate memory candidates."""
    try:
        conn = _open_encrypted(db_path, key)
        next(conn.execute("SELECT count(*) FROM sqlite_master;"))
        conn.close()
        return True
    except Exception:
        return False


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text)


def _ts_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    # LINE _createdTime is epoch milliseconds (13-digit).
    seconds = ts / 1000 if ts > 1_000_000_000_000 else ts
    return datetime.fromtimestamp(seconds, tz=_TZ_TAIPEI).isoformat()


def _meta(row: dict[str, Any]) -> dict:
    raw = row.get("_contentMetadata")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_message_row(row: dict[str, Any], contact_map: dict[str, str]) -> dict:
    """Map a raw _message row to a summary-friendly dict.
    Real columns: _from, _createdTime, _text, _contentType, _contentMetadata."""
    sender = contact_map.get(row.get("_from"), row.get("_from"))
    ct = row.get("_contentType", 0) or 0
    sent = _ts_to_iso(row.get("_createdTime"))
    kind = _CONTENT_TYPE.get(ct, f"type_{ct}")
    meta = _meta(row)

    if kind == "text":
        content = row.get("_text") or ""
        return {"type": "text", "sender": sender, "content": content,
                "urls": extract_urls_from_text(content), "sent_at": sent}
    if kind == "image":
        return {"type": "image", "sender": sender, "content": None, "sent_at": sent}
    if kind == "sticker":
        return {"type": "sticker", "sender": sender, "content": "[貼圖]", "sent_at": sent}
    if kind == "file":
        return {"type": "file", "sender": sender, "content": None,
                "filename": meta.get("FILE_NAME") or meta.get("fileName"), "sent_at": sent}
    if kind == "link":
        return {"type": "link", "sender": sender,
                "url": meta.get("url") or meta.get("linkUrl"),
                "title": meta.get("title"), "description": meta.get("desc"),
                "sent_at": sent}
    if kind in ("video", "audio", "location", "contact"):
        return {"type": kind, "sender": sender, "content": row.get("_text"),
                "sent_at": sent}
    # Unknown content type: surface the code rather than silently dropping it.
    return {"type": kind, "sender": sender, "content": row.get("_text"),
            "sent_at": sent}


class DbReader:
    def __init__(self, db_path: str, key: str | None, _test_mode: bool = False):
        self._db_path = db_path
        self._key = key
        self._test_mode = _test_mode

    def _open(self):
        if self._test_mode or self._key is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            return conn
        return _open_encrypted(self._db_path, self._key)

    # -- name resolution -------------------------------------------------------
    def _name_maps(self, conn) -> dict[str, dict[str, str]]:
        """Build id->name maps for each chat kind (one pass each)."""
        def safe(sql: str) -> list:
            try:
                return list(conn.execute(sql))
            except Exception:
                return []
        groups = {r["_chatMid"]: r["_chatName"]
                  for r in safe(f"SELECT _chatMid, _chatName FROM {_T_GROUP};")}
        squares = {r["_squareChatMid"]: r["_name"]
                   for r in safe(f"SELECT _squareChatMid, _name FROM {_T_SQUARE};")}
        contacts = {
            r["_mid"]: (r["_displayNameOverridden"] or r["_displayName"])
            for r in safe(
                f"SELECT _mid, _displayName, _displayNameOverridden FROM {_T_CONTACT};"
            )
        }
        rooms = {r["_mid"]: r for r in safe(f"SELECT _mid FROM {_T_ROOM};")}
        return {"group": groups, "square": squares,
                "contact": contacts, "room": rooms}

    def _resolve_chat(self, chat_id: str, maps: dict) -> tuple[str, str]:
        """Return (display_name, chat_type) for a _chat._id."""
        if chat_id in maps["group"]:
            return maps["group"][chat_id] or chat_id, "group"
        if chat_id in maps["square"]:
            return maps["square"][chat_id] or chat_id, "open"
        if chat_id in maps["room"]:
            return "多人聊天", "multi"
        if chat_id in maps["contact"]:
            return maps["contact"][chat_id] or chat_id, "personal"
        return chat_id, "unknown"

    def _contacts_map(self, conn) -> dict[str, str]:
        """mid -> display name. Covers friends (_contact) AND OpenChat members
        (_squareMember), since square senders are not in _contact."""
        def safe(sql: str) -> list:
            try:
                return list(conn.execute(sql))
            except Exception:
                return []
        m = {r["_mid"]: (r["_displayNameOverridden"] or r["_displayName"])
             for r in safe(
                 f"SELECT _mid, _displayName, _displayNameOverridden FROM {_T_CONTACT};"
             )}
        for r in safe(
            f"SELECT _squareMemberMid, _displayName FROM {_T_SQUARE_MEMBER};"
        ):
            m.setdefault(r["_squareMemberMid"], r["_displayName"])
        return m

    # -- public API ------------------------------------------------------------
    def list_chats(
        self, query: str = "", chat_type: str = "", limit: int = 50
    ) -> list[dict]:
        conn = self._open()
        try:
            maps = self._name_maps(conn)
            rows = list(conn.execute(
                f"SELECT _id, _lastUpdatedTime FROM {_T_CHAT} "
                f"ORDER BY _lastUpdatedTime DESC;"
            ))
            out = []
            for r in rows:
                name, ctype = self._resolve_chat(r["_id"], maps)
                if chat_type and ctype != chat_type:
                    continue
                if query and query.lower() not in (name or "").lower():
                    continue
                out.append({
                    "chat_id": r["_id"],
                    "name": name,
                    "type": ctype,
                    "last_message_at": _ts_to_iso(r["_lastUpdatedTime"]),
                })
                if len(out) >= limit:
                    break
            return out
        finally:
            conn.close()

    def get_history(
        self, chat_id: str, since_ts: int, until_ts: int, limit: int = 500
    ) -> list[dict]:
        conn = self._open()
        try:
            contact_map = self._contacts_map(conn)
            # mcp passes bounds in seconds; _createdTime is milliseconds.
            since_ms, until_ms = since_ts * 1000, until_ts * 1000
            rows = list(conn.execute(
                f"SELECT * FROM {_T_MESSAGE} "
                f"WHERE _chatId=? AND _createdTime>=? AND _createdTime<=? "
                f"ORDER BY _createdTime ASC LIMIT ?;",
                (chat_id, since_ms, until_ms, limit)
            ))
            return [parse_message_row(dict(r), contact_map) for r in rows]
        finally:
            conn.close()

    def get_contacts(self, query: str = "") -> list[dict]:
        conn = self._open()
        try:
            if query:
                rows = list(conn.execute(
                    f"SELECT _mid, _displayName, _displayNameOverridden "
                    f"FROM {_T_CONTACT} WHERE _displayName LIKE ? "
                    f"OR _displayNameOverridden LIKE ?;",
                    (f"%{query}%", f"%{query}%")
                ))
            else:
                rows = list(conn.execute(
                    f"SELECT _mid, _displayName, _displayNameOverridden "
                    f"FROM {_T_CONTACT};"
                ))
            return [{"contact_id": r["_mid"],
                     "display_name": r["_displayNameOverridden"] or r["_displayName"]}
                    for r in rows]
        finally:
            conn.close()
