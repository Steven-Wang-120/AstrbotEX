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

$hostName = if ($env:ASTRBOTEX_HOST) { $env:ASTRBOTEX_HOST } else { "127.0.0.1" }
$port = if ($env:ASTRBOTEX_PORT) { $env:ASTRBOTEX_PORT } else { "8765" }
$tickHz = if ($env:ASTRBOTEX_TICK_HZ) { $env:ASTRBOTEX_TICK_HZ } else { "5" }

& $python.Source -m astrbot_ex.core.api_server --host $hostName --port $port --tick-hz $tickHz
