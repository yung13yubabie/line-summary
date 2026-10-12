"""Windows native SQLite VFS source guard; install only in a short-lived child."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import stat
import struct
import apsw

INVALID = C.c_void_p(-1).value
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3

class SourceShmUnavailable(RuntimeError):
    """The existing source SHM cannot be safely opened for reading."""

class FileInfo(C.Structure):
    _fields_ = [("attributes", W.DWORD), ("creation", W.FILETIME),
                ("access", W.FILETIME), ("write", W.FILETIME),
                ("volume", W.DWORD), ("size_high", W.DWORD),
                ("size_low", W.DWORD), ("links", W.DWORD),
                ("index_high", W.DWORD), ("index_low", W.DWORD)]

CREATE = C.WINFUNCTYPE(W.HANDLE, W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                      W.DWORD, W.DWORD, W.HANDLE, use_last_error=True)
CREATE_A = C.WINFUNCTYPE(W.HANDLE, C.c_char_p, W.DWORD, W.DWORD, C.c_void_p,
                        W.DWORD, W.DWORD, W.HANDLE, use_last_error=True)
MAPPING = C.WINFUNCTYPE(W.HANDLE, W.HANDLE, C.c_void_p, W.DWORD,
                       W.DWORD, W.DWORD, W.LPCWSTR, use_last_error=True)
MAPVIEW = C.WINFUNCTYPE(C.c_void_p, W.HANDLE, W.DWORD, W.DWORD,
                       W.DWORD, C.c_size_t, use_last_error=True)
WRITE = C.WINFUNCTYPE(W.BOOL, W.HANDLE, C.c_void_p, W.DWORD,
                     C.c_void_p, C.c_void_p, use_last_error=True)
TRUNCATE = C.WINFUNCTYPE(W.BOOL, W.HANDLE, use_last_error=True)
DELETE = C.WINFUNCTYPE(W.BOOL, W.LPCWSTR, use_last_error=True)
DELETE_A = C.WINFUNCTYPE(W.BOOL, C.c_char_p, use_last_error=True)
LOCK = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.DWORD, W.DWORD, W.DWORD,
                    W.DWORD, C.c_void_p, use_last_error=True)

class Overlapped(C.Structure):
    _fields_ = [("internal", C.c_size_t), ("internal_high", C.c_size_t),
                ("offset", W.DWORD), ("offset_high", W.DWORD), ("event", W.HANDLE)]

class IoMethods(C.Structure):
    # Public sqlite3_io_methods ABI, through version-2 xShmBarrier.
    _fields_ = [("version", C.c_int)] + [(name, C.c_void_p) for name in (
        "xClose", "xRead", "xWrite", "xTruncate", "xSync", "xFileSize",
        "xLock", "xUnlock", "xCheckReservedLock", "xFileControl", "xSectorSize",
        "xDeviceCharacteristics", "xShmMap", "xShmLock", "xShmBarrier")]

def checksum(data, endian, initial=(0, 0)):
    words = struct.unpack(endian + str(len(data) // 4) + "I", data)
    first, second = initial
    for offset in range(0, len(words), 2):
        first = (first + words[offset] + second) & 0xffffffff
        second = (second + words[offset + 1] + first) & 0xffffffff
    return first, second

def norm(path):
    value = os.path.abspath(os.path.normpath(str(path)))
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(value)

class Guard:
    def __init__(self, source):
        self.counts = {name: 0 for name in (
            "read_opens", "create_downgraded_to_existing", "denied_open",
            "denied_write", "denied_truncate", "denied_delete",
            "readonly_mappings", "denied_mapping", "os_lock_calls", "callback_errors")}
        self.callbacks = []
        self.originals = {}
        self.pins = []
        self.allowed = {}
        self.control = None
        self.validation_view = None
        self.validation_mapping = None
        self.validation_region = None
        self.os = C.WinDLL("kernel32", use_last_error=True)
        self.os.GetFileInformationByHandle.argtypes = [W.HANDLE, C.POINTER(FileInfo)]
        self.os.GetFileInformationByHandle.restype = W.BOOL
        self.os.CloseHandle.argtypes = [W.HANDLE]
        self.os.CloseHandle.restype = W.BOOL
        self.os.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
        self.os.GetFinalPathNameByHandleW.restype = W.DWORD
        if (os.name != "nt" or apsw.apswversion() != "3.53.3.1"
                or apsw.sqlitelibversion() != "3.53.3"
                or "multipleciphers-win32" not in apsw.vfs_names()):
            raise RuntimeError("unsupported SQLite runtime")
        if not isinstance(source, str) or not os.path.isabs(source) or Path(source).suffix.lower() != ".edb":
            raise RuntimeError("source path is invalid")
        self.os.GetDriveTypeW.argtypes = [W.LPCWSTR]
        self.os.GetDriveTypeW.restype = W.UINT
        if self.os.GetDriveTypeW(Path(source).anchor) not in (2, 3, 6):
            raise RuntimeError("source must be on a local drive")
        try:
            self._pin_source(source)
            self.control = apsw.VFS("readonly-worker-control", "win32")
            available = set()
            current = None
            while (current := self.control.xNextSystemCall(current)) is not None:
                available.add(current)
            if any(name.startswith("CreateFile") and name not in
                   {"CreateFileW", "CreateFileA", "CreateFileMappingW"} for name in available):
                raise RuntimeError("unsupported SQLite syscall table")
            self.install()
        except BaseException:
            self.close()
            raise

    def _pin_source(self, source):
        native_open = CREATE(("CreateFileW", self.os))
        for suffix in ("", "-wal", "-shm"):
            path = str(source) + suffix
            for part in (Path(path), *Path(path).parents):
                try:
                    metadata = os.lstat(part)
                except OSError:
                    if suffix == "-shm":
                        raise SourceShmUnavailable() from None
                    raise RuntimeError("source path unavailable") from None
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("source reparse point rejected")
            expected = os.stat(path)
            handle = native_open(path, GENERIC_READ, 3, None, OPEN_EXISTING, 0x00200000, None)
            if handle in (None, INVALID):
                if suffix == "-shm":
                    raise SourceShmUnavailable()
                raise RuntimeError("source read pin unavailable")
            self.pins.append(handle)
            identity, attributes, size = self.info(handle)
            if attributes & 0x400 or identity[1] != expected.st_ino:
                raise RuntimeError("source identity changed")
            final = C.create_unicode_buffer(32768)
            length = self.os.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not 0 < length < len(final) or norm(final.value) != norm(path):
                raise RuntimeError("source canonical path mismatch")
            self.allowed[norm(path)] = identity

    def close(self):
        self._close_validation()
        if self.control is not None:
            for name, address in reversed(tuple(self.originals.items())):
                self.control.xSetSystemCall(name, address)
            self.originals.clear()
        while self.pins:
            if not self.os.CloseHandle(self.pins[-1]):
                raise RuntimeError("source pin cleanup failed")
            self.pins.pop()

    def _close_validation(self):
        if self.validation_view:
            self.os.UnmapViewOfFile.argtypes = [C.c_void_p]
            self.os.UnmapViewOfFile.restype = W.BOOL
            if not self.os.UnmapViewOfFile(self.validation_view):
                raise RuntimeError("source validation view cleanup failed")
            self.validation_view = None
        if self.validation_mapping:
            if not self.os.CloseHandle(self.validation_mapping):
                raise RuntimeError("source validation mapping cleanup failed")
            self.validation_mapping = None
        if self.validation_region is not None:
            unlock = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.DWORD, W.DWORD, W.DWORD,
                                  C.c_void_p, use_last_error=True)(("UnlockFileEx", self.os))
            if not unlock(self.pins[2], 0, 7, 0, C.byref(self.validation_region)):
                raise RuntimeError("source validation lock cleanup failed")
            self.validation_region = None

    def info(self, handle):
        result = FileInfo()
        if not self.os.GetFileInformationByHandle(handle, C.byref(result)):
            raise RuntimeError("source metadata unavailable")
        return ((result.volume, (result.index_high << 32) | result.index_low),
                result.attributes, (result.size_high << 32) | result.size_low)

    def deny(self, name, invalid=0):
        self.counts[name] += 1
        C.set_last_error(5)
        return invalid

    def hook(self, name, prototype, function):
        address = self.control.xGetSystemCall(name)
        if not address:
            raise RuntimeError("required VFS syscall unavailable")
        original = prototype(address)
        def callback(*args):
            try:
                return function(original, *args)
            except BaseException:
                self.counts["callback_errors"] += 1
                C.set_last_error(5)
                return INVALID if name.startswith("CreateFile") and "Mapping" not in name else 0
        wrapped = prototype(callback)
        self.callbacks.append(wrapped)
        if not self.control.xSetSystemCall(name, C.cast(wrapped, C.c_void_p).value):
            raise RuntimeError("VFS syscall hook rejected")
        self.originals[name] = address

    def install(self):
        def create(original, path, access, share, security, disposition, flags, template):
            name = norm(path)
            if name not in self.allowed or access != GENERIC_READ or flags & 0x04000000:
                return self.deny("denied_open", INVALID)
            if disposition not in (OPEN_EXISTING, 4):
                return self.deny("denied_open", INVALID)
            if disposition == 4:
                self.counts["create_downgraded_to_existing"] += 1
            handle = original(path, access, share & 3, security, OPEN_EXISTING,
                              flags | 0x00200000, template)
            if handle in (None, INVALID):
                return handle
            try:
                identity, attributes, size = self.info(handle)
            except BaseException:
                self.os.CloseHandle(handle)
                raise
            if attributes & 0x400 or identity != self.allowed[name]:
                self.os.CloseHandle(handle)
                return self.deny("denied_open", INVALID)
            self.counts["read_opens"] += 1
            return handle

        def mapping(original, handle, security, protection, high, low, name):
            identity, attributes, size = self.info(handle)
            requested = (high << 32) | low
            if identity not in self.allowed.values() or protection != 2 or requested > size:
                return self.deny("denied_mapping")
            self.counts["readonly_mappings"] += 1
            return original(handle, security, protection, high, low, name)

        def mapview(original, handle, access, high, low, size):
            if access != 4:
                return self.deny("denied_mapping")
            return original(handle, access, high, low, size)

        def lock(original, *args):
            self.counts["os_lock_calls"] += 1
            return original(*args)

        self.hook("CreateFileW", CREATE, create)
        self.hook("CreateFileA", CREATE_A, lambda original, *args: self.deny("denied_open", INVALID))
        self.hook("CreateFileMappingW", MAPPING, mapping)
        self.hook("MapViewOfFile", MAPVIEW, mapview)
        self.hook("WriteFile", WRITE, lambda original, *args: self.deny("denied_write"))
        self.hook("SetEndOfFile", TRUNCATE, lambda original, *args: self.deny("denied_truncate"))
        self.hook("DeleteFileW", DELETE, lambda original, *args: self.deny("denied_delete"))
        self.hook("DeleteFileA", DELETE_A, lambda original, *args: self.deny("denied_delete"))
        self.hook("LockFileEx", LOCK, lock)

    def negative_checks(self, source):
        create = CREATE(self.control.xGetSystemCall("CreateFileW"))
        write = WRITE(self.control.xGetSystemCall("WriteFile"))
        truncate = TRUNCATE(self.control.xGetSystemCall("SetEndOfFile"))
        delete = DELETE(self.control.xGetSystemCall("DeleteFileW"))
        mapping = MAPPING(self.control.xGetSystemCall("CreateFileMappingW"))
        buffer = C.create_string_buffer(b"SYNTHETIC")
        return {
            "write_open_denied": create(str(source), 0xC0000000, 3, None, 3, 0, None) == INVALID,
            "new_file_denied": create(str(source) + "-forbidden", GENERIC_READ, 3, None, 4, 0, None) == INVALID,
            "byte_write_denied": not write(self.pins[0], buffer, 9, None, None),
            "truncate_denied": not truncate(self.pins[0]),
            "delete_denied": not delete(str(source)),
            "writable_mapping_denied": not mapping(self.pins[0], None, 4, 0, 0, None),
        }

    def verify_committed_wal(self, connection, cancelled=lambda: False):
        """Validate declared commits inside this actual read transaction.

        Shared CKPT/RECOVER/read-slot locks prevent backfill, recovery and WAL
        reset during validation, but do not take the WRITER lock: appending
        uncommitted frames is allowed. Read SHM via a read-only coherent mapping,
        not ReadFile over its reserved lock bytes. All WAL bytes use SQLite's
        already-open native VFS handle via APSW read(), never a path precheck.
        """
        if connection.txn_state("main") != apsw.SQLITE_TXN_READ:
            raise RuntimeError("read transaction required")
        pointer = C.c_void_p()
        if not connection.file_control("main", apsw.SQLITE_FCNTL_FILE_POINTER, C.addressof(pointer)):
            raise RuntimeError("native file unavailable")
        if not pointer.value:
            raise RuntimeError("native file unavailable")
        methods_address = C.c_void_p.from_address(pointer.value).value
        methods = IoMethods.from_address(methods_address)
        if methods.version < 2 or not methods.xShmBarrier:
            raise RuntimeError("native SHM barrier unavailable")
        barrier = C.CFUNCTYPE(None, C.c_void_p)(methods.xShmBarrier)
        shm = self.pins[2]
        if self.info(shm)[2] < 100:
            raise RuntimeError("source SHM header unavailable")
        lock = LOCK(("LockFileEx", self.os))
        region = Overlapped(offset=121)  # CKPT, RECOVER, READ0..READ4; excludes WRITE120.
        if not lock(shm, 1, 0, 7, 0, C.byref(region)):  # shared, fail immediately
            raise RuntimeError("source validation lock unavailable")
        self.validation_region = region
        mapping = view = None
        try:
            create_mapping = MAPPING(self.control.xGetSystemCall("CreateFileMappingW"))
            map_view = MAPVIEW(self.control.xGetSystemCall("MapViewOfFile"))
            mapping = create_mapping(shm, None, 2, 0, 0, None)  # PAGE_READONLY, existing size
            if not mapping:
                raise RuntimeError("source SHM mapping unavailable")
            self.validation_mapping = mapping
            view = map_view(mapping, 4, 0, 0, 100)  # FILE_MAP_READ; never access lock bytes
            if not view:
                raise RuntimeError("source SHM mapping unavailable")
            self.validation_view = view

            def header():
                first = C.string_at(view, 48)
                barrier(pointer)
                second = C.string_at(view + 48, 48)
                backfill = C.string_at(view + 96, 4)
                return first, second, backfill

            for _ in range(8):  # finite retries for a concurrent header publication
                if cancelled():
                    raise RuntimeError("read cancelled")
                first, second, backfill_bytes = header()
                if (first != second or first[12] != 1
                        or checksum(first[:40], "<") != struct.unpack("<2I", first[40:48])):
                    continue
                version = struct.unpack_from("<I", first)[0]
                mx_frame, n_page = struct.unpack_from("<2I", first, 16)
                backfill = struct.unpack("<I", backfill_bytes)[0]
                page_size = struct.unpack_from("<H", first, 14)[0]
                page_size = 65536 if page_size == 1 else page_size
                if (version != 3007000 or backfill > mx_frame or first[13] not in (0, 1)
                        or not 512 <= page_size <= 65536 or page_size & (page_size - 1)):
                    raise RuntimeError("source WAL metadata invalid")
                if mx_frame > backfill:
                    required = 32 + mx_frame * (24 + page_size)
                    if self.info(self.pins[1])[2] < required:
                        raise RuntimeError("declared committed WAL is incomplete")
                    complete, wal_header = connection.read("main", 1, 0, 32)
                    if not complete:
                        raise RuntimeError("declared committed WAL is incomplete")
                    magic, wal_version, wal_page = struct.unpack_from(">3I", wal_header)
                    if (magic not in (0x377f0682, 0x377f0683) or wal_version != 3007000
                            or wal_page != page_size or wal_header[16:24] != first[32:40]
                            or magic & 1 != first[13]):
                        raise RuntimeError("source WAL header disagrees with SHM")
                    endian = ">" if magic & 1 else "<"
                    chain = checksum(wal_header[:24], endian)
                    if chain != struct.unpack_from(">2I", wal_header, 24):
                        raise RuntimeError("source WAL header checksum invalid")
                    for index in range(1, mx_frame + 1):
                        if cancelled():
                            raise RuntimeError("read cancelled")
                        complete, frame = connection.read("main", 1,
                            32 + (index - 1) * (24 + page_size), 24 + page_size)
                        if not complete or frame[8:16] != first[32:40]:
                            raise RuntimeError("declared committed WAL is incomplete")
                        page, commit_size = struct.unpack_from(">2I", frame)
                        chain = checksum(frame[:8], endian, chain)
                        chain = checksum(frame[24:], endian, chain)
                        if not page or chain != struct.unpack_from(">2I", frame, 16):
                            raise RuntimeError("declared committed WAL checksum invalid")
                    if commit_size != n_page or chain != struct.unpack_from("<2I", first, 24):
                        raise RuntimeError("declared committed WAL disagrees with SHM")
                barrier(pointer)
                if header() == (first, second, backfill_bytes):
                    self.counts["committed_wal_checks"] = self.counts.get("committed_wal_checks", 0) + 1
                    return
            raise RuntimeError("source SHM publication unavailable")
        finally:
            self._close_validation()

