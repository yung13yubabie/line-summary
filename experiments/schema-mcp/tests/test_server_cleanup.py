"""Pure fake-owner regressions for the adopted exact-owner client cleanup.

The test loads only the current Server AST, never native/product code.
"""
import ast
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server_cleanup

HERE = Path(__file__).resolve().parent


class Clock:
    now = 0.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += max(.001, seconds)


class Handle:
    def __init__(self, no_close=False):
        self.closed = False
        self.no_close = no_close
    def Close(self):
        if not self.no_close:
            self.closed = True


class Child:
    def __init__(self, *, running=False):
        self.stdin, self.stdout, self.stderr = (io.BytesIO() for _ in range(3))
        self._thread, self._handle = Handle(), Handle()
        self.returncode = None if running else 0
        self.kills = 0
        self.transient_handles, self.transient_fds = [], []
    def poll(self):
        return self.returncode
    def wait(self, timeout):
        if self.returncode is None:
            raise TimeoutError('fake exact child is still alive')
        return self.returncode
    def kill(self):
        self.kills += 1
        self.returncode = 125


class Job:
    def __init__(self, child, *, member=True, terminate_error=False):
        self.child, self.member = child, member
        self.terminate_error = terminate_error
        self.handle, self.inherited_handle = 1, 2
        self.terminations = 0
    def active(self):
        return int(self.member and self.child.returncode is None)
    def terminate(self):
        self.terminations += 1
        if self.terminate_error:
            raise OSError('fake uncertain termination')
        if self.member:
            self.child.returncode = 125
    def close(self):
        self.handle = self.inherited_handle = None


def fail(label):
    def operation():
        raise ValueError(label)
    return operation


def make_server(*, running=False, assigned=True, member=True, terminate_error=False):
    source = HERE / 'stdio_client.py'
    tree = ast.parse(source.read_text('utf-8'))
    definition = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
    clock = Clock()
    registry = {}
    server_cleanup.time = clock
    namespace = {'time': clock, 'RETAINED': registry, 'close_server': server_cleanup.close_server}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
    server_type = namespace['Server']
    server = server_type.__new__(server_type)
    child = Child(running=running)
    server.child = child
    server.job = Job(child, member=member, terminate_error=terminate_error)
    server.backend = SimpleNamespace(close_startup=lambda child: True)
    server.frames = SimpleNamespace(finish=lambda: None)
    server.drain = lambda: None
    server.deadline = 50
    server.closed = server.forced = False
    server.assigned = assigned
    registry['fixed-server'] = server
    return server, registry, clock


class ExactCleanup(unittest.TestCase):
    def assert_resources_closed(self, server, registry):
        self.assertTrue(server.closed)
        self.assertNotIn('fixed-server', registry)
        for label in ('stdin', 'stdout', 'stderr', '_thread', '_handle'):
            self.assertTrue(getattr(server.child, label).closed, label)
        self.assertIsNone(server.job.handle)
        self.assertIsNone(server.job.inherited_handle)

    def test_drain_failure_still_closes_every_exact_resource_and_preserves_error(self):
        server, registry, _ = make_server()
        server.drain = fail('original wire failure')
        with self.assertRaisesRegex(ValueError, '^original wire failure$'):
            server.close()
        self.assert_resources_closed(server, registry)

    def test_finish_failure_still_closes_every_exact_resource_and_preserves_error(self):
        server, registry, _ = make_server()
        server.frames.finish = fail('original truncated frame')
        with self.assertRaisesRegex(ValueError, '^original truncated frame$'):
            server.close()
        self.assert_resources_closed(server, registry)

    def test_unassigned_suspended_child_uses_only_its_exact_process_handle(self):
        server, registry, clock = make_server(running=True, assigned=False, member=False)
        server.close()
        self.assertEqual(server.child.kills, 1)
        self.assertEqual(server.job.terminations, 0)
        self.assertLess(clock.now, .02)
        self.assertTrue(server.forced)
        self.assert_resources_closed(server, registry)

    def test_assigned_live_child_forces_exact_job_within_cleanup_budget(self):
        server, registry, clock = make_server(running=True)
        server.close()
        self.assertEqual(server.job.terminations, 1)
        self.assertEqual(server.child.kills, 0)
        self.assertEqual(server._cooperative_deadline, 3.05)
        self.assertEqual(server._cleanup_deadline, 4.05)
        self.assertGreaterEqual(clock.now, 3.05)
        self.assertLessEqual(clock.now, 4.051)
        self.assert_resources_closed(server, registry)

    def test_cooperative_eof_exit_after_two_seconds_is_not_forced(self):
        server, registry, clock = make_server(running=True)
        def poll():
            if clock.now >= 2:
                server.child.returncode = 0
            return server.child.returncode
        server.child.poll = poll
        server.close()
        self.assertGreaterEqual(clock.now, 2)
        self.assertLess(clock.now, 3.05)
        self.assertFalse(server.forced)
        self.assertEqual(server.job.terminations, 0)
        self.assert_resources_closed(server, registry)

    def test_failed_force_cannot_claim_unforced_or_exact_release(self):
        server, registry, clock = make_server(running=True, terminate_error=True)
        with self.assertRaises(OSError):
            server.close()
        self.assertTrue(server.forced, 'A failed native force call has uncertain side effects')
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        self.assertFalse(server.child._handle.closed)
        self.assertIsNotNone(server.job.handle)
        self.assertLessEqual(clock.now, 4.051)

    def test_noop_native_handle_close_cannot_claim_exact_release(self):
        server, registry, _ = make_server()
        server.child._thread.no_close = True
        with self.assertRaisesRegex(RuntimeError, '^Exact server cleanup incomplete$'):
            server.close()
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        self.assertTrue(server.child._handle.closed)

    def test_returning_from_terminate_without_exit_is_not_cleanup_proof(self):
        server, registry, clock = make_server(running=True)
        server.job.terminate = lambda: None
        with self.assertRaises(RuntimeError):
            server.close()
        self.assertTrue(server.forced)
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        self.assertFalse(server.child._handle.closed)
        self.assertIsNotNone(server.job.handle)
        self.assertLessEqual(clock.now, 4.051)

    def test_failed_job_query_cannot_be_treated_as_empty(self):
        server, registry, _ = make_server()
        server.job.active = fail('exact Job query failed')
        with self.assertRaisesRegex(ValueError, '^exact Job query failed$'):
            server.close()
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        self.assertFalse(server.child._handle.closed)
        self.assertIsNotNone(server.job.handle)

    def test_failed_startup_release_can_recover_exact_owner_without_new_budget(self):
        server, registry, _ = make_server()
        server.backend.close_startup = fail('startup release failed')
        # close_startup receives this exact child as its single argument.
        server.backend.close_startup = lambda child: fail('startup release failed')()
        with self.assertRaisesRegex(ValueError, '^startup release failed$'):
            server.close()
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        saved_deadline = getattr(server, '_cleanup_deadline', None)
        self.assertIsNotNone(saved_deadline)
        server.backend.close_startup = lambda child: True
        server.close()
        self.assertEqual(server._cleanup_deadline, saved_deadline)
        self.assert_resources_closed(server, registry)

    def test_one_stream_close_failure_does_not_skip_other_releases(self):
        server, registry, _ = make_server()
        server.child.stdin = SimpleNamespace(closed=False, close=fail('stdin close failed'))
        with self.assertRaisesRegex(ValueError, '^stdin close failed$'):
            server.close()
        self.assertFalse(server.closed)
        self.assertIs(registry.get('fixed-server'), server)
        for label in ('stdout', 'stderr', '_thread', '_handle'):
            self.assertTrue(getattr(server.child, label).closed, label)
        self.assertIsNone(server.job.handle)

    def test_preprocess_failure_releases_partial_pipes_without_process_poll(self):
        server, registry, _ = make_server()
        server.child._thread = server.child._handle = None
        server.child.poll = fail('no process was created')
        server.close()
        self.assert_resources_closed_partial(server, registry)

    def assert_resources_closed_partial(self, server, registry):
        self.assertTrue(server.closed)
        self.assertNotIn('fixed-server', registry)
        for label in ('stdin', 'stdout', 'stderr'):
            self.assertTrue(getattr(server.child, label).closed, label)
        self.assertFalse(server.forced)


if __name__ == '__main__':
    unittest.main(verbosity=2)
