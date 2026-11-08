"""Optional read-only semantics, deterministic formatting, and owned helper lifetime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Callable, Protocol

from .native_client import NativeClient, NativeError, validate_accessibility

SEMANTIC_COLUMNS = ["id", "parent", "role", "name", "value", "x", "y", "width", "height",
                    "enabled", "focusable", "password"]


class SemanticError(RuntimeError):
    """A classified semantic failure; only explicit inadequacy permits fallback."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SemanticProvider(Protocol):
    def inspect(self, window: dict, *, cancel_event: threading.Event) -> dict: ...
    def status(self) -> dict: ...
    def close(self) -> None: ...


def _rect(rect: dict) -> dict:
    return {key: rect[key] for key in ("x", "y", "width", "height")}


def _validated_snapshot(value: object, window: dict) -> dict:
    try:
        result = validate_accessibility(value, {"window_id": window["id"], "expected_pid": window["pid"]})
    except NativeError as exc:
        # A returned target mismatch is an identity fault, never an availability
        # condition that may silently switch the model to image planning.
        if isinstance(value, dict) and isinstance(value.get("window"), dict):
            target = value["window"]
            if target.get("id") != window.get("id") or target.get("pid") != window.get("pid"):
                raise SemanticError("target_changed", "Semantic target identity changed") from exc
        raise SemanticError("semantic_provider_failed", "Invalid semantic provider response") from exc
    if result["window"]["rect"] != _rect(window["rect"]):
        raise SemanticError("target_changed", "Semantic target geometry changed")
    if result["truncated"] or any(node["password"] is None for node in result["nodes"]):
        raise SemanticError("semantic_incomplete", "Accessibility coverage is incomplete; use a fresh image observation")
    if not any(any(isinstance(node.get(key), str) and node[key].strip() for key in ("name", "value"))
               for node in result["nodes"][1:]):
        raise SemanticError("semantic_empty", "No readable child controls; use a fresh image observation")
    return result


def compact_semantic(value: object, window: dict) -> dict:
    """Validate the native tree and return the one canonical compact table."""
    snapshot = _validated_snapshot(value, window)
    rows = []
    for node in snapshot["nodes"]:
        rows.append([node["id"], node["parent"], node["role"], node.get("name"), node.get("value"),
                     *[node["bounds"][key] for key in ("x", "y", "width", "height")],
                     node["enabled"], node["focusable"], node["password"]])
    return {"format": "controls-v1", "columns": list(SEMANTIC_COLUMNS), "rows": rows}


def validate_semantic(value: object, window: dict) -> dict:
    """Validate an HTTP/model-bound table using the same native-tree invariants."""
    if (not isinstance(value, dict) or set(value) != {"format", "columns", "rows"}
            or value["format"] != "controls-v1" or value["columns"] != SEMANTIC_COLUMNS
            or not isinstance(value["rows"], list) or not 1 <= len(value["rows"]) <= 256):
        raise SemanticError("semantic_provider_failed", "Invalid semantic table format")
    nodes = []
    for row in value["rows"]:
        if not isinstance(row, list) or len(row) != len(SEMANTIC_COLUMNS):
            raise SemanticError("semantic_provider_failed", "Invalid semantic table row")
        fields = dict(zip(SEMANTIC_COLUMNS, row))
        node = {key: fields[key] for key in ("id", "parent", "role", "enabled", "focusable", "password")}
        node["bounds"] = {key: fields[key] for key in ("x", "y", "width", "height")}
        for key in ("name", "value"):
            if fields[key] is not None:
                node[key] = fields[key]
        nodes.append(node)
    snapshot = {"snapshot_id": "table-validation", "window": {"id": window["id"], "pid": window["pid"],
                "title": window["title"], "rect": _rect(window["rect"])}, "nodes": nodes,
                "truncated": False, "truncation_reasons": [], "metrics": {"inspect_ms": 0}}
    return compact_semantic(snapshot, window)


def semantic_fingerprint(table: dict) -> str:
    """Hash only model-visible canonical content, excluding timing/snapshot IDs."""
    encoded = json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def semantic_projection(state: dict) -> dict:
    """Project a semantic observation into text with no image or local authority."""
    return {"observation_id": state["observation_id"], "width": state["window"]["rect"]["width"],
            "height": state["window"]["rect"]["height"],
            "semantic": validate_semantic(state["semantic"], state["window"])}


class NativeSemanticProvider:
    """Lazy single owned child; terminal faults require restarting the local panel."""

    def __init__(self, path: str | Path, *, client_factory: Callable[..., NativeClient] | None = None) -> None:
        self._path = Path(path).resolve(strict=True)
        if not self._path.is_file():
            raise ValueError("Native helper must be an explicit executable file")
        self._factory = client_factory or NativeClient
        self._lock, self._call_lock = threading.Lock(), threading.Lock()
        self._client: NativeClient | None = None
        self._active_cancel: threading.Event | None = None
        self._state = "uninitialized"
        self._fault_code = "semantic_provider_failed"

    def status(self) -> dict:
        # This lock never spans a native RPC or process cleanup.
        with self._lock:
            return {"configured": True, "state": self._state}

    def _native_failure(self, error: NativeError, cancel_event: threading.Event) -> SemanticError:
        code = error.code
        if code == "cancelled" or cancel_event.is_set():
            return SemanticError("cancelled", "Semantic observation cancelled locally")
        if code in {"accessibility_unavailable", "not_supported"}:
            return SemanticError("semantic_unavailable", "Accessibility is unavailable for the selected window")
        if code in {"not_found", "pid_mismatch", "window_changed"}:
            return SemanticError("target_changed", "Semantic target identity or geometry changed")
        if code == "capture_failed":
            return SemanticError("capture_failed", "Native observation capture failed")
        return SemanticError("semantic_provider_failed", "Native semantic helper failed; restart the local panel")

    def inspect(self, window: dict, *, cancel_event: threading.Event) -> dict:
        with self._call_lock:
            with self._lock:
                if self._state in {"unavailable", "closed"}:
                    raise SemanticError(self._fault_code, "Native semantic helper is unavailable; restart the local panel")
                if cancel_event.is_set():
                    raise SemanticError("cancelled", "Semantic observation cancelled locally")
                self._active_cancel = cancel_event
                client = self._client
            try:
                if client is None:
                    client = self._factory(self._path, cancel_event=cancel_event)
                    with self._lock:
                        closed = self._state == "closed"
                        if not closed:
                            self._client = client
                            self._state = "ready"
                    if closed:
                        client.close()
                        raise SemanticError("cancelled", "Semantic provider closed")
                    if "inspect_accessibility" not in client.hello["methods"]:
                        with self._lock:
                            self._state, self._fault_code = "unavailable", "semantic_unavailable"
                        client.close()
                        raise SemanticError("semantic_unavailable", "Native helper has no accessibility capability")
                return client.call("inspect_accessibility", window_id=window["id"], expected_pid=window["pid"],
                                   cancel_event=cancel_event)
            except NativeError as exc:
                terminal = client is None or cancel_event.is_set() or exc.code not in {"not_found", "pid_mismatch", "window_changed", "not_supported",
                                           "capture_failed", "accessibility_unavailable", "resource_limit"}
                if terminal:
                    with self._lock:
                        if self._state != "closed":
                            self._state = "unavailable"
                    if client is not None:
                        client.close()
                if client is None and not cancel_event.is_set() and exc.code != "cancelled":
                    raise SemanticError("semantic_provider_failed", "Native semantic helper handshake failed; restart the local panel") from exc
                raise self._native_failure(exc, cancel_event) from exc
            except SemanticError:
                raise
            except Exception as exc:
                with self._lock:
                    if self._state != "closed":
                        self._state = "unavailable"
                if client is not None:
                    client.close()
                raise SemanticError("semantic_provider_failed", "Native semantic helper failed; restart the local panel") from exc
            finally:
                with self._lock:
                    self._active_cancel = None

    def close(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
            if self._active_cancel is not None:
                self._active_cancel.set()
            client = self._client
        if client is not None:
            client.close()
