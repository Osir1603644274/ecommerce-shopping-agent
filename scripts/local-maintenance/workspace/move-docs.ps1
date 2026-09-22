[CmdletBinding()]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath('F:\agent')
$recordPath = 'F:\agent\scripts\local-maintenance\workspace\docs-archive-20260904.json'
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding utf8 | ConvertFrom-Json
function Checked-Path([string]$relative) {
    $absolute = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (-not $absolute.StartsWith($root + '\docs\', [StringComparison]::OrdinalIgnoreCase)) { throw "Outside docs: $absolute" }
    $parent = $absolute
    while ($parent -and $parent -ne $root) {
        if (Test-Path -LiteralPath $parent) {
            $item = Get-Item -LiteralPath $parent -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $parent" }
        }
        $parent = Split-Path -Parent $parent
    }
    return $absolute
}
foreach ($move in $record.moves) {
    if ($move.status -eq 'done') { continue }
    $source = Checked-Path $move.source
    $destination = Checked-Path $move.destination
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing file: $source" }
    if (Test-Path -LiteralPath $destination) { throw "No overwrite allowed: $destination" }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $move.sha256) { throw "Changed since plan: $source" }
}
if (-not $Apply) { Write-Output "Dry run passed: $($record.moves.Count) exact file moves; no deletions."; exit 0 }
foreach ($move in $record.moves) {
    if ($move.status -eq 'done') { continue }
    $source = Checked-Path $move.source
    $destination = Checked-Path $move.destination
    if (Test-Path -LiteralPath $destination) { throw "Destination appeared: $destination" }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $move.sha256) { throw "Concurrent modification: $source" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Move-Item -LiteralPath $source -Destination $destination
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $move.sha256) { throw "Integrity mismatch: $destination" }
    $move.status = 'done'
    # Generated action receipt, not a modification of a source document.
    $record | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $recordPath -Encoding utf8
}
Write-Output "Moved $($record.moves.Count) files. No original document deleted or overwritten."
