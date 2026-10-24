"""OpenChat history includes local replies within the authorized parent chat."""
from contextlib import closing
import sqlite3

import pytest

import line_mcp_server as server
from db_reader import DbReader
from tests.helpers import authorize


SINCE = "2026-10-24T17:14:00+08:00"
UNTIL = "2026-10-24T17:15:00+08:00"
START = server._parse_iso8601(SINCE)
END = server._parse_iso8601(UNTIL)


def thread_reader(tmp_path):
    path = tmp_path / "討論串測試.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE _message (
                _id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT,
                _createdTime INTEGER, _text TEXT, _contentType INTEGER,
                _contentMetadata TEXT, _status INTEGER,
                _hasUrlPreview INTEGER, _rev INTEGER);
            CREATE INDEX message_chat_time ON _message
                (_chatId,_createdTime,_contentType,_status,_hasUrlPreview,_rev);
            CREATE TABLE _squareThread (
                _threadMid TEXT PRIMARY KEY, _chatMid TEXT, _messageId TEXT,
                _rootMessage TEXT, _lastMessage TEXT);
            CREATE TABLE _squareMember (
                _squareMemberMid TEXT PRIMARY KEY, _displayName TEXT);
            INSERT INTO _squareMember VALUES ('member', '測試成員甲');
            INSERT INTO _squareThread VALUES
                ('thread-new','parent','a-root','invalid cache JSON','invalid cache JSON'),
                ('thread-old','parent','old-root',NULL,NULL),
                ('thread-other','secret','secret-root',NULL,NULL),
                ('cache-only','parent','cached-root',NULL,
                 '{"message":{"id":"cached-reply","text":"僅有快取的回覆"}}');
        """)
        conn.executemany(
            "INSERT INTO _message VALUES (?,?, 'member',?,?,0,NULL,0,0,0)",
            [
                ("old-root", "parent", START - 86400000, "昨天的主訊息"),
                ("a-root", "parent", START + 30562, "今天的主訊息"),
                ("b-reply", "thread-new", START + 40858, "用 Codex 檢查示範檔案"),
                ("c-reply", "thread-old", START + 40858, "舊討論串今天的新回覆"),
                ("before", "thread-new", START - 1, "範圍之前不能出現"),
                ("next-minute", "thread-new", END, "結束邊界不能出現"),
                ("hidden", "thread-other", START + 40858, "其他聊天室不能出現"),
            ],
        )
        conn.commit()
    return DbReader(str(path), None, _test_mode=True), path


def test_parent_history_pages_include_replies_without_crossing_scope(tmp_path, monkeypatch):
    reader, _ = thread_reader(tmp_path)
    authorize(monkeypatch, allowed_chat_ids=frozenset({"parent"}))
    monkeypatch.setattr(server, "_reader", reader)
    items, cursor = [], None
    for _ in range(4):
        result = server.line_get_history("parent", SINCE, UNTIL, limit=1, cursor=cursor)
        items.extend(result["items"])
        if not result["has_more"]:
            break
        cursor = result["next_cursor"]
        assert cursor
    else:
        pytest.fail("Thread history pagination did not terminate")

    assert [(m["message_id"], m["sender"], m["content"]) for m in items] == [
        ("a-root", "測試成員甲", "今天的主訊息"),
        ("b-reply", "測試成員甲", "用 Codex 檢查示範檔案"),
        ("c-reply", "測試成員甲", "舊討論串今天的新回覆"),
    ]
    assert "thread_id" not in items[0]
    assert [(m["thread_id"], m["thread_root_message_id"]) for m in items[1:]] == [
        ("thread-new", "a-root"), ("thread-old", "old-root"),
    ]
    assert result["coverage"] == "local_rows_only"
    for denied in ("thread-new", "thread-other", "secret"):
        with pytest.raises(PermissionError):
            server.line_get_history(denied, SINCE, UNTIL)


def test_reply_payload_still_obeys_byte_and_session_budgets(tmp_path, monkeypatch):
    reader, path = thread_reader(tmp_path)
    authorize(monkeypatch, allowed_chat_ids=frozenset({"parent"}),
              max_messages_per_call=1, max_session_messages=2)
    monkeypatch.setattr(server, "_reader", reader)
    first = server.line_get_history("parent", SINCE, UNTIL, limit=1)
    second = server.line_get_history("parent", SINCE, UNTIL, limit=1,
                                     cursor=first["next_cursor"])
    assert second["items"][0]["content"] == "用 Codex 檢查示範檔案"
    assert second["has_more"]
    with pytest.raises(PermissionError, match="message budget"):
        server.line_get_history("parent", SINCE, UNTIL, cursor=second["next_cursor"])

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE _message SET _text=? WHERE _id='b-reply'", ("測試" * 1500,))
        conn.commit()
    blocked = reader.get_history("parent", START + 40858, END, max_bytes=2048)
    assert blocked["items"] == []
    assert blocked["has_more"] and blocked["next_cursor"] is None
    assert blocked["content_complete"] is False
    assert blocked["blocked_reason"] == "item_exceeds_byte_budget"


def test_history_keeps_working_when_thread_table_is_absent(tmp_path):
    reader, path = thread_reader(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TABLE _squareThread")
        conn.commit()
    result = reader.get_history("parent", START, END)
    assert [(m["message_id"], m["content"]) for m in result["items"]] == [
        ("a-root", "今天的主訊息"),
    ]
    assert result["has_more"] is False


def test_thread_mapping_requires_unique_thread_ids(tmp_path):
    reader, path = thread_reader(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            DROP TABLE _squareThread;
            CREATE TABLE _squareThread (
                _threadMid TEXT, _chatMid TEXT, _messageId TEXT);
            INSERT INTO _squareThread VALUES
                ('thread-new','parent','a-root'),
                ('thread-new','parent','a-root');
        """)
    with pytest.raises(ValueError, match="_squareThread._threadMid"):
        reader.get_history("parent", START, END)
