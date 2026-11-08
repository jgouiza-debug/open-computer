"""Opt-in native outline proof using only a disposable process-owned window.

No arguments/help creates no windows. --run permits own fixture presentation and
movement/minimization only. No SendInput, model, user-file or panel operations.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes as w
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

REQUIRED_EXSTYLE = 0x80000 | 0x20 | 0x80 | 0x08000000
WS_DISABLED = 0x08000000


def passive_style(exstyle: int, style: int, hit_test: int) -> bool:
    return exstyle & REQUIRED_EXSTYLE == REQUIRED_EXSTYLE and bool(style & WS_DISABLED) and hit_test == -1


def rect_tuple(rect: dict | tuple | list) -> tuple[int, int, int, int]:
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        x, y, width, height = rect
        return x, y, x + width, y + height
    if all(key in rect for key in ("left", "top", "right", "bottom")):
        return tuple(int(rect[key]) for key in ("left", "top", "right", "bottom"))
    return rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"]


def expected_bars(client: tuple[int, int, int, int], thickness: int, mode: str) -> list[tuple[int, int, int, int]]:
    left, top, right, bottom = client
    if mode == "outside":
        return [(left-thickness, top-thickness, right+thickness, top),
                (left-thickness, bottom, right+thickness, bottom+thickness),
                (left-thickness, top, left, bottom), (right, top, right+thickness, bottom)]
    if mode == "inset":
        return [(left, top, right, top+thickness), (left, bottom-thickness, right, bottom),
                (left, top+thickness, left+thickness, bottom-thickness),
                (right-thickness, top+thickness, right, bottom-thickness)]
    raise RuntimeError("Unknown native outline placement mode")


def pixels(frame: dict) -> tuple[tuple[int, int], bytes]:
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(frame["image_base64"], validate=True))) as image:
        rgb = image.convert("RGB")
        return rgb.size, rgb.tobytes()


def wait_until(predicate, timeout: float = 3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise RuntimeError("Owned outline proof stage exceeded its deadline")


class NativeApi:
    def __init__(self) -> None:
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        signatures = {
            "GetForegroundWindow": ([], w.HWND),
            "GetWindowLongPtrW": ([w.HWND, ctypes.c_int], ctypes.c_ssize_t),
            "GetWindowRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "IsWindow": ([w.HWND], w.BOOL), "IsWindowVisible": ([w.HWND], w.BOOL),
            "GetWindowDisplayAffinity": ([w.HWND, ctypes.POINTER(w.DWORD)], w.BOOL),
            "SendMessageTimeoutW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
                                      ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
            "SetWindowPos": ([w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT], w.BOOL),
            "ShowWindow": ([w.HWND, ctypes.c_int], w.BOOL),
            "PostMessageW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def rectangle(self, hwnd: int) -> tuple[int, int, int, int]:
        result = w.RECT()
        if not self.api.GetWindowRect(hwnd, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return result.left, result.top, result.right, result.bottom

    def own(self, hwnd: int) -> None:
        pid = w.DWORD()
        if not self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or pid.value != os.getpid():
            raise RuntimeError("Proof refused a window outside its own process")

    def bars(self, diagnostics: dict) -> list[dict]:
        result = []
        handles = diagnostics.get("windows", [])
        if len(handles) != 4:
            raise RuntimeError("Native outline did not expose exactly four bars")
        for hwnd in handles:
            self.own(hwnd)
            rect = self.rectangle(hwnd)
            point = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            lparam = (point[0] & 0xffff) | ((point[1] & 0xffff) << 16)
            response = ctypes.c_size_t()
            # Hit-test only. No mouse/key messages or input injection is sent.
            if not self.api.SendMessageTimeoutW(hwnd, 0x84, 0, lparam, 2, 500, ctypes.byref(response)):
                raise ctypes.WinError(ctypes.get_last_error())
            hit = ctypes.c_ssize_t(response.value).value
            exstyle, style = self.api.GetWindowLongPtrW(hwnd, -20), self.api.GetWindowLongPtrW(hwnd, -16)
            affinity = w.DWORD()
            affinity_read = bool(self.api.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
            result.append({"hwnd": int(hwnd), "rect": list(rect), "visible": bool(self.api.IsWindowVisible(hwnd)),
                "exstyle": exstyle, "style": style, "hit_test": hit,
                "passive": passive_style(exstyle, style, hit), "affinity_read": affinity_read,
                "affinity": int(affinity.value) if affinity_read else None})
        return result

    def move(self, hwnd: int, x: int, y: int) -> None:
        self.own(hwnd)
        left, top, right, bottom = self.rectangle(hwnd)
        if not self.api.SetWindowPos(hwnd, None, x, y, right - left, bottom - top, 0x10 | 0x4):
            raise ctypes.WinError(ctypes.get_last_error())

    def place(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        self.own(hwnd)
        left, top, right, bottom = rect
        if not self.api.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, 0x10 | 0x4):
            raise ctypes.WinError(ctypes.get_last_error())

    def monitors(self) -> list[tuple[int, int, int, int]]:
        rectangles = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
        @callback_type
        def collect(monitor, dc, rect, data):
            value = rect.contents
            rectangles.append((value.left, value.top, value.right, value.bottom))
            return True
        self.api.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
        self.api.EnumDisplayMonitors.restype = w.BOOL
        if not self.api.EnumDisplayMonitors(None, None, collect, 0) or not rectangles:
            raise ctypes.WinError(ctypes.get_last_error())
        return rectangles


class SpyBackend:
    """Real own-window reads, with every native input operation prohibited."""
    real = True

    def __init__(self, backend, hwnd: int) -> None:
        self.backend, self.hwnd, self.input_attempts = backend, str(hwnd), []

    def get_window(self, hwnd):
        if str(hwnd) != self.hwnd:
            raise RuntimeError("Proof backend refused another target")
        return self.backend.get_window(hwnd)

    def process_identity(self, hwnd):
        self.get_window(hwnd)
        return self.backend.process_identity(hwnd)

    def capture(self, hwnd):
        self.get_window(hwnd)
        return self.backend.capture(hwnd)

    def set_cancel_event(self, event):
        self.backend.set_cancel_event(event)

    def perform(self, action, hwnd, **arguments):
        self.input_attempts.append(action)
        raise AssertionError("Native input is forbidden in the outline proof")


class HealthProxy:
    def __init__(self, outline) -> None:
        self.outline, self.inject_failure = outline, False

    def check_health(self):
        if self.inject_failure:
            raise RuntimeError("Injected outline-health proof failure")
        return self.outline.check_health()

    def __getattr__(self, name):
        return getattr(self.outline, name)


def run_proof(output: Path, selected_case: str, timeout: int) -> dict:
    from opencomputer.backend import WindowsBackend
    from opencomputer.headless import HeadlessSession
    from opencomputer.headless_overlay import NativeOutline
    from scripts.win32_semantic_fixture import Win32Fixture

    native = NativeApi()
    foreground = int(native.api.GetForegroundWindow() or 0)
    report = {"schema": "opencomputer.headless-outline-proof/1", "passed": False, "pid": os.getpid(),
              "foreground_before": foreground, "selected_case": selected_case, "stages": [],
              "native_input_attempts": 0, "model_requests": 0, "screen_capture": "own target PNG only"}
    holder, fixture_ready = {}, threading.Event()
    owned_outlines = []

    class Fixture(Win32Fixture):
        def _save(self, state):
            result = super()._save(state)
            if state == "ready":
                fixture_ready.set()
            return result

    def fixture_worker():
        try:
            fixture = Fixture(output / "fixture", timeout, False)
            holder["fixture"] = fixture
            holder["result"] = fixture.run()
        except BaseException as exc:
            holder["error"] = type(exc).__name__
            fixture_ready.set()

    thread = threading.Thread(target=fixture_worker, name="owned-outline-fixture", daemon=True)
    thread.start()
    spy = None
    try:
        if not fixture_ready.wait(5) or holder.get("error") or not holder.get("fixture", None):
            raise RuntimeError("Owned native fixture did not become ready")
        fixture = holder["fixture"]
        hwnd = int(fixture.hwnd)
        native.own(hwnd)
        backend = WindowsBackend()
        spy = SpyBackend(backend, hwnd)
        identity = spy.process_identity(hwnd)
        report["target"] = identity
        outer_initial = native.rectangle(hwnd)
        def stable_capture():
            previous = None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                frame = spy.capture(hwnd)
                current = pixels(frame)
                if current == previous:
                    return frame
                previous = current
                time.sleep(.1)
            raise RuntimeError("Owned fixture pixels did not settle before outline presentation")
        baseline = stable_capture()
        (output / "target-without-outline.png").write_bytes(base64.b64decode(baseline["image_base64"], validate=True))
        baseline_pixels = pixels(baseline)

        def new_session(*, ttl=10, failing=False, read_only=False):
            created = []
            def factory():
                outline = NativeOutline()
                result = HealthProxy(outline) if failing else outline
                created.append(result)
                owned_outlines.append(result)
                return result
            started = time.perf_counter()
            session = HeadlessSession(spy, window_id=str(hwnd), pid=os.getpid(),
                process_start=identity["process_start"], allow_input=not read_only, ttl=ttl, outline_factory=factory)
            if len(created) != 1:
                session.close()
                raise RuntimeError("Headless session did not construct exactly one native outline")
            return session, created[0], round((time.perf_counter() - started) * 1000, 3)

        def visible(outline):
            return wait_until(lambda: outline.diagnostics() if outline.diagnostics().get("visible") else None)

        def check_visible(outline, stage):
            diagnostic = visible(outline)
            bars = native.bars(diagnostic)
            if not all(bar["passive"] and bar["visible"] for bar in bars):
                raise RuntimeError("A native bar is interactive or not visible")
            client = rect_tuple(spy.get_window(hwnd)["rect"])
            expected = expected_bars(client, diagnostic["thickness"], diagnostic["edge_mode"])
            if ([tuple(bar["rect"]) for bar in bars] != expected
                    or [rect_tuple(rect) for rect in diagnostic["bar_rects"]] != expected
                    or rect_tuple(diagnostic["rect"]) != client):
                raise RuntimeError("Native frame geometry does not match the actual owned client")
            actual = spy.capture(hwnd)
            equal = pixels(actual) == baseline_pixels
            stage.update(diagnostics=diagnostic, bars=bars, target_pixels_equal=equal,
                         foreground_after=int(native.api.GetForegroundWindow() or 0))
            if not equal:
                raise RuntimeError("Native outline changed the target capture")
            if stage["foreground_after"] != foreground:
                raise RuntimeError("Foreground changed while presenting the passive outline")
            if diagnostic.get("edge_mode") != "outside" and not all(
                    bar["affinity_read"] and bar["affinity"] == 0x11 for bar in bars):
                raise RuntimeError("Inset outline lacks verified capture exclusion")
            return actual

        cases = ["normal", "readonly", "edge", "monitor-fill", "minimize", "expiry", "fault"] if selected_case == "all" else [selected_case]
        for case in cases:
            stage = {"case": case, "passed": False}
            report["stages"].append(stage)
            session = None
            outline = None
            try:
                # Restoring/moving only our own fixture uses NOACTIVATE throughout.
                native.api.ShowWindow(hwnd, 4)
                native.place(hwnd, outer_initial)
                if case in {"edge", "monitor-fill"}:
                    monitor = min(native.monitors(), key=lambda rect: rect[0])
                    stage["physical_monitor"] = list(monitor)
                    client = spy.get_window(hwnd)["rect"]
                    dx, dy = client["x"] - outer_initial[0], client["y"] - outer_initial[1]
                    if case == "edge":
                        native.move(hwnd, monitor[0] - dx, monitor[1] + 64 - dy)
                    else:
                        # Equivalent monitor-fill client geometry, without the
                        # activating SW_MAXIMIZE command or a fullscreen claim.
                        right_margin = outer_initial[2] - client["right"]
                        bottom_margin = outer_initial[3] - client["bottom"]
                        native.place(hwnd, (monitor[0] - dx, monitor[1] - dy,
                            monitor[2] + right_margin, monitor[3] + bottom_margin))
                baseline_pixels = pixels(stable_capture())
                session, outline, startup_ms = new_session(ttl=1 if case == "expiry" else 10,
                    failing=case == "fault", read_only=case == "readonly")
                stage["startup_ms"] = startup_ms
                check_visible(outline, stage)
                if case == "readonly" and session.controller.status()["armed"]:
                    raise RuntimeError("Read-only outline proof unexpectedly armed input")
                if case == "normal":
                    native.move(hwnd, outer_initial[0] + 80, outer_initial[1] + 60)
                    moved = spy.get_window(hwnd)
                    wait_until(lambda: rect_tuple(outline.diagnostics()["rect"]) == rect_tuple(moved["rect"]))
                    frame = check_visible(outline, stage)
                    (output / "target-with-outline.png").write_bytes(base64.b64decode(frame["image_base64"], validate=True))
                    stage["moved_rect"] = moved["rect"]
                elif case in {"edge", "monitor-fill"}:
                    check_visible(outline, stage)
                elif case == "minimize":
                    native.api.ShowWindow(hwnd, 7)  # SW_SHOWMINNOACTIVE.
                    wait_until(lambda: not outline.diagnostics().get("visible"))
                    if session.controller.status()["armed"]:
                        raise RuntimeError("Minimized-target outline hid without revoking input")
                elif case == "expiry":
                    wait_until(lambda: session.controller.status()["stopped"], 2.5)
                    wait_until(lambda: not outline.diagnostics().get("visible"))
                elif case == "fault":
                    outline.inject_failure = True
                    wait_until(lambda: session.controller.status()["stopped"])
                    wait_until(lambda: not outline.diagnostics().get("visible"))
                    stage["fault_kind"] = "injected Python feedback-health failure"
                started_stop = time.perf_counter()
                session.stop()
                wait_until(lambda: not outline.diagnostics().get("visible"))
                stage["stop_hide_ms"] = round((time.perf_counter() - started_stop) * 1000, 3)
                if any(native.api.IsWindowVisible(handle) for handle in outline.diagnostics().get("windows", [])):
                    raise RuntimeError("Native bars remained visible after stop")
                stage["passed"] = True
            except Exception as exc:
                stage["error"] = {"type": type(exc).__name__, "message": str(exc)}
                if outline is not None:
                    stage["failure_diagnostics"] = outline.diagnostics()
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception as exc:
                        stage["passed"] = False
                        stage["cleanup_error"] = type(exc).__name__
        report["passed"] = bool(report["stages"]) and all(stage["passed"] for stage in report["stages"])
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        for outline in owned_outlines:
            try:
                outline.close()
                if any(native.api.IsWindow(handle) for handle in outline.diagnostics().get("windows", [])):
                    raise RuntimeError("Owned native frame window survived outline close")
            except Exception as exc:
                report.setdefault("outline_cleanup_errors", []).append(type(exc).__name__)
        fixture = holder.get("fixture")
        try:
            if fixture is not None and fixture.hwnd and native.api.IsWindow(fixture.hwnd):
                native.own(int(fixture.hwnd))
                if not native.api.PostMessageW(fixture.hwnd, 0x10, 0, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
        except Exception as exc:
            report["fixture_close_error"] = type(exc).__name__
        thread.join(timeout=5)
        report["fixture_thread_exited"] = not thread.is_alive()
        report["fixture_error"] = holder.get("error")
        report["native_input_attempts"] = len(spy.input_attempts) if spy else 0
        report["foreground_after"] = int(native.api.GetForegroundWindow() or 0)
        report["tk_modules_loaded"] = sorted(name for name in sys.modules if name == "tkinter" or name.startswith("tkinter."))
        report["passed"] = bool(report["passed"] and not report["native_input_attempts"]
            and report["foreground_after"] == foreground and report["fixture_thread_exited"]
            and not report["fixture_error"] and not report.get("fixture_close_error")
            and not report.get("outline_cleanup_errors") and not report["tk_modules_loaded"])
        (output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=("all", "normal", "readonly", "edge", "monitor-fill", "minimize", "expiry", "fault"), default="all")
    parser.add_argument("--timeout", type=int, default=40)
    options = parser.parse_args(argv)
    if not options.run:
        parser.error("--run is required; no window was created")
    if sys.platform != "win32" or options.output is None or not 10 <= options.timeout <= 120:
        parser.error("Windows, an explicit fresh output directory and timeout10..120 required")
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    return 0 if run_proof(output, options.case, options.timeout)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
