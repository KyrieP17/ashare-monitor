$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$bindAddress = "127.0.0.1"
$port = 8501

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project environment is missing. Create .venv and install requirements.txt first."
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($null -ne $listener) {
    throw "Port $port is already in use. Stop the existing listener explicitly; this launcher will not kill Python processes or drift to another port."
}

$env:PYTHONNOUSERSITE = "1"
$executable = (& $python -c "import sys; print(sys.executable)").Trim()
$appUrl = "http://${bindAddress}:${port}"
Write-Host "Python executable: $executable"
Write-Host "Application URL: $appUrl"

Push-Location $projectRoot
try {
    & $python -m streamlit run app.py @args --server.address $bindAddress --server.port $port
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
