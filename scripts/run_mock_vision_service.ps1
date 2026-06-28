$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) {
    $fallback = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path -LiteralPath $fallback) {
        $python = [pscustomobject]@{ Source = $fallback }
    }
}

if (-not $python) {
    Write-Error "Python was not found. Install Python 3.11+ or set PATH to a Python interpreter."
}

$hostName = if ($env:ASTRBOTEX_MOCK_VISION_HOST) { $env:ASTRBOTEX_MOCK_VISION_HOST } else { "127.0.0.1" }
$port = if ($env:ASTRBOTEX_MOCK_VISION_PORT) { $env:ASTRBOTEX_MOCK_VISION_PORT } else { "8770" }

& $python.Source .\scripts\mock_vision_service.py --host $hostName --port $port
