"""Defense in depth for the disabled preview, not an OS security sandbox."""
from pathlib import Path
import os
import ntpath
import socket
import sys
import threading


class PreviewDenied(RuntimeError):
    pass


def install(package_root, site):
    roots = (package_root.resolve(), site.resolve(), Path(sys.base_prefix).resolve())
    local = threading.local()
    stats = {'blocked_socket': 0, 'blocked_subprocess': 0, 'blocked_file': 0,
             'blocked_native': 0, 'internal_socketpairs': 0}
    original_pair = socket.socketpair

    def internal_pair(*args, **kwargs):
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == '_make_self_pipe' and Path(frame.f_code.co_filename).parent.name == 'asyncio':
                break
            frame = frame.f_back
        if frame is None:
            stats['blocked_socket'] += 1
            raise PreviewDenied('Preview denies external sockets')
        local.internal_pair = True
        try:
            result = original_pair(*args, **kwargs)
            stats['internal_socketpairs'] += 1
            return result
        finally:
            local.internal_pair = False

    def audit(event, args):
        if event in ('os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.link', 'os.symlink', 'os.truncate', 'os.chmod', 'os.utime'):
            stats['blocked_file'] += 1
            raise PreviewDenied('Preview denies filesystem mutation')
        if event.startswith('socket.') and not getattr(local, 'internal_pair', False):
            stats['blocked_socket'] += 1
            raise PreviewDenied('Preview denies external sockets')
        if event in ('subprocess.Popen', '_winapi.CreateProcess', 'os.system', 'os.posix_spawn', 'os.startfile', 'os.startfile/2', 'os.kill'):
            stats['blocked_subprocess'] += 1
            raise PreviewDenied('Preview denies child processes')
        if event == 'ctypes.dlopen':
            frame = sys._getframe(1)
            while frame is not None:
                if Path(frame.f_code.co_filename).name in ('key_extractor.py', 'line_process_identity.py'):
                    stats['blocked_native'] += 1
                    raise PreviewDenied('Preview denies native source preparation')
                frame = frame.f_back
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            supplied = os.fsdecode(args[0]).replace('/', '\\')
            drive,tail = ntpath.splitdrive(supplied)
            # Pure lexical checks precede any Win32 path resolution or I/O.
            if (len(drive)!=2 or not drive[0].isalpha() or drive[1]!=':'
                    or not tail.startswith('\\') or ':' in tail
                    or supplied.startswith(('\\\\', '\\??\\'))):
                stats['blocked_file'] += 1
                raise PreviewDenied('Preview denies network and device paths')
            path = Path(supplied).resolve()
            name = path.name.casefold()
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            private = (name in ('settings.json', 'plugin_settings.json', 'auth.json', '.mcp.json')
                       or path.suffix.casefold() in ('.edb', '.cfg', '.db', '.sqlite', '.sqlite3')
                       or name.endswith(('-wal', '-shm')))
            if write or private or not any(path.is_relative_to(root) for root in roots):
                stats['blocked_file'] += 1
                raise PreviewDenied('Preview denies source and arbitrary file access')

    socket.socketpair = internal_pair
    sys.addaudithook(audit)
    return stats
