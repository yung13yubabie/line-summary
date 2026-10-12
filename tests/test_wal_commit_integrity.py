"""Only synthetic encrypted WAL: distinguish committed damage from legal tails."""
from pathlib import Path
import hashlib
import shutil
import threading
import time

import pytest

from db_reader import _open_encrypted
from readonly_errors import SnapshotUnavailable
from tests.test_readonly_worker import SYNTHETIC_KEY, _digests, _encrypted_writer


@pytest.mark.parametrize("mode", ["empty", "tail_short", "committed_byte_corrupt"])
def test_retained_shm_commit_cannot_be_lost_as_success(tmp_path, mode):
    live = tmp_path / "writer.edb"
    source = tmp_path / "damaged.edb"
    writer = _encrypted_writer(live)
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
        wal = Path(str(source) + "-wal")
        data = bytearray(wal.read_bytes())
        if mode == "empty":
            data = bytearray()
        elif mode == "tail_short":
            data = data[:-1]
        else:
            data[32 + 24 + 100] ^= 1
        wal.write_bytes(data)
        before = _digests(source)
        with pytest.raises(SnapshotUnavailable):
            _open_encrypted(str(source), SYNTHETIC_KEY)
        assert _digests(source) == before
    finally:
        writer.close()


@pytest.mark.parametrize("mode", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
def test_legal_checkpoint_keeps_current_committed_data(tmp_path, mode):
    source = tmp_path / "checkpoint.edb"
    writer = _encrypted_writer(source)
    try:
        assert list(writer.execute("PRAGMA wal_checkpoint(" + mode + ")"))[0][0] == 0
        before = _digests(source)
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert list(reader.execute("SELECT value FROM event ORDER BY value")) == [
                {"value": 1}, {"value": 2}]
        finally:
            reader.close()
        assert _digests(source) == before
    finally:
        writer.close()


def test_active_writer_uncommitted_spilled_frames_do_not_hide_old_commit(tmp_path):
    source = tmp_path / "active-writer.edb"
    writer = _encrypted_writer(source)
    writer.execute("PRAGMA cache_size=1")
    writer.execute("CREATE TABLE payload(value BLOB)")
    writer.execute("BEGIN IMMEDIATE")
    writer.executemany("INSERT INTO payload VALUES(?)", [(b"synthetic" * 1500,) for _ in range(12)])
    try:
        # SHM bytes 120..127 are mandatory OS lock slots, never ReadFile data.
        # A live writer holds WRITE120; hash only DB/WAL during that transaction.
        digest_data = lambda: {s: hashlib.sha256(Path(str(source) + s).read_bytes()).hexdigest()
                               for s in ("", "-wal")}
        before = digest_data()
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert list(reader.execute("SELECT value FROM event ORDER BY value")) == [
                {"value": 1}, {"value": 2}]
            assert list(reader.execute("SELECT count(*) AS n FROM payload")) == [{"n": 0}]
        finally:
            reader.close()
        assert digest_data() == before
    finally:
        writer.execute("ROLLBACK")
        writer.close()


@pytest.mark.parametrize("tail_size", [1, 24, 4000])
def test_uncommitted_incomplete_tail_after_declared_commit_is_legal(tmp_path, tail_size):
    live = tmp_path / "writer.edb"
    source = tmp_path / "partial-tail.edb"
    writer = _encrypted_writer(live)
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
        wal = Path(str(source) + "-wal")
        wal.write_bytes(wal.read_bytes() + bytes(tail_size))
        before = _digests(source)
        reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        try:
            assert list(reader.execute("SELECT value FROM event ORDER BY value")) == [
                {"value": 1}, {"value": 2}]
        finally:
            reader.close()
        assert _digests(source) == before
    finally:
        writer.close()


@pytest.mark.parametrize("iteration", range(12))
def test_cancel_acknowledges_clean_native_exit_before_immediate_replace(tmp_path, iteration):
    live = tmp_path / "writer.edb"
    source = tmp_path / "reader.edb"
    writer = _encrypted_writer(live)
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
    finally:
        writer.close()
    replacements = []
    for suffix in ("", "-wal", "-shm"):
        incoming = tmp_path / ("incoming" + suffix)
        incoming.write_bytes(b"synthetic replacement")
        replacements.append((incoming, Path(str(source) + suffix)))
    reader = _open_encrypted(str(source), SYNTHETIC_KEY)
    reader.cancel()
    assert reader._process.returncode == 0  # Explicit SQLite/pins/watcher cleanup, not forced rc=1.
    assert reader._source_release_confirmed
    for incoming, target in replacements:
        incoming.replace(target)  # No hash/read/sleep/write before immediate replacement.


def test_busy_native_query_cancel_is_cooperative_and_immediately_releases(tmp_path):
    live = tmp_path / "writer.edb"
    source = tmp_path / "busy.edb"
    writer = _encrypted_writer(live)
    writer.execute("CREATE TABLE many(value INTEGER)")
    writer.executemany("INSERT INTO many VALUES(?)", [(i,) for i in range(1000)])
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
    finally:
        writer.close()
    replacements = []
    for suffix in ("", "-wal", "-shm"):
        incoming = tmp_path / ("incoming" + suffix)
        incoming.write_bytes(b"synthetic replacement")
        replacements.append((incoming, Path(str(source) + suffix)))
    reader = _open_encrypted(str(source), SYNTHETIC_KEY)
    entered = threading.Event()
    failures = []

    def query():
        entered.set()
        try:
            list(reader.execute("SELECT count(*) FROM many a, many b, many c"))
        except SnapshotUnavailable:
            failures.append("cancelled")

    actor = threading.Thread(target=query)
    actor.start()
    assert entered.wait(1)
    # Yield until the exchange owns its lock; no delay is inserted after cancel.
    deadline = time.monotonic() + 1
    while reader._lock.acquire(blocking=False):
        reader._lock.release()
        if time.monotonic() >= deadline:
            raise AssertionError("query did not enter exchange")
    reader.cancel()
    assert reader._process.returncode == 0 and reader._source_release_confirmed
    for incoming, target in replacements:
        incoming.replace(target)
    actor.join(1)
    assert not actor.is_alive() and failures == ["cancelled"]
