$ErrorActionPreference = 'Stop'
$taskRoot = 'D:\agent-datasets\search-closure-v1'
$taskPython = 'F:\agent\.venv\Scripts\python.exe'
$executionDir = Join-Path $taskRoot 'test-to-agent-execution'
if (Test-Path -LiteralPath $executionDir) { throw 'Existing execution must be inspected, never blindly repeated' }
New-Item -ItemType Directory -Path $executionDir | Out-Null
$utf8 = [Text.UTF8Encoding]::new($false)
$pidToWait = 17528
$proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidToWait"
if ($proc -and $proc.CommandLine -notmatch 'run_final_test_pool.py') { throw 'Unexpected process identity' }
[IO.File]::WriteAllText((Join-Path $executionDir 'STARTED.json'), (@{status='WAITING_FOR_ACTUAL_TEST_PROCESS_TERMINAL';processId=$pidToWait;preparedSha256='9d53ddcce7cc8d76d93dbb031d31bcd4f6989ca645d477701e52f072858bc6c3'} | ConvertTo-Json -Compress), $utf8)
while (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 10 }
$pool = Join-Path $taskRoot 'test-pool\POOL_COMPLETE.json'
if (!(Test-Path -LiteralPath $pool)) { throw 'Actual test process ended without completed pool; do not start Agent' }
$receipt = Get-Content -LiteralPath $pool -Raw -Encoding utf8 | ConvertFrom-Json
if ($receipt.status -ne 'FINAL_TEST_CANDIDATE_POOL_READY_UNJUDGED' -or $receipt.candidate_count -ne 6400) { throw 'Incomplete pool' }
$prepared = Join-Path $taskRoot 'agent-execution-preparation\agent-pairs-v1\PREPARED.json'
if ((Get-FileHash -LiteralPath $prepared -Algorithm SHA256).Hash.ToLower() -ne '9d53ddcce7cc8d76d93dbb031d31bcd4f6989ca645d477701e52f072858bc6c3') { throw 'Prepared Agent changed' }
Push-Location $PSScriptRoot
try {
    & $taskPython run_agent_pairs.py run --prepared $prepared --prepared-sha256 9d53ddcce7cc8d76d93dbb031d31bcd4f6989ca645d477701e52f072858bc6c3 *> (Join-Path $executionDir 'agent.log')
    if ($LASTEXITCODE -ne 0) { throw "Agent run exited $LASTEXITCODE; preserve consumed slots" }
    $complete = Join-Path $taskRoot 'agent-execution-preparation\agent-pairs-v1\COMPLETE.json'
    if (!(Test-Path -LiteralPath $complete)) { throw 'Missing actual Agent completion' }
    [IO.File]::WriteAllText((Join-Path $executionDir 'COMPLETE.json'), (@{status='TEST_POOL_THEN_AGENT_TERMINAL';agentCompleteSha256=(Get-FileHash -LiteralPath $complete -Algorithm SHA256).Hash.ToLower();poolCompleteSha256=(Get-FileHash -LiteralPath $pool -Algorithm SHA256).Hash.ToLower()} | ConvertTo-Json -Compress), $utf8)
    Write-Output 'TEST_POOL_THEN_AGENT_TERMINAL'
} finally { Pop-Location }
