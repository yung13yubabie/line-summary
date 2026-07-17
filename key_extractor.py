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
import os
import re
import subprocess

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
_PAT32 = re.compile(rb'(?<![0-9a-zA-Z])([0-9a-fA-F]{32})(?![0-9a-zA-Z])')
_PAT32_U16 = re.compile(rb'(?:[0-9a-fA-F]\x00){32}')


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


def validate_key_format(candidate: str) -> bool:
    if len(candidate) not in (32, 64):
        return False
    return bool(re.fullmatch(r'[0-9a-f]+', candidate))


def find_line_pid() -> int | None:
    # Absolute path avoids PATH/search-order hijack of a spoofed tasklist.exe.
    tasklist = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tasklist.exe"
    )
    result = subprocess.run(
        [tasklist, '/FI', 'IMAGENAME eq LINE.exe', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True, encoding='utf-8', errors='ignore'
    )
    for line in result.stdout.splitlines():
        if 'LINE.exe' in line:
            parts = line.split(',')
            try:
                return int(parts[1].strip('"'))
            except (IndexError, ValueError):
                continue
    return None


def _scan_memory_regions(pid: int) -> list[str]:
    """Scan LINE private memory; return 32-char hex candidates ordered by
    frequency (most-repeated first). The DB passphrase is referenced in several
    places, so a frequency-first serial probe usually hits within a few tries."""
    from collections import Counter

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
    )
    if not handle:
        return []

    counts: "Counter[str]" = Counter()
    address = 0
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
            if readable:
                buf = ctypes.create_string_buffer(mbi.RegionSize)
                n = ctypes.c_size_t(0)
                if kernel32.ReadProcessMemory(
                    handle, ctypes.c_void_p(address),
                    buf, mbi.RegionSize, ctypes.byref(n)
                ) and n.value > 0:
                    chunk = buf.raw[:n.value]
                    # LINE's key is a 32-char hex passphrase (confirmed aes128cbc
                    # + PRAGMA key), held as ASCII or UTF-16LE (Qt QString).
                    for m in _PAT32.findall(chunk):
                        counts[m.decode('ascii').lower()] += 1
                    for m in _PAT32_U16.findall(chunk):
                        try:
                            counts[m.decode('utf-16le').lower()] += 1
                        except Exception:
                            pass

            next_addr = address + mbi.RegionSize
            if next_addr <= address or next_addr >= 0x7FFFFFFF0000:
                break
            address = next_addr
    finally:
        kernel32.CloseHandle(handle)

    return [c for c, _ in counts.most_common() if validate_key_format(c)]


def confirm_user_consent() -> bool:
    print("\n" + "=" * 58)
    print("LINE Summary -- Memory Access Required")
    print("=" * 58)
    print("This tool reads LINE's process memory to extract the")
    print("database encryption key. The key is used locally only")
    print("and is NEVER written to disk or sent over any network.")
    print("=" * 58)
    return input("Proceed? Type 'yes' to continue: ").strip().lower() == 'yes'


def extract_key(edb_path: str, require_consent: bool = True) -> str | None:
    """
    Extract LINE wxSQLite3 key from process memory.
    Returns key or None. Key never logged or written to disk.

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
    except DbAccessError as e:
        raise RuntimeError(f"Cannot access the LINE database: {e}") from e

    candidates = _scan_memory_regions(pid)
    if not candidates:
        raise RuntimeError(
            "Could not read LINE process memory. "
            "Try running as Administrator."
        )

    for candidate in candidates:
        if probe_key(edb_path, candidate):
            return candidate

    raise RuntimeError(
        "Found key candidates but none decrypted the database. The DB is readable "
        "(preflight passed), so LINE likely rotated its key or changed its cipher."
    )
