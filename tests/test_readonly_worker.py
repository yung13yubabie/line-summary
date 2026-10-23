"""Synthetic SQLite3MultipleCiphers WAL boundary checks; never opens LINE data."""

from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import apsw
import pytest
from tests.native_platform import WINDOWS_NATIVE

from db_reader import DbReader, _open_encrypted
if os.name == "nt":
    from readonly_guard import CREATE, GENERIC_READ, Guard, INVALID
from readonly_errors import SnapshotUnavailable


SYNTHETIC_KEY = "A1b2" * 8


def _encrypted_writer(path):
    connection = apsw.Connection(str(path))
    connection.execute("PRAGMA cipher='aes128cbc'")
    connection.execute(f"PRAGMA key='{SYNTHETIC_KEY}'")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE event(value INTEGER)")
    connection.execute("INSERT INTO event VALUES(1)")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO event VALUES(2)")
    return connection


def _digests(path):
    return {suffix: hashlib.sha256(Path(str(path) + suffix).read_bytes()).hexdigest()
            if Path(str(path) + suffix).exists() else None
            for suffix in ("", "-wal", "-shm")}


@WINDOWS_NATIVE
def test_active_encrypted_wal_is_read_once_without_source_byte_changes(tmp_path):
    source = tmp_path / "active.edb"
    writer = _encrypted_writer(source)
    before = _digests(source)
    try:
        connection = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert list(connection.execute("SELECT value FROM event ORDER BY value")) == [
                {"value": 1}, {"value": 2}]
            assert connection.diagnostics["read_opens"] >= 3
            assert connection.diagnostics["os_lock_calls"] > 0
        finally:
            connection.close()
        assert connection._process.poll() is not None
        assert _digests(source) == before
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_missing_source_shm_fails_without_creating_it(tmp_path):
    live = tmp_path / "live.edb"
    writer = _encrypted_writer(live)
    source = tmp_path / "missing-shm.edb"
    try:
        shutil.copyfile(live, source)
        shutil.copyfile(Path(str(live) + "-wal"), Path(str(source) + "-wal"))
        shm = Path(str(source) + "-shm")
        assert not shm.exists()
        with pytest.raises(SnapshotUnavailable, match="source SHM is missing or unreadable"):
            connection = _open_encrypted(str(source), SYNTHETIC_KEY)
            connection.close()
        assert not shm.exists()
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_invalid_static_shm_uses_safe_read_path_or_fails(tmp_path):
    live = tmp_path / "live.edb"
    writer = _encrypted_writer(live)
    source = tmp_path / "invalid-shm.edb"
    try:
        shutil.copyfile(live, source)
        shutil.copyfile(Path(str(live) + "-wal"), Path(str(source) + "-wal"))
        Path(str(source) + "-shm").write_bytes(bytes(32768))
        before = _digests(source)
        try:
            connection = _open_encrypted(str(source), SYNTHETIC_KEY)
        except SnapshotUnavailable:
            pass
        else:
            try:
                assert list(connection.execute("SELECT value FROM event ORDER BY value")) == [
                    {"value": 1}, {"value": 2}]
            finally:
                connection.close()
        assert _digests(source) == before
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_damaged_wal_header_never_returns_old_base_as_success(tmp_path):
    live = tmp_path / "live.edb"
    writer = _encrypted_writer(live)
    source = tmp_path / "bad-wal.edb"
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
        wal = Path(str(source) + "-wal")
        data = wal.read_bytes()
        wal.write_bytes(b"BAD!" + data[4:])
        before = _digests(source)
        with pytest.raises(SnapshotUnavailable):
            _open_encrypted(str(source), SYNTHETIC_KEY)
        assert _digests(source) == before
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_wrong_key_is_redacted_and_no_key_in_argv(tmp_path, capsys):
    source = tmp_path / "private-sentinel.edb"
    writer = _encrypted_writer(source)
    wrong_key = "f" * 64
    captured_argv = []
    import readonly_client
    original = readonly_client.subprocess.Popen

    def spy(argv, **kwargs):
        captured_argv.extend(argv)
        return original(argv, **kwargs)

    try:
        with patch.object(readonly_client.subprocess, "Popen", spy):
            with pytest.raises(Exception, match="not a database") as caught:
                _open_encrypted(str(source), wrong_key)
        assert wrong_key not in " ".join(map(str, captured_argv))
        assert str(source) not in " ".join(map(str, captured_argv))
        visible = str(caught.value) + repr(capsys.readouterr())
        assert wrong_key not in visible and "private-sentinel" not in visible
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_reader_holds_snapshot_across_two_writers_and_checkpoint(tmp_path):
    source = tmp_path / "concurrent.edb"
    first_writer = _encrypted_writer(source)
    second_writer = apsw.Connection(str(source))
    second_writer.execute("PRAGMA cipher='aes128cbc'")
    second_writer.execute(f"PRAGMA key='{SYNTHETIC_KEY}'")
    try:
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert [row["value"] for row in reader.execute("SELECT value FROM event ORDER BY value")] == [1, 2]
            first_writer.execute("INSERT INTO event VALUES(3)")
            second_writer.execute("INSERT INTO event VALUES(4)")
            assert list(first_writer.execute("PRAGMA wal_checkpoint(PASSIVE)"))[0][2] == 0
            first_writer.set_busy_timeout(50)
            assert list(first_writer.execute("PRAGMA wal_checkpoint(TRUNCATE)"))[0][0] == 1
            assert [row["value"] for row in reader.execute("SELECT value FROM event ORDER BY value")] == [1, 2]
        finally:
            reader.close()
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert [row["value"] for row in reader.execute("SELECT value FROM event ORDER BY value")] == [1, 2, 3, 4]
        finally:
            reader.close()
    finally:
        second_writer.close()
        first_writer.close()


@WINDOWS_NATIVE
def test_source_replacement_is_blocked_while_reader_pins_handles(tmp_path):
    source = tmp_path / "pinned.edb"
    writer = _encrypted_writer(source)
    replacement = tmp_path / "replacement.edb"
    replacement.write_bytes(b"synthetic replacement")
    try:
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            with pytest.raises(PermissionError):
                os.replace(replacement, source)
            assert [row["value"] for row in reader.execute("SELECT value FROM event ORDER BY value")] == [1, 2]
        finally:
            reader.close()
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_callback_exception_fails_closed_and_handles_release(tmp_path):
    source = tmp_path / "callback.edb"
    writer = _encrypted_writer(source)
    guard = Guard(str(source))
    original_info = guard.info
    try:
        def fail(_handle):
            raise RuntimeError("synthetic callback fault")

        guard.info = fail
        create = CREATE(guard.control.xGetSystemCall("CreateFileW"))
        handle = create(str(source), GENERIC_READ, 3, None, 3, 0, None)
        assert handle == INVALID
        assert guard.counts["callback_errors"] == 1
    finally:
        guard.info = original_info
        guard.close()
        writer.close()
    assert not guard.pins


@WINDOWS_NATIVE
def test_repeated_start_stop_and_bounded_cancel(tmp_path):
    source = tmp_path / "repeated.edb"
    writer = _encrypted_writer(source)
    before = _digests(source)
    try:
        for _ in range(4):
            reader = _open_encrypted(str(source), SYNTHETIC_KEY)
            assert list(reader.execute("SELECT count(*) AS n FROM event")) == [{"n": 2}]
            reader.close()
            assert reader._process.poll() is not None
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        reader._deadline = time.monotonic() - 1
        with pytest.raises(SnapshotUnavailable):
            list(reader.execute("SELECT value FROM event"))
        reader.close()  # Failed exchanges now reap immediately; close is idempotent.
        assert reader._process.poll() is not None
        assert _digests(source) == before
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_sql_cannot_change_connection_settings(tmp_path):
    source = tmp_path / "read-only.edb"
    writer = _encrypted_writer(source)
    try:
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            with pytest.raises(SnapshotUnavailable):
                reader.execute("PRAGMA writable_schema=ON")
        finally:
            reader.close()
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_existing_shm_unreadable_fails_clearly(tmp_path):
    source = tmp_path / "active.edb"
    writer = _encrypted_writer(source)
    clone = tmp_path / "unreadable.edb"
    import ctypes

    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(source) + suffix), Path(str(clone) + suffix))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = CREATE(("CreateFileW", kernel))
        handle = open_file(str(clone) + "-shm", GENERIC_READ, 0, None, 3, 0, None)
        assert handle != INVALID
        try:
            with pytest.raises(SnapshotUnavailable, match="source SHM is missing or unreadable"):
                _open_encrypted(str(clone), SYNTHETIC_KEY)
        finally:
            kernel.CloseHandle(handle)
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_production_reader_preserves_all_four_query_methods(tmp_path):
    source = tmp_path / "four-tools.edb"
    writer = _encrypted_writer(source)
    try:
        writer.execute("CREATE TABLE _chat(_id TEXT PRIMARY KEY, _lastUpdatedTime INTEGER, _unreadCount INTEGER)")
        writer.execute("CREATE TABLE _groupChat(_chatMid TEXT PRIMARY KEY, _chatName TEXT)")
        writer.execute("CREATE TABLE _contact(_mid TEXT PRIMARY KEY, _displayName TEXT, _displayNameOverridden TEXT)")
        writer.execute("CREATE TABLE _message(_id TEXT PRIMARY KEY, _chatId TEXT, _from TEXT, "
                       "_createdTime INTEGER, _text TEXT, _contentType INTEGER, _contentMetadata TEXT)")
        writer.execute("INSERT INTO _chat VALUES('c1', 1718600010000, 1)")
        writer.execute("INSERT INTO _groupChat VALUES('c1', 'Synthetic group')")
        writer.execute("INSERT INTO _contact VALUES('u1', 'Synthetic user', NULL)")
        writer.execute("INSERT INTO _message VALUES('m1','c1','u1',1718600000000,'Synthetic hello',0,NULL)")
        reader = DbReader(str(source), SYNTHETIC_KEY)
        assert reader.list_chats()["items"][0]["chat_id"] == "c1"
        assert reader.get_history("c1", 1718599999000, 1718600020000)["items"][0]["content"] == "Synthetic hello"
        unread = reader.get_unread()["items"]
        assert len(unread) == 1 and unread[0]["returned_count"] == 1
        assert reader.get_contacts()["items"][0]["display_name"] == "Synthetic user"
    finally:
        writer.close()


@WINDOWS_NATIVE
def test_pipe_input_and_output_are_bounded(tmp_path):
    source = tmp_path / "bounds.edb"
    writer = _encrypted_writer(source)
    try:
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            with pytest.raises(SnapshotUnavailable, match="read request limit exceeded"):
                reader.execute("SELECT ?", ("synthetic" * 3000,))
        finally:
            reader.close()
        writer.execute("INSERT INTO event VALUES(?)", ("synthetic" * 150000,))
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            with pytest.raises(SnapshotUnavailable, match="source read unavailable"):
                list(reader.execute("SELECT value FROM event ORDER BY rowid DESC LIMIT 1"))
        finally:
            reader.close()
    finally:
        writer.close()


def test_source_boundary_error_in_metadata_query_is_not_suppressed():
    class FailingConnection:
        def execute(self, _sql):
            raise SnapshotUnavailable("source read unavailable")

        def close(self):
            pass

    reader = DbReader("synthetic-only.edb", SYNTHETIC_KEY)
    reader._open = FailingConnection
    with pytest.raises(SnapshotUnavailable, match="source read unavailable"):
        reader.list_chats()


@WINDOWS_NATIVE
def test_blocked_pipe_write_obeys_deadline_and_reaps_child():
    from readonly_client import WorkerConnection
    import readonly_client
    original = subprocess.Popen
    children = []

    def spawn(_argv, **kwargs):
        process = original([sys.executable, "-I", "-B", "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(process)
        return process

    start = time.monotonic()
    with patch.object(readonly_client, "SESSION_SECONDS", 0.4), \
            patch.object(readonly_client, "REAP_SECONDS", 0.1), \
            patch.object(readonly_client.subprocess, "Popen", spawn):
        with pytest.raises(SnapshotUnavailable):
            WorkerConnection("synthetic-only.edb", "x" * 12000)
        assert time.monotonic() - start < 1.0
    assert children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed
    assert children[0]._handle.closed
