"""Actual frozen EXE/WebView2 startup and normal close with isolated app data."""

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time
from pathlib import Path


def main(app, work, runtime):
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=False)
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(work / 'runtime'), str(Path(runtime).resolve(strict=True))],
                   check=True, capture_output=True)
    env = {**os.environ, 'BORASUKI_DATA_DIR': str(work)}
    child = subprocess.Popen([str(Path(app).resolve() / 'Borasuki.exe')], env=env,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    try:
        deadline = time.monotonic() + 30
        log = work / 'borasuki.log'
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('Frozen app exited before frontend connection.')
            if log.exists() and 'Frontend connected to application service' in log.read_text(encoding='utf-8'):
                break
            time.sleep(0.2)
        else:
            raise TimeoutError('Frozen frontend did not connect.')
        deadline = time.monotonic() + 60
        while not (work / 'runtime-validation.json').is_file():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise TimeoutError('Frozen first-run runtime validation did not complete.')
            time.sleep(0.2)
        time.sleep(0.5)
        windows = []
        @callback_type
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == child.pid and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True
        user32.EnumWindows(collect, 0)
        if not windows:
            raise RuntimeError('Frozen app has no visible native window.')
        for hwnd in windows:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
        if child.wait(timeout=20) != 0:
            raise RuntimeError('Frozen application did not close normally.')
        print('Frozen EXE frontend connection and normal shutdown passed.', flush=True)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


if __name__ == '__main__':
    main(*sys.argv[1:])
