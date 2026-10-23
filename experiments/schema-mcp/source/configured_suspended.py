"""Exact new-child ownership; explicit inherited handles; no process lookup."""
import _winapi as api
import ctypes
from ctypes import wintypes as W
import msvcrt
import os
import subprocess

kernel = ctypes.WinDLL('kernel32', use_last_error=True, winmode=0x800)
kernel.ResumeThread.argtypes = [W.HANDLE]
kernel.ResumeThread.restype = W.DWORD


class Handle:
    def __init__(self, value):
        self.value, self.closed = value, False

    def __int__(self):
        return self.value

    def Close(self):
        if not self.closed:
            api.CloseHandle(self.value)
            self.closed = True


class Child:
    def __init__(self):
        self.stdin = self.stdout = self.stderr = None
        self._handle = self._thread = None
        self.returncode = None
        self.resumed = False
        self.transient_handles, self.transient_fds = [], []

    def resume(self):
        count = kernel.ResumeThread(int(self._thread))
        if count != 1:
            raise OSError('owned resume unavailable')
        self.resumed = True

    def poll(self):
        if self.returncode is None:
            status = api.WaitForSingleObject(int(self._handle), 0)
            if status == api.WAIT_OBJECT_0:
                self.returncode = api.GetExitCodeProcess(int(self._handle))
            elif status != api.WAIT_TIMEOUT:
                raise OSError('owned wait unavailable')
        return self.returncode

    def wait(self, timeout):
        if self.returncode is None:
            status = api.WaitForSingleObject(int(self._handle), max(0, int(timeout * 1000)))
            if status == api.WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired('configured-owned-child', timeout)
            if status != api.WAIT_OBJECT_0:
                raise OSError('owned wait unavailable')
            self.returncode = api.GetExitCodeProcess(int(self._handle))
        return self.returncode

    def kill(self):
        api.TerminateProcess(int(self._handle), 125)


def create(argv, inherited, register):
    child = Child()
    register(child)  # Partial pipe/CreateProcess failure still has an exact owner.
    raw, child_ends = child.transient_handles, []
    try:
        current = api.GetCurrentProcess()
        for label, parent_reads in (('stdin', False), ('stdout', True), ('stderr', True)):
            read, write = api.CreatePipe(None, 0)
            raw.extend((read, write))
            parent, remote = (read, write) if parent_reads else (write, read)
            allowed = api.DuplicateHandle(current, remote, current, 0, True, api.DUPLICATE_SAME_ACCESS)
            raw.append(allowed)
            child_ends.append(allowed)
            api.CloseHandle(remote)
            raw.remove(remote)
            fd = msvcrt.open_osfhandle(parent, (os.O_RDONLY if parent_reads else os.O_WRONLY) | os.O_BINARY)
            raw.remove(parent)
            child.transient_fds.append(fd)
            setattr(child, label, os.fdopen(fd, 'rb' if parent_reads else 'wb', buffering=0))
            child.transient_fds.remove(fd)
        startup = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESTDHANDLES,
            hStdInput=child_ends[0], hStdOutput=child_ends[1], hStdError=child_ends[2],
            lpAttributeList={'handle_list': child_ends + list(inherited)})
        hp, ht, _, _ = api.CreateProcess(argv[0], subprocess.list2cmdline(argv), None, None,
            True, subprocess.CREATE_NO_WINDOW | 0x4, None, None, startup)
        child._handle, child._thread = Handle(hp), Handle(ht)
        return child
    finally:
        failure = None
        for handle in list(raw):
            try:
                api.CloseHandle(handle)
                raw.remove(handle)
            except OSError as error:
                failure = error
        if failure is not None:
            raise failure
