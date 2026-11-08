"""Fresh-native packaging with inert PE bytes; no compiler, helper, UI or network."""
import copy
import importlib.util
import json
import marshal
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("native_artifact_support", HERE / "build_support.py")
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class NativeArtifactTests(unittest.TestCase):
    def fixture(self, base):
        source = base / "source"
        support.stage(ROOT, source)
        binary = base / "native-target" / support.NATIVE_TARGET / "release/opencomputer-native.exe"
        binary.parent.mkdir(parents=True)
        data = bytearray(256)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3c, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", data, 0x84, 0x8664)
        struct.pack_into("<H", data, 0x80+24, 0x20b)
        struct.pack_into("<H", data, 0x80+24+68, 3)
        binary.write_bytes(data)
        sysroot = base / "toolchain"
        for name in ["COPYRIGHT-library.html"] + ["licenses/" + name for name in support.RUST_LICENSE_FILES]:
            path = sysroot / "share/doc/rust" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"inert upstream notice fixture")
        context = {"source_clean": True, "source_commit": "c" * 40, "native_tree": "d" * 40,
            "native_source_files": support.native_source_inventory(source), "private_scan_roots": ["C:/Users/BuildOwner"],
            "native_rustc_version": support.RUSTC_VERSION,
            "native_rustc_verbose": support.RUSTC_VERSION + "\nhost: " + support.NATIVE_TARGET + "\nrelease: 1.99.0",
            "native_cargo_version": "cargo 1.99.0 (fixture metadata only)"}
        context_path = base / "context.json"
        context_path.write_text(json.dumps(context), encoding="utf-8")
        payloads = {"LICENSE": (ROOT / "LICENSE").read_bytes(),
                    "third-party-licenses/manifest.json": json.dumps([{"fixture": i} for i in range(34)]).encode()}
        old_rows = [{"path": name, "sha256": support.hashlib.sha256(value).hexdigest(), "size": len(value)}
                    for name, value in payloads.items()]
        old_rows += [{"path": "opencomputer-native.exe"}, {"path": "BUILD-INFO.json"}]
        (source / "packaging/windows/native-input.json").write_text(json.dumps({"files": old_rows}))
        return source, binary, context_path, sysroot, payloads

    def prepare(self, base):
        source, binary, context_path, sysroot, payloads = self.fixture(base)
        archive = base / "fresh-native.zip"
        with patch.object(support, "_legacy_licenses", return_value=payloads):
            support.prepare_native(source, binary, context_path, base / "unused-license.zip", sysroot, archive)
        return source, binary, context_path, archive

    def test_prepare_and_extract_fresh_artifact_bind_current_source_and_literal_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            source, binary, context_path, archive = self.prepare(Path(directory))
            context = support.load(context_path)
            self.assertEqual((source / "opencomputer/_native_pin.py").read_text().strip(),
                             "NATIVE_SHA256 = '" + support.digest(binary) + "'")
            bundle = Path(directory) / "bundle"
            expected = set()
            native = support.native_payload(archive, source, bundle, expected, context)
            self.assertEqual((bundle / native["path"]).read_bytes(), binary.read_bytes())
            self.assertEqual(native["native_tree"], context["native_tree"])
            self.assertEqual(native["source_commit"], context["source_commit"])
            self.assertEqual(len(native["rust_library_notices"]), 13)
            self.assertNotIn("THIRD_PARTY_NOTICES/native-v0.1.3/BUILD-INFO.json", expected)
            self.assertTrue((bundle / "THIRD_PARTY_NOTICES/native-current/BUILD-INFO.json").is_file())

    def test_wrong_source_toolchain_and_reused_helper_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, binary, context_path, archive = self.prepare(Path(directory))
            context = support.load(context_path)
            with zipfile.ZipFile(archive) as stream:
                build = json.loads(stream.read("BUILD-INFO.json"))
            for field, invalid in (("native_tree", "e"*40), ("source_commit", "e"*40),
                    ("cargo_lock_lf_sha256", "e"*64), ("cargo_toml_lf_sha256", "e"*64),
                    ("rust_toolchain", "stable"), ("rustc_version", "rustc 1.98.0"),
                    ("native_sha256", support.NATIVE_SHA)):
                value = copy.deepcopy(build)
                value[field] = invalid
                with self.subTest(field=field), self.assertRaises(ValueError):
                    support.validate_native_build(value, context)
            changed = copy.deepcopy(context)
            changed["native_tree"] = "e"*40
            with self.assertRaises(ValueError):
                support.native_payload(archive, source, Path(directory)/"bundle", set(), changed)

    def test_old_verified_archive_supplies_only_license_bytes_never_its_helper_or_buildinfo(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, binary, context_path, sysroot, licenses = self.fixture(base)
            old_helper = b"inert old helper, never executed"
            old_sha = support.hashlib.sha256(old_helper).hexdigest()
            old_build = json.dumps({"source_commit": support.NATIVE_SOURCE, "native_sha256": old_sha,
                                    "native_input_capability": False}).encode()
            payloads = {**licenses, "opencomputer-native.exe": old_helper, "BUILD-INFO.json": old_build}
            path = base / "old-license-input.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, value in payloads.items():
                    archive.writestr(name, value)
            rows = [{"path": name, "sha256": support.hashlib.sha256(value).hexdigest(), "size": len(value)}
                    for name, value in payloads.items()]
            (source / "packaging/windows/native-input.json").write_text(json.dumps({"files": rows}))
            with patch.object(support, "NATIVE_ZIP_SHA", support.digest(path)), patch.object(support, "NATIVE_SHA", old_sha):
                result = support._legacy_licenses(path, source)
            self.assertEqual(result, licenses)
            self.assertNotIn("opencomputer-native.exe", result)
            self.assertNotIn("BUILD-INFO.json", result)

    def test_fresh_archive_tamper_fails_before_any_payload_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            source, binary, context_path, archive = self.prepare(Path(directory))
            with zipfile.ZipFile(archive, "a") as stream:
                stream.writestr("unlisted-private-file.txt", b"inert tamper fixture")
            bundle = Path(directory) / "bundle"
            with self.assertRaisesRegex(ValueError, "generated artifact"):
                support.native_payload(archive, source, bundle, set(), support.load(context_path))
            self.assertFalse(bundle.exists())

    def test_generated_pin_and_staged_source_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            source, binary, context_path, sysroot, payloads = self.fixture(Path(directory))
            pin = source / "opencomputer/_native_pin.py"
            pin.write_text("NATIVE_SHA256='unexpected'\n")
            with self.assertRaisesRegex(ValueError, "must be new"):
                support.prepare_native(source, binary, context_path, Path("unused"), sysroot, Path(directory)/"fresh.zip")
            pin.unlink()
            (source / "native/src/main.rs").write_text("// altered staged source\n")
            with self.assertRaisesRegex(ValueError, "source changed"):
                support.prepare_native(source, binary, context_path, Path("unused"), sysroot, Path(directory)/"fresh.zip")

    def test_cargo_license_reuse_checks_both_canonical_inputs_before_archive_io(self):
        with tempfile.TemporaryDirectory() as directory:
            source, binary, context_path, sysroot, payloads = self.fixture(Path(directory))
            for name in ("Cargo.toml", "Cargo.lock"):
                path = source / "native" / name
                original = path.read_bytes()
                path.write_bytes(original.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
                self.assertEqual(support.canonical_digest(path), support.hashlib.sha256(original.replace(b"\r\n", b"\n")).hexdigest())
                path.write_bytes(original + b"\n# dependency drift\n")
                with self.assertRaisesRegex(ValueError, "Cargo inputs changed"):
                    support._legacy_licenses(Path(directory)/"missing.zip", source)
                path.write_bytes(original)

    def test_source_checkout_cannot_supply_a_generated_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            (root / "opencomputer").mkdir(parents=True)
            (root / "opencomputer/_native_pin.py").write_text("NATIVE_SHA256='injected'\n")
            with patch.object(support, "validate_inputs", return_value={}):
                with self.assertRaisesRegex(ValueError, "must not contain"):
                    support.stage(root, Path(directory) / "stage")
            self.assertFalse((Path(directory) / "stage").exists())

    def test_both_embedded_executables_must_contain_the_actual_helper_pin(self):
        expected = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "_internal").mkdir()
            with zipfile.ZipFile(bundle / "_internal/base_library.zip", "w") as archive:
                for index in range(101):
                    code = compile("pass", f"stdlib/module{index}.py", "exec")
                    archive.writestr(f"module{index}.pyc", bytes(16) + marshal.dumps(code))
            variants = {"OpenComputer.exe": expected, "OpenComputerCLI.exe": expected}
            class Reader:
                def __init__(self, path):
                    self.name = Path(path).name
                    self.toc = {"PYZ.pyz": ("z",)}
                def open_embedded_archive(self, name):
                    sha = variants[self.name]
                    code = compile(f"NATIVE_SHA256 = {sha!r}\n", "opencomputer/_native_pin.py", "exec")
                    return types.SimpleNamespace(toc={"opencomputer._native_pin": None} if sha else {}, extract=lambda module: code)
            readers = types.ModuleType("PyInstaller.archive.readers")
            readers.CArchiveReader = Reader
            with patch.dict(sys.modules, {"PyInstaller": types.ModuleType("PyInstaller"),
                    "PyInstaller.archive": types.ModuleType("PyInstaller.archive"), "PyInstaller.archive.readers": readers}):
                self.assertGreater(support.inspect_archives(bundle, [], expected), 100)
                for exe in variants:
                    for bad in (None, "b"*64):
                        variants[exe] = bad
                        with self.subTest(exe=exe, sha=bad), self.assertRaises(ValueError):
                            support.inspect_archives(bundle, [], expected)
                    variants[exe] = expected


if __name__ == "__main__":
    unittest.main()
