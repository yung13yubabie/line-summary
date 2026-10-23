"""Pure wrapper boundaries: fake module APIs, no native launch or database open.

When copied into schema-mcp/tests, the candidate root is discovered normally.
For an independent test checkout, set SYNTHETIC_BOUNDARY_ROOT explicitly.
"""
import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(os.environ.get('SYNTHETIC_BOUNDARY_ROOT',
                          str(Path(__file__).resolve().parents[1]))).resolve()


def load_file(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticWorkerBoundaries(unittest.TestCase):
    def setUp(self):
        self.worker = load_file('_boundary_synthetic_worker', 'synthetic_worker.py')
        self.gate = load_file('_boundary_synthetic_gate', 'synthetic_gate.py')
        # This source module has only stdlib imports and no top-level main call.
        # Only its real lexical Windows-path policy is reused below.
        self.lexical_child = load_file('_boundary_configured_child',
                                       'source/configured_child.py')
        self.temporary = tempfile.TemporaryDirectory(prefix='synthetic_wrapper_unit_')
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name).resolve()

    def owned(self, name='owned'):
        session = self.scratch / name
        directory = session / 'operation' / 'entry'
        directory.mkdir(parents=True)
        return session, directory

    def fake_worker_modules(self, session, requested):
        child = ModuleType('configured_child')
        child.explicit_local_file = self.lexical_child.explicit_local_file
        child.read_start = Mock(name='original_read_start', return_value=str(requested))
        provider = ModuleType('source_provider')
        provider.ConfiguredSourceProvider = Mock(name='original_provider')
        provider._kind = lambda error: type(error).__name__
        launch_policy = ModuleType('fixture_launch_policy')
        launch_policy.fixed_worker_command = Mock(name='unused_command_matcher')
        apsw = ModuleType('apsw')
        apsw.Connection = Mock(name='forbidden_apsw_connection',
                               side_effect=AssertionError('native DB API reached'))
        reader = ModuleType('readonly_client')
        reader.WorkerConnection = Mock(name='forbidden_reader',
                                       side_effect=AssertionError('native reader reached'))
        modules = {'configured_child': child, 'source_provider': provider,
                   'fixture_launch_policy': launch_policy, 'apsw': apsw,
                   'readonly_client': reader}
        for name in ('key_extractor', 'db_reader', 'schema_probe'):
            modules[name] = ModuleType(name)
        return SimpleNamespace(child=child, provider=provider, apsw=apsw,
                               reader=reader, modules=modules)

    def run_worker(self, fake, session, directory, *, deny_factory_imports=False):
        captured = []
        forbidden_imports = []
        original_read_start = fake.child.read_start
        original_provider = fake.provider.ConfiguredSourceProvider

        def fake_child_main():
            try:
                fake.child.read_start(io.BytesIO(b'ignored synthetic private frame\n'))
                fake.provider.ConfiguredSourceProvider()
            except BaseException as error:
                captured.append(error)
                raise
            raise AssertionError('boundary should reject before provider construction')

        fake.child.main = fake_child_main
        original_import = builtins.__import__
        native_names = {'apsw', 'key_extractor', 'db_reader', 'schema_probe',
                        'readonly_client'}

        def guarded_import(name, *args, **kwargs):
            if deny_factory_imports and name in native_names:
                forbidden_imports.append(name)
                raise AssertionError('factory dependency imported before owned start')
            return original_import(name, *args, **kwargs)

        arguments = [str(ROOT / 'synthetic_worker.py'), '--case', 'success',
                     '--resource', str(directory / 'synthetic_resource.bin'),
                     '--run-id', 'a' * 32, '--deadline', '123.0',
                     '--fixture-mode', 'success', '--synthetic-root', str(session)]
        with (patch.dict(sys.modules, fake.modules),
              patch.object(sys, 'path', sys.path[:]),
              patch.object(sys, 'argv', arguments),
              patch.object(builtins, '__import__', guarded_import)):
            result = self.worker.main()
            self.assertEqual(sys.argv, arguments)
        self.assertIs(fake.child.read_start, original_read_start)
        self.assertIs(fake.provider.ConfiguredSourceProvider, original_provider)
        original_provider.assert_not_called()
        fake.apsw.Connection.assert_not_called()
        fake.reader.WorkerConnection.assert_not_called()
        self.assertEqual(result, 3)
        self.assertEqual(len(captured), 1)
        self.assertIs(type(captured[0]), ValueError)
        self.assertEqual(forbidden_imports, [])
        return captured[0]

    def test_settings_accept_only_exact_session_file(self):
        session, directory = self.owned()
        other_session, _ = self.owned('other_session')
        expected = session / 'provider_settings.cfg'
        with patch.dict(sys.modules, {'configured_child': self.lexical_child}):
            self.assertEqual(self.worker._synthetic_settings_target(
                str(expected), directory, session), expected)
            for rejected in (directory / 'provider_settings.cfg',
                             session / 'sibling_settings.cfg',
                             other_session / 'provider_settings.cfg'):
                with self.subTest(target=rejected.name, parent=rejected.parent.name):
                    with self.assertRaisesRegex(ValueError, 'settings target rejected'):
                        self.worker._synthetic_settings_target(str(rejected), directory, session)
        self.assertFalse(expected.exists())

    def test_invalid_private_settings_stops_before_factory_imports(self):
        session, directory = self.owned()
        fake = self.fake_worker_modules(session, directory / 'provider_settings.cfg')
        error = self.run_worker(fake, session, directory, deny_factory_imports=True)
        self.assertEqual(str(error), 'synthetic settings target rejected')
        self.assertEqual(list(directory.iterdir()), [])
        self.assertFalse((session / 'provider_settings.cfg').exists())

    def test_missing_backend_marker_stops_before_apsw_and_reader_imports(self):
        session, directory = self.owned()
        fake = self.fake_worker_modules(session, session / 'provider_settings.cfg')
        error = self.run_worker(fake, session, directory, deny_factory_imports=True)
        self.assertEqual(str(error), 'synthetic fixture requires owned start')
        self.assertEqual(list(directory.iterdir()), [])
        self.assertFalse((session / 'provider_settings.cfg').exists())

    def test_preexisting_fixture_or_sidecar_is_never_overwritten(self):
        payload = b'synthetic unit bytes; deliberately not a database'
        for index, name in enumerate(base + suffix
                                     for base in ('provider_fixture.edb', 'synthetic_origin.edb')
                                     for suffix in ('', '-wal', '-shm')):
            with self.subTest(existing=name):
                session, directory = self.owned('preexisting_' + str(index))
                (directory / 'configured_backend_started.json').write_text(
                    '{"after_start":true}', encoding='utf-8')
                existing = directory / name
                existing.write_bytes(payload)
                fake = self.fake_worker_modules(session, session / 'provider_settings.cfg')
                error = self.run_worker(fake, session, directory)
                self.assertEqual(str(error), 'synthetic fixture must be fresh')
                self.assertEqual(existing.read_bytes(), payload)
                self.assertFalse((session / 'provider_settings.cfg').exists())
                observation = json.loads((directory / 'fixture_observation.json').read_text('utf-8'))
                self.assertFalse(observation['fixture_created'])
                self.assertEqual(observation['reader_count'], 0)
                self.assertEqual(observation['wrapper_error_kind'], 'ValueError')
                generated = {path.name for path in directory.iterdir()}
                self.assertEqual(generated, {name, 'configured_backend_started.json',
                                             'fixture_observation.json'})

    def test_gate_rejects_tampered_fixed_argv_and_restores_api(self):
        session, directory = self.owned()
        operation = directory.parent
        site = self.scratch / 'runtime' / 'Lib' / 'site-packages'
        launches = []
        rejected = []
        process = object()
        options = {'stdin': object(), 'stdout': object(), 'creationflags': 123}

        def original_create(owner, argv, **kwargs):
            launches.append((owner, list(argv), kwargs))
            return process

        owned_job = type('FakeOwnedJob', (), {'create': original_create})
        runner = ModuleType('runner')
        runner.OwnedJob = owned_job
        entry_gate = ModuleType('configured_entry_gate')

        def original_load(value):
            self.assertEqual(value, str(site))
            self.assertIs(owned_job.create, original_create)
            return object()

        entry_gate._load_entry = original_load

        def fake_gate_main():
            self.assertEqual(len(sys.argv), 3)
            entry_gate._load_entry(str(site))
            owner = owned_job()
            argv = [sys.executable, '-I', '-B', str(ROOT / 'source/configured_child.py'),
                    '--case', 'success', '--resource',
                    str(operation / 'entry/synthetic_resource.bin'),
                    '--run-id', 'a' * 32, '--deadline', '123.0']
            changes = [(0, 'arbitrary-python'), (1, '-E'),
                       (3, str(ROOT / 'source/arbitrary_child.py')),
                       (5, 'wrong_key'), (7, str(session / 'outside_resource.bin')),
                       (9, 'not-a-run-id'), (11, 'nan'), (11, 'inf')]
            tampered = []
            for position, value in changes:
                command = argv[:]
                command[position] = value
                tampered.append(command)
            tampered.append(argv + ['--settings', 'synthetic-private-sentinel'])
            for command in tampered:
                with self.assertRaises(ValueError):
                    owner.create(command, **options)
                rejected.append(True)
                self.assertEqual(launches, [])
            self.assertIs(owner.create(argv, **options), process)
            self.assertEqual(argv[3], str(ROOT / 'source/configured_child.py'))
            return 0

        entry_gate.main = fake_gate_main
        arguments = [str(ROOT / 'synthetic_gate.py'), str(operation), str(site),
                     'success', str(session)]
        with (patch.dict(sys.modules, {'configured_child': self.lexical_child,
                                      'configured_entry_gate': entry_gate, 'runner': runner}),
              patch.object(sys, 'path', sys.path[:]), patch.object(sys, 'argv', arguments)):
            self.assertEqual(self.gate.main(), 0)
            self.assertEqual(sys.argv, arguments)
        self.assertEqual(len(rejected), 9)
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][1][3], str(ROOT / 'synthetic_worker.py'))
        self.assertEqual(launches[0][1][-4:],
                         ['--fixture-mode', 'success', '--synthetic-root', str(session)])
        self.assertEqual(launches[0][2], options)
        self.assertIs(entry_gate._load_entry, original_load)
        self.assertIs(owned_job.create, original_create)
        observation = json.loads((operation / 'synthetic_gate_observation.json').read_text('utf-8'))
        self.assertTrue(observation['after_owned_start'])
        self.assertEqual(observation['fixed_worker_launches'], 1)


if __name__ == '__main__':
    unittest.main()
