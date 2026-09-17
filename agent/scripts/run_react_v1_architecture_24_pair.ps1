param(
    [ValidateSet('smoke', 'full')]
    [string]$Mode = 'smoke',
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [int]$FixedPort = 18006,
    [int]$ReactPort = 18007,
    [int]$FixedRedisDb = 5,
    [int]$ReactRedisDb = 6
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$agentRoot = Join-Path $repoRoot 'agent'
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputRoot)
$runtimeRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot '.runtime'))
if (-not $resolvedOutput.StartsWith($runtimeRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw 'OutputRoot must be a new directory below F:\agent\.runtime'
}
if (Test-Path -LiteralPath $resolvedOutput) {
    throw "OutputRoot already exists: $resolvedOutput"
}
foreach ($port in @($FixedPort, $ReactPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port already in use: $port"
    }
}
New-Item -ItemType Directory -Path $resolvedOutput | Out-Null

$debugKey = [guid]::NewGuid().ToString('N')
$python = (Get-Command python).Source
$common = @{
    BACKEND_BASE_URL = 'http://127.0.0.1:18083'
    PRODUCT_RETRIEVAL_MODE = 'bm25'
    USED_PHONE_SYNTHETIC_PRICE_POLICY = 'budget_and_ranking'
    USED_PHONE_SYNTHETIC_PRICE_DIR = (
        Join-Path $repoRoot 'data\derived\ecommerce\used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3'
    )
    AGENT_GRAPH_V2_DURABLE_ENABLED = 'true'
    AGENT_TRACE_DEBUG_ENABLED = 'true'
    AGENT_TRACE_DEBUG_KEY = $debugKey
    AGENT_TOOL_TRANSPORT_MODE = 'live'
    AGENT_TRANSACTION_ENABLED = 'false'
    AGENT_CONTEXT_MODE = 'context_pack'
    AGENT_ORCHESTRATOR_MODE = 'unified'
    USED_PHONE_FAST_PREVIEW_ENABLED = 'false'
    EVIDENCE_CRITIC_ENABLED = 'false'
    WEB_QUERY_INTAKE_ENABLED = 'false'
    DEEPSEEK_MODEL = 'deepseek-v4-flash'
    AGENT_REQUEST_DEADLINE_SECONDS = '45'
    AGENT_REACT_DECISION_TIMEOUT_SECONDS = '40'
    AGENT_REACT_FINAL_ANSWER_TIMEOUT_SECONDS = '30'
    AGENT_REACT_V1_MAX_MODEL_DECISIONS = '2'
    PYTHONDONTWRITEBYTECODE = '1'
}
$fixedEnvironment = $common.Clone()
$fixedEnvironment.REDIS_URL = "redis://127.0.0.1:16380/$FixedRedisDb"
$fixedEnvironment.AGENT_CONTROL_RUNTIME = 'fixed_v1'
$fixedEnvironment.AGENT_REACT_LIVE_ENABLED = 'false'
$reactEnvironment = $common.Clone()
$reactEnvironment.REDIS_URL = "redis://127.0.0.1:16380/$ReactRedisDb"
$reactEnvironment.AGENT_CONTROL_RUNTIME = 'react_v1'
$reactEnvironment.AGENT_REACT_LIVE_ENABLED = 'true'

$launchReceiptPath = Join-Path $resolvedOutput 'launch_receipt.json'
$launchReceipt = [ordered]@{
    schemaVersion = 'react-v1-architecture-24-launch-v1'
    mode = $Mode
    createdAt = [DateTimeOffset]::UtcNow.ToString('o')
    pythonExecutable = $python
    commonEnvironment = [ordered]@{
        BACKEND_BASE_URL = $common.BACKEND_BASE_URL
        PRODUCT_RETRIEVAL_MODE = $common.PRODUCT_RETRIEVAL_MODE
        USED_PHONE_SYNTHETIC_PRICE_POLICY = $common.USED_PHONE_SYNTHETIC_PRICE_POLICY
        USED_PHONE_SYNTHETIC_PRICE_DIR = $common.USED_PHONE_SYNTHETIC_PRICE_DIR
        AGENT_GRAPH_V2_DURABLE_ENABLED = $common.AGENT_GRAPH_V2_DURABLE_ENABLED
        AGENT_TRACE_DEBUG_ENABLED = $common.AGENT_TRACE_DEBUG_ENABLED
        AGENT_TOOL_TRANSPORT_MODE = $common.AGENT_TOOL_TRANSPORT_MODE
        AGENT_TRANSACTION_ENABLED = $common.AGENT_TRANSACTION_ENABLED
        AGENT_CONTEXT_MODE = $common.AGENT_CONTEXT_MODE
        AGENT_ORCHESTRATOR_MODE = $common.AGENT_ORCHESTRATOR_MODE
        USED_PHONE_FAST_PREVIEW_ENABLED = $common.USED_PHONE_FAST_PREVIEW_ENABLED
        EVIDENCE_CRITIC_ENABLED = $common.EVIDENCE_CRITIC_ENABLED
        WEB_QUERY_INTAKE_ENABLED = $common.WEB_QUERY_INTAKE_ENABLED
        DEEPSEEK_MODEL = $common.DEEPSEEK_MODEL
        AGENT_REQUEST_DEADLINE_SECONDS = $common.AGENT_REQUEST_DEADLINE_SECONDS
        AGENT_REACT_DECISION_TIMEOUT_SECONDS = $common.AGENT_REACT_DECISION_TIMEOUT_SECONDS
        AGENT_REACT_FINAL_ANSWER_TIMEOUT_SECONDS = $common.AGENT_REACT_FINAL_ANSWER_TIMEOUT_SECONDS
        AGENT_REACT_V1_MAX_MODEL_DECISIONS = $common.AGENT_REACT_V1_MAX_MODEL_DECISIONS
    }
    arms = [ordered]@{
        fixed = [ordered]@{
            baseUrl = "http://127.0.0.1:$FixedPort"
            redisDb = $FixedRedisDb
            controlRuntime = $fixedEnvironment.AGENT_CONTROL_RUNTIME
            reactLiveEnabled = $fixedEnvironment.AGENT_REACT_LIVE_ENABLED
        }
        react = [ordered]@{
            baseUrl = "http://127.0.0.1:$ReactPort"
            redisDb = $ReactRedisDb
            controlRuntime = $reactEnvironment.AGENT_CONTROL_RUNTIME
            reactLiveEnabled = $reactEnvironment.AGENT_REACT_LIVE_ENABLED
        }
    }
    runner = [ordered]@{
        httpTimeoutSeconds = 120
        maxTransitions = 8
        selectedMode = $Mode
    }
    secretsPersisted = $false
}
$launchReceipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $launchReceiptPath -Encoding utf8

$fixedProcess = $null
$reactProcess = $null
try {
    $fixedProcess = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$FixedPort") `
        -WorkingDirectory $agentRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $resolvedOutput 'fixed.stdout.log') `
        -RedirectStandardError (Join-Path $resolvedOutput 'fixed.stderr.log') `
        -Environment $fixedEnvironment
    $reactProcess = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ReactPort") `
        -WorkingDirectory $agentRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $resolvedOutput 'react.stdout.log') `
        -RedirectStandardError (Join-Path $resolvedOutput 'react.stderr.log') `
        -Environment $reactEnvironment

    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            $fixedHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$FixedPort/health" -TimeoutSec 2
            $reactHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$ReactPort/health" -TimeoutSec 2
            if ($fixedHealth.status -and $reactHealth.status) {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw 'Temporary Agent processes did not become ready'
    }

    $env:AGENT_TRACE_DEBUG_KEY = $debugKey
    $arguments = @(
        '-B', 'agent/evaluation/react_v1_architecture_24_runner_v1.py',
        '--fixed-url', "http://127.0.0.1:$FixedPort",
        '--react-url', "http://127.0.0.1:$ReactPort",
        '--output-dir', (Join-Path $resolvedOutput 'paired'),
        '--model-name', 'deepseek-v4-flash',
        '--launch-receipt', $launchReceiptPath,
        '--timeout-seconds', '120'
    )
    if ($Mode -eq 'smoke') {
        $arguments += @(
            '--scenario-id', 'scenario-001',
            '--scenario-id', 'scenario-009',
            '--scenario-id', 'scenario-024'
        )
    }
    & $python @arguments
    $runnerExit = $LASTEXITCODE
    Write-Output "RUNNER_EXIT=$runnerExit"
    $pairedResult = Join-Path $resolvedOutput 'paired\paired_result.json'
    if (Test-Path -LiteralPath $pairedResult) {
        Get-Content -Raw -Encoding utf8 -LiteralPath $pairedResult
    }
    if ($runnerExit -ne 0) {
        if (Test-Path -LiteralPath $pairedResult) {
            throw "Runner produced non-ACCEPT evidence: $runnerExit"
        }
        throw "Runner failed before producing paired_result.json: $runnerExit"
    }
} finally {
    Remove-Item Env:AGENT_TRACE_DEBUG_KEY -ErrorAction SilentlyContinue
    foreach ($process in @($fixedProcess, $reactProcess)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    $ownedPids = @($fixedProcess, $reactProcess) |
        Where-Object { $null -ne $_ } |
        ForEach-Object { $_.Id }
    Write-Output "OWNED_PIDS_STOPPED=$($ownedPids -join ',')"
}
