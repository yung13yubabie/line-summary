"""Unsupported hosts must reject reads without a child or database fallback."""
import sys
import traceback

import apsw
import pytest

import db_reader
import readonly_client
from readonly_errors import SnapshotUnavailable


@pytest.mark.skipif(sys.platform == "win32", reason="unsupported-host contract runs on Linux CI")
@pytest.mark.parametrize("entry", ["worker", "open", "probe", "preflight"])
def test_non_windows_rejects_before_spawn_or_database_access(monkeypatch, tmp_path, capsys, entry):
    sentinel = "SYNTHETIC_UNSUPPORTED_PRIVATE_SENTINEL"
    path = tmp_path / (sentinel + ".edb")
    path.write_bytes(b"synthetic bytes; not a database")
    before = path.read_bytes()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unsupported host reached spawn or database fallback")

    monkeypatch.setattr(readonly_client.subprocess, "Popen", forbidden)
    monkeypatch.setattr(apsw, "Connection", forbidden)
    monkeypatch.setattr(db_reader.sqlite3, "connect", forbidden)
    readers = {
        "worker": lambda: readonly_client.WorkerConnection(str(path), sentinel),
        "open": lambda: db_reader._open_encrypted(str(path), sentinel),
        "probe": lambda: db_reader.probe_key(str(path), sentinel),
        "preflight": lambda: db_reader.preflight_db_access(str(path)),
    }
    expected = db_reader.DbAccessError if entry == "preflight" else SnapshotUnavailable
    with pytest.raises(expected) as caught:
        readers[entry]()
    expected_message = (
        "unexpected DB access error; cipher and file state are unverified"
        if entry == "preflight" else "source read unavailable"
    )
    assert str(caught.value) == expected_message
    # Format only the product exception: pytest's caller frame includes the
    # deliberate sentinel variable, which is not a product diagnostic.
    visible = "".join(traceback.format_exception_only(caught.type, caught.value))
    visible += repr(capsys.readouterr())
    assert sentinel not in visible
    assert caught.value.__suppress_context__
    assert calls == []
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]
