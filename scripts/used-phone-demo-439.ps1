[CmdletBinding()]
param(
    [ValidateSet("start", "health", "stop")]
    [string]$Action = "start",
    [int]$AgentPort = 18000,
    [ValidateSet("fixed_v1", "react_v0_shadow", "react_v1")]
    [string]$ControlRuntime = "fixed_v1",
    [string]$TraceDebugKey = "",
    [switch]$Knowledge
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\used-phone-demo-439"
$StateFile = Join-Path $RuntimeDir "state.json"
$AgentOut = Join-Path $RuntimeDir "agent.stdout.log"
$AgentErr = Join-Path $RuntimeDir "agent.stderr.log"
$BackendPort = 18083
$ElasticsearchPort = 19281
$ExpectedIndex = "used-phone-demo-439-products-v1"
$PriceDir = Join-Path $ProjectRoot "datasets\current\used-phone"

$RequiredContainers = @(
    @{ Name = "agent-used-phone-benchmark-mysql-stage3-20260811"; Image = "mysql:8.4" },
    @{ Name = "agent-used-phone-benchmark-redis-stage3-20260811"; Image = "redis:7-alpine" },
    @{ Name = "agent-used-phone-demo-session-redis-v1"; Image = "redis:7-alpine" },
    @{ Name = "agent-used-phone-demo-elasticsearch-v1"; Image = "docker.elastic.co/elasticsearch/elasticsearch:8.17.6" },
    @{ Name = "agent-used-phone-demo-search-backend-439-v1"; Image = "agent-used-phone-demo-search-backend:439-v1" }
)

function Fail([string]$Class, [string]$Message) {
    throw "${Class}: $Message"
}

function Invoke-Docker([string[]]$Arguments) {
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "DOCKER" ("docker " + ($Arguments -join " ") + " failed: " + ($output -join " "))
    }
    return $output
}

function Get-Container([string]$Name) {
    $raw = Invoke-Docker @("inspect", $Name)
    return ($raw -join "`n" | ConvertFrom-Json)[0]
}

function Ensure-Container([string]$Name, [string]$Image) {
    $container = Get-Container $Name
    if ($container.Name -ne "/$Name" -or $container.Config.Image -ne $Image) {
        Fail "IDENTITY" "unexpected container identity for $Name"
    }
    if (-not $container.State.Running) {
        Invoke-Docker @("start", $Name) | Out-Null
    }
}

function Wait-Http([string]$Url, [int]$Seconds = 60) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        try { return Invoke-RestMethod -TimeoutSec 3 -Uri $Url }
        catch { Start-Sleep -Milliseconds 500 }
    } while ([DateTime]::UtcNow -lt $deadline)
    Fail "HTTP" "timeout waiting for $Url"
}

function Get-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    return Get-Content -Raw -Encoding utf8 -LiteralPath $StateFile | ConvertFrom-Json
}

function Save-State($State) {
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $State | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath $StateFile
}

function Assert-BackendIdentity {
    $backend = Get-Container "agent-used-phone-demo-search-backend-439-v1"
    $binding = $backend.HostConfig.PortBindings."8080/tcp"
    $env = @{} 
    foreach ($entry in $backend.Config.Env) {
        $parts = $entry -split "=", 2
        if ($parts.Count -eq 2) { $env[$parts[0]] = $parts[1] }
    }
    if (
        $null -eq $binding -or
        $binding[0].HostIp -ne "127.0.0.1" -or
        [int]$binding[0].HostPort -ne $BackendPort -or
        $env.DB_NAME -ne "used_phone_runtime_439_v1_09807c7" -or
        $env.SEARCH_INDEX_PREFIX -ne "used-phone-demo-439"
    ) {
        Fail "IDENTITY" "439 backend binding or data identity mismatch"
    }
}

function Invoke-Health {
    $state = Get-State
    $agent = Wait-Http "http://127.0.0.1:${AgentPort}/health" 10
    $page = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:${AgentPort}/"
    $backend = Wait-Http "http://127.0.0.1:${BackendPort}/actuator/health/readiness" 10
    $catalog = Invoke-RestMethod -TimeoutSec 15 -Uri "http://127.0.0.1:${BackendPort}/api/products?category=%E6%89%8B%E6%9C%BA&limit=1500"
    $indices = Invoke-RestMethod -TimeoutSec 10 -Uri "http://127.0.0.1:${ElasticsearchPort}/_cat/indices/${ExpectedIndex}?format=json"
    $retrieval = Invoke-RestMethod -TimeoutSec 15 -Uri "http://127.0.0.1:${BackendPort}/api/products/retrieval?query=%E6%8B%8D%E7%85%A7&category=%E6%89%8B%E6%9C%BA&limit=5"
    $hasExpandedId = @($retrieval.data.products | Where-Object { [Int64]$_.id -gt [Int64][int]::MaxValue }).Count -gt 0
    if (
        [string]::IsNullOrWhiteSpace([string]$agent.status) -or
        $page.StatusCode -ne 200 -or
        $backend.status -ne "UP" -or
        @($catalog.data).Count -ne 439 -or
        @($indices).Count -ne 1 -or
        [int]$indices[0]."docs.count" -ne 439 -or
        $retrieval.data.channel -ne "elasticsearch" -or
        -not $hasExpandedId
    ) {
        Fail "HEALTH" "439 end-to-end identity check failed"
    }
    return [ordered]@{
        status = "ok"
        url = "http://127.0.0.1:${AgentPort}/"
        catalogProducts = 439
        elasticsearchDocuments = 439
        retrievalChannel = "elasticsearch"
        expandedProductObserved = $true
        syntheticPrices = "439_deterministic_non_market"
        productKnowledgeEnabled = if ($null -ne $state) { [bool]$state.productKnowledgeEnabled } else { $false }
        controlRuntime = if ($null -ne $state -and -not [string]::IsNullOrWhiteSpace([string]$state.controlRuntime)) { [string]$state.controlRuntime } else { "fixed_v1" }
        traceDebugEnabled = if ($null -ne $state) { [bool]$state.traceDebugEnabled } else { $false }
    }
}

function Start-Demo {
    Assert-BackendIdentity
    foreach ($required in $RequiredContainers) {
        Ensure-Container $required.Name $required.Image
    }
    Wait-Http "http://127.0.0.1:${ElasticsearchPort}/_cluster/health?wait_for_status=yellow&timeout=1s" 90 | Out-Null
    Wait-Http "http://127.0.0.1:${BackendPort}/actuator/health/readiness" 90 | Out-Null

    $state = Get-State
    if (Get-NetTCPConnection -State Listen -LocalPort $AgentPort -ErrorAction SilentlyContinue) {
        if ($null -eq $state) { Fail "PORT" "Agent port is occupied by an unowned process" }
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($null -eq $process) { Fail "PORT" "stale 439 state and occupied Agent port" }
        $runningRuntime = if ([string]::IsNullOrWhiteSpace([string]$state.controlRuntime)) { "fixed_v1" } else { [string]$state.controlRuntime }
        if ($runningRuntime -ne $ControlRuntime) {
            Fail "IDENTITY" "running Agent control runtime is $runningRuntime, requested $ControlRuntime"
        }
        if ([bool]$state.productKnowledgeEnabled -ne [bool]$Knowledge) {
            Fail "IDENTITY" "running Agent knowledge flag differs; stop the owned demo before switching modes"
        }
        return Invoke-Health
    }

    if ($ControlRuntime -ne "fixed_v1" -and [string]::IsNullOrWhiteSpace($TraceDebugKey)) {
        Fail "CONFIG" "$ControlRuntime requires -TraceDebugKey for gated decision evidence"
    }

    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $python = if ($Knowledge) { Join-Path $ProjectRoot ".venv\Scripts\python.exe" } else { (Get-Command python -ErrorAction Stop).Source }
    if ($Knowledge) {
        & $python -B -m agent.app.product_knowledge.health
        if ($LASTEXITCODE -ne 0) { Fail "KNOWLEDGE" "start the shared product_knowledge.server on port 18791 first" }
    }
    $environment = @{
        PYTHONPATH = $ProjectRoot
        BACKEND_BASE_URL = "http://127.0.0.1:$BackendPort"
        REDIS_URL = "redis://127.0.0.1:16380/0"
        ECOMMERCE_GUIDE_ENABLED = "true"
        PRODUCT_RETRIEVAL_MODE = "hybrid"
        PRODUCT_VECTOR_BACKEND = "local"
        PRODUCT_VECTOR_TIMEOUT_SECONDS = "30"
        RAG_MODEL_CACHE_DIR = (Join-Path $ProjectRoot "agent\.cache\fastembed")
        PRODUCT_TITLE_RERANKER_ENABLED = "false"
        PRODUCT_KNOWLEDGE_ENABLED = if ($Knowledge) { "true" } else { "false" }
        PRODUCT_KNOWLEDGE_MCP_URL = "http://127.0.0.1:18791/mcp"
        PRODUCT_KNOWLEDGE_TIMEOUT_SECONDS = "10"
        USED_PHONE_SYNTHETIC_PRICE_POLICY = "budget_and_ranking"
        USED_PHONE_SYNTHETIC_PRICE_DIR = $PriceDir
        WEB_QUERY_INTAKE_ENABLED = "true"
        WEB_QUERY_INTAKE_PATH = (Join-Path $RuntimeDir "web-query-intake.sqlite3")
        WEB_QUERY_INTAKE_RETENTION_DAYS = "90"
        AGENT_TRANSACTION_ENABLED = "false"
        AGENT_ORCHESTRATOR_MODE = "unified"
        AGENT_CONTROL_RUNTIME = $ControlRuntime
        AGENT_REACT_LIVE_ENABLED = if ($ControlRuntime -eq "react_v1") { "true" } else { "false" }
        AGENT_TRACE_DEBUG_ENABLED = if ([string]::IsNullOrWhiteSpace($TraceDebugKey)) { "false" } else { "true" }
        AGENT_TRACE_DEBUG_KEY = $TraceDebugKey
        AGENT_LEGACY_FALLBACK_ENABLED = "false"
    }
    if ($Knowledge) {
        $environment["SHOPPING_STATE_AUTHORITY"] = "v2"
        $environment["AGENT_FINAL_ANSWER_THINKING"] = "disabled"
        $environment["AGENT_FINAL_ANSWER_MAX_TOKENS"] = "800"
        $environment["MODEL_CALL_RECEIPTS_ENABLED"] = "true"
    }
    $old = @{}
    foreach ($key in $environment.Keys) {
        $old[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $environment[$key], "Process")
    }
    try {
        $process = Start-Process -FilePath $python -ArgumentList @(
            "-m", "uvicorn", "agent.app.main:app", "--host", "127.0.0.1", "--port", "$AgentPort"
        ) -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $AgentOut -RedirectStandardError $AgentErr -PassThru
    } finally {
        foreach ($key in $environment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $old[$key], "Process")
        }
    }
    Save-State ([ordered]@{
        pid = $process.Id
        port = $AgentPort
        backendPort = $BackendPort
        catalogCount = 439
        priceDir = $PriceDir
        controlRuntime = $ControlRuntime
        productKnowledgeEnabled = [bool]$Knowledge
        traceDebugEnabled = -not [string]::IsNullOrWhiteSpace($TraceDebugKey)
        startedAtUtc = $process.StartTime.ToUniversalTime().ToString("o")
    })
    Wait-Http "http://127.0.0.1:${AgentPort}/health" 60 | Out-Null
    return Invoke-Health
}

function Stop-Demo {
    $state = Get-State
    if ($null -ne $state) {
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            $command = (Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)").CommandLine
            if ($command -notmatch "uvicorn" -or $command -notmatch "--port\s+$AgentPort") {
                Fail "IDENTITY" "refusing to stop a process that is not the owned 439 Agent"
            }
            Stop-Process -Id ([int]$state.pid)
        }
        $resolvedState = [System.IO.Path]::GetFullPath($StateFile)
        $resolvedRuntime = [System.IO.Path]::GetFullPath($RuntimeDir) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $resolvedState.StartsWith($resolvedRuntime, [System.StringComparison]::OrdinalIgnoreCase)) {
            Fail "IDENTITY" "state path escaped the 439 runtime directory"
        }
        Remove-Item -LiteralPath $resolvedState
    }
    return [ordered]@{ status = "stopped"; containersPreserved = $true }
}

try {
    $result = switch ($Action) {
        "start" { Start-Demo }
        "health" { Invoke-Health }
        "stop" { Stop-Demo }
    }
    $result | ConvertTo-Json -Depth 6
    exit 0
} catch {
    [ordered]@{ status = "error"; message = $_.Exception.Message } | ConvertTo-Json -Depth 4
    exit 2
}
