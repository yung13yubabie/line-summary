"""Protocol/ownership policy with fake pipes and virtual time, not native I/O."""
import traceback

import pytest

import readonly_client as client
from readonly_errors import SnapshotUnavailable
from tests.fake_readonly_transport import FakeTransport

OPEN = {"ok": True, "counts": {"read_opens": 3}}
PRIVATE = "synthetic-private-sentinel"


def test_partial_transport_binary_roundtrip_and_private_launch(monkeypatch):
    fake = FakeTransport(monkeypatch, [OPEN, {"ok": True, "cursor": 4},
        {"ok": True, "rows": [{"value": {"__binary__": "AP8="}}], "done": True}],
        write_size=7, read_size=5)
    connection = client.WorkerConnection(PRIVATE + ".edb", "1" * 32)
    assert list(connection.execute("SELECT ?", (b"\x00\xff",))) == [{"value": b"\x00\xff"}]
    assert fake.requests[1]["parameters"] == [{"__binary__": "AP8="}]
    argv, options = fake.spawn_calls[0]
    assert argv[1:3] == ["-I", "-B"] and len(argv) == 4
    assert PRIVATE not in repr(argv) and "1" * 32 not in repr(argv)
    assert "env" not in options and options["close_fds"]
    assert options["stderr"] == client.subprocess.DEVNULL
    assert options["startupinfo"].lpAttributeList == {"handle_list": [713]}
    connection.cancel()
    connection.close()
    fake.assert_released()


@pytest.mark.parametrize("response,exception,message", [
    ({"ok": False, "code": "wrong_key", "detail": PRIVATE}, client.WrongKeyError, "not a database"),
    ({"ok": False, "code": "source_shm_unavailable", "detail": PRIVATE}, SnapshotUnavailable, "source SHM"),
    ({"ok": False, "code": PRIVATE}, SnapshotUnavailable, "source read unavailable"),
    (b'{"ok":"yes","detail":"synthetic-private-sentinel"}\n', SnapshotUnavailable, "source read unavailable"),
    (b'{"ok":true}\n{"ok":true}\n', SnapshotUnavailable, "source read unavailable"),
    (b'not-json-synthetic-private-sentinel\n', SnapshotUnavailable, "source read unavailable"),
])
def test_response_validation_redacts_and_releases(monkeypatch, capsys, response, exception, message):
    fake = FakeTransport(monkeypatch, [response])
    with pytest.raises(exception, match=message) as caught:
        client.WorkerConnection(PRIVATE + ".edb", "1" * 32)
    visible = "".join(traceback.format_exception(caught.value)) + repr(capsys.readouterr())
    assert PRIVATE not in visible and "1" * 32 not in visible
    fake.assert_released()


def test_query_failure_does_not_close_healthy_session(monkeypatch):
    fake = FakeTransport(monkeypatch, [OPEN, {"ok": False, "code": "query_failed", "detail": PRIVATE}])
    connection = client.WorkerConnection("synthetic.edb", "1" * 32)
    with pytest.raises(client.WorkerQueryError, match="read query failed"):
        connection.execute("SELECT absent")
    assert not connection._released
    connection.cancel()
    fake.assert_released()


def test_request_limit_prevents_write(monkeypatch):
    fake = FakeTransport(monkeypatch, [OPEN])
    connection = client.WorkerConnection("synthetic.edb", "1" * 32)
    with pytest.raises(SnapshotUnavailable, match="request limit"):
        connection._request({"op": "query", "sql": "x" * client.MAX_REQUEST})
    assert len(fake.requests) == 1 and fake.input == b""
    connection.cancel()
    fake.assert_released()


def test_reply_limit_disposes_without_unbounded_buffer(monkeypatch):
    fake = FakeTransport(monkeypatch, [b"x" * (client.MAX_REPLY + 1)])
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic.edb", "1" * 32)
    fake.assert_released()
    assert fake.pending == b""
    assert fake.now < 1  # Size rejection must be immediate, not the 23-second I/O timeout.


def test_absent_reply_consumes_shared_budget_and_releases(monkeypatch):
    fake = FakeTransport(monkeypatch, [])
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection("synthetic.edb", "1" * 32)
    assert client.SESSION_SECONDS - client.REAP_SECONDS <= fake.now <= client.SESSION_SECONDS
    fake.assert_released()


def test_expired_spawn_sends_no_secrets(monkeypatch):
    fake = FakeTransport(monkeypatch, [], spawn_delay=client.SESSION_SECONDS + 1)
    with pytest.raises(SnapshotUnavailable):
        client.WorkerConnection(PRIVATE + ".edb", "1" * 32)
    assert fake.requests == [] and fake.input == b"" and fake.kills == 1
    fake.assert_released()


def test_cleanup_retry_keeps_owned_handle(monkeypatch):
    fake = FakeTransport(monkeypatch, [OPEN])
    connection = client.WorkerConnection("synthetic.edb", "1" * 32)
    fake.wait_failures = 1
    with pytest.raises(SnapshotUnavailable, match="cleanup incomplete") as caught:
        connection.cancel()
    assert caught.value.worker is connection and not connection._released and not fake._handle.closed
    assert PRIVATE not in str(caught.value)
    connection.cancel()
    connection.close()
    fake.assert_released()


def test_keyboard_interrupt_cleans_before_propagating(monkeypatch):
    fake = FakeTransport(monkeypatch, [OPEN])
    connection = client.WorkerConnection("synthetic.edb", "1" * 32)

    def interrupt(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(client.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        connection._request({"op": "query"})
    fake.assert_released()


@pytest.mark.parametrize("delay,exception,forced", [
    (0.20, client.WrongKeyError, False), (0.30, SnapshotUnavailable, True),
])
def test_wrong_key_cleanup_boundary_remains_fail_closed(monkeypatch, delay, exception, forced):
    """Characterize the grace boundary, not a claim that historical failure is fixed."""
    fake = FakeTransport(monkeypatch, [{"ok": False, "code": "wrong_key"}], exit_after_cancel=delay)
    with pytest.raises(exception) as caught:
        client.WorkerConnection("synthetic.edb", "1" * 32)
    assert bool(fake.kills) is forced
    if forced:
        assert not caught.value.worker._source_release_confirmed
        assert isinstance(caught.value.__context__, client.WrongKeyError)
        for cleanup in (caught.value.worker.close, caught.value.worker.cancel):
            with pytest.raises(SnapshotUnavailable, match="cleanup incomplete"):
                cleanup()
    fake.assert_released()
