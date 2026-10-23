"""Synthetic fixture supply for the unchanged configured worker/provider API.

Only discovery and candidate supply are synthetic. Key probes and schema reads
use the real read-only subprocess and its native source guard.
"""
import hashlib
import json
from pathlib import Path
import stat
import sys
import time


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'source'
sys.path.insert(0, str(ROOT))
from lifecycle_trace import Trace
TRACE = Trace()
CASES = frozenset({'success', 'wrong_key', 'missing_wal', 'missing_shm',
                   'invalid_db', 'exception', 'cancel_key', 'timeout_key'})
SENTINEL = 'SYNTHETIC_PRIVATE_SENTINEL'
SUFFIXES = ('', '-wal', '-shm')


def _plain_local_path(value, *, existing=False):
    from configured_child import explicit_local_file
    if not explicit_local_file(value):
        raise ValueError('synthetic local path rejected')
    path = Path(value)
    for part in (path, *path.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            if existing or part != path:
                raise ValueError('synthetic path unavailable') from None
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
            raise ValueError('synthetic reparse path rejected')
    return path.resolve(strict=existing)


def _synthetic_settings_target(requested, directory, session):
    session = _plain_local_path(str(session), existing=True)
    directory = _plain_local_path(str(directory), existing=True)
    target = _plain_local_path(requested)
    expected = session / 'provider_settings.cfg'
    if (not session.is_dir() or not directory.is_dir() or directory == session
            or not directory.is_relative_to(session) or directory.name != 'entry'
            or target != expected):
        raise ValueError('synthetic settings target rejected')
    return target


def _take_option(arguments, name):
    if arguments.count(name) != 1:
        raise ValueError('synthetic fixed option required')
    position = arguments.index(name)
    if position + 1 >= len(arguments):
        raise ValueError('synthetic fixed option required')
    value = arguments[position + 1]
    del arguments[position:position + 2]
    return value


def main():
    sys.path.insert(0, str(SOURCE))
    sys.path.insert(0, str(ROOT / 'validation'))
    import configured_child
    import source_provider
    from fixture_launch_policy import fixed_worker_command

    arguments = sys.argv[:]
    mode = _take_option(arguments, '--fixture-mode')
    session_value = _take_option(arguments, '--synthetic-root')
    if mode not in CASES:
        return 3
    session = _plain_local_path(session_value, existing=True)
    if arguments.count('--resource') != 1:
        return 3
    resource_position = arguments.index('--resource')
    if resource_position + 1 >= len(arguments):
        return 3
    resource = _plain_local_path(arguments[resource_position + 1])
    directory = resource.parent
    if (resource.name != 'synthetic_resource.bin' or directory.name != 'entry'
            or directory == session or not directory.is_relative_to(session)):
        return 3

    original_provider = source_provider.ConfiguredSourceProvider
    original_read_start = configured_child.read_start
    private_settings_target = None
    writer = None
    before = None
    source = directory / 'provider_fixture.edb'
    origin = directory / 'synthetic_origin.edb'
    readers = []
    provider_instances = []
    calls = {'factory': 0, 'discovery': 0, 'memory_candidates': 0,
             'native_memory_calls': 0, 'blocked_subprocess': 0}
    state = {'settings_target_verified': False, 'fixture_created': False,
             'wal_only_schema': False, 'selected_sidecar_removed': False,
             'reader_serial_confirmed': True, 'max_live_readers': 0}

    def read_start(stream):
        nonlocal private_settings_target
        requested = original_read_start(stream)
        private_settings_target = _synthetic_settings_target(requested, directory, session)
        state['settings_target_verified'] = True
        return requested

    def digests():
        # Absence is part of the baseline for missing-sidecar cases.
        result = {}
        for suffix in SUFFIXES:
            path = Path(str(source) + suffix)
            result[suffix] = (hashlib.sha256(path.read_bytes()).hexdigest()
                              if path.exists() else None)
        return result

    def create_fixture(apsw):
        nonlocal writer, before
        for base in (source, origin):
            if any(Path(str(base) + suffix).exists() for suffix in SUFFIXES):
                raise ValueError('synthetic fixture must be fresh')
        if mode == 'invalid_db':
            # Deliberately invalid, wholly synthetic bytes; no external DB is
            # opened, copied, or modified to construct this negative case.
            for suffix, size in (('', 4096), ('-wal', 32), ('-shm', 32768)):
                payload = (SENTINEL.encode('ascii') + b' invalid synthetic database\n')
                with Path(str(source) + suffix).open('xb') as handle:
                    handle.write((payload * (size // len(payload) + 1))[:size])
        else:
            writer = apsw.Connection(str(origin))
            writer.execute("PRAGMA cipher='aes128cbc'")
            writer.execute("PRAGMA key='" + '1' * 32 + "'")
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE synthetic_first(value TEXT)')
            writer.execute('INSERT INTO synthetic_first VALUES(?)', (SENTINEL,))
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            main_before = hashlib.sha256(origin.read_bytes()).digest()
            writer.execute('CREATE TABLE synthetic_second(value INTEGER)')
            writer.execute('CREATE VIEW synthetic_view AS SELECT value FROM synthetic_second')
            writer.execute('INSERT INTO synthetic_second VALUES(23)')
            state['wal_only_schema'] = (
                hashlib.sha256(origin.read_bytes()).digest() == main_before
                and Path(str(origin) + '-wal').stat().st_size > 32)
            if not state['wal_only_schema']:
                raise ValueError('synthetic WAL fixture rejected')
            # The source reader sees one immutable, generated three-file copy.
            # Keep the writer alive on origin until all native readers release.
            for suffix in SUFFIXES:
                payload = Path(str(origin) + suffix).read_bytes()
                with Path(str(source) + suffix).open('xb') as handle:
                    handle.write(payload)
            if mode in ('missing_wal', 'missing_shm'):
                suffix = '-wal' if mode == 'missing_wal' else '-shm'
                target = Path(str(source) + suffix)
                if target.parent != directory or not target.is_relative_to(session):
                    raise ValueError('synthetic sidecar scope rejected')
                target.unlink()
                state['selected_sidecar_removed'] = True
        state['fixture_created'] = True
        before = digests()

    def factory():
        # configured_child.main writes this only after read_start and the real
        # owned-Job check. Settings validation must also have succeeded first.
        if (private_settings_target is None
                or not (directory / 'configured_backend_started.json').is_file()):
            raise ValueError('synthetic fixture requires owned start')
        calls['factory'] += 1
        if calls['factory'] != 1:
            raise ValueError('synthetic provider is single use')
        import apsw
        import key_extractor
        import db_reader
        import schema_probe
        import readonly_client

        TRACE.mark('fixture_in')
        try:
            create_fixture(apsw)
        finally:
            TRACE.mark('fixture_out')
        # The only accepted settings file is generated after the owned start.
        private_settings_target.write_text(
            json.dumps({'enabled': True, 'db_path': str(source)}), encoding='utf-8')

        def discovery():
            calls['discovery'] += 1
            return 123

        def candidates(pid):
            TRACE.mark('key_candidates')
            calls['memory_candidates'] += 1
            if pid != 123:
                raise ValueError('synthetic discovery token rejected')
            if mode in ('cancel_key', 'timeout_key'):
                budget = key_extractor.current_budget()
                if budget is None:
                    raise ValueError('synthetic candidate budget required')
                budget.check('memory_scan')
                (directory / 'timeout_ready.json').write_text(
                    json.dumps({'candidate_loop_checked': True}), encoding='utf-8')
                while True:
                    budget.check('memory_scan')
                    time.sleep(.01)
            if mode == 'exception':
                raise ValueError(SENTINEL + ' ' + '1' * 32)
            return ['f' * 32] if mode == 'wrong_key' else ['f' * 32, '1' * 32]

        def no_native_memory():
            calls['native_memory_calls'] += 1
            raise AssertionError('native discovery and memory access forbidden')

        key_extractor.find_line_pid = discovery
        key_extractor._scan_memory_regions = candidates
        key_extractor._load_kernel32 = no_native_memory
        original_reader = readonly_client.WorkerConnection

        def observed_reader(path, key):
            TRACE.mark('reader_in')
            live = sum(not getattr(reader, '_released', False) for reader in readers)
            if live:
                state['reader_serial_confirmed'] = False
                raise AssertionError('synthetic key readers must release serially')
            # Never permit a reader to leave this generated fixture scope.
            if Path(path) != source:
                raise ValueError('synthetic reader source rejected')
            try:
                reader = original_reader(path, key)
            except BaseException as error:
                TRACE.mark('reader_out')
                reader = getattr(error, 'worker', None)
                if reader is not None:
                    readers.append(reader)
                    state['max_live_readers'] = max(state['max_live_readers'], live + 1)
                raise
            TRACE.mark('reader_out')
            readers.append(reader)
            state['max_live_readers'] = max(state['max_live_readers'], live + 1)
            return reader

        db_reader.WorkerConnection = observed_reader
        schema_probe.WorkerConnection = observed_reader
        expected_command = [sys.executable, '-I', '-B', str(SOURCE / 'readonly_worker.py')]

        def audit(event, values):
            if event == 'subprocess.Popen' and not fixed_worker_command(values[1], expected_command):
                calls['blocked_subprocess'] += 1
                raise AssertionError('only the fixed synthetic source reader may launch')
            if event in ('socket.connect', 'socket.bind', 'os.system', 'os.kill'):
                raise AssertionError('offline synthetic integration only')

        sys.addaudithook(audit)
        provider = original_provider()
        provider_instances.append(provider)
        return provider

    configured_child.read_start = read_start
    source_provider.ConfiguredSourceProvider = factory
    original_arguments = sys.argv
    sys.argv = arguments
    code = 3
    wrapper_error_kind = None
    try:
        TRACE.mark('provider_main_in')
        with TRACE.imports():
            code = configured_child.main()
    except BaseException as error:
        wrapper_error_kind = source_provider._kind(error)
    finally:
        TRACE.mark('provider_main_out')
        configured_child.read_start = original_read_start
        source_provider.ConfiguredSourceProvider = original_provider
        sys.argv = original_arguments
        stable = False
        if before is not None:
            try:
                stable = digests() == before
            except BaseException:
                pass
        evidence = []
        if readers:
            from schema_probe import release_evidence
            evidence = [release_evidence(reader) for reader in readers]
        closed = writer is None
        if writer is not None:
            try:
                writer.close()
                closed = True
            except BaseException:
                closed = False
        observation = {
            'mode': mode, 'lifecycle': TRACE.snapshot(),
            'injection': 'synthetic_discovery_and_memory_candidates_only',
            'calls': calls, **state,
            'source_bytes_unchanged': stable, 'fixture_writer_closed': closed,
            'reader_count': len(readers), 'reader_release_evidence': evidence,
            'all_reader_release_confirmed': all(all(row.values()) for row in evidence),
            'normal_key_loader': bool(provider_instances) and all(
                provider.state.get('key_loader_kind') == 'normal' for provider in provider_instances),
            'preparation_release_confirmed': bool(provider_instances) and all(
                provider.state.get('preparation_release_confirmed') is True for provider in provider_instances),
            'wrapper_error_kind': wrapper_error_kind, 'LINE_read': False,
        }
        # Invalid/unproved starts cannot create even local fixture observations.
        if state['settings_target_verified'] and calls['factory']:
            (directory / 'fixture_observation.json').write_text(
                json.dumps(observation, indent=2), encoding='utf-8')
    return code


if __name__ == '__main__':
    try:
        code = main()
    except BaseException:
        code = 3
    raise SystemExit(code)
