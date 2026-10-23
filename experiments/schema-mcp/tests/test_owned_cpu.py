"""Only an already retained child handle and its exact Job may be sampled."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from owned_cpu import sample_owner


class Function:
    def __init__(self, invoke):
        self.invoke = invoke
    def __call__(self, *args):
        return self.invoke(*args)


class Handle:
    closed = False
    def __int__(self):
        return 17


class OwnedCpu(unittest.TestCase):
    def test_samples_exact_handles_and_returns_only_cpu_totals(self):
        seen = []
        def process(handle, creation, exit, kernel, user):
            seen.append(('process', handle))
            kernel._obj.value = 23
            user._obj.value = 19
            return 1
        def accounting(handle, kind, buffer, size, reserved):
            seen.append(('job', handle, kind))
            buffer._obj.kernel = 29
            buffer._obj.user = 31
            return 1
        api = SimpleNamespace(GetProcessTimes=Function(process), QueryInformationJobObject=Function(accounting))
        result = sample_owner(SimpleNamespace(_handle=Handle()), SimpleNamespace(handle=33, kernel=api))
        self.assertEqual(seen, [('process', 17), ('job', 33, 1)])
        self.assertEqual(result, {'owned_cpu_ns': 4200, 'job_cpu_ns': 6000})

    def test_missing_or_closed_owner_cannot_be_sampled(self):
        for child in (SimpleNamespace(_handle=None),
                      SimpleNamespace(_handle=SimpleNamespace(closed=True))):
            with self.assertRaises(ValueError):
                sample_owner(child, SimpleNamespace(handle=33, kernel=None))


if __name__ == '__main__':
    unittest.main()
