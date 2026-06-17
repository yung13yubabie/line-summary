from unittest.mock import MagicMock, patch
import pytest
from key_extractor import find_line_pid, validate_key_format, _scan_memory_regions


def test_find_line_pid_returns_none_when_not_running():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(stdout="INFO: No tasks running\n")
        assert find_line_pid() is None


def test_find_line_pid_parses_pid_from_csv():
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            stdout='"LINE.exe","9999","Console","1","150 K"\n'
        )
        assert find_line_pid() == 9999


def test_validate_key_format_accepts_32_char_hex():
    assert validate_key_format("a" * 32) is True


def test_validate_key_format_accepts_64_char_hex():
    assert validate_key_format("b" * 64) is True


def test_validate_key_format_rejects_wrong_length():
    assert validate_key_format("a" * 10) is False


def test_validate_key_format_rejects_non_hex():
    assert validate_key_format("z" * 32) is False


def test_scan_memory_regions_returns_empty_when_open_fails():
    with patch('ctypes.windll') as mock_windll:
        mock_windll.kernel32.OpenProcess.return_value = None
        result = _scan_memory_regions(pid=9999)
        assert result == []
