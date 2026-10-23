"""Synthetic audit inputs only: no actual socket, subprocess or private read."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from synthetic_guard import SyntheticGuard, SyntheticDenied


class SyntheticBoundary(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='synthetic-guard-')
        self.owned = Path(self.directory.name)
        self.guard = SyntheticGuard(ROOT, ROOT / 'synthetic-site', self.owned)

    def tearDown(self):
        self.directory.cleanup()

    def test_network_audit_inputs_are_denied(self):
        for event in ('socket.__new__', 'socket.connect', 'socket.bind', 'socket.getaddrinfo'):
            with self.subTest(event=event), self.assertRaises(SyntheticDenied):
                self.guard.audit(event, ())
        self.assertEqual(self.guard.stats['blocked_socket'], 4)

    def test_arbitrary_process_audit_inputs_are_denied(self):
        for event in ('subprocess.Popen', 'os.system', 'os.kill', 'os.posix_spawn',
                      'os.startfile', 'os.startfile/2', '_winapi.CreateProcess'):
            with self.subTest(event=event), self.assertRaises(SyntheticDenied):
                self.guard.audit(event, ('synthetic-not-executed',))
        self.assertEqual(self.guard.stats['blocked_subprocess'], 7)

    def test_fake_private_source_and_outside_writes_are_denied(self):
        for name in ('not-real.edb', 'not-real.edb-wal', 'not-real.edb-shm',
                     'settings.json', 'auth.json', 'not-real.cfg'):
            with self.subTest(name=name), self.assertRaises(SyntheticDenied):
                self.guard.audit('open', (str(ROOT / name), 'r', 0))
        for event, args in [('open', (str(ROOT / 'not-written.txt'), 'w', os.O_WRONLY)),
                            ('os.remove', (str(ROOT / 'not-removed.txt'),)),
                            ('os.rename', (str(self.owned / 'synthetic'), str(ROOT / 'outside')))]:
            with self.subTest(event=event), self.assertRaises(SyntheticDenied):
                self.guard.audit(event, args)

    def test_ambiguous_paths_reject_before_resolution(self):
        for path in (r'\\synthetic-host\share\file', r'C:\synthetic\..\file',
                     r'C:\synthetic\file:alternate', r'\rooted\file'):
            with self.subTest(path=path), patch.object(Path, 'resolve', side_effect=AssertionError('resolved')):
                with self.assertRaises(SyntheticDenied):
                    self.guard.path(path)

    def test_owned_files_and_public_source_reads_are_scoped(self):
        self.guard.path(str(self.owned / 'synthetic.edb'), True)
        self.guard.path(str(ROOT / 'source' / 'source_provider.py'))
        self.assertEqual(self.guard.stats['blocked_file'], 0)

    def test_socketpair_does_not_allow_arbitrary_callers(self):
        with patch.object(self.guard, 'original_pair', side_effect=AssertionError('real pair called')):
            with self.assertRaises(SyntheticDenied):
                self.guard.pair()
        self.assertFalse(getattr(self.guard.local, 'internal_pair', False))

    def test_native_creation_scope_is_cleared_after_failure(self):
        with self.assertRaises(ValueError):
            with self.guard.creation(['synthetic-python', 'fixed-not-executed.py'], (17,)):
                self.assertIsNotNone(self.guard.local.expected)
                raise ValueError('injected')
        self.assertIsNone(self.guard.local.expected)
        self.assertIsNone(self.guard.local.job)
        with self.assertRaises(SyntheticDenied):
            self.guard.audit('_winapi.CreateProcess', ('synthetic-python',))


if __name__ == '__main__':
    unittest.main()
