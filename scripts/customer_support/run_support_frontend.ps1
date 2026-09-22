# Run in the foreground. An occupied 5173 is an error; never stop an existing server.
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:COMMERCE_BFF_URL='http://127.0.0.1:18000'
Push-Location (Join-Path $taskRoot 'frontend')
$taskExitCode=1
try {
    npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
    $taskExitCode=$LASTEXITCODE
}
finally { Pop-Location }
exit $taskExitCode
