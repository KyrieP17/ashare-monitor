$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project environment is missing. Create .venv and install requirements.txt first."
}

Push-Location $projectRoot
try {
    & $python -m streamlit run app.py @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
