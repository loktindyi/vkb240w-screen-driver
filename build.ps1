$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found. Install uv and make sure it is on PATH."
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    & uv venv --python 3.13 .venv
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

& uv pip install --python $python -r requirements.txt "Nuitka[onefile]"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $python -m nuitka `
    --onefile `
    --windows-console-mode=disable `
    --include-package-data=imageio_ffmpeg `
    --output-dir=dist `
    --output-filename=vk_b240w_no_console.exe `
    vk_b240w.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Build complete: $(Join-Path $PSScriptRoot 'dist\vk_b240w_no_console.exe')"
