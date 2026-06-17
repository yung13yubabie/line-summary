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
import re
import subprocess

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

_PAT32 = re.compile(rb'(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])')
_PAT64 = re.compile(rb'(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])')


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
    result = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq LINE.exe', '/FO', 'CSV', '/NH'],
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
    """Internal: scan LINE process memory, return valid hex candidates."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
    )
    if not handle:
        return []

    candidates: set[str] = set()
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
                and 0 < mbi.RegionSize <= 20 * 1024 * 1024
            )
            if readable:
                buf = ctypes.create_string_buffer(mbi.RegionSize)
                n = ctypes.c_size_t(0)
                if kernel32.ReadProcessMemory(
                    handle, ctypes.c_void_p(address),
                    buf, mbi.RegionSize, ctypes.byref(n)
                ) and n.value > 0:
                    chunk = buf.raw[:n.value]
                    for m in _PAT32.findall(chunk):
                        candidates.add(m.decode('ascii'))
                    for m in _PAT64.findall(chunk):
                        candidates.add(m.decode('ascii'))

            next_addr = address + mbi.RegionSize
            if next_addr <= address or next_addr >= 0x7FFFFFFF0000:
                break
            address = next_addr
    finally:
        kernel32.CloseHandle(handle)

    return [c for c in candidates if validate_key_format(c)]


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
    """
    if require_consent and not confirm_user_consent():
        return None

    pid = find_line_pid()
    if pid is None:
        raise RuntimeError("LINE is not running. Start LINE and try again.")

    candidates = _scan_memory_regions(pid)
    if not candidates:
        raise RuntimeError(
            "Could not read LINE process memory. "
            "Try running as Administrator."
        )

    from db_reader import probe_key
    for candidate in candidates:
        if probe_key(edb_path, candidate):
            return candidate

    raise RuntimeError(
        "Memory candidates found but none decrypted the database. "
        "LINE may have been updated -- check spike/FINDINGS.md."
    )
