# PyInstaller 6.22.3: two independent embedded PYZs, one shared onedir COLLECT.
import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parents[1]
HERE = ROOT / "packaging" / "windows"
imports = [
    "opencomputer.app", "opencomputer.cli", "opencomputer.mcp", "opencomputer.setup",
    "opencomputer.model_client", "opencomputer.controller", "opencomputer.backend",
    "opencomputer.semantic", "opencomputer.native_client", "opencomputer.overlay",
    "opencomputer.server", "tkinter", "tkinter.ttk", "PIL.ImageGrab", "PIL.ImageTk",
]
imports += collect_submodules("PIL", filter=lambda name: not name.startswith("PIL.tests"))
common = dict(
    pathex=[str(ROOT)], binaries=[], datas=copy_metadata("Pillow"),
    hiddenimports=imports, hookspath=[], runtime_hooks=[],
    excludes=["pytest", "_pytest", "jsonschema", "pip", "setuptools", "PyInstaller", "tkinter.test"],
    noarchive=False, optimize=0,
)
gui = Analysis([str(HERE / "gui_entry.py")], **common)
cli = Analysis([str(HERE / "cli_entry.py")], **common)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="OpenComputer",
              debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              console=False, contents_directory="_internal")
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="OpenComputerCLI",
              debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              console=True, contents_directory="_internal")

# Private build evidence, never added to COLLECT. This is the precise runtime
# allowlist; no recursive repository or dist-directory inclusion is used.
toc = []
for analysis in (gui, cli):
    for destination, source, kind in list(analysis.binaries) + list(analysis.datas):
        toc.append({"path": "_internal/" + destination.replace("\\", "/"),
                    "source": source, "kind": kind})
Path(os.environ["OC_PORTABLE_TOC"]).write_text(json.dumps(toc), encoding="utf-8")
COLLECT(gui_exe, cli_exe, gui.binaries, cli.binaries, gui.datas, cli.datas,
        strip=False, upx=False, name="OpenComputer")
