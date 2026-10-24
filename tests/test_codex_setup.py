"""Codex configuration must survive Windows paths and preserve user settings."""
import tomllib
from pathlib import Path
import subprocess
import sys

import pytest

from tools.setup_codex import configure_codex


def test_config_uses_absolute_paths_and_allows_slow_first_call(tmp_path):
    (tmp_path / "line_mcp_server.py").touch()
    python = tmp_path / "Python's 中文" / "python.exe"
    config = configure_codex(tmp_path, python)
    server = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["line"]
    assert server == {
        "command": str(python.resolve()),
        "args": ["-u", str((tmp_path / "line_mcp_server.py").resolve())],
        "cwd": str(tmp_path.resolve()),
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 300,
        "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }


def test_config_preserves_other_settings_and_is_idempotent(tmp_path):
    (tmp_path / "line_mcp_server.py").touch()
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = '# 保留自訂設定\nmodel_reasoning_effort = "high"\n'
    config.write_text(original, encoding="utf-8")
    configure_codex(tmp_path)
    once = config.read_text(encoding="utf-8")
    assert once.startswith(original)
    assert tomllib.loads(once)["model_reasoning_effort"] == "high"
    configure_codex(tmp_path)
    assert config.read_text(encoding="utf-8") == once


@pytest.mark.parametrize("original", [
    '[mcp_servers.line]\ncommand = "my-custom-python"\n',
    'invalid = [\n',
])
def test_config_conflict_or_invalid_toml_is_not_overwritten(tmp_path, original):
    (tmp_path / "line_mcp_server.py").touch()
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        configure_codex(tmp_path)
    assert config.read_text(encoding="utf-8") == original


def test_missing_server_does_not_create_config(tmp_path):
    with pytest.raises(FileNotFoundError):
        configure_codex(tmp_path)
    assert not (tmp_path / ".codex").exists()


def test_inline_mcp_table_is_preserved_with_actionable_error(tmp_path):
    (tmp_path / "line_mcp_server.py").touch()
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = 'mcp_servers = { other = { command = "python" } }\n'
    config.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="Convert inline tables"):
        configure_codex(tmp_path)
    assert config.read_text(encoding="utf-8") == original


def test_cli_configures_its_repo_from_another_working_directory(tmp_path):
    repo = tmp_path / "中文專案"
    scripts = repo / "tools"
    scripts.mkdir(parents=True)
    script = scripts / "setup_codex.py"
    source = Path(__file__).resolve().parents[1] / "tools" / "setup_codex.py"
    script.write_bytes(source.read_bytes())
    (repo / "line_mcp_server.py").touch()
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(script)], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Codex LINE MCP config ready:" in result.stdout
    config = tomllib.loads((repo / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["line"]["cwd"] == str(repo.resolve())
    assert config["mcp_servers"]["line"]["command"] == str(Path(sys.executable).resolve())
