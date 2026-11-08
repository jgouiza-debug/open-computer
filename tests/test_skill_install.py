"""PowerShell installer boundary tests using inert files and an injected verifier.

No portable executable, GUI, native provider, model or desktop input is run.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_skill.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")


def inventory(root, excluded=()):
    return [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(root.rglob("*")) if p.is_file() and p.name not in excluded]


def fixture(path, version="0.1.6", capability=True):
    runtime = path / "runtime/OpenComputer"
    files = {"OpenComputerCLI.exe": b"INERT-NOT-A-PE-NOT-EXECUTED", "_internal/dependency.txt": b"fixture",
             "skill/SKILL.md": b"---\nname: open-computer\ndescription: fixture\n---\n",
             "skill/scripts/open-computer.ps1": b"throw 'must not execute'\n", "LICENSE": b"fixture license"}
    files['skill/references/hosts.md'] = b'fixture host workflow\n'
    for name, value in files.items():
        target = runtime / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    (runtime / "build-manifest.json").write_text(json.dumps({"schema_version": 1,
        "app": {"version": version, "capabilities": ["headless-v1"] if capability else []},
        "files": inventory(runtime)}), encoding="utf-8")
    (path / "scripts").mkdir()
    shutil.copyfile(runtime / "skill/SKILL.md", path / "SKILL.md")
    shutil.copyfile(runtime / "skill/scripts/open-computer.ps1", path / "scripts/open-computer.ps1")
    (path / "references").mkdir()
    shutil.copyfile(runtime / "skill/references/hosts.md", path / "references/hosts.md")
    (path / "install-manifest.json").write_text(json.dumps({"schema_version": 1,
        "kind": "opencomputer-skill-v1", "version": version, "files": inventory(path)}), encoding="utf-8")
    return runtime


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def windows_short_path(path):
    """Return a filesystem 8.3 alias when enabled; never change OS settings."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(str(path), buffer, len(buffer))
    return Path(buffer.value) if 0 < length < len(buffer) else None


@unittest.skipUnless(POWERSHELL, "PowerShell unavailable; no installer process test")
class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="skill install 配置 café ")
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"
        fixture(self.candidate)

    def tearDown(self):
        self.temporary.cleanup()

    def ps(self, body, executable=None):
        script = self.root / "test-script.ps1"
        script.write_text("$ErrorActionPreference='Stop'\n. " + quote(INSTALLER) +
            " -PortablePath " + quote(self.candidate) + "\n" + body, encoding="utf-8-sig")
        result = subprocess.run([executable or POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=self.root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def test_install_and_upgrade_unicode_path_with_fake_verifier(self):
        skills = self.root / "skills 文"
        skills.mkdir()
        unrelated = skills / "another-skill.txt"
        unrelated.write_text("preserve", encoding="utf-8")
        self.ps("$script:checks=0; $verify={param($p) $script:checks++; if (-not (Test-Path -LiteralPath (Join-Path $p 'runtime/OpenComputer/OpenComputerCLI.exe'))) {throw 'missing'}}; "
            f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $false $verify | Out-Null; "
            f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true $verify | Out-Null; "
            "if ($script:checks -ne 2) {throw 'verifier count'}")
        receipt = json.loads((skills / "open-computer/install-receipt.json").read_text())
        self.assertTrue(receipt["offline_checks_passed"])
        self.assertFalse(receipt["session_started"])
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertFalse(list(skills.glob(".opencomputer-*")))

    def test_verification_path_is_not_longer_than_final_runtime_on_ps5_and_ps7(self):
        shells = list(dict.fromkeys(filter(None, (shutil.which("powershell.exe"), shutil.which("pwsh")))))
        roots = [("long", self.root)]
        alias = windows_short_path(self.root)
        if alias is not None and str(alias).casefold() != str(self.root).casefold():
            roots.append(("short-alias", alias))
        for label, root in roots:
            for index, shell in enumerate(shells):
                with self.subTest(shell=Path(shell).name, path=label):
                    skills = root / ("Windows PowerShell skills " + label + str(index))
                    # PowerShell may expand a Python-supplied RUNNER~1 temp path.
                    # Compare actual sibling paths using the same provider rules.
                    self.ps("$verify={param($p) "
                        "$staged=Join-Path $p 'runtime/OpenComputer/OpenComputerCLI.exe'; "
                        "$final=Join-Path (Split-Path -Parent $p) 'open-computer/runtime/OpenComputer/OpenComputerCLI.exe'; "
                        "if ($staged.Length -gt $final.Length) {throw 'Verification lengthens runtime path'} }; "
                        f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $false $verify | Out-Null; "
                        f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true $verify | Out-Null", executable=shell)
                    self.assertFalse(list(skills.glob(".oc-*")))

    def test_tamper_and_unlisted_files_reject_before_verifier(self):
        for mode in ("hash", "extra"):
            with self.subTest(mode=mode):
                if mode == "hash":
                    path = self.candidate / "SKILL.md"
                    original = path.read_bytes()
                    path.write_bytes(b"tampered")
                else:
                    path = self.candidate / "unlisted.txt"
                    path.write_bytes(b"extra")
                self.ps(f"try {{ Install-CheckedSkill {quote(self.candidate)} {quote(self.root/'skills')} $false {{throw 'verifier must not run'}}; throw 'accepted' }} "
                        "catch {if ($_.Exception.Message -notmatch 'hash|Unlisted') {throw}}")
                if mode == "hash":
                    path.write_bytes(original)
                else:
                    path.unlink()

    def test_unmanaged_and_locally_changed_upgrade_preserved(self):
        skills = self.root / "skills"
        target = skills / "open-computer"
        target.mkdir(parents=True)
        note = target / "personal.txt"
        note.write_text("keep", encoding="utf-8")
        self.ps(f"try {{ Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true {{}}; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'unmanaged') {{throw}}}}")
        self.assertEqual(note.read_text(), "keep")
        shutil.rmtree(target)
        self.ps(f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $false {{}} | Out-Null")
        (target / "SKILL.md").write_text("my edit", encoding="utf-8")
        self.ps(f"try {{ Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true {{}}; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'hash') {{throw}}}}")
        self.assertEqual((target / "SKILL.md").read_text(), "my edit")

    def test_failed_offline_check_does_not_replace_existing_install(self):
        skills = self.root / "skills"
        self.ps(f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $false {{}} | Out-Null")
        previous = (skills / "open-computer/install-receipt.json").read_bytes()
        self.ps(f"try {{ Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true {{throw 'fake offline failure'}}; throw 'accepted' }} catch {{if ($_.Exception.Message -ne 'fake offline failure') {{throw}}}}")
        self.assertEqual((skills / "open-computer/install-receipt.json").read_bytes(), previous)
        self.assertFalse(list(skills.glob(".opencomputer-*")))

    def test_missing_capability_and_old_release_rejected(self):
        for version, capability in (("0.1.5", True), ("0.1.6", False)):
            with self.subTest(version=version, capability=capability):
                candidate = self.root / (version + str(capability))
                fixture(candidate, version, capability)
                self.ps(f"try {{ Install-CheckedSkill {quote(candidate)} {quote(self.root/'skills')} $false {{throw 'verifier must not run'}}; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'headless|Headless') {{throw}}}}")

    def test_local_portable_candidate_uses_verified_bundled_template(self):
        runtime = self.candidate / "runtime/OpenComputer"
        generated = self.root / "generated"
        generated.mkdir()
        self.ps(f"New-LocalCandidate {quote(runtime)} {quote(generated)}; Test-Inventory {quote(generated)} 'install-manifest.json' | Out-Null")
        self.assertEqual((generated / "SKILL.md").read_bytes(), (runtime / "skill/SKILL.md").read_bytes())

    def test_zip_traversal_duplicates_and_symlink_reject(self):
        for mode, names in (("traversal", ["../outside"]), ("duplicate", ["open-computer/a", "open-computer/A"]), ("link", ["open-computer/link"])):
            with self.subTest(mode=mode):
                archive = self.root / (mode + ".zip")
                with zipfile.ZipFile(archive, "w") as z:
                    for name in names:
                        info = zipfile.ZipInfo(name)
                        if mode == "link":
                            info.external_attr = (0o120777 << 16)
                        z.writestr(info, b"inert")
                self.ps(f"try {{ Expand-CheckedZip {quote(archive)} {quote(self.root/mode)}; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'Unsafe|duplicate|links') {{throw}}}}")
        self.assertFalse((self.root / "outside").exists())

    def test_cleanup_rejects_unowned_path(self):
        self.ps(f"try {{ Remove-OwnedStage {quote(self.root)} {quote(self.root.parent)}; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'boundary') {{throw}}}}")

    def test_active_runtime_blocks_upgrade_before_verifier(self):
        skills = self.root / "skills"
        self.ps(f"Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $false {{}} | Out-Null; "
            "function Assert-NoActiveRuntime {throw 'active fixture runtime'}; "
            f"try {{ Install-CheckedSkill {quote(self.candidate)} {quote(skills)} $true {{throw 'verifier must not run'}}; throw 'accepted' }} catch {{if ($_.Exception.Message -ne 'active fixture runtime') {{throw}}}}")

    def test_copy_uses_recorded_digest_when_original_changes(self):
        manifest = json.loads((self.candidate / "install-manifest.json").read_text())
        row = next(r for r in manifest["files"] if r["path"] == "SKILL.md")
        (self.candidate / "SKILL.md").write_bytes(b"changed after validation")
        record = self.root / "record.json"
        record.write_text(json.dumps([row]), encoding="utf-8")
        self.ps(f"$files=Get-Content -Raw -LiteralPath {quote(record)} -Encoding UTF8|ConvertFrom-Json; "
            f"try {{ Copy-Inventory {quote(self.candidate)} {quote(self.root/'copied')} $files; throw 'accepted' }} catch {{if ($_.Exception.Message -notmatch 'recorded hash') {{throw}}}}")

    def test_public_entry_rejects_wrong_archive_hash_without_runtime(self):
        archive = self.root / "invalid.zip"
        archive.write_bytes(b"not an archive; wrong hash must reject before extraction")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
            "-ArchivePath", str(archive), "-ExpectedArchiveSha256", "0" * 64, "-SkillRoot", str(self.root / "skills")],
            cwd=self.root, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"hash or size differs", result.stderr)
        self.assertFalse((self.root / "skills").exists())


if __name__ == "__main__":
    unittest.main()
