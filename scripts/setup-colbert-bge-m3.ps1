param(
    [ValidateSet('all', 'download', 'smoke')]
    [string]$Mode = 'all'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Runtime = Join-Path $ProjectRoot '.runtime\bge-m3-colbert-v1'
$Packages = Join-Path $Runtime 'site-packages'
$Runner = Join-Path $ProjectRoot 'agent\evaluation\colbert_bge_m3_v1\runner.py'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python is missing: $Python"
}

if ($Mode -in @('all', 'download')) {
    New-Item -ItemType Directory -Path $Packages -Force | Out-Null
    & $Python -m pip install --disable-pip-version-check --no-deps --target $Packages 'FlagEmbedding==1.3.5'
    if ($LASTEXITCODE -ne 0) { throw 'FlagEmbedding installation failed' }
}

& $Python $Runner $Mode
if ($LASTEXITCODE -ne 0) { throw "ColBERT bootstrap failed in mode: $Mode" }
