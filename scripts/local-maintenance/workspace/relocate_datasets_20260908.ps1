$ErrorActionPreference = 'Stop'
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$recordRoot = Join-Path $workspaceRoot 'docs/data/dataset-organization-2026-09-08'
$plan = Get-Content -LiteralPath (Join-Path $recordRoot 'migration-plan.json') -Raw -Encoding utf8 | ConvertFrom-Json
$logPath = Join-Path $recordRoot 'moves.jsonl'
if (Test-Path -LiteralPath $logPath) { throw 'Execution record exists; inspect it before resuming.' }
# Validate EVERY resolved source and destination before moving any directory.
foreach ($entry in $plan.moves) {
    $source = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $entry.old))
    $destination = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $entry.new))
    foreach ($candidate in @($source, $destination)) {
        if (-not $candidate.StartsWith($workspaceRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Outside workspace: $candidate" }
    }
    if ((Get-Item -LiteralPath $source).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Source is a link: $source" }
    if (Test-Path -LiteralPath $destination) { throw "Destination exists: $destination" }
    foreach ($file in $entry.files) {
        $actual = (Get-FileHash -LiteralPath (Join-Path $source $file.path) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $file.sha256) { throw "Source changed since plan: $source / $($file.path)" }
    }
}
foreach ($entry in $plan.moves) {
    $source = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $entry.old))
    $destination = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $entry.new))
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Move-Item -LiteralPath $source -Destination $destination
    try {
        New-Item -ItemType Junction -Path $source -Target $destination | Out-Null
    } catch {
        # Both absolute paths were validated above. Restore the just-moved directory.
        if (-not (Test-Path -LiteralPath $source)) { Move-Item -LiteralPath $destination -Destination $source }
        throw
    }
    @{old=$entry.old; new=$entry.new; status='MOVED_WITH_JUNCTION'; time=(Get-Date).ToString('o')} | ConvertTo-Json -Compress | Add-Content -LiteralPath $logPath -Encoding utf8
}
Write-Output "Relocated $($plan.moves.Count) directories; old paths retained as junctions."
