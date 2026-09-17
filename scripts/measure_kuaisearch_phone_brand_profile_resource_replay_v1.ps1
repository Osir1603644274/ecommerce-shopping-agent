param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [Parameter(Mandatory = $true)][string]$Runner,
    [Parameter(Mandatory = $true)][string]$Attempt001,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [Parameter(Mandatory = $true)][string]$ResourceReceipt
)

$ErrorActionPreference = "Stop"
if (Test-Path -LiteralPath $OutputDir) {
    throw "OutputDir already exists: $OutputDir"
}
if (-not (Test-Path -LiteralPath $Manifest)) { throw "Manifest missing" }
if (-not (Test-Path -LiteralPath $Runner)) { throw "Runner missing" }

$stdoutPath = [System.IO.Path]::GetTempFileName()
$stderrPath = [System.IO.Path]::GetTempFileName()
$started = [DateTime]::UtcNow
$peakWorkingSet = 0L
$peakPrivate = 0L
$samples = 0

try {
    $process = Start-Process -FilePath "python" -ArgumentList @(
        $Runner, "--manifest", $Manifest, "--attempt001", $Attempt001, "--output-dir", $OutputDir
    ) -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

    while (-not $process.HasExited) {
        $process.Refresh()
        $peakWorkingSet = [Math]::Max($peakWorkingSet, [int64]$process.WorkingSet64)
        $peakPrivate = [Math]::Max($peakPrivate, [int64]$process.PrivateMemorySize64)
        $samples += 1
        Start-Sleep -Milliseconds 250
    }
    $process.WaitForExit()
    $process.Refresh()
    $peakWorkingSet = [Math]::Max($peakWorkingSet, [int64]$process.PeakWorkingSet64)
    $peakPrivate = [Math]::Max($peakPrivate, [int64]$process.PeakPagedMemorySize64)
    $finished = [DateTime]::UtcNow
    $stdout = Get-Content -Raw -Encoding utf8 -LiteralPath $stdoutPath
    $stderr = Get-Content -Raw -Encoding utf8 -LiteralPath $stderrPath
    if ($process.ExitCode -ne 0) {
        throw "Resource replay failed exit=$($process.ExitCode): $stderr"
    }
    $runReceipt = Join-Path $OutputDir "receipt.json"
    if (-not (Test-Path -LiteralPath $runReceipt)) { throw "Replay receipt missing" }
    $payload = [ordered]@{
        schemaVersion = "kuaisearch-phone-brand-profile-resource-replay-v1"
        evidenceRole = "NON_CONFIRMATORY_RESOURCE_REPLAY; not a second validation decision"
        startedAt = $started.ToString("o")
        finishedAt = $finished.ToString("o")
        wallClockSeconds = [Math]::Round(($finished - $started).TotalSeconds, 3)
        sampleIntervalMs = 250
        sampleCount = $samples
        peakProcessWorkingSetBytes = $peakWorkingSet
        peakProcessPrivateOrPagedBytes = $peakPrivate
        exitCode = $process.ExitCode
        manifest = [ordered]@{ path = (Resolve-Path -LiteralPath $Manifest).Path; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Manifest).Hash.ToLower() }
        runner = [ordered]@{ path = (Resolve-Path -LiteralPath $Runner).Path; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Runner).Hash.ToLower() }
        replayReceipt = [ordered]@{ path = (Resolve-Path -LiteralPath $runReceipt).Path; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $runReceipt).Hash.ToLower() }
        stdout = if ($null -eq $stdout) { "" } else { $stdout.Trim() }
        stderr = if ($null -eq $stderr) { "" } else { $stderr.Trim() }
    }
    $parent = Split-Path -Parent $ResourceReceipt
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 -LiteralPath $ResourceReceipt
    $payload | ConvertTo-Json -Depth 8
}
finally {
    foreach ($temporary in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}
