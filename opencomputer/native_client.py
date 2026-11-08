"""Explicit, read-only client for the optional owned Rust observation process."""

from __future__ import annotations

import base64
from collections import deque
import json
import math
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Sequence

MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PIXELS = 8_388_608
METHODS = frozenset({"hello", "list_windows", "inspect_window", "capture_window", "inspect_accessibility"})
ERRORS = frozenset({"invalid_request", "unknown_method", "invalid_params", "not_found", "pid_mismatch",
                    "window_changed", "not_supported", "capture_failed", "accessibility_unavailable",
                    "resource_limit", "internal_error"})


class NativeError(RuntimeError):
    """A bounded native error; a failure grants no input authority."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _object(value: object, required: set[str], optional: frozenset[str] | set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise NativeError("protocol_error", "Invalid native object fields")
    return value


def _integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise NativeError("protocol_error", "Invalid native integer")
    return value


def _text(value: object, maximum: int, *, minimum: int = 0) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise NativeError("protocol_error", "Invalid native string")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise NativeError("protocol_error", "Invalid native boolean")
    return value


def _duration(value: object) -> None:
    if type(value) not in (int, float) or not 0 <= value <= 9_007_199_254_740_991 or not math.isfinite(value):
        raise NativeError("protocol_error", "Invalid native duration")


def _rect(value: object) -> dict:
    rect = _object(value, {"x", "y", "width", "height"})
    for key in ("x", "y"):
        _integer(rect[key], -2_147_483_648, 2_147_483_647)
    for key in ("width", "height"):
        _integer(rect[key], 1, MAX_PIXELS)
    return rect


def _window(value: object, target: dict | None = None) -> dict:
    window = _object(value, {"id", "pid", "title", "rect"})
    if not re.fullmatch(r"[1-9][0-9]{0,19}", _text(window["id"], 20)) or int(window["id"]) > 2**64 - 1:
        raise NativeError("protocol_error", "Invalid native window handle")
    _integer(window["pid"], 1, 2**32 - 1)
    _text(window["title"], 2048)
    _rect(window["rect"])
    if target and (window["id"] != target["window_id"] or window["pid"] != target["expected_pid"]):
        raise NativeError("protocol_error", "Native response target differs")
    return window


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def _hello(value: object) -> dict:
    result = _object(value, {"engine", "version", "platform", "read_only", "supports_input", "supports_arm", "methods", "limits"})
    if (result["engine"] != "opencomputer-native" or result["platform"] != "windows"
            or result["read_only"] is not True or result["supports_input"] is not False or result["supports_arm"] is not False):
        raise NativeError("protocol_error", "Native child is not the read-only Windows observer")
    _text(result["version"], 32, minimum=1)
    methods = result["methods"]
    required = METHODS - {"inspect_accessibility"}
    if (not isinstance(methods, list) or any(not isinstance(name, str) for name in methods)
            or len(set(methods)) != len(methods) or not required <= set(methods) <= METHODS):
        raise NativeError("protocol_error", "Invalid native capabilities")
    expected = {"max_request_bytes": MAX_REQUEST_BYTES, "max_response_bytes": MAX_RESPONSE_BYTES,
                "max_capture_pixels": MAX_PIXELS, "max_windows": 512, "max_title_chars": 2048,
                "max_accessibility_nodes": 256, "max_accessibility_depth": 12, "max_accessibility_text_chars": 8192}
    limits = _object(result["limits"], set(expected))
    for name, limit in expected.items():
        if _integer(limits[name], limit, limit) != limit:
            raise NativeError("protocol_error", "Native limits differ")
    return result


def _capture(value: object, target: dict) -> dict:
    result = _object(value, {"window", "image_base64", "mime_type", "image_width", "image_height", "metrics"})
    window = _window(result["window"], target)
    width = _integer(result["image_width"], 1, MAX_PIXELS)
    height = _integer(result["image_height"], 1, MAX_PIXELS)
    if width * height > MAX_PIXELS or (width, height) != (window["rect"]["width"], window["rect"]["height"]):
        raise NativeError("protocol_error", "Native capture geometry differs")
    if result["mime_type"] != "image/png":
        raise NativeError("protocol_error", "Native capture is not PNG")
    try:
        data = base64.b64decode(_text(result["image_base64"], MAX_RESPONSE_BYTES, minimum=4), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise NativeError("protocol_error", "Invalid native PNG encoding") from exc
    if (len(data) < 33 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or int.from_bytes(data[16:20], "big") != width or int.from_bytes(data[20:24], "big") != height):
        raise NativeError("protocol_error", "Native PNG header differs")
    metrics = _object(result["metrics"], {"capture_ms", "encode_ms", "cache_hit"})
    _duration(metrics["capture_ms"])
    _duration(metrics["encode_ms"])
    _boolean(metrics["cache_hit"])
    return result


def _accessibility(value: object, target: dict) -> dict:
    result = _object(value, {"snapshot_id", "window", "nodes", "truncated", "truncation_reasons", "metrics"})
    _text(result["snapshot_id"], 128, minimum=1)
    window = _window(result["window"], target)
    nodes = result["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        raise NativeError("protocol_error", "Invalid accessibility node count")
    depths, denied, characters = {}, set(), 0
    for index, node in enumerate(nodes):
        node = _object(node, {"id", "parent", "role", "bounds", "enabled", "focusable", "password"}, {"name", "value"})
        identity = _text(node["id"], 4)
        if not re.fullmatch(r"e[0-9]{1,3}", identity) or identity in depths:
            raise NativeError("protocol_error", "Invalid accessibility identity")
        parent = node["parent"]
        if (index == 0 and parent is not None) or (index > 0 and (not isinstance(parent, str) or parent not in depths or parent in denied)):
            raise NativeError("protocol_error", "Invalid accessibility parent")
        depths[identity] = 0 if parent is None else depths[parent] + 1
        if depths[identity] > 12:
            raise NativeError("protocol_error", "Accessibility depth exceeded")
        _text(node["role"], 64)
        _boolean(node["enabled"])
        _boolean(node["focusable"])
        password = node["password"]
        if password is not None:
            _boolean(password)
        if password is not False:
            denied.add(identity)
            if "name" in node or "value" in node:
                raise NativeError("protocol_error", "Accessibility password text was returned")
        for key, limit in (("name", 160), ("value", 256)):
            if key in node:
                characters += len(_text(node[key], limit))
        rect = _rect(node["bounds"])
        if rect["x"] < 0 or rect["y"] < 0 or rect["x"] + rect["width"] > window["rect"]["width"] or rect["y"] + rect["height"] > window["rect"]["height"]:
            raise NativeError("protocol_error", "Accessibility bounds leave target")
    reasons = result["truncation_reasons"]
    allowed = {"node_limit", "depth_limit", "text_limit", "time_limit"}
    if (characters > 8192 or not isinstance(reasons, list) or any(not isinstance(reason, str) or reason not in allowed for reason in reasons)
            or len(set(reasons)) != len(reasons) or _boolean(result["truncated"]) != bool(reasons)):
        raise NativeError("protocol_error", "Invalid accessibility truncation")
    _duration(_object(result["metrics"], {"inspect_ms"})["inspect_ms"])
    return result


class NativeClient:
    """One serialized owned process; no input/arm methods or automatic retries."""

    def __init__(self, executable: str | Path, args: Sequence[str] = (), *, timeout: float = 5.0) -> None:
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("Native timeout must be in (0, 30]")
        path = Path(executable).resolve(strict=True)
        if not path.is_file() or isinstance(args, (str, bytes)) or any(not isinstance(arg, str) for arg in args):
            raise ValueError("Native executable and arguments must be explicit")
        self._timeout = float(timeout)
        self._lock, self._close_lock = threading.Lock(), threading.Lock()
        self._closed = False
        self._sequence = 0
        self._replies: queue.Queue[bytes | Exception] = queue.Queue(maxsize=1)
        self._fault = threading.Event()
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_lock = threading.Lock()
        self._process = subprocess.Popen([str(path), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True), threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        try:
            self.hello = _hello(self._request("hello", {}))
        except BaseException:
            self.close()
            raise

    def _read_stdout(self) -> None:
        try:
            while not self._closed:
                line = self._process.stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line or len(line) > MAX_RESPONSE_BYTES or not line.endswith(b"\n"):
                    raise NativeError("protocol_error", "Native output ended or exceeded its line bound")
                self._replies.put_nowait(line)
        except queue.Full:
            self._fault.set()
        except (OSError, ValueError, NativeError) as exc:
            try:
                self._replies.put_nowait(exc)
            except queue.Full:
                self._fault.set()

    def _read_stderr(self) -> None:
        try:
            while not self._closed:
                chunk = self._process.stderr.read1(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.append(chunk)
                    self._stderr_size += len(chunk)
                    while self._stderr_size > MAX_STDERR_BYTES:
                        self._stderr_size -= len(self._stderr.popleft())
        except (OSError, ValueError):
            return  # Pipe closure is expected during owned-child shutdown.

    def _request(self, method: str, params: dict, *, timeout: float | None = None) -> object:
        with self._lock:
            if self._closed:
                raise NativeError("closed", "Native observer is closed")
            self._sequence += 1
            identity = str(self._sequence)
            line = json.dumps({"v": 1, "id": identity, "method": method, "params": params}, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
            deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
            try:
                if self._fault.is_set() or not self._replies.empty() or len(line) > MAX_REQUEST_BYTES:
                    raise NativeError("protocol_error", "Unexpected native output or oversized request")
                written: queue.Queue[Exception | None] = queue.Queue(maxsize=1)
                writer = threading.Thread(target=self._write_request, args=(line, written), daemon=True)
                writer.start()
                write_error = written.get(timeout=max(0, deadline - time.monotonic()))
                if write_error is not None:
                    raise NativeError("protocol_error", "Native request could not be written") from write_error
                raw = self._replies.get(timeout=max(0, deadline - time.monotonic()))
                if isinstance(raw, Exception):
                    raise NativeError("protocol_error", "Native output could not be read") from raw
                reply = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
                _object(reply, {"v", "id", "ok"}, {"result", "error"})
                if type(reply["v"]) is not int or reply["v"] != 1 or reply["id"] != identity or type(reply["ok"]) is not bool or self._fault.is_set():
                    raise NativeError("protocol_error", "Native response envelope differs")
                if reply["ok"]:
                    _object(reply, {"v", "id", "ok", "result"})
                    return reply["result"]
                _object(reply, {"v", "id", "ok", "error"})
                error = _object(reply["error"], {"code", "message", "retryable"})
                if not isinstance(error["code"], str) or error["code"] not in ERRORS or error["retryable"] is not False:
                    raise NativeError("protocol_error", "Invalid native error")
                raise NativeError(error["code"], _text(error["message"], 512))
            except queue.Empty as exc:
                self.close()
                raise NativeError("timeout", "Native observation exceeded its deadline") from exc
            except (OSError, ValueError, NativeError, RecursionError) as exc:
                self.close()
                if isinstance(exc, NativeError):
                    raise
                raise NativeError("protocol_error", "Native transport failed") from exc
            except BaseException:
                self.close()
                raise

    def _write_request(self, line: bytes, completed: queue.Queue[Exception | None]) -> None:
        try:
            self._process.stdin.write(line)
            self._process.stdin.flush()
            completed.put_nowait(None)
        except (OSError, ValueError) as exc:
            completed.put_nowait(exc)

    def call(self, method: str, *, window_id: str | None = None, expected_pid: int | None = None) -> dict:
        """Call an advertised read-only method and validate its complete result."""
        if method not in METHODS or method == "hello" or method not in self.hello["methods"]:
            raise NativeError("not_supported", "Native read-only method is unavailable")
        params = {}
        if method != "list_windows":
            params = {"window_id": window_id, "expected_pid": expected_pid}
            _window({"id": window_id, "pid": expected_pid, "title": "", "rect": {"x": 0, "y": 0, "width": 1, "height": 1}})
        elif window_id is not None or expected_pid is not None:
            raise ValueError("list_windows takes no target")
        result = self._request(method, params)
        try:
            if method == "inspect_window":
                return _window(result, params)
            if method == "capture_window":
                return _capture(result, params)
            if method == "inspect_accessibility":
                return _accessibility(result, params)
            result = _object(result, {"windows", "truncated"})
            if not isinstance(result["windows"], list) or len(result["windows"]) > 512:
                raise NativeError("protocol_error", "Invalid native window list")
            _boolean(result["truncated"])
            for window in result["windows"]:
                _window(window)
            return result
        except NativeError:
            self.close()
            raise

    def close(self) -> None:
        """Terminate and reap only this owned child; safe during a pending call."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1)
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                pipe.close()

    def __enter__(self) -> NativeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
