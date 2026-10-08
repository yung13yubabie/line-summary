import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mock_packager',ROOT/'tools/build_mock_bundle.py')
packager=importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def test_bundle_has_only_inert_mock_source_and_no_raw_autoload(tmp_path):
    output=tmp_path/'demo.zip'
    manifest=packager.build(ROOT,output)
    with zipfile.ZipFile(output) as archive:
        names=archive.namelist()
        assert all(Path(name).name not in packager.FORBIDDEN_NAMES for name in names)
        assert not any(part in {'.git','.claude','.venv'} for name in names for part in Path(name).parts)
        assert not any(name.endswith(('.db','.sqlite','.edb','.key')) for name in names)
        assert 'plugin_adapter.py' in names and 'README.md' in names
        assert 'tests/test_acceptance_lifecycle.py' in names
        settings=json.loads(archive.read('plugin_settings.example.json'))
        assert settings['mode']=='disabled'
        for name in ['plugin/mock-client.example.json','adapters/claude/desktop.mock.example.json']:
            config=json.loads(archive.read(name))
            for server in config['mcpServers'].values():
                assert any(arg.endswith('plugin_adapter.py') for arg in server['args'])
                assert server['args'][-2:]==['--mode','mock']
        assert manifest['real_data_implemented'] is False
        assert manifest['auto_load_configuration'] is False


def test_bundle_is_reproducible_and_will_not_overwrite(tmp_path):
    first,second=tmp_path/'one.zip',tmp_path/'two.zip'
    packager.build(ROOT,first);packager.build(ROOT,second)
    assert first.read_bytes()==second.read_bytes()
    with pytest.raises(FileExistsError):packager.build(ROOT,first)
