[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction Stop).Source
$Failures = [System.Collections.Generic.List[string]]::new()

$SourceFiles = @(
    "agent/app/llm.py",
    "agent/app/control/react_context.py",
    "agent/app/control/react_runtime.py",
    "agent/evaluation/used_phone_react_repair_stability_gate_v1.py",
    "agent/evaluation/used_phone_react_independent_author_package_v1.py",
    "agent/tests/test_react_v0_heldout_repairs.py",
    "agent/tests/test_used_phone_react_repair_stability_gate_v1.py",
    "agent/tests/test_used_phone_react_independent_author_package_v1.py",
    "scripts/run-react-v0-generalization-pilot.ps1",
    "scripts/check-react-v0-repair-readiness.ps1"
)
$AuthorKitFiles = @(
    "review-bundles/react-v0-independent-author-kit-v1/README.md",
    "review-bundles/react-v0-independent-author-kit-v1/SUBMISSION_CHECKLIST.md",
    "review-bundles/react-v0-independent-author-kit-v1/MANIFEST.json",
    "review-bundles/react-v0-independent-author-kit-v1/public/scenarios.template.jsonl",
    "review-bundles/react-v0-independent-author-kit-v1/private/preregistration.template.json",
    "review-bundles/react-v0-independent-author-kit-v1.zip"
)
$EvidenceFiles = @(
    ".runtime/react-v0-generalization-repair-stability-001-react_v0/run/receipts.jsonl",
    ".runtime/react-v0-generalization-repair-stability-002-react_v0/run/receipts.jsonl",
    ".runtime/react-v0-generalization-repair-stability-003-react_v0/run/receipts.jsonl",
    ".runtime/react-v0-repair-stability-gate-20260826.json"
)
$RequiredFiles = @($SourceFiles + $AuthorKitFiles + $EvidenceFiles)
foreach ($RelativePath in $RequiredFiles) {
    $FullPath = Join-Path $ProjectRoot $RelativePath
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        $Failures.Add("missing_file:$RelativePath")
    }
}

$SettingsText = Get-Content -Raw -Encoding utf8 -LiteralPath (
    Join-Path $ProjectRoot "agent/app/settings.py"
)
$EnvExampleText = Get-Content -Raw -Encoding utf8 -LiteralPath (
    Join-Path $ProjectRoot ".env.example"
)
$DeclaredDefaultFixed = (
    $SettingsText -match 'agent_control_runtime:\s*str\s*=\s*"fixed_v1"' -and
    $EnvExampleText -match '(?m)^AGENT_CONTROL_RUNTIME=fixed_v1\s*$'
)
if (-not $DeclaredDefaultFixed) {
    $Failures.Add("default_runtime_not_fixed_v1")
}

$PortStatus = @()
foreach ($Port in 18006, 18007, 18008, 18009) {
    $Listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    $Free = $null -eq $Listener
    $PortStatus += [ordered]@{ port = $Port; free = $Free }
    if (-not $Free) {
        $Failures.Add("port_in_use:$Port")
    }
}

$CompileExit = 0
& $Python -m compileall -q (
    Join-Path $ProjectRoot "agent/app"
) (
    Join-Path $ProjectRoot "agent/evaluation/used_phone_react_repair_stability_gate_v1.py"
) (
    Join-Path $ProjectRoot "agent/evaluation/used_phone_react_independent_author_package_v1.py"
)
$CompileExit = $LASTEXITCODE
if ($CompileExit -ne 0) {
    $Failures.Add("compileall_failed")
}

$TestExit = $null
if (-not $SkipTests) {
    $Tests = @(
        "agent/tests/test_react_shadow_integration.py",
        "agent/tests/test_react_actions.py",
        "agent/tests/test_react_context.py",
        "agent/tests/test_react_decision.py",
        "agent/tests/test_react_runtime.py",
        "agent/tests/test_react_runtime_config.py",
        "agent/tests/test_react_v0_heldout_repairs.py",
        "agent/tests/test_candidate_scope.py",
        "agent/tests/test_used_phone_harness_behavior_runner_v1.py",
        "agent/tests/test_used_phone_react_generalization_scorer_v1.py",
        "agent/tests/test_used_phone_react_repair_stability_gate_v1.py",
        "agent/tests/test_used_phone_react_independent_author_package_v1.py",
        "agent/tests/test_executor.py"
    )
    Push-Location $ProjectRoot
    try {
        & $Python -m pytest @Tests -q
        $TestExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($TestExit -ne 0) {
        $Failures.Add("relevant_tests_failed")
    }
}

$GateStatus = "NOT_RUN"
if (($EvidenceFiles | Where-Object {
    -not (Test-Path -LiteralPath (Join-Path $ProjectRoot $_) -PathType Leaf)
}).Count -eq 0) {
    $GateCode = @'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
from agent.evaluation.used_phone_react_repair_stability_gate_v1 import evaluate_repair_stability

report = evaluate_repair_stability(
    dataset_path=root / "agent/evaluation/assets/used_phone_react_generalization_v1_20260826/public/scenarios.jsonl",
    run_roots=[
        root / ".runtime/react-v0-generalization-repair-stability-001-react_v0",
        root / ".runtime/react-v0-generalization-repair-stability-002-react_v0",
        root / ".runtime/react-v0-generalization-repair-stability-003-react_v0",
    ],
)
print(json.dumps(report, ensure_ascii=False))
raise SystemExit(0 if report["status"] == "ACCEPT_REPAIR_STABILITY" else 1)
'@
    Push-Location $ProjectRoot
    try {
        $GateOutput = & $Python -c $GateCode $ProjectRoot
        $GateExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($GateExit -eq 0) {
        $GateStatus = ($GateOutput | ConvertFrom-Json).status
    } else {
        $GateStatus = "HOLD"
        $Failures.Add("repair_stability_gate_failed")
    }
}

$Hashes = @()
foreach ($RelativePath in $RequiredFiles) {
    $FullPath = Join-Path $ProjectRoot $RelativePath
    if (Test-Path -LiteralPath $FullPath -PathType Leaf) {
        $Hashes += [ordered]@{
            path = $RelativePath
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $FullPath).Hash.ToLower()
        }
    }
}

$Result = [ordered]@{
    schemaVersion = "react-v0-repair-readiness-v1"
    status = if ($Failures.Count -ne 0) {
        "HOLD"
    } elseif ($SkipTests) {
        "READY_ARTIFACTS_ONLY"
    } else {
        "READY_FOR_NEW_BLIND_GATE"
    }
    scope = "repair_artifacts_and_development_evidence_only"
    declaredDefaultRuntime = if ($DeclaredDefaultFixed) { "fixed_v1" } else { "unknown" }
    compileExitCode = $CompileExit
    relevantTests = [ordered]@{
        skipped = [bool]$SkipTests
        exitCode = $TestExit
    }
    stabilityGateStatus = $GateStatus
    ports = $PortStatus
    artifacts = $Hashes
    failureCount = $Failures.Count
    failures = @($Failures)
    claimBoundary = [ordered]@{
        requiresNewIndependentDataset = $true
        provesReactSuperiority = $false
        authorizesDefaultRuntimeSwitch = $false
    }
}
$Result | ConvertTo-Json -Depth 8
if ($Failures.Count -ne 0) {
    exit 1
}
