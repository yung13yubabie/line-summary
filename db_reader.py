"""
Reads LINE PC wxSQLite3-encrypted .edb.

SCHEMA:
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
from pathlib import Path
from safety import CursorCodec, page

# -- Database cipher configuration -------------------------------------------
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

# LINE message content-type codes.
_CONTENT_TYPE: dict[int, str] = {
    0: "text", 1: "image", 2: "video", 3: "audio",
    6: "location", 7: "sticker", 13: "contact", 14: "file", 16: "link",
}

# Official/bot account _contact._type codes -- their unread is mostly marketing
# pushes, so they are excluded from the unread list by default. This mapping
# is schema-dependent. If _contact has no _type column, official filtering
# degrades to a no-op rather than erroring.
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
    except (TypeError, ValueError, OverflowError):
        return default
    if v <= 0:
        return default
    return min(v, _MAX_LIMIT)


def _checked_id(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Unsupported schema: IDs must be nonempty strings of at most 256 characters")
    return value


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
    seconds = ts / 1000
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
    # video/audio/location/contact (known) and any unknown _contentType all surface
    # the raw text under their resolved kind, rather than being silently dropped.
    return {"type": kind, "sender": sender, "content": row.get("_text"),
            "sent_at": sent}


class DbReader:
    def __init__(self, db_path: str, key: str | None, _test_mode: bool = False):
        self._db_path = db_path
        self._key = key
        self._test_mode = _test_mode
        self._cursors = CursorCodec()

    def _open(self):
        if self._test_mode:
            conn = sqlite3.connect(Path(self._db_path).resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        if not self._key:
            raise ValueError("Encrypted production reads require a key")
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
        self, query: str = "", chat_type: str = "", limit: int = 50,
        cursor: str | None = None, allowed_chat_ids: frozenset | None = None,
        max_bytes: int = 262144,
    ) -> dict:
        limit = min(_sane_limit(limit, 50), 500)
        scope = ["chats", query, chat_type, sorted(allowed_chat_ids) if allowed_chat_ids is not None else None]
        after = self._cursors.decode(cursor, scope) if cursor else None
        conn = self._open()
        try:
            maps = self._name_maps(conn)
            out, positions = [], []
            for r in conn.execute(f"SELECT _id, _lastUpdatedTime FROM {_T_CHAT} ORDER BY _id COLLATE BINARY;"):
                cid = _checked_id(r["_id"])
                if after is not None and cid <= after:
                    continue
                if allowed_chat_ids is not None and cid not in allowed_chat_ids:
                    continue
                name, ctype = self._resolve_chat(cid, maps)
                if chat_type and ctype != chat_type:
                    continue
                if query and query.casefold() not in (name or "").casefold():
                    continue
                out.append({"chat_id": cid, "name": name, "type": ctype,
                            "last_message_at": _ts_to_iso(r["_lastUpdatedTime"])})
                positions.append(cid)
                if len(out) > limit:
                    break
            return page(out[:limit], positions[:limit], scope, self._cursors,
                        len(out) > limit, max_bytes, consistency="live_metadata")
        finally:
            conn.close()

    def get_history(
        self, chat_id: str, since_ms: int, until_ms: int, limit: int = 100,
        cursor: str | None = None, max_bytes: int = 262144,
    ) -> dict:
        """Keyset scan of local rows in [since_ms, until_ms), milliseconds.

        Timestamp + unique _id prevent gaps for equal timestamps. The first
        page's rowid ceiling reduces ordinary append/backfill changes, but SQLite
        can reuse row IDs after deletion. Inserts, edits and deletions are not
        frozen across calls: this is neither a snapshot nor remote sync proof.
        """
        if until_ms <= since_ms:
            raise ValueError("until must be after since")
        limit = min(_sane_limit(limit, 100), 500)
        scope = ["history", chat_id, since_ms, until_ms]
        position = self._cursors.decode(cursor, scope) if cursor else None
        conn = self._open()
        try:
            if position is None:
                ceiling = next(iter(conn.execute(f"SELECT COALESCE(MAX(rowid), 0) AS n FROM {_T_MESSAGE};")))["n"]
                last_time, last_id = since_ms, None
            else:
                ceiling, last_time, last_id = position
            primary = [r["name"] for r in conn.execute(f"PRAGMA table_info({_T_MESSAGE});") if r["pk"]]
            if primary != ["_id"]:
                raise ValueError("Unsupported schema: _message._id must be the sole primary key")
            # _id must be a non-null unique identifier in supported LINE schema.
            contact_map = self._contacts_map(conn)
            rows = list(conn.execute(
                f"SELECT * FROM {_T_MESSAGE} WHERE _chatId=? AND _createdTime>=? "
                f"AND _createdTime<? AND rowid<=? "
                f"AND (? IS NULL OR _createdTime>? OR (_createdTime=? AND _id COLLATE BINARY>?)) "
                f"ORDER BY _createdTime ASC, _id COLLATE BINARY ASC LIMIT ?;",
                (chat_id, since_ms, until_ms, ceiling, last_id, last_time, last_time, last_id, limit + 1),
            ))
            items, positions = [], []
            for r in rows[:limit]:
                _checked_id(r["_id"])
                item = parse_message_row(dict(r), contact_map)
                item["message_id"] = r["_id"]
                items.append(item)
                positions.append([ceiling, r["_createdTime"], r["_id"]])
            return page(items, positions, scope, self._cursors, len(rows) > limit, max_bytes,
                        coverage="local_rows_only", consistency="live_keyset_scan", database_changes_may_affect_pagination=True,
                        range_since_ms=since_ms, range_until_ms=until_ms)
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

    def _unread_messages(self, conn, chat_id, unread_count, contact_map, limit):
        # No trustworthy per-message read boundary: these may ALL be read rows.
        rows = list(conn.execute(
            f"SELECT * FROM {_T_MESSAGE} WHERE _chatId=? "
            f"ORDER BY _createdTime DESC, _id COLLATE BINARY DESC LIMIT ?;",
            (chat_id, min(unread_count, limit) + 1),
        ))
        more = len(rows) > min(unread_count, limit)
        chosen = rows[:min(unread_count, limit)]
        chosen.reverse()
        messages = [{**parse_message_row(dict(r), contact_map), "message_id": _checked_id(r["_id"])}
                    for r in chosen]
        return messages, more

    def get_unread(
        self, limit_chats: int = 20, include_official: bool = False,
        per_chat_limit: int = 50, total_message_limit: int = 500,
        cursor: str | None = None, allowed_chat_ids: frozenset | None = None,
        max_bytes: int = 262144,
    ) -> dict:
        """Unread counts plus explicitly approximate recent local messages."""
        limit_chats = min(_sane_limit(limit_chats, 20), 100)
        per_chat_limit = min(_sane_limit(per_chat_limit, 50), 500)
        total_message_limit = min(_sane_limit(total_message_limit, 500), 500)
        scope = ["unread", include_official, per_chat_limit,
                 sorted(allowed_chat_ids) if allowed_chat_ids is not None else None]
        after = self._cursors.decode(cursor, scope) if cursor else None
        conn = self._open()
        try:
            maps = self._name_maps(conn)
            contact_map = self._contacts_map(conn)
            official = set() if include_official else self._official_mids(conn)
            rows = conn.execute(f"SELECT _id, _unreadCount FROM {_T_CHAT} "
                                f"WHERE _unreadCount>0 ORDER BY _id COLLATE BINARY;")
            out, positions, used, more = [], [], 0, False
            for r in rows:
                cid = _checked_id(r["_id"])
                if (after is not None and cid <= after) or cid in official:
                    continue
                if allowed_chat_ids is not None and cid not in allowed_chat_ids:
                    continue
                if len(out) >= limit_chats or used >= total_message_limit:
                    more = True
                    break
                name, ctype = self._resolve_chat(cid, maps)
                count = max(0, r["_unreadCount"] or 0)
                cap = min(per_chat_limit, total_message_limit - used)
                msgs, more_local = self._unread_messages(conn, cid, count, contact_map, cap)
                out.append({"chat_id": cid, "name": name, "type": ctype,
                            "unread_count": count, "returned_count": len(msgs),
                            "selection": "latest_local_approximation", "sync_status": "unknown",
                            "unread_boundary_verified": False,
                            "messages_limited": count > cap and more_local,
                            "more_local_messages": more_local, "messages": msgs})
                positions.append(cid)
                used += len(msgs)
            return page(out, positions, scope, self._cursors, more, max_bytes,
                        coverage="approximate_recent_local_messages", consistency="live_metadata")
        finally:
            conn.close()

    def get_contacts(self, query: str = "", limit: int = 50,
                     cursor: str | None = None, max_bytes: int = 262144) -> dict:
        limit = min(_sane_limit(limit, 50), 100)
        scope = ["contacts", query]
        after = self._cursors.decode(cursor, scope) if cursor else None
        conn = self._open()
        try:
            # Escape LIKE wildcards so user text remains a literal substring.
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = list(conn.execute(
                f"SELECT _mid, _displayName, _displayNameOverridden FROM {_T_CONTACT} "
                f"WHERE (? IS NULL OR _mid COLLATE BINARY>?) "
                f"AND (?='' OR _displayName LIKE ? ESCAPE '\\' OR _displayNameOverridden LIKE ? ESCAPE '\\') "
                f"ORDER BY _mid COLLATE BINARY LIMIT ?;", (after, after, query, pattern, pattern, limit + 1),
            ))
            items = [{"contact_id": _checked_id(r["_mid"]), "display_name": r["_displayNameOverridden"] or r["_displayName"]}
                     for r in rows[:limit]]
            return page(items, [r["_mid"] for r in rows[:limit]], scope, self._cursors,
                        len(rows) > limit, max_bytes, consistency="live_metadata")
        finally:
            conn.close()
