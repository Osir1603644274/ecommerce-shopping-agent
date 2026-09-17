[CmdletBinding()]
param(
    [ValidateSet('start', 'health', 'stop')]
    [string]$Action = 'start'
)
# Compatibility alias for the single merged deployment. Never start the old Docker Agent.
& (Join-Path $PSScriptRoot 'merged-commerce.ps1') -Action $Action
exit $LASTEXITCODE
