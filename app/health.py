"""Docker healthcheck: process/event-loop liveness, independent of Core reachability."""
import json
import os
import time
from pathlib import Path


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        os.kill(pid, 0)
        return True
    # Windows os.kill(pid, 0) is not a POSIX liveness probe: do not use it.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def healthy(path: Path, max_age: float = 90) -> bool:
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
        if health.get("fatal", True) or not 0 <= time.time() - health["timestamp"] < max_age:
            return False
        return process_alive(int(health["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    path = Path(os.environ.get("WORKER_DATA_PATH", "/data")) / "health.json"
    raise SystemExit(0 if healthy(path, float(os.environ.get("HEALTH_MAX_AGE", "90"))) else 1)


if __name__ == "__main__":
    main()
