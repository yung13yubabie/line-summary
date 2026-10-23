"""Only artificial handles and byte buffers; no native process access."""
import ctypes
import struct
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import key_extractor as ke
import line_process_identity as identity

TOKEN = "aBcD0123456789EFaBcD0123456789EF"
PRIVATE_PATH = r"C:\SYNTHETIC_PRIVATE_PATH\unrelated\LINE.exe"
TRUSTED_PATH = r"C:\合成使用者\應用程式\LINE\LINE.exe"


def version_blob(product="LINE", filename="LINE.exe"):
    def block(key, value=b"", children=(), kind=1):
        blob = bytearray(b"\0" * 6 + key.encode("utf-16le") + b"\0\0")
        blob += b"\0" * (-len(blob) % 4)
        blob += value
        if children:
            blob += b"\0" * (-len(blob) % 4)
        for child in children:
            blob += child
            blob += b"\0" * (-len(blob) % 4)
        struct.pack_into("<HHH", blob, 0, len(blob), len(value) // 2 if kind else len(value), kind)
        return bytes(blob)
    fields = [block(key, (text + "\0").encode("utf-16le")) for key, text in
              [("ProductName", product), ("OriginalFilename", filename)]]
    table = block("040904b0", children=fields)
    strings = block("StringFileInfo", children=[table])
    return block("VS_VERSION_INFO", value=b"\0" * 52, children=[strings], kind=0)


def signed_pe(product="LINE", filename="LINE.exe", pe64=False):
    """Artificial PE resources; authenticity itself is supplied by fake WinTrust."""
    version = version_blob(product, filename)
    resources = bytearray(96 + len(version))
    for offset, resource_id, target in [(0, 16, 24 | 0x80000000), (24, 1, 48 | 0x80000000), (48, 1033, 80)]:
        struct.pack_into("<HHII", resources, offset + 12, 0, 1, resource_id, target)
    struct.pack_into("<IIII", resources, 80, 0x1000 + 96, len(version), 0, 0)
    resources[96:] = version
    image = bytearray(0x200 + len(resources))
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 60, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    optional_size = 240 if pe64 else 224
    struct.pack_into("<H", image, 0x80 + 6, 1)
    struct.pack_into("<H", image, 0x80 + 20, optional_size)
    struct.pack_into("<H", image, 0x98, 0x20b if pe64 else 0x10b)
    directory = 112 if pe64 else 96
    struct.pack_into("<I", image, 0x98 + directory - 4, 16)
    struct.pack_into("<II", image, 0x98 + directory + 16, 0x1000, len(resources))
    struct.pack_into("<IIII", image, 0x98 + optional_size + 8, len(resources), 0x1000, len(resources), 0x200)
    image[0x200:] = resources
    return bytes(image)


class Kernel:
    anchor, scan, image_file = 0x100000001, 0x100000002, 0x100000003

    def __init__(self, path=TRUSTED_PATH):
        self.path = path
        self.final_path = "\\\\?\\" + path
        self.reads = 0
        self.queried = False
        self.closed = []
        self.events = []
        self.before_scan = lambda: None
        self.after_trust = lambda: None
        self.states = {h: {"pid": 901, "created": 123456789, "exited": 0, "path": path}
                       for h in (self.anchor, self.scan)}
        self.image_ok = self.times_ok = self.file_ok = True
        self.position = 0
        self.file_bytes = signed_pe()
        self.publisher = "LY Corporation"
        self.trust_status = 0 if path == TRUSTED_PATH else 0x80096010
        self.open_fail = None

    def OpenProcess(self, rights, inherit, pid):
        assert inherit is False
        self.events.append(("open", rights, pid))
        if rights == self.open_fail:
            return 0
        if rights == identity.QUERY_LIMITED:
            return self.anchor
        assert rights == identity.SCAN_RIGHTS
        self.before_scan()
        return self.scan

    def GetProcessId(self, handle):
        return self.states[handle]["pid"]

    def GetProcessTimes(self, handle, created, exited, kernel, user):
        for pointer, value in [(created, self.states[handle]["created"]), (exited, self.states[handle]["exited"])]:
            stamp = ctypes.cast(pointer, ctypes.POINTER(identity.FILETIME)).contents
            stamp.low, stamp.high = value & 0xffffffff, value >> 32
        return self.times_ok

    def QueryFullProcessImageNameW(self, handle, flags, target, length):
        target.value = self.states[handle]["path"]
        ctypes.cast(length, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = identity._utf16_length(target.value)
        return self.image_ok

    def CreateFileW(self, path, access, share, attributes, creation, flags, template):
        assert path == identity._path(self.path)
        assert (access, share, attributes, creation, flags, template) == (0x80000000, 1, None, 3, 0, None)
        self.events.append(("lock_image",))
        return self.image_file if self.file_ok else identity.INVALID_HANDLE

    def GetFinalPathNameByHandleW(self, handle, target, size, flags):
        assert handle == self.image_file and flags == 0
        target.value = self.final_path
        return identity._utf16_length(target.value)

    def GetFileSizeEx(self, handle, target):
        assert handle == self.image_file
        ctypes.cast(target, ctypes.POINTER(ctypes.c_int64)).contents.value = len(self.file_bytes)
        return 1

    def SetFilePointerEx(self, handle, offset, result, origin):
        assert handle == self.image_file and origin == 0
        self.position = offset
        return 1

    def ReadFile(self, handle, target, size, count, overlapped):
        assert handle == self.image_file and self.image_file not in self.closed
        chunk = self.file_bytes[self.position:self.position + size]
        ctypes.memmove(target, chunk, len(chunk))
        ctypes.cast(count, ctypes.POINTER(identity.DWORD)).contents.value = len(chunk)
        return 1

    def VirtualQueryEx(self, handle, address, target, size):
        if self.queried:
            return 0
        self.queried = True
        region = ctypes.cast(target, ctypes.POINTER(ke.MEMORY_BASIC_INFORMATION)).contents
        region.RegionSize = 4096
        region.State, region.Type, region.Protect = ke.MEM_COMMIT, ke.MEM_PRIVATE, 4
        return size

    def ReadProcessMemory(self, handle, address, target, size, count):
        assert handle == self.scan and not self.closed
        self.reads += 1
        self.events.append(("read_memory",))
        chunk = (" " + TOKEN + " ").encode()
        ctypes.memmove(target, chunk, len(chunk))
        ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(chunk)
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1  # Successful Win32 CloseHandle returns a nonzero BOOL.


def trust_apis(kernel):
    certificate = identity.PROVIDER_CERT_PREFIX(ctypes.sizeof(identity.PROVIDER_CERT_PREFIX), 12)

    def verify(window, action, pointer):
        data = ctypes.cast(pointer, ctypes.POINTER(identity.WINTRUST_DATA)).contents
        assert data.dwUIChoice == 2 and data.fdwRevocationChecks == 1
        assert data.dwProvFlags == 0x80 | 0x1000 | 0x2000
        assert data.pFile.contents.hFile == kernel.image_file
        assert data.pFile.contents.pcwszFilePath == identity._path(kernel.path)
        if data.dwStateAction == 1:
            kernel.events.append(("trust_verify",))
            data.hWVTStateData = 23
            kernel.after_trust()
            return kernel.trust_status
        assert data.dwStateAction == 2
        kernel.events.append(("trust_close",))
        return 0

    def name(cert, kind, flags, oid, target, size):
        assert cert == 12 and kind == 3 and flags == 0
        assert ctypes.cast(oid, ctypes.c_char_p).value == b"2.5.4.10"
        target.value = kernel.publisher
        return len(target.value) + 1

    return SimpleNamespace(WinVerifyTrust=verify,
                           WTHelperProvDataFromStateData=lambda state: 34,
                           WTHelperGetProvSignerFromChain=lambda *a: 45,
                           WTHelperGetProvCertFromChain=lambda *a: ctypes.pointer(certificate)), SimpleNamespace(CertGetNameStringW=name)


def bind(monkeypatch, kernel):
    monkeypatch.setattr(ke, "_load_kernel32", lambda: kernel)
    monkeypatch.setattr(identity, "_load_trust_apis", lambda: trust_apis(kernel))


def rejected(monkeypatch, kernel):
    bind(monkeypatch, kernel)
    with pytest.raises(RuntimeError, match="identity") as error:
        ke._scan_memory_regions(901)
    assert kernel.reads == 0
    formatted = "".join(traceback.format_exception(error.value))
    assert PRIVATE_PATH not in formatted and TRUSTED_PATH not in formatted
    assert error.value.__suppress_context__
    assert kernel.closed and len(kernel.closed) == len(set(kernel.closed))


def test_unrelated_same_basename_rejected_before_first_read(monkeypatch):
    rejected(monkeypatch, Kernel(PRIVATE_PATH))


@pytest.mark.parametrize("publisher", sorted(identity.LINE_PUBLISHERS))
@pytest.mark.parametrize("pe64", [False, True])
def test_verified_unicode_install_uses_only_retained_scan_handle(monkeypatch, publisher, pe64):
    kernel = Kernel()
    kernel.publisher, kernel.file_bytes = publisher, signed_pe(pe64=pe64)
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == [TOKEN]
    assert kernel.reads == 1
    assert kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]
    assert [event for event in kernel.events if event[0] == "open"] == [
        ("open", identity.QUERY_LIMITED, 901), ("open", identity.SCAN_RIGHTS, 901)]
    assert kernel.events.index(("trust_verify",)) < kernel.events.index(("read_memory",))


@pytest.mark.parametrize("handle_field,field,value", [
    ("scan", "pid", 902), ("scan", "created", 123456790),
    ("scan", "created", 123456789 + (1 << 32)), ("scan", "path", PRIVATE_PATH),
    ("anchor", "created", 123456790), ("anchor", "path", PRIVATE_PATH),
    ("scan", "exited", 1), ("anchor", "exited", 1),
], ids=["pid_reused", "creation_reused", "creation_high_word", "switched_before_open", "anchor_changed", "anchor_path_changed", "scan_exited", "anchor_exited"])
def test_process_change_during_handle_open_rejected(monkeypatch, handle_field, field, value):
    kernel = Kernel()
    kernel.before_scan = lambda: kernel.states[getattr(kernel, handle_field)].update({field: value})
    rejected(monkeypatch, kernel)


@pytest.mark.parametrize("failure", ["image_query", "time_query", "zero_creation", "file_lock", "final_path", "trust_unavailable", "trust_loader", "signed_metadata", "scan_open"])
def test_unavailable_or_unbound_identity_never_reads(monkeypatch, failure):
    kernel = Kernel()
    if failure == "image_query":
        kernel.image_ok = False
    elif failure == "time_query":
        kernel.times_ok = False
    elif failure == "zero_creation":
        kernel.states[kernel.anchor]["created"] = 0
    elif failure == "file_lock":
        kernel.file_ok = False
    elif failure == "final_path":
        # A different resolved path is a legal alias, not an API failure.
        # Keep the unavailable-path case; its former input is covered below.
        kernel.final_path = ""
    elif failure == "trust_unavailable":
        kernel.trust_status = 0x80092013
    elif failure == "trust_loader":
        def fail():
            raise OSError(PRIVATE_PATH)
        kernel.after_trust = fail
    elif failure == "signed_metadata":
        kernel.file_bytes = b"no metadata" * 100
    else:
        kernel.open_fail = identity.SCAN_RIGHTS
    rejected(monkeypatch, kernel)
    if ("trust_verify",) in kernel.events:
        assert ("trust_close",) in kernel.events


@pytest.mark.parametrize("publisher", ["", "Unrelated Corporation", "Evil LY Corporation", "LY Corporation evil", "ly corporation"])
def test_publisher_is_exact_verified_organisation(monkeypatch, publisher):
    kernel = Kernel()
    kernel.publisher = publisher
    rejected(monkeypatch, kernel)


@pytest.mark.parametrize("product,filename", [("Other", "LINE.exe"), ("LINE", "Other.exe"), ("LINE Extra", "LINE.exe"), ("", "LINE.exe")])
def test_another_signed_product_cannot_be_renamed_line(monkeypatch, product, filename):
    kernel = Kernel()
    kernel.file_bytes = signed_pe(product, filename)
    rejected(monkeypatch, kernel)


def test_positive_nonzero_wintrust_status_is_failure(monkeypatch):
    kernel = Kernel()
    kernel.trust_status = 1
    rejected(monkeypatch, kernel)


@pytest.mark.parametrize("path", ["LINE.exe", r"C:LINE.exe", r"\\server\share\LINE.exe", r"C:\path\..\LINE.exe", r"C:\path\LINE.exe:stream", r"C:\path\LINE.exe ", r"C:\path\other.exe"])
def test_ambiguous_or_remote_paths_never_become_trusted(monkeypatch, path):
    rejected(monkeypatch, Kernel(path))


@pytest.mark.parametrize("api", ["GetProcessId", "GetProcessTimes", "QueryFullProcessImageNameW", "CreateFileW", "GetFinalPathNameByHandleW", "ReadFile"])
def test_identity_exceptions_do_not_disclose_private_paths(monkeypatch, api):
    kernel = Kernel()
    def fail(*a):
        raise OSError(PRIVATE_PATH)
    setattr(kernel, api, fail)
    rejected(monkeypatch, kernel)


def test_exit_before_anchor_does_not_retry_or_read(monkeypatch):
    kernel = Kernel()
    kernel.open_fail = identity.QUERY_LIMITED
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == []
    assert kernel.reads == 0 and kernel.closed == []


def test_scan_error_releases_all_handles(monkeypatch):
    kernel = Kernel()
    bind(monkeypatch, kernel)
    def fail(*a):
        raise OSError("synthetic scan failure")
    kernel.ReadProcessMemory = fail
    with pytest.raises(OSError):
        ke._scan_memory_regions(901)
    assert kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]


def test_native_declarations_keep_pointer_sized_handles(monkeypatch):
    kernel = SimpleNamespace(**{name: Mock() for name in ["GetProcessId", "GetProcessTimes", "CreateFileW", "GetFinalPathNameByHandleW", "GetFileSizeEx", "SetFilePointerEx", "ReadFile"]})
    identity.configure_kernel(kernel)
    assert kernel.GetProcessId.argtypes == [ctypes.c_void_p]
    assert kernel.CreateFileW.restype is ctypes.c_void_p
    assert kernel.GetProcessTimes.argtypes[0] is ctypes.c_void_p
    assert ctypes.sizeof(identity.FILETIME) == 8


@pytest.mark.parametrize("missing", ["provider", "signer", "certificate", "subject", "library"])
def test_missing_trust_material_is_never_an_allow_fallback(monkeypatch, missing):
    kernel = Kernel()
    bind(monkeypatch, kernel)
    trust, crypto = trust_apis(kernel)
    if missing == "provider":
        trust.WTHelperProvDataFromStateData = lambda *a: 0
    elif missing == "signer":
        trust.WTHelperGetProvSignerFromChain = lambda *a: 0
    elif missing == "certificate":
        trust.WTHelperGetProvCertFromChain = lambda *a: None
    elif missing == "subject":
        crypto.CertGetNameStringW = lambda *a: 0
    def load():
        if missing == "library":
            raise OSError(PRIVATE_PATH)
        return trust, crypto
    monkeypatch.setattr(identity, "_load_trust_apis", load)
    with pytest.raises(RuntimeError, match="identity") as error:
        ke._scan_memory_regions(901)
    assert kernel.reads == 0 and kernel.closed == [kernel.image_file, kernel.anchor]
    assert PRIVATE_PATH not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("fault", ["dos", "pe", "sections", "optional", "directory", "resource_size", "section_rva", "cycle", "leaf", "blob_bounds", "version_block", "version_key", "duplicate_version_field"])
def test_malformed_signed_resource_cannot_authorise_reads(monkeypatch, fault):
    kernel = Kernel()
    data = bytearray(kernel.file_bytes)
    if fault == "dos":
        data[:2] = b"XX"
    elif fault == "pe":
        data[0x80:0x84] = b"XX\0\0"
    elif fault == "sections":
        struct.pack_into("<H", data, 0x86, 97)
    elif fault == "optional":
        struct.pack_into("<H", data, 0x98, 0)
    elif fault == "directory":
        struct.pack_into("<I", data, 0x98 + 92, 2)
    elif fault == "resource_size":
        struct.pack_into("<I", data, 0x98 + 116, 0xffffffff)
    elif fault == "section_rva":
        struct.pack_into("<I", data, 0x98 + 224 + 12, 0x3000)
    elif fault == "cycle":
        struct.pack_into("<I", data, 0x200 + 20, 0x80000000)
    elif fault == "leaf":
        struct.pack_into("<I", data, 0x200 + 68, 0x80000050)
    elif fault == "blob_bounds":
        struct.pack_into("<I", data, 0x200 + 84, 0x100001)
    elif fault == "version_block":
        struct.pack_into("<H", data, 0x200 + 96, 0xffff)
    elif fault == "version_key":
        data[0x200 + 102:] = b"X" * (len(data) - 0x200 - 102)
    else:
        # A second ProductName must not override the signed OriginalFilename.
        needle = "OriginalFilename".encode("utf-16le")
        start = data.index(needle)
        data[start:start + len(needle)] = ("ProductName" + "\0" * 5).encode("utf-16le")
    kernel.file_bytes = bytes(data)
    rejected(monkeypatch, kernel)


@pytest.mark.parametrize("fault", ["size_query", "seek", "short_read", "read_failed"])
def test_image_handle_io_failure_never_falls_back_to_path(monkeypatch, fault):
    kernel = Kernel()
    if fault == "size_query":
        kernel.GetFileSizeEx = lambda *a: 0
    elif fault == "seek":
        kernel.SetFilePointerEx = lambda *a: 0
    elif fault == "short_read":
        def short(handle, target, size, count, overlapped):
            ctypes.cast(count, ctypes.POINTER(identity.DWORD)).contents.value = size - 1
            return 1
        kernel.ReadFile = short
    else:
        kernel.ReadFile = lambda *a: 0
    rejected(monkeypatch, kernel)


def test_cleanup_exception_still_attempts_all_closes_without_path_leak(monkeypatch):
    kernel = Kernel()
    bind(monkeypatch, kernel)
    def close(handle):
        kernel.closed.append(handle)
        raise OSError(PRIVATE_PATH)
    kernel.CloseHandle = close
    with pytest.raises(RuntimeError, match="cleanup") as error:
        ke._scan_memory_regions(901)
    assert kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]
    assert PRIVATE_PATH not in "".join(traceback.format_exception(error.value))


def test_trust_dlls_load_only_from_system32_and_use_explicit_abi(monkeypatch):
    libraries = {"wintrust.dll": SimpleNamespace(**{name: Mock() for name in ["WinVerifyTrust", "WTHelperProvDataFromStateData", "WTHelperGetProvSignerFromChain", "WTHelperGetProvCertFromChain"]}),
                 "crypt32.dll": SimpleNamespace(CertGetNameStringW=Mock())}
    seen = []
    def load(name, **kwargs):
        seen.append((name, kwargs))
        return libraries[name]
    monkeypatch.setattr(identity.c, "WinDLL", load, raising=False)
    trust, crypto = identity._load_trust_apis()
    assert seen == [(name, {"use_last_error": True, "winmode": 0x800}) for name in libraries]
    assert trust.WinVerifyTrust.restype is ctypes.c_int32
    assert trust.WTHelperProvDataFromStateData.restype is ctypes.c_void_p
    assert crypto.CertGetNameStringW.argtypes[0] is ctypes.c_void_p
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(identity.WINTRUST_FILE_INFO) == 32
        assert ctypes.sizeof(identity.WINTRUST_DATA) == 88


def test_non_bmp_unicode_path_uses_windows_utf16_lengths(monkeypatch):
    kernel = Kernel(r"C:\合成使用者😀\LINE\LINE.exe")
    kernel.trust_status = 0
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == [TOKEN]
    assert kernel.reads == 1


def test_image_query_length_pointer_matches_kernel_abi(monkeypatch):
    kernel = Kernel()
    callback_type = ctypes.CFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HANDLE,
                                    ctypes.wintypes.DWORD, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.wintypes.DWORD))
    def query(handle, flags, buffer, length):
        image = ctypes.create_unicode_buffer(TRUSTED_PATH)
        ctypes.memmove(buffer, image, ctypes.sizeof(image))
        length.contents.value = identity._utf16_length(TRUSTED_PATH)
        return 1
    kernel.QueryFullProcessImageNameW = callback_type(query)
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == [TOKEN]


def test_r2_previous_different_final_path_is_a_valid_signed_alias(monkeypatch):
    kernel = Kernel()
    kernel.final_path = PRIVATE_PATH
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == [TOKEN]
    assert kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]


@pytest.mark.parametrize("failed_handle", ["scan", "image_file", "anchor"])
def test_r2_false_close_reports_failure_and_attempts_all_handles(monkeypatch, failed_handle):
    kernel = Kernel()
    bind(monkeypatch, kernel)
    attempted = []
    def close(handle):
        attempted.append(handle)
        if handle == getattr(kernel, failed_handle):
            return 0
        kernel.closed.append(handle)
        return 1
    kernel.CloseHandle = close
    with pytest.raises(RuntimeError, match="cleanup") as error:
        ke._scan_memory_regions(901)
    assert attempted == [kernel.scan, kernel.image_file, kernel.anchor]
    assert getattr(kernel, failed_handle) not in kernel.closed
    assert PRIVATE_PATH not in "".join(traceback.format_exception(error.value))


def test_r2_early_failure_and_false_close_never_read_or_leak(monkeypatch):
    kernel = Kernel()
    kernel.image_ok = False
    bind(monkeypatch, kernel)
    attempted = []
    def close(handle):
        attempted.append(handle)
        return 0
    kernel.CloseHandle = close
    with pytest.raises(RuntimeError, match="identity") as error:
        ke._scan_memory_regions(901)
    assert kernel.reads == 0 and attempted == [kernel.anchor]
    assert PRIVATE_PATH not in "".join(traceback.format_exception(error.value))


def test_r2_raw_extended_unicode_image_path_is_passed_to_open(monkeypatch):
    path = "\\\\?\\C:\\合成😀\\" + "測" * 270 + "\\LINE.exe"
    kernel = Kernel(path)
    kernel.trust_status = 0
    kernel.final_path = path
    original = kernel.CreateFileW
    seen = []
    def open_file(value, *args):
        seen.append(value)
        return original(value, *args)
    kernel.CreateFileW = open_file
    bind(monkeypatch, kernel)
    assert ke._scan_memory_regions(901) == [TOKEN]
    assert seen == [path]
