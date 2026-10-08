"""Synthetic regression tests: none of these may access a real LINE process."""

import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest


def _integration_namespace(monkeypatch):
    """Execute only the fixture definitions; forbid every live-access import."""
    source = Path(__file__).with_name("test_integration.py").read_text(encoding="utf-8")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"key_extractor", "db_reader", "line_mcp_server", "sys", "platform"}:
            raise AssertionError(f"live boundary imported before opt-in: {name}")
        return original_import(name, *args, **kwargs)

    namespace = {"__name__": "synthetic_integration_definitions"}
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", guarded_import)
        exec(compile(source, "test_integration.py", "exec"), namespace)
    return namespace


def test_integration_collection_does_not_import_live_modules(monkeypatch):
    namespace = _integration_namespace(monkeypatch)
    assert "live" in namespace


def test_live_fixture_checks_opt_in_before_platform_or_line(monkeypatch):
    namespace = _integration_namespace(monkeypatch)
    original_import = builtins.__import__

    def no_live_imports(name, *args, **kwargs):
        if name in {"sys", "platform", "key_extractor", "db_reader", "line_mcp_server"}:
            raise AssertionError(f"boundary reached before opt-in: {name}")
        return original_import(name, *args, **kwargs)

    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda name: False))
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", no_live_imports)
        with pytest.raises(pytest.skip.Exception, match="requires --run-live-line"):
            namespace["live"].__wrapped__(request)


@pytest.mark.parametrize("selector", [[], ["-m", "integration"]])
def test_plain_pytest_never_runs_live_fixtures(pytester, selector):
    # Copy the real hook into a temporary synthetic project. Even -m integration
    # is only test selection, not consent to read the owner's real data.
    pytester.makeconftest(Path(conftest.__file__).read_text(encoding="utf-8"))
    pytester.makepyfile(line_mcp_server="_SETTINGS_PATH = 'settings.json'\n_reader = None")
    pytester.makeini("[pytest]\nmarkers = integration: synthetic live boundary")
    pytester.makepyfile(
        test_synthetic="""
        import pathlib
        import pytest

        @pytest.fixture(scope="session")
        def sensitive_fixture():
            pathlib.Path("LIVE_BOUNDARY_REACHED").write_text("bad")
            raise AssertionError("a real live fixture would have accessed LINE")

        @pytest.mark.integration
        def test_sensitive(sensitive_fixture):
            raise AssertionError("integration ran without explicit opt-in")
        """
    )
    result = pytester.runpytest_subprocess("-q", *selector)
    result.assert_outcomes(skipped=1)
    assert not (pytester.path / "LIVE_BOUNDARY_REACHED").exists()


def test_offline_settings_cannot_use_real_configuration(tmp_path):
    import line_mcp_server as server

    assert server._SETTINGS_PATH == str(tmp_path / "absent-settings.json")
    assert not Path(server._SETTINGS_PATH).exists()
    assert server._reader is None


def test_opt_in_enables_only_synthetic_integration(pytester):
    # This subprocess has ONLY synthetic test code; it never imports the real
    # integration module or any production module, even with the opt-in flag.
    pytester.makeconftest(Path(conftest.__file__).read_text(encoding="utf-8"))
    pytester.makeini("[pytest]\nmarkers = integration: synthetic live boundary")
    pytester.makepyfile(
        test_synthetic="""
        import pytest
        @pytest.mark.integration
        def test_synthetic_only():
            assert 2 + 2 == 4
        """
    )
    result = pytester.runpytest_subprocess("-q", "--run-live-line", "-m", "integration")
    result.assert_outcomes(passed=1)
