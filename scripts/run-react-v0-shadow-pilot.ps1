[CmdletBinding()]
param(
    [int]$AgentPort = 18001,
    [string[]]$ScenarioId = @("UPHB-V1-002"),
    [int]$TimeoutSeconds = 120,
    [int]$RequestDeadlineSeconds = 45,
    [ValidateSet("fixed_v1", "react_v0_shadow")]
    [string]$ControlRuntime = "react_v0_shadow",
    [string]$PilotId = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PilotId)) {
    $PilotId = [guid]::NewGuid().ToString("N").Substring(0, 10)
} elseif ($PilotId -notmatch "^[a-z0-9][a-z0-9-]{2,40}$") {
    throw "CONFIG: PilotId must match ^[a-z0-9][a-z0-9-]{2,40}$"
}
$PilotRoot = Join-Path $ProjectRoot ".runtime\react-v0-shadow-pilot-$PilotId"
$RunDir = Join-Path $PilotRoot "run"
$DebugKey = [guid]::NewGuid().ToString("N")
$PriceDir = Join-Path $ProjectRoot "data\derived\ecommerce\used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3"

if (Get-NetTCPConnection -State Listen -LocalPort $AgentPort -ErrorAction SilentlyContinue) {
    throw "PORT: $AgentPort is already occupied"
}
New-Item -ItemType Directory -Force -Path $PilotRoot | Out-Null

$Environment = [ordered]@{
    PYTHONPATH = $ProjectRoot
    BACKEND_BASE_URL = "http://127.0.0.1:18083"
    REDIS_URL = "redis://127.0.0.1:16380/0"
    ECOMMERCE_GUIDE_ENABLED = "true"
    PRODUCT_RETRIEVAL_MODE = "hybrid"
    PRODUCT_VECTOR_BACKEND = "local"
    PRODUCT_VECTOR_TIMEOUT_SECONDS = "30"
    PRODUCT_TITLE_RERANKER_ENABLED = "false"
    USED_PHONE_SYNTHETIC_PRICE_POLICY = "budget_and_ranking"
    USED_PHONE_SYNTHETIC_PRICE_DIR = $PriceDir
    WEB_QUERY_INTAKE_ENABLED = "true"
    WEB_QUERY_INTAKE_PATH = (Join-Path $PilotRoot "web-query-intake.sqlite3")
    AGENT_TRANSACTION_ENABLED = "false"
    AGENT_ORCHESTRATOR_MODE = "unified"
    AGENT_CONTROL_RUNTIME = $ControlRuntime
    AGENT_REACT_DECISION_TIMEOUT_SECONDS = "15"
    AGENT_REQUEST_DEADLINE_SECONDS = "$RequestDeadlineSeconds"
    AGENT_CONTEXT_MODE = "context_pack"
    AGENT_LEGACY_FALLBACK_ENABLED = "false"
    AGENT_GRAPH_V2_ENABLED = "false"
    AGENT_GRAPH_V2_SHADOW_ENABLED = "false"
    AGENT_GRAPH_V2_DURABLE_ENABLED = "false"
    USED_PHONE_FAST_PREVIEW_ENABLED = "false"
    AGENT_TRACE_DEBUG_ENABLED = "true"
    AGENT_TRACE_DEBUG_KEY = $DebugKey
}

$OldEnvironment = @{}
foreach ($Key in $Environment.Keys) {
    $OldEnvironment[$Key] = [Environment]::GetEnvironmentVariable($Key, "Process")
    [Environment]::SetEnvironmentVariable($Key, $Environment[$Key], "Process")
}
try {
    $Python = (Get-Command python -ErrorAction Stop).Source
    $Server = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "agent.app.main:app", "--host", "127.0.0.1",
        "--port", "$AgentPort"
    ) -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $PilotRoot "agent.stdout.log") `
      -RedirectStandardError (Join-Path $PilotRoot "agent.stderr.log") `
      -PassThru
} finally {
    foreach ($Key in $Environment.Keys) {
        [Environment]::SetEnvironmentVariable($Key, $OldEnvironment[$Key], "Process")
    }
}

try {
    $Deadline = [DateTime]::UtcNow.AddSeconds(60)
    $Healthy = $false
    do {
        try {
            $Health = Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$AgentPort/health"
            $Healthy = -not [string]::IsNullOrWhiteSpace([string]$Health.status)
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while (-not $Healthy -and [DateTime]::UtcNow -lt $Deadline)
    if (-not $Healthy) { throw "HTTP: shadow pilot server health timeout" }

    $RunnerArgs = @(
        "agent/evaluation/used_phone_harness_behavior_runner_v1.py",
        "--base-url", "http://127.0.0.1:$AgentPort",
        "--expected-runtime", $ControlRuntime,
        "--output-dir", $RunDir,
        "--debug-key", $DebugKey,
        "--timeout-seconds", "$TimeoutSeconds"
    )
    foreach ($Id in $ScenarioId) {
        $RunnerArgs += @("--scenario-id", $Id)
    }
    & $Python @RunnerArgs
    if ($LASTEXITCODE -ne 0) { throw "RUNNER: shadow runner failed" }

    & $Python "agent/evaluation/used_phone_harness_behavior_scorer_v1.py" `
      "--receipts" (Join-Path $RunDir "receipts.jsonl") `
      "--output" (Join-Path $RunDir "score.json")
    if ($LASTEXITCODE -ne 0) { throw "SCORER: shadow scorer failed" }

    [ordered]@{
        status = "ok"
        pilotRoot = $PilotRoot
        serverPid = $Server.Id
        serverHealth = $Health.status
        score = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $RunDir "score.json") | ConvertFrom-Json
    } | ConvertTo-Json -Depth 10
} finally {
    $Owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($Server.Id)" -ErrorAction SilentlyContinue
    if (
        $null -ne $Owned -and
        $Owned.CommandLine -match "uvicorn" -and
        $Owned.CommandLine -match "--port\s+$AgentPort"
    ) {
        Stop-Process -Id $Server.Id
    }
}
