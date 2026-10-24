"""Protecting a human's concurrent save while an edit is applied.

The hazard: we verify the file still has the content we based the edit on, then replace it. A person pressing Save in an editor
between the check and the replace would be overwritten silently.

What this module can and cannot do (measured, see tests/test_write_guard.py):
  * Windows: while the guard is held, any ordinary writer (VS Code's normal save, other programs opening the file for writing)
    is REFUSED by the operating system; their save fails visibly and nothing of theirs is lost. The replace itself uses
    ReplaceFileW, which succeeds because the guard shares delete access. If the file is already open for writing elsewhere when
    we try to take the guard, we report a conflict instead of proceeding.
  * Windows residual: an editor that saves by writing a temporary file and renaming it OVER the target is not a writer of the
    target and is not blocked; the window shrinks to the instant between our last check and our replace but does not vanish.
  * POSIX: there is no mandatory lock editors honour, so no guard is available; the final-check-then-replace window remains.
    Live application therefore stays an experimental capability that a device owner must switch on explicitly.
"""
import os
import time
from contextlib import contextmanager

class GuardUnavailable(OSError):
    """Another program has the file open for writing right now."""

TEST_HOOKS = {}   # tests may register "after_check": a callable run inside the guarded window, after the final content check

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k.CreateFileW.restype = wintypes.HANDLE
    _k.CloseHandle.argtypes = [wintypes.HANDLE]
    _k.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    _k.ReplaceFileW.restype = wintypes.BOOL
    _READ, _SHARE_READ_DELETE, _OPEN_EXISTING, _NORMAL = 0x80000000, 1 | 4, 3, 0x80
    _INVALID = ctypes.c_void_p(-1).value

    @contextmanager
    def guard(path):
        for attempt in range(5):
            handle = _k.CreateFileW(str(path), _READ, _SHARE_READ_DELETE, None, _OPEN_EXISTING, _NORMAL, None)
            if handle not in (None, _INVALID):
                break
            error = ctypes.get_last_error()
            if error != 32 or attempt == 4:   # 32 = sharing violation: somebody has it open for writing
                raise GuardUnavailable(f"cannot guard the file (Windows error {error}); it is probably being written by another program")
            time.sleep(0.05 * (attempt + 1))
        try:
            yield True
        finally:
            _k.CloseHandle(handle)

    def replace_file(source, target):
        if not _k.ReplaceFileW(str(target), str(source), None, 0, None, None):
            raise PermissionError(f"ReplaceFileW failed (Windows error {ctypes.get_last_error()})")
else:
    @contextmanager
    def guard(path):
        yield False   # no mandatory lock exists on this platform

    def replace_file(source, target):
        os.replace(source, target)

def run_hook(name):
    hook = TEST_HOOKS.get(name)
    if hook is not None:
        hook()
