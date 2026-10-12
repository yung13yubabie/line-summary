"""Fail-closed Windows process identity, separate from candidate scanning.

Trust is the OS Authenticode chain AND an exact publisher organisation AND
signed PE version resources. Paths, environment variables and caller arguments
are never authorities. All native operations are replaceable by synthetic APIs
in tests; no native library is loaded at import time.
"""
from contextlib import ExitStack
import ctypes as c
from ctypes import wintypes
from dataclasses import dataclass
import ntpath
import struct
import uuid

DWORD = c.c_uint32
BOOL = c.c_int32
HANDLE = c.c_void_p
INVALID_HANDLE = c.c_void_p(-1).value
QUERY_LIMITED = 0x1000
SCAN_RIGHTS = 0x0400 | 0x0010
IDENTITY_ERROR = "LINE process identity could not be verified; no memory read performed"
# Publisher organisations, not certificate display-name substrings. An update
# signed by a different entity requires a reviewed policy change, never fallback.
LINE_PUBLISHERS = frozenset({"LINE Corporation", "LY Corporation"})


class FILETIME(c.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class WINTRUST_FILE_INFO(c.Structure):
    _fields_ = [("cbStruct", DWORD), ("pcwszFilePath", c.c_wchar_p),
                ("hFile", HANDLE), ("pgKnownSubject", c.c_void_p)]


class WINTRUST_DATA(c.Structure):
    _fields_ = [("cbStruct", DWORD), ("pPolicyCallbackData", c.c_void_p),
                ("pSIPClientData", c.c_void_p), ("dwUIChoice", DWORD),
                ("fdwRevocationChecks", DWORD), ("dwUnionChoice", DWORD),
                ("pFile", c.POINTER(WINTRUST_FILE_INFO)),
                ("dwStateAction", DWORD), ("hWVTStateData", HANDLE),
                ("pwszURLReference", c.c_wchar_p), ("dwProvFlags", DWORD),
                ("dwUIContext", DWORD), ("pSignatureSettings", c.c_void_p)]


class PROVIDER_CERT_PREFIX(c.Structure):
    # Only the documented leading fields are dereferenced.
    _fields_ = [("cbStruct", DWORD), ("pCert", c.c_void_p)]


def _declare(library, signatures):
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result


def configure_kernel(kernel):
    _declare(kernel, {
        "GetProcessId": ([HANDLE], DWORD),
        "GetProcessTimes": ([HANDLE] + [c.POINTER(FILETIME)] * 4, BOOL),
        "CreateFileW": ([c.c_wchar_p, DWORD, DWORD, c.c_void_p, DWORD, DWORD, HANDLE], HANDLE),
        "GetFinalPathNameByHandleW": ([HANDLE, c.c_wchar_p, DWORD, DWORD], DWORD),
        "GetFileSizeEx": ([HANDLE, c.POINTER(c.c_int64)], BOOL),
        "SetFilePointerEx": ([HANDLE, c.c_int64, c.POINTER(c.c_int64), DWORD], BOOL),
        "ReadFile": ([HANDLE, c.c_void_p, DWORD, c.POINTER(DWORD), c.c_void_p], BOOL),
    })


def _load_trust_apis():
    # LOAD_LIBRARY_SEARCH_SYSTEM32: no current-directory/PATH DLL lookup.
    trust = c.WinDLL("wintrust.dll", use_last_error=True, winmode=0x800)
    crypto = c.WinDLL("crypt32.dll", use_last_error=True, winmode=0x800)
    _declare(trust, {
        "WinVerifyTrust": ([HANDLE, c.c_void_p, c.POINTER(WINTRUST_DATA)], c.c_int32),
        "WTHelperProvDataFromStateData": ([HANDLE], c.c_void_p),
        "WTHelperGetProvSignerFromChain": ([c.c_void_p, DWORD, BOOL, DWORD], c.c_void_p),
        "WTHelperGetProvCertFromChain": ([c.c_void_p, DWORD], c.POINTER(PROVIDER_CERT_PREFIX)),
    })
    _declare(crypto, {
        "CertGetNameStringW": ([c.c_void_p, DWORD, DWORD, c.c_void_p, c.c_wchar_p, DWORD], DWORD),
    })
    return trust, crypto


def _verify_publisher(file_handle, path):
    trust, crypto = _load_trust_apis()
    info = WINTRUST_FILE_INFO(c.sizeof(WINTRUST_FILE_INFO), path, file_handle, None)
    data = WINTRUST_DATA()
    data.cbStruct = c.sizeof(data)
    data.dwUIChoice = 2  # WTD_UI_NONE
    data.fdwRevocationChecks = 1  # WTD_REVOKE_WHOLECHAIN
    data.dwUnionChoice = 1  # WTD_CHOICE_FILE
    data.pFile = c.pointer(info)
    data.dwStateAction = 1  # VERIFY; every call paired with CLOSE below
    # Chain revocation (except root), cache only, reject MD2/MD4. No downloads,
    # UI, weaker retry, or automatic trust-store changes on unavailable trust.
    data.dwProvFlags = 0x80 | 0x1000 | 0x2000
    action = (c.c_ubyte * 16).from_buffer_copy(
        uuid.UUID("00aac56b-cd44-11d0-8cc2-00c04fc295ee").bytes_le)
    try:
        # WinVerifyTrust is LONG, not HRESULT: ONLY exactly zero is success.
        if trust.WinVerifyTrust(INVALID_HANDLE, action, c.byref(data)) != 0:
            raise ValueError("untrusted image")
        provider = trust.WTHelperProvDataFromStateData(data.hWVTStateData)
        if not provider:
            raise ValueError("missing trust provider")
        signer = trust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
        if not signer:
            raise ValueError("missing signer")
        certificate = trust.WTHelperGetProvCertFromChain(signer, 0)
        if not certificate or certificate.contents.cbStruct < c.sizeof(PROVIDER_CERT_PREFIX) or not certificate.contents.pCert:
            raise ValueError("missing signer certificate")
        # CERT_NAME_ATTR_TYPE with OID organizationName; never a display name.
        oid = c.c_char_p(b"2.5.4.10")
        subject = c.create_unicode_buffer(256)
        length = crypto.CertGetNameStringW(certificate.contents.pCert, 3, 0,
                                         c.cast(oid, c.c_void_p), subject, len(subject))
        if not 1 < length < len(subject) or length != len(subject.value) + 1 or subject.value not in LINE_PUBLISHERS:
            raise ValueError("unapproved publisher")
    finally:
        data.dwStateAction = 2
        trust.WinVerifyTrust(INVALID_HANDLE, action, c.byref(data))


def _signed_product_from_handle(kernel, file_handle):
    """Read bounded PE RT_VERSION data from the SAME locked, signed file.

    GetFileVersionInfo(path) would reopen a pathname and allow a replacement
    race/MUI metadata; this tiny reader follows only PE resource directories.
    It never loads or executes the image. Malformed/ambiguous data fails closed.
    """
    size = c.c_int64()
    if not kernel.GetFileSizeEx(file_handle, c.byref(size)) or not 64 <= size.value <= 512 * 1024 * 1024:
        raise ValueError("invalid image size")

    def read(offset, length):
        if offset < 0 or not 0 < length <= 1024 * 1024 or offset + length > size.value:
            raise ValueError("invalid image bounds")
        if not kernel.SetFilePointerEx(file_handle, offset, None, 0):
            raise ValueError("image seek failed")
        buffer, count = c.create_string_buffer(length), DWORD()
        if not kernel.ReadFile(file_handle, buffer, length, c.byref(count), None) or count.value != length:
            raise ValueError("image read failed")
        return buffer.raw

    dos = read(0, 64)
    if dos[:2] != b"MZ":
        raise ValueError("not PE")
    pe = struct.unpack_from("<I", dos, 60)[0]
    header = read(pe, 24)
    sections, optional_size = struct.unpack_from("<H", header, 6)[0], struct.unpack_from("<H", header, 20)[0]
    if header[:4] != b"PE\0\0" or not 1 <= sections <= 96 or not 120 <= optional_size <= 4096:
        raise ValueError("invalid PE headers")
    optional = read(pe + 24, optional_size)
    magic = struct.unpack_from("<H", optional)[0]
    directory = {0x10b: 96, 0x20b: 112}.get(magic)
    if directory is None or directory + 24 > len(optional) or struct.unpack_from("<I", optional, directory - 4)[0] < 3:
        raise ValueError("missing resource directory")
    resource_rva, resource_size = struct.unpack_from("<II", optional, directory + 16)
    if not resource_rva or not 16 <= resource_size <= 16 * 1024 * 1024:
        raise ValueError("invalid resource size")
    table = read(pe + 24 + optional_size, 40 * sections)

    def rva_read(rva, length):
        matches = []
        for i in range(sections):
            virtual_size, base, raw_size, raw_offset = struct.unpack_from("<IIII", table, i * 40 + 8)
            delta = rva - base
            if 0 <= delta and delta + length <= min(virtual_size, raw_size):
                matches.append(raw_offset + delta)
        if len(matches) != 1:
            raise ValueError("ambiguous resource mapping")
        return read(matches[0], length)

    def resource_read(offset, length):
        if offset < 0 or offset + length > resource_size:
            raise ValueError("resource outside directory")
        return rva_read(resource_rva + offset, length)

    visited = set()
    versions = []

    def directory_entries(offset, depth):
        if depth > 2 or offset in visited or len(visited) >= 128:
            raise ValueError("invalid resource tree")
        visited.add(offset)
        node = resource_read(offset, 16)
        named, ids = struct.unpack_from("<HH", node, 12)
        if not 1 <= named + ids <= 256:
            raise ValueError("invalid directory count")
        entries = resource_read(offset + 16, (named + ids) * 8)
        for name, target in struct.iter_unpack("<II", entries):
            if depth == 0 and name != 16:  # RT_VERSION
                continue
            if depth < 2:
                if not target & 0x80000000:
                    raise ValueError("invalid resource branch")
                directory_entries(target & 0x7fffffff, depth + 1)
            else:
                if target & 0x80000000 or len(versions) >= 16:
                    raise ValueError("invalid resource leaf")
                rva, length, _, reserved = struct.unpack("<IIII", resource_read(target, 16))
                if reserved or not 0 < length <= 1024 * 1024:
                    raise ValueError("invalid version data")
                versions.append(rva_read(rva, length))

    directory_entries(0, 0)
    if not versions:
        raise ValueError("missing signed version")
    for version in versions:
        _check_version_resource(version)


def _check_version_resource(blob):
    tables = []
    nodes = 0

    def block(start, limit, parents):
        nonlocal nodes
        nodes += 1
        if nodes > 512 or len(parents) > 4 or start + 6 > limit:
            raise ValueError("invalid version tree")
        length, value_length, kind = struct.unpack_from("<HHH", blob, start)
        end = start + length
        if length < 8 or end > limit or kind not in (0, 1):
            raise ValueError("invalid version block")
        cursor = start + 6
        key_end = cursor
        while key_end + 2 <= end and blob[key_end:key_end + 2] != b"\0\0":
            key_end += 2
        if key_end + 2 > end:
            raise ValueError("unterminated version key")
        key = blob[cursor:key_end].decode("utf-16le")
        cursor = (key_end + 2 + 3) & ~3
        value_bytes = value_length * 2 if kind == 1 else value_length
        if cursor + value_bytes > end:
            raise ValueError("invalid version value")
        value = blob[cursor:cursor + value_bytes]
        fields = {}
        cursor = (cursor + value_bytes + 3) & ~3
        while cursor + 6 <= end:
            child_end, child_key, child_value = block(cursor, end, parents + (key,))
            if child_key in fields:
                raise ValueError("duplicate version field")
            fields[child_key] = child_value
            cursor = (child_end + 3) & ~3
        if parents == ("VS_VERSION_INFO", "StringFileInfo"):
            tables.append(fields)
        if not parents and key != "VS_VERSION_INFO":
            raise ValueError("invalid version root")
        text = value.decode("utf-16le") if kind == 1 else ""
        return end, key, text.rstrip("\0")

    block(0, len(blob), ())
    if not tables or any(t.get("ProductName") != "LINE" or t.get("OriginalFilename", "").lower() != "line.exe" for t in tables):
        raise ValueError("unapproved signed product")


def _path(value):
    # Local DOS paths only; no UNC/network lookup, device names, ADS, or paths
    # derived from the environment. Extended-length local paths remain Unicode.
    original = value
    if value.startswith("\\\\?\\"):
        value = value[4:]
    drive, tail = ntpath.splitdrive(value)
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha() or not tail.startswith("\\"):
        raise ValueError("nonlocal image path")
    parts = tail.split("\\")[1:]
    if not parts or any(not p or p in (".", "..") or p.endswith((".", " ")) or ":" in p or "\0" in p for p in parts):
        raise ValueError("ambiguous image path")
    if parts[-1].lower() != "line.exe":
        raise ValueError("wrong executable")
    # Validation must not rewrite the name used to open the image. In a
    # case-sensitive directory a lowercased name can identify a different file.
    # Preserve the extended-length prefix as well as every Unicode character.
    return original


def _utf16_length(value):
    return len(value.encode("utf-16le")) // 2


@dataclass(frozen=True)
class Identity:
    pid: int
    created: int
    path: str


def _identity(kernel, handle, requested_pid):
    actual_pid = kernel.GetProcessId(handle)
    if actual_pid != requested_pid:
        raise ValueError("process id changed")
    creation, exit_time, kernel_time, user_time = (FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, c.byref(creation), c.byref(exit_time), c.byref(kernel_time), c.byref(user_time)):
        raise ValueError("process time unavailable")
    created = (creation.high << 32) | creation.low
    if not created or exit_time.low or exit_time.high:
        raise ValueError("process exited")
    image = c.create_unicode_buffer(32768)
    # Match the existing kernel32 declaration exactly (wintypes.DWORD).
    length = wintypes.DWORD(len(image))
    if not kernel.QueryFullProcessImageNameW(handle, 0, image, c.byref(length)) or not 0 < length.value < len(image) or length.value != _utf16_length(image.value):
        raise ValueError("image path unavailable")
    return Identity(actual_pid, created, _path(image.value))


@dataclass
class VerifiedProcess:
    handle: int
    _resources: ExitStack

    def close(self):
        try:
            self._resources.close()
        except Exception:
            raise RuntimeError("LINE process handle cleanup failed; diagnostic details suppressed") from None


def _close_handle(kernel, handle):
    # Win32 reports normal CloseHandle failure as BOOL FALSE, not a Python
    # exception. ExitStack still attempts the remaining callbacks if this fails.
    if not kernel.CloseHandle(handle):
        raise RuntimeError("LINE process handle cleanup failed; diagnostic details suppressed") from None


def open_verified_line_process(kernel, pid):
    """Retain anchor, locked image and scan handles until scanning completes."""
    try:
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError("invalid process id")
        with ExitStack() as resources:
            anchor = kernel.OpenProcess(QUERY_LIMITED, False, pid)
            if anchor in (None, 0, -1, INVALID_HANDLE):
                return None
            resources.callback(_close_handle, kernel, anchor)
            before = _identity(kernel, anchor, pid)
            # GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING. Deny in-place
            # write/delete while checking the executable and scanning memory.
            image_file = kernel.CreateFileW(before.path, 0x80000000, 1, None, 3, 0, None)
            if image_file in (None, 0, -1, INVALID_HANDLE):
                raise ValueError("image could not be locked")
            resources.callback(_close_handle, kernel, image_file)
            final_path = c.create_unicode_buffer(32768)
            length = kernel.GetFinalPathNameByHandleW(image_file, final_path, len(final_path), 0)
            if not 0 < length < len(final_path) or length != _utf16_length(final_path.value):
                raise ValueError("image path unavailable")
            _path(final_path.value)
            # GetFinalPathNameByHandle returns the fully resolved target. It
            # may differ from the process name for a stable junction/symlink
            # or short-name alias; textual equality is not an identity proof.
            # Authenticate the retained file handle, and recheck the unchanged
            # process handles below. The pre-open mapped-image/path replacement
            # question remains a separate, unverified native boundary.
            _verify_publisher(image_file, before.path)
            _signed_product_from_handle(kernel, image_file)
            scan = kernel.OpenProcess(SCAN_RIGHTS, False, pid)
            if scan in (None, 0, -1, INVALID_HANDLE):
                raise ValueError("process unavailable")
            resources.callback(_close_handle, kernel, scan)
            # Recheck the retained anchor as well as the newly opened handle.
            # No PID-only reopening occurs after this point.
            if _identity(kernel, scan, pid) != before or _identity(kernel, anchor, pid) != before:
                raise ValueError("process identity changed")
            return VerifiedProcess(scan, resources.pop_all())
    except Exception:
        raise RuntimeError(IDENTITY_ERROR) from None
