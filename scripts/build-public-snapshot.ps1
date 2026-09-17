[CmdletBinding()]
param(
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$RuntimeRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot ".runtime"))
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $RuntimeRoot "public-snapshot-v13"
}
$OutputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$runtimePrefix = $RuntimeRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $OutputRoot.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "public snapshot output must stay below $RuntimeRoot"
}
if (Test-Path -LiteralPath $OutputRoot) {
    if (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -gt 0) {
        throw "output already exists and is not empty: $OutputRoot"
    }
} else {
    New-Item -ItemType Directory -Path $OutputRoot | Out-Null
}

$ForbiddenParts = @(
    ".git", ".runtime", ".cache", ".codex", ".claude", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", "target", "node_modules", "review-bundles", "outputs"
)

function Copy-PublicFile([string]$RelativePath) {
    $source = Join-Path $ProjectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "required public file is missing: $RelativePath"
    }
    $destination = Join-Path $OutputRoot $RelativePath
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}

function Copy-PublicTree([string]$RelativeRoot, [switch]$PythonOnly) {
    $sourceRoot = Join-Path $ProjectRoot $RelativeRoot
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
        throw "required public directory is missing: $RelativeRoot"
    }
    foreach ($source in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse) {
        $relativeInside = [IO.Path]::GetRelativePath($sourceRoot, $source.FullName)
        $parts = $relativeInside -split '[\\/]'
        if (@($parts | Where-Object { $_ -in $ForbiddenParts }).Count -gt 0) { continue }
        if ($source.Extension -in @(".pyc", ".pyo", ".log")) { continue }
        if ($PythonOnly -and $source.Extension -ne ".py") { continue }
        if ($source.Length -gt 5MB) {
            throw "public file exceeds 5 MiB: $RelativeRoot/$relativeInside"
        }
        $relative = Join-Path $RelativeRoot $relativeInside
        $destination = Join-Path $OutputRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $source.FullName -Destination $destination
    }
}

$rootFiles = @(
    ".env.example",
    ".gitattributes",
    ".gitignore",
    ".github/workflows/public-integrity.yml",
    "README.md",
    "docker-compose.yml",
    "agent/.dockerignore",
    "agent/Dockerfile",
    "agent/pyproject.toml",
    "backend/.dockerignore",
    "backend/Dockerfile",
    "backend/pom.xml",
    "backend/settings.xml",
    "backend-gateway/Dockerfile",
    "backend-gateway/pom.xml",
    "backend-gateway/settings.xml",
    "db/shop_type.sql",
    "db/shop.sql",
    "db/review.sql",
    "db/user_behavior.sql",
    "db/product.sql",
    "db/bootstrap-cloud-split-roles.sh",
    "observability/prometheus.yml",
    "scripts/commerce-demo.ps1",
    "scripts/build-public-snapshot.ps1",
    "scripts/check_public_snapshot.py",
    "scripts/check_repository_hygiene.py",
    "scripts/check_markdown_links.py",
    "scripts/document_paths.py",
    "docs/PUBLIC_REPOSITORY_GUIDE.md",
    "docs/FEATURE_EVIDENCE_INDEX.md",
    "docs/demo/README.md",
    "docs/demo/THREE_MINUTE_PITCH.md",
    "docs/assets/demo/commerce-demo-walkthrough.gif",
    "docs/assets/demo/commerce-demo-search.png",
    "docs/assets/demo/commerce-demo-paid.png",
    "docs/assets/demo/agent-flow-desktop.png",
    "docs/diagrams/current-architecture/README.md",
    "docs/diagrams/current-architecture/current-system.mmd",
    "docs/acceptance/commerce-demo-and-public-repository-governance-2026-09-03.md",
    "docs/acceptance/github-publication-and-demo-2026-09-03.md"
)
foreach ($relative in $rootFiles) { Copy-PublicFile $relative }

foreach ($tree in @(
    "agent/app",
    "agent/knowledge_data",
    "agent/place_data",
    "agent/rag",
    "backend/src",
    "backend-gateway/src",
    "retrieval_judgment_pool_core",
    "retrieval_judgment_pool_mcp"
)) {
    Copy-PublicTree $tree
}
foreach ($tree in @("agent/evaluation", "agent/recommendation", "agent/scripts")) {
    Copy-PublicTree $tree -PythonOnly
}

$agentTests = @(
    "__init__.py",
    "fake_redis.py",
    "memory_v2_fetch_support.py",
    "two_stage_ranking_fixtures.py",
    "test_candidate_scope.py",
    "test_graph_v2_interrupt_resume.py",
    "test_run_agent.py",
    "test_reference_context.py",
    "test_historical_events.py",
    "test_long_term_memory_contract.py",
    "test_memory_bff.py",
    "test_memory_candidate_worker.py",
    "test_memory_context_projection.py",
    "test_memory_contextpack_integration.py",
    "test_memory_governance.py",
    "test_memory_projection_client.py",
    "test_memory_runtime_bridge.py",
    "test_memory_v3_runtime.py",
    "test_session_memory.py",
    "test_task_state.py",
    "test_shopping_task_state_v2_contract.py",
    "test_shopping_state_update.py",
    "test_shopping_state_authority.py",
    "test_ecommerce_transactions.py",
    "test_transaction_capabilities.py",
    "test_transaction_agent_runtime.py",
    "test_transaction_agent_api.py",
    "test_commerce_demo_api.py",
    "test_order_status_agent.py",
    "test_chat_endpoint.py"
)
foreach ($name in $agentTests) {
    Copy-PublicFile (Join-Path "agent/tests" $name)
}

# Public Git blobs use canonical LF text. Normalize before hashing so a remote
# archive remains byte-for-byte verifiable against the manifest.
$binaryExtensions = @(".gif", ".jpg", ".jpeg", ".png", ".webp", ".pdf", ".zip")
$utf8NoBom = [Text.UTF8Encoding]::new($false)
foreach ($file in Get-ChildItem -LiteralPath $OutputRoot -File -Recurse) {
    if ($file.Extension.ToLowerInvariant() -in $binaryExtensions) { continue }
    $content = [IO.File]::ReadAllText($file.FullName, $utf8NoBom)
    $normalized = $content.Replace("`r`n", "`n").Replace("`r", "`n")
    [IO.File]::WriteAllText($file.FullName, $normalized, $utf8NoBom)
}

$manifestFiles = @(
    Get-ChildItem -LiteralPath $OutputRoot -File -Recurse |
        Where-Object { $_.Name -ne "PUBLIC_SNAPSHOT_MANIFEST.json" } |
        Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = ([IO.Path]::GetRelativePath($OutputRoot, $_.FullName) -replace "\\", "/")
                bytes = $_.Length
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
            }
        }
)
$manifest = [ordered]@{
    schemaVersion = "public-snapshot-manifest-v1"
    fileCount = $manifestFiles.Count
    files = $manifestFiles
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputRoot "PUBLIC_SNAPSHOT_MANIFEST.json") -Encoding utf8

& python (Join-Path $ProjectRoot "scripts/check_public_snapshot.py") $OutputRoot
if ($LASTEXITCODE -ne 0) { throw "public snapshot verification failed" }

[ordered]@{
    status = "PASS"
    output = $OutputRoot
    fileCount = $manifestFiles.Count
    manifest = (Join-Path $OutputRoot "PUBLIC_SNAPSHOT_MANIFEST.json")
} | ConvertTo-Json -Depth 4
