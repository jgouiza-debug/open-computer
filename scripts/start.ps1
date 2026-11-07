param(
    [switch]$Demo,
    [switch]$NoServer,
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$appArguments = @('-m', 'opencomputer.app', '--port', "$Port")
if ($Demo) { $appArguments += '--demo' }
if ($NoServer) { $appArguments += '--no-server' }
& python @appArguments
exit $LASTEXITCODE
