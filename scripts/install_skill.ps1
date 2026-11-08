[CmdletBinding(DefaultParameterSetName = 'Online')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Online')][string]$Release,
    [Parameter(Mandatory = $true, ParameterSetName = 'Online')]
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string]$ExpectedArchiveSha256,
    [Parameter(Mandatory = $true, ParameterSetName = 'Archive')][string]$ArchivePath,
    [Parameter(Mandatory = $true, ParameterSetName = 'Portable')][string]$PortablePath,
    [string]$SkillRoot,
    [switch]$Upgrade
)
$ErrorActionPreference = 'Stop'

function Get-FullPath([string]$Path) { return [IO.Path]::GetFullPath($Path) }
function Get-Sha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create(); $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
function Assert-PlainPath([string]$Path) {
    $current = Get-FullPath $Path
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Linked install paths are not supported.'
            }
        }
        $next = [IO.Path]::GetDirectoryName($current)
        if ($next -eq $current) { break }; $current = $next
    }
}
function Get-RelativePath([string]$Value) {
    if (-not $Value -or $Value.Contains('\') -or $Value.Contains(':') -or $Value.StartsWith('/')) {
        throw 'Unsafe archive or manifest path.'
    }
    foreach ($part in $Value.Split('/')) {
        if ($part -in @('', '.', '..') -or $part -match '[. ]$|[\x00-\x1f<>"|?*]' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            throw 'Unsafe archive or manifest path.'
        }
    }
    return $Value
}
function Write-JsonFile([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12) + "`n", (New-Object Text.UTF8Encoding($false)))
}
function Get-Inventory([string]$Root, [string[]]$Exclude = @()) {
    $rootPath = (Get-FullPath $Root).TrimEnd('\') + '\'
    $result = @(); $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push((Get-FullPath $Root)); [long]$total = 0
    while ($pending.Count) {
        foreach ($file in @(Get-ChildItem -LiteralPath $pending.Pop() -Force)) {
            if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked install paths are not supported.' }
            if ($file.PSIsContainer) { $pending.Push($file.FullName); continue }
            $name = $file.FullName.Substring($rootPath.Length).Replace('\', '/')
            if ($name -in $Exclude) { continue }
            $total += $file.Length
            if ($result.Count -ge 20000 -or $total -gt 2GB) { throw 'Inventory exceeds its limit.' }
            $result += @{path=$name; size=$file.Length; sha256=(Get-Sha256 $file.FullName)}
        }
    }
    return $result
}
function Test-Inventory([string]$Root, [string]$ManifestName, [string[]]$Exclude = @()) {
    Assert-PlainPath $Root
    $manifestPath = Join-Path $Root $ManifestName
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 8MB) { throw 'Manifest exceeds its limit.' }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or @($manifest.files).Count -lt 1 -or @($manifest.files).Count -gt 20000) {
        throw 'Unsupported install manifest.'
    }
    $seen = @{}
    foreach ($file in $manifest.files) {
        $name = Get-RelativePath ([string]$file.path)
        if ($seen.ContainsKey($name) -or $name -in (@($ManifestName) + $Exclude)) { throw 'Duplicate or excluded manifest entry.' }
        $seen[$name] = $true
        $path = Join-Path $Root $name
        Assert-PlainPath $path
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or $file.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            (Get-Item -LiteralPath $path).Length -ne $file.size -or
            (Get-Sha256 $path) -ne $file.sha256) { throw 'Manifest file differs from its recorded hash or size.' }
    }
    $actual = @(Get-Inventory $Root (@($ManifestName) + $Exclude))
    if ($actual.Count -ne $seen.Count -or @($actual | Where-Object {-not $seen.ContainsKey($_.path)}).Count -ne 0) {
        throw 'Unlisted files or local changes found.'
    }
    return $manifest
}
function Copy-Inventory([string]$Source, [string]$Destination, $Files) {
    foreach ($file in $Files) {
        $name = Get-RelativePath ([string]$file.path)
        $target = Join-Path $Destination $name
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $target
        if ((Get-Sha256 $target) -ne $file.sha256) { throw 'Staged file differs from its recorded hash.' }
    }
}
function Expand-CheckedZip([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -gt 20001) { throw 'Archive has too many entries.' }
        $seen = @{}; [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $name = Get-RelativePath $entry.FullName.TrimEnd('/')
            if ($name -ne 'open-computer' -and -not $name.StartsWith('open-computer/', [StringComparison]::Ordinal)) { throw 'Archive must contain only the open-computer skill folder.' }
            if ($seen.ContainsKey($name)) { throw 'Archive has duplicate paths.' }; $seen[$name] = $true
            $kind = ($entry.ExternalAttributes -shr 16) -band 0xf000
            if ($kind -notin @(0, 0x8000, 0x4000) -or ($entry.ExternalAttributes -band 0x400)) { throw 'Archive links are not supported.' }
            $total += $entry.Length
            if ($total -gt 2GB -or $entry.Length -gt 512MB) { throw 'Archive exceeds extraction limits.' }
        }
        foreach ($entry in $zip.Entries) {
            $target = Join-Path $Destination $entry.FullName
            if ($entry.FullName.EndsWith('/')) { [IO.Directory]::CreateDirectory($target) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
            $inputStream = $entry.Open(); $outputStream = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
            try {
                $buffer = New-Object byte[] 65536; [long]$written = 0
                while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $written += $count; if ($written -gt $entry.Length) { throw 'Archive entry size mismatch.' }
                    $outputStream.Write($buffer, 0, $count)
                }
                if ($written -ne $entry.Length) { throw 'Archive entry size mismatch.' }
            }
            finally { $outputStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
}
function Get-ReleaseArchive([string]$Tag, [string]$Destination) {
    if ($Tag -notmatch '^v\d+\.\d+\.\d+$') { throw 'Use an exact release tag.' }
    $version = $Tag.Substring(1)
    $url = "https://github.com/jgouiza-debug/open-computer/releases/download/$Tag/OpenComputer-$version-windows-x64-skill.zip"
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object Net.Http.HttpClient
    $cancel = New-Object Threading.CancellationTokenSource
    $cancel.CancelAfter(60000)
    $response = $null; $stream = $null; $outputStream = $null
    try {
        $response = $client.GetAsync($url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew)
        $buffer = New-Object byte[] 65536; [long]$total = 0
        while (($count = $stream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()) -gt 0) {
            $total += $count; if ($total -gt 128MB) { throw 'Download exceeds its size limit.' }
            $outputStream.Write($buffer, 0, $count)
        }
    } finally {
        if ($outputStream) {$outputStream.Dispose()}; if ($stream) {$stream.Dispose()}; if ($response) {$response.Dispose()}
        $cancel.Dispose(); $client.Dispose()
    }
}
function Assert-NoActiveRuntime([string]$Destination) {
    $prefix = (Get-FullPath $Destination).TrimEnd('\') + '\'
    foreach ($process in @(Get-Process -Name OpenComputerCLI, OpenComputer -ErrorAction SilentlyContinue)) {
        try { $path = $process.Path } catch { throw 'Cannot verify whether this installation is active.' }
        if ($path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Close this installation sessions before upgrading.' }
    }
}
function Test-Runtime([string]$Skill) {
    $exe = Join-Path $Skill 'runtime/OpenComputer/OpenComputerCLI.exe'
    foreach ($arguments in @(@('--help'), @('headless','serve','--help'), @('setup','doctor','--offline'))) {
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $exe; $start.Arguments = $arguments -join ' '
        $start.UseShellExecute = $false; $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process; $process.StartInfo = $start
        $started = $false
        try {
            $started = $process.Start()
            $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(15000)) { $process.Kill(); $process.WaitForExit(2000) | Out-Null; throw 'Portable offline runtime verification timed out.' }
            $stdout.GetAwaiter().GetResult() | Out-Null; $stderr.GetAwaiter().GetResult() | Out-Null
            if ($process.ExitCode -ne 0) { throw 'Portable offline runtime verification failed. Check dependencies or choose a shorter -SkillRoot path.' }
        } finally {
            if ($started -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit(2000) | Out-Null }
            $process.Dispose()
        }
    }
}
function Remove-OwnedStage([string]$Path, [string]$Parent) {
    $full = Get-FullPath $Path
    if ([IO.Path]::GetDirectoryName($full).TrimEnd('\') -ne (Get-FullPath $Parent).TrimEnd('\') -or
        [IO.Path]::GetFileName($full) -notmatch '^(?:\.opencomputer-(stage|backup)-[a-f0-9]{32}|\.oc-[a-f0-9]{8})$') { throw 'Cleanup boundary refused.' }
    Assert-PlainPath $full
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}
function Install-CheckedSkill([string]$Candidate, [string]$Root, [bool]$Replace, [scriptblock]$Verifier = ${function:Test-Runtime}) {
    $manifest = Test-Inventory $Candidate 'install-manifest.json'
    if ($manifest.kind -ne 'opencomputer-skill-v1' -or $manifest.version -notmatch '^\d+\.\d+\.\d+$' -or
        [version]$manifest.version -lt [version]'0.1.6') { throw 'A headless-capable skill release is required.' }
    $runtime = Test-Inventory (Join-Path $Candidate 'runtime/OpenComputer') 'build-manifest.json'
    if ($runtime.app.version -ne $manifest.version -or 'headless-v1' -notin @($runtime.app.capabilities)) { throw 'Headless runtime capability or version is missing.' }
    foreach ($required in @('SKILL.md','scripts/open-computer.ps1','references/hosts.md','runtime/OpenComputer/OpenComputerCLI.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Candidate $required) -PathType Leaf)) { throw 'Skill payload is incomplete.' }
    }
    $rootPath = Get-FullPath $Root; Assert-PlainPath $rootPath
    [IO.Directory]::CreateDirectory($rootPath) | Out-Null
    $destination = Join-Path $rootPath 'open-computer'
    if (Test-Path -LiteralPath $destination) {
        if (-not $Replace) { throw 'Skill already exists; use explicit -Upgrade for an unchanged managed installation.' }
        $receipt = Join-Path $destination 'install-receipt.json'
        if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) { throw 'Existing skill is unmanaged; move it aside first.' }
        $old = Test-Inventory $destination 'install-manifest.json' @('install-receipt.json')
        if ($old.kind -ne 'opencomputer-skill-v1') { throw 'Existing skill is unmanaged.' }
        Assert-NoActiveRuntime $destination
    }
    # Native imports can fail near MAX_PATH. Never lengthen the runtime path
    # beyond its final "open-computer" directory merely to verify it.
    $stage = Join-Path $rootPath ('.oc-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $backup = Join-Path $rootPath ('.opencomputer-backup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
    try {
        Copy-Inventory $Candidate $stage $manifest.files
        Copy-Item -LiteralPath (Join-Path $Candidate 'install-manifest.json') -Destination $stage
        & $Verifier $stage
        Write-JsonFile (Join-Path $stage 'install-receipt.json') @{kind='opencomputer-skill-v1'; version=$manifest.version; manifest_sha256=(Get-Sha256 (Join-Path $stage 'install-manifest.json')); runtime_manifest_sha256=(Get-Sha256 (Join-Path $stage 'runtime/OpenComputer/build-manifest.json')); offline_checks_passed=$true; model_calls=0; target_observations=0; session_started=$false}
        if (Test-Path -LiteralPath $destination) { [IO.Directory]::Move($destination, $backup) }
        try { [IO.Directory]::Move($stage, $destination) }
        catch { if (Test-Path -LiteralPath $backup) { [IO.Directory]::Move($backup, $destination) }; throw }
        Remove-OwnedStage $backup $rootPath
        return @{installed=$true; version=$manifest.version; skill_path=$destination; session_started=$false; client_configuration_changed=$false}
    } finally { Remove-OwnedStage $stage $rootPath }
}
function New-LocalCandidate([string]$Portable, [string]$Destination) {
    $portableRoot = Get-FullPath $Portable
    $runtime = Test-Inventory $portableRoot 'build-manifest.json'
    if ('headless-v1' -notin @($runtime.app.capabilities)) { throw 'This portable release has no headless capability.' }
    Copy-Inventory $portableRoot (Join-Path $Destination 'runtime/OpenComputer') $runtime.files
    Copy-Item -LiteralPath (Join-Path $portableRoot 'build-manifest.json') -Destination (Join-Path $Destination 'runtime/OpenComputer')
    foreach ($name in @('SKILL.md','scripts/open-computer.ps1','references/hosts.md')) {
        $target = Join-Path $Destination $name; [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $Destination ('runtime/OpenComputer/skill/' + $name)) -Destination $target
    }
    Write-JsonFile (Join-Path $Destination 'install-manifest.json') @{schema_version=1; kind='opencomputer-skill-v1'; version=$runtime.app.version; files=@(Get-Inventory $Destination)}
}

if ($MyInvocation.InvocationName -ne '.') {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne 'X64') { throw 'This release requires Windows x64, not ARM64.' }
    if (-not $SkillRoot) {
        $SkillRoot = if ($env:CODEX_HOME) {Join-Path $env:CODEX_HOME 'skills'} else {Join-Path $env:USERPROFILE '.codex/skills'}
    }
    if ($PSCmdlet.ParameterSetName -ne 'Portable' -and $ExpectedArchiveSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'An expected archive SHA-256 is required.' }
    $tempRoot = Get-FullPath ([IO.Path]::GetTempPath())
    $temporary = Join-Path $tempRoot ('.opencomputer-stage-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temporary) | Out-Null
    try {
        if ($PSCmdlet.ParameterSetName -eq 'Portable') {
            $candidate = Join-Path $temporary 'open-computer'; [IO.Directory]::CreateDirectory($candidate) | Out-Null
            New-LocalCandidate $PortablePath $candidate
        } else {
            if ($PSCmdlet.ParameterSetName -eq 'Online') { $ArchivePath = Join-Path $temporary 'skill.zip'; Get-ReleaseArchive $Release $ArchivePath }
            if ((Get-Item -LiteralPath $ArchivePath).Length -gt 128MB -or (Get-Sha256 $ArchivePath) -ne $ExpectedArchiveSha256) { throw 'Skill archive hash or size differs.' }
            Expand-CheckedZip $ArchivePath $temporary
            $candidate = Join-Path $temporary 'open-computer'
        }
        Install-CheckedSkill $candidate $SkillRoot ([bool]$Upgrade) | ConvertTo-Json -Depth 5
    } finally { Remove-OwnedStage $temporary $tempRoot }
}
