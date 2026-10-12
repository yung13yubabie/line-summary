"""Candidate enumeration is heuristic; only a real synthetic DB probe verifies."""
import ctypes
from types import SimpleNamespace
from dataclasses import dataclass
import json
from pathlib import Path
import uuid

import apsw
import pytest
import db_reader as db
import key_extractor as ke

ROOT=Path(__file__).resolve().parents[2]
KEY32='aB0123456789CdEf'*2
KEY64=KEY32+KEY32.swapcase()
OBS=[]
BARRIERS=['scan_start','guard','uncommitted','failed','zero_read','short','address_gap','oversize']

@dataclass
class Segment:
    data: bytes
    state: int=0x1000
    protect: int=4
    kind: int=0x20000
    ok: bool=True
    count: int|None=None
    base_delta: int=0

class SyntheticMemory:
    def __init__(self,segments):
        self.segments=segments
        self.index=-1
        self.reads=[]
        self.closed=[]
    def OpenProcess(self,*args):
        return 713
    def QueryFullProcessImageNameW(self,handle,flags,target,length):
        target.value='LINE.exe'
        return 1
    def VirtualQueryEx(self,handle,address,out,size):
        self.index+=1
        if self.index==len(self.segments):
            return 0
        item=self.segments[self.index]
        info=ctypes.cast(out,ctypes.POINTER(ke.MEMORY_BASIC_INFORMATION)).contents
        info.BaseAddress=(address.value or 0)+item.base_delta
        info.RegionSize=len(item.data)
        info.State,info.Protect,info.Type=item.state,item.protect,item.kind
        return size
    def ReadProcessMemory(self,handle,address,out,size,read):
        item=self.segments[self.index]
        self.reads.append(self.index)
        count=len(item.data) if item.count is None else item.count
        assert 0<=count<=size
        ctypes.memmove(out,item.data,count)
        ctypes.cast(read,ctypes.POINTER(ctypes.c_size_t)).contents.value=count
        return int(item.ok)
    def CloseHandle(self,handle):
        self.closed.append(handle)

def interrupted_segments(monkeypatch,payload,encoding,barrier):
    last=Segment(payload)
    prefix=Segment((' '+KEY32[:16]).encode(encoding))
    blocked=Segment('Q'.encode(encoding))
    segments=[prefix,blocked,last]
    if barrier=='scan_start':
        segments=[last]
    elif barrier=='guard':
        blocked.protect=4|ke.PAGE_GUARD
    elif barrier=='uncommitted':
        blocked.state=0x2000
    elif barrier=='failed':
        blocked.ok=False
    elif barrier=='zero_read':
        blocked.count=0
    elif barrier=='short':
        prefix.count=len(prefix.data)-1
        segments=[prefix,last]
    elif barrier=='address_gap':
        last.base_delta=4096
        segments=[prefix,last]
    elif barrier=='oversize':
        monkeypatch.setattr(ke,'_MAX_REGION_BYTES',512)
        segments=[prefix,Segment(b'Q'*513),last]
    return segments

def encrypted_database(tmp_path,key):
    path=tmp_path/'synthetic-contract.edb'
    writer=apsw.Connection(str(path))
    try:
        writer.execute("PRAGMA cipher='aes128cbc'")
        writer.execute("PRAGMA key='"+key+"'")
        writer.execute('CREATE TABLE artificial_schema(value INTEGER)')
    finally:
        writer.close()
    return path

def recover(monkeypatch,segments,path):
    memory=SyntheticMemory(segments)
    monkeypatch.setattr(ke,'_load_kernel32',lambda:memory)
    # Candidate-only unit scope; full identity/scan/probe is covered separately.
    monkeypatch.setattr(ke,'open_verified_line_process',lambda k,p: SimpleNamespace(
        handle=713,close=lambda:memory.CloseHandle(713)))
    monkeypatch.setattr(ke,'find_line_pid',lambda:713)
    original_scan=ke._scan_memory_regions
    original_probe=db.probe_key
    evidence={'candidates':[],'attempts':[]}
    def record_scan(pid):
        # Preserve the actual scanner, chunk helper, regex, ordering and dedupe.
        evidence['candidates']=original_scan(pid)
        return evidence['candidates']
    def record_probe(candidate_path,candidate):
        # Preserve actual encrypted opening and schema validation, not a mock.
        verified=original_probe(candidate_path,candidate)
        evidence['attempts'].append((candidate,verified))
        return verified
    monkeypatch.setattr(ke,'_scan_memory_regions',record_scan)
    monkeypatch.setattr(db,'probe_key',record_probe)
    try:
        result=ke.extract_key(str(path),require_consent=False)
    except RuntimeError:
        result=None
    assert memory.closed==[713]
    assert len(evidence['candidates'])==len(set(evidence['candidates']))
    return result,evidence,memory

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
@pytest.mark.parametrize('tail_length',[32,64])
@pytest.mark.parametrize('barrier',BARRIERS)
def test_unknown_left_candidate_is_rejected_by_actual_database(monkeypatch,tmp_path,encoding,tail_length,barrier):
    unverified=KEY64[:tail_length].swapcase()
    valid=KEY32.lower()
    data=(unverified+' '+valid+' ').encode(encoding)
    segments=interrupted_segments(monkeypatch,data,encoding,barrier)
    path=encrypted_database(tmp_path,valid)
    result,evidence,memory=recover(monkeypatch,segments,path)
    assert evidence['candidates']==[unverified,valid]
    assert evidence['attempts']==[(unverified,False),(valid,True)]
    assert result==valid
    if barrier in ('guard','uncommitted','oversize'):
        assert 1 not in memory.reads
    OBS.append({'case':'unknown_left_verification','encoding':encoding,'length':tail_length,'barrier':barrier,
        'candidate_count':len(evidence['candidates']),'probe_count':len(evidence['attempts']),
        'unverified_rejected':evidence['attempts'][0][1] is False,'correct_key_returned':result==valid})

@pytest.mark.parametrize('encoding,alignment',[('ascii',0),('utf-16le',0),('utf-16le',1)])
@pytest.mark.parametrize('length',[32,64])
@pytest.mark.parametrize('barrier',BARRIERS)
def test_legal_key_at_region_start_remains_supported(monkeypatch,tmp_path,encoding,alignment,length,barrier):
    valid=KEY64[:length]
    decoy=KEY32.lower()
    data=b'!'*alignment+(valid+' '+decoy+' ').encode(encoding)
    segments=interrupted_segments(monkeypatch,data,encoding,barrier)
    path=encrypted_database(tmp_path,valid)
    result,evidence,_=recover(monkeypatch,segments,path)
    assert evidence['candidates']==[valid,decoy]
    assert evidence['attempts']==[(valid,True)]
    assert result==valid
    OBS.append({'case':'legal_region_start','encoding':encoding,'alignment':alignment,'length':length,
        'barrier':barrier,'candidate_count':len(evidence['candidates']),'probe_count':1,'correct_key_returned':True})

CUTS=[('ascii',x) for x in (1,16,31,32,33,63)]+[('utf-16le',x) for x in (1,2,31,32,63,64,65,126,127)]

@pytest.mark.parametrize('encoding,cut',CUTS)
def test_cross_region_64_is_never_probed_as_two_32_fragments(monkeypatch,tmp_path,encoding,cut):
    data=(' '+KEY64+' ').encode(encoding)
    path=encrypted_database(tmp_path,KEY64)
    result,evidence,_=recover(monkeypatch,[Segment(data[:cut]),Segment(data[cut:])],path)
    assert evidence['candidates']==[KEY64]
    assert evidence['attempts']==[(KEY64,True)]
    assert result==KEY64

@pytest.mark.parametrize('encoding',['ascii','utf-16le'])
@pytest.mark.parametrize('length',[32,64])
def test_candidate_format_alone_cannot_become_verified_key(monkeypatch,tmp_path,encoding,length):
    decoy=KEY64[:length].swapcase()
    valid=KEY32.lower()
    path=encrypted_database(tmp_path,valid)
    result,evidence,_=recover(monkeypatch,[Segment((decoy+' ').encode(encoding))],path)
    assert evidence['candidates']==[decoy]
    assert evidence['attempts']==[(decoy,False)]
    assert result is None

@pytest.fixture(scope='session',autouse=True)
def save_safe_observations():
    yield
    target=ROOT/'contract_observations.json'
    if target.exists():
        target=ROOT/('contract_observations_'+uuid.uuid4().hex[:8]+'.json')
    with target.open('x',encoding='utf-8') as file:
        json.dump(OBS,file,indent=2)
