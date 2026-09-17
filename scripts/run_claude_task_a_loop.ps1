[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Workspace = "F:\agent"
$StatePath = "F:\agent\docs\acceptance\.claude-codex-loop-state.json"
$PromptPath = "F:\agent\docs\acceptance\claude-codex-executor-prompt.md"
$DocumentPath = $null
$ClaudePath = "C:\Users\ming\AppData\Roaming\npm\claude.ps1"
$AutomationDirectory = "C:\Users\ming\.codex\automations\10-a"
$LockPath = Join-Path $AutomationDirectory "claude-codex-loop.lock"
$LogPath = Join-Path $AutomationDirectory "claude-latest.json"

$ExpectedWorkspace = [System.IO.Path]::GetFullPath($Workspace).TrimEnd('\')
$ResolvedWorkspace = [System.IO.Path]::GetFullPath($Workspace).TrimEnd('\')
if ($ResolvedWorkspace -ne $ExpectedWorkspace) {
    throw "Workspace path mismatch: $ResolvedWorkspace"
}

foreach ($RequiredPath in @($Workspace, $StatePath, $PromptPath, $ClaudePath, $AutomationDirectory)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "Required path is missing: $RequiredPath"
    }
}

function Read-LoopState {
    Get-Content -LiteralPath $StatePath -Raw -Encoding utf8 | ConvertFrom-Json
}

function Save-LoopState {
    param([Parameter(Mandatory)]$State)

    $StateDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $StatePath)).TrimEnd('\')
    $ExpectedStateDirectory = [System.IO.Path]::GetFullPath("F:\agent\docs\acceptance").TrimEnd('\')
    if ($StateDirectory -ne $ExpectedStateDirectory) {
        throw "Refusing to write state outside the expected directory: $StateDirectory"
    }

    $TemporaryPath = Join-Path $StateDirectory (".claude-codex-loop-state.{0}.tmp" -f [guid]::NewGuid().ToString("N"))
    try {
        $Json = $State | ConvertTo-Json -Depth 8
        Set-Content -LiteralPath $TemporaryPath -Value $Json -Encoding utf8
        Move-Item -LiteralPath $TemporaryPath -Destination $StatePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
}

function Get-OutOfScopeSnapshot {
    param([Parameter(Mandatory)][string[]]$AllowedPaths)

    $Allowed = @{}
    foreach ($AllowedPath in $AllowedPaths) {
        $Allowed[$AllowedPath.Replace('\', '/')] = $true
    }

    $TrackedFiles = @(& git -c core.quotepath=false -C $Workspace ls-files)
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files failed"
    }

    $TrackedHashes = @()
    foreach ($RelativePath in ($TrackedFiles | Sort-Object)) {
        $NormalizedPath = $RelativePath.Replace('\', '/')
        if ($Allowed.ContainsKey($NormalizedPath)) {
            continue
        }

        $AbsolutePath = Join-Path $Workspace $RelativePath
        if (Test-Path -LiteralPath $AbsolutePath -PathType Leaf) {
            $TrackedHashes += [pscustomobject]@{
                path = $NormalizedPath
                hash = (Get-FileHash -LiteralPath $AbsolutePath -Algorithm SHA256).Hash
            }
        }
        else {
            $TrackedHashes += [pscustomobject]@{
                path = $NormalizedPath
                hash = "<missing>"
            }
        }
    }

    $UntrackedFiles = @(& git -c core.quotepath=false -C $Workspace ls-files --others --exclude-standard)
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-files --others failed"
    }
    $OutsideUntrackedPaths = @(
        $UntrackedFiles |
            ForEach-Object { $_.Replace('\', '/') } |
            Where-Object { -not $Allowed.ContainsKey($_) } |
            Sort-Object
    )
    $OutsideUntrackedHashes = @()
    foreach ($RelativePath in $OutsideUntrackedPaths) {
        $AbsolutePath = Join-Path $Workspace $RelativePath
        if (Test-Path -LiteralPath $AbsolutePath -PathType Leaf) {
            $OutsideUntrackedHashes += [pscustomobject]@{
                path = $RelativePath
                hash = (Get-FileHash -LiteralPath $AbsolutePath -Algorithm SHA256).Hash
            }
        }
        else {
            $OutsideUntrackedHashes += [pscustomobject]@{
                path = $RelativePath
                hash = "<missing>"
            }
        }
    }

    $Payload = [pscustomobject]@{
        tracked = $TrackedHashes
        untracked = $OutsideUntrackedHashes
    }
    $PayloadJson = $Payload | ConvertTo-Json -Depth 6 -Compress
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($PayloadJson)
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Fingerprint = ([System.BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "")
    }
    finally {
        $Hasher.Dispose()
    }

    [pscustomobject]@{
        fingerprint = $Fingerprint
        details = $Payload
    }
}

$State = Read-LoopState
$DocumentPath = [string]$State.handoffDocument
if (-not (Test-Path -LiteralPath $DocumentPath -PathType Leaf)) {
    throw "Handoff document is missing: $DocumentPath"
}
if ($State.workspace -ne $Workspace) {
    throw "State workspace mismatch: $($State.workspace)"
}
if ($State.automationId -ne "10-a") {
    throw "State automation id mismatch: $($State.automationId)"
}

if ($DryRun) {
    $DryRunAllowedPaths = @($State.allowedPaths | ForEach-Object { [string]$_ })
    $DryRunSnapshot = Get-OutOfScopeSnapshot -AllowedPaths $DryRunAllowedPaths
    [pscustomobject]@{
        dryRun = $true
        status = $State.status
        round = $State.round
        maxRounds = $State.maxRounds
        claudePath = $ClaudePath
        workspace = $Workspace
        document = $DocumentPath
        allowedPaths = $State.allowedPaths
        permissionMode = "dontAsk"
        safeMode = $true
        writeToolEnabled = $false
        dangerousPermissionBypass = $false
        outOfScopeFingerprint = $DryRunSnapshot.fingerprint
        outsideTrackedFileCount = @($DryRunSnapshot.details.tracked).Count
        outsideUntrackedFileCount = @($DryRunSnapshot.details.untracked).Count
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($State.status -ne "NEEDS_FIX") {
    Write-Output "No Claude execution required: status=$($State.status)"
    exit 0
}
if ([int]$State.round -ge [int]$State.maxRounds) {
    $State.status = "BLOCKED"
    $State.lastActor = "orchestrator"
    $State.lastOutcome = "round_limit_reached"
    $State.blockedReason = "Maximum autonomous rounds reached"
    $State.updatedAt = (Get-Date).ToString("o")
    Save-LoopState -State $State
    throw "Maximum autonomous rounds reached"
}

$LockStream = $null
try {
    try {
        $LockStream = [System.IO.File]::Open(
            $LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch [System.IO.IOException] {
        Write-Output "Another Claude/Codex loop instance holds the lock; skipping this run."
        exit 0
    }

    $AllowedPaths = @($State.allowedPaths | ForEach-Object { [string]$_ })
    $State.round = [int]$State.round + 1
    $State.status = "CLAUDE_RUNNING"
    $State.lastActor = "orchestrator"
    $State.lastOutcome = "claude_started"
    $State.blockedReason = $null
    $State.updatedAt = (Get-Date).ToString("o")
    Save-LoopState -State $State
    $BeforeSnapshot = Get-OutOfScopeSnapshot -AllowedPaths $AllowedPaths

    $Prompt = Get-Content -LiteralPath $PromptPath -Raw -Encoding utf8

    $env:PYTHONPATH = "F:\agent\agent"
    $ClaudeArguments = @(
        "-p", $Prompt,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--no-session-persistence",
        "--no-chrome",
        "--strict-mcp-config",
        "--effort", "high",
        "--tools", "Read,Edit,Grep,Glob,Bash",
        "--allowedTools",
        "Read",
        "Edit",
        "Grep",
        "Glob",
        "Bash(python *)",
        "Bash(git diff *)",
        "Bash(git status *)",
        "Bash(git rev-parse *)",
        "Bash(rg *)",
        "Bash(pwd)"
    )

    Push-Location $Workspace
    try {
        $ClaudeOutput = (& $ClaudePath @ClaudeArguments 2>&1 | Out-String)
        $ClaudeExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    $AfterSnapshot = Get-OutOfScopeSnapshot -AllowedPaths $AllowedPaths
    $OutOfScopeChanged = $BeforeSnapshot.fingerprint -ne $AfterSnapshot.fingerprint

    $LogRecord = [pscustomobject]@{
        automationId = "10-a"
        round = $State.round
        finishedAt = (Get-Date).ToString("o")
        exitCode = $ClaudeExitCode
        outOfScopeChanged = $OutOfScopeChanged
        beforeOutOfScope = $BeforeSnapshot
        afterOutOfScope = $AfterSnapshot
        output = $ClaudeOutput
    }
    $LogRecord | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $LogPath -Encoding utf8

    $State.lastClaudeLog = $LogPath
    $State.updatedAt = (Get-Date).ToString("o")
    $State.lastDocumentSha256 = (Get-FileHash -LiteralPath $DocumentPath -Algorithm SHA256).Hash

    if ($OutOfScopeChanged) {
        $State.status = "BLOCKED"
        $State.lastActor = "orchestrator"
        $State.lastOutcome = "out_of_scope_change_detected"
        $State.blockedReason = "A file outside the conservative allowlist changed during Claude execution; no automatic rollback was attempted"
        Save-LoopState -State $State
        throw $State.blockedReason
    }

    if ($ClaudeExitCode -ne 0) {
        $State.status = "BLOCKED"
        $State.lastActor = "claude"
        $State.lastOutcome = "claude_failed"
        $State.blockedReason = "Claude CLI exited with code $ClaudeExitCode; inspect $LogPath"
        Save-LoopState -State $State
        throw $State.blockedReason
    }

    $State.status = "READY_FOR_REVIEW"
    $State.lastActor = "claude"
    $State.lastOutcome = "implementation_recorded"
    $State.blockedReason = $null
    Save-LoopState -State $State

    Write-Output "Claude round $($State.round) completed; status=READY_FOR_REVIEW; log=$LogPath"
}
catch {
    if ($null -ne $State -and $State.status -eq "CLAUDE_RUNNING") {
        $State.status = "BLOCKED"
        $State.lastActor = "orchestrator"
        $State.lastOutcome = "orchestrator_exception"
        $State.blockedReason = $_.Exception.Message
        $State.updatedAt = (Get-Date).ToString("o")
        Save-LoopState -State $State
    }
    throw
}
finally {
    if ($null -ne $LockStream) {
        $LockStream.Dispose()
    }
}
