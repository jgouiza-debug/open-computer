"""Opt-in installed MCP SDK smoke against an isolated read-only fake service.

No real panel, model, native observer, GUI, or input calls. No dependency install.
Root must review this harness before --run. Default invocation prints help only.
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import timedelta
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = '1.26.0'


class ReadOnlyFakeController:
    """No native backend exists; any mutation is a harness failure."""

    def __init__(self):
        self.calls = []
        self.mutations = 0
        self.delay_next_status = False
        self.slow_started = threading.Event()
        self.slow_finished = threading.Event()

    def status(self):
        self.calls.append('status')
        if self.delay_next_status:
            self.delay_next_status = False
            self.slow_started.set()
            # Deliberately bounded fake latency; no work or OS input is done.
            time.sleep(1.25)
            self.slow_finished.set()
        return {'armed': False, 'stopped': False, 'selected_window': None,
                'backend': 'demo', 'control_generation': 0, 'observation_id': None,
                'controller_instance_id': 'isolated-sdk-fixture',
                'semantic_provider': {'configured': False, 'state': 'disabled'}}

    def list_windows(self):
        self.calls.append('list_windows')
        return []

    def get_window_state(self):
        from opencomputer.server import ToolError
        self.calls.append('get_window_state')
        raise ToolError('no_target', 'This isolated fake has no selected window')

    def get_semantic_state(self):
        from opencomputer.server import ToolError
        self.calls.append('get_semantic_state')
        raise ToolError('semantic_unavailable', 'This isolated fake has no observer')

    def reject_mutation(self, *_args, **_kwargs):
        self.mutations += 1
        raise AssertionError('This proof must never request a state change')

    select_window = execute = stop = reject_mutation


def explicit_server_environment() -> dict[str, str]:
    """Allow only ordinary Windows runtime/profile variables; no Python paths."""
    allowed = {'systemroot', 'windir', 'comspec', 'pathext', 'temp', 'tmp',
               'userprofile', 'homedrive', 'homepath', 'appdata', 'localappdata',
               'programdata', 'os', 'number_of_processors', 'processor_architecture'}
    result = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    system = os.environ.get('SystemRoot') or os.environ.get('WINDIR')
    if not system:
        raise ValueError('An actual Windows system directory is required')
    result['PATH'] = str(Path(system) / 'System32')
    result['PYTHONUTF8'] = '1'
    result['PYTHONNOUSERSITE'] = '1'
    return result


def server_launch(connection: Path, directory: Path, *, server_command: Path | None = None,
                  server_args: list[str] | None = None, working_directory: Path | None = None,
                  server_environment: dict[str, str] | None = None) -> dict:
    if server_command is None:
        if server_args is not None or server_environment is not None:
            raise ValueError('Explicit server arguments/environment require a server command')
        return {'command': str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True)),
                'args': ['-P', '-m', 'opencomputer.mcp', '--connection', str(connection)],
                'env': {'PYTHONPATH': str(ROOT), 'PYTHONUTF8': '1'},
                'cwd': working_directory or directory}
    if not server_command.is_absolute() or not server_command.is_file():
        raise ValueError('Explicit server command must be an existing absolute executable')
    if server_args != ['mcp']:
        raise ValueError('This isolated probe permits only the explicit CLI mcp command')
    environment = dict(server_environment) if server_environment is not None else explicit_server_environment()
    if any(key.casefold() in {'pythonpath', 'pythonhome'} for key in environment):
        raise ValueError('Explicit server environment must omit Python source/runtime overrides')
    return {'command': str(server_command.resolve(strict=True)),
            'args': [*server_args, '--connection', str(connection.resolve())],
            'env': environment, 'cwd': working_directory or directory}


async def sdk_probe(connection: Path, controller: ReadOnlyFakeController, directory: Path, *,
                    server_command: Path | None = None, server_args: list[str] | None = None,
                    working_directory: Path | None = None,
                    server_environment: dict[str, str] | None = None) -> dict:
    import anyio
    import mcp.types as types
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client
    from mcp.shared.exceptions import McpError
    from opencomputer import __version__

    if importlib.metadata.version('mcp') != SDK_VERSION:
        raise RuntimeError('Review the installed SDK version before this version-pinned cancellation probe')
    if server_command is not None and any(key.casefold() in {'pythonpath', 'pythonhome'}
            for key in get_default_environment()):
        raise RuntimeError('SDK default environment unexpectedly inherits Python overrides')
    parameters = StdioServerParameters(**server_launch(connection, directory,
        server_command=server_command, server_args=server_args,
        working_directory=working_directory, server_environment=server_environment))
    result = {'sdk_version': SDK_VERSION, 'baseline_passed': False, 'cancellation_probe': {}}
    with (directory / 'sdk-stderr.txt').open('w', encoding='utf-8') as errors:
        with contextlib.redirect_stderr(errors):
            # SDK transport owns and bounds its child shutdown; only our MCP
            # bridge is launched, with an explicit nondefault connection path.
            async with stdio_client(parameters, errlog=errors) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3)) as session:
                    initialized = await session.initialize()
                    result['initialize'] = initialized.model_dump(mode='json', by_alias=True, exclude_none=True)
                    if initialized.serverInfo.name != 'open-computer' or initialized.serverInfo.version != __version__:
                        raise RuntimeError('Unexpected bridge identity/version')
                    await session.send_ping()
                    tools = await session.list_tools()
                    names = [tool.name for tool in tools.tools]
                    if len(names) != 13 or 'status' not in names or 'get_semantic_state' not in names:
                        raise RuntimeError('Unexpected tool discovery')
                    result['tool_names'] = names
                    status = await session.call_tool('status', {})
                    if status.isError or len(status.content) != 1 or status.content[0].type != 'text':
                        raise RuntimeError('Status result was not standard MCP text content')
                    state = json.loads(status.content[0].text)
                    if state.get('armed') is not False or state.get('backend') != 'demo':
                        raise RuntimeError('Status did not come from the isolated fake')
                    negative = await session.call_tool('get_window_state', {})
                    if not negative.isError or 'no_target' not in negative.content[0].text:
                        raise RuntimeError('Structured business error was not preserved')
                    result['baseline_passed'] = True
                    result['status'] = state
                    result['business_error_is_error'] = negative.isError

                    controller.delay_next_status = True
                    # SDK1.26 has no public cancellation helper or exposed return
                    # request ID. Read its next counter for this single pending
                    # request, then send the SDK's typed standard notification.
                    request_id = session._request_id
                    if type(request_id) is not int:
                        raise RuntimeError('SDK request ID API changed')
                    probe = result['cancellation_probe']

                    async def delayed_status():
                        started = time.perf_counter()
                        try:
                            await session.call_tool('status', {}, read_timeout_seconds=timedelta(seconds=0.2))
                            probe['request_timed_out'] = False
                        except McpError as error:
                            probe['request_timed_out'] = error.error.code == 408
                            probe['client_error_code'] = error.error.code
                        finally:
                            probe['client_request_ms'] = round((time.perf_counter() - started) * 1000, 3)

                    async with anyio.create_task_group() as tasks:
                        tasks.start_soon(delayed_status)
                        with anyio.fail_after(2):
                            while not controller.slow_started.is_set():
                                await anyio.sleep(0.01)
                        await session.send_notification(types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=request_id,
                                reason='Cancel isolated read-only fake status'))))
                        probe['typed_cancellation_notification_sent'] = True
                        started = time.perf_counter()
                        with anyio.fail_after(3):
                            await session.send_ping()
                        probe['cancellation_to_ping_ms'] = round((time.perf_counter() - started) * 1000, 3)
                        probe['slow_http_completed_before_ping'] = controller.slow_finished.is_set()
                    probe['head_of_line_gap_observed'] = bool(probe.get('request_timed_out') and
                        probe['slow_http_completed_before_ping'] and probe['cancellation_to_ping_ms'] > 500)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Run only SDK/fake-HTTP compatibility probes')
    parser.add_argument('--server-command', type=Path, help='Reviewed absolute portable CLI executable')
    parser.add_argument('--server-arg', action='append', help='Repeat for the explicit argument list (only mcp)')
    options = parser.parse_args(argv)
    if options.server_command is not None:
        if (not options.server_command.is_absolute() or not options.server_command.is_file()
                or options.server_arg != ['mcp']):
            parser.error('--server-command requires an existing absolute file and --server-arg mcp')
    elif options.server_arg is not None:
        parser.error('--server-arg requires --server-command')
    if not options.run:
        parser.print_help()
        return 0
    sys.path.insert(0, str(ROOT))
    import anyio
    from opencomputer.server import start_server

    directory = ROOT / 'artifacts/compatibility' / ('sdk-' + time.strftime('%Y%m%dT%H%M%S-') + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    controller = ReadOnlyFakeController()
    service = None
    report = {'passed': False, 'real_panel_calls': 0, 'native_observer_calls': 0, 'desktop_calls': 0,
              'model_calls': 0, 'input_calls': 0,
              'mcp_source_sha256': hashlib.sha256((ROOT / 'opencomputer/mcp.py').read_bytes()).hexdigest(),
              'scope': 'Installed SDK -> own stdio child -> own port0 fake HTTP; no real controller/backend'}
    try:
        report['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        connection = directory / 'fake-connection.json'
        service = start_server(controller, host='127.0.0.1', port=0, connection_path=connection)

        async def bounded_probe():
            with anyio.fail_after(15):
                return await sdk_probe(connection, controller, directory,
                    server_command=options.server_command, server_args=options.server_arg)

        report.update(anyio.run(bounded_probe))
        report['passed'] = report['baseline_passed'] and controller.mutations == 0
    except BaseException as error:
        report['failure'] = type(error).__name__
        report['passed'] = False
    finally:
        if service is not None:
            service.close()
        report['fake_http_calls'] = controller.calls
        report['input_calls'] = controller.mutations
        (directory / 'receipt.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({**report, 'receipt': str(directory / 'receipt.json')}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
