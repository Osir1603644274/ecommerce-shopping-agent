$ErrorActionPreference = 'Stop'
$stageRoot = 'D:/agent-datasets/search-stage1-v1'
$stageSessions = @(Get-ChildItem -LiteralPath 'C:/Users/ming/.codex/sessions/2026/09/09' -File -Filter '*.jsonl')
$stageModels = @{}
foreach ($stageDispatchFile in (Get-ChildItem -LiteralPath "$stageRoot/labeling/provenance" -Filter 'dispatch-*.json')) {
    $stageDispatch = Get-Content -LiteralPath $stageDispatchFile.FullName -Raw -Encoding utf8 | ConvertFrom-Json
    foreach ($stagePacket in $stageDispatch.packets) {
        if (-not $stagePacket.local_directory) { continue }
        $stageMatches = @($stageSessions | Where-Object { $_.Name.EndsWith($stagePacket.thread_id + '.jsonl') })
        if ($stageMatches.Count -ne 1) { continue }
        $stageStream = [System.IO.File]::Open($stageMatches[0].FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $stageReader = [System.IO.StreamReader]::new($stageStream)
        try {
            while (($stageLine = $stageReader.ReadLine()) -ne $null) {
                $stageRecord = $stageLine | ConvertFrom-Json
                if ($stageRecord.type -eq 'turn_context') {
                    $stageHash = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($stageLine))).ToLowerInvariant()
                    $stageModels[$stagePacket.thread_id] = @{
                        model = $stageRecord.payload.model
                        reasoning_effort = $stageRecord.payload.effort
                        source_thread_id = $stagePacket.thread_id
                        session_path = $stageMatches[0].FullName
                        first_turn_context_line_sha256 = $stageHash
                    }
                    break
                }
            }
        } finally { $stageReader.Dispose() }
    }
}
$stageModels | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "$stageRoot/labeling/provenance/judge-models.json" -Encoding utf8
"Recorded model metadata for $($stageModels.Count) independent task contexts."
