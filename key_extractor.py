"""
Extracts LINE PC wxSQLite3 encryption key from process memory.

SECURITY (enforced):
- Local only -- never expose key over network
- Interactive user consent required on first call
- Key never written to disk, log, env var, or stdout
- Error messages never contain key candidates or chat content
"""
import ctypes
import ctypes.wintypes
import csv
import os
import re
import subprocess
from line_process_identity import configure_kernel, open_verified_line_process

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

# The key is a small server-provided string on a normal heap allocation, so cap
# region size aggressively — scanning multi-GB regions dominated startup (~146s).
# 128MB keeps coverage while cutting the scan to well under a minute.
_MAX_REGION_BYTES = 128 * 1024 * 1024

# LINE 26.3 (Qt6) holds strings as UTF-16LE, so scan BOTH ASCII and UTF-16LE and
# accept either case — an ASCII-only scan misses ~1000 real candidates (see FINDINGS.md).
# Keep legacy private names for local diagnostics and enumerate format-matching
# 32/64-character candidates in both encodings without changing their case.
# Observed alphanumeric neighbors exclude embedded/overlong matches. Unknown
# left boundaries can still yield false positives; only a DB probe verifies a key.
_PAT32 = re.compile(rb'(?<![0-9a-zA-Z])([0-9a-fA-F]{64}|[0-9a-fA-F]{32})(?![0-9a-zA-Z])')
_PAT32_U16 = re.compile(rb'(?<![0-9a-zA-Z]\x00)((?:[0-9a-fA-F]\x00){64}|(?:[0-9a-fA-F]\x00){32})(?![0-9a-zA-Z]\x00)')

# Longest UTF-16 token plus one code unit of context on either side. Only this
# bounded tail survives a region; a region ending is not a token terminator.
_CANDIDATE_CARRY_BYTES = 2 * (64 + 2)


def _count_candidate_chunk(chunk, carry, truncated, counts):
    data = carry + chunk
    for pattern, width, encoding in ((_PAT32, 1, 'ascii'), (_PAT32_U16, 2, 'utf-16le')):
        for match in pattern.finditer(data):
            # Matches wholly inside the stable old tail were counted already.
            # An incomplete right code unit must wait for the next region.
            if match.end() <= len(carry) - width or match.end() > len(data) - width:
                continue
            # A truncated carry prefix is not a newly established left boundary.
            if truncated and match.start() < width:
                continue
            counts[match.group(1).decode(encoding)] += 1
    return data[-_CANDIDATE_CARRY_BYTES:], truncated or len(data) > _CANDIDATE_CARRY_BYTES


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("__align1", ctypes.wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", ctypes.wintypes.DWORD),
        ("Protect", ctypes.wintypes.DWORD),
        ("Type", ctypes.wintypes.DWORD),
        ("__align2", ctypes.wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.WCHAR * 260),
    ]


class ProcessDiscoveryError(RuntimeError):
    """Fixed diagnostics; external stdout/stderr/exception text never enters."""
    def __init__(self, code):
        messages = {
            "unavailable": "LINE process enumeration unavailable; process state is unknown",
            "ambiguous": "Found multiple LINE processes; select one locally before reading memory",
        }
        self.code = code if code in messages else "unavailable"
        super().__init__(messages[self.code])


def _load_kernel32():
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        signatures = {
            "OpenProcess": ([ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD], ctypes.wintypes.HANDLE),
            "CloseHandle": ([ctypes.wintypes.HANDLE], ctypes.wintypes.BOOL),
            "VirtualQueryEx": ([ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t], ctypes.c_size_t),
            "ReadProcessMemory": ([ctypes.wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], ctypes.wintypes.BOOL),
            "QueryFullProcessImageNameW": ([ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.LPWSTR, ctypes.POINTER(ctypes.wintypes.DWORD)], ctypes.wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([ctypes.wintypes.DWORD, ctypes.wintypes.DWORD], ctypes.wintypes.HANDLE),
            "Process32FirstW": ([ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], ctypes.wintypes.BOOL),
            "Process32NextW": ([ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], ctypes.wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
        configure_kernel(kernel)
        return kernel
    except Exception:
        raise ProcessDiscoveryError("unavailable") from None


def _enumerate_line_pids() -> list[int]:
    """Toolhelp metadata fallback, never another process's memory or UI."""
    kernel = _load_kernel32()
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if handle in (None, 0, -1, ctypes.c_void_p(-1).value):
        raise ProcessDiscoveryError("unavailable") from None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(handle, ctypes.byref(entry))
        pids = set()
        for _ in range(65536):
            if not success:
                if ctypes.get_last_error() == 18:  # ERROR_NO_MORE_FILES
                    return sorted(pids)
                raise ProcessDiscoveryError("unavailable") from None
            if entry.szExeFile.casefold() == "line.exe" and entry.th32ProcessID > 0:
                pids.add(int(entry.th32ProcessID))
                if len(pids) > 1:
                    return sorted(pids)
            success = kernel.Process32NextW(handle, ctypes.byref(entry))
        raise ProcessDiscoveryError("unavailable") from None
    finally:
        kernel.CloseHandle(handle)


def validate_key_format(candidate: str) -> bool:
    if type(candidate) is not str or len(candidate) not in (32, 64):
        return False
    return bool(re.fullmatch(r'[0-9a-fA-F]+', candidate))


def find_line_pid() -> int | None:
    # Absolute path avoids PATH/search-order hijack of a spoofed tasklist.exe.
    tasklist = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe"
    )
    pids = set()
    try:
        result = subprocess.run(
            [tasklist, '/FI', 'IMAGENAME eq LINE.exe', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore',
            timeout=5,
        )
        if result.returncode == 0 and isinstance(result.stdout, str):
            for parts in csv.reader(result.stdout.splitlines()):
                if parts and parts[0].casefold() == 'line.exe':
                    if len(parts) < 2 or not parts[1].isascii() or not parts[1].isdigit() or not 0 < int(parts[1]) <= 0xFFFFFFFF:
                        pids.clear()
                        break
                    pids.add(int(parts[1]))
    except Exception:
        # A failed tool does not establish absence; do not expose its error.
        pids.clear()
    if not pids:
        try:
            pids = set(_enumerate_line_pids())
        except Exception:
            raise ProcessDiscoveryError("unavailable") from None
    if len(pids) > 1:
        raise ProcessDiscoveryError("ambiguous") from None
    return next(iter(pids), None)


def _scan_memory_regions(pid: int) -> list[str]:
    """Scan LINE private memory; return unverified 32/64-char candidates ordered by
    frequency (most-repeated first). The DB passphrase is referenced in several
    places, so a frequency-first serial probe usually hits within a few tries.
    A readable region's unknown left edge may be a string start or a suffix;
    retain the candidate rather than discard a possible key. extract_key probes
    every candidate against the DB and returns only a successful probe result."""
    from collections import Counter

    kernel32 = _load_kernel32()
    verified = open_verified_line_process(kernel32, pid)
    if verified is None:
        return []
    handle = verified.handle

    counts: "Counter[str]" = Counter()
    address = 0
    carry, carry_truncated, previous_read_end = b'', False, None
    try:
        mbi = MEMORY_BASIC_INFORMATION()
        sz = ctypes.sizeof(mbi)
        while kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(address), ctypes.byref(mbi), sz
        ):
            readable = (
                mbi.State == MEM_COMMIT
                and mbi.Protect != PAGE_NOACCESS
                and not (mbi.Protect & PAGE_GUARD)
                and mbi.Type == MEM_PRIVATE
                and 0 < mbi.RegionSize <= _MAX_REGION_BYTES
            )
            full_read = False
            if readable:
                if previous_read_end != address or mbi.BaseAddress != address:
                    carry, carry_truncated = b'', False
                buf = ctypes.create_string_buffer(mbi.RegionSize)
                n = ctypes.c_size_t(0)
                if kernel32.ReadProcessMemory(
                    handle, ctypes.c_void_p(address),
                    buf, mbi.RegionSize, ctypes.byref(n)
                ) and 0 < n.value <= mbi.RegionSize:
                    chunk = buf.raw[:n.value]
                    carry, carry_truncated = _count_candidate_chunk(
                        chunk, carry, carry_truncated, counts
                    )
                    full_read = n.value == mbi.RegionSize
                    previous_read_end = address + n.value

            # Never join across unreadable/skipped regions or a failed/short
            # read. Unknown bytes also cannot finalize a pending 32/64 token.
            if not full_read:
                carry, carry_truncated, previous_read_end = b'', False, None

            next_addr = address + mbi.RegionSize
            if next_addr <= address or next_addr >= 0x7FFFFFFF0000:
                break
            address = next_addr
    finally:
        verified.close()

    return [c for c, _ in counts.most_common() if validate_key_format(c)]


def confirm_user_consent() -> bool:
    print("\n" + "=" * 58)
    print("LINE Summary -- Memory Access Required")
    print("=" * 58)
    print("This tool reads LINE's process memory to extract the")
    print("database encryption key. The key is used locally only")
    print("by this program; OS paging or crash dumps may retain it.")
    print("=" * 58)
    return input("Proceed? Type 'yes' to continue: ").strip().lower() == 'yes'


def extract_key(edb_path: str, require_consent: bool = True) -> str | None:
    """
    Extract LINE wxSQLite3 key from process memory.
    Returns a DB-probe-accepted key, or None when consent is declined. Scanned
    candidates are not verified keys, including matches at unknown left edges.
    Key never logged or written to disk.

    Probing is SERIAL by design. This runs inside an MCP stdio server that is
    itself a child process; spawning a ProcessPool from there fails on Windows
    (multiprocessing DuplicateHandle -> WinError 5, access denied), so parallel
    probing would hang in real use. Candidates are frequency-ordered so the
    serial probe usually hits within the first handful of tries.
    """
    if require_consent and not confirm_user_consent():
        return None

    pid = find_line_pid()
    if pid is None:
        raise RuntimeError("LINE is not running. Start LINE and try again.")

    # Preflight the DB so a broken environment (missing file, permission, lock,
    # cipher) is reported for what it is, instead of being swallowed by the probe
    # loop and misreported as 'none of the keys worked / LINE updated'.
    from db_reader import preflight_db_access, DbAccessError, probe_key
    try:
        preflight_db_access(edb_path)
    except Exception:
        raise RuntimeError("Cannot access the LINE database; check local file access and cipher setup") from None

    try:
        candidates = _scan_memory_regions(pid)
    except Exception:
        raise RuntimeError("LINE memory inspection failed; diagnostic details suppressed") from None
    if not candidates:
        raise RuntimeError(
            "Could not read LINE process memory or find supported key candidates. "
            "Check local access and candidate coverage; do not elevate the MCP host automatically."
        )

    for candidate in candidates:
        if not validate_key_format(candidate):
            continue
        try:
            if probe_key(edb_path, candidate):
                return candidate
        except Exception:
            raise RuntimeError("LINE database probe failed; diagnostic details suppressed") from None

    raise RuntimeError(
        "Found key candidates but none decrypted the database. "
        "The cause is unverified; check cipher parameters, candidate coverage and file state locally."
    )
