[CmdletBinding()]
param(
    [ValidateSet('check', 'recycle', 'move', 'empty', 'restore-live-paths')]
    [string]$Phase = 'check'
)

$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath('F:\agent')
$taskPrefix = $taskRoot + '\'
$recordPath = 'F:\agent\scripts\local-maintenance\workspace\organization-record-20260904.json'
$record = Get-Content -LiteralPath $recordPath -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable -Depth 60
$checkedParents = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

function Save-Record {
    $record.updatedAt = [DateTime]::UtcNow.ToString('o')
    [IO.File]::WriteAllText($recordPath, ($record | ConvertTo-Json -Depth 60), [Text.UTF8Encoding]::new($false))
}

function Assert-WorkspacePath([string]$value) {
    $absolute = if ([IO.Path]::IsPathFullyQualified($value)) {
        [IO.Path]::GetFullPath($value)
    } else {
        [IO.Path]::GetFullPath((Join-Path $taskRoot $value))
    }
    if (-not $absolute.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the named workspace or is the workspace root: $absolute"
    }
    $cursor = $absolute
    while ($cursor.Length -ge $taskRoot.Length) {
        if ($checkedParents.Contains($cursor)) { break }
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $cursor" }
            if ($item.PSIsContainer) { $null = $checkedParents.Add($cursor) }
        }
        if ($cursor -eq $taskRoot) { break }
        $cursor = Split-Path -Parent $cursor
    }
    return $absolute
}

function Get-SafeTreeFiles([string]$absolute) {
    $stack = [Collections.Generic.Stack[string]]::new()
    $stack.Push($absolute)
    while ($stack.Count) {
        $directory = $stack.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            $validated = Assert-WorkspacePath $item.FullName
            if ($item.PSIsContainer) { $stack.Push($validated) }
            else { $item }
        }
    }
}

function Get-KeeperPath([string]$value) {
    if ([IO.Path]::IsPathFullyQualified($value)) {
        $absolute = [IO.Path]::GetFullPath($value)
        if (-not $absolute.StartsWith('C:\Users\ming\Desktop\袁明珠_简历\', [StringComparison]::OrdinalIgnoreCase) -and
            -not $absolute.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unapproved keeper location' }
        return $absolute
    }
    return Assert-WorkspacePath $value
}

function Assert-Source($action, [bool]$IgnoreEarlierRecycles = $false) {
    $absolute = Assert-WorkspacePath $action.source
    if (-not (Test-Path -LiteralPath $absolute)) { throw "Missing planned source: $absolute" }
    $actual = if ($action.isDirectory) { @(Get-SafeTreeFiles $absolute) } else { @(Get-Item -LiteralPath $absolute -Force) }
    if ($IgnoreEarlierRecycles) {
        $priorFiles = @($record.actions | Where-Object { $_.type -eq 'recycle' -and -not $_.isDirectory } | ForEach-Object { Assert-WorkspacePath $_.source })
        $actual = @($actual | Where-Object { $_.FullName -notin $priorFiles })
    }
    if ($actual.Count -ne $action.files.Count) { throw "File count changed: $($action.id), actual=$($actual.Count), planned=$($action.files.Count)" }
    $expected = @{}
    foreach ($f in $action.files) { $expected[(Assert-WorkspacePath $f.path)] = $f }
    foreach ($file in $actual) {
        if (-not $expected.ContainsKey($file.FullName)) { throw "Uninventoried file: $($file.FullName)" }
        $f = $expected[$file.FullName]
        if ($file.Length -ne $f.size -or (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $f.sha256) {
            throw "Source changed since inventory: $($file.FullName)"
        }
    }
    if ($action.destination) {
        $target = Assert-WorkspacePath $action.destination
        if (Test-Path -LiteralPath $target) { throw "Destination exists, no overwrite allowed: $target" }
    }
    return $absolute
}

function Assert-Proof($action) {
    if ($action.proof.kind -eq 'exact-file') {
        $keeper = Get-KeeperPath $action.proof.keeper
        if ((Get-FileHash -LiteralPath $keeper -Algorithm SHA256).Hash -ne $action.files[0].sha256) { throw "Keeper hash mismatch: $keeper" }
    } elseif ($action.proof.kind -eq 'tree-equality') {
        foreach ($f in $action.files) {
            $suffix = $f.path.Substring($action.proof.insidePrefix.Length).TrimStart('/')
            $keeper = Assert-WorkspacePath ($action.proof.keeper + '/' + $suffix)
            if ((Get-FileHash -LiteralPath $keeper -Algorithm SHA256).Hash -ne $f.sha256) { throw "Keeper mismatch: $keeper" }
        }
    } elseif ($action.proof.kind -eq 'zip-expanded-equality') {
        $source = Assert-WorkspacePath $action.source
        $expanded = Assert-WorkspacePath $action.proof.keeper
        $archive = [IO.Compression.ZipFile]::OpenRead($source)
        try {
            $count = 0
            foreach ($entry in $archive.Entries) {
                if (-not $entry.Name) { continue }
                if ($entry.FullName -match '(^|[/\\])\.\.([/\\]|$)' -or [IO.Path]::IsPathRooted($entry.FullName)) { throw 'Unsafe archive entry' }
                $stream = $entry.Open(); $sha = [Security.Cryptography.SHA256]::Create()
                try { $hash = [Convert]::ToHexString($sha.ComputeHash($stream)) } finally { $stream.Dispose(); $sha.Dispose() }
                $inside = $entry.FullName.Replace('/', '\')
                $options = @((Join-Path $expanded $inside), (Join-Path (Split-Path -Parent $expanded) $inside))
                $separator = $inside.IndexOf('\')
                if ($separator -ge 0) { $options += Join-Path $expanded $inside.Substring($separator + 1) }
                $matched = $false
                foreach ($candidate in $options) {
                    $candidate = Assert-WorkspacePath $candidate
                    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                        $candidateHash = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash
                        if ($candidateHash -eq $hash) { $matched = $true; break }
                    }
                }
                if (-not $matched) { throw "Archive entry has no equal retained original: $($entry.FullName)" }
                $count++
            }
            if ($count -ne $action.proof.entries) { throw 'Archive entry count changed' }
        } finally { $archive.Dispose() }
    }
}

Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.IO.Compression.FileSystem

if ($Phase -eq 'restore-live-paths') {
    foreach ($id in @('A025', 'A035')) {
        $action = $record.actions | Where-Object { $_.id -eq $id } | Select-Object -First 1
        if ($action.status -eq 'reverted') { continue }
        if ($action.status -ne 'done' -or $action.type -ne 'move') { throw "Unexpected original action state: $id" }
        $old = Assert-WorkspacePath $action.source
        $archive = Assert-WorkspacePath $action.destination
        $archiveFiles = @(Get-SafeTreeFiles $archive)
        if ($archiveFiles.Count -ne $action.files.Count) { throw 'Archived source file count changed' }
        foreach ($f in $action.files) {
            $archivedFile = Assert-WorkspacePath ($action.destination + $f.path.Substring($action.source.Length))
            if ((Get-FileHash -LiteralPath $archivedFile -Algorithm SHA256).Hash -ne $f.sha256) { throw "Archived source changed: $archivedFile" }
        }
        if (Test-Path -LiteralPath $old) {
            if (@(Get-SafeTreeFiles $old).Count) { throw "Old path has new files; no overwrite allowed: $old" }
            $action.recreatedEmptyDirectories = @(Get-ChildItem -LiteralPath $old -Directory -Recurse -Force | ForEach-Object { $_.FullName })
            Save-Record
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($old, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        }
        $checkedParents.Clear()
        $null = Assert-WorkspacePath $old
        if (Test-Path -LiteralPath $old) { throw 'Concurrent recreation before restore; no overwrite allowed' }
        Move-Item -LiteralPath $archive -Destination $old
        foreach ($f in $action.files) {
            $restored = Assert-WorkspacePath $f.path
            if ((Get-FileHash -LiteralPath $restored -Algorithm SHA256).Hash -ne $f.sha256) { throw "Restored original differs: $restored" }
        }
        $action.status = 'reverted'
        $action.revertedAt = [DateTime]::UtcNow.ToString('o')
        $action.revertReason = '旧路径被后台 Java 工具重建；撤回本轮搬迁以保留现有编辑器和构建引用，原位索引'
        $record.retained += @{path=$action.source;reason=$action.revertReason}
        Save-Record
        Write-Output "RESTORED ORIGINAL PATH $id $($action.source)"
    }
    exit
}

if ($Phase -eq 'empty') {
    foreach ($relative in @('.codex', '.tmp', 'output', 'downloads')) {
        $absolute = Assert-WorkspacePath $relative
        if (-not (Test-Path -LiteralPath $absolute)) { continue }
        if (@(Get-SafeTreeFiles $absolute).Count) { throw "Empty-folder cleanup found a file: $absolute" }
        $action = @{id="E-$relative";type='recycle';source=$relative;destination=$null;reason='归档或重复副本清理后，仅剩空目录';isDirectory=$true;files=@();proof=@{kind='empty-tree'};status='started'}
        $record.actions += $action; Save-Record
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($absolute, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        if (Test-Path -LiteralPath $absolute) { throw "Still exists: $absolute" }
        $action.status='done'; $action.completedAt=[DateTime]::UtcNow.ToString('o'); Save-Record
        Write-Output "RECYCLED EMPTY $relative"
    }
    exit
}

foreach ($action in $record.actions) {
    if ($action.status -in @('done', 'reverted')) { continue }
    if ($Phase -ne 'check' -and $action.type -ne $Phase) { continue }
    $checkedParents.Clear()
    $source = Assert-Source $action ($Phase -eq 'check' -and $action.type -eq 'move')
    Assert-Proof $action
    if ($Phase -eq 'check') { Write-Output "CHECKED $($action.id) $($action.source)"; continue }
    $action.status='started'; Save-Record
    try {
        if ($action.type -eq 'move') {
            $target = Assert-WorkspacePath $action.destination
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent)) { $null = New-Item -ItemType Directory -Path $parent -Force }
            $null = Assert-WorkspacePath $target
            Move-Item -LiteralPath $source -Destination $target
            if (Test-Path -LiteralPath $source) { throw 'Move left source in place' }
            foreach ($f in $action.files) {
                $suffix = $f.path.Substring($action.source.Length)
                $moved = Assert-WorkspacePath ($action.destination + $suffix)
                if ((Get-FileHash -LiteralPath $moved -Algorithm SHA256).Hash -ne $f.sha256) { throw "Moved-file integrity failure: $moved" }
            }
        } elseif ($action.isDirectory) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($source, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        } else {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($source, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin, [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        }
        if (Test-Path -LiteralPath $source) { throw "Source still present: $source" }
        $action.status='done'; $action.completedAt=[DateTime]::UtcNow.ToString('o'); Save-Record
        Write-Output "$($action.type.ToUpper()) $($action.id) $($action.source)"
    } catch {
        $action.status='error'; $action.error=$_.Exception.Message; Save-Record; throw
    }
}
