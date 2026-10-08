"""Offline structural checks only: no host login, install, config write or model call.

These assertions check our bounded templates against documented field shapes;
they do not execute a vendor schema validator or prove actual-host acceptance.
"""
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("skill_packager", ROOT / "tools/build_skill_bundle.py")
skill_packager = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skill_packager)

TEMPLATES = (
    "plugin/mock-client.example.json",
    "adapters/claude/desktop.mock.example.json",
    "adapters/qwen/settings.mock.example.json",
)


def assert_mock_command(server):
    assert isinstance(server["command"], str)
    assert "REPLACE_WITH" in server["command"]
    assert server["args"][-2:] == ["--mode", "mock"]
    assert len(server["args"]) == 3
    assert server["args"][0].endswith("plugin_adapter.py")
    assert "ISOLATED_MOCK" in server["args"][0]
    assert not set(server) & {"url", "httpUrl", "headers", "oauth", "env"}
    assert server.get("trust") in (None, False)
    encoded = json.dumps(server).lower()
    for prohibited in ("line_mcp_server", "key_extractor", "api_key", "api-key", "token", "password", "db_path", "https://", "http://"):
        assert prohibited not in encoded
    if "cwd" in server:
        assert "ISOLATED_MOCK" in server["cwd"]


@pytest.mark.parametrize("relative", TEMPLATES)
def test_stdio_templates_are_inert_explicit_mock_json(relative):
    config = json.loads((ROOT / relative).read_text())
    assert set(config) <= {"mcpServers", "mcp"}
    assert len(config["mcpServers"]) == 1
    server = next(iter(config["mcpServers"].values()))
    assert set(server) <= {"command", "args", "cwd", "trust"}
    assert_mock_command(server)


def test_qwen_preserves_confirmation_and_bounds_mcp_names():
    config = json.loads((ROOT / TEMPLATES[2]).read_text())
    assert config["mcp"] == {"allowed": ["line-summary-mock"]}
    assert config["mcpServers"]["line-summary-mock"]["trust"] is False


def test_harness_overlay_is_json_compatible_yaml_not_generic_mcp_json():
    # JSON is a YAML subset, avoiding an added YAML parser dependency in the
    # prototype environment; a separate local PyYAML readback is also recorded.
    overlay = json.loads((ROOT / "adapters/deepseek/harness.mock.example.cordis.yml").read_text())
    assert len(overlay) == 1 and set(overlay[0]) == {"insert"}
    assert len(overlay[0]["insert"]) == 1
    entry = overlay[0]["insert"][0]
    assert set(entry) == {"id", "name", "config"}
    assert entry["id"] == "mcp-line-summary-mock"
    assert entry["name"] == "@deepseek-ai/dsh-mcp-client"
    server = entry["config"]
    assert set(server) == {"serverName", "transport", "command", "args", "cwd", "reconnect"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", server["serverName"])
    assert server["transport"] == "stdio"
    assert server["reconnect"] == {"enabled": False}
    assert_mock_command(server)


def test_skill_spec_records_distinct_host_layouts_and_unrun_status():
    spec = json.loads((ROOT / skill_packager.SPEC_SOURCE).read_text())
    assert spec["actual_host_acceptance"] == "NOT_RUN"
    assert spec["checked_on"] == "2026-10-08"
    assert spec["files"] == [{"source": skill_packager.SKILL_SOURCE, "archive_path": skill_packager.SKILL_ARCHIVE_PATH}]
    assert all(route["actual_test"] == "NOT_RUN" for route in spec["host_routes"])
    routes = {route["host"]: route for route in spec["host_routes"]}
    assert routes["Claude Skills"]["layout"] == "line-summary-mock/SKILL.md"
    assert routes["Perplexity Computer"]["layout"] == "SKILL.md at ZIP root, or a direct .md file"
    assert all(route["source"].startswith("https://") for route in routes.values())


@pytest.mark.parametrize("layout, member", [("directory", "line-summary-mock/SKILL.md"), ("root", "SKILL.md")])
def test_skill_zip_contains_only_instructions_and_valid_frontmatter(tmp_path, layout, member):
    output = tmp_path / "skill.zip"
    report = skill_packager.build(ROOT, output, layout)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == [member]
        for entry in archive.infolist():
            assert not PurePosixPath(entry.filename).is_absolute()
            assert ".." not in PurePosixPath(entry.filename).parts
            assert (entry.external_attr >> 16) & 0o111 == 0
            assert PurePosixPath(entry.filename).suffix == ".md"
        content = archive.read(member)
    metadata = skill_packager.validate_frontmatter(content)
    assert metadata["name"] == "line-summary-mock"
    assert 1 <= len(metadata["description"]) <= 200
    assert report["real_data_implemented"] is False
    assert report["actual_host_acceptance"] == "NOT_RUN"
    # A forbidden source name can appear in a safety instruction, but never as
    # an archive member. No server/config/source files are bundled in this ZIP.
    assert content == (ROOT / skill_packager.SKILL_SOURCE).read_bytes()


@pytest.mark.parametrize("layout", ["directory", "root"])
def test_skill_zip_reproducible_and_will_not_overwrite(tmp_path, layout):
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    skill_packager.build(ROOT, first, layout)
    skill_packager.build(ROOT, second, layout)
    assert first.read_bytes() == second.read_bytes()
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        skill_packager.build(ROOT, first, layout)
    assert first.read_bytes() == before


def source_copy(tmp_path):
    root = tmp_path / "source"
    for relative in (skill_packager.SKILL_SOURCE, skill_packager.SPEC_SOURCE):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return root


def test_skill_zip_rejects_extra_config_in_spec(tmp_path):
    root = source_copy(tmp_path)
    path = root / skill_packager.SPEC_SOURCE
    spec = json.loads(path.read_text())
    spec["files"].append({"source": ".mcp.json", "archive_path": ".mcp.json"})
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="allowlisted"):
        skill_packager.build(root, tmp_path / "unsafe.zip")
    assert not (tmp_path / "unsafe.zip").exists()


def test_skill_zip_rejects_symlinked_source(tmp_path):
    root = source_copy(tmp_path)
    path = root / skill_packager.SKILL_SOURCE
    data = path.read_bytes()
    other = tmp_path / "other.md"
    other.write_bytes(data)
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        skill_packager.build(root, tmp_path / "unsafe.zip")


@pytest.mark.parametrize("frontmatter", [
    "name: line-summary-mock\ndescription: [not, plain]",
    "name: line-summary-mock\ndescription: x # comment",
    "name: wrong-name\ndescription: valid",
    "name: line-summary-mock\ndescription: ",
    "name: line-summary-mock\ndescription: " + "x" * 201,
    "name: line-summary-mock\ndescription: valid\nextra: not-allowed",
    "name: line-summary-mock\ndescription: x: mapping",
    "name: line-summary-mock\ndescription: true",
    "name: line-summary-mock\ndescription: null",
    "name: line-summary-mock\ndescription: 123",
    "name: line-summary-mock\ndescription: .inf",
])
def test_skill_frontmatter_rejects_outside_bounded_yaml_subset(frontmatter):
    with pytest.raises(ValueError):
        skill_packager.validate_frontmatter(("---\n" + frontmatter + "\n---\n\n# Body\n").encode())


def test_skill_zip_rejects_unknown_layout(tmp_path):
    with pytest.raises(ValueError, match="Unknown"):
        skill_packager.build(ROOT, tmp_path / "unsafe.zip", "fake-host-plugin")
    assert not (tmp_path / "unsafe.zip").exists()
