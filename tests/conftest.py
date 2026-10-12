"""Offline by default; live LINE access always needs an explicit opt-in."""

import pytest

pytest_plugins = ["pytester"]


def pytest_configure(config):
    # The opt-in tests copy this hook into a tiny synthetic project that has
    # no package/helper. Load storage fixtures only in the actual source tree.
    from pathlib import Path
    if Path(__file__).with_name("hot_wal_fixture.py").is_file():
        config.pluginmanager.import_plugin("tests.hot_wal_fixture")


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-line",
        action="store_true",
        default=False,
        help=(
            "Allow integration tests to read the local LINE process memory and "
            "encrypted database. Never enable this for ordinary tests or CI."
        ),
    )


def pytest_collection_modifyitems(config, items):
    # This decision must not inspect the OS, LINE processes, or database paths.
    if config.getoption("--run-live-line"):
        return
    skip_live = pytest.mark.skip(reason="live LINE access requires --run-live-line")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def isolate_offline_server_settings(request, monkeypatch, tmp_path):
    """Never let a developer's real settings/cache redirect synthetic tests."""
    if request.node.get_closest_marker("integration"):
        return
    import line_mcp_server as server
    import key_extractor

    class ForbiddenNativeAccess(BaseException):
        pass

    def no_native_access(*args, **kwargs):
        raise ForbiddenNativeAccess("Offline tests forbid native process access")

    monkeypatch.setattr(key_extractor, "_load_kernel32", no_native_access)
    monkeypatch.setattr(key_extractor, "_enumerate_line_pids", no_native_access)

    monkeypatch.setattr(server, "_SETTINGS_PATH", str(tmp_path / "absent-settings.json"))
    monkeypatch.setattr(server, "_reader", None)
    monkeypatch.setattr(server, "_policy", None)
    monkeypatch.setattr(server, "_session_messages", 0)
    monkeypatch.setattr(server, "_session_bytes", 0)
