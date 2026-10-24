"""Create repo-local Codex MCP config; never change global Codex settings.

Run with the Python environment that has requirements.txt installed.
"""
import json
from pathlib import Path
import sys
import tomllib


def configure_codex(repo_root: Path, python: Path | None = None) -> Path:
    repo_root = repo_root.resolve()
    server = repo_root / "line_mcp_server.py"
    if not server.is_file():
        raise FileNotFoundError(f"LINE MCP server not found: {server}")
    executable = (python or Path(sys.executable)).resolve()
    line = {
        "command": str(executable),
        "args": ["-u", str(server)],
        "cwd": str(repo_root),
        "startup_timeout_sec": 30,
        # Key extraction happens on the first tool call, not MCP initialization.
        "tool_timeout_sec": 300,
        "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    }
    config = repo_root / ".codex" / "config.toml"
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    try:
        existing = tomllib.loads(original)
    except tomllib.TOMLDecodeError:
        raise ValueError(f"Invalid TOML; existing config was not changed: {config}") from None
    servers = existing.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"mcp_servers must be a TOML table: {config}")
    if "line" in servers:
        if servers["line"] == line:
            return config
        raise ValueError(
            f"An existing line MCP config differs; update it manually: {config}"
        )
    # JSON strings/arrays use escaping that is also valid TOML. In particular,
    # Windows backslashes, quotes, spaces and Chinese paths must round-trip.
    block = "\n[mcp_servers.line]\n"
    for key, value in line.items():
        if key != "env":
            block += f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
    block += "\n[mcp_servers.line.env]\n"
    for key, value in line["env"].items():
        block += f"{key} = {json.dumps(value)}\n"
    updated = original + "\n" + block
    # Validate the entire result before touching disk. Preserve unrelated settings
    # and comments verbatim rather than reserializing the user's config.
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError:
        raise ValueError(
            "Cannot extend existing MCP tables. Convert inline tables to "
            f"[mcp_servers.<name>] sections or register line manually; config was not changed: {config}"
        ) from None
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(updated, encoding="utf-8")
    return config


def main() -> int:
    try:
        config = configure_codex(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as exc:
        print(f"Codex setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Codex LINE MCP config ready: {config}")
    print("This registers the transport only; configure LINE scope in settings.json yourself.")
    print("Restart Codex in this project, then check /mcp or codex mcp get line.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
