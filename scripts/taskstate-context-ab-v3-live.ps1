[CmdletBinding()]
param(
    [ValidateSet("start", "health", "stop")]
    [string]$Action = "start",
    [int]$Port = 18001,
    [string]$TraceDebugKey = "taskstate-context-ab-v3-20260827"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\taskstate-context-ab-v3"
$StateFile = Join-Path $RuntimeDir "treatment-process.json"
$Stdout = Join-Path $RuntimeDir "treatment.stdout.log"
$Stderr = Join-Path $RuntimeDir "treatment.stderr.log"
$Module = "agent.evaluation.shopping_task_state_context_ab_v3_server:app"

function Wait-Http([string]$Url, [int]$Seconds = 60) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        try { return Invoke-RestMethod -TimeoutSec 3 -Uri $Url }
        catch { Start-Sleep -Milliseconds 500 }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "timeout waiting for $Url"
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    return Get-Content -Raw -Encoding utf8 -LiteralPath $StateFile | ConvertFrom-Json
}

function Invoke-Health {
    $health = Wait-Http "http://127.0.0.1:${Port}/health" 10
    $status = Invoke-RestMethod -TimeoutSec 5 `
        -Uri "http://127.0.0.1:${Port}/internal/evaluation/taskstate-context-ab-v3" `
        -Headers @{ "X-Agent-Debug-Key" = $TraceDebugKey }
    if ($status.runtime -ne "fixed_v1") { throw "unexpected runtime: $($status.runtime)" }
    return [ordered]@{
        status = $health.status
        experimentId = $status.experimentId
        runtime = $status.runtime
        projectionCount = $status.projectionCount
        projectionFailures = $status.projectionFailures
        productionContractRevision = $status.productionContractRevision
    }
}

function Start-Treatment {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        $state = Read-State
        if ($null -eq $state) { throw "treatment port is occupied by an unowned process" }
        return Invoke-Health
    }
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $environment = @{
        PYTHONPATH = $ProjectRoot
        BACKEND_BASE_URL = "http://127.0.0.1:18083"
        REDIS_URL = "redis://127.0.0.1:16380/0"
        ECOMMERCE_GUIDE_ENABLED = "true"
        PRODUCT_RETRIEVAL_MODE = "hybrid"
        PRODUCT_VECTOR_BACKEND = "local"
        PRODUCT_VECTOR_TIMEOUT_SECONDS = "30"
        PRODUCT_TITLE_RERANKER_ENABLED = "false"
        USED_PHONE_SYNTHETIC_PRICE_POLICY = "budget_and_ranking"
        USED_PHONE_SYNTHETIC_PRICE_DIR = (Join-Path $ProjectRoot "data\derived\ecommerce\used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3")
        WEB_QUERY_INTAKE_ENABLED = "true"
        WEB_QUERY_INTAKE_PATH = (Join-Path $RuntimeDir "web-query-intake.sqlite3")
        WEB_QUERY_INTAKE_RETENTION_DAYS = "90"
        AGENT_TRANSACTION_ENABLED = "false"
        AGENT_ORCHESTRATOR_MODE = "unified"
        AGENT_CONTROL_RUNTIME = "fixed_v1"
        AGENT_TRACE_DEBUG_ENABLED = "true"
        AGENT_TRACE_DEBUG_KEY = $TraceDebugKey
        AGENT_LEGACY_FALLBACK_ENABLED = "false"
    }
    $old = @{}
    foreach ($key in $environment.Keys) {
        $old[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $environment[$key], "Process")
    }
    try {
        $python = (Get-Command python -ErrorAction Stop).Source
        $process = Start-Process -FilePath $python -ArgumentList @(
            "-m", "uvicorn", $Module, "--host", "127.0.0.1", "--port", "$Port"
        ) -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
            -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -PassThru
    } finally {
        foreach ($key in $environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $old[$key], "Process")
        }
    }
    [ordered]@{
        startedAt = $process.StartTime.ToUniversalTime().ToString("o")
        pid = $process.Id
        port = $Port
        command = "python -m uvicorn $Module"
    } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    return Invoke-Health
}

function Stop-Treatment {
    $state = Read-State
    if ($null -ne $state) {
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            $command = (Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)").CommandLine
            if ($command -notmatch [regex]::Escape($Module) -or $command -notmatch "--port\s+$Port") {
                throw "refusing to stop a process that does not match the v3 treatment identity"
            }
            Stop-Process -Id ([int]$state.pid)
        }
        Remove-Item -LiteralPath $StateFile
    }
    return [ordered]@{ status = "stopped"; dependenciesPreserved = $true }
}

$result = switch ($Action) {
    "start" { Start-Treatment }
    "health" { Invoke-Health }
    "stop" { Stop-Treatment }
}
$result | ConvertTo-Json -Depth 5
