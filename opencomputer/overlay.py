"""Non-interactive Windows target outline and clearly labelled AI pointer."""

from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from typing import Mapping

BLUE = "#438fff"
TRANSPARENT = "#fa00fa"


def rectangle_bounds(rect: Mapping[str, object]) -> tuple[int, int, int, int]:
    """Normalize the public window rectangle without allowing empty overlays."""
    left = int(rect.get("left", rect.get("x", 0)))
    top = int(rect.get("top", rect.get("y", 0)))
    right = int(rect["right"]) if "right" in rect else left + int(rect["width"])
    bottom = int(rect["bottom"]) if "bottom" in rect else top + int(rect["height"])
    if right <= left or bottom <= top:
        raise ValueError("Target rectangle must have positive width and height")
    return left, top, right, bottom


def _native_window(window: tk.Toplevel) -> tuple[object, int] | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                wintypes.BYTE, wintypes.DWORD]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    # Layered, transparent, tool window, and NOACTIVATE: input goes to the target.
    style = user32.GetWindowLongPtrW(hwnd, -20)
    required = 0x80000 | 0x20 | 0x80 | 0x08000000
    ctypes.set_last_error(0)
    previous = user32.SetWindowLongPtrW(hwnd, -20, style | required)
    error = ctypes.get_last_error()
    if (previous == 0 and error) or user32.GetWindowLongPtrW(hwnd, -20) & required != required:
        raise OSError(error, "Cannot establish a non-interactive overlay window")
    if not user32.SetLayeredWindowAttributes(hwnd, 0xFA00FA, 255, 1):
        raise ctypes.WinError(ctypes.get_last_error())
    # Keep the artificial pointer/outline out of screenshot observations where supported.
    user32.SetWindowDisplayAffinity(hwnd, 0x11)
    return user32, hwnd


class _PassiveSurface:
    def __init__(self, root: tk.Misc) -> None:
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=TRANSPARENT)
        self.window.attributes("-topmost", True)
        if sys.platform == "win32":
            self.window.attributes("-transparentcolor", TRANSPARENT)
        self.canvas = tk.Canvas(self.window, bg=TRANSPARENT, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()
        self.native = _native_window(self.window)
        self.visible = False

    def place(self, left: int, top: int, width: int, height: int) -> None:
        # NOACTIVATE is installed before the first mapping. Override-redirect also
        # avoids the ordinary window-manager activation path.
        self.window.geometry(f"{width}x{height}{left:+d}{top:+d}")
        if not self.visible:
            self.window.deiconify()
            self.visible = True
        if self.native:
            user32, hwnd = self.native
            if not user32.SetWindowPos(hwnd, -1, left, top, width, height, 0x10 | 0x40):
                raise ctypes.WinError(ctypes.get_last_error())
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE

    def hide(self) -> None:
        self.window.withdraw()
        self.visible = False

    def destroy(self) -> None:
        self.window.destroy()


class TargetOverlay:
    """All methods must be invoked on the Tk main thread; never injects input."""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.border = _PassiveSurface(root)
        self.pointer = _PassiveSurface(root)
        self._rect: tuple[int, int, int, int] | None = None
        self._animation: str | None = None
        self._pointer_started = 0.0

    def show_target(self, rect: Mapping[str, object]) -> None:
        bounds = rectangle_bounds(rect)
        if bounds == self._rect and self.border.visible:
            return
        self._rect = bounds
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        self.border.place(left, top, width, height)
        canvas = self.border.canvas
        canvas.delete("all")
        canvas.create_rectangle(2, 2, width - 3, height - 3, outline=BLUE, width=3)
        for x, y, dx, dy in ((4, 4, 1, 1), (width - 5, 4, -1, 1),
                              (4, height - 5, 1, -1), (width - 5, height - 5, -1, -1)):
            canvas.create_line(x, y + 19 * dy, x, y, x + 19 * dx, y,
                               fill="#b8d6ff", width=3)
        canvas.create_rectangle(14, 3, 121, 25, fill=BLUE, outline=BLUE)
        canvas.create_text(67, 14, text="AI CONTROL", fill="#ffffff",
                           font=("Segoe UI", 9, "bold"))

    def show_action(self, x: int, y: int) -> None:
        if self._animation is not None:
            self.root.after_cancel(self._animation)
            self._animation = None
        self.pointer.place(int(x) - 42, int(y) - 42, 100, 100)
        self._pointer_started = time.monotonic()
        self._draw_pointer()

    def _draw_pointer(self) -> None:
        progress = min(1.0, (time.monotonic() - self._pointer_started) / 0.65)
        canvas = self.pointer.canvas
        canvas.delete("all")
        radius = 10 + 23 * progress
        canvas.create_oval(42 - radius, 42 - radius, 42 + radius, 42 + radius,
                           outline=BLUE, width=3 if progress < 0.5 else 2)
        canvas.create_polygon(42, 42, 45, 67, 52, 60, 59, 72, 65, 68,
                              57, 56, 68, 54, fill=BLUE, outline="white", width=2)
        # A solid badge avoids color-key fringes around anti-aliased text.
        canvas.create_rectangle(63, 73, 88, 91, fill=BLUE, outline=BLUE)
        canvas.create_text(76, 82, text="AI", fill="white", font=("Segoe UI", 9, "bold"))
        if progress < 1:
            self._animation = self.root.after(25, self._draw_pointer)
        else:
            self._animation = None
            self.pointer.hide()

    def hide(self) -> None:
        self.border.hide()
        self.pointer.hide()
        self._rect = None
        if self._animation is not None:
            self.root.after_cancel(self._animation)
            self._animation = None

    def destroy(self) -> None:
        self.hide()
        self.border.destroy()
        self.pointer.destroy()
