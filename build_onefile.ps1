Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distDir = Join-Path $projectRoot "dist"

if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python not found: $venvPython"
}

& $venvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "Meeting Recorder Assistant" `
    --paths (Join-Path $projectRoot "src") `
    (Join-Path $projectRoot "meeting_recorder_gui.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

New-Item -ItemType Directory -Path $distDir -Force | Out-Null

if (Test-Path (Join-Path $projectRoot "config.json")) {
    Copy-Item (Join-Path $projectRoot "config.json") (Join-Path $distDir "config.json") -Force
}

Copy-Item (Join-Path $projectRoot "config.example.json") (Join-Path $distDir "config.example.json") -Force
Copy-Item (Join-Path $projectRoot "README.md") (Join-Path $distDir "README.md") -Force
