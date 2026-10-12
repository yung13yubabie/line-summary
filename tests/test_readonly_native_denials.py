"""Win32 native VFS adversaries on synthetic files only."""

import os
from pathlib import Path
import shutil

import pytest
from tests.native_platform import WINDOWS_NATIVE

from db_reader import _open_encrypted
from readonly_errors import SnapshotUnavailable
if os.name == "nt":
    from readonly_guard import CREATE, GENERIC_READ, Guard, INVALID
from tests.test_readonly_client_lifecycle import handles
from tests.test_readonly_worker import SYNTHETIC_KEY, _digests, _encrypted_writer

pytestmark = WINDOWS_NATIVE


def test_native_vfs_write_create_delete_truncate_mapping_are_rejected(tmp_path):
    source = tmp_path / "native-denials.edb"
    writer = _encrypted_writer(source)
    before = _digests(source)
    guard = Guard(str(source))
    try:
        assert all(guard.negative_checks(source).values())
        assert guard.counts["denied_open"] == 2
        assert guard.counts["denied_write"] == 1
        assert guard.counts["denied_truncate"] == 1
        assert guard.counts["denied_delete"] == 1
        assert guard.counts["denied_mapping"] == 1
    finally:
        guard.close()
    try:
        assert not guard.pins
        assert not guard.originals
        assert _digests(source) == before
        assert not Path(str(source) + "-forbidden").exists()
    finally:
        writer.close()


def test_callback_failure_after_native_open_does_not_leak_handles(tmp_path):
    source = tmp_path / "callback-handles.edb"
    writer = _encrypted_writer(source)
    guard = Guard(str(source))
    original = guard.info
    baseline = handles()

    def fail(_handle):
        raise RuntimeError("synthetic metadata failure")

    guard.info = fail
    try:
        create = CREATE(guard.control.xGetSystemCall("CreateFileW"))
        for _ in range(16):
            assert create(str(source), GENERIC_READ, 3, None, 3, 0, None) == INVALID
        assert guard.counts["callback_errors"] == 16
        assert handles() <= baseline
    finally:
        guard.info = original
        guard.close()
        writer.close()


@pytest.mark.parametrize("size", [0, 1, 63, 32767])
def test_truncated_shm_reads_consistent_current_wal_or_fails_unchanged(tmp_path, size, record_property):
    live = tmp_path / "live.edb"
    writer = _encrypted_writer(live)
    source = tmp_path / "truncated.edb"
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
        shm = Path(str(source) + "-shm")
        shm.write_bytes(shm.read_bytes()[:size])
        before = _digests(source)
        try:
            reader = _open_encrypted(str(source), SYNTHETIC_KEY)
        except SnapshotUnavailable:
            record_property("observed_outcome", "rejected")
        else:
            try:
                assert list(reader.execute("SELECT value FROM event ORDER BY value")) == [
                    {"value": 1}, {"value": 2}]
                record_property("observed_outcome", "consistent_current_wal")
            finally:
                reader.close()
        assert _digests(source) == before
        record_property("source_hashes_unchanged", "true")
    finally:
        writer.close()


def test_source_identity_replacement_between_stat_and_pin_is_rejected(tmp_path, monkeypatch):
    import readonly_guard

    source = tmp_path / "replacement-race.edb"
    replacement = tmp_path / "replacement.edb"
    source.write_bytes(b"synthetic original")
    replacement.write_bytes(b"synthetic replacement")
    original_stat = readonly_guard.os.stat
    triggered = False

    def replace_after_stat(path, *args, **kwargs):
        nonlocal triggered
        result = original_stat(path, *args, **kwargs)
        if str(path) == str(source) and not triggered:
            triggered = True
            readonly_guard.os.replace(replacement, source)
        return result

    baseline = handles()
    monkeypatch.setattr(readonly_guard.os, "stat", replace_after_stat)
    with pytest.raises(RuntimeError, match="source identity changed"):
        Guard(str(source))
    assert triggered
    assert handles() <= baseline


@pytest.mark.parametrize("cancel", [False, True])
def test_reader_alone_pins_all_source_paths_and_releases_after_exit(tmp_path, cancel):
    live = tmp_path / "writer.edb"
    source = tmp_path / "reader-alone.edb"
    writer = _encrypted_writer(live)
    try:
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(str(live) + suffix), Path(str(source) + suffix))
    finally:
        writer.close()  # No writer holds any handle to the reader's copied paths.
    replacements = []
    for index, suffix in enumerate(("", "-wal", "-shm")):
        incoming = tmp_path / f"incoming-{index}"
        incoming.write_bytes(b"synthetic replacement")
        replacements.append((incoming, Path(str(source) + suffix)))
    reader = _open_encrypted(str(source), SYNTHETIC_KEY)
    try:
        assert list(reader.execute("SELECT value FROM event ORDER BY value")) == [
            {"value": 1}, {"value": 2}]
        for incoming, target in replacements:
            with pytest.raises(PermissionError):
                os.replace(incoming, target)
    finally:
        if cancel:
            reader.cancel()
        else:
            reader.close()
    assert reader._process.poll() is not None
    assert reader._process._handle.closed
    for incoming, target in replacements:
        os.replace(incoming, target)  # Reader pins must now be released by the OS.

