param(
    [string]$Source = ".runtime/react-v0-final-3x-paired-20260826/blind-review/public-items.jsonl",
    [string]$Output = "docs/archive/experiments/react-v0/REACT_V0_SECOND_HUMAN_BLIND_REVIEW_PACKET_2026-08-26.md",
    [int]$ExpectedItemCount = 9,
    [string]$Title = "二手手机助手匿名 A/B 独立评审包",
    [switch]$RedactArchitectureLabels
)

$ErrorActionPreference = "Stop"

if (Test-Path -LiteralPath $Output) {
    throw "Refusing to overwrite existing review packet: $Output"
}

$items = @(
    Get-Content -LiteralPath $Source -Encoding utf8 |
        Where-Object { $_.Trim() } |
        ForEach-Object { $_ | ConvertFrom-Json }
)

if ($items.Count -ne $ExpectedItemCount) {
    throw "Expected exactly $ExpectedItemCount public review items, got $($items.Count)"
}

$builder = [System.Text.StringBuilder]::new()
$architectureLabelRedactionCount = 0

function Add-Line([string]$Text = "") {
    [void]$builder.AppendLine($Text)
}

function Add-Conversation($Messages) {
    foreach ($message in $Messages) {
        $label = if ($message.role -eq "user") { "用户" } else { "助手" }
        $content = [string]$message.content
        if ($RedactArchitectureLabels) {
            $matches = [regex]::Matches($content, "(?i)ReAct")
            $script:architectureLabelRedactionCount += $matches.Count
            $content = [regex]::Replace($content, "(?i)ReAct", "系统")
        }
        Add-Line "**$label：**"
        Add-Line
        foreach ($line in ($content -split "`r?`n")) {
            Add-Line "> $line"
        }
        Add-Line
    }
}

Add-Line "# $Title"
Add-Line
Add-Line "## 评审纪律"
Add-Line
Add-Line "- 请独立完成，不询问候选对应的系统或架构。"
Add-Line "- 不要查看 sealed mapping、源码、运行记录或此前评审结论。"
Add-Line "- A/B 顺序已打乱；只评价对话本身。"
Add-Line "- 每项均需评价完整多轮对话，不只看最后一句。"
Add-Line '- 若信息不足以判断，选择 `unjudgeable`，不要猜测。'
Add-Line
Add-Line "## 评分标准"
Add-Line
Add-Line "每个候选按四个维度分别评分 1–5："
Add-Line
Add-Line "1. **约束忠实**：是否保留预算、系统、成色等硬条件；是否避免静默放宽。"
Add-Line "2. **证据纪律**：是否区分已知、未知和未验证信息；是否避免把标题、推测或缺失数据说成事实。"
Add-Line "3. **任务推进**：是否正确理解上下文、更新条件、处理旧候选范围，并推动用户完成下一步。"
Add-Line "4. **实用性**：答案是否清楚、相关、可执行，并在现有证据范围内真正帮助选择。"
Add-Line
Add-Line "统一分值锚点："
Add-Line
Add-Line "- **5**：完整满足，基本无可见问题。"
Add-Line "- **4**：总体很好，存在轻微问题但不改变主要判断。"
Add-Line "- **3**：优缺点并存，部分满足。"
Add-Line "- **2**：存在明显问题，显著影响可信度或可用性。"
Add-Line "- **1**：严重违反该维度要求。"
Add-Line
Add-Line '每项还需填写总体偏好：`A`、`B`、`tie` 或 `unjudgeable`。总体偏好不要求机械服从四项总分，但应基于完整对话。'
Add-Line

for ($index = 0; $index -lt $items.Count; $index++) {
    $item = $items[$index]
    $number = $index + 1
    Add-Line "---"
    Add-Line
    Add-Line "## 第 $number 项"
    Add-Line
    Add-Line "### 候选 A"
    Add-Line
    Add-Conversation $item.candidateA
    Add-Line "### 候选 B"
    Add-Line
    Add-Conversation $item.candidateB
    Add-Line "### 本项评分"
    Add-Line
    Add-Line "| 候选 | 约束忠实 | 证据纪律 | 任务推进 | 实用性 |"
    Add-Line "|---|---:|---:|---:|---:|"
    Add-Line "| A |  |  |  |  |"
    Add-Line "| B |  |  |  |  |"
    Add-Line
    Add-Line "总体偏好："
    Add-Line
}

Add-Line "---"
Add-Line
Add-Line "## 集中作答模板"
Add-Line
Add-Line '```text'
for ($number = 1; $number -le $items.Count; $number++) {
    Add-Line "$number. A：__/__/__/__；B：__/__/__/__；总体：A/B/tie/unjudgeable"
}
Add-Line '```'

$outputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$outputDirectory = [System.IO.Path]::GetDirectoryName($outputPath)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
[System.IO.File]::WriteAllText($outputPath, $builder.ToString(), [System.Text.UTF8Encoding]::new($false))

$sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
$outputHash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLowerInvariant()

[pscustomobject]@{
    status = "ok"
    itemCount = $items.Count
    source = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Source))
    sourceSha256 = $sourceHash
    output = $outputPath
    outputSha256 = $outputHash
    sealedMappingRead = $false
    architectureLabelsRedacted = [bool]$RedactArchitectureLabels
    architectureLabelRedactionCount = $architectureLabelRedactionCount
} | ConvertTo-Json
