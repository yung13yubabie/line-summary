"""Integration tests against the live LINE PC (the OS-boundary coverage layer).

These cover what unit tests can't fake: real process-memory key extraction and
apsw/wxSQLite3 decryption of the real .edb. They are marked `integration` and
SKIP automatically when LINE is not running, so a plain `pytest` still stays green
on a machine without LINE. Run with LINE open to hit the >=90% coverage gate.

The session-scoped fixture pays the ~82s memory scan ONCE and shares the reader.
SECURITY: the key is never printed or asserted by value, only by length.
"""
import pytest

from key_extractor import find_line_pid, extract_key
from line_mcp_server import _find_edb_path
from db_reader import DbReader, probe_key

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def live():
    if find_line_pid() is None:
        pytest.skip("LINE is not running")
    db = _find_edb_path()
    if not db:
        pytest.skip("no LINE .edb found")
    key = extract_key(db, require_consent=False)  # ~82s memory scan, once
    if not key:
        pytest.skip("key extraction failed")
    # SECURITY: the key MUST NOT appear in any fixture value, because pytest prints
    # fixture reprs on failure. Expose only derived facts + the reader (which keeps
    # the key private); never the key string itself.
    return {
        "db": db,
        "reader": DbReader(db, key),
        "key_len": len(key),
        "probe_ok": probe_key(db, key),
    }


def test_key_extraction_and_probe(live):
    assert live["key_len"] in (32, 64)       # length only; never the value
    assert live["probe_ok"] is True


def test_list_chats_live(live):
    chats = live["reader"].list_chats(limit=5)
    assert isinstance(chats, list)
    for c in chats:
        assert c["chat_id"]
        assert c["type"] in {"group", "open", "personal", "multi", "unknown"}


def test_get_history_live(live):
    reader = live["reader"]
    chats = reader.list_chats(limit=1)
    if not chats:
        pytest.skip("no chats in this DB")
    msgs = reader.get_history(chats[0]["chat_id"], since_ts=0,
                              until_ts=99_999_999_999, limit=5)
    assert isinstance(msgs, list)
    for m in msgs:
        assert "type" in m and "sent_at" in m


def test_get_unread_live_counts_are_honest(live):
    unread = live["reader"].get_unread(limit_chats=10)
    assert isinstance(unread, list)
    for u in unread:
        assert u["unread_count"] > 0
        assert u["available_count"] <= u["unread_count"]
        assert u["missing_count"] == max(0, u["unread_count"] - u["available_count"])
        assert len(u["messages"]) <= u["available_count"]
        assert "key" not in u  # never leak the key through tool output


def test_get_contacts_live(live):
    contacts = live["reader"].get_contacts()
    assert isinstance(contacts, list) and len(contacts) > 0
    assert all("contact_id" in c and "display_name" in c for c in contacts)
