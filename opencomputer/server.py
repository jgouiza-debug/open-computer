"""Authenticated loopback transport for the locally armed controller."""

from __future__ import annotations

import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 128 * 1024
DEFAULT_CONNECTION_PATH = Path.home() / ".opencomputer" / "connection.json"


class ControllerProtocol(Protocol):
    """Narrow controller interface; arming is deliberately absent."""

    def status(self) -> dict: ...
    def list_windows(self) -> list: ...
    def select_window(self, window_id: str) -> dict: ...
    def get_window_state(self) -> dict: ...
    def get_semantic_state(self) -> dict: ...
    def execute(self, action: str, observation_id: str, **params: object) -> dict: ...
    def stop(self) -> dict: ...


def _schema(properties: dict | None = None, required: list | None = None) -> dict:
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}


def tool_definitions() -> list[dict]:
    """Return portable MCP/JSON-schema tools with screenshot-relative coordinates."""
    text = {"type": "string"}
    integer = {"type": "integer"}
    observation = {"observation_id": text}
    xy = {"x": integer, "y": integer}
    specs = [
        ("status", "Read selected target and local arm state.", {}, []),
        ("list_windows", "List visible windows that can be selected.", {}, []),
        ("select_window", "Select a window; selection disarms input.", {"window_id": text}, ["window_id"]),
        ("get_window_state", "Capture selected window and obtain one fresh, single-use observation ID.", {}, []),
        ("get_semantic_state", "Read bounded accessibility controls as text; obtain one single-use observation ID. Use get_window_state if unavailable or insufficient.", {}, []),
        ("click", "Click once using screenshot-relative coordinates.", {**xy, "button": {"enum": ["left", "right", "middle"]}, "count": {"type": "integer", "minimum": 1, "maximum": 2}}, ["x", "y"]),
        ("move", "Move cursor within the selected screenshot.", xy, ["x", "y"]),
        ("scroll", "Scroll at screenshot-relative x,y by signed wheel detents.", {**xy, "delta": integer}, ["x", "y", "delta"]),
        ("type_text", "Type literal Unicode text into the selected foreground window.", {"text": {"type": "string", "maxLength": 10000}}, ["text"]),
        ("press_key", "Press a named key or chord, for example Ctrl+A or Enter.", {"key": text}, ["key"]),
        ("drag", "Drag between two screenshot-relative positions.", {**xy, "to_x": integer, "to_y": integer, "duration": {"type": "number", "minimum": 0.1, "maximum": 3}}, ["x", "y", "to_x", "to_y"]),
        ("activate", "Activate the selected window using a fresh observation.", {}, []),
        ("stop", "Immediately stop and disarm all input. Local reset is required.", {}, []),
    ]
    actions = {"click", "move", "scroll", "type_text", "press_key", "drag", "activate"}
    return [{"name": name, "description": description,
             "inputSchema": _schema({**observation, **props} if name in actions else props,
                                    ["observation_id", *required] if name in actions else required)}
            for name, description, props, required in specs]


class ToolError(Exception):
    """Safe, structured transport error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def dispatch(controller: ControllerProtocol, name: str, arguments: dict) -> object:
    """Validate a public tool call before entering the controller."""
    definitions = {tool["name"]: tool for tool in tool_definitions()}
    if name not in definitions:
        raise ToolError("unknown_tool", "Unknown tool")
    schema = definitions[name]["inputSchema"]
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", "Arguments must be an object")
    if set(arguments) - set(schema["properties"]):
        raise ToolError("invalid_arguments", "Unknown argument")
    if set(schema["required"]) - set(arguments):
        raise ToolError("invalid_arguments", "Missing required argument")
    for key, value in arguments.items():
        spec = schema["properties"][key]
        kind = spec.get("type")
        valid = (kind != "string" or isinstance(value, str))
        valid &= kind != "integer" or (isinstance(value, int) and not isinstance(value, bool))
        valid &= kind != "number" or (isinstance(value, (int, float)) and not isinstance(value, bool))
        valid &= "enum" not in spec or value in spec["enum"]
        if not valid:
            raise ToolError("invalid_arguments", f"Invalid {key}")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ToolError("invalid_arguments", f"{key} is too long")
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolError("invalid_arguments", f"{key} below minimum")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolError("invalid_arguments", f"{key} above maximum")
    if name == "select_window":
        return controller.select_window(**arguments)
    if name in {"status", "list_windows", "get_window_state", "get_semantic_state", "stop"}:
        return getattr(controller, name)()
    return controller.execute(name, **arguments)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class ServerHandle:
    """Running loopback service and its per-launch credentials."""

    def __init__(self, server: _Server, token: str, connection_path: Path | None) -> None:
        self._server = server
        self.token = token
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.connection_path = connection_path
        self._closed = False
        self._thread = threading.Thread(target=server.serve_forever, daemon=True, name="opencomputer-http")
        self._thread.start()

    def close(self) -> None:
        """Stop transport and remove only this launch's connection file."""
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        if self.connection_path is not None:
            try:
                saved = json.loads(self.connection_path.read_text(encoding="utf-8"))
                if saved.get("token") == self.token:
                    self.connection_path.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                LOGGER.warning("Could not remove connection file", exc_info=True)

    stop = close


def start_server(controller: ControllerProtocol, host: str = "127.0.0.1", port: int = 8787,
                 connection_path: Path | str | None = DEFAULT_CONNECTION_PATH) -> ServerHandle:
    """Start on IPv4 loopback only. A None path disables credential persistence."""
    if host != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 is allowed")
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.debug(format, *args)

        def _send(self, status: int, body: object) -> None:
            raw = json.dumps(body, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def _authorize(self) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Origin") or self.headers.get_all("Host") != [expected_host]:
                self._send(403, {"error": {"code": "forbidden", "message": "Invalid host or browser origin"}})
                return False
            auth = self.headers.get_all("Authorization") or []
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("utf-8"), f"Bearer {token}".encode("ascii")):
                self._send(401, {"error": {"code": "unauthorized", "message": "Bearer token required"}})
                return False
            return True

        def do_GET(self) -> None:
            if not self._authorize():
                return
            if self.path == "/health":
                self._send(200, {"ok": True, "protocol": "opencomputer/1"})
            elif self.path == "/tools":
                self._send(200, {"tools": tool_definitions()})
            else:
                self._send(404, {"error": {"code": "not_found", "message": "Unknown path"}})

        def do_POST(self) -> None:
            if not self._authorize():
                return
            if self.path != "/tools/call":
                self._send(404, {"error": {"code": "not_found", "message": "Unknown path"}})
                return
            try:
                lengths = self.headers.get_all("Content-Length") or []
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ToolError("invalid_request", "One Content-Length is required")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ToolError("invalid_request", "Request body exceeds limit or is empty")
                if self.headers.get_content_type() != "application/json":
                    raise ToolError("invalid_request", "application/json required")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ToolError("invalid_request", "Truncated request")
                request = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                if not isinstance(request, dict) or set(request) - {"name", "arguments"}:
                    raise ToolError("invalid_request", "Expected name and arguments")
                if not isinstance(request.get("name"), str):
                    raise ToolError("invalid_request", "Tool name must be a string")
                result = dispatch(controller, request["name"], request.get("arguments", {}))
                self._send(200, {"result": result})
            except (ToolError, ValueError, TypeError, UnicodeError) as exc:
                self._send(400, {"error": {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}})
            except Exception as exc:
                # ControllerError is intentionally duck-typed to avoid an import cycle.
                if isinstance(getattr(exc, "code", None), str):
                    self._send(409, {"error": {"code": exc.code, "message": str(exc)}})
                else:
                    LOGGER.exception("Controller tool failed")
                    self._send(500, {"error": {"code": "internal_error", "message": "Controller failed"}})

    server = _Server((host, port), Handler)
    path = Path(connection_path) if connection_path is not None else None
    try:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Windows inherits the user's profile ACL. POSIX applies owner-only modes.
            if os.name != "nt":
                path.parent.chmod(0o700)
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"url": f"http://127.0.0.1:{server.server_port}", "token": token}, stream)
            os.replace(temporary, path)
        return ServerHandle(server, token, path)
    except BaseException:
        server.server_close()
        raise
