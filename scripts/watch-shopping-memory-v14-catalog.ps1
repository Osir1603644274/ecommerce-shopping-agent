param(
    [Guid]$BitsJobId = [Guid]"a9a086b7-a302-48a9-9be2-6bc9c7db0cb9",
    [string]$WorkspaceRoot = "F:\agent",
    [string]$CatalogPath = "D:\agent-datasets\shopping-companion\9a8a2a1c13f0d88de070238352bcf71f98ca851f\products.jsonl",
    [string]$StatusPath = "D:\agent-datasets\shopping-companion\9a8a2a1c13f0d88de070238352bcf71f98ca851f\v14-download-and-audit-status.json",
    [string]$PythonExecutable = "C:\Users\ming\AppData\Local\Programs\Python\Python312\python.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedBytes = 20030340587L
$expectedContractHash = "04ba1ed7193afc8d81967a7c6a139754579913089741a2068bceb65f82f5f640"
$expectedAuditorHash = "2402cfdfd863f28b2926e00babc765ae61443bc8dbd7a0a813cb62b31f6e5be2"
$contractPath = Join-Path $WorkspaceRoot "docs\experiments\shopping-memory-v14-2026-08-30\catalog-structure-contract-v1.1.json"
$auditorPath = Join-Path $WorkspaceRoot "agent\evaluation\audit_shopping_companion_catalog_v14.py"
$catalogValuesPath = Join-Path $WorkspaceRoot "agent\evaluation\assets\shopping_memory_v13_20260830\catalog-values.jsonl"
$trainTargetsPath = Join-Path $WorkspaceRoot "agent\evaluation\assets\shopping_memory_v13_20260830\train-target-catalog.jsonl"
$outputPath = Join-Path $WorkspaceRoot "agent\evaluation\results\shopping_memory_v14_catalog_structure_v1_20260830_attempt001"
$logPath = [IO.Path]::ChangeExtension($StatusPath, ".log")

function Save-V14Status {
    param([hashtable]$Value)
    $Value["observedAtUtc"] = [DateTime]::UtcNow.ToString("o")
    $json = $Value | ConvertTo-Json -Depth 8
    $temporaryPath = "$StatusPath.tmp"
    [IO.File]::WriteAllText($temporaryPath, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $StatusPath -Force
}

try {
    Save-V14Status @{ phase = "DOWNLOAD_MONITOR"; jobId = [string]$BitsJobId; state = "STARTED" }
    while (-not (Test-Path -LiteralPath $CatalogPath)) {
        $taskJob = Get-BitsTransfer -ErrorAction SilentlyContinue | Where-Object JobId -eq $BitsJobId
        if ($null -eq $taskJob) {
            throw "BITS job is absent and the catalog destination does not exist"
        }
        if ($taskJob.JobState -eq "Transferred") {
            Complete-BitsTransfer -BitsJob $taskJob
            break
        }
        if ($taskJob.JobState -in @("Error", "Cancelled", "Acknowledged")) {
            throw "BITS reached terminal state $($taskJob.JobState): $($taskJob.ErrorDescription)"
        }
        Save-V14Status @{
            phase = "DOWNLOAD_MONITOR"
            jobId = [string]$BitsJobId
            state = [string]$taskJob.JobState
            bytesTransferred = [long]$taskJob.BytesTransferred
            bytesTotal = [long]$taskJob.BytesTotal
            error = $taskJob.ErrorDescription
        }
        Start-Sleep -Seconds 30
    }

    $catalogItem = Get-Item -LiteralPath $CatalogPath
    if ($catalogItem.Length -ne $expectedBytes) {
        throw "catalog byte count mismatch: $($catalogItem.Length)"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $contractPath).Hash.ToLowerInvariant() -ne $expectedContractHash) {
        throw "frozen V14 structure contract hash mismatch"
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $auditorPath).Hash.ToLowerInvariant() -ne $expectedAuditorHash) {
        throw "frozen V14 auditor hash mismatch"
    }
    if (Test-Path -LiteralPath $outputPath) {
        throw "refusing to overwrite existing audit output"
    }

    Save-V14Status @{
        phase = "STRUCTURE_AUDIT"
        jobId = [string]$BitsJobId
        state = "RUNNING"
        catalogBytes = [long]$catalogItem.Length
        outputPath = $outputPath
    }
    Push-Location $WorkspaceRoot
    try {
        $auditOutput = & $PythonExecutable $auditorPath `
            --catalog $CatalogPath `
            --catalog-values $catalogValuesPath `
            --train-target-catalog $trainTargetsPath `
            --output-dir $outputPath 2>&1
        $exitCode = $LASTEXITCODE
        [IO.File]::WriteAllLines($logPath, [string[]]$auditOutput, [Text.UTF8Encoding]::new($false))
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "catalog auditor exited with code $exitCode"
    }
    $reportPath = Join-Path $outputPath "report.json"
    $report = Get-Content -Raw -Encoding utf8 -LiteralPath $reportPath | ConvertFrom-Json
    Save-V14Status @{
        phase = "COMPLETE"
        jobId = [string]$BitsJobId
        state = [string]$report.decision
        catalogBytes = [long]$catalogItem.Length
        catalogSha256 = [string]$report.source.sha256
        catalogRows = [long]$report.source.rows
        reportPath = $reportPath
        reportSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $reportPath).Hash.ToLowerInvariant()
    }
} catch {
    Save-V14Status @{
        phase = "FAILED"
        jobId = [string]$BitsJobId
        state = "HOLD"
        error = $_.Exception.Message
    }
    exit 1
}
