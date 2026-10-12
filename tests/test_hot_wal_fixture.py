"""The storage adapter preserves existing schema, data, and user metadata."""
from pathlib import Path
import hashlib

import apsw
import pytest

from db_reader import _open_encrypted, probe_key
from tests.hot_wal_fixture import retain_hot_wal


@pytest.mark.parametrize('key', ['Ab3d' * 8, 'Ac7e' * 16])
def test_hot_adapter_preserves_data_and_native_readonly_probe(tmp_path, request, key):
    path = tmp_path / 'fixture-contract.edb'
    writer = apsw.Connection(str(path))
    writer.execute("PRAGMA cipher='aes128cbc'")
    writer.execute("PRAGMA key='" + key + "'")
    writer.execute('CREATE TABLE artificial(id INTEGER PRIMARY KEY, value TEXT)')
    writer.execute("INSERT INTO artificial VALUES(7, 'synthetic value')")
    writer.execute('PRAGMA user_version=19')
    schema = list(writer.execute('SELECT type,name,tbl_name,sql FROM sqlite_master'))
    writer.close()
    retained = retain_hot_wal(path, key, request.addfinalizer)
    assert list(retained.execute('SELECT type,name,tbl_name,sql FROM sqlite_master')) == schema
    assert list(retained.execute('SELECT * FROM artificial')) == [(7, 'synthetic value')]
    assert next(retained.execute('PRAGMA user_version'))[0] == 19
    hashes = lambda: {s:hashlib.sha256(Path(str(path)+s).read_bytes()).hexdigest() for s in ('','-wal','-shm')}
    before = hashes()
    assert probe_key(str(path), key)
    assert not probe_key(str(path), key.lower())
    reader = _open_encrypted(str(path), key)
    try:
        assert list(reader.execute('SELECT * FROM artificial')) == [{'id':7,'value':'synthetic value'}]
    finally:
        reader.close()
    assert hashes() == before
