"""
Reads LINE PC wxSQLite3-encrypted .edb.
Update constants below after spike/FINDINGS.md is recorded.
"""
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

# -- Update from spike/FINDINGS.md ------------------------------------------
_WORKING_PRAGMA_IDX = 0   # 0=raw hex, 1=aes256cbc, 2=chacha20, 3=text
_TABLE_CHAT    = "chat"
_TABLE_MESSAGE = "message"
_TABLE_CONTACT = "contact"
_MSG_TEXT    = 1
_MSG_IMAGE   = 2
_MSG_STICKER = 3
_MSG_FILE    = 4
_MSG_VIDEO   = 5
_MSG_LINK    = 6

# Chat type codes -- update from spike/FINDINGS.md after Phase 0
_CHAT_TYPE_MAP: dict[int, str] = {
    1: "personal",    # 個人聊天
    2: "group",       # 群組
    3: "multi",       # 多人聊天（臨時）
    4: "official",    # 官方帳號 / LINE@
    5: "open",        # 開放聊天室
}
_CHAT_TYPE_LABEL: dict[str, str] = {
    "personal":  "個人",
    "group":     "群組",
    "multi":     "多人聊天",
    "official":  "官方帳號",
    "open":      "開放聊天室",
}
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://[^\s、-￿]+')
_TZ_TAIPEI = timezone(timedelta(hours=8))


def _pragma_statements(key: str, idx: int) -> list[str]:
    sets = [
        [f"PRAGMA key = \"x'{key}'\";"],
        [f"PRAGMA key = \"x'{key}'\";", "PRAGMA cipher = 'aes256cbc';"],
        [f"PRAGMA key = \"x'{key}'\";", "PRAGMA cipher = 'chacha20';"],
        [f"PRAGMA key = '{key}';"],
    ]
    return sets[idx]


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text)


def _ts_to_iso(ts: int) -> str:
    dt = datetime.fromtimestamp(
        ts / 1000 if ts > 1_000_000_000_000 else ts, tz=_TZ_TAIPEI
    )
    return dt.isoformat()


def parse_message_row(row: dict[str, Any], contact_map: dict[str, str]) -> dict:
    sender = contact_map.get(row["sender_id"], row["sender_id"])
    t = row.get("type", _MSG_TEXT)
    sent = _ts_to_iso(row["sent_at"])

    if t == _MSG_TEXT:
        content = row.get("content") or ""
        return {"type": "text", "sender": sender, "content": content,
                "urls": extract_urls_from_text(content), "sent_at": sent}
    if t == _MSG_IMAGE:
        return {"type": "image", "sender": sender, "content": None,
                "local_path": row.get("local_path"), "sent_at": sent}
    if t == _MSG_STICKER:
        return {"type": "sticker", "sender": sender, "content": "[貼圖]", "sent_at": sent}
    if t == _MSG_FILE:
        return {"type": "file", "sender": sender, "content": None,
                "filename": row.get("filename"), "sent_at": sent}
    if t == _MSG_LINK:
        return {"type": "link", "sender": sender,
                "url": row.get("url"), "title": row.get("title"),
                "description": row.get("description"), "sent_at": sent}
    return {"type": "unknown", "sender": sender,
            "content": row.get("content"), "sent_at": sent}


def probe_key(db_path: str, key: str) -> bool:
    """Return True if key successfully decrypts the .edb. Called by key_extractor."""
    try:
        import sqlcipher3
        conn = sqlcipher3.connect(db_path)
        for p in _pragma_statements(key, _WORKING_PRAGMA_IDX):
            conn.execute(p)
        conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        conn.close()
        return True
    except Exception:
        return False


class DbReader:
    def __init__(self, db_path: str, key: str | None, _test_mode: bool = False):
        self._db_path = db_path
        self._key = key
        self._test_mode = _test_mode

    def _open(self) -> sqlite3.Connection:
        if self._test_mode or self._key is None:
            conn = sqlite3.connect(self._db_path)
        else:
            import sqlcipher3
            conn = sqlcipher3.connect(self._db_path)
            for p in _pragma_statements(self._key, _WORKING_PRAGMA_IDX):
                conn.execute(p)
        conn.row_factory = sqlite3.Row
        return conn

    def _contacts(self, conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute(
            f"SELECT contact_id, display_name FROM {_TABLE_CONTACT};"
        ).fetchall()
        return {r["contact_id"]: r["display_name"] for r in rows}

    def list_chats(
        self, query: str = "", chat_type: str = "", limit: int = 50
    ) -> list[dict]:
        conn = self._open()
        try:
            conditions, params = [], []
            if query:
                conditions.append("name LIKE ?")
                params.append(f"%{query}%")
            if chat_type:
                code = next(
                    (k for k, v in _CHAT_TYPE_MAP.items() if v == chat_type), None
                )
                if code is not None:
                    conditions.append("type = ?")
                    params.append(code)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_CHAT} {where} "
                f"ORDER BY last_message_at DESC LIMIT ?;",
                params
            ).fetchall()
            return [
                {
                    "chat_id": r["chat_id"],
                    "name": r["name"],
                    "type": _CHAT_TYPE_MAP.get(r["type"], "unknown"),
                    "type_label": _CHAT_TYPE_LABEL.get(
                        _CHAT_TYPE_MAP.get(r["type"], ""), str(r["type"])
                    ),
                    "member_count": r["member_count"],
                    "last_message_at": _ts_to_iso(r["last_message_at"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_history(
        self, chat_id: str, since_ts: int, until_ts: int, limit: int = 500
    ) -> list[dict]:
        conn = self._open()
        try:
            contact_map = self._contacts(conn)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_MESSAGE} "
                f"WHERE chat_id=? AND sent_at>=? AND sent_at<=? "
                f"ORDER BY sent_at ASC LIMIT ?;",
                (chat_id, since_ts, until_ts, limit)
            ).fetchall()
            return [parse_message_row(dict(r), contact_map) for r in rows]
        finally:
            conn.close()

    def get_contacts(self, query: str = "") -> list[dict]:
        conn = self._open()
        try:
            if query:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CONTACT} WHERE display_name LIKE ?;",
                    (f"%{query}%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CONTACT};"
                ).fetchall()
            return [{"contact_id": r["contact_id"],
                     "display_name": r["display_name"]} for r in rows]
        finally:
            conn.close()
