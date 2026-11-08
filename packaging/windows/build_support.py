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
import tomllib
import types
from typing import Any
import zipfile

PYTHON = "3.14.10"
NATIVE_SHA = "3ba321acc65b9f84131c0e615f4817eb5865f4b3ab668c298a5535b29e7a757d"
NATIVE_ZIP_SHA = "7d101ac98ab1a2c1cba0ed8a212454e7d39d5e882a49f42c858b4a43eddcd869"
NATIVE_SOURCE = "05c3721b3962ace619441431fcdb8e4eb4be2e93"
NATIVE_TREE = "bb5910da594cefabb3cb7e00b41c1e0ca14c1199"
APP_MODULES = (
    "__init__", "app", "backend", "cli", "controller", "mcp", "model_client",
    "native_client", "overlay", "semantic", "server", "setup",
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
    if destination.exists():
        raise ValueError("Staging destination must be new; no existing files are deleted")
    files = ["LICENSE", "pyproject.toml"]
    files += [f"opencomputer/{name}.py" for name in APP_MODULES]
    files += [f"packaging/windows/{name}" for name in PACKAGING_FILES]
    catalog = load(root / "packaging/windows/runtime-licenses.json")
    files += ["packaging/windows/" + r["file"] for r in catalog["licenses"] + [catalog["upstream_sbom"]]]
    for name in files:
        checked_copy(root / relative(name), destination / relative(name), root)


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


def inspect_archives(bundle: Path, forbidden: list[str]) -> int:
    from PyInstaller.archive.readers import CArchiveReader

    count = 0
    for filename in ("OpenComputer.exe", "OpenComputerCLI.exe"):
        archive = CArchiveReader(str(bundle / filename))
        for name, item in archive.toc.items():
            kind = item[-1]
            if kind == "z":
                pyz = archive.open_embedded_archive(name)
                for module in pyz.toc:
                    code = pyz.extract(module)
                    if isinstance(code, types.CodeType):
                        count += check_code(code, forbidden)
            elif kind in {"s", "m", "M"}:
                code = marshal.loads(archive.extract(name))
                if not isinstance(code, types.CodeType):
                    raise ValueError("Unexpected executable Python archive entry")
                count += check_code(code, forbidden)
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


def native_payload(archive_path: Path, root: Path, bundle: Path, expected: set[str]) -> dict:
    if digest(archive_path) != NATIVE_ZIP_SHA:
        raise ValueError("Native archive differs from the reviewed published release")
    inputs = load(root / "packaging/windows/native-input.json")
    with zipfile.ZipFile(archive_path) as archive:
        members = {entry.filename: entry for entry in archive.infolist() if not entry.is_dir()}
        if set(members) != {r["path"] for r in inputs["files"]}:
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
            target = "optional/opencomputer-native.exe" if name == "opencomputer-native.exe" else "THIRD_PARTY_NOTICES/native-v0.1.3/" + name
            path = bundle / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            expected.add(target)
    return {"path": "optional/opencomputer-native.exe", "sha256": NATIVE_SHA,
            "archive_sha256": NATIVE_ZIP_SHA, "source_commit": NATIVE_SOURCE,
            "native_tree": NATIVE_TREE, "license_dependency_count": 34,
            "rust_toolchain": "Not recorded by reused published artifact; not rebuilt",
            "automatic_enable": False, "supports_input": False}


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
    if context["native_tree"] != NATIVE_TREE:
        raise ValueError("Native source changed since the reused helper was built")
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
                           (root / "packaging/windows/Start-OpenComputer-Semantic.cmd", "Start-OpenComputer-Semantic.cmd")):
        checked_copy(source, bundle / target, root)
        expected.add(target)
    notices(root, bundle, wheels, deps, expected)
    native = native_payload(native_zip, root, bundle, expected)
    native["cargo_lock_sha256"] = context["native_cargo_lock_sha256"]
    if digest(bundle / native["path"]) != NATIVE_SHA:
        raise ValueError("Extracted native helper hash differs")
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
    code_count = inspect_archives(bundle, forbidden)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.]*)", version):
        raise ValueError("Unexpected application version")
    tool_versions = {item["name"]: importlib.metadata.version(item["name"]) for item in deps["packages"]}
    if any(tool_versions[r["name"]] != r["version"] for r in deps["packages"]):
        raise ValueError("Installed dependency differs from locked version")
    manifest = {"schema_version": 1, "app": {"version": version, "source_commit": context["source_commit"], "source_clean": True, "minimum_windows": "10"},
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
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-inputs", "stage", "finalize"))
    parser.add_argument("--root", type=Path, required=True)
    for name in ("destination", "bundle", "toc", "wheels", "context", "native-zip", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.command == "validate-inputs":
        validate_inputs(args.root)
        print("Locked input and notice hashes verified; no build or dependency installation performed.")
    elif args.command == "stage":
        if args.destination is None:
            parser.error("stage requires --destination")
        stage(args.root, args.destination)
    else:
        if any(getattr(args, name) is None for name in ("bundle", "toc", "wheels", "context", "native_zip", "output")):
            parser.error("finalize requires bundle/toc/wheels/context/native-zip/output")
        print(finalize(args.root, args.bundle, args.toc, args.wheels, args.context, args.native_zip, args.output))


if __name__ == "__main__":
    main()
