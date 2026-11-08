[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$RuntimeArgs)
$ErrorActionPreference = 'Stop'
$runtime = Join-Path $PSScriptRoot '../runtime/OpenComputer/OpenComputerCLI.exe'
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) {
    throw 'The managed portable runtime is missing; reinstall the complete skill.'
}
if (-not $RuntimeArgs -or $RuntimeArgs.Count -eq 0) { $RuntimeArgs = @('--help') }
& $runtime @RuntimeArgs
exit $LASTEXITCODE
