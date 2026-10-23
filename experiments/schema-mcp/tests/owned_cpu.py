"""TEST ONLY CPU totals from existing exact owned handles; no PID lookup."""
import ctypes as C


class Accounting(C.Structure):
    _fields_ = [(name, C.c_longlong) for name in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (name, C.c_uint32) for name in ('faults', 'total', 'active', 'terminated')]


def sample_owner(child, job):
    handle = child._handle
    if handle is None or handle.closed or job.handle is None:
        raise ValueError('Exact retained owner required')
    api = job.kernel
    get_times = api.GetProcessTimes
    get_times.argtypes = [C.c_void_p] + [C.POINTER(C.c_ulonglong)] * 4
    get_times.restype = C.c_int
    creation, exit, kernel, user = (C.c_ulonglong() for _ in range(4))
    if not get_times(int(handle), C.byref(creation), C.byref(exit), C.byref(kernel), C.byref(user)):
        raise OSError('Owned process timing unavailable')
    accounting = Accounting()
    if not api.QueryInformationJobObject(job.handle, 1, C.byref(accounting), C.sizeof(accounting), None):
        raise OSError('Owned Job timing unavailable')
    return {'owned_cpu_ns': (kernel.value + user.value) * 100,
            'job_cpu_ns': (accounting.kernel + accounting.user) * 100}
