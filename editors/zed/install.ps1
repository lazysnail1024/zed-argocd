# Installs only the precompiled Zed adapter; the adapter manages its own native LSP.
param(
    [string]$Version = '0.0.1-alpha.4',
    [string]$AssetsDir = '',
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProgressPreference = 'SilentlyContinue'

$repo = 'lazysnail1024/zed-argocd'
$extension = 'argocd-lsp'
if ($Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$') {
    throw "Invalid adapter version: $Version"
}
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is missing; run this script on Windows' }
$installed = Join-Path $env:LOCALAPPDATA 'Zed\extensions\installed'
$target = Join-Path $installed $extension
$stateDir = Join-Path (Split-Path -Parent $installed) '.alpha-installer'
$previous = Join-Path $stateDir "$extension.previous"
New-Item -ItemType Directory -Force -Path $installed | Out-Null
if ((Test-Path -LiteralPath $stateDir) -and
    ((Get-Item -Force -LiteralPath $stateDir).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Refusing symlinked installer state: $stateDir"
}
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

function Test-PathSafe([string]$path) {
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -Force -LiteralPath $path
        if (-not $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to replace a non-directory or link: $path"
        }
    }
}

function Test-SameTree([string]$left, [string]$right) {
    # -Name returns paths relative to the queried directory. Avoid slicing
    # FullName: Windows can expand an 8.3 parent path to a different spelling.
    $files = @(Get-ChildItem -LiteralPath $left -Recurse -File -Name)
    $otherFiles = @(Get-ChildItem -LiteralPath $right -Recurse -File -Name)
    if ($files.Count -ne $otherFiles.Count) { return $false }
    foreach ($relative in $files) {
        $file = Join-Path $left $relative
        $other = Join-Path $right $relative
        if (-not (Test-Path -LiteralPath $other -PathType Leaf)) { return $false }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $other -Algorithm SHA256).Hash) { return $false }
    }
    return $true
}

Test-PathSafe $target
Test-PathSafe $previous
# Complete recovery if the previous process stopped after moving the old directory.
if (-not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $previous)) {
    Move-Item -LiteralPath $previous -Destination $target
    Write-Host "Recovered previous installation at $target"
    if ($Rollback) { return }
}

$work = Join-Path $stateDir (".$extension.install." + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$rotatedPrevious = $false
$rotatedCurrent = $false
$activated = $false
$keepWork = $false
try {
    $payload = Join-Path $work 'payload'
    New-Item -ItemType Directory -Path $payload | Out-Null
    $staged = Join-Path $payload $extension
    if ($Rollback) {
        if (-not (Test-Path -LiteralPath $previous -PathType Container)) {
            throw "No previous version to roll back to: $previous"
        }
        Copy-Item -LiteralPath $previous -Destination $staged -Recurse
    } else {
        $archiveName = "$($repo.Split('/')[-1])-$Version.zip"
        $archive = Join-Path $work $archiveName
        $checksums = Join-Path $work 'SHA256SUMS'
        if ($AssetsDir) {
            $sourceArchive = Join-Path $AssetsDir $archiveName
            $sourceChecksums = Join-Path $AssetsDir 'SHA256SUMS'
            Copy-Item -LiteralPath $sourceArchive -Destination $archive
            Copy-Item -LiteralPath $sourceChecksums -Destination $checksums
        } else {
            $base = "https://github.com/$repo/releases/download/zed-v$Version"
            Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile $checksums -UseBasicParsing
            Invoke-WebRequest -Uri "$base/$archiveName" -OutFile $archive -UseBasicParsing
        }
        $pattern = '^([0-9a-fA-F]{64})  ' + [regex]::Escape($archiveName) + '$'
        $matching = @(Get-Content -LiteralPath $checksums | Where-Object { $_ -match $pattern })
        if ($matching.Count -ne 1) { throw "Missing, duplicate or malformed checksum for $archiveName" }
        $expected = [regex]::Match($matching[0], $pattern).Groups[1].Value
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
        if ($actual -ne $expected) { throw "SHA-256 verification failed for $archiveName" }

        # Check ZIP paths before extraction, including a malformed ZIP with a valid checksum.
        try {
            Add-Type -AssemblyName System.IO.Compression.ZipFile
        } catch {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
        }
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            if ($zip.Entries.Count -eq 0) { throw 'Empty release ZIP' }
            foreach ($entry in $zip.Entries) {
                $name = $entry.FullName
                if (-not $name.StartsWith("$extension/", [StringComparison]::Ordinal) -or
                    $name.Contains('..') -or $name.Contains('\') -or
                    $name.Contains('//') -or $name.Contains(':')) {
                    throw "Unsafe or unexpected ZIP entry: $name"
                }
            }
        } finally { $zip.Dispose() }
        Expand-Archive -LiteralPath $archive -DestinationPath $payload
        foreach ($name in @('extension.toml', 'extension.wasm', 'MANIFEST.json')) {
            if (-not (Test-Path -LiteralPath (Join-Path $staged $name) -PathType Leaf)) {
                throw "Release is missing $name"
            }
        }
        $manifest = Get-Content -LiteralPath (Join-Path $staged 'MANIFEST.json') -Raw | ConvertFrom-Json
        if ($manifest.extensionId -ne $extension -or $manifest.version -ne $Version) {
            throw 'Release identity or version mismatch'
        }
    }
    if ((Test-Path -LiteralPath $target -PathType Container) -and
        (Test-SameTree $staged $target)) {
        Write-Host "Already installed: $target"
        return
    }

    # All inputs have been checked. Move within the same filesystem and keep one backup.
    if (Test-Path -LiteralPath $previous) {
        Move-Item -LiteralPath $previous -Destination (Join-Path $work 'older')
        $rotatedPrevious = $true
    }
    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $previous
        $rotatedCurrent = $true
    }
    Move-Item -LiteralPath $staged -Destination $target
    $activated = $true
    if ($Rollback) {
        Write-Host "Restored previous $extension installation at $target"
    } else {
        Write-Host "Installed $extension ($Version) at $target"
    }
    Write-Host 'Restart Zed to load the adapter. Roll back: .\install.ps1 -Rollback'
} catch {
    $originalError = $_
    try {
        if ($activated -and (Test-Path -LiteralPath $target)) {
            Move-Item -LiteralPath $target -Destination (Join-Path $work 'failed-new')
        }
        if ($rotatedCurrent -and (Test-Path -LiteralPath $previous)) {
            Move-Item -LiteralPath $previous -Destination $target
        }
        if ($rotatedPrevious -and (Test-Path -LiteralPath (Join-Path $work 'older'))) {
            Move-Item -LiteralPath (Join-Path $work 'older') -Destination $previous
        }
    } catch {
        $keepWork = $true
        Write-Warning "Automatic recovery failed. Preserve $work and restore the previous directory manually: $_"
    }
    throw $originalError
} finally {
    if (-not $keepWork) { Remove-Item -LiteralPath $work -Recurse -Force }
}
