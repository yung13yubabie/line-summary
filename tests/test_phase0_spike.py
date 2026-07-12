import importlib.util
from pathlib import Path


def load_phase0():
    path = Path(__file__).resolve().parents[1] / "spike" / "phase0.py"
    spec = importlib.util.spec_from_file_location("phase0_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_find_edb_path_excludes_media_prefixes_and_picks_largest(tmp_path):
    phase0 = load_phase0()
    stem = "qw0123456789abcdef0123456789abcd"
    main = tmp_path / f"{stem}.edb"
    main.write_bytes(b"x" * 5000)  # largest main DB
    (tmp_path / f"album_{stem}.edb").write_bytes(b"x" * 9000)  # bigger but excluded
    (tmp_path / f"keep_{stem}.edb").write_bytes(b"x" * 8000)
    (tmp_path / f"chatStats_{stem}.edb").write_bytes(b"x" * 7000)
    (tmp_path / f"{stem}.edb-wal").write_bytes(b"x" * 9999)  # sidecar ignored

    assert phase0.find_edb_path(str(tmp_path)) == str(main)


def test_find_edb_path_returns_none_when_empty(tmp_path):
    phase0 = load_phase0()
    assert phase0.find_edb_path(str(tmp_path)) is None


def test_main_success_flow(monkeypatch):
    phase0 = load_phase0()
    monkeypatch.setattr(phase0, "find_line_pid", lambda: 1234)
    monkeypatch.setattr(phase0, "find_edb_path", lambda: "C:/fake/main.edb")
    monkeypatch.setattr(phase0.os.path, "getsize", lambda _p: 1_000_000)
    monkeypatch.setattr(
        phase0, "scan_memory_for_candidates",
        lambda _pid: {"pass32": ["a" * 32], "raw64": [], "stats": {
            "regions": 10, "skipped_big": 0, "read_failed": 0,
            "u16_only_32": 1, "u16_only_64": 0}},
    )
    captured = {}

    def fake_decrypt(edb, cands):
        captured["edb"] = edb
        captured["cands"] = cands
        return ("a" * 32, "aes256cbc", "pass")

    monkeypatch.setattr(phase0, "try_decrypt_edb", fake_decrypt)
    monkeypatch.setattr(
        phase0, "query_schema",
        lambda _e, _k, _s, _m: ["message: cols=[], rows=42"],
    )

    assert phase0.main() == 0
    assert captured["edb"] == "C:/fake/main.edb"
    assert captured["cands"]["pass32"] == ["a" * 32]


def test_main_exhaustive_miss_returns_2(monkeypatch):
    phase0 = load_phase0()
    monkeypatch.setattr(phase0, "find_line_pid", lambda: 1234)
    monkeypatch.setattr(phase0, "find_edb_path", lambda: "C:/fake/main.edb")
    monkeypatch.setattr(phase0.os.path, "getsize", lambda _p: 1_000_000)
    monkeypatch.setattr(
        phase0, "scan_memory_for_candidates",
        lambda _pid: {"pass32": ["a" * 32], "raw64": [], "stats": {
            "regions": 10, "skipped_big": 0, "read_failed": 0,
            "u16_only_32": 0, "u16_only_64": 0}},
    )
    monkeypatch.setattr(phase0, "try_decrypt_edb", lambda _e, _c: None)

    # 2 = exhaustive miss, distinct from 1 = environment failure (LINE off / no .edb)
    assert phase0.main() == 2


def test_main_fails_when_line_not_running(monkeypatch):
    phase0 = load_phase0()
    monkeypatch.setattr(phase0, "find_line_pid", lambda: None)
    assert phase0.main() == 1
