"""Window-scoped Windows desktop input and a deterministic demonstration backend."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import io
import os
import threading


class BackendError(RuntimeError):
    """An input or capture operation could not safely complete."""


class BackendCancelled(BackendError):
    """Local disarm, target selection, or emergency stop cancelled an action."""


def rectangle(x: int, y: int, width: int, height: int) -> dict:
    return {"x": x, "y": y, "width": width, "height": height,
            "left": x, "top": y, "right": x + width, "bottom": y + height}


class FakeBackend:
    """No operating-system input. Useful for demonstrations and unit tests."""

    real = False

    def __init__(self) -> None:
        self.window = {"id": "demo", "title": "Open Computer Demo", "rect": rectangle(100, 100, 640, 400)}
        self.actions = []
        self.cancel_event = threading.Event()

    def set_cancel_event(self, event: threading.Event) -> None:
        self.cancel_event = event

    def list_windows(self) -> list[dict]:
        return [self.get_window("demo")]

    def get_window(self, window_id: str | int) -> dict:
        if str(window_id) != self.window["id"]:
            raise BackendError("Target window is unavailable")
        return {**self.window, "rect": dict(self.window["rect"])}

    def capture(self, window_id: str | int) -> dict:
        window = self.get_window(window_id)
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (window["rect"]["width"], window["rect"]["height"]), "#102030")
        ImageDraw.Draw(image).text((24, 24), "DEMO - no desktop input", fill="white")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return {"image_base64": base64.b64encode(stream.getvalue()).decode("ascii"), "mime_type": "image/png", "rect": window["rect"]}

    def perform(self, action: str, window_id: str | int, **params: object) -> None:
        if self.cancel_event.is_set():
            raise BackendCancelled("Action cancelled locally")
        self.get_window(window_id)
        self.actions.append((action, window_id, params))


class WindowsBackend:
    """Injects input only after checking the selected window and visible point."""

    real = True

    def __init__(self) -> None:
        if os.name != "nt":
            raise BackendError("The native backend requires Windows")
        self.cancel_event = threading.Event()
        self._title_cache: dict[tuple[int, int, int], str] = {}
        self._capture_cache = None
        self._capture_cache_lock = threading.Lock()
        self._capture_cache_limit = 32 * 1024 * 1024
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self.user32
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        try:
            u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
        self._create_input_types()

    def _create_input_types(self):
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("payload",)
            _fields_ = [("type", wintypes.DWORD), ("payload", InputUnion)]

        self.Input, self.MouseInput, self.KeyboardInput = Input, MouseInput, KeyboardInput
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def set_cancel_event(self, event: threading.Event) -> None:
        self.cancel_event = event

    def _handle(self, window_id):
        try:
            hwnd = int(window_id)
        except (ValueError, TypeError) as exc:
            raise BackendError("Invalid window identifier") from exc
        if not self.user32.IsWindow(hwnd) or not self.user32.IsWindowVisible(hwnd) or self.user32.IsIconic(hwnd):
            raise BackendError("Target window is closed, hidden, or minimized")
        return hwnd

    def get_window(self, window_id: str | int) -> dict:
        hwnd = self._handle(window_id)
        native_rect = wintypes.RECT()
        if not self.user32.GetClientRect(hwnd, ctypes.byref(native_rect)):
            raise BackendError("Cannot read target geometry")
        # Pillow's HWND capture uses GetDC/GetClientRect. Match its client-area
        # origin explicitly; window borders/titlebars are outside the image.
        origin = wintypes.POINT(native_rect.left, native_rect.top)
        corner = wintypes.POINT(native_rect.right, native_rect.bottom)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(origin)) or not self.user32.ClientToScreen(hwnd, ctypes.byref(corner)):
            raise BackendError("Cannot map target client coordinates to the desktop")
        width, height = corner.x - origin.x, corner.y - origin.y
        if width <= 0 or height <= 0:
            raise BackendError("Target window has no visible area")
        pid = wintypes.DWORD()
        window_thread = self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not window_thread or not pid.value:
            raise BackendError("Cannot read target process identity")
        title = self._window_title(hwnd, pid.value, window_thread)
        return {"id": str(hwnd), "pid": pid.value, "title": title,
                "rect": rectangle(origin.x, origin.y, width, height)}

    def _window_title(self, hwnd: int, pid: int, window_thread: int) -> str:
        key = (hwnd, pid, window_thread)
        if pid == os.getpid() and window_thread != threading.get_native_id():
            # GetWindowText sends WM_GETTEXT synchronously for same-process
            # windows. A worker holding the controller lock must never wait on
            # the UI thread, which may itself be polling that controller.
            return self._title_cache.get(key, f"Window {hwnd}")
        title = ctypes.create_unicode_buffer(self.user32.GetWindowTextLengthW(hwnd) + 1)
        self.user32.GetWindowTextW(hwnd, title, len(title))
        self._title_cache[key] = title.value
        return title.value

    def list_windows(self) -> list[dict]:
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd, _):
            try:
                window = self.get_window(hwnd)
                if window["title"]:
                    windows.append(window)
            except BackendError:
                pass
            return True

        self.user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        if not self.user32.EnumWindows(collect, 0):
            raise BackendError("Cannot enumerate desktop windows")
        return sorted(windows, key=lambda window: window["title"].casefold())

    def capture(self, window_id: str | int) -> dict:
        from PIL import ImageGrab
        try:
            window = self.get_window(window_id)
            rect = window["rect"]
            # Always capture fresh pixels, including on an encoding-cache hit.
            image = ImageGrab.grab(window=int(window_id))
            if image.size != (rect["width"], rect["height"]):
                raise BackendError("Captured dimensions differ from target geometry")
            after = self.get_window(window_id)
            if after["rect"] != rect or after.get("pid") != window.get("pid"):
                raise BackendError("Window identity or geometry changed during capture; observe again")
            encoded = self._encode_capture(image, window)
            return {"image_base64": encoded, "mime_type": "image/png", "rect": rect}
        except Exception:
            self._clear_capture_cache()
            raise

    def _clear_capture_cache(self) -> None:
        with self._capture_cache_lock:
            self._capture_cache = None

    @staticmethod
    def _encode_png(image) -> str:
        stream = io.BytesIO()
        image.save(stream, format="PNG", compress_level=6)
        return base64.b64encode(stream.getvalue()).decode("ascii")

    def _encode_capture(self, image, window: dict) -> str:
        """Reuse encoding only after exact comparison of this fresh frame."""
        rect = window["rect"]
        key = (window["id"], window.get("pid"),
               tuple(rect[field] for field in ("x", "y", "width", "height")), image.mode, image.size)
        # Native grabs are RGB. Avoid large raw copies and unusual pixel-layout
        # assumptions when an image cannot fit the bounded cache anyway.
        raw_size = image.width * image.height * len(image.getbands())
        with self._capture_cache_lock:
            if image.mode not in ("RGB", "RGBA", "L") or raw_size > self._capture_cache_limit:
                self._capture_cache = None
                return self._encode_png(image)
            pixels = image.tobytes()
            cached = self._capture_cache
            if cached is not None and cached[0] == key and cached[1] == pixels:
                return cached[2]
            # The previous frame must not survive a target or pixel change, or
            # an encoding error. Cache payload includes raw pixels and base64.
            self._capture_cache = None
            encoded = self._encode_png(image)
            if len(pixels) + len(encoded) <= self._capture_cache_limit:
                self._capture_cache = (key, pixels, encoded)
            return encoded

    def activate(self, window_id: str | int) -> None:
        hwnd = self._handle(window_id)
        if getattr(self, "_expected_pid", None) is not None and self.get_window(window_id).get("pid") != self._expected_pid:
            raise BackendError("Target process identity changed")
        if self.cancel_event.is_set():
            raise BackendCancelled("Action cancelled locally")
        self.user32.SetForegroundWindow(hwnd)
        if self.user32.GetForegroundWindow() != hwnd:
            raise BackendError("Windows refused to focus the target; focus it manually")

    def _check(self, window_id, rect, point=None):
        if self.cancel_event.is_set():
            raise BackendCancelled("Action cancelled locally")
        hwnd = self._handle(window_id)
        window = self.get_window(window_id)
        if window.get("pid") != self._expected_pid:
            raise BackendError("Target process identity changed")
        if window["rect"] != rect:
            raise BackendError("Target geometry changed; observe again")
        if self.user32.GetForegroundWindow() != hwnd:
            raise BackendError("Target lost foreground focus")
        # Existing physical modifier/button presses can change input semantics.
        for vk in (0x01, 0x02, 0x04, 0x10, 0x11, 0x12, 0x5B, 0x5C):
            if vk not in getattr(self, "_held", set()) and self.user32.GetAsyncKeyState(vk) & 0x8000:
                raise BackendError("Release physical mouse buttons and modifier keys first")
        if point is not None:
            x, y = point
            if not (rect["left"] <= x < rect["right"] and rect["top"] <= y < rect["bottom"]):
                raise BackendError("Input point is outside the target")
            hit = self.user32.WindowFromPoint(wintypes.POINT(x, y))
            if not hit or self.user32.GetAncestor(hit, 2) != hwnd:
                raise BackendError("Input point is covered by another window")

    def _send(self, *inputs):
        array = (self.Input * len(inputs))(*inputs)
        if self.user32.SendInput(len(inputs), array, ctypes.sizeof(self.Input)) != len(inputs):
            raise BackendError("Windows rejected input (possibly a higher-integrity target)")

    def _mouse(self, flags, data=0, x=0, y=0):
        item = self.Input()
        item.type = 0
        item.mi = self.MouseInput(x, y, data & 0xFFFFFFFF, flags, 0, 0)
        return item

    def _keyboard(self, vk=0, scan=0, flags=0):
        item = self.Input()
        item.type = 1
        item.ki = self.KeyboardInput(vk, scan, flags, 0, 0)
        return item

    def _move(self, window_id, rect, x, y):
        point = (rect["x"] + x, rect["y"] + y)
        self._check(window_id, rect, point)
        sx, sy = self.user32.GetSystemMetrics(76), self.user32.GetSystemMetrics(77)
        sw, sh = self.user32.GetSystemMetrics(78), self.user32.GetSystemMetrics(79)
        if sw <= 1 or sh <= 1 or not (sx <= point[0] < sx + sw and sy <= point[1] < sy + sh):
            raise BackendError("Input point is outside the visible desktop")
        self._send(self._mouse(0x0001 | 0x8000 | 0x4000,
                               x=round((point[0] - sx) * 65535 / (sw - 1)),
                               y=round((point[1] - sy) * 65535 / (sh - 1))))
        self._check(window_id, rect, point)
        return point

    def perform(self, action: str, window_id: str | int, **params: object) -> None:
        self._held = set()
        self._expected_pid = params.pop("_expected_pid", None)
        rect = params.pop("_expected_rect", self.get_window(window_id)["rect"])
        if action == "activate":
            if self.get_window(window_id)["rect"] != rect:
                raise BackendError("Target geometry changed; observe again")
            self.activate(window_id)
            self._check(window_id, rect)
            return
        self._check(window_id, rect)
        if action in ("move", "click", "scroll", "drag"):
            point = self._move(window_id, rect, params["x"], params["y"])
        if action == "click":
            self._click(window_id, rect, point, params.get("button", "left"), params.get("count", 1))
        elif action == "scroll":
            self._check(window_id, rect, point)
            self._send(self._mouse(0x0800, params["delta"] * 120))
        elif action == "drag":
            self._drag(window_id, rect, params)
        elif action == "type_text":
            self._type_text(window_id, rect, params["text"])
        elif action == "press_key":
            self._press_key(window_id, rect, params["key"])

    def _click(self, window_id, rect, point, button, count):
        down, up, vk = {"left": (2, 4, 1), "right": (8, 16, 2), "middle": (32, 64, 4)}[button]
        for _ in range(count):
            self._check(window_id, rect, point)
            self._held.add(vk)
            try:
                self._send(self._mouse(down))
                self._check(window_id, rect, point)
            finally:
                self._send(self._mouse(up))
                self._held.discard(vk)

    def _drag(self, window_id, rect, params):
        self._check(window_id, rect, (rect["x"] + params["x"], rect["y"] + params["y"]))
        self._held.add(1)
        try:
            self._send(self._mouse(2))
            steps = max(2, round(params.get("duration", 0.4) * 60))
            for step in range(1, steps + 1):
                if self.cancel_event.wait(params.get("duration", 0.4) / steps):
                    raise BackendCancelled("Action cancelled locally")
                x = round(params["x"] + (params["to_x"] - params["x"]) * step / steps)
                y = round(params["y"] + (params["to_y"] - params["y"]) * step / steps)
                self._move(window_id, rect, x, y)
        finally:
            self._send(self._mouse(4))
            self._held.discard(1)

    def _type_text(self, window_id, rect, text):
        for character in text:
            self._check(window_id, rect)
            encoded = character.encode("utf-16-le")
            units = [int.from_bytes(encoded[offset:offset + 2], "little")
                     for offset in range(0, len(encoded), 2)]
            # SendInput inserts one call's events without input interleaving.
            # Keep a supplementary character's surrogate pair together so a
            # local cancellation cannot leave only its high surrogate typed.
            inputs = [event for unit in units for event in
                      (self._keyboard(scan=unit, flags=4), self._keyboard(scan=unit, flags=6))]
            try:
                self._send(*inputs)
            except Exception:
                self._send(*(self._keyboard(scan=unit, flags=6) for unit in units))
                raise

    def _press_key(self, window_id, rect, key):
        keys = parse_key(key)
        extended = {33, 34, 35, 36, 37, 38, 39, 40, 45, 46}
        try:
            for vk in keys:
                self._check(window_id, rect)
                self._held.add(vk)
                self._send(self._keyboard(vk=vk, flags=1 if vk in extended else 0))
        finally:
            release_error = None
            for vk in reversed(keys):
                if vk in self._held:
                    try:
                        self._send(self._keyboard(vk=vk, flags=2 | (1 if vk in extended else 0)))
                    except BackendError as exc:
                        release_error = exc
                    finally:
                        self._held.discard(vk)
            if release_error:
                raise release_error


def parse_key(key: str) -> list[int]:
    aliases = {"CTRL": 17, "CONTROL": 17, "ALT": 18, "SHIFT": 16, "ENTER": 13,
               "RETURN": 13, "TAB": 9, "ESC": 27, "ESCAPE": 27, "SPACE": 32,
               "BACKSPACE": 8, "DELETE": 46, "INSERT": 45, "HOME": 36, "END": 35,
               "PAGEUP": 33, "PAGEDOWN": 34, "LEFT": 37, "UP": 38, "RIGHT": 39, "DOWN": 40}
    if not isinstance(key, str) or not key or len(key) > 80:
        raise BackendError("Invalid key combination")
    keys = []
    for token in key.upper().split("+"):
        token = token.strip()
        if token in aliases:
            keys.append(aliases[token])
        elif len(token) == 1 and token.isascii() and token.isalnum():
            keys.append(ord(token))
        elif token.startswith("F") and token[1:].isdigit() and 1 <= int(token[1:]) <= 24:
            keys.append(111 + int(token[1:]))
        else:
            raise BackendError(f"Unsupported key: {token}")
    if len(keys) > 4 or len(set(keys)) != len(keys):
        raise BackendError("Invalid key combination")
    return keys
