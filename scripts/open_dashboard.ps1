$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$dashboard = Join-Path $repoRoot "dashboard\index.html"
$dashboardUri = (New-Object System.Uri($dashboard)).AbsoluteUri

$edge = Get-Command msedge.exe -ErrorAction SilentlyContinue
if ($edge) {
    Start-Process $edge.Source -ArgumentList $dashboardUri
    exit 0
}

Start-Process $dashboardUri
