"""Tk-free independent emergency-stop watcher."""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable


class SafetyHotkey:
    """Poll Ctrl+Alt+Esc independently of observation, model, and protocol work."""

    def __init__(self, stop_callback: Callable[[], object], *,
                 poll: Callable[[], bool] | None = None) -> None:
        self.done = threading.Event()
        self.ready = threading.Event()
        self.failed = False

        def watch() -> None:
            try:
                read = poll
                if read is None:
                    if sys.platform != "win32":
                        raise RuntimeError("Emergency stop requires Windows")
                    user32 = ctypes.WinDLL("user32", use_last_error=True)
                    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
                    user32.GetAsyncKeyState.restype = ctypes.c_short
                    read = lambda: all(user32.GetAsyncKeyState(key) & 0x8000 for key in (0x11, 0x12, 0x1B))
                # Exercise the native reader before reporting startup readiness.
                pressed = bool(read())
                if pressed:
                    stop_callback()
                held = pressed
                self.ready.set()
                while not self.done.is_set():
                    if self.done.wait(0.05):
                        break
                    pressed = bool(read())
                    if pressed and not held:
                        stop_callback()
                    held = pressed
            except BaseException:
                self.failed = True
                self.ready.set()
                try:
                    stop_callback()
                except BaseException:
                    pass

        self.thread = threading.Thread(target=watch, name="local-stop-hotkey", daemon=True)
        self.thread.start()
        if not self.ready.wait(2) or self.failed:
            self.close()
            raise RuntimeError("Emergency-stop watcher failed to start")

    def close(self) -> None:
        self.done.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)

