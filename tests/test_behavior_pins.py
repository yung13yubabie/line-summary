"""Behavior-pinning tests added after mutation testing found real gaps: existing
tests asserted key-presence / loose substrings, letting mutants survive. These
pin the actual values so the mutation is caught.
"""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import db_reader
import line_mcp_server as srv
from db_reader import _ts_to_iso, parse_message_row, preflight_db_access, DbAccessError, DbReader


# gap 2/3: timestamp scale (/1000) and Taipei offset were never asserted
def test_ts_to_iso_pins_scale_and_offset():
    ms = 1718600000000
    expected = datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8))).isoformat()
    got = _ts_to_iso(ms)
    assert got == expected
    assert got.endswith("+08:00")


# gap 1: preflight classification must produce a SPECIFIC message per branch, not
# just any DbAccessError whose interpolated cause happens to contain the keyword.
def test_preflight_lock_message_is_specific(tmp_path):
    p = tmp_path / "x.edb"; p.write_bytes(b"x")
    with patch.object(db_reader, "_open_encrypted", side_effect=Exception("database is locked")):
        with pytest.raises(DbAccessError) as ei:
            preflight_db_access(str(p))
    assert str(ei.value).startswith("database is locked")  # not "unexpected..."


def test_preflight_permission_message_is_specific(tmp_path):
    p = tmp_path / "x.edb"; p.write_bytes(b"x")
    with patch.object(db_reader, "_open_encrypted", side_effect=Exception("Access is denied")):
        with pytest.raises(DbAccessError) as ei:
            preflight_db_access(str(p))
    assert str(ei.value).startswith("cannot open the DB file")


def test_preflight_unexpected_message_is_specific(tmp_path):
    p = tmp_path / "x.edb"; p.write_bytes(b"x")
    with patch.object(db_reader, "_open_encrypted", side_effect=Exception("weird boom")):
        with pytest.raises(DbAccessError) as ei:
            preflight_db_access(str(p))
    assert str(ei.value).startswith("unexpected DB access error")


# gap 3: get_history time bounds (seconds*1000 vs ms) actually filter
def _hist_db(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE _contact (_mid TEXT, _displayName TEXT, _displayNameOverridden TEXT)")
    conn.execute("CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, _createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)")
    conn.execute("INSERT INTO _message VALUES ('a','c','u',1000,'early',0,NULL)")   # 1s
    conn.execute("INSERT INTO _message VALUES ('b','c','u',5000,'inrange',0,NULL)")  # 5s
    conn.commit(); conn.close()


def test_get_history_bounds_filter_by_time(tmp_path):
    db = str(tmp_path / "h.db"); _hist_db(db)
    r = DbReader(db, key=None, _test_mode=True)
    # since=2s (2000ms), until=6s (6000ms): only the 5000ms message qualifies
    msgs = r.get_history("c", since_ts=2, until_ts=6, limit=10)
    assert [m["content"] for m in msgs] == ["inrange"]


# gap 4: line_get_unread must default to EXCLUDING official
def test_line_get_unread_defaults_to_excluding_official():
    fake = MagicMock(); fake.get_unread.return_value = []
    with patch.object(srv, "_get_reader", return_value=fake):
        srv.line_get_unread()
    assert fake.get_unread.call_args.kwargs["include_official"] is False


# gap 5: file/link with empty metadata must not crash (_meta returns {}, not None)
def test_parse_file_with_missing_metadata_does_not_crash():
    row = {"_from": "u", "_contentType": 14, "_createdTime": 1,
           "_text": None, "_contentMetadata": None}
    r = parse_message_row(row, {})
    assert r["type"] == "file" and r["filename"] is None


def test_parse_link_with_missing_metadata_does_not_crash():
    row = {"_from": "u", "_contentType": 16, "_createdTime": 1,
           "_text": None, "_contentMetadata": None}
    r = parse_message_row(row, {})
    assert r["type"] == "link" and r["url"] is None
