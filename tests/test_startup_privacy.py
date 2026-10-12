"""Fresh synthetic processes verify warnings/CLI paths cannot escape."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MARKER = "SYNTHETIC_STARTUP_PRIVATE_SENTINEL"


def test_sdk_import_warnings_do_not_echo_details_or_filenames():
    script = '''
import builtins, os, sys, warnings
sys.path.insert(0, os.getcwd())
real_import = builtins.__import__
def importing(name, *args, **kwargs):
    if name == 'mcp' or name.startswith('mcp.'):
        warnings.warn_explicit('SYNTHETIC_STARTUP_PRIVATE_SENTINEL', UserWarning,
            'SYNTHETIC_STARTUP_PRIVATE_SENTINEL.py', 123)
    return real_import(name, *args, **kwargs)
builtins.__import__ = importing
import line_mcp_server
assert line_mcp_server._policy is None and line_mcp_server._reader is None
print('SYNTHETIC_IMPORT_OK')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert MARKER not in result.stdout + result.stderr
    assert "SYNTHETIC_IMPORT_OK" in result.stdout
    profile = os.environ.get("USERPROFILE", "")
    assert not profile or profile.casefold() not in (result.stdout + result.stderr).casefold()


def test_cli_unhandled_exception_has_fixed_diagnostic():
    script = '''
import os, runpy, sys
sys.path.insert(0, os.getcwd())
from mcp.server.fastmcp import FastMCP
def fail(*args, **kwargs):
    raise RuntimeError('SYNTHETIC_STARTUP_PRIVATE_SENTINEL')
FastMCP.run = fail
runpy.run_path('line_mcp_server.py', run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert MARKER not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
