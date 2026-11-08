"""Allowlisted, fail-closed helpers for the remote Windows portable build.

Importing this module needs only Python's standard library. PyInstaller readers
are imported only while checking a completed candidate in its isolated CI venv.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import marshal
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import sys
import sysconfig
import tempfile
import tomllib
import types
from typing import Any
import zipfile

PYTHON = "3.14.10"
NATIVE_SHA = "3ba321acc65b9f84131c0e615f4817eb5865f4b3ab668c298a5535b29e7a757d"
NATIVE_ZIP_SHA = "7d101ac98ab1a2c1cba0ed8a212454e7d39d5e882a49f42c858b4a43eddcd869"
NATIVE_SOURCE = "05c3721b3962ace619441431fcdb8e4eb4be2e93"
NATIVE_TREE = "bb5910da594cefabb3cb7e00b41c1e0ca14c1199"
NATIVE_LOCK_LF_SHA = "9b2cf2a4b424394929161c19e45cc31bf07fb0242683d13d403f2e95e19461f6"
NATIVE_TOML_LF_SHA = "d538610e2b3da836279c4da40ec95cf15f50bf829778878b14473a278ef96e94"
RUST_TOOLCHAIN = "1.99.0-x86_64-pc-windows-msvc"
RUSTC_VERSION = "rustc 1.99.0 (b940084d7 2026-09-28)"
NATIVE_TARGET = "x86_64-pc-windows-msvc"
NATIVE_FILES = ("Cargo.toml", "Cargo.lock", "src/main.rs", "src/window.rs", "src/protocol.rs",
                "src/capture.rs", "src/accessibility.rs", "tests/wire.rs")
RUST_LICENSE_FILES = ("Apache-2.0.txt", "BSD-2-Clause.txt", "CC-BY-SA-4.0.txt", "GCC-exception-3.1.txt",
                      "GPL-2.0-only.txt", "GPL-3.0-or-later.txt", "ISC.txt", "LLVM-exception.txt",
                      "MIT.txt", "NCSA.txt", "OFL-1.1.txt", "Unicode-3.0.txt")
STAGE_MARKER = ".opencomputer-build-stage.json"
APP_MODULES = (
    "__init__", "app", "backend", "cli", "controller", "mcp", "model_client",
    "native_client", "overlay", "semantic", "server", "setup", "headless", "safety", "headless_overlay",
)
PACKAGING_FILES = (
    "OpenComputer.spec", "gui_entry.py", "cli_entry.py", "build_support.py",
    "requirements-build.in", "requirements-build.lock", "dependencies.json",
    "runtime-licenses.json", "native-input.json", "PORTABLE-README.txt",
    "Start-OpenComputer-Semantic.cmd",
)
TOKEN = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,}|sk-[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})")
WINDOWS_OS_COMPONENT = re.compile(r"(?:api-ms-win-(?:core|crt)-[a-z0-9-]+|ucrtbase)\.dll", re.IGNORECASE)
WINDOWS_OS_POLICY = {
    "minimum_windows": "10",
    "reason": "Windows10+ provides these API-set/UCRT components; app-local UCRT copies are not used.",
    "references": [
        "https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170",
        "https://docs.python.org/3.14/using/windows.html#supported-windows-versions",
        "https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/PyInstaller/depend/dylib.py",
    ],
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def native_source_inventory(root: Path) -> list[dict]:
    return [{"path": "native/" + name, "sha256": canonical_digest(root / "native" / name)}
            for name in NATIVE_FILES]


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def relative(value: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")) or ":" in value:
        raise ValueError("Unsafe package-relative path")
    return path.as_posix()


def omit_windows_os_components(entries: list[tuple[str, str, str]]) -> tuple[list, list[str]]:
    """Do not redistribute OS API sets/UCRT for the fixed Windows10+ target.

    This does not admit any outside-origin DLL. VC runtime/Pillow/other DLLs,
    nested destinations and non-binary records retain their existing guards.
    """
    kept, omitted = [], []
    for entry in entries:
        name = relative(entry[0])
        if entry[2] == "BINARY" and WINDOWS_OS_COMPONENT.fullmatch(name):
            omitted.append(name)
        else:
            kept.append(entry)
    return kept, sorted(set(omitted), key=str.casefold)


def verify_os_components_absent(actual: set[str], omitted_names: list[str]) -> None:
    folded = {name.casefold() for name in actual}
    if any(("_internal/" + name).casefold() in folded for name in omitted_names):
        raise ValueError("An OS component recorded as omitted is still bundled")


def checked_copy(source: Path, target: Path, source_root: Path) -> None:
    if source.is_symlink() or not source.resolve().is_relative_to(source_root.resolve()) or not source.is_file():
        raise ValueError("Copy source is missing or escapes its allowlisted root")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def origin_diagnostic(source: Path, destination: str, kind: str,
                      root: Path, toc_path: Path) -> dict:
    """Explain a rejected origin without printing an unknown absolute path."""
    scope, tail = "outside-known-roots", None
    known = [("build-venv", Path(sys.prefix)), ("python-base", Path(sys.base_prefix)),
             ("staged-source", root), ("generated-work", toc_path.parent / "work"),
             ("other-build-file", toc_path.parent)]
    if os.environ.get("SystemRoot"):
        known.append(("windows-system", Path(os.environ["SystemRoot"])))
    for label, folder in known:
        if source.is_relative_to(folder.resolve()):
            scope = label
            tail = source.relative_to(folder.resolve()).as_posix()
            # Relative component names are enough to locate generated/runtime
            # artifacts. Control characters and oversized values are not logged.
            tail = re.sub(r"[^A-Za-z0-9_./+ -]", "?", tail)[:240]
            break
    return {"destination": relative(destination)[:240],
            "toc_kind": kind if kind in {"BINARY", "DATA", "EXTENSION", "SYMLINK", "DEPENDENCY", "ZIPFILE", "PYZ"} else "unrecognized",
            "source_basename": re.sub(r"[^A-Za-z0-9_.+-]", "?", source.name)[:100],
            "source_scope": scope, "source_relative": tail}


def validate_inputs(root: Path) -> dict:
    here = root / "packaging/windows"
    deps = load(here / "dependencies.json")
    if deps["target"] != {"os": "Windows", "architecture": "x64", "python": PYTHON, "abi": "cp314"}:
        raise ValueError("Wrong locked target")
    lock = (here / "requirements-build.lock").read_text(encoding="utf-8")
    entries = [line.strip() for line in lock.splitlines() if line.strip() and not line.startswith("#")]
    expected = [f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}" for r in deps["packages"]]
    if sorted(entries) != sorted(expected) or len(entries) != 9:
        raise ValueError("Lock does not exactly match reviewed wheel metadata")
    for item in deps["packages"]:
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or not item["url"].startswith("https://files.pythonhosted.org/"):
            raise ValueError("Invalid wheel origin/hash")
        relative(item["filename"])
        if not item["wheel_license_files"]:
            raise ValueError("Missing wheel license inventory")
    licenses = load(here / "runtime-licenses.json")
    # The actual Python3.14.10/Tcl9 candidate ships libtommath.dll. Keep its
    # separate upstream notice mandatory rather than assuming Tcl covers it.
    if not any(item.get("component") == "LibTomMath-Tcl-9.0.4.0" and
               item.get("file") == "licenses/LibTomMath-Tcl-9.0.4.0.txt" and
               item.get("source_url") == "https://raw.githubusercontent.com/python/cpython-source-deps/tcl-9.0.4.0/libtommath/LICENSE"
               for item in licenses["licenses"]):
        raise ValueError("Missing pinned LibTomMath notice for the Python3.14.10 Tcl9 runtime")
    for item in licenses["licenses"] + [licenses["upstream_sbom"]]:
        source = here / relative(item["file"])
        if source.is_symlink() or digest(source) != item["sha256"]:
            raise ValueError("Component notice hash differs")
    return deps


def stage(root: Path, destination: Path) -> None:
    validate_inputs(root)
    unexpected_pin = root / "opencomputer/_native_pin.py"
    if unexpected_pin.exists() or unexpected_pin.is_symlink():
        raise ValueError("Source checkout must not contain a generated native pin")
    if destination.exists():
        raise ValueError("Staging destination must be new; no existing files are deleted")
    files = ["LICENSE", "pyproject.toml"]
    files += ["scripts/install_skill.ps1", "skills/open-computer/SKILL.md",
              "skills/open-computer/scripts/open-computer.ps1", "skills/open-computer/references/hosts.md"]
    files += [f"opencomputer/{name}.py" for name in APP_MODULES]
    files += ["native/" + name for name in NATIVE_FILES]
    files += [f"packaging/windows/{name}" for name in PACKAGING_FILES]
    catalog = load(root / "packaging/windows/runtime-licenses.json")
    files += ["packaging/windows/" + r["file"] for r in catalog["licenses"] + [catalog["upstream_sbom"]]]
    for name in files:
        checked_copy(root / relative(name), destination / relative(name), root)
    (destination / STAGE_MARKER).write_text(json.dumps({"schema_version": 1,
        "kind": "opencomputer-portable-build-stage", "native_sources": native_source_inventory(root)}), encoding="utf-8")


def pe_header(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("Missing PE DOS header")
    start = struct.unpack_from("<I", data, 0x3c)[0]
    if start + 94 > len(data) or data[start:start + 4] != b"PE\0\0":
        raise ValueError("Invalid PE header")
    machine = struct.unpack_from("<H", data, start + 4)[0]
    magic = struct.unpack_from("<H", data, start + 24)[0]
    subsystem = struct.unpack_from("<H", data, start + 24 + 68)[0]
    if machine != 0x8664 or magic != 0x20b:
        raise ValueError("Expected x64 PE32+ binary")
    return {"machine": "x64", "subsystem": subsystem}


def privacy_bytes(data: bytes, forbidden_roots: list[str]) -> None:
    lowered = data.lower()
    for root in forbidden_roots:
        for form in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
            if form and any(form.encode(enc).lower() in lowered for enc in ("utf-8", "utf-16-le")):
                raise ValueError("Private build/home path found in distributable")
    if re.search(rb"[a-z]:[\\/]users[\\/][^\x00\r\n\\/]{1,100}[\\/]", lowered) or TOKEN.search(data):
        raise ValueError("User home path or credential-shaped content in distributable")


def code_filenames(code: types.CodeType):
    yield code.co_filename
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from code_filenames(value)


def check_code(code: types.CodeType, forbidden: list[str]) -> int:
    # Raw executable/ZIP scans cannot see compressed constants. Inspect the
    # decompressed marshalled object, including nested string/bytes constants.
    privacy_bytes(marshal.dumps(code), forbidden)
    count = 0
    for filename in code_filenames(code):
        privacy_bytes(filename.encode("utf-8"), forbidden)
        if filename.startswith(("/", "\\")) or Path(filename).is_absolute() or re.match(r"^[A-Za-z]:", filename):
            raise ValueError("Absolute bytecode filename; expected PyInstaller anonymization")
        count += 1
    return count


def check_native_pin_code(code: types.CodeType, expected_sha: str) -> None:
    expected = compile(f"NATIVE_SHA256 = {expected_sha!r}\n", "opencomputer/_native_pin.py", "exec")
    if (not isinstance(code, types.CodeType) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha)
            or code.co_code != expected.co_code or code.co_consts != expected.co_consts
            or code.co_names != expected.co_names or code.co_varnames != expected.co_varnames):
        raise ValueError("Compiled native pin is not the exact generated SHA256 literal")


def inspect_archives(bundle: Path, forbidden: list[str], native_sha: str | None = None) -> int:
    from PyInstaller.archive.readers import CArchiveReader

    count = 0
    for filename in ("OpenComputer.exe", "OpenComputerCLI.exe"):
        archive = CArchiveReader(str(bundle / filename))
        pin_count = 0
        for name, item in archive.toc.items():
            kind = item[-1]
            if kind == "z":
                pyz = archive.open_embedded_archive(name)
                for module in pyz.toc:
                    code = pyz.extract(module)
                    if isinstance(code, types.CodeType):
                        count += check_code(code, forbidden)
                        if module == "opencomputer._native_pin":
                            if native_sha is None:
                                raise ValueError("Generated native pin has no expected helper digest")
                            check_native_pin_code(code, native_sha)
                            pin_count += 1
            elif kind in {"s", "m", "M"}:
                code = marshal.loads(archive.extract(name))
                if not isinstance(code, types.CodeType):
                    raise ValueError("Unexpected executable Python archive entry")
                count += check_code(code, forbidden)
        if native_sha is not None and pin_count != 1:
            raise ValueError("Each frozen executable must contain exactly one generated native pin")
    with zipfile.ZipFile(bundle / "_internal/base_library.zip") as archive:
        for entry in archive.infolist():
            if entry.filename.endswith(".pyc"):
                if entry.file_size > 10 << 20:
                    raise ValueError("Unexpected oversized stdlib bytecode")
                code = marshal.loads(archive.read(entry)[16:])
                count += check_code(code, forbidden)
    if count < 100:
        raise ValueError("Bytecode inspection did not cover the expected application/runtime")
    return count


def _legacy_licenses(archive_path: Path, root: Path) -> dict[str, bytes]:
    if (canonical_digest(root / "native/Cargo.lock") != NATIVE_LOCK_LF_SHA
            or canonical_digest(root / "native/Cargo.toml") != NATIVE_TOML_LF_SHA):
        raise ValueError("Native Cargo inputs changed; reviewed dependency license reuse is not valid")
    if digest(archive_path) != NATIVE_ZIP_SHA:
        raise ValueError("Native archive differs from the reviewed published release")
    inputs = load(root / "packaging/windows/native-input.json")
    payloads = {}
    with zipfile.ZipFile(archive_path) as archive:
        members = {entry.filename: entry for entry in archive.infolist() if not entry.is_dir()}
        if len(members) != len([entry for entry in archive.infolist() if not entry.is_dir()]) or set(members) != {r["path"] for r in inputs["files"]}:
            raise ValueError("Native archive file allowlist differs")
        if sum(entry.file_size for entry in members.values()) > 10 << 20:
            raise ValueError("Oversized native release")
        build = json.loads(archive.read("BUILD-INFO.json"))
        licenses = json.loads(archive.read("third-party-licenses/manifest.json"))
        if (build["source_commit"] != NATIVE_SOURCE or build["native_sha256"] != NATIVE_SHA or
                build["native_input_capability"] is not False or len(licenses) != 34):
            raise ValueError("Native provenance/license capability differs")
        for item in inputs["files"]:
            name = relative(item["path"])
            if members[name].file_size != item["size"]:
                raise ValueError("Native member size differs")
            payload = archive.read(name)
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ValueError("Native member hash differs")
            if name not in {"opencomputer-native.exe", "BUILD-INFO.json"}:
                payloads[name] = payload
    if payloads["LICENSE"] != (root / "LICENSE").read_bytes():
        raise ValueError("Current project license differs from the reviewed license input")
    return payloads


def validate_native_build(build: dict, context: dict) -> None:
    if (type(build.get("schema_version")) is not int or build["schema_version"] != 1
            or build.get("native_input_capability") is not False
            or not re.fullmatch(r"[a-f0-9]{40}", str(context.get("source_commit", "")))
            or not re.fullmatch(r"[a-f0-9]{40}", str(context.get("native_tree", "")))
            or build.get("source_commit") != context["source_commit"]
            or build.get("native_tree") != context["native_tree"]
            or build.get("native_sources") != context.get("native_source_files")
            or build.get("cargo_lock_lf_sha256") != NATIVE_LOCK_LF_SHA
            or build.get("cargo_toml_lf_sha256") != NATIVE_TOML_LF_SHA):
        raise ValueError("Fresh native provenance does not match the current committed Cargo source")
    sources = build["native_sources"]
    if (not isinstance(sources, list) or {row.get("path") for row in sources} != {"native/" + name for name in NATIVE_FILES}
            or len(sources) != len(NATIVE_FILES)
            or any(not re.fullmatch(r"[a-f0-9]{64}", str(row.get("sha256", ""))) for row in sources)):
        raise ValueError("Native staged source inventory differs")
    by_name = {row["path"]: row["sha256"] for row in sources}
    if by_name["native/Cargo.lock"] != NATIVE_LOCK_LF_SHA or by_name["native/Cargo.toml"] != NATIVE_TOML_LF_SHA:
        raise ValueError("Native staged Cargo inputs differ from the reviewed dependency licenses")
    if (build.get("rust_toolchain") != RUST_TOOLCHAIN or build.get("rustc_version") != RUSTC_VERSION
            or build.get("target") != NATIVE_TARGET
            or not str(build.get("cargo_version", "")).startswith("cargo 1.99.0 ")
            or not isinstance(build.get("rustc_verbose"), str)
            or "host: " + NATIVE_TARGET not in build["rustc_verbose"].splitlines()
            or "release: 1.99.0" not in build["rustc_verbose"].splitlines()):
        raise ValueError("Fresh native toolchain differs from exact reviewed Rust1.99.0")
    sha = build.get("native_sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("Fresh native helper digest is malformed")
    if context["native_tree"] != NATIVE_TREE and sha == NATIVE_SHA:
        raise ValueError("Changed native source cannot reuse the old published helper")
    if (build.get("license_source") != {"archive_sha256": NATIVE_ZIP_SHA, "source_commit": NATIVE_SOURCE}
            or build.get("rust_library_notices") != [row for row in build.get("files", [])
                                                     if row["path"].startswith("rust-library-notices/")]):
        raise ValueError("Fresh native license provenance differs")


def _rust_notices(sysroot: Path) -> dict[str, bytes]:
    base = (sysroot / "share/doc/rust").resolve(strict=True)
    names = ["COPYRIGHT-library.html"] + ["licenses/" + name for name in RUST_LICENSE_FILES]
    payloads = {}
    for name in names:
        path = base / name
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(base) or path.stat().st_size > 2 << 20:
            raise ValueError("Rust library notice left its verified toolchain input or exceeded its bound")
        payloads["rust-library-notices/" + name] = path.read_bytes()
    return payloads


def prepare_native(root: Path, binary: Path, context_path: Path, license_zip: Path,
                   rust_sysroot: Path, output: Path) -> Path:
    marker = load(root / STAGE_MARKER)
    if marker.get("kind") != "opencomputer-portable-build-stage" or (root / ".git").exists():
        raise ValueError("Native build pin may be generated only in an isolated packaging stage")
    pin = root / "opencomputer/_native_pin.py"
    if pin.exists() or pin.is_symlink() or output.exists():
        raise ValueError("Generated native pin/archive must be new")
    expected_binary = root.parent / "native-target" / NATIVE_TARGET / "release/opencomputer-native.exe"
    if binary.is_symlink() or binary.resolve(strict=True) != expected_binary.resolve(strict=True):
        raise ValueError("Native helper must come from this isolated Cargo target directory")
    context = load(context_path)
    sources = native_source_inventory(root)
    if not context.get("source_clean") or sources != marker["native_sources"] or sources != context["native_source_files"]:
        raise ValueError("Staged native source changed before packaging")
    if pe_header(binary)["subsystem"] != 3:
        raise ValueError("Fresh native helper must be an AMD64 console executable")
    privacy_bytes(binary.read_bytes(), context["private_scan_roots"])
    payloads = _legacy_licenses(license_zip, root)
    payloads.update(_rust_notices(rust_sysroot))
    payloads["opencomputer-native.exe"] = binary.read_bytes()
    files = [{"path": name, "sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
             for name, value in sorted(payloads.items())]
    build = {"schema_version": 1, "source_commit": context["source_commit"], "native_tree": context["native_tree"],
        "native_sources": sources, "native_sha256": digest(binary), "native_input_capability": False,
        "cargo_lock_lf_sha256": canonical_digest(root / "native/Cargo.lock"),
        "cargo_toml_lf_sha256": canonical_digest(root / "native/Cargo.toml"),
        "rust_toolchain": RUST_TOOLCHAIN, "rustc_version": context["native_rustc_version"],
        "rustc_verbose": context["native_rustc_verbose"], "cargo_version": context["native_cargo_version"],
        "target": NATIVE_TARGET, "path_remapping": {"source": "/source", "repository": "/repository", "user": "/user"},
        "compiler_flags": {"debuginfo": 0, "strip": "symbols", "pdb_alt_path": "opencomputer-native.pdb"},
        "license_source": {"archive_sha256": NATIVE_ZIP_SHA, "source_commit": NATIVE_SOURCE},
        "rust_library_notices": [row for row in files if row["path"].startswith("rust-library-notices/")], "files": files}
    validate_native_build(build, context)
    encoded = json.dumps(build, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    privacy_bytes(encoded, context["private_scan_roots"])
    payloads["BUILD-INFO.json"] = encoded
    with pin.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(f"NATIVE_SHA256 = {build['native_sha256']!r}\n")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            archive.writestr(info, payload)
    context["native_artifact"] = {"archive_sha256": digest(output), "sha256": build["native_sha256"],
                                  "build_info_sha256": hashlib.sha256(encoded).hexdigest()}
    context_path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    return output


def native_payload(archive_path: Path, root: Path, bundle: Path, expected: set[str], context: dict | None = None) -> dict:
    artifact = (context or {}).get("native_artifact", {})
    if digest(archive_path) != artifact.get("archive_sha256"):
        raise ValueError("Native archive differs from this build's generated artifact")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        members = {entry.filename: entry for entry in entries}
        if len(members) != len(entries) or sum(entry.file_size for entry in entries) > 20 << 20:
            raise ValueError("Fresh native archive inventory is duplicate or oversized")
        encoded = archive.read("BUILD-INFO.json")
        if hashlib.sha256(encoded).hexdigest() != artifact.get("build_info_sha256"):
            raise ValueError("Fresh native build metadata changed")
        build = json.loads(encoded)
        validate_native_build(build, context)
        if build["native_sha256"] != artifact.get("sha256"):
            raise ValueError("Fresh native artifact digest differs")
        legacy_rows = load(root / "packaging/windows/native-input.json")["files"]
        legacy_names = {row["path"] for row in legacy_rows}
        allowed = (legacy_names - {"BUILD-INFO.json"}) | {"rust-library-notices/COPYRIGHT-library.html"}
        allowed |= {"rust-library-notices/licenses/" + name for name in RUST_LICENSE_FILES}
        rows = build["files"]
        if ({row["path"] for row in rows} != allowed or len(rows) != len(allowed)
                or set(members) != allowed | {"BUILD-INFO.json"}):
            raise ValueError("Fresh native archive allowlist differs")
        actual_rows = {row["path"]: row for row in rows}
        for row in legacy_rows:
            if row["path"] not in {"opencomputer-native.exe", "BUILD-INFO.json"}:
                actual = actual_rows[row["path"]]
                if actual["sha256"] != row["sha256"] or actual["size"] != row["size"]:
                    raise ValueError("Reused native dependency license bytes differ from their reviewed input")
        for row in rows + [{"path": "BUILD-INFO.json", "sha256": hashlib.sha256(encoded).hexdigest(), "size": len(encoded)}]:
            name = relative(row["path"])
            payload = archive.read(name)
            if len(payload) != row["size"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise ValueError("Fresh native member size/hash differs")
            target = "optional/opencomputer-native.exe" if name == "opencomputer-native.exe" else "THIRD_PARTY_NOTICES/native-current/" + name
            path = bundle / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            expected.add(target)
    return {"path": "optional/opencomputer-native.exe", "sha256": build["native_sha256"],
            "archive_sha256": digest(archive_path), "source_commit": build["source_commit"], "native_tree": build["native_tree"],
            "cargo_lock_lf_sha256": build["cargo_lock_lf_sha256"], "cargo_toml_lf_sha256": build["cargo_toml_lf_sha256"],
            "license_dependency_count": 34, "license_source": build["license_source"],
            "rust_library_notices": build["rust_library_notices"], "rust_toolchain": build["rust_toolchain"],
            "rustc_version": build["rustc_version"], "cargo_version": build["cargo_version"],
            "automatic_enable": True, "initialization": "lazy read-only semantics in frozen headless sessions", "supports_input": False}


def notices(root: Path, bundle: Path, wheels: Path, deps: dict, expected: set[str]) -> None:
    def copy(source: Path, target: str, source_root: Path):
        checked_copy(source, bundle / target, source_root)
        expected.add(target)

    runtime_license = Path(sys.base_prefix) / "LICENSE.txt"
    if "Microsoft Distributable Code" not in runtime_license.read_text(encoding="utf-8"):
        raise ValueError("CPython runtime notice lacks Microsoft distribution restrictions")
    copy(runtime_license, "THIRD_PARTY_NOTICES/runtime/CPython/LICENSE.txt", Path(sys.base_prefix))
    catalog = load(root / "packaging/windows/runtime-licenses.json")
    for entry in catalog["licenses"] + [catalog["upstream_sbom"]]:
        source = root / "packaging/windows" / entry["file"]
        copy(source, "THIRD_PARTY_NOTICES/runtime/CPython/components/" + Path(entry["file"]).name,
             root / "packaging/windows")
    for item in deps["packages"]:
        wheel = wheels / item["filename"]
        if digest(wheel) != item["sha256"]:
            raise ValueError("Downloaded wheel hash differs from lock")
        group = "runtime" if item["name"] == "Pillow" else "build-tools"
        with zipfile.ZipFile(wheel) as archive:
            for filename in item["wheel_license_files"]:
                target = f"THIRD_PARTY_NOTICES/{group}/{item['name']}-{item['version']}/{relative(filename)}"
                payload = archive.read(filename)
                path = bundle / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                expected.add(target)


def finalize(root: Path, bundle: Path, toc_path: Path, wheels: Path,
             context_path: Path, native_zip: Path, output: Path) -> Path:
    deps = validate_inputs(root)
    if sys.version.split()[0] != PYTHON or struct.calcsize("P") != 8 or sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise ValueError("Build requires standard GIL CPython 3.14.10 x64")
    import _tkinter
    if (_tkinter.TCL_VERSION, _tkinter.TK_VERSION) != ("9.0", "9.0"):
        raise ValueError("Tcl/Tk runtime differs from pinned Python3.14.10 component notices")
    context = load(context_path)
    if not context["source_clean"] or not re.fullmatch(r"[a-f0-9]{40}", context["source_commit"]):
        raise ValueError("Release candidate requires a clean committed source checkout")
    if (not re.fullmatch(r"[a-f0-9]{40}", str(context.get("native_tree", "")))
            or native_source_inventory(root) != context.get("native_source_files")):
        raise ValueError("Current staged native source differs from this committed build")
    expected = {"OpenComputer.exe", "OpenComputerCLI.exe"}
    omissions = load(toc_path.with_suffix(".os-components.json"))
    omitted_names = omissions["names"]
    if (omissions["policy"] != WINDOWS_OS_POLICY or not isinstance(omitted_names, list) or
            len(omitted_names) != len(set(omitted_names)) or
            any(not isinstance(name, str) or not WINDOWS_OS_COMPONENT.fullmatch(name) for name in omitted_names)):
        raise ValueError("OS component omission evidence differs from the narrow Windows10+ policy")
    origins = {}
    for row in load(toc_path):
        name = relative(row["path"])
        source = Path(row["source"]).resolve()
        generated_base_zip = (toc_path.parent / "work/OpenComputer/base_library.zip").resolve()
        is_generated_base_zip = name == "_internal/base_library.zip" and source == generated_base_zip
        if not is_generated_base_zip and not any(source.is_relative_to(p.resolve()) for p in (Path(sys.base_prefix), Path(sys.prefix), root)):
            detail = origin_diagnostic(source, name, row["kind"], root, toc_path)
            raise ValueError("Collected runtime came from outside reviewed build inputs: " + json.dumps(detail, sort_keys=True))
        if name in origins and digest(source) != digest(origins[name]):
            raise ValueError("Shared COLLECT destination has conflicting source bytes")
        origins[name] = source
        expected.add(name)
    for name, subsystem in (("OpenComputer.exe", 2), ("OpenComputerCLI.exe", 3)):
        if pe_header(bundle / name)["subsystem"] != subsystem:
            raise ValueError("Wrong console/windowed PE subsystem")
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual != expected or any(p.is_symlink() for p in bundle.rglob("*")):
        raise ValueError("COLLECT output differs from its explicit TOC allowlist")
    verify_os_components_absent(actual, omitted_names)
    for source, target in ((root / "LICENSE", "LICENSE"),
                           (root / "packaging/windows/PORTABLE-README.txt", "PORTABLE-README.txt"),
                           (root / "packaging/windows/Start-OpenComputer-Semantic.cmd", "Start-OpenComputer-Semantic.cmd"),
                           (root / "scripts/install_skill.ps1", "Install-OpenComputerSkill.ps1"),
                           (root / "skills/open-computer/SKILL.md", "skill/SKILL.md"),
                           (root / "skills/open-computer/scripts/open-computer.ps1", "skill/scripts/open-computer.ps1"),
                           (root / "skills/open-computer/references/hosts.md", "skill/references/hosts.md")):
        checked_copy(source, bundle / target, root)
        expected.add(target)
    notices(root, bundle, wheels, deps, expected)
    native = native_payload(native_zip, root, bundle, expected, context)
    native["cargo_lock_sha256"] = context["native_cargo_lock_sha256"]
    if digest(bundle / native["path"]) != native["sha256"]:
        raise ValueError("Extracted fresh native helper hash differs")
    pin = root / "opencomputer/_native_pin.py"
    check_native_pin_code(compile(pin.read_text(encoding="ascii"), "opencomputer/_native_pin.py", "exec"), native["sha256"])
    pe_header(bundle / native["path"])
    forbidden = context["private_scan_roots"]
    inventory = []
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Final payload differs from its file allowlist")
    for name in sorted(actual):
        path = bundle / name
        if any(part.casefold() in {".git", ".venv", "__pycache__", "artifacts", "tests", "connection.json"} for part in Path(name).parts):
            raise ValueError("Private/source-test file in payload")
        privacy_bytes(path.read_bytes(), forbidden)
        inventory.append({"path": name, "sha256": digest(path), "size": path.stat().st_size})
    code_count = inspect_archives(bundle, forbidden, native["sha256"])
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.]*)", version):
        raise ValueError("Unexpected application version")
    tool_versions = {item["name"]: importlib.metadata.version(item["name"]) for item in deps["packages"]}
    if any(tool_versions[r["name"]] != r["version"] for r in deps["packages"]):
        raise ValueError("Installed dependency differs from locked version")
    manifest = {"schema_version": 1, "app": {"version": version, "source_commit": context["source_commit"], "source_clean": True, "minimum_windows": "10", "capabilities": ["headless-v1"]},
                "toolchain": {"python": PYTHON, "architecture": "x64", "packages": tool_versions,
                              "runner_image": context["runner_image"], "windows": context["windows"]},
                "inputs": {"spec_sha256": digest(root / "packaging/windows/OpenComputer.spec"),
                           "lock_sha256": digest(root / "packaging/windows/requirements-build.lock"),
                           "wheels": [{k: r[k] for k in ("name", "version", "filename", "sha256")} for r in deps["packages"]],
                           "actions": context["actions"]}, "optional_native": native, "files": inventory,
                "os_components_omitted": {"policy": WINDOWS_OS_POLICY, "names": omitted_names},
                "checks": {"allowlist": "passed", "notice_hashes": "passed", "pe_subsystems": "passed",
                           "privacy_content": "passed", "bytecode_filename_count": code_count,
                           "clean_windows_vm": "not performed", "application_signing": "unsigned",
                           "bit_reproducibility": "not established"},
                "manifest_hash_policy": "Manifest excludes its own hash; archive SHA256 is supplied separately."}
    encoded = json.dumps(manifest, indent=2).encode() + b"\n"
    privacy_bytes(encoded, forbidden)
    (bundle / "build-manifest.json").write_bytes(encoded)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"OpenComputer-{version}-windows-x64-portable.zip"
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo("OpenComputer/" + path.relative_to(bundle).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    target.with_suffix(target.suffix + ".sha256").write_text(f"{digest(target)} *{target.name}\n", encoding="ascii")
    create_skill_archive(bundle, output)
    return target


def create_skill_archive(bundle: Path, output: Path) -> Path:
    """Build a self-contained skill from the exact portable manifest inventory."""
    manifest = load(bundle / "build-manifest.json")
    version = manifest["app"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or "headless-v1" not in manifest["app"].get("capabilities", []):
        raise ValueError("Skill requires a headless-capable release")
    with tempfile.TemporaryDirectory(prefix="opencomputer-skill-build-") as temporary:
        skill = Path(temporary) / "open-computer"
        runtime = skill / "runtime/OpenComputer"
        for row in manifest["files"]:
            name = relative(row["path"])
            source, destination = bundle / name, runtime / name
            checked_copy(source, destination, bundle)
            if destination.stat().st_size != row["size"] or digest(destination) != row["sha256"]:
                raise ValueError("Skill runtime copy differs from portable manifest")
        checked_copy(bundle / "build-manifest.json", runtime / "build-manifest.json", bundle)
        for name in ("SKILL.md", "scripts/open-computer.ps1", "references/hosts.md"):
            checked_copy(runtime / "skill" / name, skill / name, runtime)
        files = [{"path": path.relative_to(skill).as_posix(), "size": path.stat().st_size, "sha256": digest(path)}
                 for path in sorted(p for p in skill.rglob("*") if p.is_file())]
        (skill / "install-manifest.json").write_text(json.dumps({"schema_version": 1,
            "kind": "opencomputer-skill-v1", "version": version, "files": files}, indent=2) + "\n", encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        archive_path = output / f"OpenComputer-{version}-windows-x64-skill.zip"
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(p for p in skill.rglob("*") if p.is_file()):
                info = zipfile.ZipInfo("open-computer/" + path.relative_to(skill).as_posix(), (2026, 1, 1, 0, 0, 0))
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
                archive.writestr(info, path.read_bytes())
    archive_hash = digest(archive_path)
    archive_path.with_suffix(".zip.sha256").write_text(f"{archive_hash} *{archive_path.name}\n", encoding="ascii")
    installer = output / "Install-OpenComputerSkill.ps1"
    checked_copy(bundle / installer.name, installer, bundle)
    installer_hash = digest(installer)
    installer.with_suffix(".ps1.sha256").write_text(f"{installer_hash} *{installer.name}\n", encoding="ascii")
    # One PowerShell command; downloaded installer executes only after its pinned hash matches.
    url = f"https://github.com/jgouiza-debug/open-computer/releases/download/v{version}/{installer.name}"
    command = ("& { $ocInstaller = Join-Path ([IO.Path]::GetTempPath()) ('oc-skill-' + [guid]::NewGuid().ToString('N') + '.ps1'); "
        "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri '" + url + "' -OutFile $ocInstaller; "
        "$ocHash = [Security.Cryptography.SHA256]::Create(); try { $ocDigest = [BitConverter]::ToString($ocHash.ComputeHash([IO.File]::ReadAllBytes($ocInstaller))).Replace('-', '').ToLowerInvariant() } finally { $ocHash.Dispose() }; "
        "if ($ocDigest -ne '" + installer_hash + "') { throw 'Installer hash mismatch' }; "
        "& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ocInstaller -Release v" + version +
        " -ExpectedArchiveSha256 '" + archive_hash + "'; if ($LASTEXITCODE -ne 0) { throw 'Skill install failed' } } "
        "finally { if (Test-Path -LiteralPath $ocInstaller) { Remove-Item -LiteralPath $ocInstaller -Force } } }\n")
    (output / "install-skill-command.txt").write_text(command, encoding="utf-8")
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-inputs", "stage", "prepare-native", "finalize"))
    parser.add_argument("--root", type=Path, required=True)
    for name in ("destination", "bundle", "toc", "wheels", "context", "native-zip", "output", "native-exe", "license-zip", "rust-sysroot"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.command == "validate-inputs":
        validate_inputs(args.root)
        print("Locked input and notice hashes verified; no build or dependency installation performed.")
    elif args.command == "stage":
        if args.destination is None:
            parser.error("stage requires --destination")
        stage(args.root, args.destination)
    elif args.command == "prepare-native":
        if any(getattr(args, name) is None for name in ("native_exe", "context", "license_zip", "rust_sysroot", "output")):
            parser.error("prepare-native requires native-exe/context/license-zip/rust-sysroot/output")
        print(prepare_native(args.root, args.native_exe, args.context, args.license_zip, args.rust_sysroot, args.output))
    else:
        if any(getattr(args, name) is None for name in ("bundle", "toc", "wheels", "context", "native_zip", "output")):
            parser.error("finalize requires bundle/toc/wheels/context/native-zip/output")
        print(finalize(args.root, args.bundle, args.toc, args.wheels, args.context, args.native_zip, args.output))


if __name__ == "__main__":
    main()
