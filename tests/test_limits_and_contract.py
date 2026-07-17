"""Input-bound validation + MCP wrapper->reader contract tests.

These close two gaps an external audit found:
- negative/zero limits bypassed bounds (SQLite treats LIMIT -1 as unlimited),
  leaking rows beyond the requested set;
- the wrapper tests only checked return values via MagicMock, so breaking the
  MCP time-range plumbing (ignore since/until) still passed. These assert the
  parsed arguments actually reach the reader.
"""
import sqlite3
from unittest.mock import MagicMock, patch

import line_mcp_server as srv
from db_reader import DbReader, _sane_limit


# --- _sane_limit clamps -----------------------------------------------------
def test_sane_limit_rejects_negative_zero_and_bad():
    assert _sane_limit(-1, 500) == 500      # negative -> default (NOT unlimited)
    assert _sane_limit(0, 50) == 50
    assert _sane_limit("x", 200) == 200
    assert _sane_limit(None, 200) == 200


def test_sane_limit_respects_and_caps():
    assert _sane_limit(10, 50) == 10
    assert _sane_limit(10_000_000, 50) == 5000   # capped at _MAX_LIMIT


def _leak_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, _unreadCount INTEGER, _firstUnreadId TEXT)")
    conn.execute("CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT)")
    conn.execute("CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, _createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)")
    conn.execute("INSERT INTO _groupChat VALUES ('g1','群一')")
    conn.execute("INSERT INTO _groupChat VALUES ('g2','群二')")
    conn.execute("INSERT INTO _chat VALUES ('g1', 10, 2, NULL)")  # unread 2
    conn.execute("INSERT INTO _chat VALUES ('g2', 5, 1, NULL)")
    for i in range(5):  # 5 messages present but only 2 unread
        conn.execute("INSERT INTO _message VALUES (?,?,?,?,?,?,?)", (f"m{i}", "g1", "u", 1000 + i, f"t{i}", 0, None))
    conn.commit(); conn.close()


def test_negative_per_chat_limit_does_not_leak_read_messages(tmp_path):
    db = str(tmp_path / "leak.db"); _leak_db(db)
    r = DbReader(db, key=None, _test_mode=True)
    g1 = next(c for c in r.get_unread(per_chat_limit=-1) if c["chat_id"] == "g1")
    assert g1["available_count"] == 2
    assert len(g1["messages"]) <= g1["available_count"]  # was 5 before the fix


def test_limit_chats_zero_returns_all_not_one(tmp_path):
    db = str(tmp_path / "leak.db"); _leak_db(db)
    r = DbReader(db, key=None, _test_mode=True)
    assert len(r.get_unread(limit_chats=0)) == 2   # was 1 before the fix


def test_get_history_negative_limit_is_bounded(tmp_path):
    db = str(tmp_path / "leak.db"); _leak_db(db)
    r = DbReader(db, key=None, _test_mode=True)
    neg = r.get_history("g1", 0, 9_999_999_999, limit=-1)
    default = r.get_history("g1", 0, 9_999_999_999, limit=500)
    assert len(neg) == len(default)  # -1 clamps to default, not "unlimited"


# --- MCP wrapper -> reader contract (the anti-slop gap) ---------------------
def test_line_get_history_passes_parsed_bounds_to_reader():
    fake = MagicMock()
    fake.get_history.return_value = ["ok"]
    since, until = "2026-06-15T00:00:00+08:00", "2026-06-16T00:00:00+08:00"
    with patch.object(srv, "_get_reader", return_value=fake):
        srv.line_get_history("chatX", since, until, limit=7)
    fake.get_history.assert_called_once_with(
        chat_id="chatX",
        since_ts=srv._parse_iso8601(since),
        until_ts=srv._parse_iso8601(until),
        limit=7,
    )
    # regression guard: if the wrapper ignored since/until, since_ts==until_ts
    call = fake.get_history.call_args.kwargs
    assert call["since_ts"] != call["until_ts"]


def test_line_get_unread_forwards_its_arguments():
    fake = MagicMock()
    fake.get_unread.return_value = []
    with patch.object(srv, "_get_reader", return_value=fake):
        srv.line_get_unread(limit_chats=3, include_official=True, per_chat_limit=9)
    fake.get_unread.assert_called_once_with(
        limit_chats=3, include_official=True, per_chat_limit=9
    )
