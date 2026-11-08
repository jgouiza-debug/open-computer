"""Opt-in process-owned Win32 controls for read-only UIA observation benchmarks.

No arguments or --help opens no GUI. --run creates only this disposable fixture;
it never arms control, sends input, invokes a model, or inspects other windows.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

WIDTH, HEIGHT = 720, 440
MAX_CONTROLS = 16
EXPECTED_TEXT = "Open Computer model test"
PASSWORD_SENTINEL = "not-a-secret-fixture-value"


def control_specs(with_password: bool = False) -> list[dict]:
    controls = [
        {"control_id": 1101, "class_name": "STATIC", "kind": "label", "label": "Task text",
         "bounds": {"x": 32, "y": 32, "width": 600, "height": 24}},
        {"control_id": 1001, "class_name": "EDIT", "kind": "edit", "label": "Task text",
         "bounds": {"x": 32, "y": 62, "width": 648, "height": 32}},
        {"control_id": 1002, "class_name": "BUTTON", "kind": "button", "label": "Test click",
         "bounds": {"x": 32, "y": 126, "width": 150, "height": 40}},
        {"control_id": 1003, "class_name": "STATIC", "kind": "counter", "label": "Clicks: 0",
         "bounds": {"x": 32, "y": 182, "width": 250, "height": 28}},
        {"control_id": 1004, "class_name": "BUTTON", "kind": "decoy", "label": "Decoy",
         "bounds": {"x": 224, "y": 126, "width": 150, "height": 40}},
        {"control_id": 1005, "class_name": "STATIC", "kind": "decoy_counter", "label": "Decoy activations: 0",
         "bounds": {"x": 320, "y": 182, "width": 350, "height": 28}},
    ]
    if with_password:
        controls.extend([
            {"control_id": 1102, "class_name": "STATIC", "kind": "label", "label": "Password test",
             "bounds": {"x": 32, "y": 260, "width": 600, "height": 24}},
            {"control_id": 1006, "class_name": "EDIT", "kind": "edit", "label": "Password test",
             "password": True, "bounds": {"x": 32, "y": 290, "width": 648, "height": 32}},
        ])
    return controls


def fixture_metadata(pid: int, hwnd: int, controls: list[dict], *,
                     clicks: int = 0, decoy_clicks: int = 0,
                     task_text_exact_match: bool = False, state: str = "ready") -> dict:
    """Bound and allowlist discovery metadata; never export edit/password values."""
    if type(pid) is not int or pid <= 0 or type(hwnd) is not int or hwnd <= 0:
        raise ValueError("Fixture process and window identities must be positive integers")
    if len(controls) > MAX_CONTROLS:
        raise ValueError("Fixture control count exceeds its bound")
    seen = set()
    exported = []
    for control in controls:
        identifier = control.get("control_id")
        handle = control.get("hwnd")
        if type(identifier) is not int or identifier <= 0 or identifier in seen:
            raise ValueError("Fixture control IDs must be positive and unique")
        if type(handle) is not int or handle <= 0:
            raise ValueError("Fixture controls require their own native handles")
        seen.add(identifier)
        bounds = control["bounds"]
        if (any(type(bounds.get(key)) is not int for key in ("x", "y", "width", "height")) or
                bounds["x"] < 0 or bounds["y"] < 0 or bounds["width"] <= 0 or bounds["height"] <= 0 or
                bounds["x"] + bounds["width"] > WIDTH or bounds["y"] + bounds["height"] > HEIGHT):
            raise ValueError("Fixture control bounds must remain inside its client area")
        item = {key: control[key] for key in ("control_id", "hwnd", "class_name", "kind", "bounds")}
        item["password"] = control.get("password") is True
        # Ground-truth labels are safe fixture constants; password control names
        # and all editable values remain absent from exported metadata.
        if not item["password"]:
            item["label"] = str(control.get("label", ""))[:160]
        exported.append(item)
    return {"schema_version": 1, "fixture_pid": pid, "window_hwnd": hwnd,
            "client": {"width": WIDTH, "height": HEIGHT}, "controls": exported,
            "clicks": clicks, "decoy_clicks": decoy_clicks,
            "task_text_exact_match": task_text_exact_match, "state": state,
            "discovery_only": True, "live_identity_and_geometry_must_be_rechecked": True,
            "snapshot_at": datetime.now(timezone.utc).isoformat()}


class Win32Fixture:
    def __init__(self, output: Path, timeout: int, with_password: bool) -> None:
        if sys.platform != "win32":
            raise RuntimeError("The semantic fixture requires Windows")
        self.output, self.timeout = output, timeout
        self.specs = control_specs(with_password)
        self.hwnd = None
        self.controls: dict[int, int] = {}
        self.clicks = self.decoy_clicks = 0
        self.callback_error = None
        self.last_metadata = None
        self.close_reason = "closed"
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._bind()
        self.class_name = f"OpenComputerSemanticFixture_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.wndproc = self.WNDPROC(self._window_proc)

    def _bind(self) -> None:
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", self.WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        # Include MSG.lPrivate explicitly, including on 32-bit Python.
        class Message(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT), ("lPrivate", wintypes.DWORD)]

        self.WindowClass, self.Message = WindowClass, Message
        signatures = {
            "RegisterClassW": ([ctypes.POINTER(WindowClass)], wintypes.ATOM),
            "UnregisterClassW": ([wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            "CreateWindowExW": ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "DestroyWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "GetMessageW": ([ctypes.POINTER(Message), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int),
            "TranslateMessage": ([ctypes.POINTER(Message)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(Message)], ctypes.c_ssize_t),
            "IsDialogMessageW": ([wintypes.HWND, ctypes.POINTER(Message)], wintypes.BOOL),
            "PostQuitMessage": ([ctypes.c_int], None),
            "SetTimer": ([wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p], ctypes.c_size_t),
            "KillTimer": ([wintypes.HWND, ctypes.c_size_t], wintypes.BOOL),
            "AdjustWindowRectEx": ([ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "SetWindowPos": ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            "UpdateWindow": ([wintypes.HWND], wintypes.BOOL),
            "SendMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "SetWindowTextW": ([wintypes.HWND, wintypes.LPCWSTR], wintypes.BOOL),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "ScreenToClient": ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.user32, name)
            function.argtypes, function.restype = arguments, result
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.gdi32.GetStockObject.argtypes = [ctypes.c_int]
        self.gdi32.GetStockObject.restype = wintypes.HANDLE

    def _save(self, state: str) -> dict:
        controls = []
        for spec in self.specs:
            handle = self.controls.get(spec["control_id"])
            if not handle:
                continue
            owner = wintypes.DWORD()
            if not self.user32.GetWindowThreadProcessId(handle, ctypes.byref(owner)) or owner.value != os.getpid():
                raise RuntimeError("A fixture control is no longer owned by this process")
            rectangle = wintypes.RECT()
            if not self.user32.GetWindowRect(handle, ctypes.byref(rectangle)):
                raise ctypes.WinError(ctypes.get_last_error())
            origin = wintypes.POINT(rectangle.left, rectangle.top)
            if not self.user32.ScreenToClient(self.hwnd, ctypes.byref(origin)):
                raise ctypes.WinError(ctypes.get_last_error())
            controls.append({**spec, "hwnd": int(handle), "bounds": {"x": origin.x, "y": origin.y,
                "width": rectangle.right - rectangle.left, "height": rectangle.bottom - rectangle.top}})
        text = ctypes.create_unicode_buffer(512)
        # Only the known non-password edit is read; its value is never exported.
        self.user32.GetWindowTextW(self.controls[1001], text, len(text))
        metadata = fixture_metadata(os.getpid(), int(self.hwnd), controls, clicks=self.clicks,
            decoy_clicks=self.decoy_clicks, task_text_exact_match=text.value == EXPECTED_TEXT, state=state)
        metadata["close_reason"] = self.close_reason if state == "closed" else None
        metadata["timeout_seconds"] = self.timeout
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "fixture.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.last_metadata = metadata
        return metadata

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == 0x0111 and ((wparam >> 16) & 0xFFFF) == 0:  # WM_COMMAND / BN_CLICKED
                identifier = wparam & 0xFFFF
                if identifier == 1002 and lparam == self.controls.get(1002):
                    self.clicks += 1
                    self.user32.SetWindowTextW(self.controls[1003], f"Clicks: {self.clicks}")
                    self._save("ready")
                    return 0
                if identifier == 1004 and lparam == self.controls.get(1004):
                    self.decoy_clicks += 1
                    self.user32.SetWindowTextW(self.controls[1005], f"Decoy activations: {self.decoy_clicks}")
                    self._save("ready")
                    return 0
            if message == 0x0010 or (message == 0x0113 and wparam == 1):  # WM_CLOSE / own WM_TIMER
                self.close_reason = "timeout" if message == 0x0113 else "local_close"
                self._save("closed")
                self.user32.KillTimer(hwnd, 1)
                self.user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:  # WM_DESTROY
                self.user32.PostQuitMessage(0)
                return 0
            return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception as exc:
            self.callback_error = f"{type(exc).__name__}: {exc}"
            self.close_reason = "fixture_error"
            if self.hwnd and self.user32.IsWindow(self.hwnd):
                self.user32.DestroyWindow(self.hwnd)
            self.user32.PostQuitMessage(1)
            return 0

    def run(self) -> dict:
        instance = self.kernel32.GetModuleHandleW(None)
        registered = False
        try:
            if hasattr(self.user32, "SetProcessDpiAwarenessContext"):
                self.user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                self.user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
                self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            window_class = self.WindowClass()
            window_class.lpfnWndProc = self.wndproc
            window_class.hInstance = instance
            window_class.hbrBackground = wintypes.HBRUSH(16)  # COLOR_BTNFACE + 1
            window_class.lpszClassName = self.class_name
            if not self.user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            style = 0x00CA0000  # Caption, system menu, minimize; fixed client size.
            rectangle = wintypes.RECT(0, 0, WIDTH, HEIGHT)
            if not self.user32.AdjustWindowRectEx(ctypes.byref(rectangle), style, False, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            self.hwnd = self.user32.CreateWindowExW(0, self.class_name,
                "Open Computer — owned semantic fixture", style, 80, 80,
                rectangle.right - rectangle.left, rectangle.bottom - rectangle.top,
                None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            font = self.gdi32.GetStockObject(17)  # DEFAULT_GUI_FONT, not owned.
            for spec in self.specs:
                bounds = spec["bounds"]
                child_style = 0x50000000  # WS_CHILD | WS_VISIBLE
                if spec["class_name"] in {"EDIT", "BUTTON"}:
                    child_style |= 0x00010000  # WS_TABSTOP
                if spec["class_name"] == "EDIT":
                    child_style |= 0x00800080  # WS_BORDER | ES_AUTOHSCROLL
                    if spec.get("password"):
                        child_style |= 0x20  # ES_PASSWORD
                text = (PASSWORD_SENTINEL if spec.get("password") else "") if spec["class_name"] == "EDIT" else spec["label"]
                handle = self.user32.CreateWindowExW(0, spec["class_name"], text, child_style,
                    bounds["x"], bounds["y"], bounds["width"], bounds["height"], self.hwnd,
                    wintypes.HMENU(spec["control_id"]), instance, None)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.controls[spec["control_id"]] = int(handle)
                self.user32.SendMessageW(handle, 0x0030, font, 1)  # WM_SETFONT
                if spec["class_name"] == "EDIT":
                    self.user32.SendMessageW(handle, 0x00C5, 256, 0)  # EM_LIMITTEXT
            if not self.user32.SetTimer(self.hwnd, 1, self.timeout * 1000, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            if not self.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0040):
                raise ctypes.WinError(ctypes.get_last_error())
            self.user32.UpdateWindow(self.hwnd)
            metadata = self._save("ready")
            print(json.dumps({"fixture_pid": os.getpid(), "window_hwnd": int(self.hwnd),
                              "metadata_file": str(self.output / "fixture.json")}), flush=True)
            message = self.Message()
            while True:
                status = self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if status == 0:
                    break
                if not self.user32.IsDialogMessageW(self.hwnd, ctypes.byref(message)):
                    self.user32.TranslateMessage(ctypes.byref(message))
                    self.user32.DispatchMessageW(ctypes.byref(message))
            if self.callback_error:
                raise RuntimeError(self.callback_error)
            return {**(self.last_metadata or metadata), "clicks": self.clicks, "decoy_clicks": self.decoy_clicks,
                    "state": "closed", "close_reason": self.close_reason}
        finally:
            if self.hwnd and self.user32.IsWindow(self.hwnd):
                self.user32.KillTimer(self.hwnd, 1)
                self.user32.DestroyWindow(self.hwnd)
            if registered:
                self.user32.UnregisterClassW(self.class_name, instance)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Open only the owned native fixture")
    parser.add_argument("--timeout", type=int, default=180, help="Close after 1–600 seconds (default 180)")
    parser.add_argument("--with-password", action="store_true", help="Add a non-secret password redaction test field")
    parser.add_argument("--output", type=Path, default=Path("artifacts/semantic-fixture"))
    options = parser.parse_args(argv)
    if not options.run:
        parser.print_help()
        return 0
    if sys.platform != "win32":
        parser.error("--run requires Windows")
    if not 1 <= options.timeout <= 600:
        parser.error("--timeout must be from 1 to 600 seconds")
    output = options.output / (datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    try:
        result = Win32Fixture(output, options.timeout, options.with_password).run()
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Owned semantic fixture failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
