"""Offline by default; live LINE access always needs an explicit opt-in."""

import pytest

pytest_plugins = ["pytester"]


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

    monkeypatch.setattr(server, "_SETTINGS_PATH", str(tmp_path / "absent-settings.json"))
    monkeypatch.setattr(server, "_reader", None)
    monkeypatch.setattr(server, "_policy", None)
    monkeypatch.setattr(server, "_session_messages", 0)
    monkeypatch.setattr(server, "_session_bytes", 0)
