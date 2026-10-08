"""Build an allowlisted, inert-root demo ZIP; never include account/autoload files."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

BASE_COMMIT = "11cdfb30b86728d53ef063974966e0ca3e52b039"
FILES = (
    "plugin_adapter.py", "plugin_runtime.py", "mock_privacy.py", "db_reader.py", "safety.py",
    "plugin_settings.example.json", "requirements.txt", "requirements-test.txt",
    "requirements.in", "requirements-test.in", "requirements-platform.in",
    "docs/PLUGIN_PREPARATION.md", "docs/PRIVACY_READINESS.md", "docs/DETAILED_USAGE_GUIDE.md",
    "adapters/README.md", "adapters/chatgpt/README.md", "adapters/gemini/README.md",
    "adapters/claude/README.md", "adapters/claude/desktop.mock.example.json",
    "adapters/grok/README.md", "adapters/deepseek/README.md", "adapters/qwen/README.md",
    "adapters/qwen/settings.mock.example.json", "adapters/deepseek/harness.mock.example.cordis.yml",
    "plugin/README.md", "plugin/DEMO_README.md", "plugin/mock-client.example.json",
    "plugin/skills/line-summary-mock/SKILL.md", "plugin/skill-package-spec.json",
    "tools/build_mock_bundle.py", "tools/build_skill_bundle.py", "tools/mock_cli.py",
    "tests/test_message_search.py", "tests/test_plugin_protocol.py",
    "tests/test_plugin_service.py", "tests/test_plugin_sanitizers.py",
    "tests/test_mock_privacy.py", "tests/test_mock_cli.py", "tests/test_host_templates.py",
    "tests/test_acceptance_lifecycle.py",
)
FORBIDDEN_NAMES = {".mcp.json", "settings.json", "plugin_settings.json",
                   "line_mcp_server.py", "key_extractor.py", "conftest.py", "test_integration.py"}


def build(source: Path, destination: Path):
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("Output already exists; choose a new generated bundle path")
    entries = {}
    for relative in FILES:
        path = source / relative
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("Refusing source symlink or path outside source root")
        if Path(relative).name in FORBIDDEN_NAMES or any(part in {".git", ".claude", ".venv"} for part in Path(relative).parts):
            raise ValueError("Unsafe bundle source path")
        entries[relative] = path.read_bytes()
    entries["README.md"] = entries["plugin/DEMO_README.md"]
    entries["pytest.ini"] = b"[pytest]\ntestpaths = tests\naddopts = --strict-markers\n"
    manifest = {
        "purpose": "isolated synthetic-only MCP prototype; not a platform installer",
        "base_commit": BASE_COMMIT, "real_data_implemented": False,
        "auto_load_configuration": False,
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, data in sorted(entries.items())],
    }
    entries["BUNDLE-MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            # Fixed timestamps make equal source inputs reproducible.
            info = zipfile.ZipInfo(name, date_time=(2026,10,8,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive validation failed")
        for name, data in entries.items():
            if archive.read(name) != data:
                raise ValueError("Archive readback mismatch")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parents[1], args.output)
    print(f"Created synthetic-only bundle with {len(result['files'])} verified source files")


if __name__ == "__main__":
    main()
