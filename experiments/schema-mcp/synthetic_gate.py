"""Synthetic-only supply after the unchanged private start and native Job gate.

The fixed launcher passes: operation_dir, approved site-packages, mode, session.
This wrapper does not read settings or replace either ingress proof.
"""
import json
import math
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'source'
CASES = frozenset({'success', 'wrong_key', 'missing_wal', 'missing_shm',
                   'invalid_db', 'exception', 'cancel_key', 'timeout_key'})


def _plain_local_path(value, *, existing=False):
    from configured_child import explicit_local_file
    if not explicit_local_file(value):
        raise ValueError('synthetic local path rejected')
    path = Path(value)
    for part in (path, *path.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            if existing or part != path:
                raise ValueError('synthetic path unavailable') from None
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
            raise ValueError('synthetic reparse path rejected')
    return path.resolve(strict=existing)


def main():
    # Keep all preliminary failures off the private/public pipes, too.
    original_argv = sys.argv[:]
    observation = None
    directory = None
    entry_gate = None
    runner = None
    original_load = None
    original_create = None
    try:
        if len(sys.argv) != 5 or sys.argv[3] not in CASES:
            return 3
        sys.path.insert(0, str(SOURCE))
        directory = _plain_local_path(sys.argv[1], existing=True)
        session = _plain_local_path(sys.argv[4], existing=True)
        if (not directory.is_dir() or not session.is_dir() or directory == session
                or not directory.is_relative_to(session)):
            return 3
        mode = sys.argv[3]
        observation = {'mode': mode, 'after_owned_start': False,
                       'fixed_worker_launches': 0, 'LINE_read': False}
        import configured_entry_gate
        entry_gate = configured_entry_gate
        original_load = entry_gate._load_entry

        def load(site_packages):
            nonlocal runner, original_create
            # Original _execute has accepted its private frame and verified the
            # inherited exact Job before it can reach this call.
            entry = original_load(site_packages)
            observation['after_owned_start'] = True
            import runner as owned_runner
            runner = owned_runner
            if original_create is not None:
                raise ValueError('synthetic entry is single use')
            original_create = runner.OwnedJob.create

            def create(owner, argv, **options):
                values = list(argv)
                expected = [sys.executable, '-I', '-B', str(SOURCE / 'configured_child.py'),
                            '--case', 'success', '--resource',
                            str(directory / 'entry' / 'synthetic_resource.bin'), '--run-id']
                if (len(values) != 12 or values[:9] != expected
                        or type(values[9]) is not str or len(values[9]) != 32
                        or any(char not in '0123456789abcdef' for char in values[9])
                        or values[10] != '--deadline'):
                    raise ValueError('synthetic fixed worker command rejected')
                deadline = float(values[11])
                if not math.isfinite(deadline):
                    raise ValueError('synthetic worker deadline rejected')
                values[3] = str(ROOT / 'synthetic_worker.py')
                values.extend(['--fixture-mode', mode, '--synthetic-root', str(session)])
                observation['fixed_worker_launches'] += 1
                # Preserve the exact suspended-create / assign / resume owner.
                return original_create(owner, values, **options)

            runner.OwnedJob.create = create
            return entry

        entry_gate._load_entry = load
        # The original configured gate must still see exactly its own 3 args.
        sys.argv = original_argv[:3]
        return entry_gate.main()
    except BaseException:
        return 3
    finally:
        sys.argv = original_argv
        if entry_gate is not None and original_load is not None:
            entry_gate._load_entry = original_load
        if runner is not None and original_create is not None:
            runner.OwnedJob.create = original_create
        if observation is not None and directory is not None:
            try:
                (directory / 'synthetic_gate_observation.json').write_text(
                    json.dumps(observation, separators=(',', ':')), encoding='utf-8')
            except BaseException:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
