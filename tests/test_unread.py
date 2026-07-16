"""Unit tests for unread-message reading (offline, via _test_mode sqlite).

available_count is capped by the authoritative _unreadCount and by how many
messages are actually present locally (LINE syncs bodies lazily). It can never
exceed unread_count -- a regression guard for the boundary-range blow-up that an
integration test caught (unread_count=1 but 97k "available").
"""
import sqlite3
from db_reader import DbReader


def _make_unread_db(path: str):
    """Synthetic DB with the unread-relevant columns of the real LINE 26.3 schema."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE _chat (_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, "
        "_unreadCount INTEGER, _firstUnreadId TEXT)"
    )
    conn.execute("CREATE TABLE _groupChat (_chatMid TEXT PRIMARY KEY, _chatName TEXT)")
    conn.execute(
        "CREATE TABLE _contact (_mid TEXT PRIMARY KEY, _displayName TEXT, "
        "_displayNameOverridden TEXT, _type INTEGER)"
    )
    conn.execute(
        "CREATE TABLE _message (_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, "
        "_createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)"
    )
    conn.execute("INSERT INTO _contact VALUES ('u1', '王小明', NULL, 0)")
    conn.execute("INSERT INTO _contact VALUES ('off1', '某官方帳號', NULL, 16)")
    for cid, name in [("c1", "家族群"), ("g2", "大群"), ("c2", "空群"), ("c4", "塞爆群")]:
        conn.execute("INSERT INTO _groupChat VALUES (?, ?)", (cid, name))

    def msg(mid, chat, t, text):
        conn.execute(
            "INSERT INTO _message VALUES (?,?,?,?,?,?,?)",
            (mid, chat, "u1", t, text, 0, None),
        )

    # c1: unread 3, 4 messages present -> fully synced, show most-recent 3
    conn.execute("INSERT INTO _chat VALUES ('c1', 4000, 3, NULL)")
    msg("90", "c1", 900, "舊"); msg("100", "c1", 1000, "一")
    msg("101", "c1", 1100, "二"); msg("102", "c1", 1200, "三")

    # g2: unread 5, only 2 present -> genuinely missing 3
    conn.execute("INSERT INTO _chat VALUES ('g2', 3500, 5, NULL)")
    msg("200", "g2", 2000, "甲"); msg("201", "g2", 2100, "乙")

    # c2: unread 2, nothing present -> available 0, honest gap
    conn.execute("INSERT INTO _chat VALUES ('c2', 3000, 2, NULL)")

    # c4: unread 1, 10 messages present -> available capped at 1 (blow-up guard)
    conn.execute("INSERT INTO _chat VALUES ('c4', 2800, 1, NULL)")
    for i in range(10):
        msg(str(300 + i), "c4", 3000 + i, f"m{i}")

    # off1: official 1:1, unread 4, 2 present -> excluded by default
    conn.execute("INSERT INTO _chat VALUES ('off1', 2500, 4, NULL)")
    msg("400", "off1", 7000, "推播一"); msg("401", "off1", 7100, "推播二")

    # c3: read, unread 0 -> never listed
    conn.execute("INSERT INTO _chat VALUES ('c3', 100, 0, NULL)")
    conn.commit()
    conn.close()


def _reader(tmp_path):
    db = str(tmp_path / "unread.db")
    _make_unread_db(db)
    return DbReader(db, key=None, _test_mode=True)


def test_unread_lists_only_unread_and_excludes_official(tmp_path):
    ids = {c["chat_id"] for c in _reader(tmp_path).get_unread()}
    assert ids == {"c1", "g2", "c2", "c4"}  # c3 (read) and off1 (official) excluded


def test_unread_fully_synced_shows_most_recent(tmp_path):
    c1 = {c["chat_id"]: c for c in _reader(tmp_path).get_unread()}["c1"]
    assert c1["name"] == "家族群" and c1["type"] == "group"
    assert c1["unread_count"] == 3
    assert c1["available_count"] == 3 and c1["missing_count"] == 0
    assert c1["fully_synced"] is True
    assert [m["content"] for m in c1["messages"]] == ["一", "二", "三"]  # chronological


def test_unread_partial_sync_reports_missing(tmp_path):
    g2 = {c["chat_id"]: c for c in _reader(tmp_path).get_unread()}["g2"]
    assert g2["unread_count"] == 5
    assert g2["available_count"] == 2 and g2["missing_count"] == 3
    assert g2["fully_synced"] is False
    assert [m["content"] for m in g2["messages"]] == ["甲", "乙"]


def test_unread_nothing_synced_reports_full_gap(tmp_path):
    c2 = {c["chat_id"]: c for c in _reader(tmp_path).get_unread()}["c2"]
    assert c2["unread_count"] == 2
    assert c2["available_count"] == 0 and c2["missing_count"] == 2
    assert c2["messages"] == []


def test_unread_available_never_exceeds_unread_count(tmp_path):
    # c4 has 10 messages present but only 1 unread -> the blow-up regression guard.
    c4 = {c["chat_id"]: c for c in _reader(tmp_path).get_unread()}["c4"]
    assert c4["unread_count"] == 1
    assert c4["available_count"] == 1 and c4["missing_count"] == 0
    assert len(c4["messages"]) == 1


def test_unread_include_official_when_requested(tmp_path):
    unread = {c["chat_id"]: c for c in _reader(tmp_path).get_unread(include_official=True)}
    assert "off1" in unread
    assert unread["off1"]["available_count"] == 2  # min(unread 4, present 2)


def test_unread_per_chat_limit_caps_messages_not_available(tmp_path):
    c1 = {c["chat_id"]: c
          for c in _reader(tmp_path).get_unread(per_chat_limit=2)}["c1"]
    assert c1["available_count"] == 3       # count not limited by display cap
    assert len(c1["messages"]) == 2          # display limited
    assert [m["content"] for m in c1["messages"]] == ["二", "三"]


def test_unread_limit_chats(tmp_path):
    assert len(_reader(tmp_path).get_unread(limit_chats=1)) == 1
