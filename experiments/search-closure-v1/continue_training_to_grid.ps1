$ErrorActionPreference = 'Stop'
$workspace = 'F:\agent\experiments\search-closure-v1'
$dataRoot = 'D:\agent-datasets\search-closure-v1'
$pythonPath = 'F:\agent\.venv\Scripts\python.exe'
$receiptRoot = Join-Path $dataRoot 'training-to-grid-execution'
if (Test-Path -LiteralPath $receiptRoot) { throw 'Execution directory already exists; inspect the existing execution before any restart.' }
$trainingProcess = Get-CimInstance Win32_Process -Filter 'ProcessId=55044'
if ($null -eq $trainingProcess -or $trainingProcess.CommandLine -notlike '*train_pairwise.py train*' -or $trainingProcess.CommandLine -notlike '*pairwise-lora-v1*') { throw 'Bound training process is not live with the expected command.' }
New-Item -ItemType Directory -Path $receiptRoot | Out-Null
@{ status='WAITING_FOR_BOUND_TRAINING_PROCESS'; pid=55044; creationDate=$trainingProcess.CreationDate; command=$trainingProcess.CommandLine; driverPid=$PID; scriptSha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant(); startedAt=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receiptRoot 'STARTED.json') -Encoding utf8
try {
    while ($null -ne (Get-Process -Id 55044 -ErrorAction SilentlyContinue)) {
        $currentProcess = Get-CimInstance Win32_Process -Filter 'ProcessId=55044'
        if ($null -ne $currentProcess -and $currentProcess.CreationDate -ne $trainingProcess.CreationDate) { throw 'Process ID was reused; refusing automatic continuation.' }
        Wait-Process -Id 55044 -Timeout 45 -ErrorAction SilentlyContinue
    }
    $completion = Join-Path $dataRoot 'training-preparation\runs\pairwise-lora-v1\training-complete.json'
    if (-not (Test-Path -LiteralPath $completion)) { throw 'Training process ended without its completion receipt.' }
    Set-Location -LiteralPath $workspace
    & $pythonPath 'finalize_training_cycle.py' '--gate' (Join-Path $dataRoot 'training-preparation\gates\v6-grid-r2\gate.json') '--gate-sha256' '3efff63cf18ad97a1d9f48ad56702776b1fa68101212069cddefe400bbf19e78' '--grid' (Join-Path $dataRoot 'development-grid-r2') '--training-complete' $completion *> (Join-Path $receiptRoot 'finalize.log')
    if ($LASTEXITCODE -ne 0) { throw "Training-cycle verifier failed with exit code $LASTEXITCODE" }
    @{status='TRAINING_VERIFIED_STARTING_NEW_GRID';at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receiptRoot 'GRID_STARTED.json') -Encoding utf8
    & $pythonPath 'run_dev_grid.py' '--phase' 'all' '--output' (Join-Path $dataRoot 'development-grid-r3') '--new-models' (Join-Path $dataRoot 'training-preparation\new-models.json') '--reuse-grid' (Join-Path $dataRoot 'development-grid-r2') *> (Join-Path $receiptRoot 'grid.log')
    if ($LASTEXITCODE -ne 0) { throw "Development-grid execution failed with exit code $LASTEXITCODE" }
    $gridReceipt = Join-Path $dataRoot 'development-grid-r3\COMPLETE.json'
    if (-not (Test-Path -LiteralPath $gridReceipt)) { throw 'Grid process ended without COMPLETE.json' }
    @{status='TRAINING_AND_NEW_DEVELOPMENT_GRID_PRODUCED';trainingCompleteSha256=(Get-FileHash -LiteralPath $completion -Algorithm SHA256).Hash.ToLowerInvariant();gridCompleteSha256=(Get-FileHash -LiteralPath $gridReceipt -Algorithm SHA256).Hash.ToLowerInvariant();at=[DateTime]::UtcNow.ToString('o');finalSelectionOrTestExecuted=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receiptRoot 'COMPLETE.json') -Encoding utf8
} catch {
    @{status='FAILED_REQUIRES_INSPECTION';message=$_.Exception.Message;at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receiptRoot 'FAILED.json') -Encoding utf8
    throw
}
