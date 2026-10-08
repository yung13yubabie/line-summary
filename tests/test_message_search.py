"""Synthetic-only tests of literal, scoped, bounded message search."""
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from db_reader import DbReader, _ts_to_iso
from safety import json_size


def reader_at(tmp_path, rows=()):
    """Rows are (message_id, chat_id, milliseconds, text, content_type)."""
    path = tmp_path / "message-search-synthetic.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE _message (
                _id TEXT PRIMARY KEY, _chatId TEXT, _createdTime INTEGER,
                _text TEXT, _contentType INTEGER, _from TEXT,
                _contentMetadata TEXT
            );
            CREATE TABLE _contact (
                _mid TEXT PRIMARY KEY, _displayName TEXT,
                _displayNameOverridden TEXT
            );
            INSERT INTO _contact VALUES ('u', 'Synthetic Sender', NULL);
        """)
        conn.executemany(
            "INSERT INTO _message VALUES (?, ?, ?, ?, ?, 'u', NULL)", rows,
        )
    return DbReader(str(path), None, _test_mode=True), path


def messages(count, text="match", timestamp=1000):
    return [(f"m{i:06}", "c", timestamp, text, 0) for i in range(count)]


def search(reader, query="match", **kwargs):
    return reader.search_messages("c", query, 0, 2000, **kwargs)


def test_more_than_500_equal_timestamps_are_exactly_once(tmp_path):
    reader, path = reader_at(tmp_path, messages(1507))
    ids, cursor = [], None
    for page_number in range(10):
        out = search(reader, limit=5000, cursor=cursor)
        assert len(out["items"]) <= 500
        ids.extend(item["message_id"] for item in out["items"])
        if page_number == 0:
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO _message VALUES ('new-backfill','c',500,'match',0,'u',NULL)"
                )
        if not out["has_more"]:
            break
        cursor = out["next_cursor"]
        assert cursor
    else:
        pytest.fail("Search pagination did not terminate")
    assert ids == [f"m{i:06}" for i in range(1507)]
    assert len(set(ids)) == 1507
    assert out["next_cursor"] is None
    assert out["coverage"] == "local_rows_only"
    assert out["consistency"] == "live_keyset_scan"
    assert out["database_changes_may_affect_pagination"] is True


@pytest.mark.parametrize(("query", "expected"), [
    ("%", ["percent"]),
    ("_", ["underscore"]),
    ("'", ["quote"]),
    ('"', ["double-quote"]),
    ("\\", ["backslash"]),
    ("中文", ["chinese"]),
    ("match", ["lower"]),
    ("MATCH", ["upper"]),
    (" Match ", ["spaced"]),
    ("é", ["composed"]),
    ("e\u0301", ["decomposed"]),
    ("'; DROP TABLE _message;--", []),
])
def test_queries_are_literal_case_sensitive_unicode(tmp_path, query, expected):
    texts = {
        "percent": "100% done", "underscore": "first_last",
        "quote": "it's fine", "double-quote": 'say "hi"',
        "backslash": "C:\\data", "chinese": "測試中文內容",
        "lower": "match", "upper": "MATCH", "spaced": "x Match y",
        "composed": "café", "decomposed": "cafe\u0301", "null": None,
    }
    reader, path = reader_at(tmp_path, [(mid, "c", 1000, text, 0) for mid, text in texts.items()])
    out = search(reader, query)
    assert [item["message_id"] for item in out["items"]] == expected
    assert out["query"] == query
    assert out["match_mode"] == "case_sensitive_literal_substring"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM _message").fetchone()[0] == len(texts)


def test_query_whitespace_is_preserved_and_sql_injection_is_only_text(tmp_path):
    injection = "'; DROP TABLE _message;--"
    reader, _ = reader_at(tmp_path, [
        ("plain", "c", 1000, "needle", 0),
        ("spaced", "c", 1000, " needle ", 0),
        ("tabbed", "c", 1000, "\tneedle\n", 0),
        ("injection", "c", 1000, injection, 0),
    ])
    assert [x["message_id"] for x in search(reader, " needle ")["items"]] == ["spaced"]
    assert [x["message_id"] for x in search(reader, "\tneedle\n")["items"]] == ["tabbed"]
    assert [x["message_id"] for x in search(reader, injection)["items"]] == ["injection"]


@pytest.mark.parametrize("query", [None, 4, True, "", " ", "\t\n\r", "\u3000", "x" * 257, "\ud800"])
def test_invalid_query_fails_before_database_access(tmp_path, monkeypatch, query):
    reader, _ = reader_at(tmp_path)
    blocked = Mock(side_effect=AssertionError("must not open database"))
    monkeypatch.setattr(reader, "_open", blocked)
    with pytest.raises(ValueError, match="query"):
        search(reader, query)
    blocked.assert_not_called()


def test_query_length_limit_counts_unicode_characters(tmp_path):
    text = "字" * 256
    reader, _ = reader_at(tmp_path, messages(1, text=text))
    assert len(search(reader, text)["items"]) == 1


@pytest.mark.parametrize("change", ["chat", "query", "case", "space", "since", "until", "tamper", "restart", "history"])
def test_cursor_is_signed_and_binds_all_search_scope(tmp_path, change):
    reader, path = reader_at(tmp_path, messages(3))
    token = search(reader, limit=1)["next_cursor"]
    kwargs = dict(chat_id="c", query="match", since_ms=0, until_ms=2000, cursor=token)
    if change == "chat":
        kwargs["chat_id"] = "secret"
    elif change == "query":
        kwargs["query"] = "%"
    elif change == "case":
        kwargs["query"] = "MATCH"
    elif change == "space":
        kwargs["query"] = " match"
    elif change == "since":
        kwargs["since_ms"] = 1
    elif change == "until":
        kwargs["until_ms"] = 2001
    elif change == "tamper":
        kwargs["cursor"] = ("A" if token[0] != "A" else "B") + token[1:]
    elif change == "restart":
        reader = DbReader(str(path), None, _test_mode=True)
    elif change == "history":
        kwargs["cursor"] = reader.get_history("c", 0, 2000, limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="cursor"):
        reader.search_messages(**kwargs)


def test_half_open_range_chat_scope_and_null_text(tmp_path):
    rows = [
        ("before", "c", 999, "match", 0),
        ("start", "c", 1000, "match", 0),
        ("last", "c", 1999, "match", 0),
        ("end", "c", 2000, "match", 0),
        ("other-chat", "secret", 1500, "match", 0),
        ("null", "c", 1500, None, 0),
    ]
    reader, _ = reader_at(tmp_path, rows)
    out = reader.search_messages("c", "match", 1000, 2000)
    assert [x["message_id"] for x in out["items"]] == ["start", "last"]
    assert out["range_since_ms"] == 1000
    assert out["range_until_ms"] == 2000


@pytest.mark.parametrize(("since", "until"), [(0, 0), (1, 0), (True, 2), (0, 1.5), ("0", 2), (0, 2**63)])
def test_invalid_range_fails_before_open(tmp_path, monkeypatch, since, until):
    reader, _ = reader_at(tmp_path)
    blocked = Mock(side_effect=AssertionError("must not open database"))
    monkeypatch.setattr(reader, "_open", blocked)
    with pytest.raises(ValueError):
        reader.search_messages("c", "match", since, until)
    blocked.assert_not_called()


def test_byte_limited_pages_advance_at_last_returned_item(tmp_path):
    reader, _ = reader_at(tmp_path, messages(12, text="中文" * 40))
    ids, cursor = [], None
    for _ in range(20):
        out = search(reader, "中文", cursor=cursor, limit=10, max_bytes=2048)
        assert json_size(out) <= 2048
        assert out["items"]
        ids.extend(x["message_id"] for x in out["items"])
        if not out["has_more"]:
            break
        assert out["next_cursor"] != cursor
        cursor = out["next_cursor"]
    else:
        pytest.fail("Byte-limited search did not progress")
    assert ids == [f"m{i:06}" for i in range(12)]


def test_oversize_first_item_cannot_be_silently_skipped(tmp_path):
    reader, _ = reader_at(tmp_path, messages(2, text="match" * 4000))
    out = search(reader, max_bytes=2048)
    assert out["items"] == []
    assert out["has_more"] is True
    assert out["next_cursor"] is None
    assert out["content_complete"] is False
    assert out["blocked_reason"] == "item_exceeds_byte_budget"
    assert json_size(out) <= 2048


def test_oversize_later_item_preserves_a_retryable_cursor(tmp_path):
    reader, _ = reader_at(tmp_path, [
        ("a", "c", 1000, "match", 0),
        ("b", "c", 1000, "match" * 4000, 0),
        ("c", "c", 1000, "match", 0),
    ])
    first = search(reader, max_bytes=2048)
    assert [x["message_id"] for x in first["items"]] == ["a"]
    blocked = search(reader, cursor=first["next_cursor"], max_bytes=2048)
    assert blocked["items"] == []
    assert blocked["next_cursor"] is None
    retry = search(reader, cursor=first["next_cursor"], max_bytes=262144)
    assert [x["message_id"] for x in retry["items"]] == ["b", "c"]


def test_byte_budget_too_small_for_metadata_fails(tmp_path):
    reader, _ = reader_at(tmp_path)
    with pytest.raises(ValueError, match="byte budget"):
        search(reader, max_bytes=10)


def test_source_identifiers_and_freshness_are_scoped_local_provenance(tmp_path):
    reader, _ = reader_at(tmp_path, [
        ("found", "c", 1000, "match", 0),
        ("nonmatching-local", "c", 1500, "something else", 0),
        ("outside-time", "c", 3000, "match", 0),
        ("unallowed-chat", "secret", 9000, "secret text", 0),
    ])
    before = datetime.now(timezone.utc)
    out = search(reader)
    after = datetime.now(timezone.utc)
    item = out["items"][0]
    assert item["chat_id"] == "c"
    assert item["message_id"] == "found"
    assert item["sent_at"] == _ts_to_iso(1000)
    assert item["source_ref"] == {
        "kind": "local_line_message", "chat_id": "c",
        "message_id": "found", "sent_at": _ts_to_iso(1000),
    }
    fetched = datetime.fromisoformat(out["fetched_at"])
    assert before <= fetched <= after
    assert fetched.utcoffset() == timedelta(0)
    assert out["source_sync_at"] is None
    assert out["source_latest_at"] == _ts_to_iso(1500)
    assert out["source_latest_at_scope"] == "chat_and_requested_range"
    assert "line://" not in str(out) and "https://line.me" not in str(out)
    assert "secret" not in str(out) and "unallowed-chat" not in str(out)


def test_empty_scope_reports_unknown_latest_without_global_stats(tmp_path):
    reader, _ = reader_at(tmp_path, [("secret", "other", 1000, "match", 0)])
    out = search(reader)
    assert out["items"] == []
    assert out["has_more"] is False
    assert out["next_cursor"] is None
    assert out["source_latest_at"] is None
    assert "total" not in str(out)


def test_nontext_content_type_still_returns_literal_matched_text(tmp_path):
    reader, _ = reader_at(tmp_path, [("image-caption", "c", 1000, "match caption", 1)])
    item = search(reader)["items"][0]
    assert item["type"] == "image"
    assert item["content"] == "match caption"


@pytest.mark.parametrize("definition", ["_id TEXT", "_id TEXT, PRIMARY KEY(_id, _chatId)"])
def test_unsupported_nonunique_or_composite_id_schema_is_rejected(tmp_path, definition):
    path = tmp_path / "unsupported-synthetic.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE _message (_chatId TEXT, _createdTime INTEGER, _text TEXT, " + definition + ")"
        )
    reader = DbReader(str(path), None, _test_mode=True)
    with pytest.raises(ValueError, match="sole primary key"):
        search(reader)


@pytest.mark.parametrize("bad_id", [None, "", "x" * 257])
def test_null_empty_or_oversize_message_ids_fail_closed(tmp_path, bad_id):
    reader, _ = reader_at(tmp_path, [(bad_id, "c", 1000, "match", 0)])
    with pytest.raises(ValueError, match="IDs must be nonempty"):
        search(reader)


def test_context_expansion_is_not_an_accepted_argument(tmp_path):
    reader, _ = reader_at(tmp_path, messages(1))
    parameters = inspect.signature(reader.search_messages).parameters
    assert "before" not in parameters and "after" not in parameters
    for kwargs in ({"before": 1000}, {"after": 1000}):
        with pytest.raises(TypeError):
            search(reader, **kwargs)


def test_rowid_reuse_is_disclosed_as_live_scan_not_snapshot(tmp_path):
    reader, path = reader_at(tmp_path, messages(2))
    first = search(reader, limit=1)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM _message WHERE _id='m000001'")
        conn.execute("INSERT INTO _message VALUES ('replacement','c',1500,'match',0,'u',NULL)")
    second = search(reader, cursor=first["next_cursor"])
    assert second["items"][0]["message_id"] == "replacement"
    assert second["consistency"] == "live_keyset_scan"
    assert second["database_changes_may_affect_pagination"] is True
