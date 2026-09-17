param(
    [switch]$SkipDatabase,
    [switch]$ReindexQdrant
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot

try {
    python -m agent.scripts.localize_yelp_to_beijing
    if ($LASTEXITCODE -ne 0) {
        throw "Beijing localization projection failed."
    }

    python agent/scripts/prepare_merchant_docs.py
    if ($LASTEXITCODE -ne 0) {
        throw "Merchant knowledge projection failed."
    }

    if ($SkipDatabase) {
        Write-Host "Generated local data, SQL, and merchant knowledge projection. Database import skipped."
        exit 0
    }

    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker is unavailable. Generated files are ready; rerun after Docker starts."
    }

    $mysqlContainer = "local-life-mysql"
    $running = docker inspect -f "{{.State.Running}}" $mysqlContainer 2>$null
    if ($LASTEXITCODE -ne 0 -or $running -ne "true") {
        docker compose up -d mysql
        if ($LASTEXITCODE -ne 0) {
            throw "Could not start the MySQL container."
        }
    }

    $healthy = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $health = docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $mysqlContainer 2>$null
        if ($health -eq "healthy" -or $health -eq "running") {
            $healthy = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) {
        throw "MySQL did not become ready in time."
    }

    docker cp db/yelp_sample.sql "${mysqlContainer}:/tmp/yelp_sample.sql"
    docker cp db/beijing_localization_migration.sql "${mysqlContainer}:/tmp/beijing_localization_migration.sql"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not copy migration files to MySQL."
    }

    docker exec $mysqlContainer sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" < /tmp/beijing_localization_migration.sql'
    if ($LASTEXITCODE -ne 0) {
        throw "MySQL localization migration failed."
    }

    $databaseName = docker exec $mysqlContainer printenv MYSQL_DATABASE
    $rootPassword = docker exec $mysqlContainer printenv MYSQL_ROOT_PASSWORD
    $sample = Get-Content -Raw -Encoding utf8 agent/recommendation/data/processed/yelp_local_life_sample.json | ConvertFrom-Json
    $expectedShops = $sample.shops.Count
    $expectedReviews = $sample.reviews.Count
    $expectedBehaviors = $sample.userBehaviors.Count
    $verificationSql = @"
SELECT
    (SELECT COUNT(*) FROM shop WHERE source = 'yelp'),
    (SELECT COUNT(*) FROM shop WHERE source = 'yelp' AND data_nature = 'localized_demo'),
    (SELECT COUNT(*) FROM shop WHERE source = 'yelp' AND coordinate_system = 'BD-09'),
    (SELECT COUNT(*) FROM shop WHERE source = 'yelp' AND localization_version = 'beijing-demo-v2'),
    (SELECT COUNT(*) FROM review WHERE source = 'yelp'),
    (SELECT COUNT(*) FROM review WHERE source = 'yelp' AND source_review_id IS NOT NULL AND source_user_id IS NOT NULL AND stars IS NOT NULL AND source_shop_name IS NOT NULL AND evidence_scope IS NOT NULL),
    (SELECT COUNT(*) FROM user_behavior WHERE source = 'yelp_review'),
    (SELECT COUNT(*) FROM user_behavior WHERE source = 'yelp_review' AND source_user_id IS NOT NULL AND source_review_id IS NOT NULL),
    (SELECT COUNT(*) FROM review r LEFT JOIN shop s ON s.id = r.shop_id WHERE r.source = 'yelp' AND s.id IS NULL),
    (SELECT COUNT(*) FROM user_behavior b LEFT JOIN shop s ON s.id = b.shop_id WHERE b.source = 'yelp_review' AND s.id IS NULL);
"@
    $verification = docker exec $mysqlContainer mysql -uroot "-p$rootPassword" $databaseName -N -e $verificationSql
    if ($LASTEXITCODE -ne 0) {
        throw "Database verification failed."
    }
    $values = @($verification -split "\s+" | Where-Object { $_ -ne "" })
    $expected = @(
        $expectedShops,
        $expectedShops,
        $expectedShops,
        $expectedShops,
        $expectedReviews,
        $expectedReviews,
        $expectedBehaviors,
        $expectedBehaviors,
        0,
        0
    )
    if ($values.Count -ne $expected.Count) {
        throw "Database verification returned an unexpected shape: $verification"
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ([int64]$values[$index] -ne [int64]$expected[$index]) {
            throw "Database verification mismatch at index $index. Expected $($expected[$index]), got $($values[$index])."
        }
    }
    Write-Host "Database verification passed: shops=$expectedShops reviews=$expectedReviews behaviors=$expectedBehaviors lineage=complete orphans=0"

    $redisContainer = "local-life-redis"
    $redisRunning = docker inspect -f "{{.State.Running}}" $redisContainer 2>$null
    if ($LASTEXITCODE -eq 0 -and $redisRunning -eq "true") {
        $shopCacheKeys = docker exec $redisContainer redis-cli --scan --pattern "local-life:shop:*"
        foreach ($key in $shopCacheKeys) {
            if ($key) {
                docker exec $redisContainer redis-cli DEL $key *> $null
            }
        }
        Write-Host "Cleared derived shop detail and GEO cache keys."
    }

    if ($ReindexQdrant) {
        docker compose --profile rag up -d redis qdrant backend
        if ($LASTEXITCODE -ne 0) {
            throw "Could not start services required for Qdrant reindexing."
        }
        docker compose run --rm agent python scripts/reindex_beijing_localization.py
        if ($LASTEXITCODE -ne 0) {
            throw "Qdrant localization reindex failed."
        }
    }

    Write-Host "Beijing localization migration completed. Review, behavior, and recommendation IDs were preserved."
}
finally {
    Pop-Location
}
