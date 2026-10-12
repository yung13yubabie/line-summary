"""Retain real synthetic WAL writers without changing data or probe behavior."""
from pathlib import Path

import apsw
import pytest


def retain_hot_wal(path, key, addfinalizer):
    """Open a completed synthetic DB and retain its genuine WAL/SHM lifecycle."""
    writer = apsw.Connection(str(path))
    # Register before setup so failures also close the owned writer.
    addfinalizer(writer.close)
    writer.execute("PRAGMA cipher='aes128cbc'")
    writer.execute("PRAGMA key='" + key + "'")
    version = next(writer.execute('PRAGMA user_version'))[0]
    assert next(writer.execute('PRAGMA journal_mode=WAL'))[0] == 'wal'
    writer.execute('PRAGMA wal_autocheckpoint=0')
    # Preserve the existing metadata value while publishing a real WAL frame.
    # No synthetic table, row, replacement SHM, or mocked VFS is introduced.
    writer.execute('PRAGMA user_version=' + str(version))
    assert next(writer.execute('PRAGMA user_version'))[0] == version
    assert Path(str(path) + '-wal').stat().st_size > 32
    assert Path(str(path) + '-shm').is_file()
    return writer


@pytest.fixture(autouse=True)
def adapt_closed_database_factories_to_hot_wal(request, monkeypatch):
    factories = {
        'test_candidate_verification_contract.py': 'encrypted_database',
        'test_identity_candidate_integration.py': 'make_db',
        'test_independent_contract.py': 'create_db',
        'test_slice_interactions.py': 'encrypted_db',
    }
    name = factories.get(Path(str(request.node.path)).name)
    if name is None:
        return
    original = getattr(request.module, name)

    def real_hot_database(tmp_path, key):
        path = original(tmp_path, key)
        retain_hot_wal(path, key, request.addfinalizer)
        return path

    monkeypatch.setattr(request.module, name, real_hot_database)
