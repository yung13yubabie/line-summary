"""One cooperative, path/key-free budget across an extraction call."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time
from readonly_errors import SnapshotUnavailable

DEFAULT_SECONDS=90.0
MAX_SCAN_BYTES=512*1024*1024
MAX_REGION_QUERIES=65536
MAX_CANDIDATES=4096
_CURRENT=ContextVar('line_extraction_budget',default=None)


class ExtractionStopped(SnapshotUnavailable):
    def __init__(self,reason,stage):
        self.reason=reason if reason in {'deadline','cancelled','cancel_check_failed','scan_bytes','region_queries','candidate_limit','probe_limit'} else 'deadline'
        self.stage=stage if stage in {'discovery','identity','memory_scan','candidate_probe','preflight','reader'} else 'reader'
        super().__init__('Key extraction stopped: '+self.reason)


class ExtractionBudget:
    def __init__(self,seconds=DEFAULT_SECONDS,cancelled=None):
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0:
            raise ValueError('Extraction timeout must be a positive finite number')
        if cancelled is not None and not callable(cancelled):raise ValueError('Cancellation check must be callable')
        self.deadline=time.monotonic()+seconds
        self.cancelled=cancelled
        self.bytes_requested=0
        self.region_queries=0
        self.probes=0

    def check(self,stage,reserve=0):
        if self.cancelled is not None:
            try:cancelled=self.cancelled()
            except Exception:raise ExtractionStopped('cancel_check_failed',stage) from None
            if cancelled:raise ExtractionStopped('cancelled',stage) from None
        if time.monotonic()>=self.deadline-reserve:raise ExtractionStopped('deadline',stage) from None

    def query(self):
        self.check('memory_scan')
        if self.region_queries>=MAX_REGION_QUERIES:raise ExtractionStopped('region_queries','memory_scan') from None
        self.region_queries+=1

    def read(self,size):
        self.check('memory_scan')
        if self.bytes_requested+size>MAX_SCAN_BYTES:raise ExtractionStopped('scan_bytes','memory_scan') from None
        self.bytes_requested+=size

    def candidates(self,count):
        self.check('memory_scan')
        if count>MAX_CANDIDATES:raise ExtractionStopped('candidate_limit','memory_scan') from None

    def probe(self):
        self.check('candidate_probe')
        self.probes+=1


def current_budget():return _CURRENT.get()


@contextmanager
def extraction_scope(budget):
    token=_CURRENT.set(budget)
    try:yield budget
    finally:_CURRENT.reset(token)
