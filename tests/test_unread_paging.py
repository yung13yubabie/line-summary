"""Single-chat local context pages remain bounded and include thread replies."""
from contextlib import closing
import sqlite3

import pytest

from db_reader import DbReader
from safety import json_size


def local_reader(tmp_path, count=1507, *, text=None, unread_count=999):
    path = tmp_path / "本機上下文.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER,
                                _unreadCount INTEGER);
            CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT,
                                   _displayNameOverridden TEXT, _type INTEGER);
            CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT);
            CREATE TABLE _squareMember (_squareMemberMid TEXT PRIMARY KEY,
                                        _displayName TEXT);
            CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT,
                _from TEXT, _createdTime INTEGER, _text TEXT,
                _contentType INTEGER, _contentMetadata TEXT);
            CREATE INDEX message_chat_time ON _message (_chatId,_createdTime);
            CREATE TABLE _squareThread (_threadMid TEXT PRIMARY KEY,
                                        _chatMid TEXT, _messageId TEXT);
            INSERT INTO _groupChat VALUES ('parent','本機測試聊天室');
            INSERT INTO _squareMember VALUES ('member','測試成員乙');
            INSERT INTO _contact VALUES ('official','測試通知',NULL,16);
            INSERT INTO _squareThread VALUES ('thread','parent','m00000');
            INSERT INTO _squareThread VALUES ('hidden-thread','secret','hidden-root');
            INSERT INTO _chat VALUES ('secret',1000,4),('official',1000,4);
            INSERT INTO _message VALUES
                ('hidden','hidden-thread','member',1000,'其他聊天室不可讀',0,NULL),
                ('notice','official','official',1000,'通知測試內容',0,NULL);
        """)
        conn.execute("INSERT INTO _chat VALUES ('parent',1000,?)", (unread_count,))
        conn.executemany("INSERT INTO _message VALUES (?,?, 'member',1000,?,0,NULL)", [
            (f"m{i:05}", "thread" if i % 2 else "parent",
             text if text is not None else f"本機第{i:05}則內容")
            for i in range(count)
        ])
        conn.commit()
    return DbReader(str(path), None, _test_mode=True), path


def test_all_local_context_pages_pass_999_and_keep_the_first_scan_ceiling(tmp_path):
    reader, path = local_reader(tmp_path)
    found, cursor = [], None
    for index in range(12):
        result = reader.get_unread_context("parent", limit=200, cursor=cursor,
                                           allowed_chat_ids=frozenset({"parent"}))
        found.extend(result["items"])
        assert result["selection"] == "all_local_context"
        assert result["sync_status"] == "unknown"
        assert result["unread_boundary_verified"] is False
        assert result["unread_count_at_start"] == 999
        assert result["order"] == "oldest_first"
        assert result["consistency"] == "live_keyset_scan"
        assert result["database_changes_may_affect_pagination"] is True
        assert "range_since_ms" not in result and "range_until_ms" not in result
        if index == 0:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("UPDATE _chat SET _unreadCount=0 WHERE _id='parent'")
                conn.executemany("INSERT INTO _message VALUES (?,'parent','member',?,?,0,NULL)", [
                    ("backfill", 500, "後加入的回補不在首輪界線"),
                    ("incoming", 2000, "下一輪才讀的新訊息"),
                ])
                conn.commit()
        if not result["has_more"]:
            break
        cursor = result["next_cursor"]
        assert cursor
    else:
        pytest.fail("Local context pagination did not terminate")
    assert [(m["message_id"], m["content"]) for m in found] == [
        (f"m{i:05}", f"本機第{i:05}則內容") for i in range(1507)
    ]
    assert found[1]["sender"] == "測試成員乙"
    assert found[1]["thread_id"] == "thread"
    assert found[1]["thread_root_message_id"] == "m00000"


def test_local_context_byte_pages_resume_after_the_last_delivered_message(tmp_path):
    reader, _ = local_reader(tmp_path, count=11, text="中文字" * 25)
    found, cursor = [], None
    for _ in range(15):
        result = reader.get_unread_context("parent", limit=10, max_bytes=2048, cursor=cursor)
        assert json_size(result) <= 2048
        assert result["items"]
        assert result["content_complete"] is True
        assert all(m["content"] == "中文字" * 25 for m in result["items"])
        found.extend(m["message_id"] for m in result["items"])
        if not result["has_more"]:
            break
        cursor = result["next_cursor"]
        assert cursor
    else:
        pytest.fail("Byte-limited local context pages did not terminate")
    assert found == [f"m{i:05}" for i in range(11)]


def test_oversized_local_context_message_blocks_without_skipping_it(tmp_path):
    reader, _ = local_reader(tmp_path, count=1, text="文字" * 1500)
    result = reader.get_unread_context("parent", max_bytes=2048)
    assert result["items"] == [] and result["has_more"]
    assert result["next_cursor"] is None
    assert result["content_complete"] is False
    assert result["blocked_reason"] == "item_exceeds_byte_budget"
    assert json_size(result) <= 2048


@pytest.mark.parametrize("change", ["chat", "official", "allowlist", "tamper", "process", "history"])
def test_local_context_cursor_cannot_change_scope(tmp_path, change):
    reader, path = local_reader(tmp_path, count=4)
    allowed = frozenset({"parent", "secret"})
    token = reader.get_unread_context("parent", limit=1, allowed_chat_ids=allowed)["next_cursor"]
    kwargs = dict(chat_id="parent", cursor=token, allowed_chat_ids=allowed)
    if change == "chat":
        kwargs["chat_id"] = "secret"
    if change == "official":
        kwargs["include_official"] = True
    if change == "allowlist":
        kwargs["allowed_chat_ids"] = frozenset({"parent"})
    if change == "tamper":
        kwargs["cursor"] = ("A" if token[0] != "A" else "B") + token[1:]
    if change == "process":
        reader = DbReader(str(path), None, _test_mode=True)
    if change == "history":
        kwargs["cursor"] = reader.get_history("parent", 0, 2**63 - 1, limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        reader.get_unread_context(**kwargs)


def test_local_context_guards_scope_and_official_accounts_before_messages(tmp_path, monkeypatch):
    reader, _ = local_reader(tmp_path, count=1)
    def forbidden(*args, **kwargs):
        raise AssertionError("No message content may be read for an excluded chat")
    monkeypatch.setattr(reader, "_history_rows", forbidden)
    with pytest.raises(PermissionError):
        reader.get_unread_context("parent", allowed_chat_ids=frozenset())
    with pytest.raises(PermissionError):
        reader.get_unread_context("official", allowed_chat_ids=frozenset({"official"}))


def test_zero_counter_and_legacy_schema_keep_local_context_and_the_500_cap(tmp_path):
    reader, path = local_reader(tmp_path, count=1001, unread_count=0)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TABLE _squareThread")
        conn.commit()
    first = reader.get_unread_context("parent", limit=999999)
    assert len(first["items"]) == 500
    assert first["unread_count_at_start"] == 0
    assert first["has_more"]
    second = reader.get_unread_context("parent", cursor=first["next_cursor"])
    assert [(m["message_id"], m["content"]) for m in second["items"]] == [
        ("m01000", "本機第01000則內容"),
    ]
    assert second["has_more"] is False
    assert "thread_id" not in first["items"][0]


def test_local_context_official_opt_in_and_original_unread_contract(tmp_path):
    reader, _ = local_reader(tmp_path, count=5, unread_count=2)
    result = reader.get_unread_context("official", include_official=True)
    assert [m["content"] for m in result["items"]] == ["通知測試內容"]
    original = reader.get_unread(per_chat_limit=1, allowed_chat_ids=frozenset({"parent"}))
    assert len(original["items"]) == 1
    assert original["items"][0]["selection"] == "latest_local_approximation"
    assert original["items"][0]["messages"][0]["content"] == "本機第00004則內容"
    assert "unread_count_at_start" not in original
