"""Optional native adapter tests; fake owned children never touch desktop APIs."""

import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

from opencomputer.native_client import NativeClient, NativeError, _accessibility, _capture, _hello


WINDOW = {"id": "123", "pid": 456, "title": "Fixture", "rect": {"x": 10, "y": 20, "width": 2, "height": 2}}
TARGET = {"window_id": "123", "expected_pid": 456}
HELLO = {"engine": "opencomputer-native", "version": "0.1.0", "platform": "windows", "read_only": True,
         "supports_input": False, "supports_arm": False,
         "methods": ["hello", "list_windows", "inspect_window", "capture_window", "inspect_accessibility"],
         "limits": {"max_request_bytes": 16384, "max_response_bytes": 33554432, "max_capture_pixels": 8388608,
                    "max_windows": 512, "max_title_chars": 2048, "max_accessibility_nodes": 256,
                    "max_accessibility_depth": 12, "max_accessibility_text_chars": 8192}}
TREE = {"snapshot_id": "own-1", "window": WINDOW,
        "nodes": [{"id": "e0", "parent": None, "role": "window", "name": "Fixture",
                   "bounds": {"x": 0, "y": 0, "width": 2, "height": 2},
                   "enabled": True, "focusable": False, "password": False}],
        "truncated": False, "truncation_reasons": [], "metrics": {"inspect_ms": 1.2}}


class NativeTests(unittest.TestCase):
    def make_child(self, mode: str = "valid") -> NativeClient:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "fake child.py"
        path.write_text("import sys,json,time\n"
                        f"hello={HELLO!r}\nwindow={WINDOW!r}\ntree={TREE!r}\nmode={mode!r}\n"
                        "for line in sys.stdin:\n"
                        " req=json.loads(line); method=req['method']\n"
                        " if method!='hello' and mode=='timeout': time.sleep(10)\n"
                        " result=hello if method=='hello' else ({'windows':[window],'truncated':False} if method=='list_windows' else (tree if method=='inspect_accessibility' else window))\n"
                        " reply={'v':1,'id':req['id'],'ok':True,'result':result}\n"
                        " if method!='hello' and mode=='wrong_id': reply['id']='wrong'\n"
                        " if method!='hello' and mode=='duplicate': print('{\"v\":1,\"v\":1}',flush=True); continue\n"
                        " if method!='hello' and mode=='oversize': print('x'*33554433,flush=True); continue\n"
                        " if mode=='input': reply['result']=dict(hello,supports_input=True)\n"
                        " print(json.dumps(reply),flush=True)\n", encoding="utf-8")
        child = NativeClient(sys.executable, ["-u", str(path)], timeout=0.5)
        self.addCleanup(child.close)
        return child

    def test_owned_child_handshake_and_read_only_calls(self) -> None:
        with self.make_child() as client:
            self.assertTrue(client.hello["read_only"])
            self.assertEqual(client.call("list_windows"), {"windows": [WINDOW], "truncated": False})
            self.assertEqual(client.call("inspect_window", **TARGET), WINDOW)
            self.assertEqual(client.call("inspect_accessibility", **TARGET), TREE)
            for method in ("click", "arm", "hello", "stop"):
                with self.assertRaises(NativeError):
                    client.call(method)
        self.assertIsNotNone(client._process.poll())

    def test_timeout_reaps_only_owned_child_without_retry(self) -> None:
        client = self.make_child("timeout")
        started = time.monotonic()
        with self.assertRaises(NativeError) as caught:
            client.call("inspect_window", **TARGET)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertIsNotNone(client._process.poll())
        with self.assertRaises(NativeError):
            client.call("list_windows")

    def test_deadline_also_bounds_a_blocked_request_writer(self) -> None:
        client = self.make_child()
        entered, release = threading.Event(), threading.Event()

        def blocked_write(_line, _completed):
            entered.set()
            release.wait(5)

        client._write_request = blocked_write
        try:
            started = time.monotonic()
            with self.assertRaises(NativeError) as caught:
                client.call("list_windows")
            self.assertTrue(entered.is_set())
            self.assertEqual(caught.exception.code, "timeout")
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertIsNotNone(client._process.poll())
        finally:
            release.set()

    def test_malformed_duplicate_and_oversized_replies_fail_closed(self) -> None:
        for mode in ("wrong_id", "duplicate", "oversize"):
            with self.subTest(mode=mode):
                client = self.make_child(mode)
                with self.assertRaises(NativeError) as caught:
                    client.call("inspect_window", **TARGET)
                self.assertEqual(caught.exception.code, "protocol_error")
                self.assertIsNotNone(client._process.poll())

    def test_input_capability_and_changed_target_are_rejected(self) -> None:
        with self.assertRaises(NativeError):
            self.make_child("input")
        client = self.make_child()
        with self.assertRaises(NativeError):
            client.call("inspect_window", window_id="123", expected_pid=999)
        self.assertIsNotNone(client._process.poll())

    def test_capture_validates_target_pixel_product_png_and_metrics(self) -> None:
        image = io.BytesIO()
        Image.new("RGB", (2, 2)).save(image, format="PNG")
        capture = {"window": WINDOW, "image_base64": base64.b64encode(image.getvalue()).decode("ascii"),
                   "mime_type": "image/png", "image_width": 2, "image_height": 2,
                   "metrics": {"capture_ms": 1, "encode_ms": 2, "cache_hit": False}}
        self.assertEqual(_capture(capture, TARGET), capture)
        mutations = (("image_width", True), ("image_height", 3), ("image_base64", "!!!!"), ("mime_type", "image/jpeg"))
        for key, value in mutations:
            changed = dict(capture, **{key: value})
            with self.subTest(key=key), self.assertRaises(NativeError):
                _capture(changed, TARGET)
        changed = copy.deepcopy(capture)
        changed["metrics"]["capture_ms"] = float("nan")
        with self.assertRaises(NativeError):
            _capture(changed, TARGET)

    def test_password_unknown_and_descendant_leaks_are_rejected(self) -> None:
        for password in (True, None):
            tree = copy.deepcopy(TREE)
            tree["nodes"][0]["password"] = password
            with self.assertRaises(NativeError):
                _accessibility(tree, TARGET)
            del tree["nodes"][0]["name"]
            self.assertEqual(_accessibility(tree, TARGET), tree)
            child = copy.deepcopy(tree["nodes"][0])
            child.update(id="e1", parent="e0", password=False)
            tree["nodes"].append(child)
            with self.assertRaises(NativeError):
                _accessibility(tree, TARGET)

    def test_tree_rejects_duplicate_identity_parent_bounds_and_false_completeness(self) -> None:
        invalid = []
        tree = copy.deepcopy(TREE)
        tree["nodes"].append(copy.deepcopy(tree["nodes"][0]))
        invalid.append(tree)
        tree = copy.deepcopy(TREE)
        tree["nodes"][0]["parent"] = "e9"
        invalid.append(tree)
        tree = copy.deepcopy(TREE)
        tree["nodes"][0]["bounds"]["width"] = 3
        invalid.append(tree)
        tree = copy.deepcopy(TREE)
        tree["truncation_reasons"] = ["time_limit"]
        invalid.append(tree)
        for tree in invalid:
            with self.assertRaises(NativeError):
                _accessibility(tree, TARGET)

    def test_canonical_schema_validates_owned_contract_examples(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("Optional jsonschema unavailable")
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/native-protocol.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        for method in HELLO["methods"]:
            params = {} if method in ("hello", "list_windows") else TARGET
            validator.validate({"v": 1, "id": "1", "method": method, "params": params})
        for result in (HELLO, WINDOW, TREE, {"windows": [WINDOW], "truncated": False}):
            validator.validate({"v": 1, "id": "1", "ok": True, "result": result})
        denied = copy.deepcopy(TREE)
        denied["nodes"][0]["password"] = None
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate({"v": 1, "id": "1", "ok": True, "result": denied})
        _hello(HELLO)


if __name__ == "__main__":
    unittest.main()
