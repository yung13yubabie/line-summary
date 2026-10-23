"""TEST ONLY: match the exact subprocess audit representation on Windows."""
import subprocess

def fixed_worker_command(value,expected):
    if type(value) is str:return value==subprocess.list2cmdline(expected)
    return type(value) in (list,tuple) and list(value)==expected
