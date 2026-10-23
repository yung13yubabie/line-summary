"""Finite diagnostics must preserve imports, failures and capability boundaries."""
import builtins
from pathlib import Path
import sys
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lifecycle_trace import Trace


class LifecycleTrace(unittest.TestCase):
    def test_late_or_missing_diagnostic_snapshot_cannot_pass(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from bootstrap_cases import after_close
        server = SimpleNamespace(forced=False, closed=True, child=SimpleNamespace(returncode=0))
        with tempfile.TemporaryDirectory(prefix='synthetic-final-trace-') as directory:
            root = Path(directory)
            session = root / 'artifacts' / 'synthetic-session'
            session.mkdir(parents=True)
            observation = {'service': {'active_operations': 0, 'pending_cleanup': False},
                'guard': {key: 0 for key in ('blocked_native_memory', 'blocked_socket',
                                           'blocked_subprocess', 'blocked_file')},
                'outer_private_start_frame': False, 'terminal_cleanup_confirmed': True,
                'retained_launchers': 0, 'wrapper_error': None, 'diagnostic_emit_failures': 0}
            (session / 'server_observation.json').write_text(json.dumps(observation), 'utf-8')
            for snapshot, expected in ((None, False),
                    ({'events': [{'phase': 'server_atexit'}], 'emit_failures': 1, 'dropped': 0}, False),
                    ({'events': [{'phase': 'server_return'}], 'emit_failures': 0, 'dropped': 0}, False),
                    ({'events': [{'phase': 'server_atexit'}], 'emit_failures': 0, 'dropped': 1}, False),
                    ({'events': [{'phase': 'server_atexit'}], 'emit_failures': 0, 'dropped': 0}, True)):
                if snapshot is not None:
                    (session / 'server_lifecycle.json').write_text(json.dumps(snapshot), 'utf-8')
                with self.subTest(snapshot=snapshot):
                    checks = after_close(server, root, 'eof_idle')
                    self.assertIs(checks.get('final_diagnostics_healthy'), expected)
                    self.assertIs(all(checks.values()), expected)

    def test_cpu_and_wall_are_distinct_integer_observations(self):
        trace = Trace()
        trace.mark('client_open', assigned=False)
        event = trace.snapshot()['events'][0]
        self.assertEqual(event['phase'], 'client_open')
        for name in ('wall_ns', 'cpu_ns', 'thread_ns'):
            self.assertIs(type(event[name]), int)
        self.assertIs(event['assigned'], False)

    def test_arbitrary_labels_and_values_cannot_enter_diagnostics(self):
        trace = Trace()
        for label, facts in [('SYNTHETIC_PRIVATE_SENTINEL', {}),
                             ('client_open', {'path': 'SYNTHETIC_PRIVATE_SENTINEL'}),
                             ('client_open', {'exit_code': 'SYNTHETIC_PRIVATE_SENTINEL'})]:
            with self.assertRaises(ValueError):
                trace.mark(label, **facts)
        self.assertEqual(trace.snapshot()['events'], [])

    def test_buffer_is_bounded_and_snapshot_cannot_mutate_records(self):
        trace = Trace()
        for _ in range(70):
            trace.mark('client_open')
        snapshot = trace.snapshot()
        self.assertEqual(len(snapshot['events']), 64)
        self.assertEqual(snapshot['dropped'], 6)
        snapshot['events'][0]['phase'] = 'changed'
        self.assertEqual(trace.snapshot()['events'][0]['phase'], 'client_open')

    def test_import_arguments_and_return_are_preserved(self):
        trace = Trace()
        expected = object()
        scope = {'__name__': 'line_mcp_server'}
        with patch.object(builtins, '__import__', return_value=expected) as original:
            with trace.imports():
                result = builtins.__import__('mcp', scope, scope, ('__file__',), 0)
            self.assertIs(builtins.__import__, original)
            original.assert_called_once_with('mcp', scope, scope, ('__file__',), 0)
            self.assertIs(result, expected)
        self.assertEqual([e['phase'] for e in trace.snapshot()['events']],
                         ['server_mcp_in', 'server_mcp_out'])

    def test_import_exception_is_not_swallowed_and_binding_restores(self):
        trace = Trace()
        failure = ValueError('SYNTHETIC_PRIVATE_SENTINEL')
        with patch.object(builtins, '__import__', side_effect=failure) as original:
            with self.assertRaises(ValueError) as caught:
                with trace.imports():
                    builtins.__import__('line_mcp_server', {'__name__': 'source_provider'})
            self.assertIs(caught.exception, failure)
            self.assertIs(builtins.__import__, original)
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', repr(trace.snapshot()))

    def test_unlisted_imports_are_unobserved_but_still_delegated(self):
        trace = Trace()
        with patch.object(builtins, '__import__', return_value=None) as original:
            with trace.imports():
                builtins.__import__('synthetic-unlisted', {'__name__': 'synthetic-caller'})
            self.assertEqual(original.call_count, 1)
        self.assertEqual(trace.snapshot()['events'], [])

    def test_failed_output_diagnostic_cannot_replace_primary_import_exception(self):
        emitted = []
        def sink(row):
            emitted.append(row)
            if len(emitted) == 2:
                raise BrokenPipeError('SYNTHETIC_PRIVATE_SENTINEL')
        trace = Trace(sink)
        primary = ValueError('primary import failure')
        with patch.object(builtins, '__import__', side_effect=primary):
            with self.assertRaises(ValueError) as caught:
                with trace.imports():
                    builtins.__import__('mcp', {'__name__': 'line_mcp_server'})
        self.assertIs(caught.exception, primary)
        self.assertEqual(trace.snapshot()['emit_failures'], 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', repr(trace.snapshot()))


if __name__ == '__main__':
    unittest.main()
