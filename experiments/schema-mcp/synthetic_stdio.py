"""Local synthetic-only stdio entry. The first input is ordinary MCP JSON-RPC."""
import sys
import time
sys.stderr.write('Synthetic trace: {"phase":"server_boot","wall_ns":%d,"budget_clock_ns":%d,"cpu_ns":%d,"thread_ns":%d}\n' %
                 (time.perf_counter_ns(), time.monotonic_ns(), time.process_time_ns(), time.thread_time_ns()))
sys.stderr.flush()
import argparse
import atexit
import json
from pathlib import Path, PureWindowsPath
import sys
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parent
STARTED = time.monotonic()
sys.path.insert(0, str(ROOT))
from lifecycle_trace import Trace
TRACE = Trace(lambda row: (sys.stderr.write('Synthetic trace: ' + json.dumps(row) + '\n'), sys.stderr.flush()))
CASES = frozenset({'success', 'wrong_key', 'missing_wal', 'missing_shm',
                   'invalid_db', 'exception', 'cancel_key', 'timeout_key', 'cancel_then_success'})


def stage(name):
    TRACE.mark(name.replace('-', '_'))
    sys.stderr.write('Synthetic stage: ' + name + ' ' + str(round(time.monotonic() - STARTED, 3)) + '\n')
    sys.stderr.flush()


class StartupParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('Synthetic startup arguments rejected')


def parse_startup(argv):
    parser = StartupParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--site-packages', required=True)
    parser.add_argument('--artifacts', required=True)
    parser.add_argument('--case', choices=sorted(CASES), default='success')
    value = parser.parse_args(argv)
    for supplied in (value.site_packages, value.artifacts):
        path = PureWindowsPath(supplied)
        if (not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ':'
                or not path.drive[0].isalpha() or len(supplied) > 1800
                or any(ord(c) < 32 for c in supplied) or '..' in path.parts
                or any(':' in part or part.endswith((' ', '.')) or PureWindowsPath(part).is_reserved()
                       or any(c in part for c in '<>"|?*') for part in path.parts[1:])):
            raise ValueError('Synthetic startup arguments rejected')
    site = PureWindowsPath(value.site_packages)
    if site.name != 'site-packages' or site.parent.name != 'Lib':
        raise ValueError('Synthetic startup arguments rejected')
    return value


def runtime(site):
    if (sys.version.split()[0] != '3.11.9' or not sys.flags.isolated
            or not sys.flags.no_site or not sys.flags.dont_write_bytecode):
        raise RuntimeError('Synthetic runtime rejected')
    sys.path.insert(0, str(ROOT))
    from preview import check_runtime_versions
    check_runtime_versions(site, {'mcp': '1.29.0', 'anyio': '4.15.1', 'pydantic': '2.13.5'})
    sys.path[:0] = [str(ROOT / 'source'), str(ROOT / 'validation')]
    sys.path.extend((str(site), str(site / 'win32'), str(site / 'win32/lib')))
    sys.prefix = str(site.parent.parent)
    sys.executable = str(site.parent.parent / 'Scripts' / 'python.exe')


def build_server(value):
    stage('runtime')
    site = Path(value.site_packages).resolve()
    runtime(site)
    stage('owned-directory')
    artifacts = Path(value.artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    session = artifacts / ('session_' + uuid.uuid4().hex)
    session.mkdir()  # Fresh ownership; never open a pre-existing source directory.
    from synthetic_guard import SyntheticGuard
    guard = SyntheticGuard(ROOT, site, session).install()
    stage('guarded-import')
    TRACE.mark('launcher_in')
    import configured_launcher as launcher
    import configured_native_owner as native
    TRACE.mark('launcher_out')
    launch_index = 0

    class SyntheticBackend(native.Backend):
        def spawn(self, argv, inherited, register):
            nonlocal launch_index
            argv = list(argv)
            expected = [sys._base_executable, '-I', '-S', '-B',
                        str(ROOT / 'source' / 'configured_entry_gate.py')]
            if (len(argv) != 7 or argv[:5] != expected
                    or not Path(argv[5]).resolve().is_relative_to(session)
                    or Path(argv[6]).resolve() != site):
                raise RuntimeError('Fixed synthetic entry required')
            argv[4] = str(ROOT / 'synthetic_gate.py')
            mode = value.case
            if mode == 'cancel_then_success':
                mode = 'cancel_key' if launch_index == 0 else 'success'
            argv.extend((mode, str(session)))
            launch_index += 1
            with guard.creation(argv, inherited):
                return super().spawn(argv, inherited, register)

    launcher._backend = SyntheticBackend
    with TRACE.imports():
        import line_mcp_server as server
    stage('tool-binding')
    from types import MappingProxyType
    server._policy = MappingProxyType({**server._DEFAULTS, 'enabled': False,
                                      'allowed_chat_ids': frozenset()})
    for name in ('line_list_chats', 'line_get_history', 'line_get_unread', 'line_get_contacts'):
        server.mcp.remove_tool(name)
    def no_legacy(*args, **kwargs):
        raise RuntimeError('Synthetic entry has no legacy source capability')
    server.extract_key = server.DbReader = server._find_edb_path = no_legacy
    server.configure_schema(str(session / 'provider_settings.cfg'), str(session / 'artifacts'))
    original_close = server._schema_service.close
    async def observed_close():
        TRACE.mark('service_close_in')
        try:
            return await original_close()
        finally:
            TRACE.mark('service_close_out')
    server._schema_service.close = observed_close
    def exit_observation():
        TRACE.mark('server_atexit')
        (session / 'server_lifecycle.json').write_text(json.dumps(TRACE.snapshot()), 'utf-8')
    atexit.register(exit_observation)
    stage('ready')
    return server, guard, session


def main(argv=None):
    value = parse_startup(sys.argv[1:] if argv is None else argv)
    server, guard, session = build_server(value)
    error = None
    try:
        # No read/consume of stdin precedes the actual MCP transport.
        TRACE.mark('mcp_run_in')
        server.mcp.run(transport='stdio')
    except BaseException as failure:
        error = type(failure).__name__
    finally:
        TRACE.mark('mcp_run_out')
        observation = {'service': server._schema_service.observation(), 'guard': guard.stats,
            'diagnostic_emit_failures': TRACE.emit_failures,
            'retained_launchers': len(__import__('configured_launcher').RETAINED),
            'wrapper_error': error, 'synthetic_only': True, 'case': value.case,
            'outer_private_start_frame': False,
            'non_daemon_threads_at_return': sum(t is not threading.current_thread() and t.is_alive() and not t.daemon for t in threading.enumerate())}
        state = observation['service']
        clean = (type(state.get('active_operations')) is int and state['active_operations'] == 0
                 and state.get('pending_cleanup') is False and state.get('stopping') is True
                 and state.get('configured') is False and observation['retained_launchers'] == 0
                 and (state.get('last_os_release_confirmed') is True
                      or state.get('recovery_release_confirmed') is True))
        observation['terminal_cleanup_confirmed'] = clean
        (session / 'server_observation.json').write_text(json.dumps(observation, indent=2), 'utf-8')
    TRACE.mark('observation_written')
    TRACE.mark('server_return')
    (session / 'server_lifecycle.json').write_text(json.dumps(TRACE.snapshot()), 'utf-8')
    return 0 if error is None and clean and TRACE.emit_failures == 0 else 3


if __name__ == '__main__':
    try:
        result = main()
    except BaseException:
        sys.stderr.write('Synthetic MCP bootstrap failed; details suppressed\n')
        result = 3
    raise SystemExit(result)
