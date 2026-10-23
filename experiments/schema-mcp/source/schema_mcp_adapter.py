"""One locally bound aggregate. No remote source/configuration or chat data."""
import threading
import time
from pathlib import Path
import uuid
import anyio
from typing import Literal
from typing_extensions import NotRequired, TypedDict
from configured_entry_gate import _local_file

STATUSES = frozenset({'completed','disabled','busy','cancelled','deadline_exceeded',
    'cleanup_unconfirmed','failed','output_rejected','receipt_failed','spawn_error',
    'output_limit','supervisor_error','output_or_receipt_rejected','protocol_error',
    'spawn_cleanup_unconfirmed'})
Status = Literal['completed','disabled','busy','cancelled','deadline_exceeded',
    'cleanup_unconfirmed','failed','output_rejected','receipt_failed','spawn_error',
    'output_limit','supervisor_error','output_or_receipt_rejected','protocol_error',
    'spawn_cleanup_unconfirmed']

class Counts(TypedDict):
    table_count: int
    view_count: int

class SchemaResult(TypedDict):
    status: Status
    successful: bool
    application_ack: bool
    os_release_confirmed: bool
    retry_required: bool
    counts: NotRequired[Counts]

def _failure(status, released=True):
    return {'status':status,'successful':False,'application_ack':False,
        'os_release_confirmed':released,'retry_required':not released}

def _public(raw, cancelled):
    flags=('successful','application_ack','os_release_confirmed','retry_required')
    if (type(raw) is not dict or type(raw.get('status')) is not str
            or raw['status'] not in STATUSES or raw['status'] in {'disabled','busy'}
            or any(type(raw.get(name)) is not bool for name in flags)
            or raw['retry_required'] is raw['os_release_confirmed']):
        return _failure('output_rejected',False)
    if cancelled:
        return _failure('cancelled' if raw['os_release_confirmed'] else 'cleanup_unconfirmed',
            raw['os_release_confirmed'])
    if raw['successful']:
        counts=raw.get('counts')
        if (raw['status']!='completed' or not raw['application_ack']
                or not raw['os_release_confirmed'] or raw['retry_required']
                or raw.get('forced_termination') is not False
                or raw.get('assigned_before_resume') is not True or raw.get('start_sent') is not True
                or type(counts) is not dict or set(counts)!={'table_count','view_count'}
                or any(type(value) is not int or not 0<=value<2**63 for value in counts.values())):
            return _failure('output_rejected',raw['os_release_confirmed'])
        return {name:raw[name] for name in ('status',*flags)}|{'counts':dict(counts)}
    if (raw['status']=='completed' or 'counts' in raw
            or ((raw.get('forced_termination') is True or raw['status']=='cleanup_unconfirmed')
                and raw['application_ack'])):
        return _failure('output_rejected',raw['os_release_confirmed'])
    return {name:raw[name] for name in ('status',*flags)}

class _Operation:
    def __init__(self):
        self.cancel=threading.Event()
        self.done=threading.Event()
        self.thread=None
        self.raw=None
        self.directory=None
        self.owner=None
        self.error=None
        self.primary_error=None
        self.start_error=None
        self.cleanup_error=None

class SchemaService:
    def __init__(self, *, launcher=None):
        self._launch=launcher
        self._lock=threading.RLock()
        self._binding=None
        self._operation=None
        self._stopping=False
        self._last=_failure('disabled')
        self._cancel_signalled=False
        self._recovered=False

    def configure(self, settings_path, output_root):
        # Pure lexical validation: no settings read or output acquisition here.
        root=str(output_root)
        if (not _local_file(settings_path) or not _local_file(root)
                or len(settings_path.encode('utf-8'))>3000):
            raise ValueError('explicit bounded local schema binding required')
        with self._lock:
            if self._binding is not None or self._operation is not None or self._stopping:
                raise ValueError('schema binding is fixed for this server')
            self._binding=(settings_path,root)

    def _worker(self, operation, binding):
        try:
            if operation.cancel.is_set():
                operation.raw=_failure('cancelled')
            else:
                settings,root=binding
                Path(root).mkdir(parents=True,exist_ok=True)
                launch=self._launch
                if launch is None:
                    from configured_launcher import run
                    launch=run
                operation.directory=Path(root)/('schema_'+uuid.uuid4().hex)
                operation.raw=launch(operation.directory,settings,
                    cancel_event=operation.cancel)
        except BaseException as error:
            # A failed native owner may still be retained by the launcher.
            # Do not infer release from the exception or expose its arguments.
            operation.raw=_failure('failed',False)
            operation.error=error
        finally:
            self._associate_owner(operation)
            binding=None
            operation.done.set()

    def _associate_owner(self, operation):
        # Retain the actual launcher object, not a PID, inferred token or path.
        # Later exact local recovery may retire it without mutating this result.
        try:
            import configured_launcher as launcher
            owner=getattr(operation.error,'launcher_owner',None)
            if owner is None and type(operation.raw) is dict:
                token=operation.raw.get('outer_run_id')
                if type(token) is str:
                    with launcher._REGISTRY_LOCK:owner=launcher.RETAINED.get(token)
            if (isinstance(owner,launcher._Owner)
                    and owner.directory==operation.directory):operation.owner=owner
        except BaseException:
            pass  # An unproved association cannot certify cleanup.

    def _finished(self, operation):
        return operation.done.is_set() and (operation.thread is None or not operation.thread.is_alive())

    def _collect(self, operation):
        if not self._finished(operation):return None
        if operation.thread is not None:operation.thread.join(timeout=0)
        result=_public(operation.raw,operation.cancel.is_set())
        self._last=result
        self._cancel_signalled=self._cancel_signalled or operation.cancel.is_set()
        recovered=(operation.owner is not None and operation.owner.cleaned is True)
        if recovered and not result['os_release_confirmed']:self._recovered=True
        if (result['os_release_confirmed'] or recovered) and self._operation is operation:
            self._operation=None
        return result

    async def _wait_cleanup(self, operation):
        # This joins only the already registered exact worker. It never renews
        # the launcher's own work/cleanup deadlines or replaces its owner.
        deadline=time.monotonic()+3.05
        with anyio.CancelScope(shield=True):
            while not self._finished(operation) and time.monotonic()<deadline:
                await anyio.sleep(.005)
        with self._lock:
            result=self._collect(operation)
            if result is None:self._last=_failure('cleanup_unconfirmed',False)
            return result

    async def execute(self) -> SchemaResult:
        with self._lock:
            if self._operation is not None:self._collect(self._operation)
            if self._operation is not None:return _failure('busy')
            if self._stopping or self._binding is None:return _failure('disabled')
            operation=_Operation()
            self._operation=operation  # Registration precedes fallible start.
            operation.thread=threading.Thread(target=self._worker,args=(operation,self._binding),
                name='schema-mcp-owned-call',daemon=True)
            try:operation.thread.start()
            except BaseException as error:
                # Thread.start may be interrupted after native creation. Keep
                # this exact object and wait; absence of done is not release.
                operation.start_error=error
                operation.cancel.set()
        try:
            if operation.start_error is not None:
                result=await self._wait_cleanup(operation)
                return _failure('supervisor_error',True) if result is not None and result['os_release_confirmed'] else _failure('cleanup_unconfirmed',False)
            while not self._finished(operation):await anyio.sleep(.005)
        except BaseException as primary:
            operation.primary_error=primary
            try:
                operation.cancel.set()
                await self._wait_cleanup(operation)
            except BaseException as cleanup_error:
                operation.cleanup_error=cleanup_error
            raise
        with self._lock:
            return self._collect(operation)

    async def close(self):
        with self._lock:
            self._stopping=True
            operation=self._operation
            if operation is not None:operation.cancel.set()
        if operation is not None:await self._wait_cleanup(operation)
        with self._lock:
            self._binding=None
            return self._operation is None

    def observation(self):
        with self._lock:
            operation=self._operation
            if operation is not None:self._collect(operation)
            operation=self._operation
            return {'configured':self._binding is not None,'stopping':self._stopping,
                'active_operations':int(operation is not None and not self._finished(operation)),
                'pending_cleanup':operation is not None,
                'last_status':self._last['status'],
                'last_application_ack':self._last['application_ack'],
                'last_os_release_confirmed':self._last['os_release_confirmed'],
                'recovery_release_confirmed':self._recovered,
                'cancel_signalled':self._cancel_signalled or (operation is not None and operation.cancel.is_set())}
