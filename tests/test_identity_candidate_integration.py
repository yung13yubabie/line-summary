"""Real identity policy + streaming candidates + synthetic encrypted DB probes.

Only Win32 API responses and discovery are fabricated. The retained-handle
helper, PE parser, scanner, chunk helper, preflight and DB verification execute.
"""
import ctypes
import traceback

import apsw
import pytest
from tests.native_platform import WINDOWS_NATIVE
import db_reader as db
import key_extractor as ke
from tests.test_process_identity import Kernel, bind, signed_pe

pytestmark = WINDOWS_NATIVE

VALID32 = 'aB0123456789CdEf' * 2
VALID64 = VALID32 + VALID32.swapcase()
PRIVATE = 'SYNTHETIC_INTEGRATION_PRIVATE_MARKER'


class StreamKernel(Kernel):
    def __init__(self, chunks):
        super().__init__()
        self.regions = {0: (2**40, None)}
        address = 2**40
        for chunk in chunks:
            self.regions[address] = (len(chunk), chunk)
            address += len(chunk)
        self.memory_addresses = []
        self.close_attempts = []
        self.close_failure = None
        self.read_failure_index = None

    def VirtualQueryEx(self, handle, address, target, size):
        assert handle == self.scan and not self.closed
        address = address.value or 0
        if address not in self.regions:
            return 0
        length, chunk = self.regions[address]
        region = ctypes.cast(target, ctypes.POINTER(ke.MEMORY_BASIC_INFORMATION)).contents
        region.BaseAddress, region.RegionSize = address, length
        region.State = ke.MEM_COMMIT if chunk is not None else 0x10000
        region.Type = ke.MEM_PRIVATE if chunk is not None else 0
        region.Protect = 4 if chunk is not None else ke.PAGE_NOACCESS
        return size

    def ReadProcessMemory(self, handle, address, target, size, count):
        assert handle == self.scan and not self.closed
        assert ('trust_verify',) in self.events and ('trust_close',) in self.events
        if len(self.memory_addresses) == self.read_failure_index:
            raise OSError(PRIVATE)
        length, chunk = self.regions[address.value]
        assert chunk is not None and length == size
        self.memory_addresses.append(address.value)
        self.reads += 1
        ctypes.memmove(target, chunk, length)
        ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = length
        return 1

    def CloseHandle(self, handle):
        self.close_attempts.append(handle)
        if handle == self.close_failure:
            return 0
        self.closed.append(handle)
        return 1


def make_db(tmp_path, key):
    path = tmp_path / 'integration-artificial.edb'
    writer = apsw.Connection(str(path))
    try:
        writer.execute("PRAGMA cipher='aes128cbc'")
        writer.execute("PRAGMA key='" + key + "'")
        writer.execute('CREATE TABLE artificial_metadata(value INTEGER)')
    finally:
        writer.close()
    return path


def configure(monkeypatch, kernel):
    bind(monkeypatch, kernel)
    monkeypatch.setattr(ke, 'find_line_pid', lambda: 901)
    real_probe = db.probe_key
    attempts = []
    def probe(path, candidate):
        # All process/image handles must already be closed before DB probing.
        assert kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]
        accepted = real_probe(path, candidate)
        attempts.append((candidate, accepted))
        return accepted
    monkeypatch.setattr(db, 'probe_key', probe)
    return attempts


@pytest.mark.parametrize('encoding,alignment', [('ascii', 0), ('utf-16le', 0), ('utf-16le', 1)])
@pytest.mark.parametrize('length', [32, 64])
def test_verified_stream_preserves_candidates_until_actual_db_probe(monkeypatch, tmp_path, encoding, alignment, length):
    valid, wrong = VALID64[:length], VALID64[:length].lower()
    assert wrong != valid
    # Unknown left edge is retained; wrong candidate is frequent and is tried
    # once. Small odd chunks split code units and the complete 32/64 token.
    data = b'!' * alignment + (wrong + ' ' + wrong + ' ' + wrong + ' ' + valid + ' ').encode(encoding)
    kernel = StreamKernel([data[i:i + 17] for i in range(0, len(data), 17)])
    attempts = configure(monkeypatch, kernel)
    result = ke.extract_key(str(make_db(tmp_path, valid)), require_consent=False)
    assert result == valid
    assert attempts == [(wrong, False), (valid, True)]
    assert len(kernel.memory_addresses) > 2 and min(kernel.memory_addresses) >= 2**40
    assert kernel.close_attempts == [kernel.scan, kernel.image_file, kernel.anchor]
    assert len([event for event in kernel.events if event[0] == 'open']) == 2


@pytest.mark.parametrize('failure', ['signature', 'publisher', 'product', 'pid_reuse'])
def test_failed_identity_never_reaches_stream_or_candidate_probe(monkeypatch, tmp_path, failure):
    data = (' ' + VALID64 + ' ').encode('utf-16le')
    kernel = StreamKernel([data[:65], data[65:]])
    if failure == 'signature':
        kernel.trust_status = 0x80096010
    elif failure == 'publisher':
        kernel.publisher = 'Unrelated Synthetic Corporation'
    elif failure == 'product':
        kernel.file_bytes = signed_pe(product='Other')
    else:
        kernel.before_scan = lambda: kernel.states[kernel.scan].update(pid=902)
    attempts = configure(monkeypatch, kernel)
    with pytest.raises(RuntimeError, match='memory inspection failed') as error:
        ke.extract_key(str(make_db(tmp_path, VALID64)), require_consent=False)
    assert kernel.reads == 0 and attempts == []
    expected = [kernel.scan, kernel.image_file, kernel.anchor] if failure == 'pid_reuse' else [kernel.image_file, kernel.anchor]
    assert kernel.close_attempts == kernel.closed == expected
    assert PRIVATE not in ''.join(traceback.format_exception(error.value))


@pytest.mark.parametrize('failed_handle', ['scan', 'image_file', 'anchor'])
def test_cleanup_failure_prevents_probing_completed_candidates(monkeypatch, tmp_path, failed_handle):
    data = (' ' + VALID64 + ' ').encode('ascii')
    kernel = StreamKernel([data[:33], data[33:]])
    kernel.close_failure = getattr(kernel, failed_handle)
    attempts = configure(monkeypatch, kernel)
    with pytest.raises(RuntimeError, match='memory inspection failed') as error:
        ke.extract_key(str(make_db(tmp_path, VALID64)), require_consent=False)
    assert kernel.reads == 2 and attempts == []
    assert kernel.close_attempts == [kernel.scan, kernel.image_file, kernel.anchor]
    assert kernel.close_failure not in kernel.closed
    assert PRIVATE not in ''.join(traceback.format_exception(error.value))


def test_midstream_exception_closes_retained_handles_without_probing(monkeypatch, tmp_path):
    data = (' ' + VALID64 + ' ').encode('utf-16le')
    kernel = StreamKernel([data[:65], data[65:]])
    kernel.read_failure_index = 1
    attempts = configure(monkeypatch, kernel)
    with pytest.raises(RuntimeError, match='memory inspection failed') as error:
        ke.extract_key(str(make_db(tmp_path, VALID64)), require_consent=False)
    assert kernel.reads == 1 and attempts == []
    assert kernel.close_attempts == kernel.closed == [kernel.scan, kernel.image_file, kernel.anchor]
    assert PRIVATE not in ''.join(traceback.format_exception(error.value))
