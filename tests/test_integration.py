"""Integration tests against the live LINE PC (the OS-boundary coverage layer).

These cover real process-memory key extraction and apsw/wxSQLite3 decryption.
They ALWAYS skip unless --run-live-line is explicitly supplied, even when LINE
is running. Neither module collection nor a default fixture invocation detects
the platform, searches processes/files, or imports the live-access modules.
After a separate privacy review, the owner can opt in locally with:
    python -m pytest --run-live-line -m integration
This permission is only for local testing, not model uploads or sharing data.

The session-scoped fixture pays the ~82s memory scan ONCE and shares the reader.
SECURITY: the key is never printed or asserted by value, only by length.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def live(request):
    # Defense in depth if this fixture is requested by an unmarked test.
    # Keep the permission check before imports and ALL platform/LINE detection.
    if not request.config.getoption("--run-live-line"):
        pytest.skip("live LINE access requires --run-live-line")

    import sys
    if sys.platform != "win32":
        pytest.skip("live LINE integration requires Windows")

    from key_extractor import find_line_pid, extract_key
    from line_mcp_server import _find_edb_path
    from db_reader import DbReader, probe_key

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


def _assert_page(page):
    assert isinstance(page, dict)
    assert isinstance(page["items"], list)
    assert isinstance(page["has_more"], bool)
    assert isinstance(page["content_complete"], bool)
    assert "consistency" in page
    assert (page["next_cursor"] is not None) == page["has_more"]


def test_list_chats_live(live):
    page = live["reader"].list_chats(limit=5)
    _assert_page(page)
    for c in page["items"]:
        assert c["chat_id"]
        assert c["type"] in {"group", "open", "personal", "official", "multi", "unknown"}


def test_get_history_live(live):
    reader = live["reader"]
    chats = reader.list_chats(limit=1)["items"]
    if not chats:
        pytest.skip("no chats in this DB")
    page = reader.get_history(chats[0]["chat_id"], since_ms=0,
                              until_ms=99_999_999_999_000, limit=5)
    _assert_page(page)
    for m in page["items"]:
        assert "message_id" in m and "type" in m and "sent_at" in m


def test_get_unread_live_counts_are_honest(live):
    page = live["reader"].get_unread(limit_chats=10)
    _assert_page(page)
    for u in page["items"]:
        assert u["unread_count"] > 0
        assert u["returned_count"] == len(u["messages"])
        assert u["selection"] == "latest_local_approximation"
        assert u["sync_status"] == "unknown"
        assert u["unread_boundary_verified"] is False
        assert isinstance(u["messages_limited"], bool)
        assert isinstance(u["more_local_messages"], bool)
        assert not {"available_count", "missing_count", "fully_synced"} & u.keys()
        assert "key" not in u  # never leak the key through tool output


def test_get_contacts_live(live):
    page = live["reader"].get_contacts()
    _assert_page(page)
    assert all("contact_id" in c and "display_name" in c for c in page["items"])
