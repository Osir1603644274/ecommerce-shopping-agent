[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Workspace = "F:\agent"
$StatePath = Join-Path $Workspace "docs\acceptance\.claude-codex-collaboration-state.json"
$DocumentPath = Join-Path $Workspace "docs\archive\collaboration\AI_COLLABORATION.md"
$PromptPath = Join-Path $Workspace "docs\acceptance\claude-codex-collaboration-prompt.md"
$ProtocolPath = Join-Path $Workspace "scripts\claude_collaboration_protocol.py"
$ClaudePath = "C:\Users\ming\AppData\Roaming\npm\claude.ps1"
$RunRoot = "C:\Users\ming\.codex\claude-collaboration"
$LockPath = Join-Path $RunRoot "collaboration.lock"

foreach ($RequiredPath in @($Workspace, $StatePath, $DocumentPath, $PromptPath, $ProtocolPath, $ClaudePath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "Required path is missing: $RequiredPath"
    }
}
New-Item -ItemType Directory -Path $RunRoot -Force | Out-Null

function Read-State {
    Get-Content -LiteralPath $StatePath -Raw -Encoding utf8 | ConvertFrom-Json
}

function Save-State {
    param([Parameter(Mandatory)]$State)

    $StateDirectory = [System.IO.Path]::GetFullPath((Split-Path -Parent $StatePath)).TrimEnd('\')
    $Expected = [System.IO.Path]::GetFullPath((Join-Path $Workspace "docs\acceptance")).TrimEnd('\')
    if ($StateDirectory -ne $Expected) {
        throw "Refusing to write state outside expected directory: $StateDirectory"
    }
    $TemporaryPath = Join-Path $StateDirectory (".claude-codex-collaboration-state.{0}.tmp" -f [guid]::NewGuid().ToString("N"))
    try {
        $State | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $TemporaryPath -Encoding utf8
        Move-Item -LiteralPath $TemporaryPath -Destination $StatePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
}

function Invoke-Protocol {
    param([Parameter(Mandatory)][ValidateSet("preflight", "postflight", "snapshot")][string]$Command)

    $Output = & python $ProtocolPath $Command --workspace $Workspace --state $StatePath --document $DocumentPath
    if ($LASTEXITCODE -ne 0) {
        throw "Protocol $Command failed: $Output"
    }
    $Output | ConvertFrom-Json
}

$State = Read-State
if ($DryRun) {
    $Preflight = Invoke-Protocol -Command "preflight"
    $Snapshot = Invoke-Protocol -Command "snapshot"
    [pscustomobject]@{
        dryRun = $true
        taskId = $State.taskId
        status = $State.status
        implementationModel = $State.implementationModel
        effort = "medium"
        allowedPaths = $State.allowedPaths
        outOfScopeFingerprint = $Snapshot.fingerprint
        outOfScopeFileCount = $Snapshot.fileCount
        preflight = $Preflight.ok
        modelInvoked = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}

$LockStream = $null
$AttemptDirectory = $null
$LogPath = $null
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
        throw "Another Claude collaboration run holds the lock"
    }

    Invoke-Protocol -Command "preflight" | Out-Null
    $Attempt = [int]$State.round + 1
    if ($Attempt -gt [int]$State.maxRounds) {
        throw "Maximum attempts reached for task $($State.taskId)"
    }
    $AttemptDirectory = Join-Path $RunRoot (Join-Path $State.taskId ("attempt-{0:d3}" -f $Attempt))
    if (Test-Path -LiteralPath $AttemptDirectory) {
        throw "Attempt directory already exists; refusing to overwrite: $AttemptDirectory"
    }
    New-Item -ItemType Directory -Path $AttemptDirectory | Out-Null
    $LogPath = Join-Path $AttemptDirectory "claude-output.json"

    $State.round = $Attempt
    $State.status = "CLAUDE_RUNNING"
    $State.lastActor = "orchestrator"
    $State.lastOutcome = "claude_started"
    $State.blockedReason = $null
    $State.updatedAt = (Get-Date).ToString("o")
    Save-State -State $State
    # Snapshot after the orchestrator-owned OPEN -> CLAUDE_RUNNING transition so
    # the state file itself cannot create a false out-of-scope alarm.
    $BeforeSnapshot = Invoke-Protocol -Command "snapshot"

    $BasePrompt = Get-Content -LiteralPath $PromptPath -Raw -Encoding utf8
    $Allowed = ($State.allowedPaths | ForEach-Object { "- $_" }) -join "`n"
    $Prompt = @"
$BasePrompt

Active task ID: $($State.taskId)
Machine status at launch: OPEN
This is Attempt $Attempt of $($State.maxRounds).

Machine-enforced edit allowlist:
$Allowed

Append the required report to: $DocumentPath
When the report is complete, stop. Do not begin any later task.
"@

    $env:PYTHONPATH = "F:\agent"
    $ClaudeArguments = @(
        "-p", $Prompt,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--safe-mode",
        "--no-session-persistence",
        "--no-chrome",
        "--strict-mcp-config",
        "--effort", "medium",
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
        "Bash(git hash-object *)",
        "Bash(rg *)",
        "Bash(pwd)"
    )

    Push-Location $Workspace
    try {
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            # Capture native stderr as attempt output. Windows PowerShell can
            # otherwise promote it to a terminating error before the failure
            # sidecar and BLOCKED state are written.
            $ErrorActionPreference = "Continue"
            $ClaudeOutput = (& $ClaudePath @ClaudeArguments 2>&1 | Out-String)
            $ClaudeExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
    }
    finally {
        Pop-Location
    }

    $AfterSnapshot = Invoke-Protocol -Command "snapshot"
    $OutOfScopeChanged = $BeforeSnapshot.fingerprint -ne $AfterSnapshot.fingerprint
    $Log = [pscustomobject]@{
        taskId = $State.taskId
        attempt = $Attempt
        startedWithModel = $State.implementationModel
        finishedAt = (Get-Date).ToString("o")
        exitCode = $ClaudeExitCode
        outOfScopeChanged = $OutOfScopeChanged
        beforeFingerprint = $BeforeSnapshot.fingerprint
        afterFingerprint = $AfterSnapshot.fingerprint
        output = $ClaudeOutput
    }
    $Log | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $LogPath -Encoding utf8

    $State = Read-State
    $State.lastClaudeLog = $LogPath
    $State.lastDocumentSha256 = (Get-FileHash -LiteralPath $DocumentPath -Algorithm SHA256).Hash.ToLower()
    $State.updatedAt = (Get-Date).ToString("o")
    if ($OutOfScopeChanged) {
        $State.status = "BLOCKED"
        $State.lastActor = "orchestrator"
        $State.lastOutcome = "out_of_scope_change_detected"
        $State.blockedReason = "A path outside the task allowlist changed; no automatic rollback was attempted"
        Save-State -State $State
        throw $State.blockedReason
    }
    if ($ClaudeExitCode -ne 0) {
        $State.status = "BLOCKED"
        $State.lastActor = "claude"
        $State.lastOutcome = "claude_failed"
        $State.blockedReason = "Claude CLI exited with code $ClaudeExitCode; inspect $LogPath"
        Save-State -State $State
        throw $State.blockedReason
    }

    try {
        Invoke-Protocol -Command "postflight" | Out-Null
    }
    catch {
        $State.status = "BLOCKED"
        $State.lastActor = "orchestrator"
        $State.lastOutcome = "invalid_claude_report"
        $State.blockedReason = $_.Exception.Message
        Save-State -State $State
        throw
    }

    $State.status = "READY_FOR_CODEX_REVIEW"
    $State.lastActor = "claude"
    $State.lastOutcome = "implementation_reported"
    $State.blockedReason = $null
    Save-State -State $State
    Write-Output "Claude task $($State.taskId) completed Attempt $Attempt; READY_FOR_CODEX_REVIEW; log=$LogPath"
}
catch {
    $FailureMessage = $_.Exception.Message
    try {
        $CurrentState = Read-State
        if ($CurrentState.status -eq "CLAUDE_RUNNING") {
            $CurrentState.status = "BLOCKED"
            $CurrentState.lastActor = "orchestrator"
            $CurrentState.lastOutcome = "orchestrator_exception"
            $CurrentState.blockedReason = $FailureMessage
            $CurrentState.updatedAt = (Get-Date).ToString("o")
            Save-State -State $CurrentState
        }
        if ($null -ne $AttemptDirectory -and (Test-Path -LiteralPath $AttemptDirectory)) {
            $FailurePath = Join-Path $AttemptDirectory "orchestrator-failure.json"
            if (-not (Test-Path -LiteralPath $FailurePath)) {
                [pscustomobject]@{
                    taskId = $CurrentState.taskId
                    attempt = $CurrentState.round
                    failedAt = (Get-Date).ToString("o")
                    error = $FailureMessage
                    modelImplementationStarted = $false
                } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $FailurePath -Encoding utf8
            }
        }
    }
    catch {
        Write-Error "Failed to persist collaboration failure state: $($_.Exception.Message)"
    }
    throw
}
finally {
    if ($null -ne $LockStream) {
        $LockStream.Dispose()
    }
}
