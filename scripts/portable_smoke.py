"""Opt-in relocated portable CLI/fake-SDK validation; GUI is a separate opt-in.

No arguments or --help launches nothing. Review this source before --run.
No screenshots, model requests, native helper requests or real desktop input.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SDK_PROBE = ROOT / 'scripts/mcp_sdk_smoke.py'
MAX_LOG = 2 * 1024 * 1024


class SmokeError(RuntimeError):
    """Fixed non-sensitive validation diagnosis."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def strict_json(raw: str | bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SmokeError('Duplicate JSON field')
            value[key] = item
        return value

    def constant(_value):
        raise SmokeError('Nonfinite JSON value')

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def sanitized_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    allowed = {'systemroot', 'windir', 'comspec', 'pathext', 'temp', 'tmp', 'userprofile',
               'homedrive', 'homepath', 'appdata', 'localappdata', 'programdata', 'os',
               'number_of_processors', 'processor_architecture'}
    result = {key: value for key, value in source.items() if key.casefold() in allowed}
    system = next((value for key, value in source.items() if key.casefold() == 'systemroot'), None)
    if not system or not Path(system).is_absolute():
        raise SmokeError('Windows system directory is unavailable')
    # SystemRoot itself can contain a globally installed py.exe launcher.
    result['PATH'] = str(Path(system) / 'System32')
    result['PYTHONUTF8'], result['PYTHONNOUSERSITE'] = '1', '1'
    return result


def safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise SmokeError('Manifest path is invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != value:
        raise SmokeError('Manifest path leaves the bundle')
    return path


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def stage_bundle(source: Path, destination: Path) -> dict:
    """Copy only hash-verified manifest files; never recursively copy the repo."""
    source = source.resolve(strict=True)
    manifest_path = source / 'build-manifest.json'
    with manifest_path.open('rb') as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise SmokeError('Build manifest exceeds limit')
    manifest = strict_json(raw)
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or
            not isinstance(manifest.get('app'), dict) or not isinstance(manifest['app'].get('version'), str)):
        raise SmokeError('Build manifest schema is unsupported')
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 20000:
        raise SmokeError('Manifest file count is invalid')
    seen, total = set(), 0
    verified = []
    for item in files:
        if not isinstance(item, dict):
            raise SmokeError('Manifest file record is invalid')
        relative = safe_relative(item.get('path'))
        key = relative.as_posix().casefold()
        if key in seen or key == 'build-manifest.json':
            raise SmokeError('Manifest has duplicate or self-referential paths')
        seen.add(key)
        origin = source.joinpath(*relative.parts)
        chain = [origin]
        while chain[-1] != source:
            chain.append(chain[-1].parent)
        if (not origin.resolve(strict=True).is_relative_to(source) or not origin.is_file()
                or any(is_link(parent) for parent in chain)):
            raise SmokeError('Manifest references a link or non-file')
        size = item.get('size')
        expected = item.get('sha256')
        if (type(size) is not int or size < 0 or not isinstance(expected, str) or len(expected) != 64
                or origin.stat().st_size != size or sha256(origin) != expected):
            raise SmokeError('Manifest file size/hash mismatch')
        total += size
        if total > 2 * 1024**3:
            raise SmokeError('Bundle exceeds validation copy limit')
        verified.append((relative, origin, expected))
    required = {'opencomputer.exe', 'opencomputercli.exe', 'license', 'portable-readme.txt'}
    if not required.issubset(seen) or not any(p.startswith('_internal/') for p in seen):
        raise SmokeError('Required portable files are absent')
    if not any(p.startswith('third_party_notices/') for p in seen):
        raise SmokeError('Third-party notices are absent')
    actual = set()
    for parent, directories, names in os.walk(source, followlinks=False):
        for name in directories:
            if is_link(Path(parent) / name):
                raise SmokeError('Bundle contains a linked directory')
        for name in names:
            path = Path(parent) / name
            if is_link(path):
                raise SmokeError('Bundle contains a linked file')
            actual.add(path.relative_to(source).as_posix().casefold())
            if len(actual) > 20001:
                raise SmokeError('Bundle contains too many files')
    if actual != seen | {'build-manifest.json'}:
        raise SmokeError('Bundle contains unlisted files')
    destination.mkdir(parents=True, exist_ok=False)
    for relative, origin, expected in verified:
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        if sha256(target) != expected:
            raise SmokeError('Relocated copy hash mismatch')
    (destination / 'build-manifest.json').write_bytes(raw)
    return manifest


def verify_pe(path: Path, subsystem: int) -> dict:
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise SmokeError('Portable executable has no PE header')
        offset = struct.unpack_from('<I', header, 60)[0]
        if offset > 1024 * 1024:
            raise SmokeError('PE header offset is invalid')
        stream.seek(offset)
        header = stream.read(96)
    if (len(header) < 96 or header[:4] != b'PE\0\0' or
            struct.unpack_from('<H', header, 4)[0] != 0x8664 or
            struct.unpack_from('<H', header, 24)[0] != 0x20B or
            struct.unpack_from('<H', header, 24 + 68)[0] != subsystem):
        raise SmokeError('Expected Windows x64 executable subsystem is missing')
    return {'machine': 'x64', 'subsystem': 'gui' if subsystem == 2 else 'console'}


def file_state(path: Path) -> dict:
    if not path.exists():
        return {'present': False}
    if not path.is_file():
        raise SmokeError('Default connection path is not an ordinary file')
    return {'present': True, 'size': path.stat().st_size, 'sha256': sha256(path)}


def native_modules(pid: int) -> list[Path]:
    """Read DLL/PYD mappings of one owned child, never Python object contents."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return []
    try:
        modules = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3):
            return []
        if needed.value > ctypes.sizeof(modules):
            raise SmokeError('Owned child module list exceeded its bound')
        paths = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            buffer = ctypes.create_unicode_buffer(32768)
            if psapi.GetModuleFileNameExW(handle, module, buffer, len(buffer)):
                paths.append(Path(buffer.value))
        return paths
    finally:
        kernel.CloseHandle(handle)


def hide_owned_windows(pid: int) -> None:
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow.restype = wintypes.BOOL
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL

    @callback_type
    def visit(window, _parameter):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid:
            user.ShowWindow(window, 0)
        return True

    user.EnumWindows(visit, 0)


def module_summary(paths: set[Path], bundle: Path) -> dict:
    bundled, other = set(), set()
    for path in paths:
        resolved = path.resolve()
        if resolved.is_relative_to(bundle):
            bundled.add(resolved.relative_to(bundle).as_posix())
        else:
            # Do not retain a user's outside module path or file contents.
            other.add(path.name)
    outside_python = sorted(name for name in other if name.casefold().startswith('python') and name.casefold().endswith('.dll'))
    outside_extensions = sorted(name for name in other if name.casefold().endswith('.pyd'))
    outside_tcl_tk = sorted(name for name in other if name.casefold().startswith(('tcl', 'tk'))
                            and name.casefold().endswith('.dll'))
    return {'bundle_native_modules': sorted(bundled), 'outside_module_basenames': sorted(other),
            'outside_python_dlls': outside_python, 'outside_python_extensions': outside_extensions,
            'outside_tcl_tk_dlls': outside_tcl_tk, 'pure_python_module_origins': 'not measured'}


def owned_process(command: list[str], cwd: Path, environment: dict[str, str], output: Path,
                  bundle: Path, *, gui: bool = False, timeout: float = 30) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    stdout, stderr = output / 'stdout.txt', output / 'stderr.txt'
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = None
    paths: set[Path] = set()
    began = time.monotonic()
    try:
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            while process.poll() is None:
                paths.update(native_modules(process.pid))
                if gui:
                    hide_owned_windows(process.pid)
                if time.monotonic() - began > timeout:
                    raise SmokeError('Owned portable child exceeded its deadline')
                if stdout.stat().st_size > MAX_LOG or stderr.stat().st_size > MAX_LOG:
                    raise SmokeError('Owned portable child exceeded log limit')
                time.sleep(0.015)
            result = {'pid': process.pid, 'exit_code': process.returncode,
                      'elapsed_ms': round((time.monotonic() - began) * 1000, 3),
                      'gui_hidden_requested': gui, 'process_reaped': True,
                      **module_summary(paths, bundle)}
        if stdout.stat().st_size > MAX_LOG or stderr.stat().st_size > MAX_LOG:
            raise SmokeError('Owned portable child exceeded log limit')
        if result['exit_code'] != 0:
            raise SmokeError('Portable command returned a failure exit code')
        if (result['outside_python_dlls'] or result['outside_python_extensions']
                or result['outside_tcl_tk_dlls']):
            raise SmokeError('Owned child loaded Python or Tcl/Tk outside its portable bundle')
        if not any(Path(p).name.casefold().startswith('python') and p.casefold().endswith('.dll')
                   for p in result['bundle_native_modules']):
            raise SmokeError('Bundled Python DLL provenance was not observed')
        return result
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def verify_config(value: dict, cli: Path) -> dict:
    if (not isinstance(value, dict) or set(value) != {'mcpServers'} or
            not isinstance(value['mcpServers'], dict) or set(value['mcpServers']) != {'open-computer'}):
        raise SmokeError('Portable MCP config structure is invalid')
    server = value['mcpServers']['open-computer']
    if (not isinstance(server, dict) or set(server) != {'command', 'args', 'env'} or
            not isinstance(server['command'], str) or Path(server['command']).resolve() != cli or
            server['args'] != ['mcp'] or server['env'] != {}):
        raise SmokeError('Portable MCP config does not use its sibling CLI')
    return {'sibling_cli_matches': True, 'args': ['mcp'], 'env': {}}


def load_sdk_probe():
    spec = importlib.util.spec_from_file_location('portable_sdk_probe', SDK_PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sdk_check(cli: Path, cwd: Path, environment: dict[str, str], output: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    import anyio
    from opencomputer.server import start_server
    probe = load_sdk_probe()
    output.mkdir(parents=True, exist_ok=False)
    controller = probe.ReadOnlyFakeController()
    connection = output / 'fake-connection.json'
    server = start_server(controller, port=0, connection_path=connection)
    try:
        async def run():
            with anyio.fail_after(20):
                return await probe.sdk_probe(connection, controller, output,
                    server_command=cli, server_args=['mcp'], working_directory=cwd,
                    server_environment=environment)
        result = anyio.run(run)
        result.update(fake_http_calls=controller.calls, input_calls=controller.mutations,
                      source_probe_sha256=sha256(SDK_PROBE))
        if not result.get('baseline_passed') or controller.mutations:
            raise SmokeError('Frozen SDK/fake-service probe failed')
        return result
    finally:
        server.close()


def cleanup_staging(path: Path, parent: Path) -> None:
    if (path.resolve().parent != parent.resolve() or not path.name.startswith('OpenComputer-portable-')
            or is_link(path)):
        raise SmokeError('Refusing cleanup outside the owned staging directory')
    shutil.rmtree(path)


def run(bundle: Path, output: Path, *, include_gui: bool = False) -> dict:
    if os.name != 'nt':
        raise SmokeError('Portable execution validation requires Windows')
    environment = sanitized_environment()
    if any(shutil.which(name, path=environment['PATH']) for name in ('python', 'python3', 'pythonw', 'py')):
        raise SmokeError('Restricted child PATH still resolves Python')
    output.mkdir(parents=True, exist_ok=False)
    default_connection = Path.home() / '.opencomputer/connection.json'
    before = file_state(default_connection)
    parent = Path(tempfile.gettempdir()).resolve()
    staging = Path(tempfile.mkdtemp(prefix='OpenComputer-portable-é漢-', dir=parent)).resolve()
    result = {'passed': False, 'fresh_vm_tested': False, 'visual_gui_observed': False,
              'model_calls': 0, 'desktop_input_calls': 0, 'native_helper_calls': 0,
              'child_path_has_python': False, 'python_overrides_removed': True,
              'scope': 'Relocated candidate; system-only child PATH; own fake HTTP/stdio only',
              'checks': {}, 'default_connection_before': before}
    try:
        relocated = staging / 'Portable app é 文'
        manifest = stage_bundle(bundle, relocated)
        cli, gui = relocated / 'OpenComputerCLI.exe', relocated / 'OpenComputer.exe'
        result.update(version=manifest['app']['version'], manifest_sha256=sha256(relocated / 'build-manifest.json'),
                      source_commit=manifest['app'].get('source_commit'))
        result['checks']['pe_cli'], result['checks']['pe_gui'] = verify_pe(cli, 3), verify_pe(gui, 2)
        cwd = staging / 'Unrelated cwd 配置'
        cwd.mkdir()
        for name, arguments in (('help', ['--help']), ('doctor', ['setup', 'doctor', '--offline']),
                                ('config', ['setup', 'config'])):
            result['checks'][name] = owned_process([str(cli), *arguments], cwd, environment, output / name, relocated)
        doctor = strict_json((output / 'doctor/stdout.txt').read_bytes())
        if doctor.get('ok') is not True or doctor.get('gui', {}).get('checked') is not False:
            raise SmokeError('Portable offline doctor did not pass without live access')
        doctor_modules = [Path(path).name.casefold()
                          for path in result['checks']['doctor']['bundle_native_modules']]
        if not any(name.startswith('_imaging') and name.endswith('.pyd') for name in doctor_modules):
            raise SmokeError('Bundled Pillow native extension provenance was not observed')
        result['config'] = verify_config(strict_json((output / 'config/stdout.txt').read_bytes()), cli)
        (cwd / 'settings').mkdir()
        relative_export = Path('settings') / 'MCP 配置 é.json'
        result['checks']['export'] = owned_process([str(cli), 'setup', 'config', '--output', str(relative_export)],
            cwd, environment, output / 'export', relocated)
        verify_config(strict_json((cwd / relative_export).read_bytes()), cli)
        result['relative_unicode_export'] = True
        result['sdk'] = sdk_check(cli, cwd, environment, output / 'sdk')
        if include_gui:
            result['checks']['gui'] = owned_process([str(gui), '--demo', '--no-server', '--smoke-test'],
                cwd, environment, output / 'gui', relocated, gui=True)
            modules = [Path(path).name.casefold() for path in result['checks']['gui']['bundle_native_modules']]
            if not all(any(name.startswith(prefix) and name.endswith('.dll') for name in modules)
                       for prefix in ('tk', 'tcl')):
                raise SmokeError('Bundled Tcl/Tk DLL provenance was not observed')
            if not any(name.startswith('_tkinter') and name.endswith('.pyd') for name in modules):
                raise SmokeError('Bundled Tkinter native extension provenance was not observed')
            result['gui_initialization_and_self_exit'] = True
        else:
            result['gui_initialization_and_self_exit'] = 'not requested'
        result['passed'] = True
    except Exception as error:
        result['passed'] = False
        result['error'] = str(error) if isinstance(error, SmokeError) else type(error).__name__
    finally:
        try:
            after = file_state(default_connection)
            result['default_connection_after'] = after
            result['default_connection_unchanged'] = before == after
            if before != after:
                result['passed'] = False
        except Exception:
            result['passed'] = False
            result['default_connection_unchanged'] = False
            result['connection_check_error'] = 'Connection metadata check failed'
        try:
            cleanup_staging(staging, parent)
            result['owned_staging_removed'] = True
        except Exception:
            result['passed'] = False
            result['owned_staging_removed'] = False
            result['cleanup_error'] = 'Owned staging cleanup failed'
        (output / 'receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize reviewed candidate CLI and fake SDK execution')
    parser.add_argument('--bundle', type=Path, help='Explicit absolute reviewed candidate folder')
    parser.add_argument('--include-gui', action='store_true', help='Separately authorize hidden demo/no-server GUI smoke')
    parser.add_argument('--artifacts', type=Path, default=ROOT / 'artifacts/portable')
    options = parser.parse_args(argv)
    if not options.run:
        parser.print_help()
        return 0
    if options.bundle is None or not options.bundle.is_absolute() or not options.bundle.is_dir():
        parser.error('--run requires an existing absolute --bundle directory')
    output = options.artifacts.resolve() / (time.strftime('%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8])
    try:
        result = run(options.bundle, output, include_gui=options.include_gui)
    except Exception as error:
        print(json.dumps({'passed': False, 'error': str(error) if isinstance(error, SmokeError) else type(error).__name__}))
        return 1
    print(json.dumps({'passed': result['passed'], 'receipt': str(output / 'receipt.json'),
                      'fresh_vm_tested': False, 'visual_gui_observed': False}, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
