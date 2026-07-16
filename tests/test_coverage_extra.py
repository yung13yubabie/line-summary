"""Offline unit tests closing pure-logic coverage gaps (no live LINE needed).
OS boundaries (real memory scan, apsw decryption) are covered by test_integration.
"""
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import db_reader as dbr
import line_mcp_server as srv
from db_reader import DbReader, parse_message_row
from tests.test_db_reader import _make_test_db


# --- parse_message_row: every content-type branch --------------------------
def _row(ct, text=None, meta=None):
    return {"_from": "u1", "_contentType": ct, "_createdTime": 1718600000000,
            "_text": text, "_contentMetadata": meta}


def test_parse_sticker():
    assert parse_message_row(_row(7), {})["content"] == "[貼圖]"


def test_parse_file_uses_metadata_filename():
    r = parse_message_row(_row(14, meta=json.dumps({"FILE_NAME": "a.pdf"})), {})
    assert r["type"] == "file" and r["filename"] == "a.pdf"


def test_parse_link_uses_metadata():
    r = parse_message_row(
        _row(16, meta=json.dumps({"url": "http://x", "title": "T", "desc": "D"})), {})
    assert r["url"] == "http://x" and r["title"] == "T" and r["description"] == "D"


def test_parse_video_audio_location_contact_generic():
    for ct, name in [(2, "video"), (3, "audio"), (6, "location"), (13, "contact")]:
        assert parse_message_row(_row(ct, text="x"), {})["type"] == name


def test_parse_meta_invalid_json_is_empty():
    r = parse_message_row(_row(16, meta="{not json"), {})
    assert r["url"] is None  # _meta swallowed the bad JSON


# --- _resolve_chat: open / multi / unknown branches ------------------------
def _make_resolve_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER)")
    conn.execute("CREATE TABLE _squareChat (_squareChatMid TEXT PRIMARY KEY, _name TEXT)")
    conn.execute("CREATE TABLE _room (_mid TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO _chat VALUES ('sq1', 3)")
    conn.execute("INSERT INTO _chat VALUES ('rm1', 2)")
    conn.execute("INSERT INTO _chat VALUES ('x1', 1)")
    conn.execute("INSERT INTO _squareChat VALUES ('sq1', '開放群')")
    conn.execute("INSERT INTO _room VALUES ('rm1')")
    conn.commit()
    conn.close()


def test_resolve_chat_open_multi_unknown(tmp_path):
    db = str(tmp_path / "r.db")
    _make_resolve_db(db)
    chats = {c["chat_id"]: c for c in DbReader(db, key=None, _test_mode=True).list_chats()}
    assert chats["sq1"]["type"] == "open" and chats["sq1"]["name"] == "開放群"
    assert chats["rm1"]["type"] == "multi" and chats["rm1"]["name"] == "多人聊天"
    assert chats["x1"]["type"] == "unknown"


# --- query filters ---------------------------------------------------------
def test_list_chats_query_filters_by_name(tmp_path):
    db = str(tmp_path / "q.db")
    _make_test_db(db)
    res = DbReader(db, key=None, _test_mode=True).list_chats(query="家族")
    assert [c["chat_id"] for c in res] == ["c1"]


def test_get_contacts_query_filters(tmp_path):
    db = str(tmp_path / "q.db")
    _make_test_db(db)
    res = DbReader(db, key=None, _test_mode=True).get_contacts(query="小明")
    assert [c["display_name"] for c in res] == ["王小明"]


# --- line_mcp_server: _parse_iso8601 malformed, settings, wrappers ---------
def test_parse_iso8601_malformed_with_tz_raises():
    with pytest.raises(ValueError, match="Invalid ISO 8601 format"):
        srv._parse_iso8601("2026-13-45T99:99:99+08:00")


def test_load_settings_reads_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"db_path": "X.edb", "media_mode": "vision"}), encoding="utf-8")
    with patch.object(srv, "_SETTINGS_PATH", str(p)):
        s = srv._load_settings()
    assert s["db_path"] == "X.edb" and s["media_mode"] == "vision"
    assert s["timezone"] == "Asia/Taipei"  # defaults merged in


def test_find_edb_path_excludes_prefixes_and_picks_largest(tmp_path):
    (tmp_path / "album_a.edb").write_bytes(b"x" * 100)
    (tmp_path / "qw_small.edb").write_bytes(b"x" * 10)
    (tmp_path / "qw_big.edb").write_bytes(b"x" * 50)
    got = srv._find_edb_path(str(tmp_path))
    assert got.endswith("qw_big.edb")  # album_ excluded, largest main chosen


def _fake_reader():
    r = MagicMock()
    r.list_chats.return_value = ["chat"]
    r.get_history.return_value = ["msg"]
    r.get_unread.return_value = ["unread"]
    r.get_contacts.return_value = ["contact"]
    return r


def test_tool_wrappers_delegate_to_reader():
    fake = _fake_reader()
    with patch.object(srv, "_get_reader", return_value=fake):
        assert srv.line_list_chats() == ["chat"]
        assert srv.line_get_history("c", "2026-01-01T00:00:00+08:00",
                                    "2026-01-02T00:00:00+08:00") == ["msg"]
        assert srv.line_get_unread() == ["unread"]
        assert srv.line_get_contacts() == ["contact"]


def test_get_reader_caches_and_builds(monkeypatch):
    srv._reader = None
    monkeypatch.setattr(srv, "_find_edb_path", lambda: "main.edb")
    monkeypatch.setattr(srv, "extract_key", lambda path, require_consent: "k" * 32)
    built = MagicMock()
    monkeypatch.setattr(srv, "DbReader", lambda db, key: built)
    r1 = srv._get_reader()
    r2 = srv._get_reader()
    assert r1 is built and r2 is built  # cached, DbReader built once
    srv._reader = None


def test_get_reader_raises_without_db(monkeypatch):
    srv._reader = None
    monkeypatch.setattr(srv, "_find_edb_path", lambda: None)
    with pytest.raises(RuntimeError, match="not found"):
        srv._get_reader()
    srv._reader = None


def test_get_reader_raises_when_key_declined(monkeypatch):
    srv._reader = None
    monkeypatch.setattr(srv, "_find_edb_path", lambda: "main.edb")
    monkeypatch.setattr(srv, "extract_key", lambda path, require_consent: None)
    with pytest.raises(RuntimeError, match="declined or key extraction failed"):
        srv._get_reader()
    srv._reader = None


# --- key_extractor: consent, pid parse error, extract_key orchestration ----
def test_confirm_user_consent_yes(capsys):
    import key_extractor as ke
    with patch("builtins.input", return_value="yes"):
        assert ke.confirm_user_consent() is True


def test_confirm_user_consent_no():
    import key_extractor as ke
    with patch("builtins.input", return_value="nope"):
        assert ke.confirm_user_consent() is False


def test_find_line_pid_non_integer_field_returns_none():
    import key_extractor as ke
    with patch("subprocess.run") as m:
        m.return_value = MagicMock(stdout='"LINE.exe","notapid","Console"\n')
        assert ke.find_line_pid() is None


def test_extract_key_consent_declined_returns_none():
    import key_extractor as ke
    with patch.object(ke, "confirm_user_consent", return_value=False):
        assert ke.extract_key("x.edb", require_consent=True) is None


def test_extract_key_raises_when_line_not_running():
    import key_extractor as ke
    with patch.object(ke, "find_line_pid", return_value=None):
        with pytest.raises(RuntimeError, match="not running"):
            ke.extract_key("x.edb", require_consent=False)


def test_extract_key_raises_when_no_candidates():
    import key_extractor as ke
    with patch.object(ke, "find_line_pid", return_value=123), \
         patch.object(ke, "_scan_memory_regions", return_value=[]):
        with pytest.raises(RuntimeError, match="read LINE process memory"):
            ke.extract_key("x.edb", require_consent=False)


def test_extract_key_returns_first_matching_candidate():
    import key_extractor as ke
    with patch.object(ke, "find_line_pid", return_value=123), \
         patch.object(ke, "_scan_memory_regions", return_value=["a" * 32, "b" * 32]), \
         patch("db_reader.probe_key", side_effect=[False, True]):
        assert ke.extract_key("x.edb", require_consent=False) == "b" * 32


def test_extract_key_raises_when_no_candidate_decrypts():
    import key_extractor as ke
    with patch.object(ke, "find_line_pid", return_value=123), \
         patch.object(ke, "_scan_memory_regions", return_value=["a" * 32]), \
         patch("db_reader.probe_key", return_value=False):
        with pytest.raises(RuntimeError, match="none decrypted"):
            ke.extract_key("x.edb", require_consent=False)


# --- small defensive branches ----------------------------------------------
def test_ts_to_iso_none_returns_none():
    assert dbr._ts_to_iso(None) is None


def test_list_chats_limit_breaks_early(tmp_path):
    db = str(tmp_path / "l.db")
    _make_test_db(db)
    assert len(DbReader(db, key=None, _test_mode=True).list_chats(limit=1)) == 1


def _make_bare_unread_db(path):
    """Only _chat + _message (no _contact/_group): exercises get_unread's safe()
    degradation and the null-firstUnreadId path."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, "
        "_unreadCount INTEGER, _firstUnreadId TEXT)"
    )
    conn.execute(
        "CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, "
        "_createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)"
    )
    conn.execute("INSERT INTO _chat VALUES ('a', 1, 2, NULL)")  # unread but no boundary
    conn.commit()
    conn.close()


def test_get_unread_null_boundary_and_missing_contact_table(tmp_path):
    db = str(tmp_path / "bare.db")
    _make_bare_unread_db(db)
    unread = DbReader(db, key=None, _test_mode=True).get_unread()
    assert len(unread) == 1
    a = unread[0]
    assert a["unread_count"] == 2
    assert a["available_count"] == 0 and a["missing_count"] == 2
    assert a["messages"] == []
    assert a["type"] == "unknown"
