param(
    [switch]$Demo,
    [switch]$NoServer,
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    [Console]::Error.WriteLine('Open Computer is not installed here. Run install.cmd first.')
    exit 1
}
Set-Location -LiteralPath $projectDirectory
$appArguments = @('-P', '-m', 'opencomputer.app', '--port', "$Port")
if ($Demo) { $appArguments += '--demo' }
if ($NoServer) { $appArguments += '--no-server' }
& $pythonExecutable @appArguments @args
exit $LASTEXITCODE
