"""Pure packaging boundary tests: no frozen process, GUI, network or native input."""
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("portable_build_support", HERE / "build_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class BuildBoundaryTests(unittest.TestCase):
    def test_committed_lock_and_notice_hashes(self):
        deps = support.validate_inputs(ROOT)
        self.assertEqual(len(deps["packages"]), 9)
        self.assertEqual({p["name"]: p["version"] for p in deps["packages"]}["Pillow"], "12.3.0")

    def test_lock_mutation_fails(self):
        with tempfile.TemporaryDirectory(prefix="portable-inputs-") as tmp:
            root = Path(tmp)
            shutil.copytree(HERE, root / "packaging/windows")
            lock = root / "packaging/windows/requirements-build.lock"
            lock.write_text(lock.read_text(encoding="utf-8").replace("Pillow==12.3.0", "Pillow==12.2.0"), encoding="utf-8")
            with self.assertRaises(ValueError):
                support.validate_inputs(root)

    def test_relative_paths_reject_escape(self):
        for value in ("../secret", "/etc/passwd", "C:/private", "folder/../secret", "folder//file", "folder/./file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                support.relative(value)
        self.assertEqual(support.relative("_internal\\PIL\\_imaging.pyd"), "_internal/PIL/_imaging.pyd")

    def test_copy_rejects_outside_root(self):
        with tempfile.TemporaryDirectory(prefix="portable-copy-") as tmp:
            root = Path(tmp)
            (root / "allowed").mkdir()
            (root / "outside.txt").write_text("not part of source", encoding="utf-8")
            with self.assertRaises(ValueError):
                support.checked_copy(root / "outside.txt", root / "out/file", root / "allowed")

    def test_rejected_origin_diagnostic_is_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory(prefix="portable-origin-") as tmp:
            base = Path(tmp)
            source = (base / "unrelated/private-user/secret/runtime.dll").resolve()
            diagnostic = support.origin_diagnostic(source, "_internal/runtime.dll", "BINARY",
                                                    base / "owned/source", base / "owned/toc.json")
            serialized = json.dumps(diagnostic)
            self.assertNotIn(str(base), serialized)
            self.assertNotIn("private-user", serialized)
            self.assertEqual(diagnostic["source_basename"], "runtime.dll")
            self.assertEqual(diagnostic["source_scope"], "outside-known-roots")
            self.assertIsNone(diagnostic["source_relative"])
            generated = (base / "owned/work/OpenComputer/other.zip").resolve()
            diagnostic = support.origin_diagnostic(generated, "_internal/other.zip", "DATA",
                                                    base / "owned/source", base / "owned/toc.json")
            self.assertEqual(diagnostic["source_scope"], "generated-work")
            self.assertEqual(diagnostic["source_relative"], "OpenComputer/other.zip")

    def test_bytecode_filename_rejects_private_and_absolute_paths(self):
        for filename in ("C:/Users/Builder/private.py", "C:/neutral/source.py", "/tmp/source.py"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                support.check_code(compile("pass", filename, "exec"), ["C:/Users/Builder"])

    def test_decompressed_constants_are_scanned(self):
        for source in ("value='C:/Users/Builder/private'", "def f():\n return 'C:/Users/Builder/private'",
                       "value=b'ghp_' + b'A'*36"):
            # Constant-folded credential example is compiled as one byte string.
            code = compile(source, "safe.py", "exec")
            if source.startswith("value=b"):
                code = compile("value=b'" + "ghp_" + "A" * 36 + "'", "safe.py", "exec")
            with self.subTest(source=source), self.assertRaises(ValueError):
                support.check_code(code, ["C:/Users/Builder"])

    def test_safe_nested_code_is_counted_without_execution(self):
        code = compile("def nested():\n raise RuntimeError('not executed')", "opencomputer/app.py", "exec")
        self.assertEqual(support.check_code(code, ["C:/Users/Builder"]), 2)

    def test_utf16_private_content(self):
        with self.assertRaises(ValueError):
            support.privacy_bytes("C:\\Users\\Builder\\private".encode("utf-16-le"), ["C:/Users/Builder"])

    def test_pe_machine_and_subsystem(self):
        with tempfile.TemporaryDirectory(prefix="portable-pe-") as tmp:
            path = Path(tmp) / "synthetic.exe"
            data = bytearray(256)
            data[:2] = b"MZ"
            struct.pack_into("<I", data, 0x3c, 0x80)
            data[0x80:0x84] = b"PE\0\0"
            struct.pack_into("<H", data, 0x84, 0x8664)
            struct.pack_into("<H", data, 0x80 + 24, 0x20b)
            struct.pack_into("<H", data, 0x80 + 24 + 68, 2)
            path.write_bytes(data)
            self.assertEqual(support.pe_header(path), {"machine": "x64", "subsystem": 2})
            struct.pack_into("<H", data, 0x84, 0x14c)
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                support.pe_header(path)

    def test_native_payload_wrong_archive_fails_before_extract(self):
        with tempfile.TemporaryDirectory(prefix="portable-native-") as tmp:
            root = Path(tmp)
            archive = root / "wrong.zip"
            archive.write_bytes(b"not the published archive")
            with self.assertRaises(ValueError):
                support.native_payload(archive, ROOT, root / "bundle", set())

    def test_staging_excludes_unlisted_repository_files(self):
        with tempfile.TemporaryDirectory(prefix="portable-stage-") as tmp:
            target = Path(tmp) / "new-source"
            support.stage(ROOT, target)
            self.assertTrue((target / "opencomputer/model_client.py").is_file())
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / "tests").exists())
            self.assertFalse((target / "artifacts").exists())
            self.assertFalse((target / "packaging/windows/test_build_support.py").exists())
            with self.assertRaises(ValueError):
                support.stage(ROOT, target)


if __name__ == "__main__":
    unittest.main()
