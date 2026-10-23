"""Explicit OS boundary for tests that exercise the real Win32 reader."""
import sys

import pytest


# Do not test for API availability or catch runtime errors here: a broken
# Windows runtime must fail its tests, not silently become a skip.
WINDOWS_NATIVE = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires the real Win32 event/pipe/VFS reader; Linux refusal is tested separately",
)
