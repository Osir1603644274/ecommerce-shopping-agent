[CmdletBinding()]
param(
    [ValidateSet("start", "audit", "health", "smoke", "stop")]
    [string]$Action = "start",
    [int]$AgentPort = 18000
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $ProjectRoot ".runtime\used-phone-demo"
$StateFile = Join-Path $StateDir "state.json"
$AgentOut = Join-Path $StateDir "agent.stdout.log"
$AgentErr = Join-Path $StateDir "agent.stderr.log"
$Python = (Get-Command python -ErrorAction Stop).Source

$MySqlContainer = "agent-used-phone-benchmark-mysql-stage3-20260811"
$ProductRedisContainer = "agent-used-phone-benchmark-redis-stage3-20260811"
$BackendContainer = "agent-used-phone-benchmark-backend-stage3-attempt2-20260811"
$SessionRedisContainer = "agent-used-phone-demo-session-redis-v1"
$ElasticsearchContainer = "agent-used-phone-demo-elasticsearch-v1"
$SearchBackendContainer = "agent-used-phone-demo-search-backend-v1"
$SessionRedisPort = 16380
$BackendPort = 18081
$ElasticsearchPort = 19281
$SearchBackendPort = 18082
$ElasticsearchVolume = "agent-used-phone-demo-elasticsearch-data-v1"
$BenchmarkNetwork = "agent-used-phone-benchmark-stage3-20260811"
$SearchIndexPrefix = "used-phone-demo"
$SearchBackendImage = "agent-used-phone-demo-search-backend:v1"
$ExpectedDatabase = "used_phone_benchmark_v1_09807c7"
$ExpectedRedisIdentity = "used-phone-benchmark-stage3-20260811:redis-v1:5dc0f693-1a62-4e7a-a386-1a52b99a4afb"
$OwnerLabel = "codex.used-phone-demo.owner=used-phone-demo-v1"
$SyntheticDir = Join-Path $ProjectRoot "data\derived\ecommerce\used_phone_synthetic_reference_price_v1"

function Fail([string]$Class, [string]$Message) {
    throw "${Class}: $Message"
}

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Get-State {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return [ordered]@{ version = 1; startedContainers = @(); agent = $null }
    }
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $StateFile | ConvertFrom-Json
}

function Save-State($State) {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $State | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $StateFile
}

function Invoke-Docker([string[]]$Arguments) {
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "CONFIG" ("docker " + ($Arguments -join " ") + " failed: " + ($output -join " ")) }
    return $output
}

function Get-Container([string]$Name) {
    $raw = Invoke-Docker @("inspect", $Name)
    return ($raw -join "`n" | ConvertFrom-Json)[0]
}

function Assert-ContainerIdentity([string]$Name, [string]$ExpectedImage, [int]$HostPort, [int]$ContainerPort) {
    $container = Get-Container $Name
    if ($container.Name -ne "/$Name") { Fail "CONFIG" "container identity mismatch: $Name" }
    if ($container.Config.Image -ne $ExpectedImage) { Fail "CONFIG" "unexpected image for ${Name}: $($container.Config.Image)" }
    $binding = $container.HostConfig.PortBindings."${ContainerPort}/tcp"
    if ($null -eq $binding -or [int]$binding[0].HostPort -ne $HostPort -or $binding[0].HostIp -ne "127.0.0.1") {
        Fail "PORT" "unexpected loopback binding for $Name"
    }
    return $container
}

function Start-KnownContainer([string]$Name, [string]$Image, [int]$HostPort, [int]$ContainerPort, $State) {
    $container = Assert-ContainerIdentity $Name $Image $HostPort $ContainerPort
    if (-not $container.State.Running) {
        Invoke-Docker @("start", $Name) | Out-Null
        $State.startedContainers = @($State.startedContainers) + @($Name)
        Save-State $State
    }
}

function Ensure-SessionRedis($State) {
    $knownNames = Invoke-Docker @(
        "ps", "-a", "--filter", "name=^/${SessionRedisContainer}$", "--format", "{{.Names}}"
    )
    $exists = $SessionRedisContainer -in @($knownNames)
    if (-not $exists) {
        if (Test-Port $SessionRedisPort) { Fail "PORT" "port $SessionRedisPort is occupied" }
        Invoke-Docker @(
            "create", "--name", $SessionRedisContainer,
            "--label", $OwnerLabel,
            "-p", "127.0.0.1:${SessionRedisPort}:6379",
            "redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no"
        ) | Out-Null
    }
    $container = Assert-ContainerIdentity $SessionRedisContainer "redis:7-alpine" $SessionRedisPort 6379
    if ($container.Config.Labels."codex.used-phone-demo.owner" -ne "used-phone-demo-v1") {
        Fail "CONFIG" "refusing unowned session Redis container"
    }
    if (-not $container.State.Running) {
        Invoke-Docker @("start", $SessionRedisContainer) | Out-Null
        $State.startedContainers = @($State.startedContainers) + @($SessionRedisContainer)
        Save-State $State
    }
}

function Assert-OwnedSearchContainer([string]$Name, [string]$ExpectedImage, [int]$HostPort, [int]$ContainerPort) {
    $container = Get-Container $Name
    if ($container.Name -ne "/$Name") { Fail "CONFIG" "search container identity mismatch: $Name" }
    if ($container.Config.Image -ne $ExpectedImage) { Fail "CONFIG" "unexpected image for ${Name}: $($container.Config.Image)" }
    if ($container.Config.Labels."codex.used-phone-demo.owner" -ne "used-phone-demo-v1") {
        Fail "CONFIG" "refusing unowned search container: $Name"
    }
    $binding = $container.HostConfig.PortBindings."${ContainerPort}/tcp"
    if ($null -eq $binding -or [int]$binding[0].HostPort -ne $HostPort -or $binding[0].HostIp -ne "127.0.0.1") {
        Fail "PORT" "unexpected loopback binding for $Name"
    }
    if ($null -eq $container.NetworkSettings.Networks.$BenchmarkNetwork) {
        Fail "CONFIG" "search container is not attached to the benchmark network: $Name"
    }
    return $container
}

function Ensure-UsedPhoneSearchStack($State) {
    # The immutable benchmark Java container deliberately has SEARCH_ENABLED=false.
    # Keep it untouched and provision a separately-owned Java+ES path for this demo.
    $network = Invoke-Docker @("network", "inspect", $BenchmarkNetwork, "--format", "{{.Name}}")
    if (($network -join "").Trim() -ne $BenchmarkNetwork) { Fail "CONFIG" "benchmark network identity mismatch" }

    $knownNames = Invoke-Docker @("ps", "-a", "--format", "{{.Names}}")
    if ($ElasticsearchContainer -notin @($knownNames)) {
        if (Test-Port $ElasticsearchPort) { Fail "PORT" "port $ElasticsearchPort is occupied" }
        $knownVolumes = Invoke-Docker @("volume", "ls", "--format", "{{.Name}}")
        if ($ElasticsearchVolume -notin @($knownVolumes)) {
            Invoke-Docker @("volume", "create", "--label", $OwnerLabel, $ElasticsearchVolume) | Out-Null
        }
        Invoke-Docker @(
            "create", "--name", $ElasticsearchContainer, "--label", $OwnerLabel,
            "--network", $BenchmarkNetwork, "--network-alias", "elasticsearch",
            "-p", "127.0.0.1:${ElasticsearchPort}:9200",
            "-e", "discovery.type=single-node", "-e", "xpack.security.enabled=false",
            "-e", "ES_JAVA_OPTS=-Xms512m -Xmx512m",
            "-v", "${ElasticsearchVolume}:/usr/share/elasticsearch/data",
            "docker.elastic.co/elasticsearch/elasticsearch:8.17.6"
        ) | Out-Null
    }
    $volume = Invoke-Docker @("volume", "inspect", $ElasticsearchVolume)
    $volumeInfo = ($volume -join "`n" | ConvertFrom-Json)[0]
    if ($volumeInfo.Labels."codex.used-phone-demo.owner" -ne "used-phone-demo-v1") {
        Fail "CONFIG" "refusing unowned Elasticsearch data volume"
    }
    $elastic = Assert-OwnedSearchContainer $ElasticsearchContainer "docker.elastic.co/elasticsearch/elasticsearch:8.17.6" $ElasticsearchPort 9200
    if (-not $elastic.State.Running) {
        Invoke-Docker @("start", $ElasticsearchContainer) | Out-Null
        $State.startedContainers = @($State.startedContainers) + @($ElasticsearchContainer)
        Save-State $State
    }
    Wait-Http "http://127.0.0.1:${ElasticsearchPort}/_cluster/health?wait_for_status=yellow&timeout=1s" "ELASTICSEARCH" 90 | Out-Null

    $searchImageId = & docker image inspect $SearchBackendImage --format "{{.Id}}" 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($searchImageId -join ""))) {
        Invoke-Docker @("build", "--tag", $SearchBackendImage, (Join-Path $ProjectRoot "backend")) | Out-Null
    }
    if ($SearchBackendContainer -notin @($knownNames)) {
        if (Test-Port $SearchBackendPort) { Fail "PORT" "port $SearchBackendPort is occupied" }
        Invoke-Docker @(
            "create", "--name", $SearchBackendContainer, "--label", $OwnerLabel,
            "--network", $BenchmarkNetwork, "-p", "127.0.0.1:${SearchBackendPort}:8080",
            "-e", "SEARCH_ENABLED=true", "-e", "ELASTICSEARCH_URL=http://elasticsearch:9200",
            "-e", "SEARCH_INDEX_PREFIX=$SearchIndexPrefix",
            "-e", "DB_HOST=$MySqlContainer", "-e", "DB_PORT=3306",
            "-e", "DB_NAME=$ExpectedDatabase", "-e", "DB_USER=used_phone_benchmark_v1",
            "-e", "DB_PASSWORD=benchmark_stage3_user_only",
            "-e", "REDIS_HOST=$ProductRedisContainer", "-e", "REDIS_PORT=6379",
            "-e", "JWT_SECRET=benchmark-stage3-isolated-jwt-secret-not-production",
            "-e", "MESSAGING_ENABLED=false", "-e", "FLASH_SALE_ENABLED=false",
            "-e", "RATE_LIMIT_ENABLED=false", "-e", "PAYMENT_SIMULATOR_ENABLED=false",
            $SearchBackendImage
        ) | Out-Null
    }
    $searchBackend = Assert-OwnedSearchContainer $SearchBackendContainer $SearchBackendImage $SearchBackendPort 8080
    if (-not $searchBackend.State.Running) {
        Invoke-Docker @("start", $SearchBackendContainer) | Out-Null
        $State.startedContainers = @($State.startedContainers) + @($SearchBackendContainer)
        Save-State $State
    }
    Wait-Http "http://127.0.0.1:${SearchBackendPort}/actuator/health/readiness" "SEARCH_JAVA" 60 | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        try {
            # Do not probe /retrieval before the scheduled reconciler has created
            # the index: a 404 would open the Java circuit breaker and delay that
            # very reconciliation.  ES index document count is the safe readiness
            # predicate; only then is it valid to exercise the search channel.
            $indices = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${ElasticsearchPort}/_cat/indices/${SearchIndexPrefix}-products-v1?format=json"
            if (@($indices).Count -eq 1 -and [int]$indices[0]."docs.count" -eq 252) { return }
        } catch {}
        Start-Sleep -Milliseconds 750
    } while ([DateTime]::UtcNow -lt $deadline)
    Fail "ELASTICSEARCH" "search-enabled Java did not finish the 252-product Elasticsearch index"
}

function Wait-Http([string]$Url, [string]$Class, [int]$Seconds = 45) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        try { return Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $Url }
        catch { Start-Sleep -Milliseconds 500 }
    } while ([DateTime]::UtcNow -lt $deadline)
    Fail $Class "timed out waiting for $Url"
}

function Read-ModelConfiguration {
    $oldPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $ProjectRoot
        $raw = & $Python -c "import json; from agent.app.settings import settings; print(json.dumps({'configured': bool(settings.deepseek_api_key), 'model': settings.deepseek_model, 'baseUrlHost': settings.deepseek_base_url.split('://')[-1].split('/')[0], 'retrievalMode': settings.product_retrieval_mode, 'vectorBackend': settings.product_vector_backend, 'titleRerankerEnabled': settings.product_title_reranker_enabled, 'syntheticPricePolicy': settings.used_phone_synthetic_price_policy}))"
        if ($LASTEXITCODE -ne 0) { Fail "CONFIG" "cannot load Agent settings" }
        return $raw | ConvertFrom-Json
    } finally { $env:PYTHONPATH = $oldPath }
}

function Invoke-Audit {
    $state = Get-State
    Invoke-Docker @("version", "--format", "{{.Server.Version}}") | Out-Null
    $mysql = Assert-ContainerIdentity $MySqlContainer "mysql:8.4" 13316 3306
    $redis = Assert-ContainerIdentity $ProductRedisContainer "redis:7-alpine" 16379 6379
    $backend = Assert-ContainerIdentity $BackendContainer "agent-used-phone-benchmark-backend-stage3-attempt2:20260811" $BackendPort 8080
    $backendEnv = @{}; foreach ($entry in $backend.Config.Env) { $pair = $entry -split "=", 2; $backendEnv[$pair[0]] = $pair[1] }
    if ($backendEnv.DB_NAME -ne $ExpectedDatabase -or $backendEnv.SEARCH_ENABLED -ne "false") {
        Fail "CONFIG" "backend DB_NAME or SEARCH_ENABLED identity mismatch"
    }
    if ($mysql.State.Running) {
        $mysqlEnv = @{}
        foreach ($entry in (Invoke-Docker @("exec", $MySqlContainer, "env"))) {
            $pair = $entry -split "=", 2
            if ($pair.Count -eq 2) { $mysqlEnv[$pair[0]] = $pair[1] }
        }
        $counts = Invoke-Docker @(
            "exec", "-e", "MYSQL_PWD=$($mysqlEnv.MYSQL_PASSWORD)", $MySqlContainer,
            "mysql", "--batch", "--skip-column-names", "-u$($mysqlEnv.MYSQL_USER)",
            $mysqlEnv.MYSQL_DATABASE, "-e",
            "SELECT CONCAT((SELECT COUNT(*) FROM product),CHAR(9),(SELECT COUNT(*) FROM product_attribute),CHAR(9),(SELECT COUNT(*) FROM catalog_state));"
        )
        if (($counts -join "").Trim() -ne "252\t1547\t1") { Fail "DATA" "MySQL count mismatch" }
    }
    if ($redis.State.Running) {
        $identity = (Invoke-Docker @("exec", $ProductRedisContainer, "redis-cli", "GET", "used-phone-benchmark:instance-identity") -join "").Trim()
        if ($identity -ne $ExpectedRedisIdentity) { Fail "DATA" "product Redis identity mismatch" }
    }
    if ($backend.State.Running) {
        $java = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${BackendPort}/api/products?category=%E6%89%8B%E6%9C%BA&limit=1500"
        if (@($java.data).Count -ne 252) { Fail "JAVA" "Java catalog count mismatch" }
    }
    $oldPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $ProjectRoot
        & $Python -c "from pathlib import Path; from agent.evaluation.used_phone_synthetic_price_v1 import validate_bundle; m,r=validate_bundle(Path(r'$SyntheticDir')); assert len(r)==252; print(m['output']['sha256'])" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "DATA" "synthetic price bundle validation failed" }
    } finally { $env:PYTHONPATH = $oldPath }
    $model = Read-ModelConfiguration
    $recordedSyntheticPolicy = $null
    $recordedRetrievalMode = $null
    $recordedVectorBackend = $null
    if ($null -ne $state.agent -and (Test-Port $AgentPort)) {
        $recordedSyntheticPolicy = $state.agent.syntheticPricePolicy
        $recordedRetrievalMode = $state.agent.retrievalMode
        $recordedVectorBackend = $state.agent.vectorBackend
    }
    [ordered]@{
        status = "ok"; mode = "read_only_audit"; containers = @($MySqlContainer, $ProductRedisContainer, $BackendContainer)
        mysql = if ($mysql.State.Running) { "252/1547/1" } else { "stopped_not_read" }
        productRedis = if ($redis.State.Running) { "identity_verified" } else { "stopped_not_read" }
        java = if ($backend.State.Running) { "252_products" } else { "stopped_not_read" }
        searchEnabled = $backendEnv.SEARCH_ENABLED
        retrievalMode = if ($recordedRetrievalMode) { $recordedRetrievalMode } else { $model.retrievalMode }
        vectorBackend = if ($recordedVectorBackend) { $recordedVectorBackend } else { $model.vectorBackend }
        titleRerankerEnabled = $model.titleRerankerEnabled
        syntheticPricePolicyConfigured = if ($recordedSyntheticPolicy) { $recordedSyntheticPolicy } else { $model.syntheticPricePolicy }
        syntheticPricePolicyEvidence = if ($recordedSyntheticPolicy) { "owned_agent_start_state" } else { "audit_process_settings" }
        modelConfigured = $model.configured; model = $model.model; modelHost = $model.baseUrlHost
    }
}

function Start-Demo {
    $state = Get-State
    Start-KnownContainer $MySqlContainer "mysql:8.4" 13316 3306 $state
    Start-KnownContainer $ProductRedisContainer "redis:7-alpine" 16379 6379 $state
    Ensure-SessionRedis $state
    Start-KnownContainer $BackendContainer "agent-used-phone-benchmark-backend-stage3-attempt2:20260811" $BackendPort 8080 $state
    Wait-Http "http://127.0.0.1:${BackendPort}/actuator/health/readiness" "JAVA" 60 | Out-Null
    Invoke-Audit | Out-Null
    Ensure-UsedPhoneSearchStack $state
    if (Test-Port $AgentPort) {
        if ($null -eq $state.agent -or [int]$state.agent.pid -le 0) { Fail "PORT" "Agent port $AgentPort is occupied by an unowned process" }
        $process = Get-Process -Id ([int]$state.agent.pid) -ErrorAction SilentlyContinue
        if ($null -eq $process) { Fail "PORT" "stale state file and occupied Agent port" }
        Invoke-Health | Out-Null
        return [ordered]@{ status = "already_running"; url = "http://127.0.0.1:$AgentPort/"; pid = $process.Id }
    }

    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    $environment = @{
        PYTHONPATH = $ProjectRoot
        BACKEND_BASE_URL = "http://127.0.0.1:$SearchBackendPort"
        REDIS_URL = "redis://127.0.0.1:$SessionRedisPort/0"
        ECOMMERCE_GUIDE_ENABLED = "true"
        PRODUCT_RETRIEVAL_MODE = "hybrid"
        PRODUCT_VECTOR_BACKEND = "local"
        PRODUCT_VECTOR_TIMEOUT_SECONDS = "30"
        PRODUCT_TITLE_RERANKER_ENABLED = "false"
        USED_PHONE_SYNTHETIC_PRICE_POLICY = "budget_and_ranking"
        USED_PHONE_SYNTHETIC_PRICE_DIR = $SyntheticDir
        WEB_QUERY_INTAKE_ENABLED = "true"
        WEB_QUERY_INTAKE_PATH = (Join-Path $StateDir "web-query-intake.sqlite3")
        WEB_QUERY_INTAKE_RETENTION_DAYS = "90"
        AGENT_TRANSACTION_ENABLED = "false"
        AGENT_ORCHESTRATOR_MODE = "unified"
        AGENT_LEGACY_FALLBACK_ENABLED = "false"
    }
    $old = @{}; foreach ($key in $environment.Keys) { $old[$key] = [Environment]::GetEnvironmentVariable($key, "Process"); [Environment]::SetEnvironmentVariable($key, $environment[$key], "Process") }
    try {
        $process = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "agent.app.main:app", "--host", "127.0.0.1", "--port", "$AgentPort") -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $AgentOut -RedirectStandardError $AgentErr -PassThru
    } finally { foreach ($key in $environment.Keys) { [Environment]::SetEnvironmentVariable($key, $old[$key], "Process") } }
    $state.agent = [ordered]@{
        pid = $process.Id
        port = $AgentPort
        startedAtUtc = $process.StartTime.ToUniversalTime().ToString("o")
        executable = $Python
        syntheticPricePolicy = "budget_and_ranking"
        syntheticPriceDir = $SyntheticDir
        webQueryIntake = "enabled"
        webQueryIntakePath = (Join-Path $StateDir "web-query-intake.sqlite3")
        retrievalMode = "hybrid"
        vectorBackend = "local"
        backendPort = $SearchBackendPort
        elasticsearchPort = $ElasticsearchPort
    }
    Save-State $state
    Wait-Http "http://127.0.0.1:${AgentPort}/health" "CONFIG" 45 | Out-Null
    return [ordered]@{ status = "started"; url = "http://127.0.0.1:$AgentPort/"; pid = $process.Id; syntheticPricePolicy = "budget_and_ranking" }
}

function Invoke-Health {
    $health = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${AgentPort}/health"
    $page = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:${AgentPort}/"
    $java = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${BackendPort}/actuator/health/readiness"
    $searchJava = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${SearchBackendPort}/actuator/health/readiness"
    $elastic = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${ElasticsearchPort}/_cluster/health"
    $search = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:${SearchBackendPort}/api/products/retrieval?query=%E6%8B%8D%E7%85%A7&category=%E6%89%8B%E6%9C%BA&limit=3"
    if ([string]::IsNullOrWhiteSpace([string]$health.status) -or $page.StatusCode -ne 200 -or $java.status -ne "UP" -or $searchJava.status -ne "UP" -or $elastic.status -notin @("green", "yellow") -or $search.data.channel -ne "elasticsearch") { Fail "CONFIG" "health contract failed" }
    return [ordered]@{ status = "ok"; agent = "reachable"; page = 200; java = $java.status; searchJava = $searchJava.status; elasticsearch = $elastic.status; elasticsearchChannel = $search.data.channel; url = "http://127.0.0.1:$AgentPort/" }
}

function Invoke-Smoke {
    $health = Invoke-Health
    $page = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:${AgentPort}/"
    if ($page.Content -notmatch "模拟参考价" -or $page.Content -notmatch "AI 合成，非真实报价") { Fail "CONFIG" "web synthetic-price disclosure is missing" }
    $model = Read-ModelConfiguration
    if (-not $model.configured) { Fail "MODEL" "DEEPSEEK_API_KEY is not configured; static web smoke passed but real Agent E2E was not run" }
    try {
        Invoke-RestMethod -Method Delete -TimeoutSec 5 -Uri "http://127.0.0.1:${AgentPort}/agent/sessions/used-phone-demo-smoke-v1" | Out-Null
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { Fail "CONFIG" "cannot reset owned smoke session" }
    }
    $body = @{ message = "预算不超过3000元，想找 iOS 二手手机，主板不要维修。"; sessionId = "used-phone-demo-smoke-v1"; domainHint = "ecommerce" } | ConvertTo-Json
    try {
        $response = Invoke-RestMethod -Method Post -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 90 -Uri "http://127.0.0.1:${AgentPort}/agent/chat-llm"
    } catch {
        $message = $_.Exception.Message
        if ($message -match "timed out|DeepSeek|model") { Fail "MODEL" $message }
        Fail "CONFIG" $message
    }
    if ($response.trace.status -ne "ok") { Fail "MODEL" "Agent request did not complete successfully" }
    $search = @($response.tool_trace | Where-Object { $_.tool -eq "search_products" -and $_.ok })
    if ($search.Count -lt 1) { Fail "JAVA" "real request did not produce a successful search_products trace" }
    if ($null -eq $response.guideResult -or @($response.guideResult.products).Count -lt 1) { Fail "DATA" "validated guideResult is missing" }
    $synthetic = @($response.guideResult.products | Where-Object {
        $_.product.priceStatus -eq "synthetic" -and
        $_.product.pricePolicy -eq "budget_and_ranking" -and
        $null -eq $_.product.snapshotPriceMinor -and
        $null -ne $_.product.syntheticReferencePriceMinor
    })
    if ($synthetic.Count -lt 1) { Fail "DATA" "guideResult lacks audited synthetic price policy" }
    return [ordered]@{
        status = "ok"; evidenceClass = "real_api_agent_java_e2e"; fixture = $false
        prompt = "fixed-used-phone-smoke-v1"; searchCalls = $search.Count
        guideProducts = @($response.guideResult.products).Count; syntheticPricedCards = $synthetic.Count
        requestId = $response.trace.requestId; runId = $response.runId
    }
}

function Stop-Demo {
    $state = Get-State
    if ($null -ne $state.agent) {
        $pidValue = [int]$state.agent.pid
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            $command = (Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue").CommandLine
            if ($command -notmatch "uvicorn" -or $command -notmatch "--port\s+$([regex]::Escape([string]$state.agent.port))") { Fail "CONFIG" "refusing to stop PID whose command line does not match owned Agent" }
            Stop-Process -Id $pidValue
            Wait-Process -Id $pidValue -Timeout 15 -ErrorAction SilentlyContinue
        }
    }
    $recorded = @($state.startedContainers | Select-Object -Unique)
    [array]::Reverse($recorded)
    foreach ($name in $recorded) {
        if ($name -notin @($MySqlContainer, $ProductRedisContainer, $BackendContainer, $SessionRedisContainer, $ElasticsearchContainer, $SearchBackendContainer)) { Fail "CONFIG" "state contains an unauthorized container name" }
        if ($name -in @($SessionRedisContainer, $ElasticsearchContainer, $SearchBackendContainer)) {
            $container = Get-Container $name
            if ($container.Config.Labels."codex.used-phone-demo.owner" -ne "used-phone-demo-v1") { Fail "CONFIG" "refusing to stop unowned session Redis" }
        }
        $container = Get-Container $name
        if ($container.State.Running) { Invoke-Docker @("stop", "--time", "10", $name) | Out-Null }
    }
    if (Test-Path -LiteralPath $StateFile) { Remove-Item -LiteralPath $StateFile }
    return [ordered]@{ status = "stopped"; stoppedOnlyRecordedResources = $true }
}

try {
    $result = switch ($Action) {
        "start" { Start-Demo }
        "audit" { Invoke-Audit }
        "health" { Invoke-Health }
        "smoke" { Invoke-Smoke }
        "stop" { Stop-Demo }
    }
    $result | ConvertTo-Json -Depth 8
    exit 0
} catch {
    [ordered]@{ status = "error"; classification = ($_.Exception.Message -split ":", 2)[0]; message = $_.Exception.Message } | ConvertTo-Json -Depth 4
    exit 2
}
