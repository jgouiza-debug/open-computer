param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

function Assert-Success([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed (exit $LASTEXITCODE)."
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "Open Computer's native app requires Windows."
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    Assert-Success "Python 3.11+ check"
    & $Python -P -m venv $venvRoot
    Assert-Success "Local virtual environment creation"
}

& $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
Assert-Success "Virtual environment Python check"
& $venvPython -P -m pip install --disable-pip-version-check --upgrade $repoRoot
Assert-Success "Local package installation"

# %~dp0 resolves the launcher's own absolute directory, independently of cwd.
# Avoid embedding user paths so cmd's text encoding cannot corrupt Unicode.
$startLauncher = '@echo off' + "`r`n" + '"%~dp0.venv\Scripts\python.exe" -P -m opencomputer.app %*' + "`r`n"
$mcpLauncher = '@echo off' + "`r`n" + '"%~dp0.venv\Scripts\python.exe" -P -m opencomputer.mcp %*' + "`r`n"
[System.IO.File]::WriteAllText((Join-Path $repoRoot "start-open-computer.cmd"), $startLauncher, [System.Text.Encoding]::Default)
[System.IO.File]::WriteAllText((Join-Path $repoRoot "open-computer-mcp.cmd"), $mcpLauncher, [System.Text.Encoding]::Default)

& $venvPython -P -m opencomputer.setup doctor --offline
Assert-Success "Installed dependency check"
Write-Host "Installed in $venvRoot"
Write-Host "Start: start-open-computer.cmd"
Write-Host "MCP config: .venv\Scripts\python.exe -m opencomputer.setup config"
Write-Host "Rerun this installer after updating the repository to upgrade the local installation."
