"""Opt-in live-model proof confined to one disposable, locally authorized window.

Only the user/root workflow should launch --run. This script never starts, loads,
downloads, unloads, or restarts a model. Unknown or busy endpoint state aborts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from urllib.parse import urlsplit
import uuid

from opencomputer import model_client
from opencomputer.app import SafetyHotkey
from opencomputer.backend import WindowsBackend
from opencomputer.controller import Controller
from opencomputer.mcp import LocalClient
from opencomputer.overlay import TargetOverlay
from opencomputer.server import start_server

TEST_TEXT = "Open Computer model test"
TASK = (
    "This is an authorized disposable Windows test window owned by this test process. "
    "In this selected window only, type exactly 'Open Computer model test' into the "
    "empty text field, then click the button labelled 'Test click' exactly once. "
    "Use screenshots and fresh observation IDs. The counter must finish at 1 and "
    "the field must match that text exactly. Do not click the window close button, "
    "change windows, open a shell, install anything, or act outside this fixture. "
    "When the displayed text and counter match, give a concise final answer."
)


class FixtureClient(LocalClient):
    """Expose only actions needed by this fixture; reject other calls locally."""

    allowed_actions = {"click", "type_text", "stop"}

    def __init__(self, url: str, token: str) -> None:
        super().__init__(url, token)
        self.fixture_window: dict | None = None
        self.output: Path | None = None
        self.observations: list[dict] = []

    def bind_fixture(self, window: dict, output: Path) -> None:
        if window.get("pid") != os.getpid():
            raise RuntimeError("Cannot bind diagnostics to another process's window")
        self.fixture_window = window
        self.output = output

    def tools(self) -> list[dict]:
        return [tool for tool in super().tools() if tool["name"] in self.allowed_actions]

    def call(self, name: str, arguments: dict | None = None) -> object:
        if name not in self.allowed_actions | {"status", "get_window_state"}:
            raise RuntimeError("Action is outside the disposable fixture tool set")
        result = super().call(name, arguments)
        if name == "get_window_state":
            window = result.get("window", {})
            if (self.fixture_window is None or self.output is None or
                    window.get("id") != self.fixture_window["id"] or
                    window.get("pid") != os.getpid()):
                raise RuntimeError("Refusing to save an observation outside this owned fixture")
            png = base64.b64decode(result["image_base64"], validate=True)
            record = {key: value for key, value in result.items() if key != "image_base64"}
            record.update(received_at=time.time(), sha256=hashlib.sha256(png).hexdigest())
            directory = self.output / "observations"
            directory.mkdir(exist_ok=True)
            stem = f"observation-{len(self.observations) + 1:03d}"
            record["image_file"] = f"observations/{stem}.png"
            (directory / f"{stem}.png").write_bytes(png)
            (directory / f"{stem}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            self.observations.append(record)
        return result


def input_evidence(events: list[dict], button_bounds: tuple[int, int, int, int],
                   typed_text: str, button_clicks: int) -> dict:
    """Separate observed input attempts, actual widget state, and full completion."""
    actions = [event for event in events if event.get("type") == "action"]
    left, top, right, bottom = button_bounds
    button_actions = sum(event.get("action") == "click" and
        left <= event.get("x", -1) < right and top <= event.get("y", -1) < bottom
        for event in actions)
    typed_action = any(event.get("action") == "type_text" for event in actions)
    return {"input_action_attempts": len(actions),
            # Events are attempts; the independently read widget values prove effects.
            "model_input_actions_observed": bool(actions),
            "model_action_types": [event.get("action") for event in actions],
            "model_text_action_observed": typed_action,
            "model_button_click_actions_observed": button_actions,
            "model_task_actions_observed": typed_action and button_actions == 1,
            "typed_text_matches_expected": typed_text == TEST_TEXT,
            "fixture_assertions_passed": typed_text == TEST_TEXT and button_clicks == 1}


def endpoint_port(endpoint: str) -> int:
    """Use precisely the same loopback-only endpoint boundary as the live runner."""
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Endpoint must be an existing http://127.0.0.1:port service")
    return parsed.port


def endpoint_readiness(endpoint: str) -> dict:
    """Strata/llama-style readiness contract; no prompts or credential fields escape."""
    port = endpoint_port(endpoint)

    def read(path: str) -> object:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 1024 * 1024:
                raise RuntimeError(f"Readiness endpoint {path} rejected ({response.status})")
            return json.loads(raw)
        finally:
            connection.close()

    health = read("/health")
    if not isinstance(health, dict) or health.get("loaded") is not True:
        raise RuntimeError("Model health must explicitly report loaded=true")
    if health.get("status") not in ("ok", "ready", "healthy"):
        raise RuntimeError("Model health did not report a recognized ready status")
    slots = read("/slots")
    if not isinstance(slots, list) or not slots:
        raise RuntimeError("Model slots must be a nonempty list with explicit idle state")
    if any(not isinstance(slot, dict) or slot.get("is_processing") is not False
           for slot in slots):
        raise RuntimeError("Model is busy or slot state is unknown; no request was sent")
    return {"loaded": True, "status": health["status"], "slots": len(slots),
            "all_slots_idle": True, "checked_at": time.time()}


def run(output: Path, endpoint: str, model: str, max_steps: int,
        wait_for_focus: int) -> dict:
    endpoint_port(endpoint)
    output = output / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    receipt = {"passed": False, "fixture_assertions_passed": False,
               "live_model_requests": 0, "fixture_pid": os.getpid(),
               "endpoint": endpoint, "model": model, "max_steps": max_steps,
               "expected_text": TEST_TEXT, "expected_button_clicks": 1,
               "locally_authorized": False, "backend": "windows",
               "input_action_attempts": 0,
               "model_input_actions_observed": False,
               "model_task_actions_observed": False,
               "artifact_directory": str(output)}
    output.mkdir(parents=True, exist_ok=True)
    # Readiness is checked before a fixture is armed and before every completion.
    # Never record the raw health/slot JSON: it can contain keys or unrelated tasks.
    try:
        receipt["initial_readiness"] = endpoint_readiness(endpoint)
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        (output / "model-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt

    backend = WindowsBackend()
    events: queue.Queue = queue.Queue()
    completed: queue.Queue = queue.Queue()
    controller = Controller(backend=backend, event_sink=events.put)
    cancellation = threading.Event()
    root = tk.Tk()
    root.title("Open Computer — disposable live-model test")
    root.geometry("720x440+80+80")
    root.configure(bg="#102030")
    root.attributes("-topmost", True)
    tk.Label(root, text="Live model test fixture", bg="#102030", fg="white",
             font=("Segoe UI", 22, "bold")).pack(pady=(24, 10))
    instruction = tk.StringVar(value="Click the empty field locally to authorize this test.")
    tk.Label(root, textvariable=instruction, bg="#102030", fg="#b7d4ff",
             font=("Segoe UI", 11), wraplength=650).pack()
    tk.Label(root, text="Target text: Open Computer model test", bg="#102030",
             fg="#b7d4ff", font=("Segoe UI", 11)).pack(pady=(12, 0))
    # Keep this test fixture's caret visibly steady: periodic blink pixels would
    # conservatively invalidate exact-PNG checks even when no app state changed.
    # This is native widget configuration, not screenshot masking or editing.
    entry = tk.Entry(root, font=("Segoe UI", 17), width=38, insertofftime=0)
    entry.pack(pady=22)
    clicks = tk.IntVar(value=0)
    button = tk.Button(root, text="Test click", font=("Segoe UI", 15),
                       command=lambda: clicks.set(clicks.get() + 1))
    button.pack(pady=5)
    tk.Label(root, textvariable=clicks, bg="#102030", fg="white",
             font=("Segoe UI", 16)).pack()
    tk.Label(root, text="Ctrl + Alt + Esc stops control. Close this window to cancel.",
             bg="#102030", fg="#8ea9cc", font=("Segoe UI", 9)).pack(pady=12)
    temporary = tempfile.TemporaryDirectory(prefix="opencomputer-model-fixture-")
    server = None
    overlay = None
    hotkey = None
    transcript: list[dict] = []
    action_events: list[dict] = []
    transcript_lock = threading.Lock()
    closed = False
    started = False
    local_click = False
    fixture_id: str | None = None
    fixture_client: FixtureClient | None = None
    deadline = time.monotonic() + wait_for_focus

    def save_transcript() -> None:
        with transcript_lock:
            (output / "model-transcript.json").write_text(
                json.dumps({"task": TASK, "turns": transcript}, indent=2), encoding="utf-8")

    def cleanup(reason: str | None = None) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if reason:
            receipt["error"] = reason
            receipt["passed"] = False
        cancellation.set()
        controller.stop()
        while True:
            try:
                action_events.append(events.get_nowait())
            except queue.Empty:
                break
        receipt["final_controller"] = controller.status()
        receipt["typed_text"] = entry.get()
        receipt["button_clicks"] = clicks.get()
        receipt["action_events"] = action_events
        bx, by = button.winfo_rootx(), button.winfo_rooty()
        receipt.update(input_evidence(action_events,
            (bx, by, bx + button.winfo_width(), by + button.winfo_height()), entry.get(), clicks.get()))
        receipt["passed"] &= receipt["fixture_assertions_passed"] and receipt["model_task_actions_observed"]
        if fixture_client:
            receipt["observations"] = list(fixture_client.observations)
        if hotkey:
            hotkey.close()
        if server:
            server.close()
        if overlay:
            overlay.destroy()
        root.destroy()

    def worker(client: FixtureClient) -> None:
        original_complete = model_client._complete

        def record_complete(call_endpoint: str, call_model: str,
                            messages: list[dict], tools: list[dict], *,
                            cancel_event: threading.Event | None = None) -> dict:
            ready = endpoint_readiness(call_endpoint)
            status = controller.status()
            if closed or not status["armed"] or status["selected_window"]["id"] != fixture_id:
                raise RuntimeError("Fixture authorization ended before model request")
            current = backend.get_window(fixture_id)
            if current["pid"] != os.getpid():
                raise RuntimeError("Fixture process identity changed")
            # Delegate to the real runner transport; this wrapper only records
            # actual responses. No fake response or predicted action is supplied.
            turn = {"step": len(transcript) + 1, "readiness": ready,
                    "request_started_at": time.time()}
            image_parts = [part for message in messages if isinstance(message.get("content"), list)
                           for part in message["content"] if part.get("type") == "image_url"]
            if len(image_parts) != 1:
                raise RuntimeError("Expected exactly one current fixture image in model request")
            image_url = image_parts[0]["image_url"]["url"]
            if not image_url.startswith("data:image/png;base64,"):
                raise RuntimeError("Model image must be this fixture's PNG observation")
            png = base64.b64decode(image_url.split(",", 1)[1], validate=True)
            digest = hashlib.sha256(png).hexdigest()
            observation = next((record for record in reversed(client.observations)
                                if record["sha256"] == digest), None)
            if observation is None:
                raise RuntimeError("Model image does not match any owned fixture capture")
            image_file = f"model-visible-{turn['step']:03d}.png"
            (output / image_file).write_bytes(png)
            turn["model_visible_image"] = {"image_file": image_file, "sha256": digest,
                "observation_id": observation.get("observation_id"),
                "control_generation": observation.get("control_generation"),
                "capture_received_at": observation["received_at"]}
            with transcript_lock:
                transcript.append(turn)
            try:
                response = original_complete(call_endpoint, call_model, messages, tools,
                                             cancel_event=cancel_event)
                with transcript_lock:
                    turn["response"] = response
                    turn["response_received_at"] = time.time()
                return response
            except Exception as exc:
                with transcript_lock:
                    turn["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                save_transcript()

        model_client._complete = record_complete
        try:
            result = model_client.run_task(client, endpoint, model, TASK, max_steps=max_steps,
                                           cancel_event=cancellation)
            completed.put({"result": result})
        except Exception as exc:
            completed.put({"error": f"{type(exc).__name__}: {exc}"})
        finally:
            model_client._complete = original_complete

    def authorize(_event: tk.Event) -> None:
        nonlocal local_click
        if not started and fixture_id:
            foreground = str(backend.user32.GetForegroundWindow())
            if foreground == fixture_id:
                local_click = True

    def start_when_focused() -> None:
        nonlocal started, fixture_id, fixture_client
        if closed or started:
            return
        try:
            root.update_idletasks()
            hwnd = backend.user32.GetAncestor(root.winfo_id(), 2)
            window = backend.get_window(str(hwnd))
            if window["pid"] != os.getpid():
                raise RuntimeError("Refusing to target a window not owned by this fixture process")
            fixture_id = window["id"]
            if not local_click or backend.user32.GetForegroundWindow() != hwnd:
                if time.monotonic() >= deadline:
                    cleanup("Local foreground authorization did not arrive before timeout")
                    return
                root.after(100, start_when_focused)
                return
            if entry.get() or clicks.get() != 0:
                raise RuntimeError("Fixture must start with empty text and zero button clicks")
            receipt["pre_arm_readiness"] = endpoint_readiness(endpoint)
            controller.select_window(fixture_id)
            entry.focus_set()
            controller.arm()
            overlay.show_target(window["rect"])
            receipt.update(locally_authorized=True, window=window,
                           foreground_before_model=backend.user32.GetForegroundWindow())
            instruction.set("Model is controlling only this fixture. Please keep it focused.")
            started = True
            client = FixtureClient.from_file(Path(temporary.name) / "connection.json")
            client.bind_fixture(window, output)
            fixture_client = client
            # Allow the human's activation click to release before input checks.
            root.after(300, lambda: threading.Thread(target=worker, args=(client,),
                       name="live-model-fixture", daemon=True).start())
        except Exception as exc:
            cleanup(f"{type(exc).__name__}: {exc}")

    def tick() -> None:
        if closed:
            return
        try:
            status = controller.status()
            if status["armed"]:
                if status["selected_window"]["id"] != fixture_id:
                    raise RuntimeError("Selected target changed unexpectedly")
                overlay.show_target(status["selected_window"]["rect"])
            else:
                overlay.hide()
            for _ in range(100):
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    break
                action_events.append(event)
                if event["type"] == "action" and "x" in event and status["armed"]:
                    overlay.show_action(event["x"], event["y"])
            try:
                outcome = completed.get_nowait()
            except queue.Empty:
                root.after(40, tick)
                return
            receipt.update(outcome)
            receipt["live_model_requests"] = len(transcript)
            bx, by = button.winfo_rootx(), button.winfo_rooty()
            receipt.update(input_evidence(action_events,
                (bx, by, bx + button.winfo_width(), by + button.winfo_height()), entry.get(), clicks.get()))
            limited = outcome.get("result", "").startswith("Step limit reached")
            receipt["passed"] = (receipt["fixture_assertions_passed"] and not outcome.get("error")
                                 and not limited and outcome.get("result") != "Stopped"
                                 and len(transcript) > 0 and receipt["model_task_actions_observed"])
            receipt["overlay_styles"] = []
            for surface in (overlay.border, overlay.pointer):
                native, hwnd = surface.native
                style = native.GetWindowLongPtrW(hwnd, -20)
                receipt["overlay_styles"].append({"transparent": bool(style & 0x20),
                    "noactivate": bool(style & 0x08000000), "layered": bool(style & 0x80000),
                    "toolwindow": bool(style & 0x80)})
            receipt["passed"] &= all(all(style.values()) for style in receipt["overlay_styles"])
            # Capture only this process-owned client area; no surrounding desktop.
            capture = backend.capture(fixture_id)
            (output / "model-native-capture.png").write_bytes(base64.b64decode(capture["image_base64"]))
            cleanup()
        except Exception as exc:
            cleanup(f"{type(exc).__name__}: {exc}")

    try:
        overlay = TargetOverlay(root)
        def emergency_stop() -> None:
            cancellation.set()
            controller.stop()

        hotkey = SafetyHotkey(emergency_stop)
        server = start_server(controller, port=0,
                              connection_path=Path(temporary.name) / "connection.json")
        root.bind("<ButtonRelease-1>", authorize, add="+")
        root.protocol("WM_DELETE_WINDOW", lambda: cleanup("Cancelled by local user"))
        print("Click the empty field in the disposable model test to authorize it; no other window is eligible.", flush=True)
        root.after(250, start_when_focused)
        root.after(50, tick)
        root.after((wait_for_focus + max_steps * 130 + 20) * 1000,
                   lambda: cleanup("Live-model fixture overall timeout"))
        root.mainloop()
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["passed"] = False
    finally:
        if not closed:
            cleanup(receipt.get("error", "Fixture ended unexpectedly"))
        temporary.cleanup()
        receipt["live_model_requests"] = len(transcript)
        save_transcript()
        (output / "model-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize only the disposable fixture, after a local click")
    parser.add_argument("--wait-for-focus", type=int, default=60, metavar="SECONDS")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8153/v1")
    parser.add_argument("--model", default="flash-next-q2-huihui-dual-amd")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("artifacts/model-smoke"))
    options = parser.parse_args()
    if not options.run:
        parser.error("Pass --run to authorize this disposable live-model fixture")
    if sys.platform != "win32":
        parser.error("This fixture requires Windows")
    if not 1 <= options.wait_for_focus <= 60:
        parser.error("--wait-for-focus must be from 1 to 60 seconds")
    if not 1 <= options.max_steps <= 8:
        parser.error("--max-steps must be from 1 to 8")
    try:
        endpoint_port(options.endpoint)
    except ValueError as exc:
        parser.error(str(exc))
    receipt = run(options.output, options.endpoint, options.model, options.max_steps,
                  options.wait_for_focus)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
