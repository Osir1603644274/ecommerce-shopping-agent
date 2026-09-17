[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Get-Command python -ErrorAction Stop

Push-Location -LiteralPath $repoRoot
try {
    & $python.Source 'scripts/document_paths.py' --check
    if ($LASTEXITCODE -ne 0) {
        throw "document archive integrity validation failed with exit code $LASTEXITCODE"
    }

    & $python.Source 'scripts/validate_project_authority.py'
    if ($LASTEXITCODE -ne 0) {
        throw "project authority validation failed with exit code $LASTEXITCODE"
    }

    & $python.Source 'scripts/check_markdown_links.py'
    if ($LASTEXITCODE -ne 0) {
        throw "Markdown link validation failed with exit code $LASTEXITCODE"
    }

    Write-Output '{"status":"PASS","scope":"project-governance"}'
}
finally {
    Pop-Location
}
