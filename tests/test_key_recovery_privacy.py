"""Synthetic process, passphrase and diagnostics adversarial regressions."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import db_reader
import key_extractor as ke

PRIVATE = "SYNTHETIC_PRIVATE_SENTINEL"
MIXED = "A1b2C3d4E5f60718A9b0C1d2E3f40516"
NATIVE_ENUMERATOR = ke._enumerate_line_pids


@pytest.fixture(autouse=True)
def no_native_process_enumeration(monkeypatch):
    monkeypatch.setattr(ke, "_enumerate_line_pids", lambda: [], raising=False)


def task_result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=PRIVATE, returncode=returncode)


def test_tasklist_failure_uses_bounded_native_fallback(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result(returncode=1))
    monkeypatch.setattr(ke, "_enumerate_line_pids", lambda: [87])
    assert ke.find_line_pid() == 87


@pytest.mark.parametrize("failure", [OSError(PRIVATE), TimeoutError(PRIVATE)], ids=["os_error", "timeout"])
def test_tasklist_exception_uses_native_fallback(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(ke.subprocess, "run", fail)
    monkeypatch.setattr(ke, "_enumerate_line_pids", lambda: [87])
    assert ke.find_line_pid() == 87


def test_tasklist_failed_output_is_not_trusted(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result('"LINE.exe","91"', 1))
    monkeypatch.setattr(ke, "_enumerate_line_pids", lambda: [87])
    assert ke.find_line_pid() == 87


def test_process_name_matching_is_exact_and_case_insensitive(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result('"notLINE.exe","9"\n"Line.Exe","87"\n'))
    assert ke.find_line_pid() == 87


def test_multiple_line_processes_are_not_silently_selected(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result('"LINE.exe","87"\n"LINE.exe","88"'))
    with pytest.raises(RuntimeError, match="multiple LINE processes"):
        ke.find_line_pid()


def test_failed_enumeration_is_distinct_from_not_running(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result(returncode=1))
    def fail():
        raise OSError(PRIVATE)
    monkeypatch.setattr(ke, "_enumerate_line_pids", fail)
    with pytest.raises(RuntimeError) as caught:
        ke.find_line_pid()
    assert PRIVATE not in str(caught.value)
    assert "not running" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_tasklist_has_timeout_and_no_shell(monkeypatch):
    received = {}
    def execute(*args, **kwargs):
        received.update(kwargs)
        return task_result('"LINE.exe","87"')
    monkeypatch.setattr(ke.subprocess, "run", execute)
    assert ke.find_line_pid() == 87
    assert 0 < received["timeout"] <= 5
    assert not received.get("shell", False)


class SyntheticMemory:
    def __init__(self, chunk):
        self.chunk = chunk
        self.queried = False
        self.CloseHandle = Mock()
        self.OpenProcess = Mock(return_value=87)
        self.image_name = "LINE.exe"
        self.memory_reads = 0

    def QueryFullProcessImageNameW(self, handle, flags, target, size):
        target.value = self.image_name
        return 1

    def VirtualQueryEx(self, handle, address, target, size):
        if self.queried:
            return 0
        self.queried = True
        region = ctypes.cast(target, ctypes.POINTER(ke.MEMORY_BASIC_INFORMATION)).contents
        region.RegionSize = len(self.chunk)
        region.State, region.Type, region.Protect = ke.MEM_COMMIT, ke.MEM_PRIVATE, 4
        return size

    def ReadProcessMemory(self, handle, address, target, size, read_count):
        self.memory_reads += 1
        ctypes.memmove(target, self.chunk, len(self.chunk))
        ctypes.cast(read_count, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(self.chunk)
        return 1


def scan(monkeypatch, chunk):
    kernel = SyntheticMemory(chunk)
    monkeypatch.setattr(ke.ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel, raising=False)
    # These unit tests cover candidate bytes only. Full handle/authenticity
    # behavior is exercised with independent native fakes in test_process_identity.
    monkeypatch.setattr(ke, "open_verified_line_process", lambda k, p: SimpleNamespace(
        handle=87, close=lambda: kernel.CloseHandle(87)))
    result = ke._scan_memory_regions(87)
    kernel.CloseHandle.assert_called_once_with(87)
    return result


@pytest.mark.parametrize("encoding", ["ascii", "utf-16le"])
def test_scanner_preserves_passphrase_case(monkeypatch, encoding):
    assert scan(monkeypatch, (" " + MIXED + " ").encode(encoding)) == [MIXED]


@pytest.mark.parametrize("encoding", ["ascii", "utf-16le"])
def test_scanner_keeps_full_64_character_candidate(monkeypatch, encoding):
    candidate = MIXED * 2
    assert scan(monkeypatch, (" " + candidate + " ").encode(encoding)) == [candidate]


def test_different_case_candidates_are_not_merged(monkeypatch):
    chunk = (" " + MIXED + " " + MIXED.lower() + " ").encode("ascii")
    assert set(scan(monkeypatch, chunk)) == {MIXED, MIXED.lower()}


@pytest.mark.parametrize("encoding", ["ascii", "utf-16le"])
def test_scanner_rejects_embedded_and_overlong_tokens(monkeypatch, encoding):
    assert scan(monkeypatch, ("Q" + MIXED + "Q " + MIXED * 3).encode(encoding)) == []


@pytest.mark.parametrize("value", [MIXED, MIXED * 2])
def test_validator_accepts_case_preserving_hex(value):
    assert ke.validate_key_format(value)


@pytest.mark.parametrize("value", [None, 32, b"a" * 32])
def test_validator_rejects_non_strings_without_echo(value):
    assert ke.validate_key_format(value) is False


@pytest.mark.parametrize("reason", ["database is locked", "permission denied", "unclassified"], ids=["lock", "permission", "unknown"])
def test_preflight_does_not_echo_backend_paths_or_causes(monkeypatch, reason):
    monkeypatch.setattr(db_reader.os.path, "exists", lambda p: True)
    monkeypatch.setattr(db_reader.os, "access", lambda *a: True)
    def fail(*args, **kwargs):
        raise RuntimeError(reason + " " + PRIVATE)
    monkeypatch.setattr(db_reader, "_open_encrypted", fail)
    with pytest.raises(db_reader.DbAccessError) as caught:
        db_reader.preflight_db_access("synthetic-account.edb")
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__


def test_missing_file_does_not_echo_given_path(monkeypatch):
    monkeypatch.setattr(db_reader.os.path, "exists", lambda p: False)
    with pytest.raises(db_reader.DbAccessError) as caught:
        db_reader.preflight_db_access(PRIVATE)
    assert PRIVATE not in str(caught.value)


def test_failed_key_probe_does_not_claim_rotation(monkeypatch):
    monkeypatch.setattr(ke, "find_line_pid", lambda: 87)
    monkeypatch.setattr(db_reader, "preflight_db_access", lambda p: None)
    monkeypatch.setattr(ke, "_scan_memory_regions", lambda p: [MIXED])
    monkeypatch.setattr(db_reader, "probe_key", lambda *a: False)
    with pytest.raises(RuntimeError) as caught:
        ke.extract_key("synthetic-account.edb", require_consent=False)
    assert "rotated" not in str(caught.value)
    assert "likely" not in str(caught.value)


def test_extract_key_does_not_relay_external_preflight_details(monkeypatch):
    monkeypatch.setattr(ke, "find_line_pid", lambda: 87)
    def fail(path):
        raise db_reader.DbAccessError(PRIVATE)
    monkeypatch.setattr(db_reader, "preflight_db_access", fail)
    with pytest.raises(RuntimeError) as caught:
        ke.extract_key("synthetic-account.edb", require_consent=False)
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("surface", ["preflight", "scan", "probe"])
def test_unexpected_recovery_errors_are_sanitized(monkeypatch, surface):
    monkeypatch.setattr(ke, "find_line_pid", lambda: 87)
    monkeypatch.setattr(db_reader, "preflight_db_access", lambda p: None)
    monkeypatch.setattr(ke, "_scan_memory_regions", lambda p: [MIXED])
    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    if surface == "preflight":
        monkeypatch.setattr(db_reader, "preflight_db_access", fail)
    elif surface == "scan":
        monkeypatch.setattr(ke, "_scan_memory_regions", fail)
    else:
        monkeypatch.setattr(db_reader, "probe_key", fail)
    with pytest.raises(RuntimeError) as caught:
        ke.extract_key("synthetic-account.edb", require_consent=False)
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__


class SyntheticSnapshot:
    def __init__(self, rows, error=18, handle=87):
        self.rows, self.error, self.handle = iter(rows), error, handle
        self.CreateToolhelp32Snapshot = Mock(return_value=handle)
        self.CloseHandle = Mock()

    def Process32FirstW(self, handle, target):
        entry = ctypes.cast(target, ctypes.POINTER(ke.PROCESSENTRY32W)).contents
        assert entry.dwSize == ctypes.sizeof(ke.PROCESSENTRY32W)
        return self.Process32NextW(handle, target)

    def Process32NextW(self, handle, target):
        try:
            name, pid = next(self.rows)
        except StopIteration:
            return 0
        entry = ctypes.cast(target, ctypes.POINTER(ke.PROCESSENTRY32W)).contents
        entry.szExeFile, entry.th32ProcessID = name, pid
        return 1


@pytest.mark.parametrize("rows,expected", [([], []), ([("other.exe", 5), ("LINE.exe", 87)], [87]), ([("Line.exe", 87), ("LINE.exe", 88)], [87, 88])], ids=["none", "one", "ambiguous"])
def test_native_snapshot_matches_only_line_and_closes(monkeypatch, rows, expected):
    kernel = SyntheticSnapshot(rows)
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.error, raising=False)
    assert NATIVE_ENUMERATOR() == expected
    kernel.CreateToolhelp32Snapshot.assert_called_once_with(2, 0)
    kernel.CloseHandle.assert_called_once_with(87)


def test_native_snapshot_failure_is_fixed_and_closes(monkeypatch):
    kernel = SyntheticSnapshot([], error=5)
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    with pytest.raises(ke.ProcessDiscoveryError, match="unknown"):
        NATIVE_ENUMERATOR()
    kernel.CloseHandle.assert_called_once_with(87)


def test_native_invalid_handle_is_never_closed(monkeypatch):
    kernel = SyntheticSnapshot([], handle=ctypes.c_void_p(-1).value)
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel)
    with pytest.raises(ke.ProcessDiscoveryError):
        NATIVE_ENUMERATOR()
    kernel.CloseHandle.assert_not_called()


def test_tasklist_oversized_pid_is_not_truncated(monkeypatch):
    monkeypatch.setattr(ke.subprocess, "run", lambda *a, **k: task_result('"LINE.exe","4294967297"'))
    monkeypatch.setattr(ke, "_enumerate_line_pids", lambda: [87])
    assert ke.find_line_pid() == 87


def test_extract_filters_invalid_candidate_without_probing(monkeypatch):
    monkeypatch.setattr(ke, "find_line_pid", lambda: 87)
    monkeypatch.setattr(db_reader, "preflight_db_access", lambda p: None)
    monkeypatch.setattr(ke, "_scan_memory_regions", lambda p: [PRIVATE])
    probe = Mock()
    monkeypatch.setattr(db_reader, "probe_key", probe)
    with pytest.raises(RuntimeError):
        ke.extract_key("synthetic-account.edb", require_consent=False)
    probe.assert_not_called()


def test_scanned_passphrase_opens_only_synthetic_codec_database(monkeypatch, tmp_path, request):
    apsw = pytest.importorskip("apsw")
    memory = apsw.Connection(":memory:")
    try:
        assert list(memory.execute("SELECT sqlite3mc_config('cipher')"))
    finally:
        memory.close()
    candidate = scan(monkeypatch, (" " + MIXED + " ").encode("utf-16le"))[0]
    path = tmp_path / "synthetic.edb"
    writer = apsw.Connection(str(path))
    writer.execute("PRAGMA cipher='aes128cbc'")
    writer.execute("PRAGMA key='" + MIXED + "'")
    writer.execute("CREATE TABLE synthetic_event(value INTEGER)")
    writer.execute("INSERT INTO synthetic_event VALUES(1)")
    writer.close()
    from tests.hot_wal_fixture import retain_hot_wal
    retain_hot_wal(path, MIXED, request.addfinalizer)
    assert db_reader.probe_key(str(path), candidate)
    assert not db_reader.probe_key(str(path), candidate.lower())


def test_reused_pid_never_reads_another_process_memory(monkeypatch):
    kernel = SyntheticMemory((" " + MIXED + " ").encode("ascii"))
    kernel.image_name = "other.exe"
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel)
    with pytest.raises(RuntimeError, match="identity"):
        ke._scan_memory_regions(87)
    assert kernel.memory_reads == 0
    kernel.CloseHandle.assert_called_once_with(87)
