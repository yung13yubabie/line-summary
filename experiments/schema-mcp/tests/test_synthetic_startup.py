"""New startup contract; no database, source discovery or native process work."""
import contextlib
import importlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StartupContract(unittest.TestCase):
    def module(self):
        return importlib.import_module('synthetic_stdio')

    def arguments(self):
        return ['--site-packages', r'C:\synthetic\Lib\site-packages',
                '--artifacts', r'C:\synthetic\owned-output', '--case', 'success']

    def test_parse_does_not_read_stdin_or_create_source(self):
        module = self.module()
        with patch.object(sys, 'stdin', object()), patch.object(Path, 'mkdir', side_effect=AssertionError('early output')):
            value = module.parse_startup(self.arguments())
        self.assertEqual(value.case, 'success')

    def test_remote_configuration_flags_are_not_accepted(self):
        module = self.module()
        for flag in ('--key', '--settings', '--db', '--module', '--command'):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaisesRegex(ValueError, '^Synthetic startup arguments rejected$'):
                    module.parse_startup(self.arguments() + [flag, 'SYNTHETIC_PRIVATE_SENTINEL'])
                self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', error.getvalue())

    def test_network_and_ambiguous_paths_refused_before_resolution(self):
        module = self.module()
        for invalid in (r'\\synthetic-host\share\output', r'\rooted\output',
                        r'C:\synthetic\output:stream', r'C:\synthetic\..\outside'):
            arguments = self.arguments()
            arguments[3] = invalid
            with self.subTest(path=invalid), patch.object(Path, 'resolve', side_effect=AssertionError('early resolution')):
                with self.assertRaises(ValueError):
                    module.parse_startup(arguments)

    def test_only_finite_synthetic_modes(self):
        module = self.module()
        self.assertEqual(module.CASES, frozenset({'success', 'wrong_key', 'missing_wal',
            'missing_shm', 'invalid_db', 'exception', 'cancel_key', 'timeout_key', 'cancel_then_success'}))
        arguments = self.arguments()
        arguments[-1] = 'arbitrary-private-source'
        with self.assertRaises(ValueError):
            module.parse_startup(arguments)

    def test_unconfirmed_service_cleanup_cannot_exit_successfully(self):
        module = self.module()
        service = SimpleNamespace(observation=lambda: {'active_operations': 0,
            'pending_cleanup': True, 'stopping': True, 'configured': False})
        server = SimpleNamespace(mcp=SimpleNamespace(run=lambda **kwargs: None), _schema_service=service)
        guard = SimpleNamespace(stats={})
        with tempfile.TemporaryDirectory(prefix='synthetic-contract-') as directory:
            with patch.dict(sys.modules, {'configured_launcher': SimpleNamespace(RETAINED={})}), \
                    patch.object(module, 'build_server', return_value=(server, guard, Path(directory))):
                self.assertEqual(module.main(self.arguments()), 3)


if __name__ == '__main__':
    unittest.main()
