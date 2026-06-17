import pytest
from unittest.mock import patch
from line_mcp_server import _parse_iso8601, _load_settings, _find_edb_path


def test_parse_iso8601_valid():
    ts = _parse_iso8601("2026-06-15T00:00:00+08:00")
    assert isinstance(ts, int) and ts > 0


def test_parse_iso8601_rejects_natural_language():
    with pytest.raises(ValueError, match="ISO 8601"):
        _parse_iso8601("2天前")


def test_parse_iso8601_rejects_missing_timezone():
    with pytest.raises(ValueError, match="timezone"):
        _parse_iso8601("2026-06-15T00:00:00")


def test_load_settings_returns_defaults_when_missing():
    with patch('builtins.open', side_effect=FileNotFoundError):
        s = _load_settings()
    assert s["media_mode"] == "placeholder"
    assert s["url_extraction"] is True
    assert s["timezone"] == "Asia/Taipei"


def test_find_edb_path_returns_none_when_dir_empty(tmp_path):
    assert _find_edb_path(str(tmp_path)) is None
