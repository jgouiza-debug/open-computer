"""Opt-in native input proof, restricted to this script's own disposable fixture."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk

from PIL import ImageGrab

from opencomputer.backend import WindowsBackend
from opencomputer.controller import Controller
from opencomputer.mcp import ClientError, LocalClient
from opencomputer.overlay import TargetOverlay
from opencomputer.server import start_server


def diagnose_focus(output: Path) -> dict:
    """Measure mapping/foreground behavior; inject no text, mouse, or key input."""
    backend = WindowsBackend()
    root = tk.Tk()
    root.title("Open Computer — focus diagnosis fixture")
    root.geometry("720x440+80+80")
    entry = tk.Entry(root)
    entry.pack(pady=50)
    overlay = TargetOverlay(root)
    controller = Controller(backend=backend)
    receipt = {"native_input": False, "diagnosis": "foreground"}

    def measure() -> None:
        try:
            root.update_idletasks()
            hwnd = backend.user32.GetAncestor(root.winfo_id(), 2)
            window = backend.get_window(str(hwnd))
            assert window["pid"] == os.getpid()
            receipt["target_hwnd"] = hwnd
            controller.select_window(str(hwnd))
            root.lift()
            root.focus_force()
            entry.focus_force()
            root.update()
            receipt["after_tk_focus"] = backend.user32.GetForegroundWindow()
            controller.arm()
            overlay.show_target(window["rect"])
            root.update()
            receipt["after_overlay"] = backend.user32.GetForegroundWindow()
            receipt["overlay_hwnds"] = [surface.native[1] for surface in (overlay.border, overlay.pointer)]
            observation = controller.get_window_state()
            receipt["after_capture"] = backend.user32.GetForegroundWindow()
            controller.execute("activate", observation["observation_id"])
            root.update()
            receipt["after_explicit_activate"] = backend.user32.GetForegroundWindow()
            receipt["activation_matches_target"] = receipt["after_explicit_activate"] == hwnd
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            controller.stop()
            overlay.destroy()
            root.destroy()

    root.after(400, measure)
    root.mainloop()
    output.mkdir(parents=True, exist_ok=True)
    (output / "focus-diagnosis.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def run(output: Path, wait_for_focus: int = 0) -> dict:
    """Create, arm, exercise, and close only a window owned by this process."""
    backend = WindowsBackend()
    root = tk.Tk()
    root.title("Open Computer — disposable native test fixture")
    root.geometry("720x440+80+80")
    root.attributes("-topmost", True)
    root.configure(bg="#102030")
    tk.Label(root, text="Native input fixture", bg="#102030", fg="white",
             font=("Segoe UI", 22, "bold")).pack(pady=(35, 12))
    tk.Label(root, text="Only this temporary window receives test input.",
             bg="#102030", fg="#b7d4ff").pack()
    entry = tk.Entry(root, font=("Segoe UI", 17), width=38)
    entry.pack(pady=25)
    clicks = tk.IntVar(value=0)
    button = tk.Button(root, text="Test click", font=("Segoe UI", 15),
                       command=lambda: clicks.set(clicks.get() + 1))
    button.pack(pady=8)
    tk.Label(root, textvariable=clicks, bg="#102030", fg="white",
             font=("Segoe UI", 16)).pack()
    overlay = TargetOverlay(root)
    events = queue.Queue()
    completed = queue.Queue()
    controller = Controller(backend=backend, event_sink=events.put)
    server = start_server(controller, port=0, connection_path=None)
    client = LocalClient(server.url, server.token)
    receipt = {"native": True, "fixture_pid": os.getpid(), "passed": False}
    output.mkdir(parents=True, exist_ok=True)
    focus_deadline = time.monotonic() + wait_for_focus
    focus_announced = False

    def cleanup() -> None:
        controller.stop()
        server.close()
        overlay.destroy()
        root.destroy()

    def worker(window: dict, point: tuple[int, int]) -> None:
        try:
            observation = client.call("get_window_state")
            client.call("type_text", {"observation_id": observation["observation_id"],
                                      "text": "Open Computer native input verified"})
            try:
                client.call("click", {"observation_id": observation["observation_id"], "x": point[0], "y": point[1]})
                raise AssertionError("A reused observation unexpectedly worked")
            except ClientError as exc:
                if exc.code != "stale_observation":
                    raise
            time.sleep(0.15)
            observation = client.call("get_window_state")
            client.call("click", {"observation_id": observation["observation_id"], "x": point[0], "y": point[1]})
            time.sleep(0.2)
            observation = client.call("get_window_state")
            (output / "native-capture.png").write_bytes(base64.b64decode(observation["image_base64"]))
            completed.put((True, window, point))
        except Exception as exc:
            completed.put((False, type(exc).__name__, str(exc)))

    def start() -> None:
        nonlocal focus_announced
        root.update_idletasks()
        hwnd = backend.user32.GetAncestor(root.winfo_id(), 2)
        window = backend.get_window(str(hwnd))
        if window["pid"] != os.getpid():
            raise AssertionError("Fixture ownership did not match current process")
        if wait_for_focus and backend.user32.GetForegroundWindow() != hwnd:
            if not focus_announced:
                print(f"Waiting for local click on disposable fixture HWND {hwnd} (maximum {wait_for_focus}s)", flush=True)
                focus_announced = True
            if time.monotonic() < focus_deadline:
                root.after(100, start)
                return
            receipt["error"] = "Fixture did not receive local foreground focus within the requested wait"
            cleanup()
            return
        controller.select_window(window["id"])
        if not wait_for_focus:
            root.lift()
            root.focus_force()
            entry.focus_force()
        else:
            entry.focus_set()
        root.update()
        controller.arm()  # --run explicitly authorizes only this fixture.
        overlay.show_target(window["rect"])
        point = (button.winfo_rootx() + button.winfo_width() // 2 - window["rect"]["x"],
                 button.winfo_rooty() + button.winfo_height() // 2 - window["rect"]["y"])
        # Let the user's activating click finish before native modifier checks.
        root.after(250, lambda: threading.Thread(target=worker, args=(window, point), daemon=True).start())

    def tick() -> None:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event["type"] == "action" and "x" in event:
                overlay.show_action(event["x"], event["y"])
        try:
            result = completed.get_nowait()
        except queue.Empty:
            root.after(25, tick)
            return
        if result[0]:
            window, point = result[1:]
            receipt.update(typed_text=entry.get(), button_clicks=clicks.get(),
                           reused_observation_rejected=True, window=window)
            receipt["passed"] = (entry.get() == "Open Computer native input verified" and clicks.get() == 1)
            overlay.show_action(window["rect"]["x"] + point[0], window["rect"]["y"] + point[1])
            root.update_idletasks()
            rect = window["rect"]
            ImageGrab.grab(bbox=(rect["left"], rect["top"], rect["right"], rect["bottom"]),
                           all_screens=True).save(output / "native-desktop-crop.png")
            styles = []
            for surface in (overlay.border, overlay.pointer):
                user32, overlay_hwnd = surface.native
                style = user32.GetWindowLongPtrW(overlay_hwnd, -20)
                styles.append({"transparent": bool(style & 0x20), "noactivate": bool(style & 0x08000000),
                               "layered": bool(style & 0x80000)})
            receipt["overlay_styles"] = styles
            receipt["passed"] &= all(all(style.values()) for style in styles)
        else:
            receipt["error"] = f"{result[1]}: {result[2]}"
        cleanup()

    root.after(500, start)
    root.after(50, tick)
    root.after((wait_for_focus + 15) * 1000, lambda: (receipt.update(error="Native fixture timeout"), cleanup()))
    root.protocol("WM_DELETE_WINDOW", cleanup)
    try:
        root.mainloop()
    finally:
        controller.stop()
        server.close()
    (output / "native-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize native input only into the temporary test window")
    parser.add_argument("--diagnose-focus", action="store_true", help="Only measure fixture/overlay foreground handles and explicit activation; no input")
    parser.add_argument("--wait-for-focus", type=int, default=0, metavar="SECONDS", help="Wait up to 60 seconds for a local click to give the fixture foreground focus")
    parser.add_argument("--output", type=Path, default=Path("artifacts/native-smoke"))
    options = parser.parse_args()
    if not options.run:
        parser.error("Pass --run to authorize the disposable native fixture")
    if sys.platform != "win32":
        parser.error("Native fixture requires Windows")
    if not 0 <= options.wait_for_focus <= 60:
        parser.error("--wait-for-focus must be from 0 to 60 seconds")
    receipt = diagnose_focus(options.output) if options.diagnose_focus else run(options.output, options.wait_for_focus)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt.get("passed", receipt.get("activation_matches_target", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
