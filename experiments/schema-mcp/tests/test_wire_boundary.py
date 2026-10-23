import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wire_boundary import WireBoundary


class WireChecks(unittest.TestCase):
    def test_split_sentinel_is_rejected(self):
        boundary = WireBoundary((b'SYNTHETIC_PRIVATE_SENTINEL',))
        boundary.feed('stdout', b'{"value":"SYNTHETIC_PRIVATE_')
        with self.assertRaises(ValueError):
            boundary.feed('stdout', b'SENTINEL"}')

    def test_unknown_windows_paths_are_rejected(self):
        for value in (b'Z:\\synthetic\\private', b'z:/synthetic/private',
                      b'"Z:\\\\synthetic\\\\private"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WireBoundary(()).feed('stderr', value)

    def test_path_prefix_split_across_reads_is_rejected(self):
        boundary = WireBoundary(())
        boundary.feed('stderr', b'error Z:')
        with self.assertRaises(ValueError):
            boundary.feed('stderr', b'\\synthetic')

    def test_streams_are_checked_independently(self):
        boundary = WireBoundary((b'private-value',))
        boundary.feed('stdout', b'private-')
        boundary.feed('stderr', b'value')
        with self.assertRaises(ValueError):
            boundary.feed('stdout', b'value')

    def test_ordinary_protocol_text_is_accepted(self):
        boundary = WireBoundary(())
        boundary.feed('stdout', b'{"jsonrpc":"2.0","result":{"counts":{"table_count":2}}}\n')
        boundary.feed('stderr', b'Synthetic stage: ready 1.25\n')


if __name__ == '__main__':
    unittest.main()
