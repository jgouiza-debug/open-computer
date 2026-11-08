"""Passive native target frame; no Tk, input injection, capture, or panel.

Only the owner thread calls Win32. Controller callbacks publish bounded state.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import threading
import time
import uuid
from typing import Callable, Mapping, Sequence

BLUE = 0xFF8F43  # COLORREF for #438fff
PASSIVE_EX_STYLE = 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000
PASSIVE_STYLE = 0x80000000 | 0x08000000  # popup, disabled
NOACTIVATE_SHOW = 0x0010 | 0x0040 | 0x0200
Rect = tuple[int, int, int, int]  # physical screen x, y, width, height
# Failed class/window teardown must not leave a registered WNDPROC pointing at a
# collected Python callback. A terminal headless process releases these at exit.
_FAILED_NATIVE_RESOURCES: dict[int, object] = {}


class OutlineError(RuntimeError):
    pass


def normalize_rect(value: Mapping) -> Rect:
    try:
        rect = tuple(value[key] for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError) as error:
        raise ValueError("Outline requires a client rectangle") from error
    if (any(type(item) is not int for item in rect) or rect[2] <= 0 or rect[3] <= 0 or
            any(not -(2**30) < item < 2**30 for item in rect) or
            rect[0] + rect[2] >= 2**30 or rect[1] + rect[3] >= 2**30):
        raise ValueError("Invalid outline client rectangle")
    return rect


def _covered(rect: Rect, screens: Sequence[Rect]) -> bool:
    """Subtract actual monitor rectangles; virtual-desktop holes are not pixels."""
    x, y, width, height = rect
    remaining = [(x, y, x + width, y + height)]
    for sx, sy, sw, sh in screens:
        next_parts = []
        for left, top, right, bottom in remaining:
            il, it, ir, ib = max(left, sx), max(top, sy), min(right, sx + sw), min(bottom, sy + sh)
            if il >= ir or it >= ib:
                next_parts.append((left, top, right, bottom))
                continue
            if left < il: next_parts.append((left, top, il, bottom))
            if ir < right: next_parts.append((ir, top, right, bottom))
            if top < it: next_parts.append((il, top, ir, it))
            if ib < bottom: next_parts.append((il, ib, ir, bottom))
        remaining = next_parts
        if not remaining:
            return True
        if len(remaining) > 256:
            return False
    return False


def frame_layout(rect: Rect, screens: Sequence[Rect], thickness: int,
                 *, allow_inset: bool = False) -> tuple[str, tuple[Rect, ...]]:
    x, y, width, height = rect
    if type(thickness) is not int or not 1 <= thickness <= 16 or not 1 <= len(screens) <= 32:
        raise OutlineError("Outline display geometry is unavailable")
    outside = ((x-thickness, y-thickness, width+2*thickness, thickness),
               (x-thickness, y+height, width+2*thickness, thickness),
               (x-thickness, y, thickness, height), (x+width, y, thickness, height))
    if all(_covered(bar, screens) for bar in outside):
        return "outside", outside
    if allow_inset and width > 2*thickness and height > 2*thickness:
        inset = ((x, y, width, thickness), (x, y+height-thickness, width, thickness),
                 (x, y+thickness, thickness, height-2*thickness),
                 (x+width-thickness, y+thickness, thickness, height-2*thickness))
        if all(_covered(bar, screens) for bar in inset):
            return "inset", inset
    raise OutlineError("Target edges cannot show a capture-safe outline; move or restore the target")


class _Win32Bars:
    """Four owned HWNDs with solid backgrounds; called only on their owner thread."""

    def __init__(self):
        if sys.platform != "win32":
            raise OutlineError("The native target outline requires Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.windows = []
        self.capture_excluded = []
        self.brush = None
        self._brush_owned_by_class = False
        self.atom = None
        self.instance = None
        self._previous_dpi = None
        self._expected_shown = False
        self.cleanup_failures = ()
        self._name = "OpenComputerOutline-" + uuid.uuid4().hex
        self._bind()
        try:
            self._previous_dpi = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not self._previous_dpi:
                raise OutlineError("Cannot establish physical-pixel outline coordinates")
            self.instance = self.kernel.GetModuleHandleW(None)
            self.brush = self.gdi.CreateSolidBrush(BLUE)
            if not self.instance or not self.brush:
                raise OutlineError("Cannot create native outline resources")
            self._procedure = self._wndproc_type(self._window_proc)
            definition = self._window_class()
            definition.lpfnWndProc, definition.hInstance = self._procedure, self.instance
            definition.hbrBackground, definition.lpszClassName = self.brush, self._name
            self.atom = self.user.RegisterClassW(ctypes.byref(definition))
            if not self.atom:
                raise OutlineError("Cannot register native outline windows")
            # Windows deletes a registered class background brush when the
            # class is unregistered. It must not also be passed to DeleteObject.
            self._brush_owned_by_class = True
            for _ in range(4):
                window = self.user.CreateWindowExW(PASSIVE_EX_STYLE, self._name, "", PASSIVE_STYLE,
                    0, 0, 1, 1, None, None, self.instance, None)
                if not window:
                    raise OutlineError("Cannot create native outline windows")
                self.windows.append(int(window))
                if self.user.GetWindowLongPtrW(window, -20) & PASSIVE_EX_STYLE != PASSIVE_EX_STYLE:
                    raise OutlineError("Native outline passive styles were not applied")
                if not self.user.SetLayeredWindowAttributes(window, 0, 255, 2):
                    raise OutlineError("Cannot configure native outline transparency")
                affinity = wintypes.DWORD()
                excluded = (sys.getwindowsversion().build >= 19041 and
                    bool(self.user.SetWindowDisplayAffinity(window, 0x11)) and
                    bool(self.user.GetWindowDisplayAffinity(window, ctypes.byref(affinity))) and affinity.value == 0x11)
                self.capture_excluded.append(bool(excluded))
        except BaseException:
            self.close()
            raise

    def _bind(self):
        user, kernel, gdi = self.user, self.kernel, self.gdi
        self._wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM)
        procedure = self._wndproc_type
        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", procedure), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]
        class Message(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT), ("lPrivate", wintypes.DWORD)]
        self._window_class, self._message = WindowClass, Message
        bindings = (
            (user, "SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
            (user, "GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
            (kernel, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
            (gdi, "CreateSolidBrush", [wintypes.DWORD], wintypes.HBRUSH),
            (gdi, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL),
            (user, "RegisterClassW", [ctypes.POINTER(WindowClass)], wintypes.ATOM),
            (user, "UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (user, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (user, "DefWindowProcW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            (user, "GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
            (user, "SetLayeredWindowAttributes", [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD], wintypes.BOOL),
            (user, "SetWindowDisplayAffinity", [wintypes.HWND, wintypes.DWORD], wintypes.BOOL),
            (user, "GetWindowDisplayAffinity", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (user, "SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            (user, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (user, "IsWindow", [wintypes.HWND], wintypes.BOOL),
            (user, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            (user, "ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (user, "DestroyWindow", [wintypes.HWND], wintypes.BOOL),
            (user, "RedrawWindow", [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], wintypes.BOOL),
            (user, "PeekMessageW", [ctypes.POINTER(Message), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            (user, "TranslateMessage", [ctypes.POINTER(Message)], wintypes.BOOL),
            (user, "DispatchMessageW", [ctypes.POINTER(Message)], ctypes.c_ssize_t))
        for library, name, arguments, result in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
        self._monitor_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, self._monitor_callback_type, wintypes.LPARAM]
        user.EnumDisplayMonitors.restype = wintypes.BOOL

    def _window_proc(self, window, message, wparam, lparam):
        if message == 0x0084: return -1  # HTTRANSPARENT
        if message == 0x0021: return 3   # MA_NOACTIVATE
        return self.user.DefWindowProcW(window, message, wparam, lparam)

    def screens(self):
        rectangles = []
        @self._monitor_callback_type
        def collect(_monitor, _dc, pointer, _data):
            value = pointer.contents
            rectangles.append((value.left, value.top, value.right-value.left, value.bottom-value.top))
            return len(rectangles) <= 32
        if not self.user.EnumDisplayMonitors(None, None, collect, 0) or not rectangles:
            raise OutlineError("Cannot inspect outline display coverage")
        return rectangles

    def dpi(self, target):
        value = self.user.GetDpiForWindow(target)
        if not 48 <= value <= 768:
            raise OutlineError("Target DPI is unavailable")
        return value

    def show(self, rectangles):
        for window, rect in zip(self.windows, rectangles):
            if not self.user.IsWindow(window) or not self.user.SetWindowPos(window, -1, *rect, NOACTIVATE_SHOW):
                raise OutlineError("Cannot position passive target outline")
            actual = wintypes.RECT()
            if not self.user.GetWindowRect(window, ctypes.byref(actual)) or (actual.left, actual.top,
                    actual.right-actual.left, actual.bottom-actual.top) != rect:
                raise OutlineError("Native outline geometry differs from physical target coordinates")
            if not self.user.IsWindowVisible(window) or not self.user.RedrawWindow(window, None, None, 0x105):
                raise OutlineError("Cannot display passive target outline")
        self._expected_shown = True

    def hide(self):
        self._expected_shown = False
        for window in self.windows:
            self.user.ShowWindow(window, 0)

    def pump(self):
        message = self._message()
        while self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            if message.message == 0x0012:
                raise OutlineError("Native outline owner loop ended")
            self.user.TranslateMessage(ctypes.byref(message))
            self.user.DispatchMessageW(ctypes.byref(message))
        if any(not self.user.IsWindow(window) for window in self.windows):
            raise OutlineError("Native outline window disappeared")
        if self._expected_shown and any(not self.user.IsWindowVisible(window) for window in self.windows):
            raise OutlineError("Native target outline became hidden")

    def close(self):
        self._expected_shown = False
        failures, remaining = [], []
        for window in self.windows:
            try:
                if self.user.IsWindow(window):
                    if not self.user.DestroyWindow(window):
                        failures.append("DestroyWindow")
                    if self.user.IsWindow(window):
                        remaining.append(window)
                        if "DestroyWindow" not in failures:
                            failures.append("DestroyWindow")
            except BaseException:
                remaining.append(window)
                failures.append("DestroyWindow")
        self.windows[:] = remaining
        if self.atom:
            try:
                if self.user.UnregisterClassW(self._name, self.instance):
                    self.atom = None
                    if self._brush_owned_by_class:
                        self.brush = None
                        self._brush_owned_by_class = False
                else:
                    failures.append("UnregisterClassW")
            except BaseException:
                failures.append("UnregisterClassW")
        if self.brush and not self._brush_owned_by_class:
            try:
                if self.gdi.DeleteObject(self.brush):
                    self.brush = None
                else:
                    failures.append("DeleteObject")
            except BaseException:
                failures.append("DeleteObject")
        if self._previous_dpi:
            try:
                if self.user.SetThreadDpiAwarenessContext(self._previous_dpi):
                    self._previous_dpi = None
                else:
                    failures.append("SetThreadDpiAwarenessContext")
            except BaseException:
                failures.append("SetThreadDpiAwarenessContext")
        self.cleanup_failures = tuple(dict.fromkeys(failures))
        if failures:
            _FAILED_NATIVE_RESOURCES[id(self)] = self
            raise OutlineError("Native outline cleanup failed: " + ", ".join(self.cleanup_failures))
        _FAILED_NATIVE_RESOURCES.pop(id(self), None)


class NativeOutline:
    """One bounded owner thread; callbacks only publish state, never call Win32."""

    def __init__(self, *, native_factory: Callable = _Win32Bars, allow_inset: bool = True,
                 poll_interval: float = 0.025, startup_timeout: float = 2.0):
        self._lock = threading.Lock()
        self._wake, self._ready, self._done, self._visible = (threading.Event() for _ in range(4))
        self._terminal = False
        self._active = False
        self._rect = None
        self._target = None
        self._error = None
        self._snapshot = {"ready": False, "failed": False, "visible": False,
            "windows": [], "rect": None, "bar_rects": [], "thickness": None,
            "edge_mode": None, "capture_excluded": []}
        self._thread = threading.Thread(target=self._run, args=(native_factory, allow_inset, poll_interval),
                                        name="headless-native-outline", daemon=True)
        self._thread.start()
        if not self._ready.wait(startup_timeout):
            self.close()
            raise OutlineError("Native target outline startup timed out")
        try:
            self.check_health()
        except BaseException:
            self.close()
            raise

    def emit(self, event: dict) -> None:
        kind = event.get("event", event.get("type"))
        if kind in {"stopped", "disarmed", "error", "cancelled"}:
            self.hide()
        elif kind in {"target", "armed"}:
            window = event.get("selected_window")
            if not isinstance(window, dict):
                self.hide()
                return
            rect = normalize_rect(window.get("rect"))
            target = int(window["id"])
            with self._lock:
                if not self._terminal:
                    if (self._rect, self._target) != (rect, target):
                        self._visible.clear()
                    self._rect, self._target, self._active = rect, target, True
            self._wake.set()
        self.check_health()

    def update_target(self, rect: Mapping | None) -> None:
        if rect is None:
            self.hide()
            return
        bounds = normalize_rect(rect)
        with self._lock:
            if not self._terminal:
                if self._rect != bounds:
                    self._visible.clear()
                self._rect = bounds
        self._wake.set()
        self.check_health()

    def hide(self) -> None:
        # Terminal even if stale target/armed messages arrive after cancellation.
        with self._lock:
            self._terminal, self._active = True, False
            self._visible.clear()
        self._wake.set()

    def check_health(self) -> None:
        with self._lock:
            error = self._error
        if error is not None or self._done.is_set() or not self._thread.is_alive():
            raise OutlineError("Native target outline is unavailable; task control must stop") from error

    def wait_visible(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self.check_health()
            with self._lock:
                if self._terminal: raise OutlineError("Target outline is stopped")
                if self._visible.is_set(): return
            if time.monotonic() >= deadline:
                raise OutlineError("Target outline did not become visible before input authorization")
            self._visible.wait(min(0.02, max(0, deadline-time.monotonic())))

    def diagnostics(self) -> dict:
        with self._lock:
            return {**self._snapshot, "thread_alive": self._thread.is_alive(),
                    "windows": list(self._snapshot["windows"]),
                    "bar_rects": list(self._snapshot["bar_rects"]),
                    "capture_excluded": list(self._snapshot["capture_excluded"])}

    def close(self) -> None:
        self.hide()
        self._done.set()
        self._wake.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive(): raise OutlineError("Native target outline did not shut down")

    def _record_failure(self, error):
        with self._lock:
            self._error = error
            self._snapshot.update(failed=True, visible=False)
            self._visible.clear()

    def _run(self, factory, allow_inset, interval):
        native = None
        try:
            native = factory()
            with self._lock:
                self._snapshot.update(ready=True, windows=list(native.windows),
                                      capture_excluded=list(native.capture_excluded))
            self._ready.set()
            last = None
            while not self._done.is_set():
                with self._lock:
                    active, rect, target = self._active and not self._terminal, self._rect, self._target
                if active and rect is not None:
                    thickness = min(8, max(2, round(3*native.dpi(target)/96)))
                    mode, bars = frame_layout(rect, native.screens(), thickness,
                        allow_inset=allow_inset and len(native.capture_excluded) == 4 and all(native.capture_excluded))
                    if (mode, bars) != last:
                        native.show(bars)
                        last = mode, bars
                    # A stop published during placement cannot be acknowledged as visible.
                    with self._lock:
                        if (self._terminal or self._done.is_set() or
                                (self._rect, self._target) != (rect, target)):
                            show = False
                        else:
                            self._snapshot.update(visible=True, rect=rect, bar_rects=list(bars),
                                                  edge_mode=mode, thickness=thickness)
                            self._visible.set()
                            show = True
                    if not show:
                        native.hide()
                        with self._lock:
                            self._snapshot["visible"] = False
                else:
                    native.hide()
                    last = None
                    with self._lock:
                        self._snapshot["visible"] = False
                        self._visible.clear()
                native.pump()
                self._wake.wait(interval)
                self._wake.clear()
        except BaseException as error:
            self._record_failure(error)
        finally:
            if native is not None:
                for cleanup in (native.hide, native.close):
                    try:
                        cleanup()
                    except BaseException as error:
                        self._record_failure(error)
            with self._lock:
                self._snapshot["visible"] = False
                self._visible.clear()
            self._ready.set()
