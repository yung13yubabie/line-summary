# LINE Summary MCP Server — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python MCP Server that reads LINE PC (Windows) encrypted chat history and exposes it to Claude for 3-round structured summarization.

**Architecture:** Phase 0 spike validates key extraction + wxSQLite3 decryption before any production code. Phase 1 builds `key_extractor.py`, `db_reader.py`, `line_mcp_server.py` with TDD. The `line-summary` SKILL.md orchestrates Claude to call MCP tools for summarization.

**Tech Stack:** Python 3.11+, ctypes (Win32 ReadProcessMemory), sqlcipher3-binary, mcp Python SDK (FastMCP), pytest

---

## File Map

| File | Responsibility |
|------|---------------|
| `spike/phase0.py` | Phase 0 spike (deleted after validation) |
| `spike/FINDINGS.md` | Phase 0 results — table names, PRAGMA index (no sensitive data) |
| `key_extractor.py` | Win32 memory scan, extract LINE wxSQLite3 key |
| `db_reader.py` | Decrypt .edb, query messages/chats/contacts |
| `line_mcp_server.py` | FastMCP server, 3 tools |
| `settings.json` | User config (gitignored) |
| `requirements.txt` | Pinned dependencies |
| `.gitignore` | Exclude output/, keys, DB files |
| `skills/line-summary/SKILL.md` | Claude skill — 3-round summarization |
| `tests/test_key_extractor.py` | Unit tests for key logic |
| `tests/test_db_reader.py` | Unit tests for DB queries |
| `tests/test_mcp_tools.py` | MCP tool interface tests |

---

## PHASE 0: Spike — Must Pass Before Phase 1

### Task 1: Phase 0 Setup — PID Detection

**Files:**
- Create: `spike/phase0.py`

- [ ] **Step 1: Create spike script**

```python
# spike/phase0.py
"""
Phase 0 Spike: Validate LINE .edb decryption feasibility.
SECURITY: Do NOT commit output. Delete spike/ after validation.
"""
import subprocess
import sys


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


if __name__ == '__main__':
    print("[Phase0-1] Detecting LINE.exe PID...")
    pid = find_line_pid()
    if pid is None:
        print("FAIL: LINE.exe not running. Start LINE and try again.")
        sys.exit(1)
    print(f"PASS: LINE.exe PID = {pid}")
```

- [ ] **Step 2: Start LINE, then run**

```
python spike/phase0.py
```

Expected:
```
[Phase0-1] Detecting LINE.exe PID...
PASS: LINE.exe PID = 12345
```

- [ ] **Step 3: Commit**

```bash
git add spike/phase0.py
git commit -m "spike: phase0 scaffold — PID detection"
```

---

### Task 2: Phase 0 — Memory Scan for Key Candidates

**Files:**
- Modify: `spike/phase0.py`

- [ ] **Step 1: Replace spike/phase0.py with memory scanner**

```python
# spike/phase0.py
"""
Phase 0 Spike: Validate LINE .edb decryption feasibility.
SECURITY: Do NOT commit output. Delete spike/ after validation.
"""
import ctypes
import ctypes.wintypes
import glob
import os
import re
import subprocess
import sys

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


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


def scan_memory_for_hex_candidates(pid: int) -> list[str]:
    """Scan LINE memory, return 32-char and 64-char hex string candidates."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
    )
    if not handle:
        print("FAIL: Cannot open LINE process. Run terminal as Administrator.")
        return []

    pat32 = re.compile(rb'(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])')
    pat64 = re.compile(rb'(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])')
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
                    for m in pat32.findall(chunk):
                        candidates.add(m.decode('ascii'))
                    for m in pat64.findall(chunk):
                        candidates.add(m.decode('ascii'))

            next_addr = address + mbi.RegionSize
            if next_addr <= address or next_addr >= 0x7FFFFFFF0000:
                break
            address = next_addr
    finally:
        kernel32.CloseHandle(handle)

    return sorted(candidates)


def find_edb_path() -> str | None:
    pattern = os.path.join(
        os.path.expandvars(r'%LOCALAPPDATA%'), 'LINE', 'Data', 'db', '*.edb'
    )
    matches = [
        p for p in glob.glob(pattern)
        if not (p.endswith('-shm') or p.endswith('-wal'))
    ]
    return matches[0] if matches else None


if __name__ == '__main__':
    print("[Phase0-1] Detecting LINE.exe PID...")
    pid = find_line_pid()
    if pid is None:
        print("FAIL: LINE.exe not running.")
        sys.exit(1)
    print(f"PASS: PID = {pid}")

    print("[Phase0-2] Scanning memory for hex candidates...")
    candidates = scan_memory_for_hex_candidates(pid)
    if not candidates:
        print("FAIL: No candidates found. Run as Administrator.")
        sys.exit(1)
    # SECURITY: never print candidate values
    print(f"PASS: Found {len(candidates)} hex candidates")

    print("[Phase0-3] Locating .edb file...")
    edb = find_edb_path()
    if edb is None:
        print("FAIL: No .edb found.")
        sys.exit(1)
    print(f"PASS: {edb}")
```

- [ ] **Step 2: Run**

```
python spike/phase0.py
```

Expected:
```
[Phase0-1] ... PASS: PID = <N>
[Phase0-2] ... PASS: Found <N> hex candidates
[Phase0-3] ... PASS: C:\Users\LIN\AppData\Local\LINE\Data\db\qw3f...edb
```

If Step 2 fails with 0 candidates: open terminal as Administrator and retry.

- [ ] **Step 3: Commit**

```bash
git add spike/phase0.py
git commit -m "spike: add memory scan and edb path detection"
```

---

### Task 3: Phase 0 — Decrypt .edb with Candidate Keys

**Files:**
- Modify: `spike/phase0.py`

- [ ] **Step 1: Install sqlcipher3-binary**

```
pip install sqlcipher3-binary
```

If that fails (build error), try:
```
pip install sqlcipher3
```

(sqlcipher3 requires libsqlcipher — if neither works, document in FINDINGS.md and try the `apsw` approach)

- [ ] **Step 2: Add decryption probe to spike/phase0.py**

Add after `find_edb_path()`, before `if __name__ == '__main__':`:

```python
def try_decrypt_edb(edb_path: str, candidates: list[str]) -> tuple[str, int] | None:
    """Try each candidate against each PRAGMA set. Return (working_key, pragma_idx) or None.
    SECURITY: Never print the working key."""
    try:
        import sqlcipher3
    except ImportError:
        print("FAIL: sqlcipher3 not installed.")
        return None

    pragma_sets = [
        lambda k: [f"PRAGMA key = \"x'{k}'\";"],
        lambda k: [f"PRAGMA key = \"x'{k}'\";", "PRAGMA cipher = 'aes256cbc';"],
        lambda k: [f"PRAGMA key = \"x'{k}'\";", "PRAGMA cipher = 'chacha20';"],
        lambda k: [f"PRAGMA key = '{k}';"],
    ]

    for candidate in candidates:
        for idx, pragma_fn in enumerate(pragma_sets):
            conn = None
            try:
                conn = sqlcipher3.connect(edb_path)
                for p in pragma_fn(candidate):
                    conn.execute(p)
                conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
                conn.close()
                return candidate, idx
            except Exception:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
    return None
```

- [ ] **Step 3: Add Steps 4-5 to the main block**

Replace `if __name__ == '__main__':` in phase0.py:

```python
if __name__ == '__main__':
    print("[Phase0-1] Detecting LINE.exe PID...")
    pid = find_line_pid()
    if pid is None:
        print("FAIL: LINE.exe not running.")
        sys.exit(1)
    print(f"PASS: PID = {pid}")

    print("[Phase0-2] Scanning memory for hex candidates...")
    candidates = scan_memory_for_hex_candidates(pid)
    if not candidates:
        print("FAIL: 0 candidates. Run as Administrator.")
        sys.exit(1)
    print(f"PASS: Found {len(candidates)} candidates")

    print("[Phase0-3] Locating .edb...")
    edb = find_edb_path()
    if edb is None:
        print("FAIL: No .edb found.")
        sys.exit(1)
    print(f"PASS: {edb}")

    print("[Phase0-4] Trying candidate keys...")
    result = try_decrypt_edb(edb, candidates)
    if result is None:
        print("FAIL: No candidate key decrypted .edb.")
        print("  → Try Administrator")
        print("  → Confirm sqlcipher3-binary installed")
        print("  → LINE version may need different PRAGMA — document in FINDINGS.md")
        sys.exit(1)
    _working_key, pragma_idx = result
    # SECURITY: _working_key never printed
    print(f"PASS: .edb decrypted (PRAGMA set index: {pragma_idx})")
```

- [ ] **Step 4: Run**

```
python spike/phase0.py
```

Expected:
```
...
[Phase0-4] Trying candidate keys...
PASS: .edb decrypted (PRAGMA set index: N)
```

Record `N` — you'll need it for `_WORKING_PRAGMA_IDX` in `db_reader.py`.

- [ ] **Step 5: Commit**

```bash
git add spike/phase0.py
git commit -m "spike: add sqlcipher3 decryption probe"
```

---

### Task 4: Phase 0 — Query Schema and Validate Complete

**Files:**
- Modify: `spike/phase0.py`
- Create: `spike/FINDINGS.md`

- [ ] **Step 1: Add schema discovery function**

Add after `try_decrypt_edb`, before `if __name__ == '__main__':`:

```python
def query_schema(edb_path: str, key: str, pragma_idx: int) -> list[str]:
    """Return table names and column previews. No row content returned."""
    import sqlcipher3

    pragma_sets = [
        lambda k: [f"PRAGMA key = \"x'{k}'\";"],
        lambda k: [f"PRAGMA key = \"x'{k}'\";", "PRAGMA cipher = 'aes256cbc';"],
        lambda k: [f"PRAGMA key = \"x'{k}'\";", "PRAGMA cipher = 'chacha20';"],
        lambda k: [f"PRAGMA key = '{k}';"],
    ]
    conn = sqlcipher3.connect(edb_path)
    for p in pragma_sets[pragma_idx](key):
        conn.execute(p)

    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ).fetchall()
    ]
    summary = []
    for table in tables:
        cols = [
            r[1] for r in conn.execute(f"PRAGMA table_info({table});").fetchall()
        ]
        count = conn.execute(f"SELECT count(*) FROM {table};").fetchone()[0]
        summary.append(f"{table}: cols={cols[:6]}, rows={count}")
    conn.close()
    return summary
```

- [ ] **Step 2: Add Step 5 to main block**

Append to the `if __name__ == '__main__':` block (after Step 4):

```python
    print("[Phase0-5] Querying database schema...")
    schema = query_schema(edb, _working_key, pragma_idx)
    if not schema:
        print("FAIL: No tables found.")
        sys.exit(1)
    print(f"PASS: Found {len(schema)} tables")
    # Print table names and column lists (no row content — security safe)
    for line in schema:
        print(f"  {line}")
    print("\n✅ Phase 0 COMPLETE")
    print("   → Record table names in spike/FINDINGS.md")
    print("   → Set _WORKING_PRAGMA_IDX in db_reader.py")
    print("   → Delete spike/ output files before committing")
```

- [ ] **Step 3: Run full Phase 0**

```
python spike/phase0.py
```

Expected final lines:
```
[Phase0-5] Querying database schema...
PASS: Found N tables
  chat: cols=[...], rows=N
  message: cols=[...], rows=N
  contact: cols=[...], rows=N
  ...
✅ Phase 0 COMPLETE
```

- [ ] **Step 4: Create spike/FINDINGS.md (no sensitive data)**

```markdown
# Phase 0 Findings

## Environment
- LINE version: [Help > About]
- .edb size: ~1.2GB
- Python: 3.11.x
- sqlcipher3-binary: x.x.x

## Key Extraction
- Key length: [32 or 64 chars]
- PRAGMA set index that worked: [0 / 1 / 2 / 3]

## Database Table Names (no content)
- Chat list table: [name from Phase0-5 output]
- Message table: [name from Phase0-5 output]
- Contact table: [name from Phase0-5 output]

## Column Names per Table
- [table name]: [col1, col2, col3, ...]
- [table name]: [col1, col2, col3, ...]

## Message Type Codes (from sample inspection)
- 1 = text
- 2 = image (guess — confirm from data)
- 3 = sticker (guess — confirm from data)

## Notes
[Any unexpected behaviour, errors, or retries needed]
```

- [ ] **Step 5: Commit**

```bash
git add spike/FINDINGS.md
git commit -m "spike: phase0 complete — schema findings documented"
```

---

## PHASE 1: Production Code

> **Before starting:** Read `spike/FINDINGS.md`. Replace table name constants and `_WORKING_PRAGMA_IDX` as noted below.

---

### Task 5: Project Scaffold

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `settings.json`
- Create: `tests/__init__.py`

- [ ] **Step 1: Write requirements.txt**

```
# requirements.txt — pin versions after Phase 0 confirms working setup
mcp>=1.0.0
sqlcipher3-binary
pywin32
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 2: Write .gitignore**

```
output/
*.edb
*.db
profiles/
history.json
metadata.json
settings.json
__pycache__/
*.pyc
.pytest_cache/
spike/phase0_output*
```

- [ ] **Step 3: Write settings.json**

```json
{
  "db_path": "",
  "output_dir": "~/line-summary/output",
  "media_mode": "placeholder",
  "url_extraction": true,
  "timezone": "Asia/Taipei"
}
```

- [ ] **Step 4: Create tests package**

```bash
mkdir -p tests
touch tests/__init__.py
```

- [ ] **Step 5: Install and verify**

```
pip install -r requirements.txt
python -c "import sqlcipher3; print('sqlcipher3 OK')"
python -c "import mcp; print('mcp OK')"
```

Expected: Both print OK.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .gitignore tests/__init__.py
git commit -m "chore: project scaffold — deps, gitignore, test package"
```

---

### Task 6: key_extractor.py

**Files:**
- Create: `key_extractor.py`
- Create: `tests/test_key_extractor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_key_extractor.py
from unittest.mock import MagicMock, patch
import pytest
from key_extractor import find_line_pid, validate_key_format, _scan_memory_regions


def test_find_line_pid_returns_none_when_not_running():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(stdout="INFO: No tasks running\n")
        assert find_line_pid() is None


def test_find_line_pid_parses_pid_from_csv():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            stdout='"LINE.exe","9999","Console","1","150 K"\n'
        )
        assert find_line_pid() == 9999


def test_validate_key_format_accepts_32_char_hex():
    assert validate_key_format("a" * 32) is True


def test_validate_key_format_accepts_64_char_hex():
    assert validate_key_format("b" * 64) is True


def test_validate_key_format_rejects_wrong_length():
    assert validate_key_format("a" * 10) is False


def test_validate_key_format_rejects_non_hex():
    assert validate_key_format("z" * 32) is False


def test_scan_memory_regions_returns_empty_when_open_fails():
    with patch('ctypes.windll') as mock_windll:
        mock_windll.kernel32.OpenProcess.return_value = None
        result = _scan_memory_regions(pid=9999)
        assert result == []
```

- [ ] **Step 2: Run — confirm fail**

```
pytest tests/test_key_extractor.py -v
```

Expected: `ImportError: No module named 'key_extractor'`

- [ ] **Step 3: Write key_extractor.py**

```python
# key_extractor.py
"""
Extracts LINE PC wxSQLite3 encryption key from process memory.

SECURITY (enforced):
- Local only — never expose key over network
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
    print("LINE Summary — Memory Access Required")
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
        "LINE may have been updated — check spike/FINDINGS.md."
    )
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_key_extractor.py -v
```

Expected: 7/7 PASS.

- [ ] **Step 5: Commit**

```bash
git add key_extractor.py tests/test_key_extractor.py
git commit -m "feat: key_extractor — Win32 memory scan with security constraints"
```

---

### Task 7: db_reader.py

**Files:**
- Create: `db_reader.py`
- Create: `tests/test_db_reader.py`

> Before writing: check `spike/FINDINGS.md` and set `_WORKING_PRAGMA_IDX`, `_TABLE_CHAT`, `_TABLE_MESSAGE`, `_TABLE_CONTACT` to match actual LINE DB.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_db_reader.py
import sqlite3
import pytest
from db_reader import (
    extract_urls_from_text,
    parse_message_row,
    DbReader,
)


def _make_test_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE chat (
        chat_id TEXT PRIMARY KEY, name TEXT, type TEXT,
        member_count INTEGER, last_message_at INTEGER)""")
    conn.execute("""CREATE TABLE message (
        msg_id TEXT PRIMARY KEY, chat_id TEXT, sender_id TEXT,
        content TEXT, type INTEGER, sent_at INTEGER,
        local_path TEXT, filename TEXT, url TEXT,
        title TEXT, description TEXT)""")
    conn.execute("""CREATE TABLE contact (
        contact_id TEXT PRIMARY KEY, display_name TEXT)""")
    conn.execute("INSERT INTO contact VALUES ('u1', '王小明')")
    conn.execute("INSERT INTO contact VALUES ('u2', '李小美')")
    conn.execute("INSERT INTO chat VALUES ('c1','家族群','group',5,1718600000)")
    conn.execute("""INSERT INTO message VALUES
        ('m1','c1','u1','大家好 https://youtu.be/xxx',1,1718600000,
         NULL,NULL,NULL,NULL,NULL)""")
    conn.execute("""INSERT INTO message VALUES
        ('m2','c1','u2',NULL,2,1718600010,
         'C:/path/img.jpg',NULL,NULL,NULL,NULL)""")
    conn.commit()
    conn.close()


def test_extract_urls_finds_https():
    assert extract_urls_from_text("看 https://youtu.be/xxx 有趣") == ["https://youtu.be/xxx"]


def test_extract_urls_empty_on_no_url():
    assert extract_urls_from_text("純文字") == []


def test_extract_urls_none_input():
    assert extract_urls_from_text(None) == []


def test_parse_message_row_text():
    row = {"msg_id": "m1", "chat_id": "c1", "sender_id": "u1",
           "content": "你好", "type": 1, "sent_at": 1718600000,
           "local_path": None, "filename": None,
           "url": None, "title": None, "description": None}
    result = parse_message_row(row, {"u1": "王小明"})
    assert result["type"] == "text"
    assert result["sender"] == "王小明"
    assert result["content"] == "你好"
    assert "sent_at" in result


def test_parse_message_row_image():
    row = {"msg_id": "m2", "chat_id": "c1", "sender_id": "u2",
           "content": None, "type": 2, "sent_at": 1718600010,
           "local_path": "C:/path/img.jpg", "filename": None,
           "url": None, "title": None, "description": None}
    result = parse_message_row(row, {"u2": "李小美"})
    assert result["type"] == "image"
    assert result["local_path"] == "C:/path/img.jpg"


def test_dbreader_list_chats(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    chats = reader.list_chats()
    assert len(chats) == 1
    assert chats[0]["name"] == "家族群"


def test_dbreader_get_history(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    msgs = reader.get_history("c1", since_ts=0, until_ts=9999999999, limit=10)
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "王小明"
    assert "https://youtu.be/xxx" in msgs[0]["urls"]
    assert msgs[1]["type"] == "image"


def test_dbreader_get_contacts(tmp_path):
    db = str(tmp_path / "test.db")
    _make_test_db(db)
    reader = DbReader(db, key=None, _test_mode=True)
    contacts = reader.get_contacts()
    names = [c["display_name"] for c in contacts]
    assert "王小明" in names
```

- [ ] **Step 2: Run — confirm fail**

```
pytest tests/test_db_reader.py -v
```

Expected: `ImportError: No module named 'db_reader'`

- [ ] **Step 3: Write db_reader.py**

> Replace `_TABLE_CHAT`, `_TABLE_MESSAGE`, `_TABLE_CONTACT`, `_WORKING_PRAGMA_IDX` with values from `spike/FINDINGS.md`.

```python
# db_reader.py
"""
Reads LINE PC wxSQLite3-encrypted .edb.
Update constants below after spike/FINDINGS.md is recorded.
"""
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

# ── Update from spike/FINDINGS.md ──────────────────────────────
_WORKING_PRAGMA_IDX = 0   # 0=raw hex, 1=aes256cbc, 2=chacha20, 3=text
_TABLE_CHAT    = "chat"
_TABLE_MESSAGE = "message"
_TABLE_CONTACT = "contact"
_MSG_TEXT    = 1
_MSG_IMAGE   = 2
_MSG_STICKER = 3
_MSG_FILE    = 4
_MSG_VIDEO   = 5
_MSG_LINK    = 6
# ───────────────────────────────────────────────────────────────

_URL_RE = re.compile(r'https?://[^\s　！-～]+')
_TZ_TAIPEI = timezone(timedelta(hours=8))


def _pragma_statements(key: str, idx: int) -> list[str]:
    sets = [
        [f"PRAGMA key = \"x'{key}'\";"],
        [f"PRAGMA key = \"x'{key}'\";", "PRAGMA cipher = 'aes256cbc';"],
        [f"PRAGMA key = \"x'{key}'\";", "PRAGMA cipher = 'chacha20';"],
        [f"PRAGMA key = '{key}';"],
    ]
    return sets[idx]


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text)


def _ts_to_iso(ts: int) -> str:
    dt = datetime.fromtimestamp(
        ts / 1000 if ts > 1_000_000_000_000 else ts, tz=_TZ_TAIPEI
    )
    return dt.isoformat()


def parse_message_row(row: dict[str, Any], contact_map: dict[str, str]) -> dict:
    sender = contact_map.get(row["sender_id"], row["sender_id"])
    t = row.get("type", _MSG_TEXT)
    sent = _ts_to_iso(row["sent_at"])

    if t == _MSG_TEXT:
        content = row.get("content") or ""
        return {"type": "text", "sender": sender, "content": content,
                "urls": extract_urls_from_text(content), "sent_at": sent}
    if t == _MSG_IMAGE:
        return {"type": "image", "sender": sender, "content": None,
                "local_path": row.get("local_path"), "sent_at": sent}
    if t == _MSG_STICKER:
        return {"type": "sticker", "sender": sender, "content": "[貼圖]", "sent_at": sent}
    if t == _MSG_FILE:
        return {"type": "file", "sender": sender, "content": None,
                "filename": row.get("filename"), "sent_at": sent}
    if t == _MSG_LINK:
        return {"type": "link", "sender": sender,
                "url": row.get("url"), "title": row.get("title"),
                "description": row.get("description"), "sent_at": sent}
    return {"type": "unknown", "sender": sender,
            "content": row.get("content"), "sent_at": sent}


def probe_key(db_path: str, key: str) -> bool:
    """Return True if key successfully decrypts the .edb. Called by key_extractor."""
    try:
        import sqlcipher3
        conn = sqlcipher3.connect(db_path)
        for p in _pragma_statements(key, _WORKING_PRAGMA_IDX):
            conn.execute(p)
        conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        conn.close()
        return True
    except Exception:
        return False


class DbReader:
    def __init__(self, db_path: str, key: str | None, _test_mode: bool = False):
        self._db_path = db_path
        self._key = key
        self._test_mode = _test_mode

    def _open(self) -> sqlite3.Connection:
        if self._test_mode or self._key is None:
            conn = sqlite3.connect(self._db_path)
        else:
            import sqlcipher3
            conn = sqlcipher3.connect(self._db_path)
            for p in _pragma_statements(self._key, _WORKING_PRAGMA_IDX):
                conn.execute(p)
        conn.row_factory = sqlite3.Row
        return conn

    def _contacts(self, conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute(
            f"SELECT contact_id, display_name FROM {_TABLE_CONTACT};"
        ).fetchall()
        return {r["contact_id"]: r["display_name"] for r in rows}

    def list_chats(self, query: str = "", limit: int = 50) -> list[dict]:
        conn = self._open()
        try:
            if query:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CHAT} WHERE name LIKE ? "
                    f"ORDER BY last_message_at DESC LIMIT ?;",
                    (f"%{query}%", limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CHAT} "
                    f"ORDER BY last_message_at DESC LIMIT ?;", (limit,)
                ).fetchall()
            return [{"chat_id": r["chat_id"], "name": r["name"],
                     "type": r["type"], "member_count": r["member_count"],
                     "last_message_at": _ts_to_iso(r["last_message_at"])}
                    for r in rows]
        finally:
            conn.close()

    def get_history(
        self, chat_id: str, since_ts: int, until_ts: int, limit: int = 500
    ) -> list[dict]:
        conn = self._open()
        try:
            contact_map = self._contacts(conn)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_MESSAGE} "
                f"WHERE chat_id=? AND sent_at>=? AND sent_at<=? "
                f"ORDER BY sent_at ASC LIMIT ?;",
                (chat_id, since_ts, until_ts, limit)
            ).fetchall()
            return [parse_message_row(dict(r), contact_map) for r in rows]
        finally:
            conn.close()

    def get_contacts(self, query: str = "") -> list[dict]:
        conn = self._open()
        try:
            if query:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CONTACT} WHERE display_name LIKE ?;",
                    (f"%{query}%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {_TABLE_CONTACT};"
                ).fetchall()
            return [{"contact_id": r["contact_id"],
                     "display_name": r["display_name"]} for r in rows]
        finally:
            conn.close()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_db_reader.py -v
```

Expected: 9/9 PASS.

- [ ] **Step 5: Commit**

```bash
git add db_reader.py tests/test_db_reader.py
git commit -m "feat: db_reader — decrypt .edb, query messages/chats/contacts"
```

---

### Task 8: line_mcp_server.py

**Files:**
- Create: `line_mcp_server.py`
- Create: `tests/test_mcp_tools.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mcp_tools.py
import pytest
from unittest.mock import patch
from line_mcp_server import _parse_iso8601, _load_settings, _find_edb_path


def test_parse_iso8601_valid():
    ts = _parse_iso8601("2026-06-15T00:00:00+08:00")
    assert isinstance(ts, int) and ts > 0


def test_parse_iso8601_rejects_natural_language():
    with pytest.raises(ValueError, match="ISO 8601"):
        _parse_iso8601("2天前")


def test_parse_iso8601_rejects_missing_timezone():
    with pytest.raises(ValueError, match="timezone"):
        _parse_iso8601("2026-06-15T00:00:00")


def test_load_settings_returns_defaults_when_missing():
    with patch('builtins.open', side_effect=FileNotFoundError):
        s = _load_settings()
    assert s["media_mode"] == "placeholder"
    assert s["url_extraction"] is True
    assert s["timezone"] == "Asia/Taipei"


def test_find_edb_path_returns_none_when_dir_empty(tmp_path):
    assert _find_edb_path(str(tmp_path)) is None
```

- [ ] **Step 2: Run — confirm fail**

```
pytest tests/test_mcp_tools.py -v
```

Expected: `ImportError: No module named 'line_mcp_server'`

- [ ] **Step 3: Write line_mcp_server.py**

```python
# line_mcp_server.py
"""
LINE Summary MCP Server.
3 tools: line_list_chats, line_get_history, line_get_contacts.

SECURITY: Key cached in process memory only — never returned by any tool.
"""
import glob
import json
import os
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from db_reader import DbReader
from key_extractor import extract_key

mcp = FastMCP("line-summary")

_DEFAULTS = {
    "db_path": "",
    "output_dir": os.path.expanduser("~/line-summary/output"),
    "media_mode": "placeholder",
    "url_extraction": True,
    "timezone": "Asia/Taipei",
}

_reader: DbReader | None = None


def _load_settings() -> dict:
    try:
        with open("settings.json", encoding="utf-8") as f:
            return {**_DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        return dict(_DEFAULTS)


def _find_edb_path(data_dir: str | None = None) -> str | None:
    if data_dir is None:
        data_dir = os.path.join(
            os.path.expandvars("%LOCALAPPDATA%"), "LINE", "Data", "db"
        )
    matches = [
        p for p in glob.glob(os.path.join(data_dir, "*.edb"))
        if not (p.endswith("-shm") or p.endswith("-wal"))
    ]
    return matches[0] if matches else None


def _parse_iso8601(value: str) -> int:
    """Parse ISO 8601 with explicit timezone to Unix seconds."""
    if not isinstance(value, str) or not any(c in value for c in ('+', 'Z')):
        raise ValueError(
            f"Invalid ISO 8601 (must include timezone offset): '{value}'"
        )
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f"Invalid ISO 8601 format: '{value}'")
    if dt.tzinfo is None:
        raise ValueError(f"Missing timezone in: '{value}'")
    return int(dt.timestamp())


def _get_reader() -> DbReader:
    global _reader
    if _reader is not None:
        return _reader
    settings = _load_settings()
    db_path = settings["db_path"] or _find_edb_path()
    if not db_path:
        raise RuntimeError("LINE .edb not found. Set db_path in settings.json.")
    key = extract_key(db_path, require_consent=True)
    if not key:
        raise RuntimeError("User declined or key extraction failed.")
    _reader = DbReader(db_path, key)
    return _reader


@mcp.tool()
def line_list_chats(query: str = "", limit: int = 50) -> list[dict]:
    """List LINE chats (groups and personal). Supports fuzzy name search.
    Returns: [{chat_id, name, type, member_count, last_message_at}]
    """
    return _get_reader().list_chats(query=query, limit=limit)


@mcp.tool()
def line_get_history(
    chat_id: str,
    since: str,
    until: str,
    limit: int = 500,
) -> list[dict]:
    """Get LINE chat message history.

    Args:
        chat_id: From line_list_chats
        since: ISO 8601 with timezone, e.g. 2026-06-15T00:00:00+08:00
        until: ISO 8601 with timezone
        limit: Max messages (default 500)

    Returns: [{type, sender, content, urls, sent_at, ...}]
    """
    return _get_reader().get_history(
        chat_id=chat_id,
        since_ts=_parse_iso8601(since),
        until_ts=_parse_iso8601(until),
        limit=limit,
    )


@mcp.tool()
def line_get_contacts(query: str = "") -> list[dict]:
    """Get LINE contacts for name resolution.
    Returns: [{contact_id, display_name}]
    """
    return _get_reader().get_contacts(query=query)


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_mcp_tools.py -v
```

Expected: 5/5 PASS.

- [ ] **Step 5: Smoke test — start server (LINE must be running)**

```
python line_mcp_server.py
```

Expected: consent prompt appears, after 'yes' server waits for MCP connections.

- [ ] **Step 6: Commit**

```bash
git add line_mcp_server.py tests/test_mcp_tools.py
git commit -m "feat: MCP server — 3 tools with ISO 8601 enforcement and key caching"
```

---

### Task 9: line-summary Skill + MCP Registration

**Files:**
- Create: `skills/line-summary/SKILL.md`
- Create: `output/.gitkeep`
- Modify: `C:\Users\LIN\.claude\settings.json`

- [ ] **Step 1: Create directories**

```bash
mkdir -p skills/line-summary output
touch output/.gitkeep
```

- [ ] **Step 2: Write skills/line-summary/SKILL.md**

```markdown
# line-summary

Summarizes LINE PC chat history using the `line` MCP server tools.

## Prerequisites
- LINE PC is running
- MCP server `line` registered in `.claude/settings.json`

## Time Conversion (Skill layer — NEVER pass natural language to MCP tools)

Convert all time references to ISO 8601 with `+08:00` before calling tools:

| User says | ISO 8601 |
|-----------|----------|
| 今天 | {today}T00:00:00+08:00 → {today}T23:59:59+08:00 |
| 昨天 | {yesterday}T00:00:00+08:00 → {yesterday}T23:59:59+08:00 |
| 最近 N 天 | {today-N}T00:00:00+08:00 → now |
| 上週 | last Monday T00:00:00+08:00 → last Sunday T23:59:59+08:00 |

## Round 1 — Find Chat and Fetch Messages

1. Call `line_list_chats(query="<chat name from user>")`.
   If multiple results, ask user to confirm which one.

2. Call `line_get_history(chat_id=<id>, since=<ISO>, until=<ISO>)`.
   If result > 1000 messages, split into daily calls.

## Round 2 — Build Skeleton (internal, not shown to user)

```
話題清單:
1. [題目] — 主要發言人 — HH:MM–HH:MM
2. ...

發言統計: 王小明 N則, 李小美 N則 ...

連結: [title 或 URL] — 分享人
媒體事件: HH:MM [發言人] 傳了 [圖片/貼圖/檔名]
```

## Round 3 — Full Summary

For each topic expand with quotes and context:

```markdown
## {date} LINE 摘要 — {chat name}

### 話題一：{topic}
**時間：** HH:MM – HH:MM　**參與：** 王小明、李小美

{2-3 句摘要}

> 「{直接引用}」— 王小明 14:30

{結論或決定}

---

### 📎 分享連結
- {title 或 URL} — {發言人} HH:MM

### 📊 發言統計
| 姓名 | 訊息數 |
|------|--------|
| 王小明 | 23 |
```

## Round 4 — Audit Before Output

- [ ] 每個話題骨架都有對應段落
- [ ] 引用名稱與原始資料一致
- [ ] 有連結訊息則有「分享連結」段
- [ ] 媒體事件有出現在上下文中（非靜默忽略）

## Save Output

Path: `~/line-summary/output/<chat_id>/<YYYY-MM-DD>.md`
Range: `~/line-summary/output/<chat_id>/<YYYY-MM-DD>_<YYYY-MM-DD>.md`

Update `~/line-summary/output/metadata.json`:
```json
{ "<chat_id>": "<display name>" }
```
```

- [ ] **Step 3: Register MCP server in Claude Code settings**

Open `C:\Users\LIN\.claude\settings.json` and add under `mcpServers`:

```json
{
  "mcpServers": {
    "line": {
      "command": "python",
      "args": ["C:\\Users\\LIN\\line-summary\\line_mcp_server.py"]
    }
  }
}
```

- [ ] **Step 4: Full integration test**

With LINE open, in Claude Code chat:

```
幫我摘要一下 [你有的一個群組名] 今天的對話
```

Verify sequence:
1. Claude calls `line_list_chats(query="群組名")`
2. Consent prompt appears → type 'yes'
3. Claude calls `line_get_history` with ISO 8601 times
4. Three-round summary produced in Chinese
5. Output saved to `~/line-summary/output/`

- [ ] **Step 5: Commit**

```bash
git add skills/ output/.gitkeep
git commit -m "feat: line-summary skill and MCP registration"
```

---

## Self-Review

**Spec coverage:**
- ✅ Phase 0 five-step gate (Tasks 1-4)
- ✅ Security constraints — consent, no-log, no network (Task 6 `confirm_user_consent`)
- ✅ wxSQLite3 PRAGMA single point of change (Task 7 `_WORKING_PRAGMA_IDX`)
- ✅ `from mcp.server.fastmcp import FastMCP` (Task 8)
- ✅ ISO 8601 at MCP layer, natural language at Skill layer (Task 8 `_parse_iso8601`, Task 9 time table)
- ✅ `chat_id` as output directory name (Task 9 SKILL.md)
- ✅ media_mode placeholder/vision/skip (Task 7 `parse_message_row` type branches)
- ✅ URL extraction always-on (Task 7 `extract_urls_from_text`)
- ✅ .gitignore covers output/, settings.json, *.edb (Task 5)
- ✅ Phase 0 must complete before Phase 1 (plan order enforced)

**Type consistency:**
- `DbReader.list_chats()` → `_get_reader().list_chats()` ✅
- `DbReader.get_history(since_ts, until_ts)` → `_parse_iso8601(since)` ✅
- `probe_key(edb_path, candidate)` → called inside `extract_key()` ✅
- `_scan_memory_regions(pid)` → called inside `extract_key()` ✅
