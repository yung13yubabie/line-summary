"""Actual scanner/identity policy with explicit fake preflight and probes.

These tests neither open encrypted storage nor claim native Windows validation.
The existing native integration cases are retained separately.
"""
from types import SimpleNamespace

import pytest

import db_reader as db
import key_extractor as ke
from tests import test_candidate_verification_contract as candidates
from tests import test_identity_candidate_integration as identities


@pytest.mark.parametrize("scenario", ["odd_utf16_gap", "ordered_dedupe", "all_rejected"])
def test_actual_scanner_precedes_fake_candidate_verification(monkeypatch, scenario):
    valid, wrong = candidates.KEY64, candidates.KEY32.lower()
    if scenario == "odd_utf16_gap":
        segments = candidates.interrupted_segments(
            monkeypatch, b"!" + (valid + " " + wrong + " ").encode("utf-16le"),
            "utf-16le", "address_gap")
        expected = [valid]
    else:
        payload = (wrong + " " + wrong + " " + wrong + " " + valid + " ").encode()
        segments = [candidates.Segment(payload[i:i + 17]) for i in range(0, len(payload), 17)]
        expected = [wrong, valid]
    memory = candidates.SyntheticMemory(segments)
    events = []
    monkeypatch.setattr(ke, "find_line_pid", lambda: 713)
    monkeypatch.setattr(ke, "_load_kernel32", lambda: memory)
    monkeypatch.setattr(ke, "open_verified_line_process", lambda *args: SimpleNamespace(
        handle=713, close=lambda: memory.CloseHandle(713)))

    def preflight(path):
        assert path == "synthetic-only.edb" and memory.reads == []
        events.append("preflight")

    def probe(path, candidate):
        assert path == "synthetic-only.edb" and events[0] == "preflight"
        assert memory.closed == [713] and memory.reads
        events.append(candidate)
        return scenario != "all_rejected" and candidate == valid

    monkeypatch.setattr(db, "preflight_db_access", preflight)
    monkeypatch.setattr(db, "probe_key", probe)
    if scenario == "all_rejected":
        with pytest.raises(RuntimeError):
            ke.extract_key("synthetic-only.edb", require_consent=False)
    else:
        assert ke.extract_key("synthetic-only.edb", require_consent=False) == valid
    assert events == ["preflight", *expected]
    assert memory.closed == [713]


def test_preflight_refusal_never_enters_scanner_or_probe(monkeypatch):
    calls = []

    def refused(path):
        calls.append("preflight")
        raise db.DbAccessError("synthetic-private-sentinel")

    def forbidden(*args):
        pytest.fail("preflight refusal must precede all scanning and probes")

    monkeypatch.setattr(ke, "find_line_pid", lambda: 713)
    monkeypatch.setattr(db, "preflight_db_access", refused)
    monkeypatch.setattr(ke, "_scan_memory_regions", forbidden)
    monkeypatch.setattr(db, "probe_key", forbidden)
    with pytest.raises(RuntimeError, match="Cannot access the LINE database") as caught:
        ke.extract_key("synthetic-only.edb", require_consent=False)
    assert calls == ["preflight"]
    assert "synthetic-private-sentinel" not in str(caught.value)


@pytest.fixture
def fake_storage(monkeypatch, tmp_path):
    """Reuse real refusal assertions; storage and probes are forbidden here."""
    calls = []

    def preflight(path):
        assert path == str(tmp_path / "never-created.edb")
        calls.append("preflight")

    def forbidden_probe(*args):
        calls.append("forbidden_probe")
        raise AssertionError("refusal must finish before candidate probing")

    monkeypatch.setattr(db, "preflight_db_access", preflight)
    monkeypatch.setattr(db, "probe_key", forbidden_probe)
    monkeypatch.setattr(identities, "make_db", lambda *args: tmp_path / "never-created.edb")
    yield
    assert calls == ["preflight"]
    assert not (tmp_path / "never-created.edb").exists()


@pytest.mark.parametrize("failure", ["signature", "publisher", "product", "pid_reuse"])
def test_identity_refusal_before_probe(monkeypatch, tmp_path, fake_storage, failure):
    identities.test_failed_identity_never_reaches_stream_or_candidate_probe(
        monkeypatch, tmp_path, failure)


@pytest.mark.parametrize("failed_handle", ["scan", "image_file", "anchor"])
def test_cleanup_refusal_before_probe(monkeypatch, tmp_path, fake_storage, failed_handle):
    identities.test_cleanup_failure_prevents_probing_completed_candidates(
        monkeypatch, tmp_path, failed_handle)


def test_midstream_refusal_before_probe(monkeypatch, tmp_path, fake_storage):
    identities.test_midstream_exception_closes_retained_handles_without_probing(monkeypatch, tmp_path)
