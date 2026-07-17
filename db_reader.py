"""
Reads LINE PC wxSQLite3-encrypted .edb.

ENGINE (confirmed 2026-07-11): LINE uses **wxSQLite3**. Decryption goes through
apsw + SQLite3MultipleCiphers with scheme **aes128cbc** + passphrase — NOT Zetetic
sqlcipher3 (incompatible; the old path failed HMAC on every key). See FINDINGS.md.

SCHEMA (confirmed against LINE 26.3):
Tables are `_`-prefixed. Chat names are resolved across _groupChat / _contact /
_room / _squareChat. Message rows live in _message keyed by _chatId, typed by
_contentType. The old chat/message/contact + sender_id/sent_at guesses were wrong.
"""
import json
import os
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

# Official/bot account _contact._type codes -- their unread is mostly marketing
# pushes, so they are excluded from the unread list by default. PROVISIONAL: the
# exact code is confirmed against the live DB in the Stage-3 verify. If _contact
# has no _type column (e.g. a synthetic test DB), official filtering degrades to
# a no-op rather than erroring.
_OFFICIAL_CONTACT_TYPES: frozenset[int] = frozenset({16})

_URL_RE = re.compile(r'https?://[^\s、-￿]+')
_TZ_TAIPEI = timezone(timedelta(hours=8))

# Hard cap on any caller-supplied LIMIT. Prevents a single call from pulling the
# whole DB into a tool result.
_MAX_LIMIT = 5000


def _sane_limit(value: Any, default: int) -> int:
    """Clamp a caller-supplied limit to a safe positive range.

    SQLite treats a negative LIMIT as 'unlimited', so an unchecked negative value
    leaks every row past the requested bound (a real privacy risk for a tool that
    returns private chat content). Zero/negative/non-int -> default; oversized ->
    capped at _MAX_LIMIT."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, _MAX_LIMIT)


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
    Called by key_extractor to validate memory candidates. Boolean by contract: a
    failure here means 'this candidate did not decrypt', nothing more. Use
    preflight_db_access() to tell a broken environment from a wrong key."""
    try:
        conn = _open_encrypted(db_path, key)
        next(conn.execute("SELECT count(*) FROM sqlite_master;"))
        conn.close()
        return True
    except Exception:
        return False


class DbAccessError(Exception):
    """The .edb cannot be accessed for reasons unrelated to the key: missing file,
    no read permission, database locked, or a broken cipher/engine setup. Kept
    distinct so these are never misreported as 'wrong key / LINE updated'."""


# Substrings that mean 'encrypted DB, wrong key' -- the engine works, the key is
# just wrong. Anything else on a preflight is an environment problem.
_WRONG_KEY_SIGNALS = ("not a database", "hmac", "file is encrypted", "encrypted")
_LOCK_SIGNALS = ("locked", "busy")
_ACCESS_SIGNALS = ("permission", "access is denied", "cannot open", "unable to open")


def preflight_db_access(db_path: str) -> None:
    """Confirm the .edb is a present, readable, engine-openable ENCRYPTED DB.

    Returns None when the file is encrypted and merely needs the correct key (the
    normal case, recognised by the engine's 'not a database'/HMAC signal). Raises
    DbAccessError with a specific reason for genuine environment problems, so a
    permission/lock/cipher failure is not swallowed into 'none of the keys worked'."""
    if not os.path.exists(db_path):
        raise DbAccessError(f"file not found: {db_path}")
    if not os.access(db_path, os.R_OK):
        raise DbAccessError(f"no read permission: {db_path}")
    try:
        conn = _open_encrypted(db_path, "0" * 32)  # deliberately wrong key
        try:
            next(conn.execute("SELECT count(*) FROM sqlite_master;"))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 -- classified below, not swallowed
        msg = str(e).lower()
        if any(s in msg for s in _WRONG_KEY_SIGNALS):
            return  # normal: encrypted DB, wrong key -> engine healthy
        if any(s in msg for s in _LOCK_SIGNALS):
            raise DbAccessError(f"database is locked: {e}") from e
        if any(s in msg for s in _ACCESS_SIGNALS):
            raise DbAccessError(f"cannot open the DB file (permission/handle): {e}") from e
        raise DbAccessError(f"unexpected DB access error: {e}") from e
    # A dummy key that actually decrypts is implausible, but if so the DB is fine.
    return


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
        official = self._official_mids(conn)
        return {"group": groups, "square": squares, "contact": contacts,
                "room": rooms, "official": official}

    def _resolve_chat(self, chat_id: str, maps: dict) -> tuple[str, str]:
        """Return (display_name, chat_type) for a _chat._id."""
        if chat_id in maps["group"]:
            return maps["group"][chat_id] or chat_id, "group"
        if chat_id in maps["square"]:
            return maps["square"][chat_id] or chat_id, "open"
        if chat_id in maps["room"]:
            return "多人聊天", "multi"
        if chat_id in maps["official"]:
            return maps["contact"].get(chat_id) or chat_id, "official"
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
        limit = _sane_limit(limit, 50)
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
        limit = _sane_limit(limit, 500)
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

    # -- unread ----------------------------------------------------------------
    def _official_mids(self, conn) -> set[str]:
        """mids of official/bot accounts, excluded from the unread list by default
        (their unread is mostly marketing pushes). Degrades to empty set if the
        _contact table has no _type column."""
        def safe(sql: str) -> list:
            try:
                return list(conn.execute(sql))
            except Exception:
                return []
        out: set[str] = set()
        for r in safe(f"SELECT _mid, _type FROM {_T_CONTACT};"):
            try:
                t = r["_type"]
            except Exception:  # pragma: no cover - defensive; SELECT guarantees the column
                t = None
            if t in _OFFICIAL_CONTACT_TYPES:
                out.add(r["_mid"])
        return out

    def _unread_messages(
        self, conn, chat_id: str, unread_count: int,
        contact_map: dict[str, str], limit: int
    ) -> tuple[list[dict], int]:
        """Return (most recent locally-present messages, available count).

        We deliberately do NOT range from _chat._firstUnreadId: that pointer is a
        stale low-water mark for some chats, so "messages after it" can be the whole
        history (observed: unread_count=1 but 97k messages after the marker). The
        authoritative unread NUMBER is _unreadCount, so available is capped by it:

            available = min(unread_count, messages present locally for this chat)

        When bodies are not synced yet, fewer than unread_count messages exist on
        disk and available drops below unread_count -- a high-confidence "missing"
        signal. LINE gives no reliable per-message read boundary, so when the chat
        DOES have >= unread_count messages on disk we optimistically treat the most
        recent unread_count as the unread ones (they usually are)."""
        if unread_count <= 0:
            return [], 0
        present_total = list(conn.execute(
            f"SELECT count(*) c FROM {_T_MESSAGE} WHERE _chatId=?;", (chat_id,)
        ))[0]["c"]
        available = min(unread_count, present_total)
        if available == 0:
            return [], 0
        rows = list(conn.execute(
            f"SELECT * FROM {_T_MESSAGE} WHERE _chatId=? "
            f"ORDER BY _createdTime DESC LIMIT ?;",
            (chat_id, min(unread_count, limit))
        ))
        rows.reverse()  # chronological
        return [parse_message_row(dict(r), contact_map) for r in rows], available

    def get_unread(
        self, limit_chats: int = 50, include_official: bool = False,
        per_chat_limit: int = 200
    ) -> list[dict]:
        """List chats with unread messages, honest about LINE's lazy sync.
        Per chat: available_count = unread bodies readable locally now (<= unread),
        missing_count = unread LINE has not downloaded yet (open the app to fetch)."""
        limit_chats = _sane_limit(limit_chats, 50)
        per_chat_limit = _sane_limit(per_chat_limit, 200)
        conn = self._open()
        try:
            maps = self._name_maps(conn)
            contact_map = self._contacts_map(conn)
            official = set() if include_official else self._official_mids(conn)
            rows = list(conn.execute(
                f"SELECT _id, _unreadCount, _lastUpdatedTime "
                f"FROM {_T_CHAT} WHERE _unreadCount>0 "
                f"ORDER BY _lastUpdatedTime DESC;"
            ))
            out = []
            for r in rows:
                cid = r["_id"]
                if cid in official:
                    continue
                name, ctype = self._resolve_chat(cid, maps)
                unread_n = r["_unreadCount"] or 0
                msgs, available = self._unread_messages(
                    conn, cid, unread_n, contact_map, per_chat_limit
                )
                out.append({
                    "chat_id": cid,
                    "name": name,
                    "type": ctype,
                    "unread_count": unread_n,
                    "available_count": available,
                    "missing_count": max(0, unread_n - available),
                    "fully_synced": available >= unread_n,
                    "messages": msgs,
                })
                if len(out) >= limit_chats:
                    break
            return out
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
