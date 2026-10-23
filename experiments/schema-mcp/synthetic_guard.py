"""Synthetic server boundary; exact native creator, no external source access."""
from contextlib import contextmanager
import ntpath
import os
from pathlib import Path
import socket
import sys
import threading
import time


class SyntheticDenied(RuntimeError):
    pass


def lexical_local(value):
    if type(value) is not str or not value or any(ord(c) < 32 for c in value):
        return False
    drive, tail = ntpath.splitdrive(value.replace('/', '\\'))
    return (len(drive) == 2 and drive[0].isalpha() and drive[1] == ':'
            and tail.startswith('\\') and ':' not in tail
            and '..' not in tail.split('\\') and not value.startswith(('\\\\', '\\??\\')))


class SyntheticGuard:
    def __init__(self, code, site, owned):
        self.read_roots = tuple(Path(p).resolve() for p in (code, site, sys.base_prefix, owned))
        self.owned = Path(owned).resolve()
        self.local = threading.local()
        self.stats = {'blocked_socket': 0, 'blocked_subprocess': 0, 'blocked_file': 0,
                      'blocked_native_memory': 0, 'internal_socketpairs': 0,
                      'configured_spawn_calls': 0, 'path_checks': 0, 'path_check_ns': 0, 'max_path_check_ns': 0}
        self.native = None
        self.original_pair = socket.socketpair

    def reject(self, counter):
        self.stats[counter] += 1
        raise SyntheticDenied('Synthetic capability denied')

    def path(self, value, write=False):
        started = time.perf_counter_ns()
        try:
            supplied = os.fsdecode(value)
            if supplied.casefold() == os.devnull.casefold():
                return
            if not lexical_local(supplied):
                self.reject('blocked_file')
            path = Path(supplied).resolve()
            private = (path.suffix.casefold() in ('.edb', '.db', '.sqlite', '.sqlite3', '.cfg')
                       or path.name.casefold() in ('settings.json', 'auth.json', '.mcp.json')
                       or path.name.casefold().endswith(('-wal', '-shm')))
            if ((write or private) and not path.is_relative_to(self.owned)
                    or not any(path.is_relative_to(root) for root in self.read_roots)):
                self.reject('blocked_file')
        finally:
            elapsed = time.perf_counter_ns() - started
            self.stats['path_checks'] += 1
            self.stats['path_check_ns'] += elapsed
            self.stats['max_path_check_ns'] = max(self.stats['max_path_check_ns'], elapsed)

    def pair(self, *args, **kwargs):
        frame = sys._getframe(1)
        while frame is not None:
            if (frame.f_code.co_name == '_make_self_pipe'
                    and Path(frame.f_code.co_filename).parent.name == 'asyncio'):
                break
            frame = frame.f_back
        if frame is None:
            self.reject('blocked_socket')
        self.local.internal_pair = True
        try:
            result = self.original_pair(*args, **kwargs)
            self.stats['internal_socketpairs'] += 1
            return result
        finally:
            self.local.internal_pair = False

    @contextmanager
    def creation(self, argv, inherited):
        import subprocess
        if len(inherited) != 1 or getattr(self.local, 'expected', None) is not None:
            self.reject('blocked_subprocess')
        self.local.expected = (argv[0], subprocess.list2cmdline(argv))
        self.local.job = inherited[0]
        try:
            yield
        finally:
            self.local.expected = self.local.job = None

    def audit(self, event, args):
        if event.startswith('socket.') and not getattr(self.local, 'internal_pair', False):
            self.reject('blocked_socket')
        if event in ('subprocess.Popen', 'os.system', 'os.kill', 'os.posix_spawn', 'os.startfile', 'os.startfile/2'):
            self.reject('blocked_subprocess')
        if event == '_winapi.CreateProcess':
            expected = getattr(self.local, 'expected', None)
            if self.native is None or not self.native.audit_matches(args, expected):
                self.reject('blocked_subprocess')
            self.stats['configured_spawn_calls'] += 1
        if event == 'ctypes.dlopen':
            frame = sys._getframe(1)
            while frame is not None:
                if Path(frame.f_code.co_filename).name in ('key_extractor.py', 'line_process_identity.py'):
                    self.reject('blocked_native_memory')
                frame = frame.f_back
        if event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 and type(args[2]) is int else 0
            write = (type(mode) is str and any(c in mode for c in 'wax+')) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            self.path(args[0], write)
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.link', 'os.symlink',
                     'os.truncate', 'os.chmod', 'os.utime'):
            paths = args[:2] if event in ('os.rename', 'os.link', 'os.symlink') else args[:1]
            for path in paths:
                if isinstance(path, (str, bytes, os.PathLike)):
                    self.path(path, True)

    def install(self):
        import _winapi
        from mcp_native_guard import NativeCreateGuard
        self.native = NativeCreateGuard(_winapi, lambda: getattr(self.local, 'expected', None),
            lambda: getattr(self.local, 'job', None))
        socket.socketpair = self.pair
        sys.addaudithook(self.audit)
        return self
