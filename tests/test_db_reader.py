import sqlite3
from db_reader import (
    extract_urls_from_text,
    parse_message_row,
    DbReader,
)


def _make_test_db(path: str):
    """Synthetic DB matching the REAL LINE 26.3 schema."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER)")
    conn.execute("CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT)")
    conn.execute(
        "CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, "
        "_displayNameOverridden TEXT)"
    )
    conn.execute(
        "CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, "
        "_createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE _squareMember (_squareMemberMid TEXT PRIMARY KEY, _displayName TEXT)"
    )
    conn.execute("INSERT INTO _contact VALUES ('u1', '王小明', NULL)")
    conn.execute("INSERT INTO _contact VALUES ('u2', '李小美', NULL)")
    conn.execute("INSERT INTO _squareMember VALUES ('sm1', '開放成員A')")
    # group chat c1, a 1:1 chat keyed by contact mid u2, and an OpenChat sqc1
    conn.execute("INSERT INTO _chat VALUES ('c1', 1718600020000)")
    conn.execute("INSERT INTO _chat VALUES ('u2', 1718600000000)")
    conn.execute("INSERT INTO _chat VALUES ('sqc1', 1718600030000)")
    conn.execute("INSERT INTO _groupChat VALUES ('c1', '家族群')")
    conn.execute(
        "INSERT INTO _message VALUES "
        "('m1','c1','u1',1718600000000,'大家好 https://youtu.be/xxx',0,NULL)"
    )
    conn.execute(
        "INSERT INTO _message VALUES "
        "('m2','c1','u2',1718600010000,NULL,1,NULL)"
    )
    conn.execute(
        "INSERT INTO _message VALUES "
        "('m3','sqc1','sm1',1718600030000,'哈囉',0,NULL)"
    )
    conn.commit()
    conn.close()


def test_probe_key_importable_and_false_on_missing_db():
    # Regression guard: key_extractor imports probe_key; it was dropped once in a
    # rewrite and broke MCP startup. Keep it importable with a stable contract.
    from db_reader import probe_key
    assert probe_key("C:/nonexistent/definitely-not-here.edb", "0" * 32) is False


def test_extract_urls_finds_https():
    assert extract_urls_from_text("看 https://youtu.be/xxx 有趣") == ["https://youtu.be/xxx"]


def test_extract_urls_empty_on_no_url():
    assert extract_urls_from_text("純文字") == []


def test_extract_urls_none_input():
    assert extract_urls_from_text(None) == []


def test_parse_message_row_text():
    row = {"_id": "m1", "_chatId": "c1", "_from": "u1",
           "_text": "你好", "_contentType": 0, "_createdTime": 1718600000000,
           "_contentMetadata": None}
    result = parse_message_row(row, {"u1": "王小明"})
    assert result["type"] == "text"
    assert result["sender"] == "王小明"
    assert result["content"] == "你好"
    assert "sent_at" in result


def test_parse_message_row_image():
    row = {"_id": "m2", "_chatId": "c1", "_from": "u2",
           "_text": None, "_contentType": 1, "_createdTime": 1718600010000,
           "_contentMetadata": None}
    result = parse_message_row(row, {"u2": "李小美"})
    assert result["type"] == "image"
    assert result["sender"] == "李小美"


def test_parse_message_row_unknown_type_surfaces_code():
    row = {"_from": "u1", "_contentType": 99, "_createdTime": 1718600000000,
           "_text": None, "_contentMetadata": None}
    result = parse_message_row(row, {})
    assert result["type"] == "type_99"  # not silently dropped


def test_dbreader_list_chats_resolves_group_name(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    chats = reader.list_chats()
    by_id = {c["chat_id"]: c for c in chats}
    assert by_id["c1"]["name"] == "家族群"
    assert by_id["c1"]["type"] == "group"
    assert by_id["u2"]["name"] == "李小美"
    assert by_id["u2"]["type"] == "personal"


def test_dbreader_list_chats_filters_by_type(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    groups = reader.list_chats(chat_type="group")
    assert len(groups) == 1 and groups[0]["chat_id"] == "c1"


def test_dbreader_get_history(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    msgs = reader.get_history("c1", since_ts=0, until_ts=9999999999, limit=10)
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "王小明"
    assert "https://youtu.be/xxx" in msgs[0]["urls"]
    assert msgs[1]["type"] == "image"


def test_dbreader_get_history_resolves_square_member_sender(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    msgs = reader.get_history("sqc1", since_ts=0, until_ts=9999999999, limit=10)
    assert len(msgs) == 1
    assert msgs[0]["sender"] == "開放成員A"  # resolved from _squareMember, not _contact


def test_dbreader_get_contacts(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    contacts = reader.get_contacts()
    names = [c["display_name"] for c in contacts]
    assert "王小明" in names
