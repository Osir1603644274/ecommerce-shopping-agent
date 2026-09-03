[CmdletBinding()]
param(
    [ValidateSet("start", "health", "smoke", "stop")]
    [string]$Action = "start"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\commerce-demo-v1"
$StateFile = Join-Path $RuntimeDir "state.json"
$Containers = @(
    "local-life-mysql",
    "local-life-redis",
    "local-life-kafka",
    "local-life-elasticsearch",
    "local-life-backend",
    "local-life-agent"
)

function Invoke-Docker([string[]]$Arguments) {
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "docker $($Arguments -join ' ') failed: $($output -join ' ')" }
    return @($output)
}

function Test-ContainerRunning([string]$Name) {
    $value = & docker inspect --format "{{.State.Running}}" $Name 2>$null
    return $LASTEXITCODE -eq 0 -and ($value -join "").Trim() -eq "true"
}

function Wait-Http([string]$Url, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try { return Invoke-RestMethod -TimeoutSec 4 -Uri $Url } catch { Start-Sleep -Seconds 2 }
    } while ((Get-Date) -lt $deadline)
    throw "health timeout: $Url"
}

function Get-State {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    return Get-Content -Raw -Encoding UTF8 -LiteralPath $StateFile | ConvertFrom-Json
}

function Save-State($State) {
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $State | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $StateFile
}

function Start-Demo {
    Invoke-Docker @("version", "--format", "{{.Server.Version}}") | Out-Null
    $alreadyRunning = @($Containers | Where-Object { Test-ContainerRunning $_ })
    if ($alreadyRunning.Count -gt 0) {
        $capability = $null
        try { $capability = Invoke-RestMethod -TimeoutSec 4 -Uri "http://127.0.0.1:8000/api/commerce-demo/capability" } catch {}
        if ($alreadyRunning.Count -eq $Containers.Count -and $capability.enabled -eq $true) {
            return [ordered]@{ status = "already_running"; url = "http://127.0.0.1:8000/commerce-demo" }
        }
        throw "refusing to reconfigure running shared containers: $($alreadyRunning -join ', ')"
    }

    $previous = @{}
    $demoEnvironment = [ordered]@{
        AGENT_TRANSACTION_ENABLED = "true"
        # The public Demo exercises a real model-backed TaskState extraction.
        # Keep this opt-in journey independent from the shorter production
        # request budget so a slow provider cannot make the start script flaky.
        AGENT_REQUEST_DEADLINE_SECONDS = "120"
        PAYMENT_SIMULATOR_ENABLED = "true"
        COMMERCE_DEMO_ENABLED = "true"
        COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED = "true"
        COMMERCE_DEMO_COOKIE_SECURE = "false"
        PRODUCT_RETRIEVAL_MODE = "elasticsearch"
    }
    foreach ($key in $demoEnvironment.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $demoEnvironment[$key], "Process")
    }
    try {
        $composeOutput = @(& docker compose -f (Join-Path $ProjectRoot "docker-compose.yml") up -d --build mysql redis kafka elasticsearch backend agent 2>&1)
        $composeExit = $LASTEXITCODE
        if ($composeExit -ne 0) {
            $tail = @($composeOutput | Select-Object -Last 20) -join " "
            throw "docker compose start failed: $tail"
        }
    } finally {
        foreach ($key in $demoEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previous[$key], "Process")
        }
    }
    Save-State ([ordered]@{ version = 1; startedContainers = $Containers; startedAtUtc = (Get-Date).ToUniversalTime().ToString("o") })
    Wait-Http "http://127.0.0.1:8080/actuator/health/readiness" | Out-Null
    Wait-Http "http://127.0.0.1:8000/health" | Out-Null
    $capability = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:8000/api/commerce-demo/capability"
    if ($capability.enabled -ne $true -or $capability.paymentSimulationEnabled -ne $true) {
        throw "commerce demo flags were not loaded by Agent"
    }
    return [ordered]@{ status = "started"; url = "http://127.0.0.1:8000/commerce-demo"; services = $Containers.Count }
}

function Get-Health {
    $agent = Wait-Http "http://127.0.0.1:8000/health" 20
    $java = Wait-Http "http://127.0.0.1:8080/actuator/health/readiness" 20
    $capability = Invoke-RestMethod -TimeoutSec 5 -Uri "http://127.0.0.1:8000/api/commerce-demo/capability"
    $page = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:8000/commerce-demo"
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch "全链路 Demo") { throw "commerce demo page contract failed" }
    return [ordered]@{
        status = "ok"
        agent = $agent.status
        java = $java.status
        demoEnabled = $capability.enabled
        paymentSimulationEnabled = $capability.paymentSimulationEnabled
        page = $page.StatusCode
    }
}

function Invoke-Smoke {
    $health = Get-Health
    $username = "commerce-demo-" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $password = "StrongPassword123!"
    $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $origin = "http://127.0.0.1:8000"
    $auth = Invoke-RestMethod -Method Post -WebSession $web -Headers @{ Origin = $origin } -ContentType "application/json" -Body (@{ username = $username; password = $password } | ConvertTo-Json) -Uri "$origin/api/commerce-demo/register"
    $csrfHeaders = @{ Origin = $origin; "X-CSRF-Token" = $auth.csrfToken }
    $sessionId = "commerce-demo-smoke-" + [Guid]::NewGuid().ToString("N")
    $query = @{ message = "推荐变形金刚 G09 耳机"; sessionId = $sessionId; domainHint = "ecommerce" } | ConvertTo-Json
    $guide = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body $query -TimeoutSec 150 -Uri "$origin/agent/chat-llm"
    $products = @($guide.guideResult.products | Where-Object { $null -ne $_ -and $null -ne $_.product })
    if ($products.Count -lt 1) { throw "Agent did not return a verified product card" }
    $productId = [long]$products[0].product.id
    $preview = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body (@{ sessionId = $sessionId; productId = $productId; quantity = 1 } | ConvertTo-Json) -Uri "$origin/transaction-agent/orders/preview"
    if ($preview.data.confirmationPhrase -ne "确认下单") { throw "order preview did not require exact confirmation" }
    $confirm = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body (@{ message = "确认下单"; sessionId = $sessionId; domainHint = "ecommerce" } | ConvertTo-Json) -TimeoutSec 150 -Uri "$origin/agent/chat-llm"
    $orderTrace = @($confirm.tool_trace | Where-Object { $_.tool -eq "create_order" -and $_.ok })[0]
    if ($null -eq $orderTrace) { throw "confirmed order did not return create_order receipt" }
    $order = $orderTrace.detail.result
    $paymentPreview = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body (@{ sessionId = $sessionId; orderId = $order.id } | ConvertTo-Json) -Uri "$origin/transaction-agent/payments/preview"
    if ($paymentPreview.data.confirmationPhrase -ne "确认发起支付") { throw "payment preview did not require exact confirmation" }
    $paymentConfirm = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body (@{ message = "确认发起支付"; sessionId = $sessionId; domainHint = "ecommerce" } | ConvertTo-Json) -TimeoutSec 150 -Uri "$origin/agent/chat-llm"
    $paymentTrace = @($paymentConfirm.tool_trace | Where-Object { $_.tool -eq "create_payment" -and $_.ok })[0]
    if ($null -eq $paymentTrace) { throw "confirmed payment did not return create_payment receipt" }
    $payment = $paymentTrace.detail.result
    $simulated = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -Uri "$origin/api/commerce-demo/payments/$($payment.id)/simulate-success"
    $status = Invoke-RestMethod -Method Post -WebSession $web -Headers $csrfHeaders -ContentType "application/json" -Body (@{ message = "查询订单 $($order.orderNo) 的状态"; sessionId = $sessionId; domainHint = "ecommerce" } | ConvertTo-Json) -TimeoutSec 150 -Uri "$origin/agent/chat-llm"
    $statusTrace = @($status.tool_trace | Where-Object { $_.tool -eq "query_order_status" -and $_.ok })[0]
    if ($null -eq $statusTrace -or $statusTrace.detail.order.status -ne "PAID") { throw "Agent did not read PAID from Java/MySQL" }
    return [ordered]@{
        status = "ok"
        evidenceClass = "real_agent_java_commerce_demo"
        requestId = $guide.trace.requestId
        productId = $productId
        orderId = $order.id
        orderNo = $order.orderNo
        paymentId = $payment.id
        paymentStatus = $simulated.data.status
        agentObservedOrderStatus = $statusTrace.detail.order.status
    }
}

function Stop-Demo {
    $state = Get-State
    if ($null -eq $state) { return [ordered]@{ status = "not_owned"; stopped = 0 } }
    $allowed = @($Containers)
    $recorded = @($state.startedContainers)
    foreach ($name in $recorded) {
        if ($name -notin $allowed) { throw "state contains unauthorized container: $name" }
    }
    [array]::Reverse($recorded)
    $stopped = 0
    foreach ($name in $recorded) {
        if (Test-ContainerRunning $name) { Invoke-Docker @("stop", "--time", "10", $name) | Out-Null; $stopped++ }
    }
    if (Test-Path -LiteralPath $StateFile) { Remove-Item -LiteralPath $StateFile }
    return [ordered]@{ status = "stopped"; stopped = $stopped; volumesDeleted = $false }
}

try {
    $result = switch ($Action) {
        "start" { Start-Demo }
        "health" { Get-Health }
        "smoke" { Invoke-Smoke }
        "stop" { Stop-Demo }
    }
    $result | ConvertTo-Json -Depth 8
    exit 0
} catch {
    [ordered]@{ status = "error"; message = $_.Exception.Message } | ConvertTo-Json -Depth 4
    exit 2
}
