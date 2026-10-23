"""Report the actual synthetic runner and reject a runtime/lock version mismatch."""
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import struct

import apsw


def test_installed_apsw_matches_lock_and_reports_runner(capsys):
    lock = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text("utf-8")
    pinned = re.findall(r"(?m)^apsw-sqlite3mc==([^\s\\]+)", lock)
    assert len(pinned) == 1
    installed = importlib.metadata.version("apsw-sqlite3mc")
    assert installed == pinned[0]
    assert apsw.apswversion() == installed
    # No paths, hostnames, account data, environment variables or database reads.
    evidence = {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "bits": struct.calcsize("P") * 8,
        "python": platform.python_version(),
        "pip": importlib.metadata.version("pip"),
        "apsw_sqlite3mc": installed,
        "sqlite": apsw.sqlitelibversion(),
    }
    with capsys.disabled():
        print("SYNTHETIC_RUNTIME " + json.dumps(evidence, sort_keys=True))
