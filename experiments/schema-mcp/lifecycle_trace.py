"""Bounded fixed-label diagnostics; no source values, paths or process IDs."""
import builtins
from contextlib import contextmanager
import threading
import time

IMPORTS = {
    ('line_mcp_server', 'mcp'): 'server_mcp',
    ('line_mcp_server', 'mcp.server.fastmcp'): 'server_fastmcp',
    ('line_mcp_server', 'mcp.server.fastmcp.exceptions'): 'server_sdk_errors',
    ('line_mcp_server', 'db_reader'): 'server_db',
    ('line_mcp_server', 'key_extractor'): 'server_key',
    ('line_mcp_server', 'schema_mcp_adapter'): 'server_adapter',
    ('source_provider', 'line_mcp_server'): 'provider_settings_sdk',
}
PHASES = frozenset({
    'server_boot', 'runtime', 'owned_directory', 'guarded_import',
    'launcher_in', 'launcher_out', 'tool_binding', 'ready',
    'mcp_run_in', 'mcp_run_out', 'server_return', 'server_atexit',
    'service_close_in', 'service_close_out', 'observation_written',
    'provider_main_in', 'provider_main_out', 'fixture_in', 'fixture_out',
    'key_candidates', 'reader_in', 'reader_out',
    'client_open', 'client_spawned', 'client_assigned', 'client_resumed',
    'client_initialize_sent', 'client_initialize_received',
    'client_stdout_first', 'client_stderr_first', 'client_eof', 'client_eof_sent',
    'client_exit_observed', 'client_job_empty', 'client_force', 'client_closed',
} | {label + suffix for label in IMPORTS.values() for suffix in ('_in', '_out')})
FACTS = {'assigned': bool, 'resumed': bool, 'exited': bool, 'job_empty': bool,
         'exit_code': int, 'active_processes': int, 'drain_calls': int,
         'drain_ns': int, 'drain_max_ns': int, 'proof_calls': int,
         'proof_ns': int, 'proof_max_ns': int, 'cooperative_deadline_ns': int,
         'owned_cpu_ns': int, 'job_cpu_ns': int}


class Trace:
    def __init__(self, emit=None):
        self.events = []
        self.dropped = 0
        self.emit_failures = 0
        self.lock = threading.Lock()
        self.emit = emit

    def mark(self, phase, **facts):
        if (phase not in PHASES or any(name not in FACTS or type(value) is not FACTS[name]
                or type(value) is int and not 0 <= value <= 2**63 - 1
                for name, value in facts.items())):
            raise ValueError('Finite lifecycle observation required')
        row = {'phase': phase, 'wall_ns': time.perf_counter_ns(),
               'budget_clock_ns': time.monotonic_ns(),
               'cpu_ns': time.process_time_ns(), 'thread_ns': time.thread_time_ns(), **facts}
        with self.lock:
            if len(self.events) >= 64:
                self.dropped += 1
                return
            self.events.append(row)
        if self.emit is not None:
            try:
                self.emit(dict(row))
            except Exception:
                # A diagnostic sink must not replace the observed exception.
                # The caller fails acceptance from this explicit counter.
                with self.lock:
                    self.emit_failures += 1

    def snapshot(self):
        with self.lock:
            return {'events': [dict(row) for row in self.events], 'dropped': self.dropped,
                    'emit_failures': self.emit_failures,
                    'clock_resolution_ns': {name: max(1, round(time.get_clock_info(name).resolution * 1e9))
                                            for name in ('perf_counter', 'monotonic', 'process_time', 'thread_time')}}

    @contextmanager
    def imports(self):
        original = builtins.__import__

        def observed(name, globals=None, locals=None, fromlist=(), level=0):
            caller = globals.get('__name__') if type(globals) is dict else None
            label = IMPORTS.get((caller, name))
            if label is None:
                return original(name, globals, locals, fromlist, level)
            self.mark(label + '_in')
            try:
                return original(name, globals, locals, fromlist, level)
            finally:
                self.mark(label + '_out')

        builtins.__import__ = observed
        try:
            yield
        finally:
            builtins.__import__ = original
