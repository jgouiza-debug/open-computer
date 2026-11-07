"""Open Computer's local-only arming panel. Run with ``python -m opencomputer.app``."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any

from .overlay import BLUE, TargetOverlay

BG = "#0b0f17"
PANEL = "#111824"
CARD = "#182231"
TEXT = "#ecf2fc"
MUTED = "#94a3ba"
GREEN = "#67dfb1"
RED = "#ff7185"


def event_description(event: dict[str, Any]) -> str:
    """Describe activity without echoing typed text, clipboard data, or tokens."""
    kind = str(event.get("type", event.get("event", "activity")))
    if kind == "action":
        action = str(event.get("action", "action"))
        return f"AI action: {action}" + (
            f" at ({event['x']}, {event['y']})" if "x" in event and "y" in event else "")
    if kind == "error":
        return str(event.get("message", event.get("error", "Action rejected")))[:350]
    return {"armed": "Target armed by local user", "disarmed": "Target disarmed",
            "stopped": "Emergency stop activated", "target": "Target selection updated",
            "reset": "Emergency stop reset; target remains disarmed"}.get(
                kind, kind.replace("_", " ").capitalize())


class SafetyHotkey:
    """A separate read-only key-state watcher keeps stop usable during UI work."""

    def __init__(self, stop_callback: Any) -> None:
        self.done = threading.Event()
        self.thread: threading.Thread | None = None
        if sys.platform == "win32":
            def watch() -> None:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
                user32.GetAsyncKeyState.restype = ctypes.c_short
                held = False
                while not self.done.wait(0.05):
                    pressed = all(user32.GetAsyncKeyState(key) & 0x8000
                                  for key in (0x11, 0x12, 0x1B))
                    if pressed and not held:
                        stop_callback()
                    held = pressed
            self.thread = threading.Thread(target=watch, name="local-stop-hotkey", daemon=True)
            self.thread.start()

    def close(self) -> None:
        self.done.set()


class OpenComputerApp:
    def __init__(self, root: tk.Tk, *, demo: bool = False, port: int = 8787,
                 server_enabled: bool = True, controller: Any = None) -> None:
        from .controller import Controller
        from .backend import FakeBackend

        self.root = root
        self.demo = demo
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.controller = controller or Controller(
            backend=FakeBackend() if demo else None, event_sink=self.events.put)
        self.server: Any = None
        self.overlay: TargetOverlay | None = None
        self.windows: list[dict[str, Any]] = []
        self.closed = False
        self.overlay_failed = False
        self._last_state: dict[str, Any] = {}
        self._build_ui()
        try:
            self.overlay = TargetOverlay(root)
        except Exception as exc:
            self.overlay_failed = True
            self.controller.stop()
            self._log(f"Overlay unavailable. Control stopped: {exc}", "error")
        self.hotkey = SafetyHotkey(self.controller.stop)
        if server_enabled:
            try:
                from .server import start_server
                self.server = start_server(self.controller, host="127.0.0.1", port=port)
                self.connection.set("LOCAL SERVER ONLINE")
                self.endpoint.set(self.server.url)
                self.connection_file.set(str(self.server.connection_path))
                self._log("Loopback server started. Connect an agent using the connection file.")
            except Exception as exc:
                self.controller.stop()
                self.connection.set("SERVER UNAVAILABLE")
                self._log(f"Server could not start: {exc}", "error")
        else:
            self.connection.set("LOCAL PREVIEW · SERVER OFF")
            self.endpoint.set("No agent connection in this session")
        self._log("Demo backend · desktop input is simulated." if demo else
                  "Ready. Select a window, then arm it locally to allow agent actions.")
        self.refresh_windows()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-Alt-Escape>", lambda _event: self._stop())
        self._tick()

    def _build_ui(self) -> None:
        root = self.root
        root.title("Open Computer — local desktop control" + (" · DEMO" if self.demo else ""))
        root.geometry("1080x740")
        root.minsize(850, 650)
        root.configure(bg=BG)
        root.option_add("*Font", "{Segoe UI} 10")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor="#2c3b52", padding=9)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", CARD)],
                  selectforeground=[("readonly", TEXT)])
        root.option_add("*TCombobox*Listbox.background", CARD)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=32, pady=(26, 22))
        tk.Label(header, text="◈", fg=BLUE, bg=BG, font=("Segoe UI", 28, "bold")).pack(side="left")
        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=13)
        tk.Label(titles, text="Open Computer", bg=BG, fg=TEXT,
                 font=("Segoe UI", 22, "bold")).pack(anchor="w")
        tk.Label(titles, text="YOUR DESKTOP. YOUR CONTROL.", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(3, 0))
        if self.demo:
            tk.Label(header, text=" DEMO · SIMULATED INPUT ", bg="#253454", fg="#b7d4ff",
                     padx=12, pady=8, font=("Segoe UI", 9, "bold")).pack(side="right")
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=32, pady=(0, 26))
        sidebar = tk.Frame(body, bg=PANEL, width=280, padx=20, pady=21)
        sidebar.pack(side="left", fill="y", padx=(0, 18))
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="CONTROL STATUS", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.status_text = tk.StringVar(value="Disarmed")
        self.status_label = tk.Label(sidebar, textvariable=self.status_text, bg=PANEL,
                                     fg=MUTED, font=("Segoe UI", 25, "bold"))
        self.status_label.pack(anchor="w", pady=(13, 6))
        self.status_detail = tk.StringVar(value="Agent actions are blocked.")
        tk.Label(sidebar, textvariable=self.status_detail, bg=PANEL, fg=MUTED,
                 wraplength=237, justify="left").pack(anchor="w")
        self.arm_button = self._button(sidebar, "ARM SELECTED WINDOW", self._arm, BLUE, "#ffffff")
        self.arm_button.pack(fill="x", pady=(25, 9))
        self._button(sidebar, "Disarm", self._disarm).pack(fill="x")
        self.stop_button = self._button(sidebar, "■  EMERGENCY STOP", self._stop, "#472632", "#ff9dad")
        self.stop_button.pack(fill="x", pady=(20, 5))
        tk.Label(sidebar, text="Ctrl + Alt + Esc · global stop", bg=PANEL,
                 fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 15))
        self.reset_button = self._button(sidebar, "Reset emergency stop", self._reset)
        self.reset_button.pack(fill="x")
        tk.Frame(sidebar, bg="#243046", height=1).pack(fill="x", pady=24)
        tk.Label(sidebar, text="VISIBLE BY DESIGN", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(sidebar, text="A blue outline marks the armed window.\nA blue AI pointer shows agent actions.",
                 bg=PANEL, fg=MUTED, wraplength=235, justify="left").pack(anchor="w", pady=(10, 0))
        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        target_card = tk.Frame(main, bg=PANEL, padx=22, pady=19)
        target_card.pack(fill="x")
        row = tk.Frame(target_card, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="Target window", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self._button(row, "↻  Refresh", self.refresh_windows).pack(side="right")
        self.target_combo = ttk.Combobox(target_card, state="readonly")
        self.target_combo.pack(fill="x", pady=(16, 10))
        self.target_combo.bind("<<ComboboxSelected>>", self._select_window)
        tk.Label(target_card, text="Selecting a different window disarms control. Arming requires this panel.",
                 bg=PANEL, fg=MUTED, justify="left", wraplength=600,
                 font=("Segoe UI", 9)).pack(anchor="w")
        connection_card = tk.Frame(main, bg=PANEL, padx=22, pady=18)
        connection_card.pack(fill="x", pady=(14, 14))
        self.connection = tk.StringVar(value="STARTING LOCAL SERVER")
        tk.Label(connection_card, textvariable=self.connection, bg=PANEL, fg=GREEN,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.endpoint = tk.StringVar(value="Connecting…")
        tk.Label(connection_card, textvariable=self.endpoint, bg=PANEL, fg=TEXT,
                 font=("Consolas", 11)).pack(anchor="w", pady=(9, 4))
        self.connection_file = tk.StringVar(value="")
        tk.Label(connection_card, textvariable=self.connection_file, bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9), wraplength=600, justify="left").pack(anchor="w")
        log_card = tk.Frame(main, bg=PANEL, padx=22, pady=18)
        log_card.pack(fill="both", expand=True)
        tk.Label(log_card, text="Activity", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 12))
        self.log_widget = tk.Text(log_card, bg=PANEL, fg=MUTED, font=("Consolas", 9),
                                  wrap="word", relief="flat", highlightthickness=0,
                                  state="disabled", height=9, cursor="arrow")
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.tag_configure("error", foreground=RED)
        self.log_widget.tag_configure("action", foreground="#9cc5ff")

    @staticmethod
    def _button(parent: tk.Misc, text: str, command: Any,
                background: str = CARD, foreground: str = TEXT) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=background, fg=foreground,
                         activebackground="#2d4366", activeforeground="white", relief="flat",
                         bd=0, padx=12, pady=10, cursor="hand2", font=("Segoe UI", 9, "bold"),
                         highlightthickness=1, highlightbackground=background,
                         highlightcolor=BLUE, disabledforeground="#6e7b90")

    def _log(self, message: str, tag: str = "") -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{datetime.now():%H:%M:%S}  {message}\n", tag)
        if int(self.log_widget.index("end-1c").split(".")[0]) > 500:
            self.log_widget.delete("1.0", "100.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _call(self, operation: Any) -> Any:
        try:
            return operation()
        except Exception as exc:
            self._log(str(exc), "error")
            return None

    def refresh_windows(self) -> None:
        windows = self._call(self.controller.list_windows)
        if windows is None:
            return
        self.windows = windows
        self.target_combo["values"] = [f"{w['title']}  ·  {w['id']}" for w in windows]
        selected = self._last_state.get("selected_window")
        match = next((index for index, window in enumerate(windows)
                      if selected and window["id"] == selected["id"]), -1)
        if match >= 0:
            self.target_combo.current(match)
        else:
            self.target_combo.set("Choose a visible window" if windows else "No eligible windows found")

    def _select_window(self, _event: Any = None) -> None:
        index = self.target_combo.current()
        if 0 <= index < len(self.windows):
            self._call(lambda: self.controller.select_window(self.windows[index]["id"]))
            self._sync_state()

    def _arm(self) -> None:
        if self.overlay_failed:
            self._log("Control blocked: the visible target overlay is unavailable.", "error")
            return
        # This method is invoked only by the native button; the server has no arm route.
        self._call(self.controller.arm)
        self._sync_state()

    def _disarm(self) -> None:
        self._call(self.controller.disarm)
        self._sync_state()

    def _stop(self) -> None:
        self._call(self.controller.stop)
        self._sync_state()

    def _reset(self) -> None:
        if not self.overlay_failed:
            self._call(self.controller.reset_stop)
            self._sync_state()

    def _sync_state(self) -> None:
        state = self.controller.status()
        self._last_state = state
        selected = state.get("selected_window")
        if selected:
            match = next((index for index, window in enumerate(self.windows)
                          if window["id"] == selected["id"]), -1)
            if match >= 0:
                if self.target_combo.current() != match:
                    self.target_combo.current(match)
            else:
                self.target_combo.set(f"{selected['title']}  ·  {selected['id']}")
        armed = bool(state.get("armed"))
        stopped = bool(state.get("stopped"))
        self.status_text.set("Stopped" if stopped else "Armed" if armed else "Disarmed")
        self.status_label.configure(fg=RED if stopped else BLUE if armed else MUTED)
        self.status_detail.set("Reset, then arm locally to resume." if stopped else
                               "Agent actions allowed in the selected window." if armed else
                               "Agent actions are blocked.")
        self.arm_button.configure(state="normal" if selected and not armed and not stopped
                                  and not self.overlay_failed else "disabled")
        self.reset_button.configure(state="normal" if stopped and not self.overlay_failed else "disabled")
        if self.overlay:
            if armed and selected:
                self.overlay.show_target(selected["rect"])
            else:
                self.overlay.hide()

    def _tick(self) -> None:
        if self.closed:
            return
        try:
            self._sync_state()
            for _ in range(100):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                kind = str(event.get("type", event.get("event", "")))
                self._log(event_description(event), "error" if kind == "error" else "action" if kind == "action" else "")
                if kind == "action" and self.overlay and self._last_state.get("armed"):
                    if "x" in event and "y" in event:
                        self.overlay.show_action(int(event["x"]), int(event["y"]))
        except Exception as exc:
            self.overlay_failed = True
            self.controller.stop()
            if self.overlay:
                self.overlay.hide()
            self.arm_button.configure(state="disabled")
            self._log(f"Control stopped after a display/status failure: {exc}", "error")
        self.root.after(120, self._tick)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.controller.stop()
        self.hotkey.close()
        if self.server:
            self.server.close()
        if self.overlay:
            self.overlay.destroy()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open Computer local desktop control panel")
    parser.add_argument("--demo", action="store_true", help="Simulate inputs; never control desktop applications")
    parser.add_argument("--port", type=int, default=8787, help="Local HTTP server port (0 chooses an available port)")
    parser.add_argument("--no-server", action="store_true", help="Show the panel without an agent server")
    parser.add_argument("--smoke-test", action="store_true", help="Open a demo panel briefly and exit without arming")
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        try:
            ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = OpenComputerApp(root, demo=args.demo or args.smoke_test, port=args.port,
                          server_enabled=not args.no_server)
    if args.smoke_test:
        root.after(1600, app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
