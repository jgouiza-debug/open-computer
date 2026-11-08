[CmdletBinding()]
param(
    [string]$PythonPath = "python",
    [switch]$ValidateOnly
)
$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$support = Join-Path $repoRoot "packaging/windows/build_support.py"
function Invoke-CheckedPython {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python step failed with exit code $LASTEXITCODE" }
}
Invoke-CheckedPython $PythonPath @("-B", $support, "validate-inputs", "--root", $repoRoot)
if ($ValidateOnly) { return }
if ($env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_OS -ne "Windows") {
    throw "Frozen builds run only in the reviewed Windows GitHub Actions workflow. Use -ValidateOnly locally."
}
if ((& $PythonPath -P -c "import sys; print(sys.version.split()[0])") -ne "3.14.10") {
    throw "The portable build requires exact CPython 3.14.10"
}
$runKey = "$($env:GITHUB_RUN_ID)-$($env:GITHUB_RUN_ATTEMPT)"
if ($runKey -notmatch '^\d+-\d+$') { throw "Invalid isolated build identity" }
$neutralBase = "$($env:SystemDrive)\OpenComputerBuild"
$buildRoot = [IO.Path]::GetFullPath((Join-Path $neutralBase $runKey))
if (-not $buildRoot.StartsWith(([IO.Path]::GetFullPath($neutralBase) + "\"), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Build directory escaped its dedicated root"
}
if (Test-Path -LiteralPath $buildRoot) { throw "Dedicated build directory already exists; refusing to replace it" }
$artifactDir = Join-Path $env:RUNNER_TEMP "OpenComputerPortable\$runKey"
if (Test-Path -LiteralPath $artifactDir) { throw "Artifact directory already exists" }
New-Item -ItemType Directory -Path $buildRoot, $artifactDir | Out-Null
$sourceRoot = Join-Path $buildRoot "source"
$contextPath = Join-Path $buildRoot "build-context.json"
$sourceCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot identify source commit" }
$sourceChanges = & git -C $repoRoot status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $sourceChanges) { throw "Build requires a clean committed checkout" }
$nativeTree = (& git -C $repoRoot rev-parse HEAD:native).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot identify reused native source tree" }
$context = @{
    source_commit = $sourceCommit; source_clean = $true; native_tree = $nativeTree
    native_cargo_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $repoRoot "native/Cargo.lock") -Algorithm SHA256).Hash.ToLowerInvariant()
    runner_image = "$($env:ImageOS)/$($env:ImageVersion)"
    windows = [Environment]::OSVersion.Version.ToString()
    private_scan_roots = @($repoRoot, $env:USERPROFILE)
    actions = @{
        checkout = "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
        setup_python = "ece7cb06caefa5fff74198d8649806c4678c61a1"
        upload_artifact = "ea165f8d65b6e75b540449e92b4886f43607fa02"
    }
}
$context | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $contextPath -Encoding utf8NoBOM
Invoke-CheckedPython $PythonPath @("-B", $support, "stage", "--root", $repoRoot, "--destination", $sourceRoot)
$env:PYTHONPATH = $null
$env:PYTHONHOME = $null
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
$venv = Join-Path $buildRoot "venv"
Invoke-CheckedPython $PythonPath @("-P", "-m", "venv", $venv)
$buildPython = Join-Path $venv "Scripts/python.exe"
$lock = Join-Path $sourceRoot "packaging/windows/requirements-build.lock"
$wheels = Join-Path $artifactDir "private-wheels"
New-Item -ItemType Directory -Path $wheels | Out-Null
Invoke-CheckedPython $buildPython @("-P", "-m", "pip", "download", "--only-binary=:all:", "--require-hashes", "--dest", $wheels, "-r", $lock)
Invoke-CheckedPython $buildPython @("-P", "-m", "pip", "install", "--no-index", "--find-links", $wheels, "--require-hashes", "-r", $lock)
$native = Join-Path $artifactDir "private-native-v0.1.3.zip"
$nativeUrl = "https://github.com/jgouiza-debug/open-computer/releases/download/v0.1.3/opencomputer-native-windows-x64-v0.1.3.zip"
Invoke-WebRequest -Uri $nativeUrl -OutFile $native
if ((Get-FileHash -LiteralPath $native -Algorithm SHA256).Hash.ToLowerInvariant() -ne "7d101ac98ab1a2c1cba0ed8a212454e7d39d5e882a49f42c858b4a43eddcd869") {
    throw "Published native archive hash differs"
}
$env:OC_PORTABLE_TOC = Join-Path $buildRoot "private-collect-toc.json"
$dist = Join-Path $buildRoot "dist"
Push-Location $sourceRoot
try {
    Invoke-CheckedPython $buildPython @("-P", "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $dist, "--workpath", (Join-Path $buildRoot "work"), (Join-Path $sourceRoot "packaging/windows/OpenComputer.spec"))
} finally { Pop-Location }
$bundle = Join-Path $dist "OpenComputer"
$public = Join-Path $artifactDir "candidate"
Invoke-CheckedPython $buildPython @("-B", (Join-Path $sourceRoot "packaging/windows/build_support.py"), "finalize", "--root", $sourceRoot, "--bundle", $bundle, "--toc", $env:OC_PORTABLE_TOC, "--wheels", $wheels, "--context", $contextPath, "--native-zip", $native, "--output", $public)

# Small offline frozen-CLI gate. Full SDK/GUI/helper protocol validation is a
# separate reviewed step; this gate makes no model, native-helper or GUI calls.
$cli = Join-Path $bundle "OpenComputerCLI.exe"
Push-Location $env:RUNNER_TEMP
$savedPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    & $cli --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Frozen CLI root help failed" }
    & $cli setup doctor --offline | Set-Content -LiteralPath (Join-Path $artifactDir "private-doctor.json") -Encoding utf8NoBOM
    if ($LASTEXITCODE -ne 0) { throw "Frozen offline doctor failed" }
    $configText = (& $cli setup config) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "Frozen config generation failed" }
    $config = $configText | ConvertFrom-Json
    $server = $config.mcpServers.'open-computer'
    if ($server.command -ne $cli -or @($server.args).Count -ne 1 -or $server.args[0] -ne "mcp" -or @($server.env.PSObject.Properties).Count -ne 0) {
        throw "Frozen config violated sibling CLI/args/env contract"
    }
    @{schema_version=1; offline_doctor_passed=$true; sibling_config_passed=$true; unrelated_cwd=$true; restricted_path=$true; model_calls=0; native_calls=0; gui_calls=0; sdk_probe="not performed by this build gate"} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $public "portable-cli-receipt.json") -Encoding utf8NoBOM
} finally { $env:PATH = $savedPath; Pop-Location }
Copy-Item -LiteralPath (Join-Path $bundle "build-manifest.json") -Destination (Join-Path $public "build-manifest.json")
if ($env:GITHUB_OUTPUT) {
    "artifact-dir=$public" | Add-Content -LiteralPath $env:GITHUB_OUTPUT
    "bundle-dir=$bundle" | Add-Content -LiteralPath $env:GITHUB_OUTPUT
}
Write-Host "Portable candidate created. Publication and clean-Windows validation remain separate."
