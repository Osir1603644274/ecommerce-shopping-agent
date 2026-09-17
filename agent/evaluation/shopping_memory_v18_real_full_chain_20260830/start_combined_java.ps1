[CmdletBinding()]
param(
    [string]$MySqlContainer = "agent-memory-v18-e2e-mysql-20260830",
    [string]$RedisContainer = "agent-memory-v18-e2e-redis-20260830",
    [string]$ElasticsearchContainer = "agent-memory-v18-e2e-es-20260830",
    [string]$JavaContainer = "agent-memory-v18-e2e-java-search-20260830",
    [string]$Network = "agent-memory-v18-e2e-net-20260830",
    [string]$JarPath = "D:\agent-e2e-memory-v18-20260830\target\local-life-backend-0.1.0-SNAPSHOT.jar",
    [int]$HostPort = 18086,
    [string]$SearchIndexPrefix = "memory-v18-e2e"
)

$ErrorActionPreference = "Stop"
$ExpectedOwner = "shopping-memory-v18-20260830"
$OwnerLabel = "codex.memory-v18.owner=$ExpectedOwner"

function Get-Owner([string]$Name) {
    return (& docker inspect $Name --format '{{index .Config.Labels "codex.memory-v18.owner"}}' 2>$null)
}

foreach ($name in @($MySqlContainer, $RedisContainer, $ElasticsearchContainer)) {
    if ((Get-Owner $name) -ne $ExpectedOwner) {
        throw "refusing unowned dependency: $name"
    }
}
if (-not (Test-Path -LiteralPath $JarPath -PathType Leaf)) {
    throw "Java JAR is missing"
}

$existing = & docker ps -a --filter "name=^/${JavaContainer}$" --format '{{.Names}}'
if ($existing -eq $JavaContainer) {
    if ((Get-Owner $JavaContainer) -ne $ExpectedOwner) {
        throw "refusing unowned Java container"
    }
    $running = & docker inspect $JavaContainer --format '{{.State.Running}}'
    if ($running -ne "true") {
        & docker start $JavaContainer | Out-Null
    }
    [ordered]@{ status = "existing_started"; javaContainer = $JavaContainer; hostPort = $HostPort } |
        ConvertTo-Json -Compress
    exit 0
}

if (Get-NetTCPConnection -LocalPort $HostPort -State Listen -ErrorAction SilentlyContinue) {
    throw "host port is occupied: $HostPort"
}
$mysqlEnvironment = & docker inspect $MySqlContainer --format '{{range .Config.Env}}{{println .}}{{end}}'
$values = @{}
foreach ($entry in $mysqlEnvironment) {
    $pair = $entry -split "=", 2
    if ($pair.Count -eq 2) { $values[$pair[0]] = $pair[1] }
}
foreach ($required in @("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD")) {
    if ([string]::IsNullOrWhiteSpace([string]$values[$required])) {
        throw "MySQL dependency environment is incomplete"
    }
}
$jwtBytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Fill($jwtBytes)
$jwtSecret = [Convert]::ToBase64String($jwtBytes)

$arguments = @(
    "create", "--name", $JavaContainer,
    "--label", $OwnerLabel,
    "--network", $Network,
    "-p", "127.0.0.1:${HostPort}:8080",
    "-v", "${JarPath}:/app/app.jar:ro",
    "-e", "DB_HOST=$MySqlContainer",
    "-e", "DB_PORT=3306",
    "-e", "DB_NAME=$($values.MYSQL_DATABASE)",
    "-e", "DB_USER=$($values.MYSQL_USER)",
    "-e", "DB_PASSWORD=$($values.MYSQL_PASSWORD)",
    "-e", "REDIS_HOST=$RedisContainer",
    "-e", "REDIS_PORT=6379",
    "-e", "JWT_SECRET=$jwtSecret",
    "-e", "SHOPPING_MEMORY_ENABLED=true",
    "-e", "SEARCH_ENABLED=true",
    "-e", "ELASTICSEARCH_URL=http://${ElasticsearchContainer}:9200",
    "-e", "SEARCH_INDEX_PREFIX=$SearchIndexPrefix",
    "-e", "SEARCH_RECONCILE_DELAY=PT1M",
    "-e", "SEARCH_RECONCILE_LIMIT=1500",
    "-e", "LOCAL_LIFE_SEARCH_INITIAL_RECONCILE_DELAY=PT1S",
    "-e", "MESSAGING_ENABLED=false",
    "-e", "FLASH_SALE_ENABLED=false",
    "-e", "RATE_LIMIT_ENABLED=false",
    "-e", "PAYMENT_SIMULATOR_ENABLED=false",
    "-e", "SHOP_CACHE_ENABLED=false",
    "-e", "PRODUCT_CACHE_ENABLED=false",
    "eclipse-temurin:17-jre", "java", "-jar", "/app/app.jar"
)
& docker @arguments | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker create failed" }
& docker start $JavaContainer | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker start failed" }

[ordered]@{ status = "created_started"; javaContainer = $JavaContainer; hostPort = $HostPort } |
    ConvertTo-Json -Compress
