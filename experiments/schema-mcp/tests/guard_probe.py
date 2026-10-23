"""Run alone: actual forbidden APIs, all rejected before their OS operation."""
import ctypes
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'validation')]
from synthetic_guard import SyntheticGuard, SyntheticDenied


def main():
    with tempfile.TemporaryDirectory(prefix='synthetic-probe-') as directory:
        guard = SyntheticGuard(ROOT, ROOT / 'synthetic-site', Path(directory)).install()
        checks = {}
        for name, operation in (
                ('socket_creation', lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
                ('arbitrary_subprocess', lambda: subprocess.Popen(['synthetic-never-executed'])),
                ('fake_private_source', lambda: (ROOT / 'not-real-private.edb').open('rb')),
                ('arbitrary_socketpair', lambda: socket.socketpair())):
            try:
                operation()
            except SyntheticDenied:
                checks[name] = True
            else:
                checks[name] = False
        # No kernel call: the synthetic filename makes the memory boundary
        # reject ctypes.dlopen before loading even this nonexistent library.
        try:
            exec(compile("ctypes.WinDLL('synthetic-never-loaded')", 'key_extractor.py', 'exec'),
                 {'ctypes': ctypes})
        except SyntheticDenied:
            checks['native_memory_entry'] = True
        else:
            checks['native_memory_entry'] = False
        try:
            with guard.creation(['synthetic-never-executed'], (17,)):
                raise ValueError('synthetic scope exit')
        except ValueError:
            pass
        checks['spawn_scope_cleared'] = guard.local.expected is None and guard.local.job is None
        print(json.dumps({'accepted': all(checks.values()), 'checks': checks,
                          'process_scoped_audit': True}), flush=True)
        return 0 if all(checks.values()) else 3


if __name__ == '__main__':
    raise SystemExit(main())
