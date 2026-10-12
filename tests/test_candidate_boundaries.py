"""Bounded streaming candidates across mocked memory boundaries only."""
import ctypes
from types import SimpleNamespace

import pytest
import key_extractor as ke

TOKEN = 'aBcD0123456789EFaBcD0123456789EF'
assert len(TOKEN) == 32

class BoundaryMemory:
    def __init__(self, chunks, states=None, gaps=None, short=None, failed=None):
        self.chunks = chunks
        self.index = -1
        self.states, self.gaps = states or {}, gaps or {}
        self.short, self.failed = short or {}, failed or set()
        self.read_indices = []
        self.closed = []
    def OpenProcess(self,*args):
        return 101
    def QueryFullProcessImageNameW(self,handle,flags,target,size):
        target.value = 'LINE.exe'
        return 1
    def VirtualQueryEx(self,handle,address,pointer,size):
        self.index += 1
        if self.index >= len(self.chunks):
            return 0
        region = ctypes.cast(pointer,ctypes.POINTER(ke.MEMORY_BASIC_INFORMATION)).contents
        region.BaseAddress = (address.value or 0) + self.gaps.get(self.index,0)
        region.RegionSize = len(self.chunks[self.index])
        region.State, region.Type, region.Protect = self.states.get(self.index,(ke.MEM_COMMIT,ke.MEM_PRIVATE,4 if self.index%2==0 else 2))
        return size
    def ReadProcessMemory(self,handle,address,target,size,n):
        self.read_indices.append(self.index)
        if self.index in self.failed:
            return 0
        chunk = self.chunks[self.index][:self.short.get(self.index,size)]
        ctypes.memmove(target,chunk,len(chunk))
        ctypes.cast(n,ctypes.POINTER(ctypes.c_size_t)).contents.value = len(chunk)
        return 1
    def CloseHandle(self,handle):
        self.closed.append(handle)

def scan(monkeypatch,chunks,**kwargs):
    kernel = BoundaryMemory(chunks,**kwargs)
    monkeypatch.setattr(ke,'_load_kernel32',lambda:kernel)
    # Candidate-only unit scope; full identity/scan/probe is covered separately.
    monkeypatch.setattr(ke,'open_verified_line_process',lambda k,p: SimpleNamespace(
        handle=101,close=lambda:kernel.CloseHandle(101)))
    result = ke._scan_memory_regions(101)
    assert kernel.closed == [101]
    return result,kernel

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
@pytest.mark.parametrize('length,split',[(32,1),(32,16),(32,31),(64,1),(64,15),(64,31),(64,32),(64,33),(64,63)])
def test_split_token_is_emitted_whole_once(monkeypatch,encoding,length,split):
    candidate = (TOKEN+TOKEN.lower())[:length]
    width = len(' '.encode(encoding))
    first = b' '*(4096-split*width)+candidate[:split].encode(encoding)
    second = (candidate[split:]+' ').encode(encoding)
    result,_ = scan(monkeypatch,[first,second])
    assert result == [candidate]

@pytest.mark.parametrize('split',[1,63,65,127])
def test_utf16_code_unit_may_cross_byte_boundary(monkeypatch,split):
    candidate = TOKEN+TOKEN.lower()
    encoded = candidate.encode('utf-16le')
    result,_ = scan(monkeypatch,[b'\x00 '+encoded[:split],encoded[split:]+b' \x00'])
    assert result == [candidate]

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
@pytest.mark.parametrize('length',[33,65,96,160])
def test_overlong_token_cannot_become_suffix_candidates(monkeypatch,encoding,length):
    candidate = (TOKEN*6)[:length]
    chunks = [(' '+candidate[:32]).encode(encoding)]
    chunks.extend(candidate[i:i+16].encode(encoding) for i in range(32,length,16))
    chunks.append(' '.encode(encoding))
    assert scan(monkeypatch,chunks)[0] == []

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
def test_deferred_32_requires_real_right_boundary(monkeypatch,encoding):
    first = (' '+TOKEN).encode(encoding)
    assert scan(monkeypatch,[first,' '.encode(encoding)])[0] == [TOKEN]
    assert scan(monkeypatch,[first,'Q '.encode(encoding)])[0] == []
    assert scan(monkeypatch,[first,TOKEN.lower().encode(encoding),' '.encode(encoding)])[0] == [TOKEN+TOKEN.lower()]

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
def test_unknown_scan_end_does_not_finalize_partial_candidate(monkeypatch,encoding):
    assert scan(monkeypatch,[(' '+TOKEN).encode(encoding)])[0] == []

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
@pytest.mark.parametrize('barrier',['noaccess','guard','uncommitted','nonprivate','failed_read','short_read','nonadjacent','oversize'])
def test_carry_is_cleared_at_unknown_boundaries(monkeypatch,encoding,barrier):
    first = (' '+TOKEN).encode(encoding)
    second = TOKEN.lower().encode(encoding)
    # Neither fragment has a confirmed right boundary before the barrier.
    states = {}
    kwargs = {}
    chunks = [first,b'??',second]
    if barrier=='noaccess':
        states[1] = (ke.MEM_COMMIT,ke.MEM_PRIVATE,ke.PAGE_NOACCESS)
    elif barrier=='guard':
        states[1] = (ke.MEM_COMMIT,ke.MEM_PRIVATE,4|ke.PAGE_GUARD)
    elif barrier=='uncommitted':
        states[1] = (0x2000,ke.MEM_PRIVATE,4)
    elif barrier=='nonprivate':
        states[1] = (ke.MEM_COMMIT,0x40000,4)
    elif barrier=='failed_read':
        kwargs['failed'] = {1}
    elif barrier=='short_read':
        chunks = [first+b'Q\x00',second]
        kwargs['short'] = {0:len(first)}
    elif barrier=='nonadjacent':
        chunks = [first,second]
        kwargs['gaps'] = {1:4096}
    elif barrier=='oversize':
        monkeypatch.setattr(ke,'_MAX_REGION_BYTES',len(first))
        chunks = [first,b'?'*(len(first)+1),second]
    result,kernel = scan(monkeypatch,chunks,states=states,**kwargs)
    assert result == []
    if barrier in {'noaccess','guard','uncommitted','nonprivate','oversize'}:
        assert 1 not in kernel.read_indices

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
def test_boundary_overlap_does_not_duplicate_frequency(monkeypatch,encoding):
    chunks = [(' '+TOKEN+' ').encode(encoding),(' '+TOKEN.lower()).encode(encoding),(' '+TOKEN.lower()+' ').encode(encoding)]
    result,_ = scan(monkeypatch,chunks)
    assert result == [TOKEN.lower(),TOKEN]

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
def test_hundreds_of_adjacent_regions_do_not_resurrect_long_token(monkeypatch,encoding):
    chunks = [' '.encode(encoding)]+[TOKEN.encode(encoding)]*300+[' '.encode(encoding)]
    assert scan(monkeypatch,chunks)[0] == []
