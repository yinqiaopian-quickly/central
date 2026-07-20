param(
    [Parameter(Mandatory = $true)]
    [string]$Name
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $PSScriptRoot
$targetsPath = Join-Path $PSScriptRoot "open_targets.json"

if (-not (Test-Path -LiteralPath $targetsPath)) {
    throw "Missing target config: $targetsPath"
}

$targets = Get-Content -LiteralPath $targetsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$targetValue = $targets.$Name

if (-not $targetValue) {
    throw "Target is not allowed: $Name"
}

if ([System.IO.Path]::IsPathRooted($targetValue)) {
    $targetPath = $targetValue
} else {
    $targetPath = Join-Path $baseDir $targetValue
}

$resolvedPath = [System.IO.Path]::GetFullPath($targetPath)

if (-not (Test-Path -LiteralPath $resolvedPath)) {
    throw "Target does not exist: $resolvedPath"
}

Start-Process -FilePath $resolvedPath
Write-Output "Opened: $resolvedPath"

