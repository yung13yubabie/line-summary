"""Windows Job operations for the fixed configured product launcher."""
import ctypes as C
from ctypes import wintypes as W
import os

FLAGS = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS_LIMIT; no breakaway.


class BasicLimit(C.Structure):
    _fields_ = [('process_time', C.c_longlong), ('job_time', C.c_longlong), ('flags', W.DWORD),
        ('min_ws', C.c_size_t), ('max_ws', C.c_size_t), ('active_limit', W.DWORD),
        ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]


class IOCounts(C.Structure):
    _fields_ = [(n, C.c_ulonglong) for n in ('ro', 'wo', 'oo', 'rb', 'wb', 'ob')]


class ExtendedLimit(C.Structure):
    _fields_ = [('basic', BasicLimit), ('io', IOCounts), ('process_memory', C.c_size_t),
        ('job_memory', C.c_size_t), ('peak_process_memory', C.c_size_t), ('peak_job_memory', C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [(n, C.c_longlong) for n in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (n, W.DWORD) for n in ('faults', 'total', 'active', 'terminated')]


def _kernel():
    kernel = C.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    for name, args, result in [
        ('CreateJobObjectW', [C.c_void_p, W.LPCWSTR], W.HANDLE),
        ('SetInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        ('QueryInformationJobObject', [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        ('AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL),
        ('IsProcessInJob', [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
        ('TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL),
        ('CloseHandle', [W.HANDLE], W.BOOL),
        ('GetCurrentProcess', [], W.HANDLE),
        ('PeekNamedPipe', [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _check(value):
    if not value:
        raise OSError('configured native operation unavailable')
    return value


def _policy(kernel, handle):
    limits = ExtendedLimit()
    _check(kernel.QueryInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits), None))
    return {'kill_on_close': limits.basic.flags == FLAGS,
            'active_process_limit': int(limits.basic.active_limit)}


def verify_inherited_job(handle):
    kernel = _kernel()
    try:
        policy = _policy(kernel, handle)
        member = W.BOOL()
        _check(kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle, C.byref(member)))
        if not member.value or policy != {'kill_on_close': True, 'active_process_limit': 8}:
            raise OSError('exact configured Job rejected')
    finally:
        _check(kernel.CloseHandle(handle))


class Job:
    def __init__(self):
        self.kernel = _kernel()
        self.handle = self.inherited_handle = None

    def open(self):
        self.handle = _check(self.kernel.CreateJobObjectW(None, None))

    def configure(self):
        limits = ExtendedLimit()
        limits.basic.flags, limits.basic.active_limit = FLAGS, 8
        _check(self.kernel.SetInformationJobObject(self.handle, 9, C.byref(limits), C.sizeof(limits)))

    def policy(self):
        return _policy(self.kernel, self.handle)

    def inherit(self):
        import _winapi as api
        current = api.GetCurrentProcess()
        # Child receives JOB_OBJECT_QUERY only, not terminate/set-policy access.
        self.inherited_handle = api.DuplicateHandle(current, self.handle, current, 4, True, 0)
        return self.inherited_handle

    def close_inherited(self):
        if self.inherited_handle is not None:
            _check(self.kernel.CloseHandle(self.inherited_handle))
            self.inherited_handle = None

    def assign(self, child):
        _check(self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)))

    def member(self, child):
        value = W.BOOL()
        _check(self.kernel.IsProcessInJob(int(child._handle), self.handle, C.byref(value)))
        return bool(value.value)

    def active(self):
        if self.handle is None:
            return 0
        value = Accounting()
        _check(self.kernel.QueryInformationJobObject(self.handle, 1, C.byref(value), C.sizeof(value), None))
        return int(value.active)

    def terminate(self):
        _check(self.kernel.TerminateJobObject(self.handle, 125))

    def close(self):
        self.close_inherited()
        if self.handle is not None:
            _check(self.kernel.CloseHandle(self.handle))
            self.handle = None


class Backend:
    def __init__(self):
        self.kernel = _kernel()

    def new_job(self, register):
        job = Job()
        register(job)
        job.open()
        return job

    def spawn(self, argv, inherited, register):
        from configured_suspended import create
        return create(argv, inherited, register)

    def read_available(self, child, label, limit):
        import msvcrt
        stream = getattr(child, label)
        if stream is None or stream.closed:
            return b''
        available = W.DWORD()
        if not self.kernel.PeekNamedPipe(msvcrt.get_osfhandle(stream.fileno()), None, 0, None, C.byref(available), None):
            if C.get_last_error() in (109, 232):
                return b''
            raise OSError('configured pipe unavailable')
        return stream.read(min(4096, limit, available.value)) if available.value else b''

    def close_startup(self, child):
        import _winapi as api
        for handle in list(child.transient_handles):
            api.CloseHandle(handle)
            child.transient_handles.remove(handle)
        for fd in list(child.transient_fds):
            os.close(fd)
            child.transient_fds.remove(fd)
        return not (child.transient_handles or child.transient_fds)
