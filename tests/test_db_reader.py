import sqlite3
import pytest
from db_reader import (
    extract_urls_from_text,
    parse_message_row,
    DbReader,
)


def _make_test_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE chat (
        chat_id TEXT PRIMARY KEY, name TEXT, type INTEGER,
        member_count INTEGER, last_message_at INTEGER)""")
    conn.execute("""CREATE TABLE message (
        msg_id TEXT PRIMARY KEY, chat_id TEXT, sender_id TEXT,
        content TEXT, type INTEGER, sent_at INTEGER,
        local_path TEXT, filename TEXT, url TEXT,
        title TEXT, description TEXT)""")
    conn.execute("""CREATE TABLE contact (
        contact_id TEXT PRIMARY KEY, display_name TEXT)""")
    conn.execute("INSERT INTO contact VALUES ('u1', '王小明')")
    conn.execute("INSERT INTO contact VALUES ('u2', '李小美')")
    conn.execute("INSERT INTO chat VALUES ('c1','家族群',2,5,1718600000)")
    conn.execute("""INSERT INTO message VALUES
        ('m1','c1','u1','大家好 https://youtu.be/xxx',1,1718600000,
         NULL,NULL,NULL,NULL,NULL)""")
    conn.execute("""INSERT INTO message VALUES
        ('m2','c1','u2',NULL,2,1718600010,
         'C:/path/img.jpg',NULL,NULL,NULL,NULL)""")
    conn.commit()
    conn.close()


def test_extract_urls_finds_https():
    assert extract_urls_from_text("看 https://youtu.be/xxx 有趣") == ["https://youtu.be/xxx"]


def test_extract_urls_empty_on_no_url():
    assert extract_urls_from_text("純文字") == []


def test_extract_urls_none_input():
    assert extract_urls_from_text(None) == []


def test_parse_message_row_text():
    row = {"msg_id": "m1", "chat_id": "c1", "sender_id": "u1",
           "content": "你好", "type": 1, "sent_at": 1718600000,
           "local_path": None, "filename": None,
           "url": None, "title": None, "description": None}
    result = parse_message_row(row, {"u1": "王小明"})
    assert result["type"] == "text"
    assert result["sender"] == "王小明"
    assert result["content"] == "你好"
    assert "sent_at" in result


def test_parse_message_row_image():
    row = {"msg_id": "m2", "chat_id": "c1", "sender_id": "u2",
           "content": None, "type": 2, "sent_at": 1718600010,
           "local_path": "C:/path/img.jpg", "filename": None,
           "url": None, "title": None, "description": None}
    result = parse_message_row(row, {"u2": "李小美"})
    assert result["type"] == "image"
    assert result["local_path"] == "C:/path/img.jpg"


def test_dbreader_list_chats(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    chats = reader.list_chats()
    assert len(chats) == 1
    assert chats[0]["name"] == "家族群"


def test_dbreader_get_history(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    msgs = reader.get_history("c1", since_ts=0, until_ts=9999999999, limit=10)
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "王小明"
    assert "https://youtu.be/xxx" in msgs[0]["urls"]
    assert msgs[1]["type"] == "image"


def test_dbreader_get_contacts(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    contacts = reader.get_contacts()
    names = [c["display_name"] for c in contacts]
    assert "王小明" in names
