"""Hidden, cancellable child processes with Windows lifetime containment."""

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes


class Interrupted(Exception):
    pass


class ProcessError(RuntimeError):
    pass


class ProcessGroup:
    def __init__(self, stop: threading.Event):
        self.stop = stop
        self.children = []
        self.handle = None
        if os.name == "nt":
            class BASIC(ctypes.Structure):
                _fields_ = [("times", ctypes.c_int64 * 2), ("flags", wintypes.DWORD),
                            ("working", ctypes.c_size_t * 2), ("active", wintypes.DWORD),
                            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
            class EXTENDED(ctypes.Structure):
                _fields_ = [("basic", BASIC), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.basic.flags = 0x2000
            if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                if self.handle:
                    self.kernel.CloseHandle(self.handle)
                raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, args: list[str], **kwargs) -> subprocess.Popen:
        if self.stop.is_set():
            raise Interrupted()
        flags = (subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0
        process = subprocess.Popen(args, creationflags=flags, **kwargs)
        self.children.append(process)
        if self.handle:
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                process.kill()
                process.wait()
                raise ctypes.WinError(ctypes.get_last_error())
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            if ntdll.NtResumeProcess(int(process._handle)) != 0:
                process.kill()
                process.wait()
                raise ProcessError("Cannot resume child process")
        return process

    def capture(self, args: list[str], timeout: float = 120, on_poll=None, **kwargs) -> bytes:
        process = self.spawn(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        deadline = time.monotonic() + timeout
        while True:
            if on_poll:
                on_poll()
            if self.stop.is_set():
                raise Interrupted()
            if time.monotonic() > deadline:
                raise ProcessError(f"Process timeout: {args[0]}")
            try:
                output, error = process.communicate(timeout=0.2)
                if on_poll:
                    on_poll()
                if process.returncode:
                    raise ProcessError(error.decode("utf-8", "replace")[-6000:])
                return output
            except subprocess.TimeoutExpired:
                continue

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
