"""Arming, observation freshness, and cancellation around desktop backends."""

from __future__ import annotations

import math
import hashlib
import threading
import time
import uuid
from typing import Callable

from .backend import BackendCancelled, BackendError, FakeBackend, WindowsBackend, parse_key
from .semantic import SemanticError, SemanticProvider, compact_semantic, semantic_fingerprint


class ControllerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Controller:
    def __init__(self, backend: WindowsBackend | FakeBackend | None = None,
                 event_sink: Callable[[dict], None] | None = None,
                 observation_max_age: float = 15.0,
                 semantic_provider: SemanticProvider | None = None) -> None:
        self.backend = backend if backend is not None else WindowsBackend()
        self.event_sink = event_sink
        self.observation_max_age = observation_max_age
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._cancel = threading.Event()
        self.backend.set_cancel_event(self._cancel)
        self._selected = None
        self._armed = False
        self._stopped = False
        self._observation = None
        self._generation = 0
        self._instance_id = uuid.uuid4().hex
        self.semantic_provider = semantic_provider
        self._semantic_cancel: threading.Event | None = None
        self._closed = False

    def _cancel_semantic(self) -> None:
        # Authority lock never spans native I/O, so local cancellation does not
        # wait for the operation lock or a potentially hung accessibility call.
        with self._lock:
            if self._semantic_cancel is not None:
                self._semantic_cancel.set()

    def _require_open(self) -> None:
        if self._closed:
            self._fail("closed", "Controller is closed")

    def _emit(self, event, **details):
        if self.event_sink:
            # UI callbacks enqueue events; they must never call into the OS.
            try:
                self.event_sink({"event": event, "type": event, **details})
            except Exception:
                # A display failure cannot leave desktop input armed.
                self._armed = False
                self._stopped = True
                self._generation += 1
                self._cancel.set()
                self._observation = None
                self._cancel_semantic()
                raise ControllerError("overlay_error", "Overlay event delivery failed; control stopped")

    def _fail(self, code, message):
        raise ControllerError(code, message)

    def _window(self):
        if self._selected is None:
            self._fail("no_target", "Select a target window first")
        try:
            current = self.backend.get_window(self._selected["id"])
        except BackendError as exc:
            self._armed = False
            self._observation = None
            self._fail("target_unavailable", str(exc))
        if current.get("pid") != self._selected.get("pid"):
            self._armed = False
            self._observation = None
            self._fail("target_changed", "Target process identity changed; select it again")
        return current

    def list_windows(self) -> list[dict]:
        try:
            return self.backend.list_windows()
        except BackendError as exc:
            self._fail("backend_error", str(exc))

    def status(self) -> dict:
        with self._lock:
            selected = None
            if self._selected:
                try:
                    selected = self._window()
                except ControllerError:
                    selected = self._selected
            return {"selected_window": selected, "armed": self._armed,
                    "stopped": self._stopped,
                    "control_generation": self._generation,
                    "controller_instance_id": self._instance_id,
                    "observation_id": self._observation["id"] if self._observation else None,
                    "backend": "windows" if self.backend.real else "demo",
                    "semantic_provider": self.semantic_provider.status() if self.semantic_provider else
                    {"configured": False, "state": "disabled"}}

    def select_window(self, window_id: str | int) -> dict:
        # Cancelling first prevents an already-running drag from following a
        # selection change. UI reset/arm is required before further input.
        self._cancel.set()
        self._cancel_semantic()
        with self._operation_lock, self._lock:
            self._require_open()
            self._armed = False
            self._observation = None
            self._generation += 1
            try:
                self._selected = self.backend.get_window(str(window_id))
            except BackendError as exc:
                self._selected = None
                self._fail("target_unavailable", str(exc))
            if not self._stopped:
                self._cancel.clear()
            self._emit("target", selected_window=self._selected)
            return self.status()

    def arm(self) -> dict:
        """Local GUI capability. Never expose this method in remote dispatch."""
        with self._operation_lock, self._lock:
            self._require_open()
            if self._stopped:
                self._fail("stopped", "Reset the emergency stop locally before arming")
            window = self._window()
            self._cancel.clear()
            self._generation += 1
            self._armed = True
            self._observation = None
            self._emit("armed", selected_window=window)
            return self.status()

    local_arm = arm

    def disarm(self) -> dict:
        self._cancel.set()
        self._cancel_semantic()
        with self._lock:
            self._armed = False
            self._observation = None
            self._generation += 1
            self._emit("disarmed", selected_window=self._selected)
            return self.status()

    def stop(self) -> dict:
        self._cancel.set()  # Immediate, including while the operation lock is held.
        self._cancel_semantic()
        with self._lock:
            self._stopped = True
            self._cancel.set()
            self._armed = False
            self._observation = None
            self._generation += 1
            self._emit("stopped", selected_window=self._selected)
            return self.status()

    def reset_stop(self) -> dict:
        # Do not clear cancellation until the running action has released keys.
        with self._operation_lock, self._lock:
            self._require_open()
            self._stopped = False
            self._cancel.clear()
            self._generation += 1
            self._armed = False
            self._observation = None
            self._emit("disarmed", selected_window=self._selected)
            return self.status()

    def get_window_state(self) -> dict:
        with self._operation_lock:
            with self._lock:
                self._require_open()
                self._observation = None
                window = self._window()
                generation = self._generation
            try:
                capture = self.backend.capture(window["id"])
            except Exception as exc:
                self._fail("capture_failed", str(exc))
            with self._lock:
                if generation != self._generation or self._window() != window or capture["rect"] != window["rect"]:
                    self._fail("target_changed", "Target changed during capture; observe again")
                observation_id = uuid.uuid4().hex
                self._observation = {"id": observation_id, "window": window,
                                     "created": time.monotonic(), "generation": generation}
                return {**capture, "window": window, "observation_id": observation_id,
                        "armed": self._armed, "control_generation": generation,
                        "controller_instance_id": self._instance_id}

    def _semantic_authority(self, window: dict, generation: int, cancel_event: threading.Event) -> None:
        with self._lock:
            if cancel_event.is_set() or self._closed:
                self._fail("cancelled", "Semantic observation cancelled locally")
            if generation != self._generation:
                self._fail("target_changed", "Local authority changed during semantic observation")
            if self._window() != window:
                self._fail("target_changed", "Target changed during semantic observation")

    def _semantic_capture(self, window: dict) -> dict:
        try:
            capture = self.backend.capture(window["id"])
        except Exception as exc:
            raise ControllerError("capture_failed", "Could not capture semantic observation") from exc
        if capture.get("rect") != window["rect"]:
            self._fail("target_changed", "Capture geometry changed during semantic observation")
        if (not isinstance(capture.get("image_base64"), str) or not capture["image_base64"]
                or not capture["image_base64"].isascii()
                or capture.get("mime_type") != "image/png"):
            self._fail("capture_failed", "Semantic capture returned no PNG")
        return capture

    @staticmethod
    def _image_digest(capture: dict) -> str:
        return hashlib.sha256(capture["image_base64"].encode("ascii")).hexdigest()

    def _semantic_bundle(self, window: dict, generation: int, cancel_event: threading.Event) -> tuple[dict, dict]:
        self._semantic_authority(window, generation, cancel_event)
        if self.semantic_provider is None:
            self._fail("semantic_unavailable", "No native semantic helper is configured")
        first = self._semantic_capture(window)
        self._semantic_authority(window, generation, cancel_event)
        try:
            snapshot = self.semantic_provider.inspect(window, cancel_event=cancel_event)
        except SemanticError as exc:
            self._semantic_authority(window, generation, cancel_event)
            raise ControllerError(exc.code, str(exc)) from exc
        except Exception as exc:
            self._semantic_authority(window, generation, cancel_event)
            raise ControllerError("semantic_provider_failed", "Semantic provider failed; no action was taken") from exc
        self._semantic_authority(window, generation, cancel_event)
        second = self._semantic_capture(window)
        self._semantic_authority(window, generation, cancel_event)
        if first["image_base64"] != second["image_base64"]:
            self._fail("observation_changed", "Screen changed during accessibility inspection; observe again")
        try:
            table = compact_semantic(snapshot, window)
        except SemanticError as exc:
            self._semantic_authority(window, generation, cancel_event)
            raise ControllerError(exc.code, str(exc)) from exc
        return second, table

    def get_semantic_state(self) -> dict:
        """Bind complete control text and an unchanged PNG to one fresh receipt."""
        with self._operation_lock:
            with self._lock:
                self._require_open()
                self._observation = None
                if self.semantic_provider is None:
                    self._fail("semantic_unavailable", "No native semantic helper is configured")
                window, generation = self._window(), self._generation
                cancel_event = threading.Event()
                self._semantic_cancel = cancel_event
            started = time.monotonic()
            try:
                capture, table = self._semantic_bundle(window, generation, cancel_event)
                digest = semantic_fingerprint(table)
                with self._lock:
                    self._semantic_authority(window, generation, cancel_event)
                    if time.monotonic() - started > self.observation_max_age:
                        self._fail("expired_observation", "Semantic observation expired during inspection")
                    observation_id = uuid.uuid4().hex
                    self._observation = {"id": observation_id, "window": window, "created": started,
                                         "generation": generation, "kind": "semantic",
                                         "image_digest": self._image_digest(capture), "semantic_digest": digest}
                    return {**capture, "window": window, "observation_id": observation_id,
                            "armed": self._armed, "control_generation": generation,
                            "controller_instance_id": self._instance_id, "observation_kind": "semantic",
                            "semantic": table, "semantic_fingerprint": digest}
            finally:
                with self._lock:
                    if self._semantic_cancel is cancel_event:
                        self._semantic_cancel = None

    def close(self) -> None:
        """Cancel active reads and reap only the optional owned helper."""
        self._cancel.set()
        self._cancel_semantic()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._armed = False
            self._stopped = True
            self._generation += 1
            self._observation = None
        if self.semantic_provider is not None:
            self.semantic_provider.close()

    def _validate(self, action, params, rect):
        allowed = {
            "activate": set(), "move": {"x", "y"},
            "click": {"x", "y", "button", "count"}, "scroll": {"x", "y", "delta"},
            "drag": {"x", "y", "to_x", "to_y", "duration"},
            "type_text": {"text"}, "press_key": {"key"},
        }
        if action not in allowed or set(params) - allowed[action]:
            self._fail("invalid_action", "Unsupported action or parameters")
        for x_name, y_name in (("x", "y"), ("to_x", "to_y")):
            if action in ("move", "click", "scroll", "drag") and (x_name == "x" or action == "drag"):
                for name, limit in ((x_name, rect["width"]), (y_name, rect["height"])):
                    value = params.get(name)
                    if type(value) is not int or not 0 <= value < limit:
                        self._fail("invalid_coordinates", f"{name} must be an integer inside the screenshot")
        if action == "click":
            if params.get("button", "left") not in ("left", "right", "middle") or type(params.get("count", 1)) is not int or params.get("count", 1) not in (1, 2):
                self._fail("invalid_arguments", "Click requires a supported button and count 1 or 2")
        if action == "scroll" and (type(params.get("delta")) is not int or not -20 <= params["delta"] <= 20):
            self._fail("invalid_arguments", "Scroll delta must be an integer from -20 to 20")
        if action == "drag":
            duration = params.get("duration", 0.4)
            if type(duration) not in (int, float) or not math.isfinite(duration) or not 0.05 <= duration <= 3:
                self._fail("invalid_arguments", "Drag duration must be between 0.05 and 3 seconds")
        if action == "type_text" and (not isinstance(params.get("text"), str) or len(params["text"]) > 10000):
            self._fail("invalid_arguments", "Text must contain at most 10000 characters")
        if action == "type_text":
            try:
                params["text"].encode("utf-16-le")
            except UnicodeEncodeError:
                self._fail("invalid_arguments", "Text must contain valid Unicode characters")
        if action == "press_key":
            try:
                parse_key(params.get("key"))
            except BackendError as exc:
                self._fail("invalid_arguments", str(exc))

    def execute(self, action: str, observation_id: str, **params: object) -> dict:
        with self._operation_lock:
            with self._lock:
                self._require_open()
                if self._stopped:
                    self._fail("stopped", "Control is stopped; reset and arm locally")
                if not self._armed:
                    self._fail("not_armed", "Arm control from the local desktop panel first")
                if self._cancel.is_set():
                    self._fail("cancelled", "Action cancelled by a local control change")
                observation = self._observation
                if observation is None or not isinstance(observation_id, str) or observation["id"] != observation_id:
                    self._fail("stale_observation", "Obtain a fresh observation before each action")
                self._observation = None  # Each valid receipt is single-use, including failures.
                if time.monotonic() - observation["created"] > self.observation_max_age:
                    self._fail("expired_observation", "Observation expired; observe again")
                window = self._window()
                if window != observation["window"] or observation["generation"] != self._generation:
                    self._fail("target_changed", "Target changed since observation; observe again")
                self._validate(action, params, window["rect"])
                event = {"action": action, "window_id": window["id"], "selected_window": window}
                if "x" in params:
                    event.update(x=window["rect"]["x"] + params["x"], y=window["rect"]["y"] + params["y"])
                semantic_cancel = None
                if observation.get("kind") == "semantic":
                    semantic_cancel = threading.Event()
                    self._semantic_cancel = semantic_cancel
            if semantic_cancel is not None:
                try:
                    capture, table = self._semantic_bundle(window, observation["generation"], semantic_cancel)
                    if (self._image_digest(capture) != observation["image_digest"]
                            or semantic_fingerprint(table) != observation["semantic_digest"]):
                        self._fail("semantic_changed", "Screen or control text changed since observation; no action was taken")
                    with self._lock:
                        self._semantic_authority(window, observation["generation"], semantic_cancel)
                        if time.monotonic() - observation["created"] > self.observation_max_age:
                            self._fail("expired_observation", "Observation expired during semantic revalidation")
                finally:
                    with self._lock:
                        if self._semantic_cancel is semantic_cancel:
                            self._semantic_cancel = None
            with self._lock:
                if self._cancel.is_set():
                    self._fail("cancelled", "Action cancelled by a local control change")
                self._emit("action", **event)
            try:
                self.backend.perform(action, window["id"], _expected_rect=window["rect"],
                                     _expected_pid=window.get("pid"), **params)
            except BackendCancelled as exc:
                with self._lock:
                    self._armed = False
                    self._observation = None
                    self._generation += 1
                    self._emit("cancelled", message=str(exc), **event)
                self._fail("cancelled", str(exc))
            except Exception as exc:
                with self._lock:
                    self._armed = False
                    self._stopped = True
                    self._generation += 1
                    self._cancel.set()
                    self._emit("error", message=str(exc), **event)
                self._fail("input_rejected", str(exc))
            return {"ok": True, "action": action, "window_id": window["id"], "observation_id": None}

    select_target = select_window
    state = status
    emergency_stop = stop
