$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found at $python. Create .venv and install requirements.txt first."
}

$env:PYTHONNOUSERSITE = "1"
if (-not $env:THESIS_DB_PATH) {
    $env:THESIS_DB_PATH = Join-Path $projectRoot "data\thesis.db"
}

Push-Location $projectRoot
try {
    & $python -m thesis.mcp_server
}
finally {
    Pop-Location
}
